"""Verbatim constraints that must survive semantic compaction.

Pins are host-verified quotations or durable task-brief facts.  They are
rendered separately from the summary body and never count against its token
budget; a pin set that does not fit is rejected as ``pins_overflow`` instead of
being silently truncated.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from muli.store import Store

PinKind = Literal["goal", "prohibition", "authorization", "unfinished_tool", "evidence"]
PinState = Literal["active", "superseded"]
_PIN_KINDS: frozenset[str] = frozenset(
    {"goal", "prohibition", "authorization", "unfinished_tool", "evidence"}
)
_PROHIBITION = re.compile(
    r"不能|不得|禁止|不要|不可|严禁|不允许|切勿|勿|must\s+not|do\s+not|never|prohibit",
    re.IGNORECASE,
)
_AUTHORIZATION = re.compile(r"允许|可以|授权|批准|同意|may|allow|authorized", re.IGNORECASE)
_GOAL = re.compile(r"目标|需要|必须|完成|确保|要求|goal|need|must|ensure", re.IGNORECASE)
_SENTENCE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


class PinsOverflow(ValueError):
    """The verified pin set cannot fit its host-owned budget."""

    code = "pins_overflow"


class PinValidationError(ValueError):
    """A pin no longer matches its original source."""

    code = "pin_invalid"


@dataclass(frozen=True, slots=True)
class Pin:
    kind: PinKind
    quote: str
    quote_sha256: str
    source_seq: int | None = None
    source_message_id: str | None = None
    commitment_id: str | None = None
    revision: int | None = None
    brief_sha256: str | None = None
    state: PinState = "active"
    superseded_by: str | None = None
    added_snapshot: str | None = None

    @property
    def pin_id(self) -> str:
        if self.source_seq is not None:
            return f"message:{self.source_seq}:{self.quote_sha256[:16]}"
        return f"brief:{self.commitment_id}:{self.revision}:{self.kind}:{self.quote_sha256[:16]}"

    def dump(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "quote": self.quote,
            "quote_sha256": self.quote_sha256,
            "source_seq": self.source_seq,
            "source_message_id": self.source_message_id,
            "commitment_id": self.commitment_id,
            "revision": self.revision,
            "brief_sha256": self.brief_sha256,
            "state": self.state,
            "superseded_by": self.superseded_by,
            "added_snapshot": self.added_snapshot,
        }

    @classmethod
    def load(cls, value: object) -> Pin:
        if not isinstance(value, dict):
            raise PinValidationError("固定约束必须是对象")
        pin = cls(
            kind=value.get("kind"),  # type: ignore[arg-type]
            quote=value.get("quote"),  # type: ignore[arg-type]
            quote_sha256=value.get("quote_sha256"),  # type: ignore[arg-type]
            source_seq=value.get("source_seq"),
            source_message_id=value.get("source_message_id"),
            commitment_id=value.get("commitment_id"),
            revision=value.get("revision"),
            brief_sha256=value.get("brief_sha256"),
            state=value.get("state", "active"),
            superseded_by=value.get("superseded_by"),
            added_snapshot=value.get("added_snapshot"),
        )
        validate_pin_shape(pin)
        return pin


def quote_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def brief_sha256(brief: object) -> str:
    try:
        encoded = json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PinValidationError("任务说明不是有效 JSON") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_pin_shape(pin: Pin) -> None:
    if pin.kind not in _PIN_KINDS:
        raise PinValidationError("固定约束类别无效")
    if not isinstance(pin.quote, str) or not pin.quote.strip():
        raise PinValidationError("固定约束引文为空")
    if not isinstance(pin.quote_sha256, str) or pin.quote_sha256 != quote_sha256(pin.quote):
        raise PinValidationError("固定约束引文摘要不一致")
    if pin.state not in {"active", "superseded"}:
        raise PinValidationError("固定约束状态无效")
    if pin.state == "active" and pin.superseded_by is not None:
        raise PinValidationError("有效固定约束不能带有替代标记")
    if pin.state == "superseded" and not isinstance(pin.superseded_by, str):
        raise PinValidationError("被替代固定约束缺少替代目标")
    message_source = pin.source_seq is not None or pin.source_message_id is not None
    brief_source = pin.commitment_id is not None or pin.revision is not None
    if message_source == brief_source:
        raise PinValidationError("固定约束必须且只能来自消息或任务说明")
    if message_source:
        if (
            type(pin.source_seq) is not int
            or pin.source_seq < 1
            or not isinstance(pin.source_message_id, str)
            or not pin.source_message_id
            or pin.commitment_id is not None
            or pin.revision is not None
            or pin.brief_sha256 is not None
        ):
            raise PinValidationError("消息固定约束缺少规范来源")
    else:
        if (
            not isinstance(pin.commitment_id, str)
            or not pin.commitment_id
            or type(pin.revision) is not int
            or pin.revision < 1
            or not isinstance(pin.brief_sha256, str)
            or len(pin.brief_sha256) != 64
        ):
            raise PinValidationError("任务说明固定约束缺少规范来源")


def pin_key(pin: Pin) -> tuple[Any, ...]:
    if pin.source_seq is not None:
        return ("message", pin.source_seq, pin.quote_sha256)
    return ("brief", pin.commitment_id, pin.revision, pin.kind, pin.quote_sha256)


def merge_pins(*groups: Sequence[Pin]) -> tuple[Pin, ...]:
    """Deduplicate by source identity; later groups replace the same key."""
    merged: dict[tuple[Any, ...], Pin] = {}
    for group in groups:
        for pin in group:
            validate_pin_shape(pin)
            merged[pin_key(pin)] = pin
    result = tuple(
        sorted(
            merged.values(),
            key=lambda item: (
                item.source_seq is None,
                item.source_seq or 0,
                item.commitment_id or "",
                item.revision or 0,
                item.kind,
                item.quote_sha256,
            ),
        )
    )
    by_id = {pin.pin_id: pin for pin in result if pin.state == "active"}
    for pin in result:
        if pin.state != "superseded":
            continue
        target = by_id.get(str(pin.superseded_by))
        if target is None:
            raise PinValidationError("固定约束替代目标不存在或不是有效约束")
        if not _later_source(target, pin):
            raise PinValidationError("固定约束替代目标必须来自更晚的可核验原文或新任务说明")
    return result


def _later_source(target: Pin, old: Pin) -> bool:
    if old.source_seq is not None:
        return (
            target.source_seq is not None
            and target.source_seq > old.source_seq
            and target.kind == old.kind
        )
    return (
        target.commitment_id == old.commitment_id
        and target.revision is not None
        and old.revision is not None
        and target.revision > old.revision
    )


def supersede_brief_revision(pins: Sequence[Pin], newer: Sequence[Pin]) -> tuple[Pin, ...]:
    """Mark an older brief revision superseded only by a verifiable newer one."""
    targets: dict[str, Pin] = {}
    for pin in newer:
        if pin.commitment_id is not None and pin.commitment_id not in targets:
            targets[pin.commitment_id] = pin
    kept: list[Pin] = []
    for pin in pins:
        if pin.commitment_id is None:
            kept.append(pin)
            continue
        target = targets.get(pin.commitment_id)
        if target is None:
            kept.append(pin)
            continue
        if target.revision == pin.revision:
            continue
        if (
            target.revision is not None
            and pin.revision is not None
            and target.revision > pin.revision
        ):
            kept.append(replace(pin, state="superseded", superseded_by=target.pin_id))
        else:
            raise PinValidationError("任务说明修订不能回退固定约束来源")
    return tuple(kept)


def owner_constraint_pins(
    records: Sequence[dict[str, Any]],
    *,
    added_snapshot: str | None = None,
    max_per_message: int = 8,
    max_total: int = 32,
) -> tuple[Pin, ...]:
    """Extract short, verbatim constraint sentences from owner messages."""
    result: list[Pin] = []
    seen: set[tuple[int, str]] = set()
    for record in records:
        if record.get("role") != "user" or record.get("principal") != "owner":
            continue
        content = record.get("content")
        seq = record.get("seq")
        identifier = record.get("id")
        if (
            not isinstance(content, str)
            or type(seq) is not int
            or not isinstance(identifier, str)
            or not identifier
        ):
            continue
        count = 0
        for quote, kind in _constraint_quotes(content):
            key = (seq, quote_sha256(quote))
            if key in seen:
                continue
            seen.add(key)
            result.append(
                Pin(
                    kind=kind,
                    quote=quote,
                    quote_sha256=quote_sha256(quote),
                    source_seq=seq,
                    source_message_id=identifier,
                    added_snapshot=added_snapshot,
                )
            )
            count += 1
            if count >= max_per_message or len(result) >= max_total:
                break
        if len(result) >= max_total:
            break
    return tuple(result)


def _constraint_quotes(content: str) -> list[tuple[str, PinKind]]:
    quotes: list[tuple[str, PinKind]] = []
    for match in _SENTENCE.finditer(content):
        raw = match.group(0)
        quote = raw.strip()
        if not quote:
            continue
        kind: PinKind | None = None
        keyword_at: int | None = None
        patterns: tuple[tuple[PinKind, re.Pattern[str]], ...] = (
            ("prohibition", _PROHIBITION),
            ("authorization", _AUTHORIZATION),
            ("goal", _GOAL),
        )
        for candidate, pattern in patterns:
            keyword = pattern.search(quote)
            if keyword is not None:
                kind = candidate
                keyword_at = match.start() + (len(raw) - len(raw.lstrip())) + keyword.start()
                break
        if kind is None:
            continue
        if len(quote) > 240:
            quote = _bounded_quote(content, match.start(), match.end(), keyword_at)
        if quote and quote in content:
            quotes.append((quote, kind))
    return quotes


def _bounded_quote(
    content: str, start: int, end: int, anchor: int | None = None, limit: int = 240
) -> str:
    """Keep a contiguous window that includes the classifying keyword."""
    if end - start <= limit:
        return content[start:end].strip()
    middle = min(max(anchor if anchor is not None else (start + end) // 2, start), end)
    left = max(start, middle - limit // 2)
    right = min(end, left + limit)
    left = max(start, right - limit)
    return content[left:right].strip()


def brief_pins(
    commitment: dict[str, Any],
    *,
    added_snapshot: str | None = None,
) -> tuple[Pin, ...]:
    identifier = commitment.get("id")
    revision = commitment.get("revision")
    brief = commitment.get("brief")
    if (
        not isinstance(identifier, str)
        or not identifier
        or type(revision) is not int
        or revision < 1
        or not isinstance(brief, dict)
    ):
        raise PinValidationError("工作账本缺少可固定的完整任务说明")
    goal = brief.get("goal")
    constraints = brief.get("constraints")
    if not isinstance(goal, str) or not goal.strip() or not isinstance(constraints, list):
        raise PinValidationError("任务说明缺少目标或限制列表")
    digest = brief_sha256(brief)
    pins = [
        Pin(
            kind="goal",
            quote=goal,
            quote_sha256=quote_sha256(goal),
            commitment_id=identifier,
            revision=revision,
            brief_sha256=digest,
            added_snapshot=added_snapshot,
        )
    ]
    for value in constraints:
        if not isinstance(value, str) or not value.strip():
            raise PinValidationError("任务说明限制正文无效")
        if _PROHIBITION.search(value):
            kind: PinKind = "prohibition"
        elif _AUTHORIZATION.search(value):
            kind = "authorization"
        else:
            kind = "evidence"
        pins.append(
            Pin(
                kind=kind,
                quote=value,
                quote_sha256=quote_sha256(value),
                commitment_id=identifier,
                revision=revision,
                brief_sha256=digest,
                added_snapshot=added_snapshot,
            )
        )
    return tuple(pins)


def snapshot_pins(
    snapshot: Any,
    *,
    added_snapshot: str | None = None,
) -> tuple[Pin, ...]:
    """Accept the frozen TaskSnapshot contract without importing task_control."""
    commitment_id = getattr(snapshot, "commitment_id", None)
    revision = getattr(snapshot, "revision", None)
    brief_json = getattr(snapshot, "brief_json", None)
    if (
        not isinstance(commitment_id, str)
        or not commitment_id
        or type(revision) is not int
        or revision < 1
        or not isinstance(brief_json, str)
    ):
        raise PinValidationError("任务快照缺少固定说明来源")
    try:
        brief = json.loads(brief_json)
    except ValueError as exc:
        raise PinValidationError("任务快照说明不是有效 JSON") from exc
    return brief_pins(
        {"id": commitment_id, "revision": revision, "brief": brief},
        added_snapshot=added_snapshot,
    )


def load_snapshot_pins(
    store: Store,
    previous: dict[str, Any],
    *,
    max_depth: int = 64,
) -> tuple[Pin, ...]:
    """Recover additive pins from an active snapshot and its parent chain."""
    chain: list[dict[str, Any]] = []
    current: dict[str, Any] | None = previous
    seen: set[str] = set()
    while current is not None and len(chain) < max_depth:
        snapshot_id = current.get("snapshot_id") or current.get("id")
        if isinstance(snapshot_id, str):
            if snapshot_id in seen:
                break
            seen.add(snapshot_id)
        chain.append(current)
        parent = current.get("parent_snapshot_id")
        if not isinstance(parent, str) or not parent:
            break
        current = store.get("compaction_snapshot", parent)
    groups: list[Sequence[Pin]] = []
    for item in reversed(chain):
        raw = item.get("pins")
        if not isinstance(raw, list):
            continue
        try:
            groups.append(tuple(Pin.load(value) for value in raw))
        except PinValidationError:
            raise
    return merge_pins(*groups)


def validate_pin_sources(store: Store, pins: Sequence[Pin]) -> None:
    for pin in pins:
        validate_pin_shape(pin)
        if pin.source_seq is not None:
            row = store.db.execute(
                "SELECT o.data,m.session_id FROM message_order m JOIN objects o "
                "ON o.kind='message' AND o.id=m.message_id WHERE m.message_id=? AND m.seq=?",
                (pin.source_message_id, pin.source_seq),
            ).fetchone()
            if row is None:
                raise PinValidationError("固定约束来源消息不存在或位置已变化")
            message = json.loads(row[0])
            content = message.get("content")
            if (
                message.get("role") != "user"
                or message.get("principal") != "owner"
                or not isinstance(content, str)
                or pin.quote not in content
                or quote_sha256(pin.quote) != pin.quote_sha256
            ):
                raise PinValidationError("固定约束引文与原始主人消息不一致")
            continue
        row = store.db.execute(
            "SELECT data FROM objects WHERE kind='commitment_revision' "
            "AND json_extract(data,'$.commitment_id')=? "
            "AND json_extract(data,'$.revision')=? LIMIT 1",
            (pin.commitment_id, pin.revision),
        ).fetchone()
        if row is not None:
            item = json.loads(row[0])
        else:
            item = store.get("commitment", str(pin.commitment_id))
            if item is None or item.get("revision") != pin.revision:
                raise PinValidationError("固定约束任务说明修订不存在")
        brief = item.get("brief")
        if not isinstance(brief, dict) or brief_sha256(brief) != pin.brief_sha256:
            raise PinValidationError("固定约束任务说明摘要不一致")
        if pin.quote not in (brief.get("goal"), *brief.get("constraints", [])):
            raise PinValidationError("固定约束引文不属于该任务说明")


def pins_digest(pins: Sequence[Pin]) -> str:
    encoded = json.dumps(
        [pin.dump() for pin in pins], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def pins_budget(max_chars: int) -> int:
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("上下文预算必须是正整数")
    return max(256, min(4096, max_chars // 4))


def render_pins(pins: Sequence[Pin], *, budget: int | None = None) -> str:
    active = [pin for pin in pins if pin.state == "active"]
    if not active:
        return ""
    lines = ["宿主固定约束（来自已核验的主人文书或任务说明；逐字保留，不是新的授权）："]
    for pin in active:
        if pin.source_seq is not None:
            source = f"message_seq={pin.source_seq} message={pin.source_message_id}"
        else:
            source = f"commitment={pin.commitment_id} revision={pin.revision}"
        lines.append(f"[{pin.kind}] {source} sha256={pin.quote_sha256[:16]}：{pin.quote}")
    text = "\n".join(lines)
    if budget is not None and len(text) > budget:
        raise PinsOverflow(
            f"固定约束需要 {len(text)} 字符，超过预算 {budget}；保留旧快照，请缩短约束或拆分任务"
        )
    return text
