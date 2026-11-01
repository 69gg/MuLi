"""Crash recovery planning without replaying side effects.

The planner is deliberately read-only.  It reports whether a session, run,
agent or workflow may be resumed automatically, requires the owner to inspect a
possibly executed effect, or must stay blocked because its scope is gone.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from muli.store import Store

Decision = Literal["resume_safe", "needs_owner", "blocked"]

_SIDE_EFFECT_RISKS = frozenset({"write", "external", "destructive", "computer"})
_BLOCKED_SESSION_STATUS = frozenset({"cancelled", "deleted"})


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    status: Decision
    reason: str
    attempt_ids: tuple[str, ...] = ()
    run_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def dump(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "attempt_ids": list(self.attempt_ids),
            "run_id": self.run_id,
            "details": dict(self.details),
        }


class RecoveryPlanner:
    """Read-only classification backed by durable attempts and host facts."""

    def __init__(
        self,
        store: Store,
        *,
        fingerprint: str | None = None,
        managed_unresolved: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        self.fingerprint = fingerprint
        self.managed_unresolved = managed_unresolved

    def plan_session(self, session_id: str) -> RecoveryDecision:
        session = self.store.get("session", session_id)
        if session is None:
            return RecoveryDecision("blocked", "会话不存在或已删除")
        if session.get("status") in _BLOCKED_SESSION_STATUS:
            return RecoveryDecision("blocked", "会话已取消或删除")
        if session.get("format_changed") is True:
            return RecoveryDecision("blocked", "存储格式或实例已变化")
        if self.fingerprint is not None and session.get("recovery_fingerprint") not in {
            None,
            self.fingerprint,
        }:
            return RecoveryDecision("blocked", "配置指纹与中断时不一致")
        run_ids = self._run_ids(session_id)
        decisions = [self.plan_run(run_id) for run_id in run_ids]
        return self._merge(decisions, run_id=None)

    def plan_run(self, run_id: str) -> RecoveryDecision:
        run = self.store.get("run", run_id)
        if run is None:
            return RecoveryDecision("blocked", "运行记录不存在", run_id=run_id)
        if run.get("status") in {"cancelled", "completed", "failed"}:
            return RecoveryDecision("blocked", "运行已进入终态", run_id=run_id)
        session_id = str(run.get("session_id", ""))
        session = self.store.get("session", session_id) if session_id else None
        if session is not None and session.get("status") in _BLOCKED_SESSION_STATUS:
            return RecoveryDecision("blocked", "所属会话已取消", run_id=run_id)
        attempts = self._attempts(run_id=run_id)
        attempt_ids = tuple(str(item.get("attempt_id", "")) for item in attempts)
        if self._managed_risk():
            return RecoveryDecision(
                "needs_owner",
                "托管事务尚未 resolved，不能自动恢复",
                attempt_ids,
                run_id,
            )
        if self._outbox_risk():
            return RecoveryDecision(
                "needs_owner",
                "存在发送中或结果未知的渠道投递",
                attempt_ids,
                run_id,
            )
        pending = self._pending_approvals(session_id)
        if pending:
            return RecoveryDecision(
                "needs_owner",
                "存在重启前的待审批请求，旧批准不能跨重启生效",
                attempt_ids,
                run_id,
                {"approval_ids": pending},
            )
        unsafe = [
            item
            for item in attempts
            if item.get("phase") in {"started", "unknown"}
        ]
        if unsafe:
            details = {
                "attempts": [
                    {
                        "attempt_id": item.get("attempt_id"),
                        "tool": item.get("tool"),
                        "risk": item.get("risk"),
                        "recovery": item.get("recovery"),
                        "phase": item.get("phase"),
                        "command_id": item.get("command_id"),
                    }
                    for item in unsafe
                ]
            }
            return RecoveryDecision(
                "needs_owner",
                "有工具已开始但结果未确认，必须先核查实际状态",
                tuple(str(item.get("attempt_id")) for item in unsafe),
                run_id,
                details,
            )
        if any(item.get("phase") not in {"planned", "committed", "not_executed"} for item in attempts):
            return RecoveryDecision(
                "needs_owner",
                "存在无法归类的中断工具尝试",
                attempt_ids,
                run_id,
            )
        return RecoveryDecision(
            "resume_safe",
            "没有已开始但结果未知的工具调用",
            attempt_ids,
            run_id,
        )

    def plan_agent(self, agent_id: str) -> RecoveryDecision:
        agent = self.store.get("agent", agent_id)
        if agent is None:
            return RecoveryDecision("blocked", "队友不存在或已删除")
        if agent.get("status") in {"cancelled", "completed", "failed"}:
            return RecoveryDecision("blocked", "队友已进入终态")
        run_id = agent.get("run_id")
        if isinstance(run_id, str) and run_id:
            return self.plan_run(run_id)
        return self.plan_session(str(agent.get("session_id", "")))

    def plan_workflow(self, workflow_run_id: str) -> RecoveryDecision:
        run = self.store.get("workflow_run", workflow_run_id)
        if run is None:
            return RecoveryDecision("blocked", "工作流运行不存在")
        if run.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
            return RecoveryDecision("blocked", "工作流已进入终态")
        parent = run.get("parent_run_id")
        if isinstance(parent, str) and parent:
            parent_run = self.store.get("run", parent)
            if parent_run is not None and parent_run.get("status") in {"completed", "cancelled"}:
                return RecoveryDecision("blocked", "父运行已结束")
        decisions: list[RecoveryDecision] = []
        for node in (run.get("nodes") or {}).values():
            if not isinstance(node, dict):
                continue
            if node.get("status") not in {"pending", "queued", "running", "recovering"}:
                continue
            session_id = node.get("session_id")
            if isinstance(session_id, str) and session_id:
                decisions.append(self.plan_session(session_id))
        return self._merge(decisions, run_id=workflow_run_id)

    def merge(
        self, decisions: Iterable[RecoveryDecision], *, run_id: str | None = None
    ) -> RecoveryDecision:
        return self._merge(decisions, run_id=run_id)

    def _merge(
        self, decisions: Iterable[RecoveryDecision], *, run_id: str | None
    ) -> RecoveryDecision:
        values = list(decisions)
        if any(item.status == "blocked" for item in values):
            decisive = next(item for item in values if item.status == "blocked")
            return RecoveryDecision(
                "blocked",
                decisive.reason,
                tuple(identifier for item in values for identifier in item.attempt_ids),
                run_id,
                dict(decisive.details),
            )
        if any(item.status == "needs_owner" for item in values):
            decisive = next(item for item in values if item.status == "needs_owner")
            return RecoveryDecision(
                "needs_owner",
                decisive.reason,
                tuple(identifier for item in values for identifier in item.attempt_ids),
                run_id,
                dict(decisive.details),
            )
        return RecoveryDecision(
            "resume_safe",
            "所有已记录的工具尝试都有可安全处理的状态",
            tuple(identifier for item in values for identifier in item.attempt_ids),
            run_id,
        )

    def _attempts(
        self, *, run_id: str | None = None, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        if run_id is not None:
            rows = self.store.db.execute(
                "SELECT data FROM objects WHERE kind='tool_attempt' "
                "AND json_extract(data,'$.run_id')=? ORDER BY rowid LIMIT 4096",
                (run_id,),
            ).fetchall()
        elif session_id is not None:
            rows = self.store.db.execute(
                "SELECT data FROM objects WHERE kind='tool_attempt' "
                "AND json_extract(data,'$.session_id')=? ORDER BY rowid LIMIT 4096",
                (session_id,),
            ).fetchall()
        else:
            return []
        return [json.loads(row[0]) for row in rows]

    def _run_ids(self, session_id: str) -> list[str]:
        rows = self.store.db.execute(
            "SELECT id FROM objects WHERE kind='run' "
            "AND json_extract(data,'$.session_id')=? "
            "AND json_extract(data,'$.status') IN ('pending','queued','running','paused','interrupted') "
            "ORDER BY rowid LIMIT 256",
            (session_id,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def _pending_approvals(self, session_id: str) -> list[str]:
        if not session_id:
            return []
        rows = self.store.db.execute(
            "SELECT id FROM objects WHERE kind='approval' "
            "AND json_extract(data,'$.session_id')=? "
            "AND json_extract(data,'$.status')='pending' ORDER BY rowid LIMIT 256",
            (session_id,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def _outbox_risk(self) -> bool:
        try:
            row = self.store.db.execute(
                "SELECT 1 FROM channel_outbox WHERE state IN ('sending','unknown') LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            # A lower-level Store may not have configured channel delivery at
            # all; absence of the table cannot hide an in-flight send.
            return False
        return row is not None

    def _managed_risk(self) -> bool:
        if self.managed_unresolved is None:
            return False
        try:
            return bool(self.managed_unresolved())
        except Exception:
            return True


def side_effect_risk(risk: str, recovery: str) -> bool:
    return risk in _SIDE_EFFECT_RISKS or recovery != "replayable"
