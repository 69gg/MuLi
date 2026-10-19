"""Opt-in screenshots and finite recordings through already installed system tools."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from muli.config import CaptureSettings
from muli.platforms.files import LocalFiles, artifact
from muli.process import run_process


class Capture:
    def __init__(
        self, config: CaptureSettings, data_dir: Path, *, platform: str | None = None
    ) -> None:
        self.config = config
        self.platform = platform or sys.platform
        self.files = LocalFiles(
            data_dir / "captures", config.max_file_bytes, config.max_total_bytes
        )

    def _mac_video_device(self) -> str:
        video, separator, audio = self.config.device.partition(":")
        if not video or (separator and audio != "none"):
            raise ValueError("macOS 无声录屏的 capture.device 只能是视频设备或视频设备:none")
        return video + ":none"

    def _backend(self, recording: bool) -> tuple[str, str]:
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
            "portal": {
                "detected": self.platform == "linux"
                and bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")),
                "implemented": False,
            },
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
