"""Owner-to-Team journeys through the actual shell tool permission boundary."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator
from test_runtime import FakeClient, make_runtime
from test_task_briefs import attach_services
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import new_id
from muli.task_control import OwnerControlRequest


async def test_owner_delegation_offers_command_shell_to_the_real_worker(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "memory": {"auto_extract": False},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
        }
    )
    await runtime.reconfigure()
    command_arguments: dict[str, Any] = {
        "command": "echo SHELL_SESSION_READY",
        "cwd": str(tmp_path),
    }
    shell_visible = False
    accepts_command = False
    main_round = worker_round = 0
    worker_result: dict[str, Any] = {}

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_round
            main_round += 1
            if main_round == 1:
                names = {tool["function"]["name"] for tool in tools}
                assert {"spawn_agent", "wait_agents"} <= names
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "spawn-shell-worker",
                            "spawn_agent",
                            task_name="shell-worker",
                            prompt="仅在临时工作目录运行一个无副作用的短命令，并依据实际回执报告。",
                        ),
                        call(
                            "wait-shell-worker", "wait_agents", targets=["shell-worker"], timeout=2
                        ),
                    ]
                )
            else:
                waited = tool_result(messages, "wait-shell-worker")
                assert waited["agents"][0]["status"] == "completed"
                turn = ModelTurn(content="已收到队友的实际命令回执，未推断命令成功。")
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_round, shell_visible, accepts_command, worker_result
            worker_round += 1
            if worker_round == 1:
                schemas = {
                    tool["function"]["name"]: tool["function"]["parameters"] for tool in tools
                }
                shell_visible = "shell_run" in schemas
                assert shell_visible, "前置：真实执行队友必须已拥有原 shell_run 工具"
                validator = Draft202012Validator(schemas["shell_run"])
                assert validator.is_valid(
                    {"argv": [sys.executable, "-c", "print('ARGV_READY')"], "cwd": str(tmp_path)}
                ), "前置：原 argv 分支应继续有效"
                accepts_command = validator.is_valid(command_arguments)
                turn = ModelTurn(
                    tool_calls=[call("worker-command", "shell_run", **command_arguments)]
                )
            else:
                assert worker_round == 2
                worker_result = tool_result(messages, "worker-command")
                assert worker_result.get("error") != "工具未启用"
                turn = ModelTurn(content="短命令已取得工具回执，具体执行状态以回执为准。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Worker([], seen) if config.model == "worker" else Main([], seen)

    runtime.client_factory = factory
    try:
        receipt = await runtime.submit(
            "请委派一位队友检查临时目录里的 shell 命令入口。", mode="yolo"
        )
        async with asyncio.timeout(4):
            reply = await runtime.wait(receipt["run_id"])
        assert reply == "已收到队友的实际命令回执，未推断命令成功。"
        main = runtime.store.get("run", receipt["run_id"])
        assert main is not None and main["status"] == "completed"
        owner = next(
            message
            for message in runtime.store.messages(receipt["session_id"])
            if message["role"] == "user" and message.get("source") == "web"
        )
        assert owner["principal"] == "owner"
        agent = runtime.team.get("shell-worker")
        assert agent["status"] == "completed" and agent["generation"] == 1
        assert agent["mode"] == "yolo" and agent["parent_id"] == receipt["session_id"]
        execution = runtime.store.get("run", agent["run_id"])
        assert execution is not None and execution["status"] == "completed"
        commands = [
            message
            for message in runtime.store.messages(agent["session_id"])
            if message.get("tool_call_id") == "worker-command"
        ]
        assert len(commands) == 1 and commands[0]["role"] == "tool"
        assert json.loads(commands[0]["content"]) == worker_result
        assert worker_round == 2 and shell_visible
        assert accepts_command, {
            "shell_run_visible": shell_visible,
            "worker_run_completed": execution["status"] == "completed",
            "command_returned_error": "error" in worker_result,
        }
        assert "error" not in worker_result
        assert worker_result["state"] in {"running", "completed"}
        if worker_result["state"] == "completed":
            assert type(worker_result["exit_code"]) is int and worker_result["exit_code"] == 0
    finally:
        await runtime.close()


ShellScript = Generator[ToolCall, dict[str, Any], str]


@dataclass
class ShellDialogue:
    script: ShellScript
    pending: str | None = None

    def advance(self, messages: list[Message]) -> ModelTurn:
        try:
            request = (
                next(self.script)
                if self.pending is None
                else self.script.send(tool_result(messages, self.pending))
            )
        except StopIteration as finished:
            assert isinstance(finished.value, str)
            return ModelTurn(content=finished.value)
        self.pending = request.id
        return ModelTurn(tool_calls=[request])


class ShellClient(FakeClient):
    def __init__(self, dialogue: ShellDialogue) -> None:
        super().__init__([], [])
        self.dialogue = dialogue

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        names = {tool["function"]["name"] for tool in tools}
        turn = self.dialogue.advance(messages)
        for request in turn.tool_calls:
            assert request.name in names, f"真实模型目录缺少 {request.name}"
        yield StreamEvent("done", turn=turn)


def completed_shell(request: ToolCall) -> Generator[ToolCall, dict[str, Any], dict[str, Any]]:
    result = yield request
    polled = False
    for attempt in range(3):
        assert "error" not in result, result
        if result["state"] != "running":
            break
        polled = True
        result = yield call(
            f"{request.id}-read-{attempt}",
            "shell_read",
            command_id=result["command_id"],
            cursor=result["next_cursor"],
            wait_seconds=1,
        )
    assert "error" not in result and result["state"] == "completed", result
    if polled:
        result = yield call(
            f"{request.id}-complete-output", "shell_read", command_id=result["command_id"]
        )
    assert "error" not in result and result["state"] == "completed", result
    assert type(result["exit_code"]) is int and result["exit_code"] == 0, result
    assert result["output_scope"] == "shell"
    return result


def shell_state_commands(flavor: str, directory: Path, variable: str) -> tuple[str, str, str]:
    if flavor in {"sh", "bash", "zsh"}:
        setup = (
            f"cd {shlex.quote(str(directory))} && export {variable}=OWNED_CONTEXT; "
            "muli_test_function() { printf '%s\\n' FUNCTION_CONTEXT; }"
        )
        inspect = f"printf '%s\\n' \"${variable}\"; pwd; muli_test_function"
        isolated = f"printf '%s\\n' \"${{{variable}-UNSET_CONTEXT}}\""
    elif flavor == "fish":
        path = "'" + str(directory).replace("\\", "\\\\").replace("'", "\\'") + "'"
        setup = (
            f"cd {path}; set -gx {variable} OWNED_CONTEXT; "
            "function muli_test_function; printf '%s\\n' FUNCTION_CONTEXT; end"
        )
        inspect = f"printf '%s\\n' ${variable}; pwd; muli_test_function"
        isolated = (
            f"if set -q {variable}; printf '%s\\n' ${variable}; else; echo UNSET_CONTEXT; end"
        )
    elif flavor == "pwsh":
        path = "'" + str(directory).replace("'", "''") + "'"
        setup = (
            f"Set-Location -LiteralPath {path}; $env:{variable}='OWNED_CONTEXT'; "
            "function global:muli_test_function { 'FUNCTION_CONTEXT' }"
        )
        inspect = f"$env:{variable}; (Get-Location).Path; muli_test_function"
        isolated = f"if (Test-Path Env:{variable}) {{ $env:{variable} }} else {{ 'UNSET_CONTEXT' }}"
    else:
        assert flavor == "cmd", f"未约定的原生 shell 类型：{flavor}"
        setup = f'cd /d "{directory}" && set "{variable}=OWNED_CONTEXT"'
        inspect = f"echo %{variable}% & cd"
        isolated = f"if defined {variable} (echo %{variable}%) else (echo UNSET_CONTEXT)"
    return setup, inspect, isolated


async def shell_runtime(tmp_path: Path) -> Runtime:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "memory": {"auto_extract": False},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
        }
    )
    await runtime.reconfigure()
    return runtime


async def test_shell_state_survives_real_agent_generation_and_isolates_other_sessions(
    tmp_path: Path,
) -> None:
    runtime = await shell_runtime(tmp_path)
    directory = tmp_path / "persistent cwd"
    directory.mkdir()
    variable = "MULI_SESSION_TEST_" + new_id().upper()
    first: dict[str, Any] = {}
    resumed: dict[str, Any] = {}
    other: dict[str, Any] = {}
    main_shell: dict[str, Any] = {}
    target = ""

    def initial_worker() -> ShellScript:
        nonlocal first
        first = yield from completed_shell(
            call("worker-boot", "shell_run", command="echo WORKER_BOOT", cwd=str(tmp_path))
        )
        setup, inspect, _ = shell_state_commands(first["flavor"], directory, variable)
        configured = yield from completed_shell(call("worker-state", "shell_run", command=setup))
        assert configured["shell_pid"] == first["shell_pid"]
        checked = yield from completed_shell(call("worker-inspect", "shell_run", command=inspect))
        assert "OWNED_CONTEXT" in checked["output"]
        if first["flavor"] != "cmd":
            assert "FUNCTION_CONTEXT" in checked["output"]
        assert Path(checked["cwd"]).resolve() == directory.resolve()
        return "本代已保存 shell 环境、目录与原生支持的函数。"

    def resumed_worker() -> ShellScript:
        nonlocal resumed
        _, inspect, _ = shell_state_commands(first["flavor"], directory, variable)
        resumed = yield from completed_shell(call("worker-g2", "shell_run", command=inspect))
        assert resumed["shell_pid"] == first["shell_pid"]
        assert resumed["os_epoch"] == first["os_epoch"]
        assert "OWNED_CONTEXT" in resumed["output"]
        if resumed["flavor"] != "cmd":
            assert "FUNCTION_CONTEXT" in resumed["output"]
        assert Path(resumed["cwd"]).resolve() == directory.resolve()
        return "同一队友新一代仍使用原 shell 状态。"

    def isolated_worker() -> ShellScript:
        nonlocal other
        boot = yield from completed_shell(
            call("other-boot", "shell_run", command="echo OTHER_BOOT", cwd=str(tmp_path))
        )
        _, _, inspect = shell_state_commands(boot["flavor"], directory, variable)
        other = yield from completed_shell(call("other-state", "shell_run", command=inspect))
        assert "UNSET_CONTEXT" in other["output"] and "OWNED_CONTEXT" not in other["output"]
        assert other["shell_pid"] != first["shell_pid"]
        assert other["os_epoch"] != first["os_epoch"]
        return "另一队友没有取得原队友的 shell 环境。"

    worker_dialogue = ShellDialogue(initial_worker())

    def main_script() -> ShellScript:
        nonlocal worker_dialogue, target, main_shell
        spawned = yield call(
            "spawn-persistent", "spawn_agent", task_name="persistent", prompt="核查本机 shell 状态"
        )
        target = spawned["id"]
        waited = yield call("wait-g1", "wait_agents", targets=[target], timeout=2)
        assert waited["agents"][0]["status"] == "completed"
        assert runtime.team.get(target)["generation"] == 1
        worker_dialogue = ShellDialogue(resumed_worker())
        yield call("resume-persistent", "resume_agent", target=target)
        waited = yield call("wait-g2", "wait_agents", targets=[target], timeout=2)
        assert waited["agents"][0]["status"] == "completed"
        assert runtime.team.get(target)["generation"] == 2
        worker_dialogue = ShellDialogue(isolated_worker())
        spawned = yield call(
            "spawn-isolated", "spawn_agent", task_name="isolated", prompt="检查隔离的 shell"
        )
        waited = yield call("wait-isolated", "wait_agents", targets=[spawned["id"]], timeout=2)
        assert waited["agents"][0]["status"] == "completed"
        main_shell = yield from completed_shell(
            call("main-shell", "shell_run", command="echo MAIN_SHELL", cwd=str(tmp_path))
        )
        assert "MAIN_SHELL" in main_shell["output"]
        assert main_shell["shell_pid"] not in {first["shell_pid"], other["shell_pid"]}
        return "已核对同队友跨代连续、另一队友隔离和主助手自己的 shell。"

    main_dialogue = ShellDialogue(main_script())

    def factory(config: ModelConfig) -> FakeClient:
        return ShellClient(worker_dialogue if config.model == "worker" else main_dialogue)

    runtime.client_factory = factory
    try:
        receipt = await runtime.submit("请核对临时目录中各会话 shell 的连续性和隔离。", mode="yolo")
        async with asyncio.timeout(4):
            result = await runtime.wait(receipt["run_id"])
        assert result == "已核对同队友跨代连续、另一队友隔离和主助手自己的 shell。"
        assert first["session_id"] == resumed["session_id"] == target
        assert other["session_id"] != target
        assert main_shell["session_id"] == receipt["session_id"]
        assert len(runtime.team.list()) == 2
    finally:
        await runtime.close()


def shell_python_command(flavor: str, script: Path) -> str:
    if flavor in {"sh", "bash", "zsh"}:
        return shlex.join([sys.executable, str(script)])
    if flavor == "fish":
        return " ".join(
            "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
            for value in (sys.executable, str(script))
        )
    if flavor == "pwsh":
        return "& " + " ".join(
            "'" + value.replace("'", "''") + "'" for value in (sys.executable, str(script))
        )
    assert flavor == "cmd"
    return f'"{sys.executable}" "{script}"'


async def test_running_shell_command_releases_main_turn_and_read_observes_real_exit(
    tmp_path: Path,
) -> None:
    runtime = await shell_runtime(tmp_path)
    release = tmp_path / "release-command"
    script = tmp_path / "bounded-command.py"
    script.write_text(
        "from pathlib import Path\n"
        "import time\n"
        f"release = Path({str(release)!r})\n"
        "deadline = time.monotonic() + 2\n"
        "while not release.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "if not release.exists():\n"
        "    raise SystemExit(7)\n"
        "print('RELEASED_COMMAND')\n",
        encoding="utf-8",
    )
    command: dict[str, Any] = {}
    final: dict[str, Any] = {}

    def launch_script() -> ShellScript:
        nonlocal command
        initial = yield call("status-without-start", "shell_read")
        assert "error" not in initial and initial["state"] == "not_started"
        assert initial["shell_pid"] is None
        boot = yield from completed_shell(
            call("main-boot", "shell_run", command="echo MAIN_BOOT", cwd=str(tmp_path))
        )
        command = yield call(
            "long-command",
            "shell_run",
            command=shell_python_command(boot["flavor"], script),
            wait_seconds=0,
            timeout=3,
        )
        assert "error" not in command and command["state"] == "running"
        assert command["exit_code"] is None and not release.exists()
        return "命令仍在运行，主助手现在可以继续回答。"

    def finish_script() -> ShellScript:
        nonlocal final
        final = yield from completed_shell(
            call(
                "read-long-command", "shell_read", command_id=command["command_id"], wait_seconds=1
            )
        )
        assert "RELEASED_COMMAND" in final["output"]
        assert final["command_id"] == command["command_id"]
        assert final["shell_pid"] == command["shell_pid"]
        assert final["os_epoch"] == command["os_epoch"]
        return "已读取真实退出状态，没有重做命令。"

    dialogue = ShellDialogue(launch_script())
    runtime.client_factory = lambda _config: ShellClient(dialogue)
    try:
        first = await runtime.submit("启动受控短命令后先回答，不等待它结束。", mode="yolo")
        async with asyncio.timeout(4):
            assert await runtime.wait(first["run_id"]) == "命令仍在运行，主助手现在可以继续回答。"
        assert runtime.store.get("run", first["run_id"])["status"] == "completed"
        assert not release.exists()
        release.write_text("continue", encoding="utf-8")
        dialogue = ShellDialogue(finish_script())
        second = await runtime.submit("现在只读取原命令状态，不再执行。", mode="yolo")
        async with asyncio.timeout(4):
            assert await runtime.wait(second["run_id"]) == "已读取真实退出状态，没有重做命令。"
        assert final["exit_code"] == 0 and final["state"] == "completed"
    finally:
        release.write_text("cleanup", encoding="utf-8")
        await runtime.close()


@pytest.mark.parametrize("decision", ["approve", "reject", "new_owner"])
async def test_shell_command_waits_for_real_owner_approval_and_revokes_unstarted_work(
    tmp_path: Path, decision: str
) -> None:
    runtime = await shell_runtime(tmp_path)
    marker = tmp_path / "approved-command.txt"
    rounds = 0
    command_result: dict[str, Any] = {}
    steering = "不要执行刚才的命令，只说明它没有开始。"
    final = "已按当前真实审批结果处理命令。"

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                assert "shell_run" in {tool["function"]["name"] for tool in tools}
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "approval-command",
                            "shell_run",
                            command=f"echo APPROVED_COMMAND > {marker.name}",
                            cwd=str(tmp_path),
                        )
                    ]
                )
            else:
                assert rounds == 2
                command_result.update(tool_result(messages, "approval-command"))
                if decision == "new_owner":
                    assert command_result["status"] == "not_executed"
                    assert any(
                        message.role == "user" and message.content == steering
                        for message in messages
                    )
                elif decision == "reject":
                    assert "拒绝" in command_result["error"]
                else:
                    assert "error" not in command_result
                    assert command_result["state"] in {"running", "completed"}
                turn = ModelTurn(content=final)
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _config: Main([], [])
    try:
        receipt = await runtime.submit("在临时目录写一份明确获批后的标记。", mode="ask")
        main_id = receipt["session_id"]
        assert receipt["message"]["principal"] == "owner"
        assert receipt["message"]["source"] == "web"
        await eventually(runtime, lambda: bool(runtime.live_approvals(main_id)))
        approvals = runtime.live_approvals(main_id)
        assert len(approvals) == 1
        approval = approvals[0]
        assert approval["run_id"] == receipt["run_id"]
        assert approval["tool"] == "shell_run" and approval["risk"] == "destructive"
        future = runtime.permissions.pending[approval["id"]]
        assert not future.done() and not marker.exists()
        assert runtime.shells.live_sessions() == ()
        assert runtime.shells.status(main_id)["state"] == "not_started"
        assert not any(
            event["type"] == "tool.started" and event["data"].get("call_id") == "approval-command"
            for event in runtime.store.events(session_id=main_id)
        )
        if decision == "new_owner":
            newer = await runtime.submit(steering, event_id="shell-approval-steering")
            assert newer["run_id"] == receipt["run_id"]
            assert newer["message"]["principal"] == "owner"
        else:
            await runtime.permissions.resolve(approval["id"], decision)
        async with asyncio.timeout(4):
            assert await runtime.wait(receipt["run_id"]) == final
        assert rounds == 2 and runtime.live_approvals(main_id) == []
        persisted = runtime.store.get("approval", approval["id"])
        assert persisted is not None
        assert (
            persisted["status"]
            == {"approve": "approved", "reject": "rejected", "new_owner": "cancelled"}[decision]
        )
        assert future.done()
        started = [
            event
            for event in runtime.store.events(session_id=main_id)
            if event["type"] == "tool.started"
            and event["data"].get("call_id") == "approval-command"
        ]
        if decision == "approve":
            assert len(started) == 1 and marker.exists()
            assert marker.read_text(encoding="utf-8").strip() == "APPROVED_COMMAND"
            assert command_result["session_id"] == main_id
            assert command_result["shell_pid"] is not None
        else:
            assert started == [] and not marker.exists()
            assert runtime.shells.live_sessions() == ()
            assert runtime.shells.status(main_id)["state"] == "not_started"
    finally:
        await runtime.close()


@pytest.mark.parametrize("action", ["owner_stop", "task_pause"])
async def test_owner_stop_and_task_pause_close_shell_after_worker_run_has_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    runtime = await shell_runtime(tmp_path)
    attach_services(runtime, tmp_path)
    release = tmp_path / "allow-command-effect"
    effect = tmp_path / "late-command-effect.txt"
    script = tmp_path / "controlled-shell-command.py"
    script.write_text(
        "from pathlib import Path\n"
        "import time\n"
        f"release = Path({str(release)!r})\n"
        "print('COMMAND_ACTIVE', flush=True)\n"
        "deadline = time.monotonic() + 2\n"
        "while not release.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "if not release.exists():\n"
        "    raise SystemExit(7)\n"
        f"Path({str(effect)!r}).write_text('must not happen', encoding='utf-8')\n",
        encoding="utf-8",
    )
    target = task_id = ""
    running: dict[str, Any] = {}
    settled = asyncio.Event()
    original_cleanup = runtime.shells._cleanup

    async def observe_cleanup(shell: Any) -> None:
        await original_cleanup(shell)
        if shell.session_id == target:
            settled.set()

    monkeypatch.setattr(runtime.shells, "_cleanup", observe_cleanup)

    def worker_script() -> ShellScript:
        nonlocal running
        boot = yield from completed_shell(
            call("controlled-boot", "shell_run", command="echo CONTROLLED_BOOT", cwd=str(tmp_path))
        )
        running = yield call(
            "controlled-command",
            "shell_run",
            command=shell_python_command(boot["flavor"], script),
            wait_seconds=1,
            timeout=3,
        )
        assert "error" not in running and running["state"] == "running"
        assert running["exit_code"] is None and "COMMAND_ACTIVE" in running["output"]
        assert not effect.exists()
        return "命令仍在运行，当前队友回合已经返回。"

    def main_script() -> ShellScript:
        nonlocal target, task_id
        saved = yield call(
            "save-controlled-shell-task",
            "commitment_save",
            title="可停止的 shell 任务",
            brief={"goal": "核查临时目录中的受控命令", "constraints": ["未经允许不得写最终标记"]},
        )
        task_id = saved["id"]
        spawned = yield call(
            "spawn-controlled-shell",
            "spawn_agent",
            task_name="controlled-shell",
            prompt="运行受控临时脚本后先报告状态，不释放它的后续写入门。",
            commitment_id=task_id,
        )
        target = spawned["id"]
        waited = yield call("wait-controlled-shell", "wait_agents", targets=[target], timeout=2)
        assert waited["agents"][0]["status"] == "completed"
        return "队友回合已结束，长命令仍等待主人决定。"

    worker_dialogue = ShellDialogue(worker_script())
    main_dialogue = ShellDialogue(main_script())

    def factory(config: ModelConfig) -> FakeClient:
        return ShellClient(worker_dialogue if config.model == "worker" else main_dialogue)

    runtime.client_factory = factory
    try:
        started = await runtime.submit("建立可随时停止的受控 shell 任务。", mode="yolo")
        async with asyncio.timeout(4):
            assert await runtime.wait(started["run_id"]) == "队友回合已结束，长命令仍等待主人决定。"
        agent = runtime.team.get(target)
        assert agent["status"] == "completed" and agent["generation"] == 1
        assert agent["commitment_id"] == task_id and agent["parent_id"] == started["session_id"]
        assert runtime.store.get("run", agent["run_id"])["status"] == "completed"
        assert runtime.shells.status(target)["state"] == "running"
        assert target not in runtime.tasks and not settled.is_set()
        if action == "owner_stop":
            await runtime.stop(target)
        else:
            result = await runtime.submit_task_control(
                task_id, OwnerControlRequest("pause", 0, "pause-returned-shell")
            )
            assert result.created
            assert runtime.task_control_current(task_id)["control"]["paused"] is True
        await asyncio.wait_for(settled.wait(), 3)
        stopped = runtime.shells.status(target)
        assert stopped["command_id"] == running["command_id"]
        assert stopped["state"] == "stopped" and stopped["shell_state"] == "closed"
        assert stopped["exit_code"] is None
        assert target not in runtime.shells.live_sessions()
        release.write_text("too late", encoding="utf-8")
        assert not effect.exists()
    finally:
        await runtime.close()


async def test_settings_preserve_default_shell_and_unrelated_idle_shell_state(
    tmp_path: Path,
) -> None:
    runtime = await shell_runtime(tmp_path)
    boot: dict[str, Any] = {}

    def initial_script() -> ShellScript:
        nonlocal boot
        boot = yield from completed_shell(
            call(
                "settings-shell-boot", "shell_run", command="echo SETTINGS_SHELL", cwd=str(tmp_path)
            )
        )
        return "当前 shell 状态已记录，可以核验配置变更。"

    def followup_script() -> ShellScript:
        status = yield call("settings-shell-status", "shell_read")
        assert "error" not in status
        assert status["shell_state"] == "idle" and status["state"] == "completed"
        assert status["shell_pid"] == boot["shell_pid"]
        assert status["os_epoch"] == boot["os_epoch"]
        continued = yield from completed_shell(
            call("settings-shell-continued", "shell_run", command="echo STILL_OWNED")
        )
        assert continued["shell_pid"] == boot["shell_pid"]
        assert continued["os_epoch"] == boot["os_epoch"]
        assert "STILL_OWNED" in continued["output"]
        return "模型配置已更新，原空闲 shell 仍可继续使用。"

    dialogue = ShellDialogue(initial_script())
    runtime.client_factory = lambda _config: ShellClient(dialogue)
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            started = await runtime.submit("记录临时 shell 状态，再检查配置换代。", mode="yolo")
            async with asyncio.timeout(4):
                assert (
                    await runtime.wait(started["run_id"])
                    == "当前 shell 状态已记录，可以核验配置变更。"
                )
            assert started["session_id"] not in runtime.tasks
            original_shells = runtime.shells
            original_bytes = runtime.config.path.read_bytes()
            assert "shell" not in json.loads(original_bytes)
            assert runtime.shells.executable == ""
            changed = await web.patch(
                "/api/settings", json={"models": {"main": {"model": "main-updated"}}}
            )
            assert changed.status_code == 200
            assert runtime.config.settings.models.main.model == "main-updated"
            same_manager = runtime.shells is original_shells
            assert same_manager
            assert runtime.config.path.read_bytes() != original_bytes
            assert "shell" not in json.loads(runtime.config.path.read_bytes())
            assert "shell" not in runtime.config.settings.model_dump()
            assert runtime.shells.executable == ""
            dialogue = ShellDialogue(followup_script())
            continued = await runtime.submit("只核验原 shell，不能重建新的 OS 会话。", mode="yolo")
            async with asyncio.timeout(4):
                assert (
                    await runtime.wait(continued["run_id"])
                    == "模型配置已更新，原空闲 shell 仍可继续使用。"
                )
    finally:
        await runtime.close()
