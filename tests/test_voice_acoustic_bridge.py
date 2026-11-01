from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

import muli.acoustic_bridge as bridge
import muli.voice as voice
from muli.config import VoiceSettings
from muli.voice import CommandDetector


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"detected": True, "score_kind": "decision", "threshold": 0.6}, True),
        ({"detected": False, "score_kind": "decision", "threshold": 0.6}, False),
        ({"detected": True, "score": 0.8}, True),
        ({"detected": True, "score": 0.5}, False),
    ],
)
async def test_detector_accepts_matching_wake_decision_without_fabricated_score(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], expected: bool
) -> None:
    requests: list[dict[str, Any]] = []

    async def process(*args: Any, **kwargs: Any) -> dict[str, Any]:
        requests.append(json.loads(kwargs["input_text"]))
        return {"exit_code": 0, "output": json.dumps(payload)}

    monkeypatch.setattr(voice.shutil, "which", lambda _: "/fake/bridge")
    monkeypatch.setattr(voice, "run_process", process)
    detector = CommandDetector(VoiceSettings(wake_command=["bridge"]), "wake")
    assert await detector.detect(Path("fake.wav")) is expected
    assert requests[0]["threshold"] == 0.6


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        ("wake", {"detected": True, "score_kind": "decision", "threshold": 0.5}),
        ("wake", {"detected": True, "score_kind": "decision", "threshold": True}),
        ("wake", {"detected": True, "score_kind": "decision", "threshold": 10**1000}),
        ("wake", {"detected": True, "score_kind": "decision"}),
        ("wake", {"detected": True, "score_kind": "unknown", "score": 1}),
        ("wake", {"detected": True, "score": 10**1000}),
        ("verify", {"verified": True, "score_kind": "decision", "threshold": 0.75}),
    ],
)
async def test_decisions_cannot_bypass_speaker_or_current_threshold(
    monkeypatch: pytest.MonkeyPatch, operation: str, payload: dict[str, Any]
) -> None:
    async def process(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"exit_code": 0, "output": json.dumps(payload)}

    monkeypatch.setattr(voice.shutil, "which", lambda _: "/fake/bridge")
    monkeypatch.setattr(voice, "run_process", process)
    detector = CommandDetector(
        VoiceSettings(wake_command=["bridge"], speaker_command=["bridge"]), operation
    )
    with pytest.raises(ValueError):
        await detector.detect(Path("fake.wav"), reference=Path("reference.wav"))


@pytest.mark.parametrize("reason", ["missing", "dependency", "device", "sample_rate", "speaker"])
async def test_builtin_preflight_rejects_before_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    config_path = tmp_path / "acoustic.json"
    command = [sys.executable, "-m", "muli.acoustic_bridge", "--config", str(config_path)]
    report = {
        "available": reason != "dependency",
        "wake_configured": True,
        "speaker_configured": reason != "speaker",
        "wake_sample_rate": 8000 if reason == "sample_rate" else 16000,
        "configured_provider": "cpu",
        "runtime_verified": False,
        "reason": "missing dependency" if reason == "dependency" else "",
    }

    def check(path: Path, *, requested_device: str = "auto") -> dict[str, Any]:
        assert path == config_path
        assert requested_device == ("rocm" if reason == "device" else "auto")
        if reason == "missing":
            raise FileNotFoundError(path)
        return report

    async def forbidden(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("不可用的内置桥接不能启动进程")

    monkeypatch.setattr(bridge, "check_config", check)
    monkeypatch.setattr(voice, "run_process", forbidden)
    detector = CommandDetector(
        VoiceSettings(
            wake_command=command,
            speaker_command=command,
            device="rocm" if reason == "device" else "auto",
        ),
        "verify" if reason == "speaker" else "wake",
    )
    assert detector.status()["available"] is False
    assert detector.status()["runtime_verified"] is False
    with pytest.raises(ValueError, match="不可用"):
        await detector.detect(Path("fake.wav"), reference=Path("reference.wav"))


def test_preflight_never_confuses_separate_venvs_sharing_a_python_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "venv-a-python", tmp_path / "venv-b-python"
    try:
        first.symlink_to(sys.executable)
        second.symlink_to(sys.executable)
    except OSError:
        pytest.skip("此平台不允许创建测试解释器符号链接")
    assert first.resolve() == second.resolve()

    def forbidden(path: Path) -> dict[str, Any]:
        pytest.fail("不能用当前虚拟环境检查另一个虚拟环境的依赖")

    monkeypatch.setattr(bridge, "check_config", forbidden)
    monkeypatch.setattr(voice.sys, "executable", str(first))
    monkeypatch.setattr(voice.shutil, "which", lambda _: str(second))
    detector = CommandDetector(
        VoiceSettings(
            wake_command=[str(second), "-m", "muli.acoustic_bridge", "--config", "unused.json"]
        ),
        "wake",
    )
    assert detector.status()["backend"] == "command"
    assert detector.status()["runtime_verified"] is False
