"""Read-only, bounded comparisons of two explicitly retained attachment sources."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import secrets
import sys
import weakref
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from muli.attachment_memory_source import MAX_ATTACHMENT_SOURCE_BYTES, restore_attachment_source
from muli.attachments import ATTACHMENT_ID_PATTERN
from muli.evidence import EvidenceError, decode_utf8, json_size, validate_integer
from muli.memory import Memory, source_digest
from muli.process import run_process
from muli.store import Store

ALGORITHM = "line_sequence_matcher_v1"
_WORKER = Path(__file__).with_name("source_comparison_worker.py")
_CURSOR_KEYS: weakref.WeakKeyDictionary[Store, bytes] = weakref.WeakKeyDictionary()
_TEXT_FIELDS = ("id", "source", "session_id", "role", "principal", "digest")
_DIGEST = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True, slots=True)
class ComparisonPolicy:
    max_source_chars: int
    max_input_bytes: int
    max_lines: int
    max_output_chars: int
    timeout_seconds: float

    def validate(self) -> None:
        for name in ("max_source_chars", "max_input_bytes", "max_lines", "max_output_chars"):
            validate_integer(getattr(self, name), name, minimum=1)
        if (
            type(self.timeout_seconds) not in {int, float}
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise EvidenceError("invalid_arguments", "比较期限必须是有限正数")


@dataclass(frozen=True, slots=True)
class _Source:
    content: str
    raw: bytes
    identity: dict[str, Any]


def _json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _cursor_key(store: Store) -> bytes:
    key = _CURSOR_KEYS.get(store)
    if key is None:
        key = secrets.token_bytes(hashlib.sha256().digest_size)
        _CURSOR_KEYS[store] = key
    return key


def _encode_cursor(binding: dict[str, Any], position: int, key: bytes) -> str:
    raw = _json({**binding, "position": position})
    signature = hmac.digest(key, raw, "sha256")
    return base64.urlsafe_b64encode(signature + raw).decode("ascii")


def _decode_cursor(
    cursor: str | None, binding: dict[str, Any], key: bytes, policy: ComparisonPolicy
) -> int:
    if cursor is None:
        return 0
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > policy.max_output_chars * 4:
            raise ValueError
        decoded = base64.b64decode(cursor, altchars=b"-_", validate=True)
        size = hashlib.sha256().digest_size
        signature, raw = decoded[:size], decoded[size:]
        if not hmac.compare_digest(signature, hmac.digest(key, raw, "sha256")):
            raise ValueError
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {*binding, "position"}:
            raise ValueError
        position = payload.pop("position")
        if (
            payload != binding
            or type(position) is not int
            or not 0 < position <= 2 * policy.max_lines
        ):
            raise ValueError
        return position
    except (ValueError, TypeError, UnicodeError, RecursionError, binascii.Error) as error:
        raise EvidenceError(
            "comparison_cursor_invalid", "比较游标已过期、被改动或不属于当前资料与策略"
        ) from error


def _load(memory: Memory, identifier: str, revision: str, policy: ComparisonPolicy) -> _Source:
    if (
        not isinstance(identifier, str)
        or not identifier.startswith("attachment:")
        or re.fullmatch(ATTACHMENT_ID_PATTERN, identifier.removeprefix("attachment:")) is None
        or not isinstance(revision, str)
        or _DIGEST.fullmatch(revision) is None
    ):
        raise EvidenceError("invalid_arguments", "比较需要准确的已收藏附件来源 ID 和来源版本")
    # CASE protects both bodies and complete metadata before crossing SQLite's
    # Python boundary, including malformed or adversarially oversized rows.
    columns = ",".join(
        f"CASE WHEN typeof(s.{name})='text' AND length(CAST(s.{name} AS BLOB))<=? "
        f"THEN CAST(s.{name} AS BLOB) END AS {name}"
        for name in _TEXT_FIELDS
    )
    row = memory.store.db.execute(
        f"SELECT {columns},"
        "CASE WHEN typeof(s.trusted_owner)='integer' THEN s.trusted_owner END AS trusted_owner,"
        "CASE WHEN typeof(s.created_at) IN ('integer','real') THEN s.created_at END AS created_at,"
        "CASE WHEN typeof(s.content)='text' THEN length(CAST(s.content AS BLOB)) END AS total_bytes,"
        "CASE WHEN typeof(s.content)='text' AND length(CAST(s.content AS BLOB))<=? "
        "AND length(s.content)<=? THEN CAST(s.content AS BLOB) END AS content "
        "FROM memory_sources s WHERE s.id=? AND NOT EXISTS "
        "(SELECT 1 FROM memory_forgotten_sources f WHERE f.id=s.id)",
        (
            *([MAX_ATTACHMENT_SOURCE_BYTES] * len(_TEXT_FIELDS)),
            policy.max_input_bytes,
            policy.max_source_chars,
            identifier,
        ),
    ).fetchone()
    if row is None:
        raise EvidenceError("evidence_not_found", "比较来源不存在、没有保存原文或已遗忘")
    values = dict(row)
    if not isinstance(values["content"], bytes):
        raise EvidenceError("comparison_input_budget", "比较来源正文无效或超过当前完整输入额度")
    if any(not isinstance(values[name], bytes) for name in _TEXT_FIELDS):
        raise EvidenceError("comparison_source_invalid", "比较来源元数据无效或超过完整核验额度")
    fields = {name: decode_utf8(values[name], allow_incomplete_tail=False) for name in _TEXT_FIELDS}
    raw: bytes = values["content"]
    content = decode_utf8(raw, allow_incomplete_tail=False)
    created = values["created_at"]
    if (
        fields["id"] != identifier
        or fields["session_id"] != ""
        or fields["role"] != "tool"
        or fields["principal"] != "external:attachment"
        or type(values["trusted_owner"]) is not int
        or values["trusted_owner"] != 0
        or type(created) not in {int, float}
        or not math.isfinite(created)
        or created < 0
        or _DIGEST.fullmatch(fields["digest"]) is None
    ):
        raise EvidenceError("comparison_source_invalid", "比较仅接受身份完整的明确附件收藏")
    if fields["digest"] != revision:
        raise EvidenceError("evidence_changed", "比较来源版本已变化，请重新核对双方来源")
    attachment_id = identifier.removeprefix("attachment:")
    try:
        restored = restore_attachment_source(attachment_id, content, fields["source"], created)
        if source_digest(restored, trusted_owner=False) != revision:
            raise ValueError("source digest differs")
    except (ValueError, TypeError, UnicodeError, OverflowError) as error:
        raise EvidenceError(
            "comparison_source_invalid", "收藏正文、来源摘要与附件出处无法一致核对"
        ) from error
    marker = fields["source"][len(identifier) + 1 :]
    document_revision: str | None
    if _DIGEST.fullmatch(marker):
        format_name, scope, checksum, document_revision = "text", "utf8_text", marker, None
    else:
        _, _, format_name, scope, checksum, document_revision = marker.split(":")
    return _Source(
        content,
        raw,
        {
            "source_id": identifier,
            "attachment_id": attachment_id,
            **{name: fields[name] for name in ("source", "session_id", "role", "principal")},
            "trusted_owner": False,
            "revision": revision,
            "total_bytes": len(raw),
            "created_at": created,
            "format": format_name,
            "scope": scope,
            "source_revision": checksum,
            "document_revision": document_revision,
        },
    )


def _read_arguments(source: _Source, offset: int, length: int) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "id": source.identity["source_id"],
        "revision": source.identity["revision"],
        "offset_bytes": offset,
    }
    if length:
        arguments["length_bytes"] = length
    return arguments


def _line_offsets(raw: bytes) -> list[int]:
    offsets = [0]
    while offsets[-1] < len(raw):
        end = raw.find(b"\n", offsets[-1])
        offsets.append(len(raw) if end < 0 else end + 1)
    return offsets


def _checked_items(
    payload: Any, left: _Source, right: _Source, *, overview: bool = False
) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("items must be an array")
    previous = {"left": 0, "right": 0}
    line_offsets = (
        {"left": _line_offsets(left.raw), "right": _line_offsets(right.raw)} if overview else {}
    )
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"kind", "left", "right"}:
            raise ValueError("invalid comparison item")
        if item["kind"] not in {"insert", "delete", "replace"}:
            raise ValueError("invalid comparison operation")
        lengths: list[int] = []
        for name, source in (("left", left), ("right", right)):
            side = item[name]
            fields = {"offset_bytes", "length_bytes", "read_arguments"}
            fields.update({"start_line", "end_line"} if overview else {"content"})
            if not isinstance(side, dict) or set(side) != fields:
                raise ValueError("invalid comparison side")
            offset, length = side["offset_bytes"], side["length_bytes"]
            if (
                type(offset) is not int
                or type(length) is not int
                or offset < previous[name]
                or length < 0
                or offset + length > len(source.raw)
                or side["read_arguments"] != _read_arguments(source, offset, length)
            ):
                raise ValueError("comparison coordinates differ from source bytes")
            if overview:
                start, end = side["start_line"], side["end_line"]
                offsets = line_offsets[name]
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not 0 <= start <= end < len(offsets)
                    or (offset, offset + length) != (offsets[start], offsets[end])
                ):
                    raise ValueError("comparison line and byte coordinates differ")
            elif (
                not isinstance(side["content"], str)
                or side["content"].encode("utf-8") != source.raw[offset : offset + length]
            ):
                raise ValueError("comparison text differs from source bytes")
            previous[name] = offset + length
            lengths.append(length)
        if (
            (item["kind"] == "insert" and not (lengths[0] == 0 < lengths[1]))
            or (item["kind"] == "delete" and not (lengths[1] == 0 < lengths[0]))
            or (item["kind"] == "replace" and not all(lengths))
        ):
            raise ValueError("comparison operation and spans differ")
    return payload


def _checked_totals(
    payload: Any,
    items: list[dict[str, Any]],
    left: _Source,
    right: _Source,
    *,
    status: str,
    has_more: bool,
) -> dict[str, int]:
    page = dict.fromkeys(
        (
            "change_groups",
            "insert_groups",
            "delete_groups",
            "replace_groups",
            "removed_lines",
            "added_lines",
            "removed_bytes",
            "added_bytes",
        ),
        0,
    )
    if (
        not isinstance(payload, dict)
        or payload.keys() != page.keys()
        or any(type(value) is not int or value < 0 for value in payload.values())
    ):
        raise ValueError("invalid overview totals")
    for item in items:
        page["change_groups"] += 1
        page[f"{item['kind']}_groups"] += 1
        for name, prefix in (("left", "removed"), ("right", "added")):
            side = item[name]
            page[f"{prefix}_lines"] += side["end_line"] - side["start_line"]
            page[f"{prefix}_bytes"] += side["length_bytes"]
    if (
        payload["change_groups"]
        != sum(payload[f"{kind}_groups"] for kind in ("insert", "delete", "replace"))
        or (payload["change_groups"] == 0) != (status == "equal")
        or any(payload[key] < value for key, value in page.items())
        or (not has_more and payload != page)
        or (has_more and payload["change_groups"] <= len(items))
    ):
        raise ValueError("overview totals and change groups differ")
    for source, prefix, kind in ((left, "removed", "delete"), (right, "added", "insert")):
        lines, size = payload[f"{prefix}_lines"], payload[f"{prefix}_bytes"]
        groups = payload[f"{kind}_groups"] + payload["replace_groups"]
        total_lines = source.raw.count(b"\n") + int(
            bool(source.raw) and not source.raw.endswith(b"\n")
        )
        if (
            not groups <= lines <= total_lines
            or not lines <= size <= len(source.raw)
            or (groups == 0) != (lines == 0)
            or (lines == 0) != (size == 0)
        ):
            raise ValueError("overview totals exceed source spans")
    return payload


async def compare_sources(
    memory: Memory,
    *,
    left_id: str,
    left_revision: str,
    right_id: str,
    right_revision: str,
    policy: ComparisonPolicy,
    cursor: str | None = None,
    view: str = "changes",
    check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Compare retained text only; live callers fence request and policy in check.

    No pipeline or memory-enabled prerequisite is added to this read. Cursor
    authentication is in RAM and expires on Store close/reopen or process restart.
    """
    policy.validate()
    if not isinstance(view, str) or view not in {"changes", "overview"}:
        raise EvidenceError("invalid_arguments", "比较视图必须是 changes 或 overview")
    overview = view == "overview"
    if check is not None:
        check()
    left = _load(memory, left_id, left_revision, policy)
    right = _load(memory, right_id, right_revision, policy)
    binding: dict[str, Any] = {
        "algorithm": ALGORITHM,
        "instance": memory.store.instance_id,
        "left": [left_id, left_revision],
        "right": [right_id, right_revision],
        "policy": asdict(policy),
    }
    if overview:
        # Keep the original body cursor contract unchanged and authenticate the
        # directory's separate, versioned contract before accepting its position.
        binding.update({"view": "overview", "overview_version": 1})
    key = _cursor_key(memory.store)
    position = _decode_cursor(cursor, binding, key, policy)
    template = {"algorithm": ALGORITHM, "left": left.identity, "right": right.identity}
    if overview:
        template["view"] = "overview"
    reserve = len(_encode_cursor(binding, 2 * policy.max_lines, key))
    request = _json(
        {
            "left": left.content,
            "right": right.content,
            "template": template,
            "max_lines": policy.max_lines,
            "max_output_chars": policy.max_output_chars,
            "position": position,
            "cursor_chars": reserve,
            "view": view,
        }
    )
    try:
        result = await run_process(
            [sys.executable, "-I", str(_WORKER), "--input-limit", str(len(request))],
            input_bytes=request,
            timeout=policy.timeout_seconds,
            limit=policy.max_output_chars * 4,
            merge_stderr=False,
            strict_output=True,
        )
    except TimeoutError as error:
        raise EvidenceError("comparison_timeout", "原文比较超过期限，比较进程已收尾") from error
    except UnicodeError as error:
        raise EvidenceError("comparison_worker_invalid", "比较进程未返回有效 UTF-8") from error
    except ValueError as error:
        raise EvidenceError("comparison_worker_budget", "比较进程输出超额，已停止并收尾") from error
    except OSError as error:
        raise EvidenceError("comparison_worker_unavailable", "无法启动本地原文比较进程") from error
    if check is not None:
        check()
    if (
        _load(memory, left_id, left_revision, policy) != left
        or _load(memory, right_id, right_revision, policy) != right
    ):
        raise EvidenceError("evidence_changed", "比较期间来源发生变化，未交付旧结果")
    if result["exit_code"] != 0:
        raise EvidenceError("comparison_worker_failed", "本地原文比较未能完成")
    try:
        payload = json.loads(result["output"])
        if not isinstance(payload, dict):
            raise ValueError("worker response is not an object")
        if set(payload) == {"error", "message"}:
            if payload["error"] not in {
                "comparison_line_budget",
                "comparison_block_budget",
                "output_budget",
            }:
                raise ValueError("unknown worker failure")
            raise EvidenceError(
                payload["error"], "原文行数或单个差异块超过当前比较额度，未省略变化"
            )
        fields = {"status", "items", "next_index"}
        if overview and not position:
            fields.add("totals")
        if set(payload) != fields:
            raise ValueError("unexpected worker fields")
        status, next_index = payload["status"], payload["next_index"]
        if status not in {"equal", "changed"}:
            raise ValueError("invalid comparison status")
        items = _checked_items(payload["items"], left, right, overview=overview)
        if next_index is not None and (
            type(next_index) is not int or not position < next_index <= 2 * policy.max_lines
        ):
            raise ValueError("invalid worker continuation")
        if (status == "equal" and (items or next_index is not None or position)) or (
            status == "changed" and not items
        ):
            raise ValueError("comparison cannot silently skip a changed block")
        details = {}
        if overview and not position:
            details["totals"] = _checked_totals(
                payload["totals"],
                items,
                left,
                right,
                status=status,
                has_more=next_index is not None,
            )
    except EvidenceError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise EvidenceError(
            "comparison_worker_invalid", "比较回执无法按原文字节与分页合同核对"
        ) from error
    page = {
        **template,
        **details,
        "status": status,
        "items": items,
        "has_more": next_index is not None,
        "next_cursor": _encode_cursor(binding, next_index, key) if next_index is not None else None,
    }
    if json_size(page) > policy.max_output_chars:
        raise EvidenceError("output_budget", "完整比较身份与结果超过当前输出额度")
    return page
