"""Run-local, revocable questions to an idle main assistant."""

from __future__ import annotations

import asyncio
import math
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from muli.store import new_id, now

if TYPE_CHECKING:
    from muli.runtime import PreparedRequest
    from muli.team import TeamManager


@dataclass(slots=True)
class ConsultRequest:
    id: str
    main_id: str
    child_id: str
    child_run_id: str
    generation: int
    question: str
    child_task: asyncio.Task[str]
    child_request: PreparedRequest
    proof: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]
    deadline: float
    input_seq: int
    wait_token: object | None = None
    main_run_id: str | None = None
    control_task: asyncio.Task[str] | None = None
    cancelled_reason: str | None = None


class TeamConsult:
    def __init__(self, team: TeamManager) -> None:
        self.team = team
        self._requests: dict[str, ConsultRequest] = {}

    def available(self, child_id: str, run_id: str | None, generation: int | None) -> bool:
        runtime = self.team.runtime
        if (
            run_id is None
            or generation is None
            or generation < 1
            or runtime._closing
            or self.team._closed
        ):
            return False
        agent = runtime.store.get("agent", child_id)
        return bool(
            agent
            and agent.get("parent_id") == runtime.store.existing_main_session_id()
            and agent.get("role") != "reviewer"
            and agent.get("commitment_id") is None
            and agent.get("status") == "running"
            and agent.get("run_id") == run_id
            and agent.get("generation") == generation
            and runtime.channel_ownership.current_proof(child_id, run_id) is not None
        )

    def guard_child(self, request: ConsultRequest) -> None:
        runtime = self.team.runtime
        runtime.maintenance.admission.require()
        budget = runtime.config.settings.limits.max_output_chars * 4
        agent = runtime.store.bounded_object("agent", request.child_id, max_bytes=budget)
        run = runtime.store.bounded_object("run", request.child_run_id, max_bytes=budget)
        session = runtime.store.get("session", request.child_id)
        if (
            runtime._closing
            or self.team._closed
            or request.cancelled_reason is not None
            or asyncio.get_running_loop().time() >= request.deadline
            or self._new_owner_input(request)
            or "team_consult" in runtime.config.settings.disabled_tools
            or runtime.store.existing_main_session_id() != request.main_id
            or runtime.tasks.get(request.child_id) is not request.child_task
            or request.child_task.done()
            or request.child_task.cancelling()
            or runtime.run_ids.get(request.child_id) != request.child_run_id
            or self.team._parent_stopping(request.child_id)
            or request.child_task in self.team._stopping_tasks
            or agent is None
            or agent.get("parent_id") != request.main_id
            or agent.get("session_id") != request.child_id
            or agent.get("role") == "reviewer"
            or agent.get("commitment_id") is not None
            or agent.get("status") != "running"
            or agent.get("run_id") != request.child_run_id
            or type(agent.get("generation")) is not int
            or agent["generation"] != request.generation
            or request.generation < 1
            or run is None
            or run.get("session_id") != request.child_id
            or run.get("status") != "running"
            or session is None
            or session.get("workflow_run_id")
            or request.child_request.consult is not None
            or request.child_request.execution_scope is not None
            or request.child_request.selected_replan is not None
            or runtime.store.pending_inputs(request.child_id)
        ):
            raise PermissionError("咨询所属队友执行已改变或收到新输入")
        runtime._guard_request(request.child_request, "read")
        proof = runtime.channel_ownership.current_proof(request.child_id, request.child_run_id)
        if proof != request.proof or request.child_request.channel_ownership != request.proof:
            raise PermissionError("咨询不再属于原运行实际接纳的渠道输入")
        if runtime.channel_ownership.return_request(proof):
            raise PermissionError("归还控制通知不能发起咨询")

    async def ask(
        self,
        child_id: str,
        run_id: str | None,
        generation: int | None,
        question: str,
        wait_seconds: float,
    ) -> dict[str, Any]:
        runtime = self.team.runtime
        prepared = runtime.request_context(child_id)
        task = runtime.tasks.get(child_id)
        if (
            run_id is None
            or generation is None
            or prepared is None
            or prepared.channel_ownership is None
            or asyncio.current_task() is not task
            or task is None
            or runtime.tool_session_id != child_id
            or runtime._tool_execution.get() != (child_id, run_id)
        ):
            raise PermissionError("咨询缺少当前队友的真实工具执行上下文")
        limits = runtime.config.settings.limits
        if not isinstance(question, str) or not question.strip():
            raise ValueError("咨询问题不能为空")
        if len(question) > limits.max_context_chars:
            raise ValueError("咨询问题超过当前上下文预算")
        if (
            isinstance(wait_seconds, bool)
            or not math.isfinite(wait_seconds)
            or not 0 < wait_seconds <= limits.max_run_seconds
        ):
            raise ValueError("咨询等待时间超出运行预算")
        if run_id in self._requests:
            raise ValueError("本轮已有尚未收口的咨询")
        request = ConsultRequest(
            new_id(),
            prepared.channel_ownership["main_id"],
            child_id,
            run_id,
            generation,
            question,
            task,
            prepared,
            deepcopy(prepared.channel_ownership),
            asyncio.get_running_loop().create_future(),
            asyncio.get_running_loop().time() + wait_seconds,
            runtime.store.db.execute("SELECT COALESCE(MAX(seq),0) FROM ingress").fetchone()[0],
        )
        self.guard_child(request)
        # Register the real child -> main edge before exposing the queue entry.
        request.wait_token = self.team._waiter._register(child_id, {request.main_id})
        self._requests[run_id] = request
        try:
            try:
                runtime._wake_main()
                async with asyncio.timeout(wait_seconds):
                    while not request.future.done():
                        revision = runtime.bus.revision
                        try:
                            self.guard_child(request)
                        except (ValueError, PermissionError) as error:
                            self.cancel(request, str(error))
                            break

                        def changed(expected: int = revision) -> bool:
                            return request.future.done() or runtime.bus.revision != expected

                        async with runtime.bus.changed:
                            await runtime.bus.changed.wait_for(changed)
            except TimeoutError:
                self.cancel(request, "咨询等待达到期限，迟到回答不会交付")
            return self._deliver(request)
        finally:
            if not request.future.done():
                self.cancel(request, "咨询所属工具调用已结束")
            self._unregister(request)
            self._requests.pop(run_id, None)

    def _new_owner_input(self, request: ConsultRequest) -> bool:
        # A newly admitted input stays revoking even after a later ordinary run
        # has claimed it. The existing journal's sequence is never reused.
        return (
            self.team.runtime.store.db.execute(
                "SELECT 1 FROM ingress WHERE session_id=? AND seq>? "
                "AND json_extract(payload,'$.principal')='owner' LIMIT 1",
                (request.main_id, request.input_seq),
            ).fetchone()
            is not None
        )

    def _deliver(self, request: ConsultRequest) -> dict[str, Any]:
        if request.cancelled_reason is None and "answer" in request.future.result():
            try:
                self.guard_child(request)
                runtime = self.team.runtime
                if request.main_id in runtime._stopping or runtime.store.pending_inputs(
                    request.main_id
                ):
                    raise PermissionError("咨询回答交付前已有新的主会话输入")
            except (ValueError, PermissionError) as error:
                self.cancel(request, str(error))
        if request.cancelled_reason is not None:
            return {"error": request.cancelled_reason, "request_id": request.id}
        return request.future.result()

    def next_request(self, main_id: str) -> ConsultRequest | None:
        runtime = self.team.runtime
        if len(runtime.tasks) >= runtime.config.settings.limits.max_pending_runs:
            return None
        for request in tuple(self._requests.values()):
            if (
                request.main_id != main_id
                or request.future.done()
                or request.main_run_id is not None
            ):
                continue
            try:
                self.guard_child(request)
            except (ValueError, PermissionError) as error:
                self.cancel(request, str(error))
                continue
            return request
        return None

    def bind(self, request: ConsultRequest, run_id: str, task: asyncio.Task[str]) -> None:
        request.main_run_id, request.control_task = run_id, task

        def cancelled_before_start(ended: asyncio.Task[str]) -> None:
            runtime = self.team.runtime
            if (
                not ended.cancelled()
                or runtime.tasks.get(request.main_id) is not ended
                or runtime.run_ids.get(request.main_id) != run_id
            ):
                return
            run = runtime.store.get("run", run_id)
            if run is None or run.get("status") != "queued":
                return
            # A coroutine cancelled before its first schedule never enters the
            # driver's try/finally and has acquired no execution resources.
            run.update(status="cancelled", finished_at=now())
            session = runtime.store.get("session", request.main_id)
            writes = [("run", run)]
            if session is not None:
                session.update(status="idle", updated_at=now())
                writes.append(("session", session))
            runtime.store.event(
                "run.cancelled", {"run_id": run_id}, request.main_id, objects=writes
            )
            runtime.tasks.pop(request.main_id, None)
            runtime.run_ids.pop(request.main_id, None)
            self.cancel(request, "咨询控制轮在开始前已取消")
            runtime._wake_main()

        task.add_done_callback(cancelled_before_start)

    def guard_control(self, request: ConsultRequest, session_id: str) -> None:
        runtime = self.team.runtime
        if (
            self._requests.get(request.child_run_id) is not request
            or request.future.done()
            or session_id != request.main_id
            or request.main_run_id is None
            or runtime.run_ids.get(session_id) != request.main_run_id
            or runtime.tasks.get(session_id) is not request.control_task
            or asyncio.current_task() is not request.control_task
            or request.control_task is None
            or request.control_task.cancelling()
            or session_id in runtime._stopping
            or runtime.store.pending_inputs(session_id)
        ):
            raise PermissionError("咨询控制轮已失效或主人输入正在等待")
        self.guard_child(request)

    def complete(self, request: ConsultRequest, run: dict[str, Any]) -> None:
        if request.future.done():
            return
        runtime = self.team.runtime
        try:
            self.guard_child(request)
            if (
                self._requests.get(request.child_run_id) is not request
                or request.control_task is not asyncio.current_task()
                or request.main_run_id != run["id"]
                or run["status"] != "completed"
                or request.main_id in runtime._stopping
                or runtime.store.pending_inputs(request.main_id)
            ):
                raise PermissionError("咨询控制轮未完成或已有新的主人输入")
        except (ValueError, PermissionError) as error:
            self.cancel(request, str(error))
            return
        request.future.set_result(
            {
                "answer": run["result"],
                "request_id": request.id,
                "child_run_id": request.child_run_id,
                "main_run_id": run["id"],
            }
        )

    def cancel(self, request: ConsultRequest, reason: str) -> None:
        if request.cancelled_reason is None:
            request.cancelled_reason = reason
        if not request.future.done():
            request.future.set_result({"error": reason, "request_id": request.id})
        task = request.control_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            if not task.cancelling():
                task.cancel()
        self._unregister(request)

    def _unregister(self, request: ConsultRequest) -> None:
        self.team._waiter._unregister(request.child_id, request.wait_token)
        request.wait_token = None

    def invalidate(self, session_id: str | None, reason: str) -> None:
        for request in tuple(self._requests.values()):
            if session_id is None or session_id in {request.child_id, request.main_id}:
                self.cancel(request, reason)
