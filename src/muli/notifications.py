"""只消费启动后事件的可选 Linux 桌面通知副本。"""

from __future__ import annotations

import asyncio
import shutil
import sys
import unicodedata
from typing import Any, Protocol
from xml.sax.saxutils import escape

from muli.config import DesktopNotifications
from muli.events import EventBus
from muli.maintenance import Admission
from muli.process import run_process
from muli.store import Store


class ProcessRunner(Protocol):
    async def __call__(self, argv: list[str], *, timeout: float, limit: int) -> dict[str, Any]: ...


def notification_capability() -> dict[str, Any]:
    """仅检测平台和已有 CLI，不调用通知服务，也不保证桌面会话可达。"""
    supported = sys.platform == "linux"
    command = shutil.which("notify-send") if supported else None
    return {
        "backend": "notify-send",
        "supported": supported,
        "available": bool(command),
        "command": command,
        "reason": (
            "当前仅支持 Linux notify-send"
            if not supported
            else "未找到已安装的 notify-send"
            if command is None
            else "CLI 已安装；桌面会话连接仅在实际发送时确认"
        ),
    }


def _literal_text(value: str, limit: int, *, body: bool = False) -> str:
    """限制最终参数长度，保留完整 XML 实体并防止 g_strcompress 还原标记。"""
    source = value[: limit * 2]
    parts: list[str] = []
    used = 0
    truncated = len(source) < len(value)
    for character in source:
        if character in "\n\r\t":
            character = "\n" if body and character in "\n\r" else " "
        elif unicodedata.category(character) in {"Cc", "Cs"}:
            continue
        unit = escape(character)
        if body:
            # notify-send 对正文调用 g_strcompress；双反斜杠保持字面值。
            unit = unit.replace("\\", "\\\\")
        if used + len(unit) > limit - 1:
            truncated = True
            break
        parts.append(unit)
        used += len(unit)
    return "".join(parts) + ("…" if truncated else "")


class DesktopNotifier:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        config: DesktopNotifications,
        *,
        max_title_chars: int = 120,
        max_body_chars: int = 2000,
        process_output_bytes: int = 4096,
        runner: ProcessRunner | None = None,
        admission: Admission | None = None,
    ) -> None:
        if min(max_title_chars, max_body_chars, process_output_bytes) < 1:
            raise ValueError("桌面通知标题、正文和进程输出限制必须大于零")
        self.store, self.bus = store, bus
        self.config = config.model_copy(deep=True)
        self.max_title_chars, self.max_body_chars = max_title_chars, max_body_chars
        self.process_output_bytes = process_output_bytes
        self.runner = runner or run_process
        self.admission = admission or Admission()
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(self.config.max_queue)
        self._queued = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._capability = notification_capability()
        self._started = False
        self._state = "disabled" if not self.config.enabled else "stopped"
        self._cursor = 0
        self._sending = False
        self._sent = 0
        self._failed = 0
        self._dropped = 0
        self._last_error: str | None = None

    def status(self) -> dict[str, Any]:
        return {
            **self._capability,
            "enabled": self.config.enabled,
            "status": self._state,
            "running": self._started and any(not task.done() for task in self._tasks),
            "sending": self._sending,
            "queued": self._queue.qsize(),
            "sent": self._sent,
            "failed": self._failed,
            "dropped": self._dropped,
            "last_error": self._last_error,
        }

    async def start(self) -> None:
        if self._started:
            return
        self._capability = notification_capability()
        # 首次 await 前标记当前游标；停机期间的通知只留在工作台，不补发桌面副本。
        self._cursor = int(
            self.store.db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
        )
        self._last_error = None
        if not self.config.enabled:
            self._state = "disabled"
            return
        if not self._capability["supported"]:
            self._state = "unsupported"
            return
        if not self._capability["available"]:
            self._state = "unavailable"
            return
        self._state, self._started = "ready", True
        self._tasks = [
            asyncio.create_task(self._collect(), name="muli-notification-events"),
            asyncio.create_task(self._send(), name="muli-desktop-notifications"),
        ]
        for task in self._tasks:
            task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._state = "error"
            self._last_error = "桌面通知后台处理异常；工作台记录仍保留"
            for other in self._tasks:
                if other is not task:
                    other.cancel()

    def _enqueue(self, data: dict[str, Any]) -> None:
        identifier = data.get("id")
        notice = self.store.get("notification", identifier) if isinstance(identifier, str) else None
        notice = notice or data
        title, body = notice.get("title"), notice.get("content", "")
        if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
            self._dropped += 1
            return
        safe_title = _literal_text(title, self.max_title_chars)
        if not safe_title.strip():
            self._dropped += 1
            return
        item = (safe_title, _literal_text(body, self.max_body_chars, body=True))
        try:
            self._queue.put_nowait(item)
            self._queued.set()
        except asyncio.QueueFull:
            self._dropped += 1

    async def _collect(self) -> None:
        while True:
            revision = self.bus.revision
            events = self.store.events(self._cursor, limit=self.config.max_queue)
            if events:
                for event in events:
                    self._cursor = event["id"]
                    if event["type"] == "notification.created" and isinstance(event["data"], dict):
                        self._enqueue(event["data"])
                await asyncio.sleep(0)
                continue

            def changed(expected: int = revision) -> bool:
                return self.bus.revision != expected

            async with self.bus.changed:
                await self.bus.changed.wait_for(changed)

    async def _send(self) -> None:
        while True:
            await self.admission.wait()
            try:
                title, body = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                self._queued.clear()
                await self._queued.wait()
                continue
            with self.admission.operation():
                await self._deliver(title, body)

    async def _deliver(self, title: str, body: str) -> None:
        self._sending = True
        try:
            async with asyncio.timeout(self.config.timeout):
                result = await self.runner(
                    [
                        self._capability["command"],
                        "--app-name=MuLi",
                        "--urgency=normal",
                        f"--expire-time={self.config.expire_ms}",
                        "--hint=boolean:suppress-sound:true",
                        "--",
                        title,
                        body,
                    ],
                    timeout=self.config.timeout,
                    limit=self.process_output_bytes,
                )
            if result["exit_code"] != 0:
                raise OSError("notify-send 未确认成功")
            self._sent += 1
            self._state, self._last_error = "ready", None
        except asyncio.CancelledError:
            self._dropped += 1
            raise
        except Exception:
            self._failed += 1
            self._state = "error"
            self._last_error = "桌面通知发送失败；请检查通知服务和当前用户桌面会话"
        finally:
            self._sending = False
            self._queue.task_done()

    async def close(self) -> None:
        self._started = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
            self._dropped += 1
        self._state = "disabled" if not self.config.enabled else "stopped"
