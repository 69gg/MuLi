"""Opt-in screenshots and finite recordings through already installed system tools."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from muli.config import CaptureSettings, Computer
from muli.platforms.files import LocalFiles, artifact
from muli.process import run_process


class Capture:
    def __init__(
        self,
        config: CaptureSettings,
        data_dir: Path,
        *,
        platform: str | None = None,
        python_path: str | None = None,
    ) -> None:
        self.config = config
        self.platform = platform or sys.platform
        self.python_path = python_path if python_path is not None else Computer().python_path
        self._portal: dict[str, Any] = {}
        self._portal_key: tuple[Any, ...] = ()
        self._portal_time = 0.0
        self._probe_task: asyncio.Task[None] | None = None
        self._closed = False
        self.files = LocalFiles(
            data_dir / "captures", config.max_file_bytes, config.max_total_bytes
        )

    def _portal_state(self) -> dict[str, Any]:
        key = (
            self.platform,
            self.python_path,
            self.config.enabled,
            self.config.backend,
            os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
        )
        if key != self._portal_key:
            self._portal_key, self._portal_time = key, 0.0
            self._portal = {
                "state": "unknown",
                "available": None,
                "version": None,
                "available_targets": None,
                "reason": "Portal 能力尚未确认",
            }
        ttl = 60 if self._portal.get("state") == "ready" else 10
        return {
            "implemented": True,
            "detected": self.platform == "linux" and bool(key[-1]),
            **self._portal,
            "stale": not self._portal_time or time.monotonic() - self._portal_time >= ttl,
        }

    async def _portal_call(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        result = await run_process(
            [self.python_path, str(Path(__file__).parent / "tools" / "portal_helper.py")],
            input_text=json.dumps(request),
            timeout=timeout,
            limit=24000,
            merge_stderr=False,
            terminate_grace=2 if request["operation"] == "screenshot" else 0,
        )
        try:
            data = json.loads(result["output"])
        except ValueError as exc:
            raise ValueError("无法读取 Portal 响应；检查系统 Python GI 与会话总线") from exc
        if not isinstance(data, dict):
            raise ValueError("Portal 响应格式无效")
        if result["exit_code"] or "error" in data:
            raise ValueError(str(data.get("error", "Portal 运行失败")))
        return data

    async def refresh_portal(self) -> dict[str, Any]:
        state = self._portal_state()
        if (
            self._closed
            or not self.config.enabled
            or self.config.backend != "portal"
            or self.platform != "linux"
        ):
            return state
        if not state["stale"]:
            return state
        if self._probe_task is None or self._probe_task.done():
            key = self._portal_key

            async def probe() -> None:
                try:
                    data = await self._portal_call({"operation": "probe", "timeout": 2}, 2)
                except (ValueError, OSError, TimeoutError) as exc:
                    data = {"state": "error", "available": False, "reason": str(exc)[:500]}
                self._portal_state()
                if key == self._portal_key:
                    self._portal.update(data)
                    self._portal_time = time.monotonic()

            self._probe_task = asyncio.create_task(probe())
        await asyncio.shield(self._probe_task)
        return self._portal_state()

    async def aclose(self) -> None:
        self._closed = True
        if self._probe_task is not None:
            self._probe_task.cancel()
            await asyncio.gather(self._probe_task, return_exceptions=True)
            self._probe_task = None

    def _mac_video_device(self) -> str:
        video, separator, audio = self.config.device.partition(":")
        if not video or (separator and audio != "none"):
            raise ValueError("macOS 无声录屏的 capture.device 只能是视频设备或视频设备:none")
        return video + ":none"

    def _backend(self, recording: bool) -> tuple[str, str]:
        if self.config.backend == "portal":
            if self.platform != "linux" or recording:
                return "", "Portal 后端仅支持 Linux 截图"
            state = self._portal_state()
            return (
                ("portal", "")
                if state["available"] and not state["stale"]
                else ("", str(state["reason"] or "Portal 能力待刷新"))
            )
        wayland, display = os.environ.get("WAYLAND_DISPLAY"), os.environ.get("DISPLAY")
        if self.platform == "linux":
            if recording:
                candidates = ["wf-recorder"] if wayland else ["ffmpeg"] if display else []
            else:
                candidates = (["grim", "gnome-screenshot"] if wayland else []) + (
                    ["gnome-screenshot", "scrot", "ffmpeg"] if display else []
                )
        elif self.platform == "darwin":
            candidates = ["ffmpeg"] if recording else ["screencapture"]
        elif self.platform == "win32":
            candidates = ["ffmpeg"]
        else:
            return "", "该平台尚无图像采集后端"
        if self.config.backend != "auto":
            candidates = [candidate for candidate in candidates if candidate == self.config.backend]
        for candidate in candidates:
            if shutil.which(candidate) is None:
                continue
            if candidate == "wf-recorder" and shutil.which("timeout") is None:
                continue
            if self.platform == "darwin" and recording and not self.config.device:
                return "", "macOS 录屏需要显式配置 capture.device（AVFoundation 设备）"
            if self.platform == "darwin" and recording:
                try:
                    self._mac_video_device()
                except ValueError as error:
                    return "", str(error)
            return candidate, ""
        return "", "没有适用的桌面采集工具或图形会话；请查看平台文档"

    def status(self) -> dict[str, Any]:
        screenshot, screenshot_reason = self._backend(False)
        recording, recording_reason = self._backend(True)
        return {
            "enabled": self.config.enabled,
            "available": self.config.enabled and bool(screenshot or recording),
            "screenshot": {
                "available": bool(screenshot),
                "backend": screenshot,
                "reason": screenshot_reason,
            },
            "recording": {
                "available": bool(recording),
                "backend": recording,
                "reason": recording_reason,
            },
            "portal": self._portal_state(),
            "permission_verified": False,
            "reason": "" if self.config.enabled else "桌面采集尚未启用",
        }

    def _command(self, backend: str, path: Path, seconds: int | None) -> list[str]:
        if backend == "grim":
            return ["grim", str(path)]
        if backend == "gnome-screenshot":
            return ["gnome-screenshot", "-f", str(path)]
        if backend == "scrot":
            return ["scrot", "-o", str(path)]
        if backend == "screencapture":
            return ["screencapture", "-x", "-t", "png", str(path)]
        if backend == "wf-recorder":
            assert seconds is not None
            return [
                "timeout",
                "--signal=INT",
                "--kill-after=2",
                str(seconds),
                "wf-recorder",
                "-y",
                "-r",
                str(self.config.fps),
                "-f",
                str(path),
            ]
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        if self.platform == "linux":
            command += [
                "-f",
                "x11grab",
                "-framerate",
                str(self.config.fps),
                "-i",
                self.config.device or os.environ["DISPLAY"],
            ]
        elif self.platform == "darwin":
            command += [
                "-f",
                "avfoundation",
                "-framerate",
                str(self.config.fps),
                "-i",
                self._mac_video_device(),
            ]
        else:
            command += [
                "-f",
                "gdigrab",
                "-framerate",
                str(self.config.fps),
                "-i",
                self.config.device or "desktop",
            ]
        if seconds is None:
            command += ["-frames:v", "1"]
        else:
            command += [
                "-t",
                str(seconds),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-vf",
                "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-fs",
                str(self.config.max_file_bytes),
            ]
        return [*command, str(path)]

    async def screenshot(self) -> dict[str, Any]:
        return await self._capture(None)

    async def record(self, seconds: int) -> dict[str, Any]:
        if type(seconds) is not int or not 1 <= seconds <= self.config.max_seconds:
            raise ValueError(f"录屏时长须为 1 至 {self.config.max_seconds} 秒的整数")
        return await self._capture(seconds)

    async def _capture(self, seconds: int | None) -> dict[str, Any]:
        if not self.config.enabled:
            raise ValueError("桌面采集尚未启用")
        if self.config.backend == "portal" and seconds is None and self.platform == "linux":
            return await self._capture_portal()
        backend, reason = self._backend(seconds is not None)
        if not backend:
            raise ValueError(reason)
        async with self.files.lock:
            path = self.files.allocate(".png" if seconds is None else ".mp4")
            try:
                await self.files.run_output(
                    run_process,
                    self._command(backend, path, seconds),
                    path,
                    timeout=(seconds or 0) + 12,
                    expected_codes=(0, 124) if backend == "wf-recorder" else (0,),
                )
                with path.open("rb") as source:
                    header = source.read(16)
                if (seconds is None and not header.startswith(b"\x89PNG\r\n\x1a\n")) or (
                    seconds is not None and header[4:8] != b"ftyp"
                ):
                    raise ValueError("采集结果不是有效的 PNG 或 MP4 文件")
                return artifact(
                    path, "image/png" if seconds is None else "video/mp4", backend=backend
                )
            except BaseException:
                path.unlink(missing_ok=True)
                raise

    async def _capture_portal(self) -> dict[str, Any]:
        async with self.files.lock:
            budget = min(
                self.config.max_file_bytes,
                self.config.max_total_bytes - self.files.used_bytes(),
            )
            if budget < 8:
                raise ValueError("本地采集目录已达到容量限制，请先清理文件")
            path = self.files.allocate(".png")
            self._portal_state()
            key = self._portal_key

            async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
                data = await self._portal_call(
                    {
                        "operation": "screenshot",
                        "output": str(path.absolute()),
                        "max_bytes": budget,
                        "timeout": self.config.portal_timeout_seconds,
                    },
                    self.config.portal_timeout_seconds + 1.5,
                )
                self._portal_state()
                if key == self._portal_key:
                    self._portal.update(data)
                    self._portal_time = time.monotonic()
                return {"exit_code": 0}

            try:
                await self.files.run_output(
                    run, [], path, timeout=self.config.portal_timeout_seconds + 1.5
                )
                with path.open("rb") as source:
                    if source.read(8) != b"\x89PNG\r\n\x1a\n":
                        raise ValueError("Portal 结果不是 PNG 文件")
                return artifact(path, "image/png", backend="portal")
            except BaseException:
                self._portal_time = 0.0
                path.unlink(missing_ok=True)
                raise
