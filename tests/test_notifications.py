from __future__ import annotations

import asyncio
import codecs
import json
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import muli.notifications as notifications
from muli.config import ConfigFile, DesktopNotifications, Settings
from muli.events import EventBus
from muli.notifications import DesktopNotifier, ProcessRunner, notification_capability
from muli.reminders import Reminders
from muli.store import Store, now


@pytest.fixture(autouse=True)
def never_run_real_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(argv: list[str], *, timeout: float, limit: int) -> dict[str, Any]:
        raise AssertionError("测试禁止实际调用 notify-send")

    monkeypatch.setattr(notifications, "run_process", forbidden)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    value = Store(tmp_path / "notifications.sqlite3")
    yield value
    value.close()


@pytest_asyncio.fixture
async def factory(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Callable[..., DesktopNotifier]]:
    monkeypatch.setattr(notifications.sys, "platform", "linux")
    monkeypatch.setattr(notifications.shutil, "which", lambda command: "/installed/notify-send")
    notifiers: list[DesktopNotifier] = []

    def create(
        config: DesktopNotifications | None = None,
        *,
        runner: ProcessRunner | None = None,
        **limits: Any,
    ) -> DesktopNotifier:
        notifier = DesktopNotifier(
            store,
            EventBus(store),
            config or DesktopNotifications(enabled=True),
            runner=runner,
            **limits,
        )
        notifiers.append(notifier)
        return notifier

    yield create
    for notifier in notifiers:
        await notifier.close()


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, argv: list[str], *, timeout: float, limit: int) -> dict[str, Any]:
        self.calls.append({"argv": argv, "timeout": timeout, "limit": limit})
        return {"exit_code": 0, "output": "", "stderr": "", "truncated": False}


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def emit(notifier: DesktopNotifier, title: str = "提醒", content: str = "正文") -> int:
    event = await notifier.bus.emit(
        "notification.created", {"id": "notice", "title": title, "content": content}
    )
    return int(event["id"])


def test_default_disabled_and_configuration_roundtrip(tmp_path: Path) -> None:
    assert not Settings().notifications.enabled
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {"notifications": {"enabled": True, "timeout": 3, "max_queue": 8, "expire_ms": 5000}}
    )
    loaded = ConfigFile(config.path)
    assert loaded.settings.notifications == DesktopNotifications(
        enabled=True, timeout=3, max_queue=8, expire_ms=5000
    )
    assert config.public()["notifications"]["enabled"] is True
    assert "notifications" in json.loads(config.path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": "true"},
        {"max_queue": 0},
        {"max_queue": 129},
        {"timeout": 0},
        {"timeout": 31},
        {"timeout": float("nan")},
        {"expire_ms": 0},
        {"expire_ms": 60001},
    ],
)
def test_notification_configuration_limits(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        DesktopNotifications(**changes)


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_other_desktops_are_explicitly_unsupported(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(notifications.sys, "platform", platform)
    monkeypatch.setattr(
        notifications.shutil,
        "which",
        lambda command: pytest.fail("不支持的平台不应寻找或执行 notify-send"),
    )
    result = notification_capability()
    assert not result["supported"] and not result["available"]
    assert result["command"] is None and "Linux" in result["reason"]


def test_cli_detection_does_not_claim_a_connected_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notifications.sys, "platform", "linux")
    monkeypatch.setattr(notifications.shutil, "which", lambda command: "/installed/notify-send")
    result = notification_capability()
    assert result["available"] and result["supported"]
    assert "实际发送时确认" in result["reason"]
    monkeypatch.setattr(notifications.shutil, "which", lambda command: None)
    assert not notification_capability()["available"]


@pytest.mark.asyncio
async def test_disabled_does_not_consume_or_send(factory: Callable[..., DesktopNotifier]) -> None:
    runner = RecordingRunner()
    notifier = factory(DesktopNotifications(), runner=runner)
    await notifier.start()
    await emit(notifier)
    await asyncio.sleep(0)
    assert notifier.status()["status"] == "disabled"
    assert not notifier.status()["running"] and not notifier._tasks and not runner.calls


@pytest.mark.asyncio
async def test_unavailable_cli_is_nonfatal_and_starts_no_tasks(
    factory: Callable[..., DesktopNotifier], monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier = factory(runner=RecordingRunner())
    monkeypatch.setattr(notifications.shutil, "which", lambda command: None)
    await notifier.start()
    assert notifier.status()["status"] == "unavailable"
    assert not notifier._tasks


@pytest.mark.asyncio
async def test_start_skips_old_events_and_repeat_start_does_not_duplicate(
    factory: Callable[..., DesktopNotifier],
) -> None:
    runner = RecordingRunner()
    notifier = factory(runner=runner)
    await emit(notifier, "旧通知")
    await notifier.start()
    await notifier.start()
    await emit(notifier, "新通知")
    await until(lambda: notifier.status()["sent"] == 1)
    assert len(runner.calls) == 1 and runner.calls[0]["argv"][-2] == "新通知"
    assert len(notifier._tasks) == 2


@pytest.mark.asyncio
async def test_idle_waits_on_condition_and_ignores_other_events_without_polling(
    factory: Callable[..., DesktopNotifier], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = RecordingRunner()
    notifier = factory(runner=runner)
    original = notifier.store.events
    reads = 0

    def counted(
        after: int = 0, session_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        nonlocal reads
        reads += 1
        return original(after, session_id, limit)

    monkeypatch.setattr(notifier.store, "events", counted)
    await notifier.start()
    await until(lambda: reads >= 1)
    initial = reads
    await asyncio.sleep(0.01)
    assert reads == initial
    event = await notifier.bus.emit("run.completed", {"run_id": "other"})
    await until(lambda: notifier._cursor == event["id"])
    assert not runner.calls
    await emit(notifier)
    await until(lambda: notifier.status()["sent"] == 1)


@pytest.mark.asyncio
async def test_id_only_chiliarch_event_reads_the_durable_notice(
    factory: Callable[..., DesktopNotifier],
) -> None:
    runner = RecordingRunner()
    notifier = factory(runner=runner)
    notifier.store.put(
        "notification",
        {
            "id": "chiliarch-event",
            "title": "外部消息",
            "content": "持久正文",
            "source": "chiliarch",
        },
    )
    await notifier.start()
    await notifier.bus.emit(
        "notification.created", {"id": "chiliarch-event", "source": "chiliarch"}
    )
    await until(lambda: notifier.status()["sent"] == 1)
    assert runner.calls[0]["argv"][-2:] == ["外部消息", "持久正文"]


@pytest.mark.asyncio
async def test_arguments_markup_octal_escapes_controls_and_lengths_are_protected(
    factory: Callable[..., DesktopNotifier],
) -> None:
    runner = RecordingRunner()
    notifier = factory(
        runner=runner, max_title_chars=80, max_body_chars=100, process_output_bytes=512
    )
    await notifier.start()
    body = r'\074img src="file:///secret"\076' + '<a href="https://invalid">X</a>&\x00' + "x" * 300
    await emit(notifier, "--action=execute\x00\n<title>" + "y" * 300, body)
    await until(lambda: notifier.status()["sent"] == 1)
    call = runner.calls[0]
    argv = call["argv"]
    assert argv[0] == "/installed/notify-send"
    assert argv[-3] == "--" and len(argv) == 8
    assert argv[-2].startswith("--action=execute")
    assert len(argv[-2]) <= 80 and len(argv[-1]) <= 100
    assert "\x00" not in "".join(argv) and "\n" not in argv[-2]
    assert "<" not in argv[-2] and "<" not in argv[-1]
    # 模拟 notify-send 对正文的一次 C 转义处理：八进制不能恢复成标签。
    interpreted = codecs.decode(argv[-1].encode("utf-8"), "unicode_escape")
    assert "<img" not in interpreted and "<a" not in interpreted
    assert r"\074img" in interpreted
    assert call["timeout"] == 5 and call["limit"] == 512
    assert "--wait" not in argv and "--hint=boolean:suppress-sound:true" in argv


@pytest.mark.asyncio
async def test_queue_overflow_is_bounded_and_never_blocks_event_producers(
    factory: Callable[..., DesktopNotifier],
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def slow(argv: list[str], *, timeout: float, limit: int) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"exit_code": 0}

    notifier = factory(DesktopNotifications(enabled=True, max_queue=2), runner=slow)
    await notifier.start()
    await emit(notifier, "正在发送")
    await asyncio.wait_for(entered.wait(), 1)
    async with asyncio.timeout(1):
        for index in range(8):
            await emit(notifier, f"排队 {index}", "大正文" * 2000)
    await until(lambda: notifier.status()["dropped"] == 6)
    assert notifier.status()["queued"] == 2
    release.set()
    await until(lambda: notifier.status()["sent"] == 3)
    assert calls == 3


@pytest.mark.asyncio
async def test_timeout_isolated_from_reminders_and_worker_can_recover(
    factory: Callable[..., DesktopNotifier],
) -> None:
    cancelled = asyncio.Event()
    calls = 0

    async def once_hung(argv: list[str], *, timeout: float, limit: int) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return {"exit_code": 0}

    notifier = factory(DesktopNotifications(enabled=True, timeout=0.01), runner=once_hung)
    await notifier.start()
    reminders = Reminders(notifier.store, notifier.bus)
    reminder = reminders.create("应该正常触发", now() + 1, "正文")
    await reminders._fire(reminder, now() + 1)
    await until(lambda: notifier.status()["failed"] == 1)
    assert cancelled.is_set() and notifier.status()["status"] == "error"
    assert len(notifier.store.list("notification")) == 1
    assert not notifier.store.get("reminder", reminder["id"])["enabled"]  # type: ignore[index]
    await emit(notifier, "下一条")
    await until(lambda: notifier.status()["sent"] == 1)
    assert notifier.status()["status"] == "ready" and notifier.status()["last_error"] is None


@pytest.mark.parametrize("failure", ["exit", "exception", "output"])
@pytest.mark.asyncio
async def test_process_failures_record_safe_status_without_sensitive_output(
    factory: Callable[..., DesktopNotifier], failure: str
) -> None:
    async def fail(argv: list[str], *, timeout: float, limit: int) -> dict[str, Any]:
        if failure == "exception":
            raise OSError("private dbus address and notification body")
        if failure == "output":
            raise ValueError("命令输出超过限制 secret-data")
        return {"exit_code": 1, "output": "private-output", "stderr": "secret-data"}

    notifier = factory(runner=fail)
    await notifier.start()
    await emit(notifier)
    await until(lambda: notifier.status()["failed"] == 1)
    status = json.dumps(notifier.status(), ensure_ascii=False)
    assert "private" not in status and "secret-data" not in status
    assert notifier.status()["running"] and notifier.status()["status"] == "error"


@pytest.mark.asyncio
async def test_close_cancels_sending_discards_queue_and_restart_skips_downtime(
    factory: Callable[..., DesktopNotifier],
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = 0

    async def hung(argv: list[str], *, timeout: float, limit: int) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return {"exit_code": 0}

    notifier = factory(runner=hung)
    await notifier.start()
    await emit(notifier, "当前发送")
    await asyncio.wait_for(entered.wait(), 1)
    queued = await emit(notifier, "待发送")
    await until(lambda: notifier._cursor == queued)
    await notifier.close()
    assert cancelled.is_set() and notifier.status()["queued"] == 0
    assert not notifier.status()["running"] and notifier.status()["status"] == "stopped"
    await emit(notifier, "停机期间")
    runner = RecordingRunner()
    notifier.runner = runner
    await notifier.start()
    new_event = await emit(notifier, "重新启动后的新提醒")
    await until(lambda: notifier._cursor == new_event and notifier.status()["sent"] == 1)
    assert calls == 1 and len(runner.calls) == 1
    assert runner.calls[0]["argv"][-2] == "重新启动后的新提醒"


@pytest.mark.asyncio
async def test_invalid_notice_does_not_kill_the_collector(
    factory: Callable[..., DesktopNotifier],
) -> None:
    runner = RecordingRunner()
    notifier = factory(runner=runner)
    await notifier.start()
    await notifier.bus.emit("notification.created", {"title": "", "content": "无标题"})
    await notifier.bus.emit(
        "notification.created", {"title": "标题", "content": {"invalid": "body"}}
    )
    await notifier.bus.emit("notification.created", {"title": "\x00", "content": "控制字符"})
    await emit(notifier, "有效消息")
    await until(lambda: notifier.status()["sent"] == 1)
    assert notifier.status()["dropped"] == 3 and len(runner.calls) == 1
