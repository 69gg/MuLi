"""One permission gate for every model-initiated operation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from muli.events import EventBus
from muli.reply_origin import MAX_ORIGIN_ID_CHARS, ReplyOrigin
from muli.store import Store, new_id, now

Mode = Literal["ask", "auto", "yolo"]
Review = Callable[[str, dict[str, Any], str], Awaitable[tuple[bool, str]]]


class PermissionDenied(Exception):
    pass


class ApprovalSuperseded(Exception):
    """New owner input deferred this unstarted call without cancelling its run."""


@dataclass(frozen=True, slots=True)
class ApprovalNotice:
    """Host provenance for one approval; not a durable live permission."""

    approval_id: str
    session_id: str
    run_id: str
    reply_origin: ReplyOrigin

    def dump(self) -> dict[str, Any]:
        return {
            "version": 1,
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "reply_origin": self.reply_origin.dump(),
        }

    @classmethod
    def load(cls, value: object) -> ApprovalNotice:
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "approval_id", "session_id", "run_id", "reply_origin"}
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise ValueError("审批通知来源无法核验")
        for key in ("approval_id", "session_id", "run_id"):
            identifier = value[key]
            if (
                not isinstance(identifier, str)
                or not 1 <= len(identifier) <= MAX_ORIGIN_ID_CHARS
                or any(ord(character) < 32 or ord(character) == 127 for character in identifier)
            ):
                raise ValueError("审批通知标识无法核验")
            identifier.encode("utf-8")
        origin = ReplyOrigin.load(value["reply_origin"])
        routes = origin.delivery_routes()
        if (
            origin.kind != "channel"
            or origin.dump() != value["reply_origin"]
            or len(routes) != 1
            or routes[0].get("name") not in {"onebot", "telegram"}
            or routes[0].get("kind") != "private"
            or routes[0].get("sender_id") != routes[0].get("target")
        ):
            raise ValueError("审批通知只接受准确原始私聊来源")
        origin.route_json.encode("utf-8")
        return cls(value["approval_id"], value["session_id"], value["run_id"], origin)


class Permissions:
    def __init__(self, store: Store, bus: EventBus, review: Review, timeout: int = 600) -> None:
        self.store, self.bus, self.review, self.timeout = store, bus, review, timeout
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self._waiting: dict[tuple[str, str], set[str]] = {}

    async def check(
        self,
        mode: Mode,
        tool: str,
        arguments: dict[str, Any],
        risk: str,
        session_id: str,
        run_id: str,
        *,
        reply_origin: ReplyOrigin | None = None,
    ) -> None:
        if mode == "yolo":
            return
        reason = "本次调用需要批准"
        if mode == "auto":
            try:
                allowed, reason = await self.review(tool, arguments, risk)
            except Exception:  # noqa: BLE001 - review failures always require user approval
                allowed, reason = False, "独立审查暂不可用"
            if allowed and risk not in {"destructive", "external", "computer"}:
                await self.bus.emit(
                    "tool.reviewed",
                    {"tool": tool, "allowed": True, "reason": reason},
                    session_id,
                )
                return
        approval: dict[str, Any] = {
            "id": new_id(),
            "session_id": session_id,
            "run_id": run_id,
            "tool": tool,
            "arguments": arguments,
            "reason": reason,
            "risk": risk,
            "status": "pending",
            "created_at": now(),
        }
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[approval["id"]] = future
        owner = (session_id, run_id)
        self._waiting.setdefault(owner, set()).add(approval["id"])
        try:
            requested = dict(approval)
            if reply_origin is not None:
                try:
                    notice = ApprovalNotice.load(
                        ApprovalNotice(approval["id"], session_id, run_id, reply_origin).dump()
                    )
                except ValueError:
                    pass  # Other origins retain the existing local approval.
                else:
                    requested["approval_notice"] = notice.dump()
            self.store.event(
                "approval.requested", requested, session_id, objects=[("approval", approval)]
            )
            await self.bus.notify()
            allowed = await asyncio.wait_for(future, self.timeout)
            if not allowed:
                raise PermissionDenied("用户拒绝了工具调用")
        except (TimeoutError, asyncio.CancelledError):
            current = self.store.get("approval", approval["id"])
            if current and current["status"] == "pending":
                current["status"] = "cancelled"
                self.store.put("approval", current)
                await self.bus.emit(
                    "approval.resolved",
                    {"id": approval["id"], "decision": "cancelled"},
                    session_id,
                )
            raise
        finally:
            # Cancellation can arrive while the requested-event wakeup is still
            # suspended, before wait_for has observed a superseding exception.
            if future.done() and not future.cancelled():
                future.exception()
            self.pending.pop(approval["id"], None)
            waiting = self._waiting[owner]
            waiting.discard(approval["id"])
            if not waiting:
                del self._waiting[owner]

    def pending_for(
        self, session_id: str, run_id: str, *, max_waiters: int | None = None
    ) -> tuple[str, ...]:
        """Locate only this live execution's waiters, without reading other approvals."""
        waiting = self._waiting.get((session_id, run_id), ())
        if max_waiters is not None:
            if type(max_waiters) is not int or max_waiters < 1:
                raise ValueError("审批核查数量预算无效")
            if len(waiting) > max_waiters:
                raise ValueError("本执行的审批等待集合超过核查预算")
            if any(identifier not in self.pending for identifier in waiting):
                raise ValueError("本执行的审批等待关联缺失")
        return tuple(
            identifier
            for identifier in waiting
            if (future := self.pending.get(identifier)) is not None and not future.done()
        )

    def _supersede_in_transaction(
        self, approval: dict[str, Any], *, input_id: str
    ) -> asyncio.Future[bool] | None:
        """Stage a verified live approval; the caller wakes it only after commit."""
        if not self.store.db.in_transaction:
            raise RuntimeError("审批延期必须与新输入在同一事务中提交")
        identifier = approval["id"]
        future = self.pending.get(identifier)
        if future is None or future.done():
            return None
        self.store._event(
            "approval.resolved",
            {"id": identifier, "decision": "cancelled", "input_id": input_id},
            approval["session_id"],
            objects=[("approval", {**approval, "status": "cancelled"})],
        )
        return future

    def resolve_now(
        self,
        identifier: str,
        decision: str,
        *,
        persist_receipt: Callable[[], None] | None = None,
    ) -> None:
        """Commit one live decision and its optional host receipt before waking it.

        The receipt callback only writes in this transaction; it must neither
        commit nor yield. A failed receipt leaves the original waiter pending.
        """
        if decision not in {"approve", "reject"}:
            raise ValueError("decision 必须为 approve 或 reject")
        if self.store.db.in_transaction:
            raise RuntimeError("审批决定不能嵌套提交")
        future = self.pending.get(identifier)
        approval = self.store.get("approval", identifier)
        if future is None or future.done() or approval is None or approval["status"] != "pending":
            raise ValueError("审批已结束或运行已中止")
        approval["status"] = "approved" if decision == "approve" else "rejected"
        with self.store.db:
            self.store._event(
                "approval.resolved",
                {"id": identifier, "decision": decision},
                approval["session_id"],
                objects=[("approval", approval)],
            )
            if persist_receipt is not None:
                persist_receipt()
        # No await separates the durable decision from its original live waiter.
        future.set_result(decision == "approve")

    async def resolve(self, identifier: str, decision: str) -> None:
        self.resolve_now(identifier, decision)
        await self.bus.notify()
