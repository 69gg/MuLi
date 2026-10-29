"""Real owner command completion, with an explicitly selected local notice."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
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

import muli.notifications as notifications
from muli.channels.manager import ChannelManager
from muli.providers import Message, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import new_id

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="首片真实命令旅程限 POSIX；不冒充 Windows 原生命令状态验收"
)


@pytest.fixture(autouse=True)
def offline_and_no_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("此旅程仅允许临时本机命令和进程内 ASGI，不允许外网/外发/桌面")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(notifications, "run_process", forbidden)
    monkeypatch.setattr(ChannelManager, "enqueue", forbidden)


def finish_notices(runtime: Runtime) -> list[dict[str, Any]]:
    return [item for item in runtime.store.list("notification") if item.get("source") == "shell"]


@pytest.mark.parametrize("notify_on_finish", [None, False, True], ids=["default", "false", "true"])
async def test_owner_command_finishes_after_main_turn_and_leaves_only_selected_notice(
    tmp_path: Path, notify_on_finish: bool | None
) -> None:
    runtime = await shell_runtime(tmp_path)
    assert not runtime.config.settings.notifications.enabled
    nonce = new_id()
    counter = tmp_path / "command-effects.txt"
    ready = tmp_path / "command-ready"
    release = tmp_path / "release-command"
    script = tmp_path / "finish-notice-command.py"
    private_output = f"PRIVATE_UNREAD_OUTPUT:{nonce}"
    private_error = f"PRIVATE_UNREAD_ERROR:{nonce}"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "\n"
        "def main() -> int:\n"
        f"    counter: Path = Path({str(counter)!r})\n"
        f"    ready: Path = Path({str(ready)!r})\n"
        f"    release: Path = Path({str(release)!r})\n"
        "    with counter.open('a', encoding='utf-8') as effects:\n"
        f"        effects.write({nonce!r} + chr(10))\n"
        f"    ready.write_text({nonce!r}, encoding='utf-8')\n"
        "    print('COMMAND_READY', flush=True)\n"
        "    deadline: float = time.monotonic() + 10\n"
        "    while not release.exists():\n"
        "        if time.monotonic() >= deadline:\n"
        "            return 19\n"
        "        time.sleep(0.01)\n"
        f"    print({private_output!r}, flush=True)\n"
        f"    print({private_error!r}, file=sys.stderr, flush=True)\n"
        "    return 7\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    launched: dict[str, Any] = {}
    read_back: dict[str, Any] = {}
    seen: list[list[Message]] = []

    def launch_script() -> ShellScript:
        nonlocal launched
        boot = yield from completed_shell(
            call("notice-boot", "shell_run", command="echo NOTICE_BOOT", cwd=str(tmp_path))
        )
        arguments: dict[str, Any] = {
            "command": shell_python_command(boot["flavor"], script),
            "wait_seconds": 0,
            "timeout": 12,
        }
        if notify_on_finish is not None:
            arguments["notify_on_finish"] = notify_on_finish
        launched = yield call("notice-launch", "shell_run", **arguments)
        return "已取得真实工具回执，本轮先结束；未宣称命令或任务成功。"

    def inspect_script() -> ShellScript:
        nonlocal read_back
        read_back = yield call(
            "notice-explicit-read", "shell_read", command_id=launched["command_id"]
        )
        return "只读取原命令的保留回执，没有重做命令。"

    dialogue = ShellDialogue(launch_script())

    class ObservedClient(ShellClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(list(messages))
            async for event in super().stream(messages, tools):
                yield event

    runtime.client_factory = lambda _config: ObservedClient(dialogue)
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            first = await runtime.submit(
                "在临时目录启动这个命令，先释放主对话；"
                + ("结束后只在 Web 通知里告诉我。" if notify_on_finish else "无需完成通知。"),
                mode="yolo",
            )
            async with asyncio.timeout(4):
                await runtime.wait(first["run_id"])
            session_id = first["session_id"]
            assert first["message"]["principal"] == "owner"
            assert first["message"]["source"] == "web"
            assert runtime.store.get("run", first["run_id"])["status"] == "completed"
            assert "error" not in launched, (
                "API 前置：notify_on_finish 参数必须通过真实工具入口；此处失败只算 API 缺口红，"
                "不算真实命令完成后的通知缺失红",
                launched,
            )
            assert launched["state"] == "running" and launched["exit_code"] is None
            command = runtime.shells._commands[launched["command_id"]]
            assert command.shell.session_id == session_id
            assert command.shell.epoch == launched["os_epoch"]
            assert not release.exists() and not command.done.is_set()
            async with asyncio.timeout(4):
                while not ready.exists():
                    command.changed.clear()
                    if ready.exists():
                        break
                    await command.changed.wait()
            assert ready.read_text(encoding="utf-8") == nonce
            assert counter.read_text(encoding="utf-8").splitlines() == [nonce]
            assert command.state == "running" and command.exit_code is None
            assert finish_notices(runtime) == []
            rounds_before_finish = len(seen)
            release.write_text("continue", encoding="utf-8")
            monitor = command.monitor
            assert monitor is not None
            # Completion and notification production must belong to the original
            # monitor, not a timer or an uncollected detached task in this test.
            async with asyncio.timeout(4):
                await command.done.wait()
                await asyncio.shield(monitor)
            assert command.proof.is_set()
            assert command.stdout_fence.is_set() and command.stderr_fence.is_set()
            assert command.state == "completed" and command.exit_code == 7
            assert counter.read_text(encoding="utf-8").splitlines() == [nonce]
            assert len(seen) == rounds_before_finish
            assert all(
                private_output not in message["content"] and private_error not in message["content"]
                for message in runtime.store.messages(session_id)
            ), "未读双流尾部不能因完成而自动进入对话"

            notices = finish_notices(runtime)
            notice_events = [
                event
                for event in runtime.store.events()
                if event["type"] == "notification.created"
                and event["data"].get("source") == "shell"
            ]
            if notify_on_finish:
                assert len(notices) == 1, (
                    "主旅程产品红：真实主人命令已派发，主回合已释放，原生产 monitor 已完成，"
                    "三份完成证明和退出码 7 已成立，但缺少唯一持久 Web 通知",
                    {"state": command.state, "exit_code": command.exit_code, "notices": notices},
                )
                notice = notices[0]
                assert notice["session_id"] == session_id and notice["read"] is False
                assert launched["command_id"] in notice["content"]
                assert "退出码 7" in notice["content"]
                assert "成功" not in notice["title"] + notice["content"]
                assert len(notice_events) == 1 and notice_events[0]["data"] == notice
                serialized = json.dumps([notice, notice_events[0]], ensure_ascii=False)
                assert private_output not in serialized and private_error not in serialized
                assert str(tmp_path) not in serialized
                response = await web.get("/api/notifications")
                assert response.status_code == 200
                assert [
                    item for item in response.json()["items"] if item["id"] == notice["id"]
                ] == [notice]
                assert (await web.get(f"/api/sessions/{notice['session_id']}")).status_code == 200
            else:
                assert notices == [] and notice_events == []

            assert len(seen) == rounds_before_finish
            dialogue = ShellDialogue(inspect_script())
            second = await runtime.submit("现在只读原命令的结果，不要重新执行。", mode="yolo")
            async with asyncio.timeout(4):
                await runtime.wait(second["run_id"])
            assert second["session_id"] == session_id
            assert "error" not in read_back and read_back["state"] == "completed", read_back
            assert read_back["command_id"] == launched["command_id"]
            assert read_back["os_epoch"] == launched["os_epoch"] and read_back["exit_code"] == 7
            assert private_output in read_back["output"] and private_error in read_back["stderr"]
            assert counter.read_text(encoding="utf-8").splitlines() == [nonce]
            assert finish_notices(runtime) == notices
    finally:
        release.write_text("cleanup", encoding="utf-8")
        await runtime.close()
