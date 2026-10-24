"""Raw owner commands over the existing live waiter and inbound receipt.

Runtime owns configuration_lock and the final transport admission guard. This
module never submits model input, changes a reply origin, or replays execution.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict
from typing import Any

from muli.mobile_task_control import MobileControlRejected, MobileControlSource
from muli.permissions import Permissions
from muli.task_control import _digest, _json

_KEY = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[0-9a-f]{32}")
_COMMAND = re.compile(r"/(审批|批准|拒绝)(?: ([0-9a-f]{32}))?")
_ERRORS = {
    "invalid_command": "格式无效，请发送 /审批，或复制 /批准 完整ID、/拒绝 完整ID。",
    "not_live": "审批已结束、运行已中止或资料无法核验；不会重新执行，请重新 /审批。",
    "output_budget": "审批资料超过当前输出预算，未登记决定；请在 Web 核对。",
}
_FIELDS = {
    "version",
    "source",
    "command_digest",
    "kind",
    "approval_id",
    "error",
}


def is_approval_command(text: str) -> bool:
    """Also reserve malformed raw commands; this is not source authorization."""
    return text.lstrip().startswith(("/审批", "/批准", "/拒绝"))


def _object(data: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError("invalid_constant")

    value = json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError("not_object")
    return value


class MobileApprovals:
    def __init__(self, permissions: Permissions, *, budget: int) -> None:
        self.permissions, self.store, self.budget = permissions, permissions.store, budget

    def _fits(self, receipt: dict[str, Any]) -> bool:
        try:
            text = _json(receipt)
            return len(text) <= self.budget and len(text.encode("utf-8")) <= self.budget * 4
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return False

    def _receipt(
        self,
        source: MobileControlSource,
        text: str,
        kind: str,
        reply: str,
        *,
        identifier: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "state": "mobile_approval",
            "reply_text": reply,
            "mobile_approval": {
                "version": 1,
                "source": asdict(source),
                "command_digest": _digest(text),
                "kind": kind,
                "approval_id": identifier,
                "error": error,
            },
        }

    def _cached(
        self, inbound_id: str, source: MobileControlSource, text: str
    ) -> dict[str, Any] | None:
        row = self.store.db.execute(
            "SELECT name=? AS matched_name,"
            "CASE WHEN typeof(fingerprint)='text' AND length(CAST(fingerprint AS BLOB))=64 "
            "THEN CAST(fingerprint AS BLOB) END AS fingerprint,typeof(receipt) AS receipt_type,"
            "CASE WHEN typeof(receipt)='text' AND length(CAST(receipt AS BLOB))<=? "
            "THEN CAST(receipt AS BLOB) END AS raw FROM channel_inbound WHERE id=?",
            (source.name, self.budget * 4, inbound_id),
        ).fetchone()
        try:
            if row is None or not row["matched_name"]:
                raise ValueError("source_rejected")
            fingerprint = row["fingerprint"]
            if not isinstance(fingerprint, bytes) or not _KEY.fullmatch(
                fingerprint.decode("ascii")
            ):
                raise ValueError("fingerprint_rejected")
            if row["receipt_type"] == "null":
                return None
            if row["receipt_type"] != "text" or not isinstance(row["raw"], bytes):
                raise ValueError("receipt_rejected")
            receipt = _object(row["raw"])
            if (
                set(receipt) != {"state", "reply_text", "mobile_approval"}
                or receipt["state"] != "mobile_approval"
                or not isinstance(receipt["reply_text"], str)
                or not receipt["reply_text"]
                or not self._fits(receipt)
            ):
                raise ValueError("receipt_rejected")
            metadata = receipt["mobile_approval"]
            if (
                not isinstance(metadata, dict)
                or set(metadata) != _FIELDS
                or type(metadata["version"]) is not int
                or metadata["version"] != 1
                or metadata["source"] != asdict(source)
                or metadata["command_digest"] != _digest(text)
                or metadata["kind"] not in {"list", "detail", "approve", "reject", "error"}
            ):
                raise ValueError("receipt_rejected")
            identifier = metadata["approval_id"]
            if metadata["kind"] in {"detail", "approve", "reject"}:
                if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
                    raise ValueError("receipt_rejected")
            elif identifier is not None:
                raise ValueError("receipt_rejected")
            if (
                metadata["kind"] == "error"
                and metadata["error"] not in _ERRORS
                or metadata["kind"] != "error"
                and metadata["error"] is not None
            ):
                raise ValueError("receipt_rejected")
            return receipt
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError) as error:
            raise MobileControlRejected("reference_invalid", "手机审批回执无法核验") from error

    def _live(self, identifier: str) -> dict[str, Any] | None:
        future = self.permissions.pending.get(identifier)
        if future is None or future.done():
            return None
        row = self.store.db.execute(
            "SELECT CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB))<=? "
            "THEN CAST(data AS BLOB) END AS raw FROM objects WHERE kind='approval' AND id=?",
            (self.budget * 4, identifier),
        ).fetchone()
        if row is None or not isinstance(row["raw"], bytes):
            raise ValueError("approval_unavailable")
        item = _object(row["raw"])
        if (
            set(item)
            != {
                "id",
                "session_id",
                "run_id",
                "tool",
                "arguments",
                "reason",
                "risk",
                "status",
                "created_at",
            }
            or item["id"] != identifier
            or not _ID.fullmatch(identifier)
            or item["status"] != "pending"
            or not all(
                isinstance(item[key], str) and item[key]
                for key in (
                    "session_id",
                    "run_id",
                    "tool",
                    "risk",
                )
            )
            or not isinstance(item["reason"], str)
            or not isinstance(item["arguments"], dict)
            or type(item["created_at"]) not in {int, float}
            or not math.isfinite(item["created_at"])
        ):
            raise ValueError("approval_unavailable")
        return item

    @staticmethod
    def _render(item: dict[str, Any]) -> str:
        return (
            f"{item['id']}\n工具：{item['tool']}；风险：{item['risk']}\n"
            f"会话：{item['session_id']}\n运行：{item['run_id']}\n"
            f"说明：{item['reason']}\n参数：{_json(item['arguments'])}"
        )

    def _save(self, inbound_id: str, receipt: dict[str, Any]) -> None:
        # Caller owns the sole transaction, including the approval decision.
        changed = self.store.db.execute(
            "UPDATE channel_inbound SET receipt=? WHERE id=? AND receipt IS NULL",
            (_json(receipt), inbound_id),
        ).rowcount
        if changed != 1:
            raise MobileControlRejected("request_id_conflict", "手机审批回执已存在，不可覆盖")

    def handle(self, inbound_id: str, source: MobileControlSource, text: str) -> dict[str, Any]:
        if not _KEY.fullmatch(inbound_id):
            raise MobileControlRejected("source_rejected", "手机审批缺少准确入站标识")
        source.reply_origin(inbound_id)
        previous = self._cached(inbound_id, source, text)
        if previous is not None:
            return previous
        decision: str | None = None
        identifier: str | None = None
        match = _COMMAND.fullmatch(text)
        try:
            if match is None or match[1] != "审批" and match[2] is None:
                raise ValueError("invalid_command")
            identifier = match[2]
            if match[1] == "审批" and identifier is None:
                lines: list[str] = []
                for key, future in self.permissions.pending.items():
                    if future.done():
                        continue
                    item = self._live(key)
                    if item is None:
                        continue
                    candidate = [*lines, self._render(item)]
                    receipt = self._receipt(source, text, "list", "\n\n".join(candidate))
                    if not self._fits(receipt):
                        lines.append("其余审批或参数超出本页预算，请在 Web 核对。")
                        break
                    lines = candidate
                reply = "\n\n".join(lines) if lines else "当前没有正在等待主人决定的工具审批。"
                receipt = self._receipt(source, text, "list", reply)
            else:
                if identifier is None or (item := self._live(identifier)) is None:
                    raise ValueError("not_live")
                if match[1] == "审批":
                    receipt = self._receipt(
                        source, text, "detail", self._render(item), identifier=identifier
                    )
                else:
                    decision = "approve" if match[1] == "批准" else "reject"
                    reply = (
                        f"审批 {identifier}：批准已登记；这不表示操作已经执行或成功。"
                        if decision == "approve"
                        else f"审批 {identifier}：拒绝已登记，该调用不会获准执行。"
                    )
                    receipt = self._receipt(source, text, decision, reply, identifier=identifier)
            if not self._fits(receipt):
                raise ValueError("output_budget")
        except (
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            UnicodeError,
            OverflowError,
        ) as error:
            code = str(error) if str(error) in _ERRORS else "not_live"
            receipt = self._receipt(source, text, "error", _ERRORS[code], error=code)
            decision = None
        if not self._fits(receipt):
            raise MobileControlRejected("output_budget", "手机审批回执超过输出预算")
        if decision is not None and identifier is not None:
            # Never catch receipt/SQL failures as a new successful error receipt.
            self.permissions.resolve_now(
                identifier, decision, persist_receipt=lambda: self._save(inbound_id, receipt)
            )
        else:
            if self.store.db.in_transaction:
                raise RuntimeError("手机审批回执不能嵌套提交")
            with self.store.db:
                self._save(inbound_id, receipt)
        return receipt
