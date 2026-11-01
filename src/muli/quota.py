"""跨团队与工作流的并行租约，避免各自的本地上限叠加后互相阻塞。

租约按 ``(session_id, commitment_id, task_generation)`` 幂等发放；同一执行重复申请返回
原租约，不会重复占槽；不同任务代次即使复用会话也不会继承尚未释放的租约。总量仍由
``safety_max_agents`` 作为安全阀。租约只控制是否开始执行，不改变模型权限、审批或任务说明。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Key = tuple[str, str, str]


@dataclass(slots=True)
class Lease:
    key: Key
    slots: int = 1
    released: bool = False

    def public(self) -> dict[str, Any]:
        session_id, commitment_id, run_id = self.key
        return {
            "session_id": session_id,
            "commitment_id": commitment_id or None,
            "run_id": run_id or None,
            "slots": self.slots,
        }


class QuotaBroker:
    """非阻塞申请加异步等待；释放时唤醒全部等待者并通知监听器。"""

    def __init__(self, limit: Callable[[], int]) -> None:
        self._limit = limit
        self._leases: dict[Key, Lease] = {}
        # 保持等待者的登记顺序，释放时按排队顺序唤醒。
        self._waiters: list[asyncio.Event] = []
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, listener: Callable[[], None]) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def _limit_value(self) -> int:
        return max(1, int(self._limit()))

    @property
    def used(self) -> int:
        return sum(lease.slots for lease in self._leases.values())

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    def status(self) -> dict[str, Any]:
        limit = self._limit_value()
        return {
            "limit": limit,
            "used": self.used,
            "leased": self.used,
            "available": max(0, limit - self.used),
            "waiting": self.waiting,
            "leases": [lease.public() for lease in self._leases.values()],
        }

    def try_acquire(self, key: Key, slots: int = 1) -> Lease | None:
        if type(slots) is not int or slots < 1:
            raise ValueError("租约槽数必须是正整数")
        existing = self._leases.get(key)
        if existing is not None:
            return existing
        if self.used + slots > self._limit_value():
            return None
        lease = Lease(key, slots)
        self._leases[key] = lease
        return lease

    async def acquire(self, key: Key, slots: int = 1) -> Lease:
        while True:
            lease = self.try_acquire(key, slots)
            if lease is not None:
                return lease
            event = asyncio.Event()
            self._waiters.append(event)
            try:
                await event.wait()
            finally:
                try:
                    self._waiters.remove(event)
                except ValueError:
                    pass

    def release(self, lease: Lease) -> None:
        if lease.released or self._leases.get(lease.key) is not lease:
            return
        lease.released = True
        self._leases.pop(lease.key, None)
        waiters = tuple(self._waiters)
        self._waiters.clear()
        for event in waiters:
            event.set()
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:
                # 唤醒失败不能吞掉已释放的槽；下一次调度仍可重新尝试。
                continue
