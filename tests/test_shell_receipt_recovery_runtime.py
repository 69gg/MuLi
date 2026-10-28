"""Normal Runtime reopen reads retained shell evidence without replaying work."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_shell_session_runtime import (
    ShellClient,
    ShellDialogue,
    ShellScript,
    completed_shell,
    shell_python_command,
    shell_runtime,
)
from test_teammate_attachment_runtime import call

from muli.config import ConfigFile
from muli.runtime import Runtime
from muli.shell_sessions import OutputCursor
from muli.store import new_id

# PowerShell's Out-Default can format native stdout/stderr. This first journey
# checks POSIX pipe bytes; it does not establish Windows byte equivalence.
pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="首片真实双流字节旅程限 POSIX；Windows 原生格式化另行验收"
)


async def test_normal_reopen_reads_unseen_shell_tail_without_replaying_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("此旅程只允许真实本机 shell 控制通道，不允许外部 HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime = await shell_runtime(tmp_path)
    # Lower the existing fixture's output cap; keep its 16000-character context
    # cap. Raw output fits, while JSON-escaped tabs/newlines require small pages.
    page_budget = 1200
    runtime.config.update({"limits": {"max_output_chars": page_budget}})
    await runtime.reconfigure()
    nonce = new_id()
    counter = tmp_path / "command-effects.txt"
    release = tmp_path / "release-command"
    script = tmp_path / "bounded-receipt-command.py"
    prefixes = {"output": f"PREFIX_OUT:{nonce}\n", "stderr": f"PREFIX_ERR:{nonce}\n"}
    tails = {
        "output": f"未读尾出-{nonce}\n" + "续出甲\t\n" * 42,
        "stderr": f"未读尾错-{nonce}\n" + "续错乙\t\n" * 42,
    }
    expected_cursor: OutputCursor = {
        "output": len((prefixes["output"] + tails["output"]).encode("utf-8")),
        "stderr": len((prefixes["stderr"] + tails["stderr"]).encode("utf-8")),
    }
    assert sum(expected_cursor.values()) < page_budget
    script.write_text(
        "from pathlib import Path\n"
        "from typing import BinaryIO\n"
        "import sys\n"
        "import time\n"
        "\n"
        "def emit(stream: BinaryIO, text: str) -> None:\n"
        "    stream.write(text.encode('utf-8'))\n"
        "    stream.flush()\n"
        "\n"
        "def main() -> None:\n"
        f"    counter: Path = Path({str(counter)!r})\n"
        f"    release: Path = Path({str(release)!r})\n"
        "    with counter.open('a', encoding='utf-8') as effects:\n"
        f"        effects.write({nonce!r} + chr(10))\n"
        f"    emit(sys.stdout.buffer, {prefixes['output']!r})\n"
        f"    emit(sys.stderr.buffer, {prefixes['stderr']!r})\n"
        "    deadline: float = time.monotonic() + 10\n"
        "    while not release.exists():\n"
        "        if time.monotonic() >= deadline:\n"
        "            raise SystemExit(7)\n"
        "        time.sleep(0.01)\n"
        f"    emit(sys.stdout.buffer, {tails['output']!r})\n"
        f"    emit(sys.stderr.buffer, {tails['stderr']!r})\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    launched: dict[str, Any] = {}
    prefix_pages: list[dict[str, Any]] = []
    original_cursor: OutputCursor = {"output": 0, "stderr": 0}
    recovered_pages: list[dict[str, Any]] = []
    empty_before: dict[str, Any] = {}
    empty_after: dict[str, Any] = {}
    reopened: Runtime | None = None

    def launch_script() -> ShellScript:
        nonlocal launched, original_cursor
        boot = yield from completed_shell(
            call("receipt-boot", "shell_run", command="echo RECEIPT_BOOT", cwd=str(tmp_path))
        )
        launched = yield call(
            "receipt-launch",
            "shell_run",
            command=shell_python_command(boot["flavor"], script),
            wait_seconds=0,
            timeout=12,
        )
        assert "error" not in launched and launched["state"] == "running", launched
        assert launched["exit_code"] is None and not release.exists()
        prefix_pages.append(launched)
        original_cursor = launched["next_cursor"].copy()
        for attempt in range(3):
            page = yield call(
                f"receipt-prefix-{attempt}",
                "shell_read",
                command_id=launched["command_id"],
                cursor=original_cursor,
                wait_seconds=1,
            )
            assert "error" not in page and page["state"] == "running", page
            assert page["exit_code"] is None
            prefix_pages.append(page)
            original_cursor = page["next_cursor"].copy()
            if all(
                "".join(part[stream] for part in prefix_pages) == prefixes[stream]
                for stream in prefixes
            ):
                break
        return "运行回执已返回，本轮不等命令结束。"

    def read_script() -> ShellScript:
        nonlocal empty_before, empty_after
        empty_before = yield call("reopened-empty-before", "shell_read")
        cursor = original_cursor.copy()
        for index in range(4):
            page = yield call(
                f"reopened-page-{index}",
                "shell_read",
                command_id=launched["command_id"],
                cursor=cursor,
            )
            recovered_pages.append(page)
            next_cursor = page.get("next_cursor")
            if (
                "error" in page
                or not isinstance(next_cursor, dict)
                or set(next_cursor) != {"output", "stderr"}
                or any(type(value) is not int for value in next_cursor.values())
                or next_cursor == cursor
            ):
                break
            cursor = next_cursor.copy()
            if cursor == expected_cursor:
                break
        empty_after = yield call("reopened-empty-after", "shell_read")
        return "只读核查回合结束，证据以实际工具消息为准。"

    dialogue = ShellDialogue(launch_script())
    runtime.client_factory = lambda _config: ShellClient(dialogue)
    try:
        first = await runtime.submit("启动临时命令，取得双流前缀后先回答。", mode="yolo")
        async with asyncio.timeout(4):
            await runtime.wait(first["run_id"])
        session_id = first["session_id"]
        assert runtime.store.get("run", first["run_id"])["status"] == "completed"
        assert first["message"]["principal"] == "owner" and first["message"]["source"] == "web"
        assert counter.read_text(encoding="utf-8").splitlines() == [nonce]
        assert not release.exists()
        assert len(prefix_pages) >= 2
        for stream in prefixes:
            assert "".join(page[stream] for page in prefix_pages) == prefixes[stream]
            assert original_cursor[stream] == len(prefixes[stream].encode("utf-8"))
        assert launched["session_id"] == session_id and launched["shell_pid"] is not None
        command = runtime.shells._commands[launched["command_id"]]
        assert command.state == "running" and not command.done.is_set()

        release.write_text("continue", encoding="utf-8")
        # Synchronize with production completion only. No tool reads this tail
        # before closing, and neither Store messages nor events contain it.
        async with asyncio.timeout(4):
            await command.done.wait()
        assert command.proof.is_set()
        assert command.stdout_fence.is_set() and command.stderr_fence.is_set()
        assert command.state == "completed" and command.exit_code == 0
        assert command.output.observed_by_stream == expected_cursor
        assert command.output.discarded == 0
        old_tools = [
            message for message in runtime.store.messages(session_id) if message["role"] == "tool"
        ]
        old_results = [json.loads(message["content"]) for message in old_tools]
        assert [
            result for result in old_results if result.get("command_id") == command.id
        ] == prefix_pages
        old_events = [
            event["data"]
            for event in runtime.store.events(session_id=session_id)
            if event["type"] == "tool.completed"
            and event["data"].get("name") in {"shell_run", "shell_read"}
        ]
        assert len(old_events) == len(old_tools)
        for marker in (f"未读尾出-{nonce}", f"未读尾错-{nonce}"):
            assert all(marker not in message["content"] for message in old_tools)
            assert all(marker not in event["result"] for event in old_events)
        config_path, data_dir = runtime.config.path, runtime.data_dir
        instance_id = runtime.store.instance_id
        await runtime.close()
        assert command.state == "completed" and command.shell.state == "closed"

        dialogue = ShellDialogue(read_script())
        reopened = Runtime(
            ConfigFile(config_path), data_dir, client_factory=lambda _config: ShellClient(dialogue)
        )
        assert reopened.config is not runtime.config and reopened.store is not runtime.store
        assert reopened.shells is not runtime.shells
        assert reopened.store.instance_id == instance_id
        assert reopened.main_session()["id"] == session_id
        assert reopened.shells.live_sessions() == ()
        assert reopened.shells.status(session_id)["state"] == "not_started"
        second = await reopened.submit(
            "只按原编号和原游标读取未交付尾部，不能重做命令。", mode="yolo"
        )
        async with asyncio.timeout(4):
            await reopened.wait(second["run_id"])
        assert second["session_id"] == session_id
        assert second["message"]["principal"] == "owner" and second["message"]["source"] == "web"
        assert reopened.store.get("run", second["run_id"])["status"] == "completed"
        stored_pages = [
            json.loads(message["content"])
            for message in reopened.store.messages(session_id)
            if (message.get("tool_call_id") or "").startswith("reopened-page-")
        ]
        assert stored_pages == recovered_pages and stored_pages
        new_events = [
            event["data"]
            for event in reopened.store.events(session_id=session_id)
            if (event["data"].get("call_id") or "").startswith("reopened-")
        ]
        assert all(event["name"] == "shell_read" for event in new_events)
        assert any(
            event.get("call_id") == "reopened-page-0" and "arguments" in event
            for event in new_events
        ), "前置：原编号读取必须通过真实权限检查并进入 tool.started"
        assert [
            json.loads(event["result"])
            for event in new_events
            if event.get("call_id", "").startswith("reopened-page-") and "result" in event
        ] == stored_pages

        # Establish the real completed command and unchanged side effect before
        # the intended product red: today's authorized read returns not_found.
        assert counter.read_text(encoding="utf-8").splitlines() == [nonce]
        assert reopened.shells.live_sessions() == ()
        assert command.done.is_set() and command.proof.is_set()
        assert command.stdout_fence.is_set() and command.stderr_fence.is_set()
        assert command.state == "completed" and command.exit_code == 0
        for status in (empty_before, empty_after):
            assert "error" not in status and status["state"] == "not_started", status
            assert status["command_id"] is None and status["shell_pid"] is None
        if "error" in stored_pages[0]:
            assert stored_pages[0].get("code") == "not_found", (
                "前置：只接受获准查找原命令后的 not_found，其他错误不是本片产品红",
                stored_pages[0],
            )
        assert stored_pages[0].get("receipt_source") == "stored", (
            "正常关闭、重开后，当前获准 shell_read 应读取原回执；实际工具消息：",
            stored_pages[0],
        )
        assert len(stored_pages) >= 2, "小输出预算必须实际分页，不能提高上限一次返回"
        cursor = original_cursor.copy()
        recovered = {"output": "", "stderr": ""}
        for page in stored_pages:
            assert "error" not in page and page["receipt_source"] == "stored", page
            assert page["session_id"] == session_id
            assert page["command_id"] == launched["command_id"]
            assert page["os_epoch"] == launched["os_epoch"]
            assert page["state"] == "completed"
            assert type(page["exit_code"]) is int and page["exit_code"] == command.exit_code
            assert page["shell_state"] == "closed" and page["shell_pid"] is None
            assert page["status_basis"] == launched["status_basis"]
            assert page["flavor"] == launched["flavor"]
            assert page["executable"] == launched["executable"]
            assert Path(page["cwd"]).resolve() == tmp_path.resolve()
            assert page["output_scope"] == "shell"
            assert page["observed_bytes"] == sum(expected_cursor.values())
            assert page["discarded_bytes"] == 0 and page["truncated"] is False
            assert page["cursor"] == cursor
            assert len(json.dumps(page, ensure_ascii=False)) <= page_budget
            next_cursor = page["next_cursor"]
            assert next_cursor != cursor
            for stream in recovered:
                assert type(next_cursor[stream]) is int
                assert cursor[stream] <= next_cursor[stream] <= expected_cursor[stream]
                assert len(page[stream].encode("utf-8")) == next_cursor[stream] - cursor[stream]
                recovered[stream] += page[stream]
            cursor = next_cursor.copy()
        assert cursor == expected_cursor
        assert recovered == tails
        assert counter.read_text(encoding="utf-8").splitlines() == [nonce]
        assert reopened.shells.live_sessions() == ()
    finally:
        release.write_text("cleanup", encoding="utf-8")
        if reopened is not None:
            await reopened.close()
        await runtime.close()


async def test_normal_close_preserves_running_command_as_stopped_without_replaying(
    tmp_path: Path,
) -> None:
    runtime = await shell_runtime(tmp_path)
    script = tmp_path / "running-at-close.py"
    effect = tmp_path / "forbidden-late-effect.txt"
    script.write_text(
        "from pathlib import Path\n"
        "import time\n"
        "\n"
        "def main() -> None:\n"
        "    print('RUNNING_AT_CLOSE', flush=True)\n"
        "    deadline: float = time.monotonic() + 10\n"
        "    while time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
        f"    Path({str(effect)!r}).write_text('must not happen', encoding='utf-8')\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    running: dict[str, Any] = {}

    def launch_script() -> ShellScript:
        nonlocal running
        boot = yield from completed_shell(
            call("stop-boot", "shell_run", command="echo STOP_BOOT", cwd=str(tmp_path))
        )
        running = yield call(
            "run-at-close",
            "shell_run",
            command=shell_python_command(boot["flavor"], script),
            wait_seconds=0,
            timeout=12,
        )
        for index in range(3):
            if "RUNNING_AT_CLOSE" in running["output"]:
                break
            running = yield call(
                f"read-before-close-{index}",
                "shell_read",
                command_id=running["command_id"],
                wait_seconds=1,
            )
        return "运行回执已返回。"

    dialogue = ShellDialogue(launch_script())
    runtime.client_factory = lambda _config: ShellClient(dialogue)
    reopened: Runtime | None = None
    try:
        started = await runtime.submit("启动受控临时命令，取得就绪输出后先回答。", mode="yolo")
        async with asyncio.timeout(4):
            await runtime.wait(started["run_id"])
        assert "error" not in running and running["state"] == "running"
        assert "RUNNING_AT_CLOSE" in running["output"]
        command = runtime.shells._commands[running["command_id"]]
        assert not command.done.is_set() and not command.proof.is_set()
        await runtime.close()
        assert command.done.is_set() and command.state == "stopped"
        assert command.exit_code is None and command.shell.state == "closed"
        assert command.shell.process is not None and command.shell.process.returncode is not None
        assert not effect.exists()

        def read_script() -> ShellScript:
            yield call("read-stopped", "shell_read", command_id=running["command_id"])
            return "只读核查结束。"

        dialogue = ShellDialogue(read_script())
        reopened = Runtime(
            ConfigFile(runtime.config.path),
            runtime.data_dir,
            client_factory=lambda _config: ShellClient(dialogue),
        )
        queried = await reopened.submit("只核查原命令终态，不执行命令。", mode="yolo")
        async with asyncio.timeout(4):
            await reopened.wait(queried["run_id"])
        stored = next(
            json.loads(message["content"])
            for message in reopened.store.messages(queried["session_id"])
            if message.get("tool_call_id") == "read-stopped"
        )
        assert stored["receipt_source"] == "stored" and stored["state"] == "stopped"
        assert stored["exit_code"] is None and stored["shell_pid"] is None
        assert stored["os_epoch"] == running["os_epoch"]
        assert stored["command_id"] == running["command_id"]
        assert "RUNNING_AT_CLOSE" in stored["output"]
        assert reopened.shells.live_sessions() == () and not effect.exists()
    finally:
        if reopened is not None:
            await reopened.close()
        await runtime.close()
