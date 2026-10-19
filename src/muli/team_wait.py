"""Observer-local team completion cursors; waiting never consumes an inbox."""

from __future__ import annotations

import asyncio
import base64
import heapq
import json
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from muli.team import TeamManager

TERMINAL = {"completed", "failed", "cancelled", "interrupted", "idle"}


def _encode(team: TeamManager, caller: str | None, seen: dict[str, int]) -> str:
    payload = {
        "v": 1,
        "store": team.runtime.store.instance_id,
        "caller": caller,
        "seen": seen,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _revision(agent: dict[str, Any]) -> int:
    value = agent.get("completion_revision", 0)
    if type(value) is not int or value < 0:
        raise ValueError("wait_cursor_stale: 队友完成修订无效")
    return value


def _states(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": agent["id"],
            "name": agent["name"],
            "status": agent["status"],
            "generation": agent.get("generation", 0),
            "completion_revision": _revision(agent),
        }
        for agent in agents
    ]


class IncrementalWait:
    def __init__(
        self, team: TeamManager, identifiers: set[str], caller: str | None, cursor: str
    ) -> None:
        self.team, self.identifiers, self.caller = team, identifiers, caller
        budget = team.runtime.config.settings.limits.max_output_chars
        if not isinstance(cursor, str) or not cursor:
            raise ValueError("wait_cursor_invalid: 游标必须是非空字符串")
        if len(cursor) > budget:
            raise ValueError("wait_budget_exceeded: 游标超过输出预算，请拆分等待目标")
        try:
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            payload = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("wait_cursor_invalid: 游标格式无效") from exc
        if not isinstance(payload, dict) or set(payload) != {"v", "store", "caller", "seen"}:
            raise ValueError("wait_cursor_invalid: 游标结构无效")
        if (
            type(payload["v"]) is not int
            or payload["v"] != 1
            or payload["store"] != team.runtime.store.instance_id
            or payload["caller"] != caller
        ):
            raise ValueError("wait_cursor_stale: 游标版本、数据集或调用方不匹配")
        seen = payload["seen"]
        if not isinstance(seen, dict) or not seen:
            raise ValueError("wait_cursor_invalid: 游标缺少观察范围")
        for identifier, revision in seen.items():
            if not identifier or type(revision) is not int or revision < 0:
                raise ValueError("wait_cursor_invalid: 完成修订必须是非负整数")
            agent = team.runtime.store.get("agent", identifier)
            if agent is None or revision > _revision(agent):
                raise ValueError("wait_cursor_stale: 目标不存在或游标来自未来")
        agents = self.agents()
        self.scope_changed = set(seen) != identifiers
        self.baseline = [
            {"agent_id": agent["id"], "completion_revision": _revision(agent)}
            for agent in agents
            if agent["id"] not in seen
        ]
        self.seen = {agent["id"]: seen.get(agent["id"], _revision(agent)) for agent in agents}
        self._check_budget(agents)

    def agents(self) -> list[dict[str, Any]]:
        agents = []
        for identifier in sorted(self.identifiers):
            agent = self.team.runtime.store.get("agent", identifier)
            if agent is None:
                raise ValueError("wait_cursor_stale: 观察目标已不存在")
            agents.append(agent)
        return agents

    def _response(
        self,
        agents: list[dict[str, Any]],
        *,
        reason: str,
        seen: dict[str, int] | None = None,
        completed: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        seen = self.seen if seen is None else seen
        response = {
            "agents": _states(agents),
            "completed": completed or [],
            "cursor": _encode(self.team, self.caller, seen),
            "reason": reason,
            "timed_out": reason == "timeout",
            "has_more": any(_revision(agent) > seen[agent["id"]] for agent in agents),
        }
        if self.baseline:
            response["baseline"] = self.baseline
        if reason in {"new_input", "mailbox"}:
            response["new_input"] = True
        return response

    def _fits(self, response: dict[str, Any]) -> bool:
        return (
            len(json.dumps(response, ensure_ascii=False))
            <= self.team.runtime.config.settings.limits.max_output_chars
        )

    def _check_budget(self, agents: list[dict[str, Any]]) -> None:
        for agent in agents:
            if self.seen[agent["id"]] > _revision(agent):
                raise ValueError("wait_cursor_stale: 完成修订已回退")
        # Reserve the longest possible cursor for this snapshot and the extra
        # inbox flag before blocking; do not let Runtime truncate an opaque token.
        future = {agent["id"]: _revision(agent) for agent in agents}
        response = self._response(agents, reason="scope_changed", seen=future)
        response["new_input"] = True
        if not self._fits(response):
            raise ValueError("wait_budget_exceeded: 状态与游标超过输出预算，请拆分等待目标")

    def _next(self, agent: dict[str, Any], after: int) -> dict[str, Any] | None:
        if after == _revision(agent):
            return None
        events = self.team.runtime.store.team_completions(agent["session_id"], after)
        if (
            not events
            or events[0]["data"].get("agent_id") != agent["id"]
            or events[0]["data"].get("completion_revision") != after + 1
            or events[0]["data"].get("status") not in TERMINAL
        ):
            raise ValueError("wait_cursor_stale: 完成日志存在缺口，请重新建立观察基线")
        return events[0]

    def _completions(self, agents: list[dict[str, Any]]) -> dict[str, Any] | None:
        seen = dict(self.seen)
        by_id = {agent["id"]: agent for agent in agents}
        pending: list[tuple[int, str, dict[str, Any]]] = []
        for agent in agents:
            event = self._next(agent, seen[agent["id"]])
            if event:
                heapq.heappush(pending, (event["id"], agent["id"], event))
        completed: list[dict[str, Any]] = []
        response: dict[str, Any] | None = None
        while pending:
            _, identifier, event = heapq.heappop(pending)
            data = event["data"]
            record = {
                **data,
                "history": {
                    "session_id": event["session_id"],
                    "run_id": data.get("run_id"),
                    "event_id": event["id"],
                },
                "coalesced": False,
                "count": 1,
            }
            preview = record["result_preview"]
            next_seen = {**seen, identifier: data["completion_revision"]}
            # Binary search a legal preview while retaining the entire result
            # reference and cursor. Even JSON escaping counts toward the budget.
            low, high = 0, len(preview)
            fitted: dict[str, Any] | None = None
            while low <= high:
                length = (low + high) // 2
                candidate = {
                    **record,
                    "result_preview": preview[:length],
                    "truncated": record["truncated"] or length < len(preview),
                }
                attempt = self._response(
                    agents,
                    reason="completed",
                    seen=next_seen,
                    completed=[*completed, candidate],
                )
                if self._fits(attempt):
                    fitted = attempt
                    low = length + 1
                else:
                    high = length - 1
            if fitted is None:
                if not completed:
                    raise ValueError("wait_budget_exceeded: 完成记录元数据超过输出预算，请拆分目标")
                break
            response = fitted
            completed = fitted["completed"]
            seen = next_seen
            following = self._next(by_id[identifier], seen[identifier])
            if following:
                heapq.heappush(pending, (following["id"], identifier, following))
        return response

    def poll(self, *, timed_out: bool = False) -> dict[str, Any] | None:
        agents = self.agents()
        self._check_budget(agents)
        store = self.team.runtime.store
        if (
            self.caller
            and store.db.execute(
                "SELECT 1 FROM ingress WHERE session_id=? AND state='pending' "
                "AND json_extract(payload,'$.principal')='owner' LIMIT 1",
                (self.caller,),
            ).fetchone()
        ):
            return self._response(agents, reason="new_input")
        completed = self._completions(agents)
        if completed:
            return completed
        if self.caller and store.pending_inputs(self.caller):
            return self._response(agents, reason="mailbox")
        if self.scope_changed:
            return self._response(agents, reason="scope_changed")
        if timed_out:
            return self._response(agents, reason="timeout")
        return None


class TeamWait:
    def __init__(self, team: TeamManager) -> None:
        self.team = team
        self._registrations: dict[str, dict[object, set[str]]] = {}

    def _register(self, caller: str | None, identifiers: set[str]) -> object | None:
        if caller is None:
            return None

        pending, visited = list(identifiers), set()
        while pending:
            node = pending.pop()
            if node == caller:
                raise ValueError("队友等待形成循环")
            if node in visited:
                continue
            visited.add(node)
            pending.extend(self.team._waiting.get(node, set()))
        token = object()
        registrations = self._registrations.setdefault(caller, {})
        registrations[token] = identifiers
        self.team._waiting[caller] = set().union(*registrations.values())
        return token

    def _unregister(self, caller: str | None, token: object | None) -> None:
        if caller is None or token is None:
            return
        registrations = self._registrations[caller]
        del registrations[token]
        if registrations:
            self.team._waiting[caller] = set().union(*registrations.values())
        else:
            del self._registrations[caller]
            self.team._waiting.pop(caller, None)

    def _legacy(
        self, identifiers: set[str], caller: str | None, *, timed_out: bool
    ) -> dict[str, Any]:
        agents = [self.team.get(identifier) for identifier in sorted(identifiers)]
        response = {"agents": agents, "timed_out": timed_out}
        if not timed_out and not any(agent["status"] in TERMINAL for agent in agents):
            if caller is None or not self.team.runtime.store.pending_inputs(caller):
                return {}
            response["new_input"] = True
        response["cursor"] = _encode(
            self.team, caller, {agent["id"]: _revision(agent) for agent in agents}
        )
        response["reason"] = (
            "timeout" if timed_out else "new_input" if response.get("new_input") else "completed"
        )
        return response

    def tool_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Keep a usable initial cursor when a legacy tool snapshot would be truncated."""
        budget = self.team.runtime.config.settings.limits.max_output_chars

        def fits(response: dict[str, Any]) -> bool:
            return len(json.dumps(response, ensure_ascii=False)) <= budget

        if fits(snapshot):
            return snapshot
        agents = snapshot["agents"]
        response = {
            **snapshot,
            "agents": [
                {
                    **state,
                    "result_preview": "",
                    "truncated": bool(agent.get("result")),
                    "history": {"session_id": agent["session_id"]},
                }
                for agent, state in zip(agents, _states(agents), strict=True)
            ],
            "legacy_snapshot": True,
            "truncated": True,
            "baseline": [
                {"agent_id": agent["id"], "completion_revision": _revision(agent)}
                for agent in agents
            ],
        }
        if not fits(response):
            raise ValueError("wait_budget_exceeded: 快照与游标元数据超过预算，请拆分等待目标")
        for agent, state in zip(agents, response["agents"], strict=True):
            preview = agent.get("result", "")
            low, high, selected = 0, len(preview), 0
            while low <= high:
                length = (low + high) // 2
                state.update(result_preview=preview[:length], truncated=length < len(preview))
                if fits(response):
                    selected = length
                    low = length + 1
                else:
                    high = length - 1
            state.update(result_preview=preview[:selected], truncated=selected < len(preview))
        return response

    async def wait(
        self,
        targets: list[str],
        *,
        timeout: float,
        caller: str | None,
        cursor: str | None,
    ) -> dict[str, Any]:
        identifiers = {self.team.get(target)["id"] for target in targets}
        limits = self.team.runtime.config.settings.limits
        if not identifiers or len(identifiers) > limits.max_pending_runs:
            raise ValueError("等待目标为空或超过未完成任务预算")
        if not math.isfinite(timeout) or timeout <= 0 or timeout > limits.max_run_seconds:
            raise ValueError("等待时间超出运行预算")
        incremental = (
            IncrementalWait(self.team, identifiers, caller, cursor) if cursor is not None else None
        )
        token = self._register(caller, identifiers)
        try:
            async with asyncio.timeout(timeout):
                while True:
                    revision = self.team.runtime.bus.revision
                    response = (
                        incremental.poll()
                        if incremental
                        else self._legacy(identifiers, caller, timed_out=False)
                    )
                    if response:
                        return response

                    def changed(expected: int = revision) -> bool:
                        return self.team.runtime.bus.revision != expected

                    async with self.team.runtime.bus.changed:
                        await self.team.runtime.bus.changed.wait_for(changed)
        except TimeoutError:
            if incremental:
                response = incremental.poll(timed_out=True)
                assert response is not None
                return response
            return self._legacy(identifiers, caller, timed_out=True)
        finally:
            self._unregister(caller, token)
