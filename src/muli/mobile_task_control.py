"""Synchronous, host-owned mobile commands over existing inbound/control receipts.

The caller owns configuration_lock and has just checked current transport and
owner authority. This module neither authenticates platform envelopes nor sends
messages; a short reference never grants permission.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

from muli.evidence import EvidenceError
from muli.reply_origin import MAX_ORIGIN_ID_CHARS, ReplyOrigin
from muli.task_control import (
    ControlConflict,
    ControlResult,
    ControlUnavailable,
    OwnerControlRequest,
    TaskControl,
    _digest,
    _json,
)

_VERSION = 1
_PREFIX_CHARS = 12
_KEY_CHARS = 64
_PAGE_ITEMS = 10
_MAX_COMMAND_CHARS = 64_000
_CHANNELS = frozenset({"telegram", "onebot"})
_MAX_CHANNEL_BYTES = max(len(name) for name in _CHANNELS)
_HEX_KEY = re.compile(rf"[0-9a-f]{{{_KEY_CHARS}}}")
_COMMAND = re.compile(rf"/(暂停|调整) ([0-9a-f]{{{_PREFIX_CHARS}}})-([1-9][0-9]?)(?: ([\s\S]+))?")
_META_FIELDS = {
    "version",
    "source",
    "command_digest",
    "kind",
    "tickets",
    "control_receipt_id",
    "commitment_id",
    "error",
}
_ERROR_TEXT = {
    "invalid_command": "控制指令格式无效，请发送 /任务 并复制完整指令。",
    "reference_invalid": "任务短码缺失、冲突或不可核验，请重新发送 /任务。",
    "epoch_conflict": "任务控制版本已改变，请重新发送 /任务，不会自动改用新版本。",
    "not_paused": "请先暂停任务，再追加重新规划要求。",
    "workflow_active": "该任务含活动工作流，请先停止或等待结束。",
    "model_unconfigured": "尚未配置主模型，任务保持暂停。",
    "queue_full": "主会话入站队列已满，要求尚未提交，任务保持暂停。",
    "output_budget": "状态回执超过当前输出预算，请核对配置后重新发送 /任务。",
    "control_unavailable": "任务或控制资料不可用，请核对后重新发送 /任务。",
}

ControlSubmit = Callable[[str, OwnerControlRequest, int], ControlResult]


class MobileControlRejected(ControlConflict):
    """An explicit admission rejection before this attempt writes control or receipt state."""


@dataclass(frozen=True, slots=True)
class MobileControlSource:
    name: str
    account_id: str
    owner_id: str
    target: str


@dataclass(frozen=True, slots=True)
class MobileControlOutcome:
    receipt: dict[str, Any]
    control_committed: bool


class MobileTaskControl:
    def __init__(self, control: TaskControl) -> None:
        self.control = control
        self.store, self.commitments = control.store, control.commitments

    @property
    def budget(self) -> int:
        return self.commitments.max_output_chars()

    def handle(
        self,
        inbound_id: str,
        source: MobileControlSource,
        text: str,
        *,
        submit_control: ControlSubmit,
    ) -> MobileControlOutcome:
        """Persist a fixed receipt before the Runtime performs its first await."""
        if not _HEX_KEY.fullmatch(inbound_id) or not isinstance(text, str):
            raise ValueError("手机控制入站标识或文本无效")
        self._validate_source(inbound_id, source)
        rows = self._records("channel_inbound.id=?", (inbound_id,))
        if (
            len(rows) != 1
            or rows[0]["name"] != source.name
            or not isinstance(rows[0]["fingerprint"], str)
            or not _HEX_KEY.fullmatch(rows[0]["fingerprint"])
        ):
            raise MobileControlRejected("source_rejected", "手机控制缺少已核验的入站记录")
        digest = _digest(text)
        if rows[0]["receipt_type"] != "null":
            try:
                receipt = self._decode(rows[0], source)
            except ControlConflict as error:
                raise MobileControlRejected(error.code, error.message) from error
            metadata = receipt["mobile_task_control"]
            if metadata["command_digest"] != digest:
                raise MobileControlRejected("request_id_conflict", "相同入站 ID 的控制文本不同")
            return MobileControlOutcome(receipt, metadata["control_receipt_id"] is not None)
        try:
            if len(text) > _MAX_COMMAND_CHARS:
                raise ControlConflict("invalid_command", "控制指令超过文本预算")
            if text == "/任务":
                receipt = self._status(inbound_id, source, digest)
            else:
                receipt = self._control(inbound_id, source, text, digest, submit_control)
        except (ControlConflict, ControlUnavailable) as error:
            code = error.code if error.code in _ERROR_TEXT else "control_unavailable"
            receipt = self._receipt(source, digest, "error", _ERROR_TEXT[code], error=code)
        except (KeyError, ValueError):
            receipt = self._receipt(
                source,
                digest,
                "error",
                _ERROR_TEXT["control_unavailable"],
                error="control_unavailable",
            )
        self._save(inbound_id, receipt)
        return MobileControlOutcome(
            receipt, receipt["mobile_task_control"]["control_receipt_id"] is not None
        )

    @staticmethod
    def _validate_source(inbound_id: str, source: MobileControlSource) -> None:
        if source.name not in _CHANNELS or source.account_id == "bot":
            raise MobileControlRejected("source_rejected", "手机控制缺少支持的可靠账户身份")
        origin = ReplyOrigin.load(
            {
                "kind": "channel",
                "input_id": inbound_id,
                "route": {
                    "name": source.name,
                    "account_id": source.account_id,
                    "sender_id": source.owner_id,
                    "target": source.target,
                    "kind": "private",
                },
            }
        )
        if not origin.delivery_routes() or source.owner_id != source.target:
            raise MobileControlRejected("source_rejected", "手机控制需要完整的主人私聊来源")

    def _records(self, clause: str, values: tuple[str, ...]) -> list[dict[str, Any]]:
        # Both callers supply fixed SQL and indexed equality/range parameters.
        rows = [
            dict(row)
            for row in self.store.db.execute(
                "SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB))=? "
                "THEN CAST(id AS BLOB) END AS id,"
                "CASE WHEN typeof(name)='text' AND length(CAST(name AS BLOB))<=? "
                "THEN CAST(name AS BLOB) END AS name,"
                "CASE WHEN typeof(fingerprint)='text' AND length(CAST(fingerprint AS BLOB))=? "
                "THEN CAST(fingerprint AS BLOB) END AS fingerprint,typeof(receipt) AS receipt_type,"
                "length(CAST(receipt AS BLOB)) AS size,"
                "CASE WHEN typeof(receipt)='text' AND length(CAST(receipt AS BLOB))<=? "
                "THEN CAST(receipt AS BLOB) END AS raw "
                f"FROM channel_inbound WHERE {clause} ORDER BY channel_inbound.id LIMIT 2",
                (_KEY_CHARS, _MAX_CHANNEL_BYTES, _KEY_CHARS, self.budget * 4, *values),
            )
        ]
        # Decode only SQL-bounded bytes. Malformed candidates still consume the
        # same LIMIT slots, so corruption cannot select a later valid reference.
        for row in rows:
            for column in ("id", "name", "fingerprint"):
                raw = row[column]
                try:
                    row[column] = raw.decode("utf-8") if isinstance(raw, bytes) else None
                except UnicodeDecodeError:
                    row[column] = None
        return rows

    def _decode(self, row: dict[str, Any], source: MobileControlSource) -> dict[str, Any]:
        try:
            raw = row["raw"]
            if (
                row["name"] != source.name
                or not isinstance(row["id"], str)
                or not _HEX_KEY.fullmatch(row["id"])
                or not isinstance(row["fingerprint"], str)
                or not _HEX_KEY.fullmatch(row["fingerprint"])
                or row["receipt_type"] != "text"
                or not isinstance(raw, bytes)
            ):
                raise ValueError
            receipt = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(receipt, dict)
                or set(receipt) != {"state", "reply_text", "mobile_task_control"}
                or receipt["state"] != "mobile_task_control"
                or not isinstance(receipt["reply_text"], str)
                or not receipt["reply_text"]
                or not self._fits(receipt)
            ):
                raise ValueError
            meta = receipt["mobile_task_control"]
            if (
                not isinstance(meta, dict)
                or set(meta) != _META_FIELDS
                or type(meta["version"]) is not int
                or meta["version"] != _VERSION
                or meta["source"] != asdict(source)
                or not isinstance(meta["command_digest"], str)
                or not _HEX_KEY.fullmatch(meta["command_digest"])
                or meta["kind"] not in {"status", "pause", "request_replan", "error"}
                or not isinstance(meta["tickets"], list)
                or len(meta["tickets"]) > _PAGE_ITEMS
            ):
                raise ValueError
            for ticket in meta["tickets"]:
                if (
                    not isinstance(ticket, dict)
                    or set(ticket) != {"commitment_id", "epoch", "approved_epoch", "actions"}
                    or not self._identifier(ticket["commitment_id"])
                    or type(ticket["epoch"]) is not int
                    or type(ticket["approved_epoch"]) is not int
                    or not 0 <= ticket["approved_epoch"] <= ticket["epoch"]
                    or ticket["actions"]
                    != [
                        "pause" if ticket["epoch"] == ticket["approved_epoch"] else "request_replan"
                    ]
                ):
                    raise ValueError
            if meta["kind"] in {"pause", "request_replan"}:
                if (
                    not self._identifier(meta["commitment_id"])
                    or not isinstance(meta["control_receipt_id"], str)
                    or not _HEX_KEY.fullmatch(meta["control_receipt_id"])
                    or meta["control_receipt_id"]
                    != self.control._receipt_id(
                        meta["commitment_id"], "owner", "mobile-task:" + row["id"]
                    )
                    or meta["error"] is not None
                ):
                    raise ValueError
            elif meta["control_receipt_id"] is not None or meta["commitment_id"] is not None:
                raise ValueError
            if meta["kind"] == "pause" and (
                len(meta["tickets"]) != 1
                or meta["tickets"][0]["commitment_id"] != meta["commitment_id"]
                or meta["tickets"][0]["actions"] != ["request_replan"]
            ):
                raise ValueError
            if meta["kind"] in {"error", "request_replan"} and meta["tickets"]:
                raise ValueError
            if (meta["kind"] == "error" and meta["error"] not in _ERROR_TEXT) or (
                meta["kind"] != "error" and meta["error"] is not None
            ):
                raise ValueError
            return cast(dict[str, Any], receipt)
        except (TypeError, ValueError, KeyError, RecursionError, UnicodeError) as error:
            raise ControlConflict("reference_invalid", _ERROR_TEXT["reference_invalid"]) from error

    @staticmethod
    def _identifier(value: object) -> bool:
        return isinstance(value, str) and 1 <= len(value) <= MAX_ORIGIN_ID_CHARS

    def _fits(self, receipt: dict[str, Any]) -> bool:
        try:
            text = _json(receipt)
            return len(text) <= self.budget and len(text.encode("utf-8")) <= self.budget * 4
        except (TypeError, ValueError, RecursionError, UnicodeError):
            return False

    @staticmethod
    def _receipt(
        source: MobileControlSource,
        digest: str,
        kind: str,
        reply: str,
        *,
        tickets: list[dict[str, Any]] | None = None,
        control_receipt_id: str | None = None,
        commitment_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "state": "mobile_task_control",
            "reply_text": reply,
            "mobile_task_control": {
                "version": _VERSION,
                "source": asdict(source),
                "command_digest": digest,
                "kind": kind,
                "tickets": tickets or [],
                "control_receipt_id": control_receipt_id,
                "commitment_id": commitment_id,
                "error": error,
            },
        }

    def _status(self, inbound_id: str, source: MobileControlSource, digest: str) -> dict[str, Any]:
        tickets: list[dict[str, Any]] = []
        lines: list[str] = []
        receipt = self._receipt(source, digest, "status", "暂无可核验的未完成任务。")
        for metadata in self.commitments.list(status="active", limit=_PAGE_ITEMS):
            if not metadata["has_brief"]:
                continue
            try:
                item = self.store.bounded_object(
                    "commitment", metadata["id"], max_bytes=self.budget * 4
                )
                if item is None:
                    continue
                self.commitments._validate_brief(item.get("brief"), stored=True)
                state = self.control._state(item)
            except (EvidenceError, ValueError, KeyError):
                continue
            paused = state["epoch"] > state["approved_epoch"]
            ticket = {
                "commitment_id": metadata["id"],
                "epoch": state["epoch"],
                "approved_epoch": state["approved_epoch"],
                "actions": ["request_replan" if paused else "pause"],
            }
            ordinal = len(tickets) + 1
            code = f"{inbound_id[:_PREFIX_CHARS]}-{ordinal}"
            title = str(metadata.get("title", "任务"))
            preview = title[: max(1, self.budget // (_PAGE_ITEMS * 4))]
            if preview != title:
                preview += "…"
            label = json.dumps(preview, ensure_ascii=False)
            command = f"/调整 {code} 新的完整要求" if paused else f"/暂停 {code}"
            line = f"{ordinal}. {label}（{'已暂停' if paused else '未暂停'}）\n{command}"
            candidate = self._receipt(
                source,
                digest,
                "status",
                "任务列表：\n" + "\n".join([*lines, line]),
                tickets=[*tickets, ticket],
            )
            if not self._fits(candidate):
                break
            tickets.append(ticket)
            lines.append(line)
            receipt = candidate
        return receipt

    def _control(
        self,
        inbound_id: str,
        source: MobileControlSource,
        text: str,
        digest: str,
        submit_control: ControlSubmit,
    ) -> dict[str, Any]:
        match = _COMMAND.fullmatch(text)
        if match is None:
            raise ControlConflict("invalid_command", _ERROR_TEXT["invalid_command"])
        verb, prefix, ordinal, requirements = match.groups()
        if (verb == "暂停" and requirements is not None) or (
            verb == "调整" and (requirements is None or not requirements.strip())
        ):
            raise ControlConflict("invalid_command", _ERROR_TEXT["invalid_command"])
        # Hexadecimal IDs lie below the next non-hex character, using the PK index.
        rows = self._records(
            "channel_inbound.id>=? AND channel_inbound.id<?", (prefix, prefix + "g")
        )
        if len(rows) != 1:
            raise ControlConflict("reference_invalid", _ERROR_TEXT["reference_invalid"])
        original = self._decode(rows[0], source)["mobile_task_control"]
        index = int(ordinal) - 1
        if index >= len(original["tickets"]):
            raise ControlConflict("reference_invalid", _ERROR_TEXT["reference_invalid"])
        ticket = original["tickets"][index]
        action: Literal["pause", "request_replan"] = "pause" if verb == "暂停" else "request_replan"
        if action not in ticket["actions"]:
            raise ControlConflict("reference_invalid", _ERROR_TEXT["reference_invalid"])
        request = OwnerControlRequest(
            action, ticket["epoch"], "mobile-task:" + inbound_id, requirements or ""
        )
        # Check receipt capacity before mutation; after a lost save the original
        # domain receipt and original ticket reconstruct these exact same bytes.
        predicted = {
            "id": self.control._receipt_id(ticket["commitment_id"], "owner", request.request_id),
            "epoch": ticket["epoch"] + 1,
        }
        candidate = self._confirmation(inbound_id, source, digest, ticket, action, predicted)
        if not self._fits(candidate):
            raise ControlUnavailable("output_budget", _ERROR_TEXT["output_budget"])
        result = submit_control(ticket["commitment_id"], request, ticket["approved_epoch"])
        return self._confirmation(inbound_id, source, digest, ticket, action, result.receipt)

    def _confirmation(
        self,
        inbound_id: str,
        source: MobileControlSource,
        digest: str,
        ticket: dict[str, Any],
        action: str,
        domain: dict[str, Any],
    ) -> dict[str, Any]:
        tickets: list[dict[str, Any]] = []
        if action == "pause":
            reply = (
                "暂停已登记，已进入的操作可能仍在收尾。\n"
                f"追加要求：/调整 {inbound_id[:_PREFIX_CHARS]}-1 新的完整要求"
            )
            tickets.append(
                {
                    "commitment_id": ticket["commitment_id"],
                    "epoch": domain["epoch"],
                    "approved_epoch": ticket["approved_epoch"],
                    "actions": ["request_replan"],
                }
            )
        else:
            reply = "重新规划要求已登记，任务保持暂停；主助手须读取完整说明并在下一轮确认方案。"
        return self._receipt(
            source,
            digest,
            action,
            reply,
            tickets=tickets,
            control_receipt_id=domain["id"],
            commitment_id=ticket["commitment_id"],
        )

    def _save(self, inbound_id: str, receipt: dict[str, Any]) -> None:
        if not self._fits(receipt):
            raise ControlUnavailable("output_budget", _ERROR_TEXT["output_budget"])
        if self.store.db.in_transaction:
            raise RuntimeError("手机控制回执不能嵌套提交")
        with self.store.db:
            changed = self.store.db.execute(
                "UPDATE channel_inbound SET receipt=? WHERE id=? AND receipt IS NULL",
                (_json(receipt), inbound_id),
            ).rowcount
            if changed != 1:
                raise ControlConflict("request_id_conflict", "手机控制回执已存在，不可覆盖")
