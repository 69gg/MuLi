"""One permission gate for every model-initiated operation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from muli.events import EventBus
from muli.store import Store, new_id, now

Mode = Literal["ask", "auto", "yolo"]
Review = Callable[[str, dict[str, Any], str], Awaitable[tuple[bool, str]]]


class PermissionDenied(Exception):
    pass


class Permissions:
    def __init__(self, store: Store, bus: EventBus, review: Review, timeout: int = 600) -> None:
        self.store, self.bus, self.review, self.timeout = store, bus, review, timeout
        self.pending: dict[str, asyncio.Future[bool]] = {}

    async def check(
        self,
        mode: Mode,
        tool: str,
        arguments: dict[str, Any],
        risk: str,
        session_id: str,
        run_id: str,
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
        try:
            self.store.put("approval", approval)
            await self.bus.emit("approval.requested", approval, session_id)
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
            self.pending.pop(approval["id"], None)

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
