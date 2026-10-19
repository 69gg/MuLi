from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import muli.capture as capture_module
from muli.capture import Capture
from muli.config import CaptureSettings, Settings
from muli.tools.computer import Computer

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":2")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(capture_module.shutil, "which", lambda name: "/bin/" + name)


async def test_capture_disabled_never_launches_process(tmp_path: Path) -> None:
    capture = Capture(CaptureSettings(), tmp_path, platform="linux")
    with pytest.raises(ValueError, match="尚未启用"):
        await capture.screenshot()
    assert not (tmp_path / "captures").exists()


async def test_screenshot_private_file_and_literal_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: None
) -> None:
    calls: list[list[str]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        assert kwargs["timeout"] == 12
        Path(arguments[-1]).write_bytes(PNG)
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(capture_module, "run_process", run)
    capture = Capture(
        CaptureSettings(enabled=True, backend="ffmpeg", device=":2 title=$x"),
        tmp_path / "有 空格",
        platform="linux",
    )
    result = await capture.screenshot()
    assert calls[0][calls[0].index("-i") + 1] == ":2 title=$x"
    assert result["mime_type"] == "image/png"
    assert Path(result["path"]).read_bytes() == PNG
    assert Path(result["path"]).parent == capture.files.root
    assert "-frames:v" in calls[0]


async def test_wayland_recording_has_interrupt_deadline_and_no_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(capture_module.shutil, "which", lambda name: "/bin/" + name)
    calls: list[list[str]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        Path(arguments[-1]).write_bytes(MP4)
        return {"exit_code": 124, "output": ""}

    monkeypatch.setattr(capture_module, "run_process", run)
    capture = Capture(CaptureSettings(enabled=True), tmp_path, platform="linux")
    result = await capture.record(3)
    assert calls[0][:5] == ["timeout", "--signal=INT", "--kill-after=2", "3", "wf-recorder"]
    assert "-y" in calls[0]
    assert "--audio" not in calls[0]
    assert result["mime_type"] == "video/mp4"


@pytest.mark.parametrize("seconds", [0, -1, 31, True, 1.5, "2"])
async def test_recording_rejects_unbounded_duration(tmp_path: Path, seconds: Any) -> None:
    with pytest.raises(ValueError, match="时长"):
        await Capture(CaptureSettings(enabled=True), tmp_path).record(seconds)


async def test_capture_growth_cancels_process_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: None
) -> None:
    cancelled = asyncio.Event()

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        Path(arguments[-1]).write_bytes(b"x" * 2048)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(capture_module, "run_process", run)
    capture = Capture(
        CaptureSettings(enabled=True, max_file_bytes=1024), tmp_path, platform="linux"
    )
    with pytest.raises(ValueError, match="容量限制"):
        await capture.screenshot()
    assert cancelled.is_set()
    assert list(capture.files.root.iterdir()) == []


async def test_capture_rejects_bad_format_and_total_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: None
) -> None:
    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        Path(arguments[-1]).write_bytes(b"not a picture")
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(capture_module, "run_process", run)
    capture = Capture(
        CaptureSettings(enabled=True, max_total_bytes=1024), tmp_path, platform="linux"
    )
    with pytest.raises(ValueError, match="PNG"):
        await capture.screenshot()
    (capture.files.root / "previous.png").write_bytes(b"x" * 1024)
    with pytest.raises(ValueError, match="容量限制"):
        await capture.screenshot()


def test_capabilities_do_not_claim_portal_probe_or_permission_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/mock")
    monkeypatch.setattr(capture_module.shutil, "which", lambda name: "/bin/" + name)
    status = Capture(CaptureSettings(enabled=True), tmp_path, platform="linux").status()
    assert status["available"] is False
    assert status["portal"]["detected"] is True
    assert status["portal"]["implemented"] is True
    assert status["portal"]["state"] == "unknown"
    assert status["portal"]["available"] is None
    assert status["permission_verified"] is False
    mac = Capture(CaptureSettings(enabled=True), tmp_path, platform="darwin").status()
    assert mac["screenshot"]["available"] is True
    assert mac["recording"]["available"] is False
    assert "device" in mac["recording"]["reason"]


def test_computer_capture_tools_use_configured_permission_mode(tmp_path: Path) -> None:
    computer = Computer(Settings(), capture=Capture(CaptureSettings(), tmp_path))
    tools = {tool.name: tool for tool in computer.tools()}
    for name in ("computer_screenshot", "computer_record"):
        assert tools[name].requires_approval is False
        assert tools[name].risk == "computer"


async def test_cancel_capture_stops_process_and_deletes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: None
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        Path(arguments[-1]).write_bytes(PNG)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(capture_module, "run_process", run)
    capture = Capture(CaptureSettings(enabled=True), tmp_path, platform="linux")
    task = asyncio.create_task(capture.screenshot())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert list(capture.files.root.iterdir()) == []


@pytest.mark.parametrize("device", ["0", "0:none", "Capture screen 1"])
async def test_macos_recording_forces_video_only_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    device: str,
) -> None:
    calls: list[list[str]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        Path(arguments[-1]).write_bytes(MP4)
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(capture_module, "run_process", run)
    monkeypatch.setattr(capture_module.shutil, "which", lambda name: "/bin/" + name)
    capture = Capture(CaptureSettings(enabled=True, device=device), tmp_path, platform="darwin")
    await capture.record(1)
    assert calls[0][calls[0].index("-i") + 1] == device.partition(":")[0] + ":none"
    assert "-an" in calls[0]


@pytest.mark.parametrize("device", ["0:0", "0:default", ":none", "0:", "0:none:0"])
async def test_macos_recording_rejects_audio_device_before_process_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    device: str,
) -> None:
    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("不能打开任何录音设备")

    monkeypatch.setattr(capture_module, "run_process", run)
    monkeypatch.setattr(capture_module.shutil, "which", lambda name: "/bin/" + name)
    capture = Capture(CaptureSettings(enabled=True, device=device), tmp_path, platform="darwin")
    assert capture.status()["recording"]["available"] is False
    with pytest.raises(ValueError, match="无声录屏"):
        await capture.record(1)
    assert not capture.files.root.exists()
