"""Focused local-notice lifecycle checks; the original journey stays frozen."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import pytest
from test_attention import attention_inputs
from test_runtime import FakeClient
from test_shell_finish_notification_runtime import finish_notices
from test_shell_finish_notification_runtime import (
    offline_and_no_desktop as offline_and_no_desktop,
)
from test_shell_session_runtime import (
    ShellClient,
    ShellDialogue,
    ShellScript,
    completed_shell,
    shell_python_command,
    shell_runtime,
)
from test_system_events_runtime import consume_until_idle, inbox_for
from test_teammate_attachment_runtime import call

from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.shell_sessions import ShellFinished

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="真实临时命令生命周期限 POSIX；原生 Windows 另行验收"
)


async def owner_running_command(
    runtime: Runtime,
    directory: Path,
    *,
    notify_on_finish: bool = True,
    timeout: float = 12,
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "notice-command.py"
    script.write_text(
        "from pathlib import Path\n"
        "import time\n"
        "\n"
        "def main() -> int:\n"
        f"    directory: Path = Path({str(directory)!r})\n"
        "    with (directory / 'effects').open('a', encoding='utf-8') as effects:\n"
        "        effects.write('ran' + chr(10))\n"
        "    (directory / 'ready').write_text('ready', encoding='utf-8')\n"
        "    print('NOTICE_READY', flush=True)\n"
        "    deadline: float = time.monotonic() + 10\n"
        "    while not (directory / 'release').exists():\n"
        "        if time.monotonic() >= deadline:\n"
        "            return 19\n"
        "        time.sleep(0.01)\n"
        "    print('PRIVATE_RETAINED_TAIL', flush=True)\n"
        "    return 7\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    receipt: dict[str, Any] = {}

    def dialogue() -> ShellScript:
        nonlocal receipt
        boot = yield from completed_shell(
            call("lifecycle-boot", "shell_run", command="echo NOTICE_BOOT", cwd=str(directory))
        )
        receipt = yield call(
            "lifecycle-launch",
            "shell_run",
            command=shell_python_command(boot["flavor"], script),
            wait_seconds=0,
            timeout=timeout,
            notify_on_finish=notify_on_finish,
        )
        return "本轮先结束；以原命令的真实状态为准。"

    plan = ShellDialogue(dialogue())
    runtime.client_factory = lambda _config: ShellClient(plan)
    submitted = await runtime.submit("在临时目录执行，按本次明确选择留本地通知。", mode="yolo")
    async with asyncio.timeout(4):
        await runtime.wait(submitted["run_id"])
    assert "error" not in receipt and receipt["state"] == "running", receipt
    assert receipt["session_id"] == submitted["session_id"]
    assert runtime.store.get("run", submitted["run_id"])["status"] == "completed"
    command = runtime.shells._commands[receipt["command_id"]]
    async with asyncio.timeout(4):
        while not (directory / "ready").exists():
            command.changed.clear()
            if (directory / "ready").exists():
                break
            await command.changed.wait()
    assert command.state == "running" and not command.done.is_set()
    assert (directory / "effects").read_text(encoding="utf-8").splitlines() == ["ran"]
    return receipt


async def release_command(runtime: Runtime, receipt: dict[str, Any], directory: Path) -> None:
    command = runtime.shells._commands[receipt["command_id"]]
    (directory / "release").write_text("continue", encoding="utf-8")
    assert command.monitor is not None
    async with asyncio.timeout(4):
        await command.done.wait()
        await asyncio.shield(command.monitor)
    assert command.proof.is_set() and command.stdout_fence.is_set()
    assert command.stderr_fence.is_set()
    assert command.state == "completed" and command.exit_code == 7


def notice_events(runtime: Runtime) -> list[dict[str, Any]]:
    return [
        event
        for event in runtime.store.events()
        if event["type"] == "notification.created" and event["data"].get("source") == "shell"
    ]


@pytest.mark.parametrize("ending", ["stop", "timeout", "lost", "close", "identity-deleted"])
async def test_notice_waits_for_real_terminal_cleanup_or_honors_identity_deletion(
    tmp_path: Path, ending: str
) -> None:
    runtime = await shell_runtime(tmp_path)
    directory = tmp_path / "terminal-command"
    receipt: dict[str, Any] = {}
    try:
        receipt = await owner_running_command(
            runtime, directory, timeout=2 if ending == "timeout" else 12
        )
        command = runtime.shells._commands[receipt["command_id"]]
        process = command.shell.process
        assert process is not None and process.returncode is None
        assert finish_notices(runtime) == []
        if ending == "stop":
            await runtime.stop(receipt["session_id"])
        elif ending == "lost":
            # This case loses the entire owned process group. Killing only
            # its shell parent leaves the waiting child holding both pipes;
            # it does not establish the same observable loss precondition.
            assert os.getpgid(process.pid) == process.pid
            os.killpg(process.pid, signal.SIGKILL)
        elif ending == "close":
            await runtime.close()
            assert command.shell.cleanup is not None and command.shell.cleanup.done()
            runtime = await shell_runtime(tmp_path)
            assert runtime.shells.live_sessions() == ()
        elif ending == "identity-deleted":
            # Main identity is protected by the public delete API. Remove only
            # this temporary DB object to exercise a late producer's existence
            # fence; this is not a claim that the public main delete is allowed.
            assert runtime.store.delete("session", receipt["session_id"])
            await release_command(runtime, receipt, directory)
            assert runtime.store.get("session", receipt["session_id"]) is None
            assert finish_notices(runtime) == [] and notice_events(runtime) == []
            return
        async with asyncio.timeout(4):
            await command.done.wait()
            cleanup = command.shell.cleanup
            assert cleanup is not None
            await asyncio.shield(cleanup)
        expected = {
            "stop": "stopped",
            "timeout": "timed_out",
            "lost": "unknown",
            "close": "stopped",
        }
        assert command.state == expected[ending] and command.exit_code is None
        assert process.returncode is not None
        assert command.shell.state == "closed" and not command.shell.cleanup_failed
        assert command.monitor is not None and command.monitor.done()
        notices = finish_notices(runtime)
        events = notice_events(runtime)
        assert len(notices) == len(events) == 1 and events[0]["data"] == notices[0]
        assert notices[0]["session_id"] == receipt["session_id"]
        assert receipt["command_id"] in notices[0]["content"]
        assert receipt["os_epoch"] in notices[0]["id"]
        assert "退出码 7" not in notices[0]["content"]
        assert "成功" not in notices[0]["title"] + notices[0]["content"]
        if ending == "lost":
            assert "未知" in notices[0]["title"]
        else:
            assert "进程树已收口" in notices[0]["content"]
        assert (directory / "effects").read_text(encoding="utf-8").splitlines() == ["ran"]
    finally:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "release").write_text("cleanup", encoding="utf-8")
        await runtime.close()


async def test_selected_command_identity_deduplication_and_read_flag_survive_later_epoch(
    tmp_path: Path,
) -> None:
    runtime = await shell_runtime(tmp_path)
    directories = [tmp_path / name for name in ("selected-old", "unselected-new", "selected-new")]
    try:
        first = await owner_running_command(runtime, directories[0])
        await release_command(runtime, first, directories[0])
        notices = finish_notices(runtime)
        assert len(notices) == 1
        read_notice = {**notices[0], "read": True}
        runtime.store.put("notification", read_notice)
        command = runtime.shells._commands[first["command_id"]]
        # Exercise both in-memory claim and the durable ID fence independently.
        await runtime.shells._finish(command)
        await runtime._shell_finished(
            ShellFinished(
                first["session_id"], first["command_id"], first["os_epoch"], "completed", 7
            )
        )
        await runtime.shells.stop(first["session_id"])
        assert finish_notices(runtime) == [read_notice] and len(notice_events(runtime)) == 1

        second = await owner_running_command(runtime, directories[1], notify_on_finish=False)
        assert second["os_epoch"] != first["os_epoch"]
        assert "notify_on_finish" not in second and "notification_destination" not in second
        await release_command(runtime, second, directories[1])
        assert finish_notices(runtime) == [read_notice] and len(notice_events(runtime)) == 1

        third = await owner_running_command(runtime, directories[2])
        assert third["os_epoch"] == second["os_epoch"]
        await release_command(runtime, third, directories[2])
        notices = finish_notices(runtime)
        assert len(notices) == len(notice_events(runtime)) == 2
        assert runtime.store.get("notification", read_notice["id"]) == read_notice
        assert sum(third["command_id"] in notice["content"] for notice in notices) == 1
        assert all(second["command_id"] not in notice["content"] for notice in notices)
    finally:
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "release").write_text("cleanup", encoding="utf-8")
        await runtime.close()


async def test_real_delegated_worker_cannot_register_notice_but_keeps_plain_shell(
    tmp_path: Path,
) -> None:
    runtime = await shell_runtime(tmp_path)
    refused: dict[str, Any] = {}
    allowed: dict[str, Any] = {}
    main_round = 0
    denial_message = "完成通知仅供真实主人当前请求中的主 MuLi 登记，队友不能登记"

    def worker_script() -> ShellScript:
        nonlocal refused, allowed
        refused = yield call(
            "worker-refused-notice",
            "shell_run",
            command="echo MUST_NOT_DISPATCH",
            cwd=str(tmp_path),
            notify_on_finish=True,
        )
        assert refused == {"error": denial_message}, refused
        assert runtime.shells.live_sessions() == () and not runtime.shells._commands
        allowed = yield from completed_shell(
            call(
                "worker-plain-command",
                "shell_run",
                command="echo PLAIN_WORKER_COMMAND",
                cwd=str(tmp_path),
                notify_on_finish=False,
            )
        )
        return "普通命令已返回真实回执，未登记通知。"

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_round
            main_round += 1
            turn = (
                ModelTurn(
                    tool_calls=[
                        call(
                            "spawn-notice-worker",
                            "spawn_agent",
                            task_name="notice-worker",
                            prompt="核对自己的普通 shell 权限，不代替主助手登记通知。",
                        ),
                        call(
                            "wait-notice-worker",
                            "wait_agents",
                            targets=["notice-worker"],
                            timeout=2,
                        ),
                    ]
                )
                if main_round == 1
                else ModelTurn(content="已经核对队友的真实工具权限。")
            )
            yield StreamEvent("done", turn=turn)

    worker = ShellDialogue(worker_script())

    def factory(config: ModelConfig) -> FakeClient:
        return ShellClient(worker) if config.model == "worker" else Main([], [])

    runtime.client_factory = factory
    try:
        submitted = await runtime.submit("委派临时队友核对普通命令和通知登记边界。", mode="yolo")
        async with asyncio.timeout(4):
            await runtime.wait(submitted["run_id"])
            async with runtime.bus.changed:
                await runtime.bus.changed.wait_for(
                    lambda: runtime.team.get("notice-worker")["status"] not in {"queued", "running"}
                )
        assert refused == {"error": denial_message}, refused
        agent = runtime.team.get("notice-worker")
        assert agent["status"] == "completed" and agent["generation"] == 1
        assert agent["parent_id"] == submitted["session_id"] and agent["mode"] == "yolo"
        session = runtime.store.get("session", agent["session_id"])
        assert session is not None and session["kind"] == "team"
        assert session["principal"] == "owner" and session["role"] == "worker"
        execution = runtime.store.get("run", agent["run_id"])
        assert execution is not None and execution["status"] == "completed"
        assert execution["session_id"] == agent["session_id"]
        assert execution["role"] == "worker" and execution["mode"] == "yolo"
        assert "error" not in allowed and allowed["state"] == "completed"
        assert allowed["session_id"] == agent["session_id"]
        assert allowed["exit_code"] == 0 and "PLAIN_WORKER_COMMAND" in allowed["output"]
        assert "notify_on_finish" not in allowed and "notification_destination" not in allowed
        assert finish_notices(runtime) == [] and notice_events(runtime) == []
    finally:
        await runtime.close()


@pytest.mark.parametrize("ending", ["completed", "stopped"])
async def test_notification_write_failure_rolls_back_without_rewriting_shell_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    runtime = await shell_runtime(tmp_path)
    directory = tmp_path / "write-failure"
    attempts = 0
    try:
        receipt = await owner_running_command(runtime, directory)
        original_put = runtime.store._put

        def failing_put(kind: str, item: dict[str, Any]) -> None:
            nonlocal attempts
            original_put(kind, item)
            if kind == "notification" and item.get("source") == "shell":
                attempts += 1
                raise OSError("temporary local notification write fault")

        with monkeypatch.context() as patch:
            patch.setattr(runtime.store, "_put", failing_put)
            if ending == "completed":
                await release_command(runtime, receipt, directory)
            else:
                await runtime.stop(receipt["session_id"])
        command = runtime.shells._commands[receipt["command_id"]]
        assert command.state == ending and command.done.is_set()
        assert command.exit_code == (7 if ending == "completed" else None)
        assert attempts == 1 and command.finish_attempted
        assert finish_notices(runtime) == [] and notice_events(runtime) == []
        await runtime.shells.stop(receipt["session_id"])
        assert command.shell.state == "closed" and not command.shell.cleanup_failed
        assert command.monitor is not None and command.monitor.done()
        assert attempts == 1 and finish_notices(runtime) == []
        assert (directory / "effects").read_text(encoding="utf-8").splitlines() == ["ran"]
        if ending == "completed":
            original = await runtime.shells.read(receipt["session_id"], receipt["command_id"])
            assert original["exit_code"] == 7 and "PRIVATE_RETAINED_TAIL" in original["output"]
    finally:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "release").write_text("cleanup", encoding="utf-8")
        await runtime.close()


async def test_enabled_attention_consumes_real_shell_notice_without_starting_a_model_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await shell_runtime(tmp_path)
    runtime.config.update({"attention": {"enabled": True, "coalesce_seconds": 0}})
    inbox = inbox_for(runtime)
    directory = tmp_path / "attention-command"
    try:
        # Establish the enabled consumer's durable cursor before this notice,
        # so skipping history cannot manufacture the no-model result.
        await inbox.start()
        await inbox.close()
        receipt = await owner_running_command(runtime, directory)
        run_count = len(runtime.store.list("run"))

        def forbidden_factory(_config: ModelConfig) -> NoReturn:
            raise AssertionError("shell 完成通知不得自动请求模型")

        runtime.client_factory = forbidden_factory
        await release_command(runtime, receipt, directory)
        events = notice_events(runtime)
        assert len(events) == 1
        await consume_until_idle(runtime, inbox, monkeypatch)
        assert inbox.cursor >= events[0]["id"]
        assert attention_inputs(runtime) == []
        assert runtime.store.list("attention_receipt") == []
        assert len(runtime.store.list("run")) == run_count
    finally:
        await inbox.close()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "release").write_text("cleanup", encoding="utf-8")
        await runtime.close()


async def test_dispatched_notice_survives_unrelated_reconfigure_and_new_owner_input(
    tmp_path: Path,
) -> None:
    runtime = await shell_runtime(tmp_path)
    directory = tmp_path / "new-input-command"
    try:
        receipt = await owner_running_command(runtime, directory)
        command = runtime.shells._commands[receipt["command_id"]]
        original_manager = runtime.shells
        runtime.config.update({"models": {"main": {"model": "main-updated"}}})
        await runtime.reconfigure()
        assert runtime.shells is original_manager
        assert runtime.shells._commands[receipt["command_id"]] is command

        def followup() -> ShellScript:
            status = yield call("new-owner-status", "shell_read")
            assert status["command_id"] == receipt["command_id"]
            assert status["os_epoch"] == receipt["os_epoch"] and status["state"] == "running"
            return "新的主人输入已处理；原命令继续执行。"

        plan = ShellDialogue(followup())
        runtime.client_factory = lambda _config: ShellClient(plan)
        newer = await runtime.submit("只查状态，原命令继续按约定留本地通知。", mode="yolo")
        async with asyncio.timeout(4):
            await runtime.wait(newer["run_id"])
        assert newer["session_id"] == receipt["session_id"]
        assert command.state == "running" and not command.done.is_set()
        assert finish_notices(runtime) == []
        await release_command(runtime, receipt, directory)
        notices = finish_notices(runtime)
        assert len(notices) == 1 and len(notice_events(runtime)) == 1
        assert receipt["command_id"] in notices[0]["content"]
        assert receipt["os_epoch"] in notices[0]["id"]
    finally:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "release").write_text("cleanup", encoding="utf-8")
        await runtime.close()


async def test_retire_collects_cancelled_finish_callback_from_an_earlier_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await shell_runtime(tmp_path)
    published = asyncio.Event()
    cancelled = asyncio.Event()
    hold = asyncio.Event()
    original_finish = runtime.shells._on_finish
    assert original_finish is not None
    directories = [tmp_path / name for name in ("earlier-monitor", "current-command")]

    async def paused_finish(result: ShellFinished) -> None:
        await original_finish(result)
        published.set()
        try:
            await hold.wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(runtime.shells, "_on_finish", paused_finish)
    try:
        first = await owner_running_command(runtime, directories[0])
        command = runtime.shells._commands[first["command_id"]]
        (directories[0] / "release").write_text("continue", encoding="utf-8")
        async with asyncio.timeout(4):
            await published.wait()
        monitor = command.monitor
        assert monitor is not None and not monitor.done()
        assert command.state == "completed" and command.done.is_set()
        second = await owner_running_command(runtime, directories[1], notify_on_finish=False)
        assert second["os_epoch"] == first["os_epoch"]
        assert first["command_id"] in runtime.shells._commands
        await runtime.stop(first["session_id"])
        assert cancelled.is_set() and monitor.done()
        current = runtime.shells._commands[second["command_id"]]
        assert current.state == "stopped" and current.done.is_set()
        assert current.monitor is not None and current.monitor.done()
        assert current.shell.state == "closed" and not current.shell.cleanup_failed
        assert runtime.shells.live_sessions() == ()
        notices = finish_notices(runtime)
        assert len(notices) == len(notice_events(runtime)) == 1
        assert first["command_id"] in notices[0]["content"]
        assert second["command_id"] not in notices[0]["content"]
    finally:
        hold.set()
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "release").write_text("cleanup", encoding="utf-8")
        await runtime.close()
