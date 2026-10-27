from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest
from test_acoustic_bridge import configuration

from muli.acoustic_bridge import load_config
from muli.evidence import EvidenceError
from muli.platforms.files import LocalFiles
from muli.voice_configuration import prepare_acoustic, setup_guidance


def models(tmp_path: Path) -> dict[str, Any]:
    path = configuration(tmp_path)
    values = json.loads(path.read_text())
    for model in values.values():
        for name in model:
            model[name] = str(path.parent / model[name])
    return values


def allowed() -> None:
    return None


async def test_prepare_reuses_exact_private_snapshot_without_applying_or_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = models(tmp_path)
    files = LocalFiles(tmp_path / "private", 2_000_000, 20_000_000)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    result = await prepare_acoustic(values, files, max_output_chars=10000, guard=allowed)
    assert result["prepared"] is True and result["applied"] is False
    assert result["preflight"]["available"] is False
    assert result["preflight"]["runtime_verified"] is False
    assert result["changes"]["voice"]["enabled"] is False
    path = Path(result["configuration_path"])
    assert path.parent == files.root and path.is_file()
    assert path.name == "acoustic-" + result["revision"] + ".json"
    assert result["changes"]["voice"]["wake_command"][-1] == str(path)
    assert result["changes"]["voice"]["speaker_command"][-1] == str(path)
    assert load_config(path).wake is not None
    second = await prepare_acoustic(values, files, max_output_chars=10000, guard=allowed)
    assert second == result and list(files.root.iterdir()) == [path]
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0


async def test_preparation_output_and_caller_gates_precede_any_write(tmp_path: Path) -> None:
    values = models(tmp_path)
    files = LocalFiles(tmp_path / "private", 2_000_000, 20_000_000)
    with pytest.raises(EvidenceError, match="预算"):
        await prepare_acoustic(values, files, max_output_chars=1, guard=allowed)
    assert not files.root.exists()

    def revoked() -> None:
        raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        await prepare_acoustic(values, files, max_output_chars=10000, guard=revoked)
    assert not files.root.exists()
    values["speaker"]["model"] = "relative.onnx"
    with pytest.raises(ValueError, match="绝对路径"):
        await prepare_acoustic(values, files, max_output_chars=10000, guard=allowed)
    assert not files.root.exists()


async def test_changed_snapshot_is_never_overwritten_and_quota_applies(tmp_path: Path) -> None:
    values = models(tmp_path)
    files = LocalFiles(tmp_path / "private", 2_000_000, 20_000_000)
    result = await prepare_acoustic(values, files, max_output_chars=10000, guard=allowed)
    path = Path(result["configuration_path"])
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="未覆盖"):
        await prepare_acoustic(values, files, max_output_chars=10000, guard=allowed)
    assert path.read_bytes() == b"changed"
    bounded = LocalFiles(tmp_path / "bounded", 1, 1)
    with pytest.raises(ValueError):
        await prepare_acoustic(values, bounded, max_output_chars=10000, guard=allowed)
    assert not bounded.root.exists()


@pytest.mark.parametrize(
    ("status", "next_step"),
    [
        ({"enabled": True}, "disable_background_before_setup"),
        ({"closed": True}, "wait_for_service"),
        ({}, "configure_recorder"),
        ({"recorder": {"available": True}}, "configure_wake"),
        (
            {"recorder": {"available": True}, "wake": {"available": True}},
            "configure_speaker_for_owner",
        ),
        (
            {
                "recorder": {"available": True},
                "wake": {"available": True},
                "speaker": {"configured": True, "available": True},
            },
            "voice_setup_enroll",
        ),
        (
            {
                "recorder": {"available": True},
                "wake": {"available": True},
                "speaker": {"configured": True, "available": True, "enrolled": True},
                "asr": True,
            },
            "voice_setup_verify",
        ),
        (
            {
                "recorder": {"available": True},
                "wake": {"available": True},
                "speaker": {"configured": True, "available": True, "enrolled": True},
                "asr": True,
                "setup": {
                    "operation": "verify",
                    "state": "completed",
                    "wake_detected": True,
                    "speaker_verified": True,
                },
            },
            "enable_background_when_requested",
        ),
    ],
)
def test_setup_next_step_is_metadata_only(status: dict[str, Any], next_step: str) -> None:
    result = setup_guidance(status)
    assert result["next_step"] == next_step and result["records_audio"] is False
    assert result["status"] is status
