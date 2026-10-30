"""Read one host-identified, confirmed Telegram text part without changing routing."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from time import monotonic
from typing import Any

from muli.channel_history import ChannelHistory
from muli.channels.types import MAX_OUTGOING_TEXT_CHARS, normalize_channel_reference
from muli.evidence import EvidenceError, json_size, validate_integer
from muli.history_evidence import HistoryEvidence
from muli.management_transaction import _object
from muli.reply_origin import ReplyOrigin, authorize_route
from muli.store import SQLITE_MAX_INTEGER, Store
from muli.store_schema import ExistingStoreLimits

_UTF8_MAX_BYTES = 4


class ChannelReferences:
    """The host supplies today's owner-round and binding checks on every page."""

    def __init__(self, store: Store) -> None:
        self.store = store

    @contextmanager
    def _snapshot(self, seconds: float) -> Iterator[None]:
        db = self.store.db
        if (
            type(db) is not sqlite3.Connection
            or db.text_factory is not str
            or db.row_factory is not sqlite3.Row
            or db.isolation_level is None
        ):
            raise EvidenceError("channel_reference_unavailable", "引用核验需要资料库自有的标准连接")
        if db.in_transaction or self.store._channel_reference_read_active:
            raise EvidenceError(
                "channel_reference_transaction_active", "引用核验不能接管已有事务或读取范围"
            )
        deadline = monotonic() + seconds
        vm_limit = ExistingStoreLimits().vm_steps
        interval = min(100, vm_limit)
        steps = 0
        interrupted = False
        installed = begun = False
        previous_timeout: int | None = None

        def progress() -> int:
            nonlocal steps, interrupted
            steps += interval
            interrupted = steps >= vm_limit or monotonic() >= deadline
            return int(interrupted)

        def check() -> None:
            if interrupted or monotonic() >= deadline:
                raise EvidenceError(
                    "channel_reference_search_incomplete",
                    "引用核验达到 SQLite 工作量或时间预算，未证明唯一",
                )

        self.store._channel_reference_read_active = True
        try:
            db.set_progress_handler(progress, interval)
            installed = True
            with closing(db.execute("PRAGMA busy_timeout")) as rows:
                previous_timeout = int(rows.fetchone()[0])
            timeout = min(previous_timeout, int(max(0, deadline - monotonic()) * 1000))
            with closing(db.execute(f"PRAGMA busy_timeout={timeout}")):
                pass
            check()
            with closing(db.execute("BEGIN")):
                begun = True
            yield
            check()
        except sqlite3.Error as exc:
            if (
                interrupted
                or monotonic() >= deadline
                or getattr(exc, "sqlite_errorcode", None)
                in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                    sqlite3.SQLITE_TOOBIG,
                    sqlite3.SQLITE_NOMEM,
                }
            ):
                raise EvidenceError(
                    "channel_reference_search_incomplete", "引用核验未能在 SQLite 读取预算内完成"
                ) from exc
            raise EvidenceError(
                "channel_reference_unavailable", "引用证据的本地只读核验失败"
            ) from exc
        finally:
            failed = False
            try:
                if installed:
                    try:
                        db.set_progress_handler(None, 0)
                    except sqlite3.Error:
                        failed = True
                if begun and db.in_transaction:
                    try:
                        db.rollback()
                    except sqlite3.Error:
                        failed = True
                if previous_timeout is not None:
                    try:
                        with closing(db.execute(f"PRAGMA busy_timeout={previous_timeout}")):
                            pass
                    except sqlite3.Error:
                        failed = True
            finally:
                self.store._channel_reference_read_active = False
            if failed:
                raise EvidenceError("channel_reference_unavailable", "引用核验的只读连接清理失败")

    def _input(self, main_id: str, input_id: str, budget: int) -> dict[str, Any]:
        archive = HistoryEvidence(self.store)._reply_owner_input(
            main_id, input_id, max_bytes=budget
        )
        probe = self.store.ingress_audit_probe(main_id, input_id=input_id, max_bytes=budget)
        if (
            probe is None
            or probe["payload_type"] != "text"
            or type(probe["required_bytes"]) is not int
            or probe["required_bytes"] > budget
        ):
            raise EvidenceError("output_budget", "完整引用入站证据超过核验字节预算")
        raw = self.store.ingress_audit_bytes(probe["seq"], max_bytes=budget)
        if raw is None:
            raise EvidenceError("channel_reference_unavailable", "引用入站原件不存在")
        try:
            canonical = _object(raw)
            if (
                ("channel_reference" in canonical) != ("channel_reference" in archive)
                or canonical.get("channel_reference") != archive.get("channel_reference")
                or any(
                    key in canonical
                    for key in ("channel_ownership", "channel_ownership_control", "task_control")
                )
            ):
                raise ValueError
            marker = normalize_channel_reference(canonical.get("channel_reference"))
            if marker != canonical.get("channel_reference"):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise EvidenceError(
                "channel_reference_unavailable", "引用标记与规范入站、存档不一致"
            ) from exc
        if marker is None:
            raise EvidenceError(
                "channel_reference_missing", "该真实入站没有保存原生引用，不能猜测最近投递"
            )
        if marker["status"] != "identified":
            raise EvidenceError(
                "channel_reference_unavailable", "该入站带有引用，但宿主未能准确定位"
            )
        route = canonical.get("route")
        if (
            canonical.get("source")
            != "channel:telegram:"
            + str(route.get("account_id", "") if isinstance(route, dict) else "")
            or not isinstance(route, dict)
            or route.get("name") != "telegram"
            or not isinstance(route.get("account_id"), str)
            or not isinstance(route.get("sender_id"), str)
            or route.get("kind") != "private"
        ):
            raise EvidenceError(
                "channel_reference_unsupported", "引用读取仅支持主助手的 Telegram 主人私聊入站"
            )
        try:
            bound = authorize_route(
                "telegram",
                route,
                account_id=route["account_id"],
                owner_ids=[route["sender_id"]],
                allowed_groups=[],
                outbound=True,
            )
        except ValueError as exc:
            raise EvidenceError(
                "channel_reference_unavailable", "引用入站的私聊范围无法核验"
            ) from exc
        origin = ReplyOrigin.from_input(canonical)
        if (
            origin.kind != "channel"
            or origin.input_id != input_id
            or origin.delivery_routes() != [bound]
        ):
            raise EvidenceError("channel_reference_unavailable", "引用入站与原始回复来源不一致")
        return {"route": bound, "marker": marker}

    def read(
        self,
        main_id: str,
        *,
        input_id: str,
        current_owner_input: Callable[[], dict[str, Any] | None],
        authorize_scope: Callable[[str, str, str], None],
        offset_bytes: int = 0,
        revision: str | None = None,
        max_output_chars: int,
        max_context_chars: int,
        max_search_seconds: float,
    ) -> dict[str, Any]:
        for name, value, minimum in (
            ("max_output_chars", max_output_chars, 1),
            ("max_context_chars", max_context_chars, 1),
            ("offset_bytes", offset_bytes, 0),
        ):
            validate_integer(value, name, minimum=minimum)
        try:
            finite = type(max_search_seconds) in {int, float} and math.isfinite(max_search_seconds)
        except OverflowError:
            finite = False
        if not finite or max_search_seconds <= 0:
            raise EvidenceError("invalid_arguments", "引用核验需要有限的正时间预算")
        if not isinstance(input_id, str) or not input_id or "\x00" in input_id:
            raise EvidenceError("invalid_arguments", "引用读取需要准确的真实入站 input_id")
        try:
            input_id.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise EvidenceError("invalid_arguments", "引用入站 ID 包含无效 Unicode 字符") from exc
        if revision is not None and (not isinstance(revision, str) or not revision):
            raise EvidenceError("invalid_arguments", "引用正文版本必须是非空字符串")
        if offset_bytes and revision is None:
            raise EvidenceError("invalid_arguments", "续读引用正文必须提供上一页 revision")
        raw_budget = min(max_context_chars * _UTF8_MAX_BYTES, SQLITE_MAX_INTEGER)
        with self._snapshot(max_search_seconds):
            current = current_owner_input()
            if current is None:
                raise PermissionError("引用读取需要当前主助手的真实主人输入轮")
            origin = ReplyOrigin.load(current.get("reply_origin"))
            if (
                current.get("session_id") != main_id
                or current.get("principal") != "owner"
                or origin.kind not in {"web", "channel"}
                or (origin.kind == "web" and current.get("source") != "web")
                or (origin.kind == "channel" and input_id != origin.input_id)
            ):
                raise PermissionError(
                    "当前渠道轮只能读取本轮引用；网页主人轮可选择同主会话的真实入站"
                )
            evidence = self._input(main_id, input_id, raw_budget)
            route, marker = evidence["route"], evidence["marker"]
            name, account, owner = route["name"], route["account_id"], route["sender_id"]
            authorize_scope(name, account, owner)
            found = self.store.channel_reference_delivery(
                name=name,
                account_id=account,
                owner_id=owner,
                platform_message_id=marker["message_id"],
                metadata_bytes=raw_budget,
                source_bytes=min(
                    MAX_OUTGOING_TEXT_CHARS * _UTF8_MAX_BYTES + raw_budget, SQLITE_MAX_INTEGER
                ),
            )
            if found is None:
                raise EvidenceError(
                    "channel_reference_not_found", "准确私聊内没有与该引用一致的已确认文字投递"
                )
            reference = {
                "section": "reference",
                "input_id": input_id,
                "platform_message_id": marker["message_id"],
                "evidence": "confirmed_text_part",
                "scope": "single_delivery_part",
            }
            remaining = max_output_chars - json_size(reference)
            if remaining < 1:
                raise EvidenceError("output_budget", "引用定位元数据超过工具输出预算")
            page = ChannelHistory(self.store).read_delivery(
                name,
                account,
                owner,
                main_id,
                delivery_id=found["delivery_id"],
                part=found["part"],
                offset_bytes=offset_bytes,
                revision=revision,
                max_output_chars=remaining,
            )
            if (
                page["confirmed_parts"] != found["confirmed_parts"]
                or page["total_parts"] != found["total_parts"]
                or (page["state"] == "sent") != (page["confirmed_parts"] == page["total_parts"])
            ):
                raise EvidenceError(
                    "channel_reference_unavailable", "引用投递的已确认位置与保存状态矛盾"
                )
            result = {**page, **reference}
            if json_size(result) > max_output_chars:
                raise EvidenceError("output_budget", "完整引用正文页超过输出预算")
            return result
