"""One real owner/Team/shell journey for process-local command continuation."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from jsonschema import Draft202012Validator
from test_runtime import FakeClient
from test_shell_session_runtime import (
    ShellDialogue,
    ShellScript,
    completed_shell,
    shell_python_command,
    shell_runtime,
)
from test_task_briefs import attach_services
from test_teammate_attachment_runtime import call

from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.shell_sessions import ShellFinished
from muli.store import new_id
from muli.task_control import OwnerControlRequest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="首片真实命令与双流游标旅程限 POSIX；原生 Windows 独立验收"
)


async def wait_team_state(
    runtime: Runtime, target: str, predicate: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    async with asyncio.timeout(5), runtime.bus.changed:
        await runtime.bus.changed.wait_for(lambda: predicate(runtime.team.get(target)))
    return runtime.team.get(target)


def paired_tool(runtime: Runtime, session_id: str, call_id: str) -> dict[str, Any]:
    matches = [
        message
        for message in runtime.store.messages(session_id)
        if message["role"] == "tool" and message.get("tool_call_id") == call_id
    ]
    assert len(matches) == 1, {"call_id": call_id, "paired_messages": len(matches)}
    message = matches[0]
    assert message.get("content_provenance") == "accepted", message
    return message


@dataclass
class RegisteredShellWait:
    runtime: Runtime
    release: Path
    counter: Path
    target: str = ""
    task_id: str = ""
    command: dict[str, Any] = field(default_factory=dict)
    original: dict[str, Any] = field(default_factory=dict)
    worker_requests: list[list[Message]] = field(default_factory=list)


@asynccontextmanager
async def registered_shell_wait(
    tmp_path: Path,
    *,
    configure: Callable[[RegisteredShellWait], None] | None = None,
    resumed: Callable[[RegisteredShellWait, list[Message]], Awaitable[ModelTurn]] | None = None,
    expected_status: str = "completed",
    check_registration: bool = True,
) -> AsyncIterator[RegisteredShellWait]:
    """Real owner/task/paired tools; yield only after the waiting generation releases."""
    runtime = await shell_runtime(tmp_path)
    attach_services(runtime, tmp_path)
    state = RegisteredShellWait(runtime, tmp_path / "release", tmp_path / "effects")
    script = tmp_path / "controlled-command.py"
    script.write_text(
        "from pathlib import Path\nimport time\n\n"
        "def main() -> None:\n"
        f"    release: Path = Path({str(state.release)!r})\n"
        f"    counter: Path = Path({str(state.counter)!r})\n"
        "    with counter.open('a', encoding='utf-8') as effects:\n"
        "        effects.write('once' + chr(10))\n"
        "    print('COMMAND_READY', flush=True)\n"
        "    deadline: float = time.monotonic() + 15\n"
        "    while not release.exists():\n"
        "        if time.monotonic() >= deadline:\n"
        "            raise SystemExit(9)\n"
        "        time.sleep(0.01)\n"
        "    raise SystemExit(7)\n\n"
        "if __name__ == '__main__':\n    main()\n",
        encoding="utf-8",
    )

    def owner_script() -> ShellScript:
        task = yield call(
            "wait-task",
            "commitment_save",
            title="真实命令完成后继续核查",
            brief={"goal": "原队友等待真实命令结束后核查", "constraints": ["不得重做命令"]},
        )
        state.task_id = task["id"]
        agent = yield call(
            "wait-worker",
            "spawn_agent",
            task_name="waiting-worker",
            prompt="运行受控命令并登记本实例内完成续办。",
            commitment_id=state.task_id,
        )
        state.target = agent["id"]
        return "已委派，任务尚未验收。"

    def worker_script() -> ShellScript:
        boot = yield from completed_shell(
            call("wait-boot", "shell_run", command="echo WAIT_BOOT", cwd=str(tmp_path))
        )
        state.command = yield call(
            "wait-command",
            "shell_run",
            command=shell_python_command(boot["flavor"], script),
            wait_seconds=0,
            timeout=20,
        )
        page = state.command
        for index in range(4):
            if "COMMAND_READY" in page["output"]:
                break
            page = yield call(
                f"wait-ready-{index}",
                "shell_read",
                command_id=state.command["command_id"],
                cursor=page["next_cursor"],
                wait_seconds=1,
            )
        assert "COMMAND_READY" in page["output"] and page["state"] == "running"
        assert state.counter.read_text(encoding="utf-8").splitlines() == ["once"]
        receipt = yield call(
            "wait-register", "shell_continue_on_finish", command_id=state.command["command_id"]
        )
        if check_registration:
            raise AssertionError(f"登记不应再请求原代模型：{receipt}")
        return "登记回执未确认有效，任务尚未验收。"

    owner, worker = ShellDialogue(owner_script()), ShellDialogue(worker_script())
    owner_finished = False

    class Client(FakeClient):
        def __init__(self, worker_model: bool) -> None:
            super().__init__([], [])
            self.worker_model = worker_model

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal owner_finished
            if not self.worker_model:
                turn = (
                    ModelTurn(content="收到实际报告。")
                    if owner_finished
                    else owner.advance(messages)
                )
                owner_finished = not turn.tool_calls
            else:
                state.worker_requests.append(list(messages))
                agent = runtime.team.get(state.target or "waiting-worker")
                state.target = agent["id"]
                if agent["generation"] == 1:
                    turn = worker.advance(messages)
                elif resumed is not None:
                    turn = await resumed(state, messages)
                else:
                    turn = ModelTurn(content="原队友已继续，具体结果尚待核查。")
            schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
            for request in turn.tool_calls:
                assert request.name in schemas
                Draft202012Validator(schemas[request.name]).validate(json.loads(request.arguments))
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda profile: Client(profile.model == "worker")
    if configure is not None:
        configure(state)
    try:
        initial = await runtime.submit("让原队友等待真实命令结束后核查，不得重做。", mode="yolo")
        async with asyncio.timeout(5):
            await runtime.wait(initial["run_id"])
        state.original = await wait_team_state(
            runtime,
            state.target,
            lambda agent: (
                agent["status"] == expected_status
                and agent["generation"] == 1
                and state.target not in runtime.tasks
                and state.target not in runtime.team._active
            ),
        )
        if check_registration:
            receipt = json.loads(paired_tool(runtime, state.target, "wait-register")["content"])
            assert receipt["registered"] is True
            assert receipt["automatic_resume_after_restart"] is False
        yield state
    finally:
        state.release.write_text("cleanup", encoding="utf-8")
        await runtime.close()


async def test_real_command_finish_continues_original_teammate_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("此旅程只允许真实本机 shell 控制通道，不允许外部 HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime = await shell_runtime(tmp_path)
    attach_services(runtime, tmp_path)
    nonce = new_id()
    release = tmp_path / "release-command"
    counter = tmp_path / "command-effects.txt"
    product = tmp_path / "export-result.txt"
    checked = tmp_path / "checked-result.txt"
    script = tmp_path / "controlled-export.py"
    private_output = f"未读导出尾部-{nonce}"
    private_error = f"未读错误尾部-{nonce}"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "\n"
        "def main() -> None:\n"
        f"    release: Path = Path({str(release)!r})\n"
        f"    counter: Path = Path({str(counter)!r})\n"
        f"    product: Path = Path({str(product)!r})\n"
        "    with counter.open('a', encoding='utf-8') as effects:\n"
        f"        effects.write({nonce!r} + chr(10))\n"
        "    print('EXPORT_READY_OUT', flush=True)\n"
        "    print('EXPORT_READY_ERR', file=sys.stderr, flush=True)\n"
        "    deadline: float = time.monotonic() + 8\n"
        "    while not release.exists():\n"
        "        if time.monotonic() >= deadline:\n"
        "            raise SystemExit(9)\n"
        "        time.sleep(0.01)\n"
        f"    product.write_text({nonce!r}, encoding='utf-8')\n"
        f"    print({private_output!r}, flush=True)\n"
        f"    print({private_error!r}, file=sys.stderr, flush=True)\n"
        "    raise SystemExit(7)\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    task_id = target = ""
    launched: dict[str, Any] = {}
    continuation_schema: dict[str, Any] | None = None
    registration_fell_through: dict[str, Any] | None = None
    original_cursor: dict[str, int] = {"output": 0, "stderr": 0}
    command_text = ""
    worker_requests: list[list[Message]] = []
    checked_pages: list[dict[str, Any]] = []
    main_requests: list[list[Message]] = []
    probe_requests: list[list[Message]] = []
    main_finished = False
    final_report = f"已核查原命令退出码7和临时产物：{nonce}"

    def main_script() -> ShellScript:
        nonlocal task_id, target
        saved = yield call(
            "save-event-resume-task",
            "commitment_save",
            title="命令结束后核查原导出",
            brief={
                "goal": "让原队友运行临时导出，真实结束后核查原回执和产物，再回报主助手",
                "constraints": ["不得轮询模型", "不得重新执行导出", "不得自动外发"],
            },
        )
        task_id = saved["id"]
        spawned = yield call(
            "spawn-event-resume-worker",
            "spawn_agent",
            task_name="event-resume-worker",
            prompt="运行临时导出，登记本实例内命令结束续办，再核查原回执与产物，最后回报。",
            commitment_id=task_id,
        )
        target = spawned["id"]
        return "导出已交给原队友；命令和任务状态以实际回执为准。"

    def initial_worker() -> ShellScript:
        nonlocal launched, original_cursor, command_text, registration_fell_through
        boot = yield from completed_shell(
            call("resume-boot", "shell_run", command="echo RESUME_BOOT", cwd=str(tmp_path))
        )
        command_text = shell_python_command(boot["flavor"], script)
        launched = yield call(
            "resume-command", "shell_run", command=command_text, wait_seconds=0, timeout=10
        )
        assert "error" not in launched and launched["state"] == "running", launched
        pages = [launched]
        original_cursor = launched["next_cursor"].copy()
        for index in range(4):
            if "EXPORT_READY_OUT" in "".join(page["output"] for page in pages) and (
                "EXPORT_READY_ERR" in "".join(page["stderr"] for page in pages)
            ):
                break
            page = yield call(
                f"resume-ready-{index}",
                "shell_read",
                command_id=launched["command_id"],
                cursor=original_cursor,
                wait_seconds=1,
            )
            assert "error" not in page and page["state"] == "running", page
            pages.append(page)
            original_cursor = page["next_cursor"].copy()
        assert "EXPORT_READY_OUT" in "".join(page["output"] for page in pages)
        assert "EXPORT_READY_ERR" in "".join(page["stderr"] for page in pages)
        assert not release.exists() and not product.exists()
        assert counter.read_text(encoding="utf-8").splitlines() == [nonce]
        registration_fell_through = yield call(
            "register-shell-continuation",
            "shell_continue_on_finish",
            command_id=launched["command_id"],
        )
        # The old product returns a real "tool disabled" receipt. A successful
        # registration must end this generation without requesting another turn.
        assert "error" in registration_fell_through, registration_fell_through
        return "登记未生效；真实命令仍在运行。"

    def resumed_worker() -> ShellScript:
        cursor = original_cursor.copy()
        for index in range(3):
            page = yield call(
                f"resume-finished-read-{index}",
                "shell_read",
                command_id=launched["command_id"],
                cursor=cursor,
            )
            assert "error" not in page and page["state"] == "completed", page
            assert page["command_id"] == launched["command_id"]
            assert page["os_epoch"] == launched["os_epoch"] and page["exit_code"] == 7
            checked_pages.append(page)
            cursor = page["next_cursor"].copy()
            if sum(cursor.values()) == page["observed_bytes"]:
                break
        assert private_output in "".join(page["output"] for page in checked_pages)
        assert private_error in "".join(page["stderr"] for page in checked_pages)
        original = yield call("resume-read-product", "fs_read", path=str(product))
        assert nonce in json.dumps(original, ensure_ascii=False)
        written = yield call(
            "resume-write-checked", "fs_write", path=str(checked), content=final_report
        )
        assert "error" not in written, written
        return final_report

    main_dialogue = ShellDialogue(main_script())
    initial_dialogue = ShellDialogue(initial_worker())
    resumed_dialogue = ShellDialogue(resumed_worker())

    class MainClient(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_finished
            main_requests.append(list(messages))
            turn = (
                ModelTurn(content="收到队友实际回报，未自动发送文件。")
                if main_finished
                else main_dialogue.advance(messages)
            )
            main_finished = not turn.tool_calls
            names = {tool["function"]["name"] for tool in tools}
            assert all(request.name in names for request in turn.tool_calls)
            yield StreamEvent("done", turn=turn)

    class WorkerClient(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal continuation_schema
            if any("CAPACITY_PROBE_TASK" in message.content for message in messages):
                probe_requests.append(list(messages))
                yield StreamEvent("done", turn=ModelTurn(content="独立队友已取得模型容量。"))
                return
            worker_requests.append(list(messages))
            agent = runtime.team.get("event-resume-worker")
            assert agent["parent_id"] == runtime.main_session()["id"]
            assert agent["commitment_id"] == task_id
            dialogue = initial_dialogue if agent["generation"] == 1 else resumed_dialogue
            turn = dialogue.advance(messages)
            schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
            for request in turn.tool_calls:
                if request.name != "shell_continue_on_finish":
                    assert request.name in schemas
                    continue
                continuation_schema = schemas.get(request.name)
                # All pre-existing production paths have really completed
                # before the new feature is requested through the actual handler.
                command = paired_tool(runtime, agent["id"], "resume-command")
                assert json.loads(command["content"]) == launched
                assert runtime.shells.status(agent["id"])["state"] == "running"
                assert agent["generation"] == 1 and agent["status"] == "running"
                assert runtime.task_control is not None
                scope = runtime.task_control.execution_snapshot(agent["id"])
                assert scope is not None and scope.commitment_id == task_id
                assert scope.epoch == scope.approved_epoch == 0
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return WorkerClient([], []) if config.model == "worker" else MainClient([], [])

    runtime.client_factory = factory
    try:
        first = await runtime.submit(
            "让原队友运行临时导出，命令真实结束后接着核查原回执与产物；不要轮询或重做。",
            mode="yolo",
        )
        async with asyncio.timeout(5):
            await runtime.wait(first["run_id"])
        assert target and task_id
        original = await wait_team_state(
            runtime,
            target,
            lambda agent: (
                agent["status"] == "completed"
                and agent["generation"] == 1
                and target not in runtime.tasks
                and target not in runtime.team._active
            ),
        )
        owner = runtime.store.get("message", first["message"]["id"])
        assert owner is not None and owner["principal"] == "owner" and owner["source"] == "web"
        task = runtime.store.get("commitment", task_id)
        assert task is not None and isinstance(task["brief"], dict) and task["status"] != "done"
        registered = paired_tool(runtime, target, "register-shell-continuation")
        receipt = json.loads(registered["content"])
        assert continuation_schema is not None, (
            "主旅程目标红：真实 owner/完整任务/原队友/受控 shell ready/accepted 原回执均已成立，"
            "实际工具目录仍缺少 shell_continue_on_finish",
            {"actual_tool_receipt": receipt, "generation": original["generation"]},
        )
        Draft202012Validator(continuation_schema).validate({"command_id": launched["command_id"]})
        assert not Draft202012Validator(continuation_schema).is_valid(
            {"command_id": launched["command_id"], "target": runtime.main_session()["id"]}
        )
        assert receipt.get("registered") is True, receipt
        assert receipt["scope"] == "current_service_instance"
        assert receipt["automatic_resume_after_restart"] is False
        assert registration_fell_through is None
        assert runtime.shells.status(target)["state"] == "running"
        assert not product.exists() and not checked.exists()
        assert runtime.store.pending_inputs(target) == 0
        assert not any(
            json.loads(row[0]).get("source") == "shell-continuation"
            for row in runtime.store.db.execute(
                "SELECT payload FROM ingress WHERE session_id=?", (target,)
            )
        )

        requests_while_waiting = len(worker_requests)
        probe = await runtime.team.spawn(
            "capacity-probe", "CAPACITY_PROBE_TASK", parent_id=first["session_id"]
        )
        await wait_team_state(runtime, probe["id"], lambda agent: agent["status"] == "completed")
        assert probe_requests and len(worker_requests) == requests_while_waiting
        assert runtime.team.get(target)["generation"] == 1 and not product.exists()
        release.write_text("finish exactly once", encoding="utf-8")
        finished = await wait_team_state(
            runtime,
            target,
            lambda agent: agent["status"] == "completed" and agent["generation"] == 2,
        )
        assert finished["completion_revision"] == original["completion_revision"] + 1
        assert finished["result"] == final_report
        assert checked.read_text(encoding="utf-8") == final_report
        assert counter.read_text(encoding="utf-8").splitlines() == [nonce]
        assert checked_pages[0]["cursor"] == original_cursor
        facts = [
            message
            for message in runtime.store.messages(target)
            if message["role"] == "user" and message.get("source") == "shell-continuation"
        ]
        assert len(facts) == 1 and facts[0]["delivery_status"] == "delivered"
        assert facts[0]["run_id"] == finished["run_id"]
        assert facts[0]["principal"] != "owner" and facts[0]["route"] is None
        serialized_fact = json.dumps(facts[0], ensure_ascii=False)
        assert launched["command_id"] in serialized_fact and launched["os_epoch"] in serialized_fact
        assert command_text not in serialized_fact
        assert private_output not in serialized_fact and private_error not in serialized_fact
        first_read = paired_tool(runtime, target, "resume-finished-read-0")
        assert json.loads(first_read["content"])["exit_code"] == 7
        reports = runtime.store.db.execute(
            "SELECT data FROM objects WHERE kind='team_report' AND json_extract(data,'$.agent_id')=?",
            (target,),
        ).fetchall()
        assert len(reports) == 1
        assert json.loads(reports[0][0])["id"] == finished["run_id"]
        await runtime.team.flush_reports()
        async with asyncio.timeout(5), runtime.bus.changed:
            await runtime.bus.changed.wait_for(
                lambda: any(
                    message["role"] == "user"
                    and message.get("source") == f"team:{target}"
                    and final_report in message["content"]
                    for message in runtime.store.messages(first["session_id"])
                )
            )
        assert main_requests
        assert runtime.team.get(target)["generation"] == 2
        assert runtime.store.pending_inputs(target) == 0
    finally:
        release.write_text("cleanup", encoding="utf-8")
        await runtime.close()


async def event_set(event: asyncio.Event) -> None:
    async with asyncio.timeout(5):
        await event.wait()


async def test_completed_before_pairing_and_slot_release_is_consumed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit_schedule = False
    early: list[ShellFinished] = []
    original_slot_seen = False

    def configure(state: RegisteredShellWait) -> None:
        runtime = state.runtime
        completed = runtime._tool_completed
        schedule = runtime.team._schedule_shell_continuations
        emit = runtime.bus.emit

        def scheduled() -> None:
            if permit_schedule:
                schedule()

        async def tool_completed(request: ToolCall, result: Any, session_id: str) -> None:
            await completed(request, result, session_id)
            if request.name != "shell_continue_on_finish" or not result.get("registered"):
                return
            done = asyncio.Event()

            def observe(fact: ShellFinished) -> None:
                early.append(fact)
                done.set()

            assert (
                runtime.shells.watch_finish(session_id, state.command["command_id"], observe)
                is None
            )
            state.release.write_text("finish before accepted pairing", encoding="utf-8")
            await event_set(done)
            assert not any(
                message.get("tool_call_id") == request.id
                for message in runtime.store.messages(session_id)
            )
            assert runtime.team.get(session_id)["generation"] == 1
            assert session_id in runtime.tasks and session_id in runtime.team._active

        async def emitted(
            kind: str, data: dict[str, Any], session_id: str | None = None
        ) -> dict[str, Any]:
            nonlocal original_slot_seen
            event = await emit(kind, data, session_id)
            if kind == "run.completed" and session_id == state.target:
                record = runtime.team._shell_continuations.get(state.target)
                if record is not None and record.bound_run_id is None:
                    original_slot_seen = True
                    assert record.finished is early[0]
                    assert state.target in runtime.tasks and state.target in runtime.team._active
                    assert record.observer is not None
                    record.observer(early[0])
                    record.observer(early[0])
                    assert runtime.team.get(state.target)["generation"] == 1
            return event

        monkeypatch.setattr(runtime.team, "_schedule_shell_continuations", scheduled)
        monkeypatch.setattr(runtime, "_tool_completed", tool_completed)
        monkeypatch.setattr(runtime.bus, "emit", emitted)

    async with registered_shell_wait(tmp_path, configure=configure) as state:
        assert original_slot_seen and len(early) == 1
        record = state.runtime.team._shell_continuations[state.target]
        assert record.paired and record.finished is early[0] and not record.reserved
        assert record.observer is not None
        callback = record.observer
        calls = len(state.worker_requests)
        permit_schedule = True
        callback(early[0])
        callback(early[0])
        state.runtime.team.start()
        finished = await wait_team_state(
            state.runtime,
            state.target,
            lambda agent: agent["status"] == "completed" and agent["generation"] == 2,
        )
        assert finished["completion_revision"] == state.original["completion_revision"] + 1
        assert len(state.worker_requests) == calls + 1
        assert state.counter.read_text(encoding="utf-8").splitlines() == ["once"]


@pytest.mark.parametrize("fault", ["not_persisted", "excerpt"])
async def test_only_a_complete_accepted_registration_pair_can_activate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    def configure(state: RegisteredShellWait) -> None:
        runtime = state.runtime
        put_many = runtime.store.put_many
        completed = runtime._tool_completed

        def persisted(items: list[tuple[str, dict[str, Any]]]) -> None:
            if any(
                kind == "message"
                and item.get("role") == "tool"
                and item.get("tool_call_id") == "wait-register"
                for kind, item in items
            ):
                raise ValueError("injected registration-pair persistence failure")
            put_many(items)

        async def tool_completed(request: ToolCall, result: Any, session_id: str) -> None:
            await completed(request, result, session_id)
            if request.name == "shell_continue_on_finish" and result.get("registered"):
                # Fault-inject growth after handler admission, then use the
                # smallest *valid* live budget to exercise the real serializer.
                result["notice"] *= 64
                runtime.config.update({"limits": {"max_output_chars": 1000}})

        if fault == "not_persisted":
            monkeypatch.setattr(runtime.store, "put_many", persisted)
        else:
            monkeypatch.setattr(runtime, "_tool_completed", tool_completed)

    async with registered_shell_wait(
        tmp_path,
        configure=configure,
        expected_status="failed" if fault == "not_persisted" else "completed",
        check_registration=False,
    ) as state:
        runtime = state.runtime
        assert state.target not in runtime.team._shell_continuations
        messages = [
            message
            for message in runtime.store.messages(state.target)
            if message.get("role") == "tool" and message.get("tool_call_id") == "wait-register"
        ]
        assert len(messages) == 1
        if fault == "not_persisted":
            assert messages[0].get("interrupted") is True
            assert json.loads(messages[0]["content"])["status"] == "unknown"
        else:
            assert messages[0]["content_provenance"] == "runtime_excerpt"
            assert json.loads(messages[0]["content"])["result_omitted"] is True
        count = len(state.worker_requests)
        state.release.write_text("no continuation from incomplete evidence", encoding="utf-8")
        runtime.team.start()
        assert runtime.team.get(state.target)["generation"] == 1
        assert (
            len(state.worker_requests) == count and runtime.store.pending_inputs(state.target) == 0
        )


@pytest.mark.parametrize(
    "boundary", ["waiting", "ready_lock", "run_started", "context", "capacity"]
)
async def test_independent_input_wins_before_claim_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    marker = f"真实新要求-{new_id()}"
    accepted_requests: list[list[Message]] = []

    async def resumed(state: RegisteredShellWait, messages: list[Message]) -> ModelTurn:
        assert any(marker in message.content for message in messages)
        accepted_requests.append(list(messages))
        return ModelTurn(content=f"已按真实新要求处理：{marker}")

    async with registered_shell_wait(tmp_path, resumed=resumed) as state:
        runtime = state.runtime
        entered, proceed = asyncio.Event(), asyncio.Event()
        lock_held = False
        if boundary == "ready_lock":
            await runtime.configuration_lock.acquire()
            lock_held = True
            schedule = runtime.team._schedule_shell_continuations

            def scheduled() -> None:
                schedule()
                record = state.runtime.team._shell_continuations.get(state.target)
                if record is not None and record.finished is not None:
                    entered.set()

            monkeypatch.setattr(runtime.team, "_schedule_shell_continuations", scheduled)
        elif boundary == "run_started":
            emit = runtime.bus.emit

            async def emitted(
                kind: str, data: dict[str, Any], session_id: str | None = None
            ) -> dict[str, Any]:
                event = await emit(kind, data, session_id)
                if kind == "run.started" and session_id == state.target:
                    entered.set()
                    await event_set(proceed)
                return event

            monkeypatch.setattr(runtime.bus, "emit", emitted)
        elif boundary == "context":

            async def context(session_id: str, messages: list[Message]) -> list[Message]:
                if session_id == state.target and not entered.is_set():
                    entered.set()
                    await event_set(proceed)
                return messages

            runtime.context_hooks.append(context)
        elif boundary == "capacity":

            class Capacity(asyncio.Semaphore):
                async def __aenter__(self) -> None:
                    entered.set()
                    await super().__aenter__()

            runtime.capacity = Capacity(0)
        try:
            if boundary != "waiting":
                state.release.write_text("finish while admission is gated", encoding="utf-8")
                await event_set(entered)
            before = len(state.worker_requests)
            receipt = await runtime.team.send(state.target, marker)
            assert runtime.store.pending_inputs(state.target) == 1
            assert state.target not in runtime.team._shell_continuations
            if lock_held:
                runtime.configuration_lock.release()
                lock_held = False
            proceed.set()
            if boundary == "capacity":
                runtime.capacity.release()
            finished = await wait_team_state(
                runtime,
                state.target,
                lambda agent: agent["status"] == "completed" and agent["generation"] == 2,
            )
            assert len(state.worker_requests) == before + 1 and len(accepted_requests) == 1
            delivered = runtime.store.get("message", receipt["receipt_id"])
            assert delivered is not None and marker in delivered["content"]
            assert delivered["delivery_status"] == "delivered"
            assert delivered["run_id"] == finished["run_id"]
            assert runtime.store.pending_inputs(state.target) == 0
            facts = [
                message
                for message in runtime.store.messages(state.target)
                if message.get("source") == "shell-continuation"
            ]
            assert len(facts) == int(boundary in {"run_started", "context", "capacity"})
            reports = runtime.store.db.execute(
                "SELECT data FROM objects WHERE kind='team_report' "
                "AND json_extract(data,'$.agent_id')=?",
                (state.target,),
            ).fetchall()
            assert len(reports) == 1 and marker in json.loads(reports[0][0])["content"]
            assert state.counter.read_text(encoding="utf-8").splitlines() == ["once"]
        finally:
            proceed.set()
            if lock_held:
                runtime.configuration_lock.release()
            if boundary == "capacity":
                runtime.capacity.release()


@pytest.mark.parametrize("failure", ["archive", "prepare"])
async def test_input_accepted_during_local_failure_cleanup_gets_ordinary_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    marker = f"清理期间真实补充-{new_id()}"
    calls: list[list[Message]] = []

    async def resumed(state: RegisteredShellWait, messages: list[Message]) -> ModelTurn:
        assert any(marker in message.content for message in messages)
        calls.append(list(messages))
        return ModelTurn(content=marker)

    async with registered_shell_wait(tmp_path, resumed=resumed) as state:
        runtime = state.runtime
        entered, proceed = asyncio.Event(), asyncio.Event()
        release = runtime.computer.release_execution
        archive = runtime.store._archive_input
        prepare = runtime._prepare_request
        failed = False

        def archived(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            if payload.get("source") == "shell-continuation":
                raise ValueError("injected archive failure")
            return archive(payload, **kwargs)

        async def prepared(session_id: str, **kwargs: Any) -> Any:
            nonlocal failed
            if session_id == state.target and not failed:
                failed = True
                raise ValueError("injected context failure")
            return await prepare(session_id, **kwargs)

        async def released(session_id: str, run_id: str) -> None:
            if session_id == state.target and not entered.is_set():
                entered.set()
                await event_set(proceed)
            await release(session_id, run_id)

        monkeypatch.setattr(runtime.computer, "release_execution", released)
        if failure == "archive":
            monkeypatch.setattr(runtime.store, "_archive_input", archived)
        else:
            monkeypatch.setattr(runtime, "_prepare_request", prepared)
        try:
            count = len(state.worker_requests)
            state.release.write_text("finish then fail locally", encoding="utf-8")
            await event_set(entered)
            if failure == "prepare":
                assert runtime.team.get(state.target)["status"] == "running"
            assert len(state.worker_requests) == count
            receipt = await runtime.team.send(state.target, marker)
            assert runtime.store.pending_inputs(state.target) == 1
            if failure == "prepare":
                assert runtime.team.get(state.target)["status"] == "running"
            proceed.set()
            finished = await wait_team_state(
                runtime,
                state.target,
                lambda agent: agent["status"] == "completed" and agent["generation"] == 3,
            )
            assert len(calls) == 1 and len(state.worker_requests) == count + 1
            delivered = runtime.store.get("message", receipt["receipt_id"])
            assert delivered is not None and delivered["delivery_status"] == "delivered"
            assert delivered["run_id"] == finished["run_id"] and marker in delivered["content"]
            assert runtime.store.pending_inputs(state.target) == 0
            assert state.counter.read_text(encoding="utf-8").splitlines() == ["once"]
            assert not any(
                agent["id"] == state.target and agent["status"] == "queued"
                for agent in runtime.team.list()
            )
        finally:
            proceed.set()


async def test_new_input_can_register_its_own_real_command_after_displacing_the_old_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = f"改为等待第二条真实命令-{new_id()}"
    second_release, second_counter = tmp_path / "second-release", tmp_path / "second-effects"
    second_script = tmp_path / "second-command.py"
    second_script.write_text(
        "from pathlib import Path\nimport time\n\n"
        "def main() -> None:\n"
        f"    release: Path = Path({str(second_release)!r})\n"
        f"    counter: Path = Path({str(second_counter)!r})\n"
        "    with counter.open('a', encoding='utf-8') as effects:\n"
        "        effects.write('second-once' + chr(10))\n"
        "    print('SECOND_COMMAND_READY', flush=True)\n"
        "    deadline: float = time.monotonic() + 15\n"
        "    while not release.exists():\n"
        "        if time.monotonic() >= deadline:\n"
        "            raise SystemExit(9)\n"
        "        time.sleep(0.01)\n"
        "    print('SECOND_COMMAND_FINISHED', flush=True)\n"
        "    raise SystemExit(3)\n\n"
        "if __name__ == '__main__':\n    main()\n",
        encoding="utf-8",
    )
    second: dict[str, Any] = {}
    cursor: dict[str, int] = {"output": 0, "stderr": 0}
    generations: list[int] = []
    initial_flavor = ""

    def second_command() -> ShellScript:
        nonlocal second, cursor
        second = yield call(
            "replacement-command",
            "shell_run",
            command=shell_python_command(initial_flavor, second_script),
            wait_seconds=0,
            timeout=20,
        )
        page = second
        for index in range(4):
            if "SECOND_COMMAND_READY" in page["output"]:
                break
            page = yield call(
                f"replacement-ready-{index}",
                "shell_read",
                command_id=second["command_id"],
                cursor=page["next_cursor"],
                wait_seconds=1,
            )
        assert page["state"] == "running" and "SECOND_COMMAND_READY" in page["output"]
        cursor = page["next_cursor"].copy()
        returned = yield call(
            "replacement-register", "shell_continue_on_finish", command_id=second["command_id"]
        )
        raise AssertionError(f"第二条命令的完整登记应结束本代：{returned}")

    def check_second_command() -> ShellScript:
        page = yield call(
            "replacement-finished",
            "shell_read",
            command_id=second["command_id"],
            cursor=cursor,
        )
        assert page["state"] == "completed" and page["exit_code"] == 3
        assert page["cursor"] == cursor and "SECOND_COMMAND_FINISHED" in page["output"]
        return "第二条真实命令已按新要求核查。"

    replacement, checked = ShellDialogue(second_command()), ShellDialogue(check_second_command())

    async def resumed(state: RegisteredShellWait, messages: list[Message]) -> ModelTurn:
        generation = state.runtime.team.get(state.target)["generation"]
        generations.append(generation)
        assert any(marker in message.content for message in messages)
        return (replacement if generation == 2 else checked).advance(messages)

    async with registered_shell_wait(tmp_path, resumed=resumed) as state:
        runtime = state.runtime
        initial_flavor = state.command["flavor"]
        entered, proceed = asyncio.Event(), asyncio.Event()
        emit = runtime.bus.emit

        async def emitted(
            kind: str, data: dict[str, Any], session_id: str | None = None
        ) -> dict[str, Any]:
            event = await emit(kind, data, session_id)
            if kind == "run.started" and session_id == state.target and not entered.is_set():
                entered.set()
                await event_set(proceed)
            return event

        monkeypatch.setattr(runtime.bus, "emit", emitted)
        try:
            state.release.write_text("finish original command", encoding="utf-8")
            await event_set(entered)
            receipt = await runtime.team.send(state.target, marker)
            assert runtime.store.pending_inputs(state.target) == 1
            proceed.set()
            waiting = await wait_team_state(
                runtime,
                state.target,
                lambda agent: (
                    agent["status"] == "completed"
                    and agent["generation"] == 2
                    and state.target not in runtime.tasks
                    and state.target not in runtime.team._active
                ),
            )
            registered = json.loads(
                paired_tool(runtime, state.target, "replacement-register")["content"]
            )
            assert registered["registered"] is True
            record = runtime.team._shell_continuations[state.target]
            assert record.run_id == waiting["run_id"] and record.generation == 2
            assert record.paired and record.bound_run_id is None
            assert record.evidence["command_id"] == second["command_id"]
            assert (
                runtime.store.get("message", receipt["receipt_id"])["delivery_status"]
                == "delivered"
            )
            assert (
                runtime.store.db.execute(
                    "SELECT count(*) FROM objects WHERE kind='team_report' "
                    "AND json_extract(data,'$.agent_id')=?",
                    (state.target,),
                ).fetchone()[0]
                == 0
            )
            second_release.write_text("finish only the replacement command", encoding="utf-8")
            finished = await wait_team_state(
                runtime,
                state.target,
                lambda agent: agent["status"] == "completed" and agent["generation"] == 3,
            )
            assert finished["result"] == "第二条真实命令已按新要求核查。"
            assert generations.count(3) == 2 and set(generations) == {2, 3}
            assert state.counter.read_text(encoding="utf-8").splitlines() == ["once"]
            assert second_counter.read_text(encoding="utf-8").splitlines() == ["second-once"]
            assert state.target not in runtime.team._shell_continuations
        finally:
            proceed.set()
            second_release.write_text("cleanup", encoding="utf-8")


@pytest.mark.parametrize(
    "revocation", ["mode_roundtrip", "pause", "task_revision", "disabled_tool", "stop", "lost"]
)
async def test_revoked_or_unknown_wait_does_not_call_a_model_or_replay(
    tmp_path: Path, revocation: str
) -> None:
    async with registered_shell_wait(tmp_path) as state:
        runtime = state.runtime
        count = len(state.worker_requests)
        if revocation == "mode_roundtrip":
            session = runtime.store.get("session", state.target)
            assert session is not None
            for old, new in (("yolo", "ask"), ("ask", "yolo")):
                runtime.store.put("session", {**session, "mode": new})
                runtime.shell_mode_changed(state.target, old, new)
        elif revocation == "pause":
            await runtime.submit_task_control(
                state.task_id, OwnerControlRequest("pause", 0, "pause-shell-wait")
            )
        elif revocation == "task_revision":
            assert runtime.task_control is not None
            task = runtime.task_control.commitments.get(state.task_id)
            await runtime.task_control.commitments.save(
                task["title"], identifier=state.task_id, next_step="先重新核查新的任务版本"
            )
        elif revocation == "disabled_tool":
            runtime.config.update({"disabled_tools": ["shell_continue_on_finish"]})
        elif revocation == "stop":
            await runtime.team.stop(state.target)
        else:
            process = runtime.shells._sessions[state.target].process
            assert process is not None
            process.kill()
            item = runtime.shells._commands[state.command["command_id"]]
            await event_set(item.done)
            assert item.state == "unknown" and item.exit_code is None
        state.release.write_text("finish after revocation", encoding="utf-8")
        async with asyncio.timeout(5), runtime.bus.changed:
            await runtime.bus.changed.wait_for(
                lambda: (
                    runtime.store.get(
                        "notification",
                        (
                            f"shell-continuation:{state.target}:{state.command['os_epoch']}:"
                            f"{state.command['command_id']}"
                        ),
                    )
                    is not None
                )
            )
        assert len(state.worker_requests) == count
        assert runtime.team.get(state.target)["generation"] == 1
        assert runtime.store.pending_inputs(state.target) == 0
        assert state.target not in runtime.team._shell_continuations
        assert state.counter.read_text(encoding="utf-8").splitlines() == ["once"]
        assert not any(
            message.get("source") == "shell-continuation"
            for message in runtime.store.messages(state.target)
        )
