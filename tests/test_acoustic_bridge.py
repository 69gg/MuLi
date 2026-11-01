"""Offline acoustic contracts using tiny WAVs and an explicitly substituted engine."""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import struct
import sys
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from muli import acoustic_bridge as bridge

WAKE_FILES = ("tokens", "encoder", "decoder", "joiner", "keywords_file")


def configuration(tmp_path: Path, **changes: Any) -> Path:
    root = tmp_path / "acoustic"
    root.mkdir(exist_ok=True)
    wake = {name: f"{name}.local" for name in WAKE_FILES}
    for name in (*wake.values(), "speaker.local"):
        (root / name).write_bytes(b"offline-placeholder-not-a-model")
    values: dict[str, Any] = {"wake": wake, "speaker": {"model": "speaker.local"}}
    values.update(changes)
    path = root / "config.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def wav(
    path: Path,
    *,
    samples: tuple[int, ...] = (-32768, 0, 16384, 32767),
    rate: int = 16000,
    channels: int = 1,
    width: int = 2,
) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        raw = struct.pack("<" + "h" * len(samples), *samples) if width == 2 else bytes(len(samples))
        output.writeframes(raw * channels)
    return path


def request(path: Path, **changes: Any) -> dict[str, Any]:
    result = {
        "operation": "wake",
        "audio_path": str(path),
        "sample_rate": 16000,
        "input_device": "",
        "device": "cpu",
        "requested_device": "auto",
        "wake_word": "沐璃",
        "reference_path": None,
        "threshold": 0.6,
    }
    return {**result, **changes}


class Stream:
    def __init__(self) -> None:
        self.waveforms: list[tuple[int, array[float]]] = []
        self.finished = False
        self.pending = 1

    def accept_waveform(self, sample_rate: int, waveform: array[float]) -> None:
        assert isinstance(waveform, array) and waveform.typecode == "f"
        self.waveforms.append((sample_rate, waveform))

    def input_finished(self) -> None:
        self.finished = True


def fake_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    keyword: str = "沐璃",
    vectors: tuple[object, object] = ((1.0, 0.0), (0.6, 0.8)),
    speaker_ready: bool = True,
    speaker_valid: bool = True,
) -> dict[str, Any]:
    calls: dict[str, Any] = {
        "imports": [],
        "wake_streams": [],
        "speaker_streams": [],
        "reset": 0,
    }

    class KeywordSpotter:
        def __init__(self, **kwargs: Any) -> None:
            calls["wake_kwargs"] = kwargs

        def create_stream(self) -> Stream:
            stream = Stream()
            calls["wake_streams"].append(stream)
            return stream

        def is_ready(self, stream: Stream) -> bool:
            return stream.pending > 0

        def decode_stream(self, stream: Stream) -> None:
            assert stream.finished
            stream.pending -= 1
            calls["decoded"] = calls.get("decoded", 0) + 1

        def get_result(self, stream: Stream) -> str:
            return keyword

        def reset_stream(self, stream: Stream) -> None:
            calls["reset"] += 1

    class SpeakerConfig:
        def __init__(self, **kwargs: Any) -> None:
            calls["speaker_kwargs"] = kwargs

        def validate(self) -> bool:
            calls["speaker_validated"] = True
            return speaker_valid

    class SpeakerExtractor:
        def __init__(self, config: SpeakerConfig) -> None:
            assert calls["speaker_validated"]
            calls["speaker_created"] = True

        def create_stream(self) -> Stream:
            stream = Stream()
            calls["speaker_streams"].append(stream)
            return stream

        def is_ready(self, stream: Stream) -> bool:
            assert stream.finished
            return speaker_ready

        def compute(self, stream: Stream) -> object:
            assert stream.finished and len(stream.waveforms) == 1
            index = calls.get("computed", 0)
            calls["computed"] = index + 1
            return vectors[index]

    engine = SimpleNamespace(
        KeywordSpotter=KeywordSpotter,
        SpeakerEmbeddingExtractorConfig=SpeakerConfig,
        SpeakerEmbeddingExtractor=SpeakerExtractor,
    )
    original_import = importlib.import_module

    def substituted(name: str, package: str | None = None) -> Any:
        if name == "sherpa_onnx":
            calls["imports"].append(name)
            return engine
        return original_import(name, package)

    monkeypatch.setattr(importlib, "import_module", substituted)
    return calls


def forbid_engine(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    imports: list[str] = []
    original_import = importlib.import_module

    def reject(name: str, package: str | None = None) -> Any:
        if name == "sherpa_onnx":
            imports.append(name)
            raise AssertionError("input gates must run before importing the acoustic engine")
        return original_import(name, package)

    monkeypatch.setattr(importlib, "import_module", reject)
    return imports


def test_wake_uses_relative_models_pcm_float32_and_exact_engine_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = configuration(tmp_path)
    audio = wav(tmp_path / "command.wav")
    elsewhere = tmp_path / "different-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    calls = fake_engine(monkeypatch)
    result = bridge.detect(bridge.load_config(path), request(audio, threshold=0.62))
    assert result == {
        "detected": True,
        "score_kind": "decision",
        "threshold": 0.62,
        "configured_provider": "cpu",
        "provider": "cpu",
    }
    assert "score" not in result
    options = calls["wake_kwargs"]
    assert options["num_threads"] == 1 and options["provider"] == "cpu"
    assert options["keywords_threshold"] == 0.62
    for name in WAKE_FILES:
        assert Path(options[name]) == path.parent / f"{name}.local"
    assert calls["imports"] == ["sherpa_onnx"] and calls["decoded"] == calls["reset"] == 1
    stream = calls["wake_streams"][0]
    assert stream.finished and stream.waveforms[0][0] == 16000
    assert list(stream.waveforms[0][1]) == pytest.approx([-1.0, 0.0, 0.5, 32767 / 32768])


@pytest.mark.parametrize("keyword", ["", "别的词", "沐璃，执行", " 沐璃"])
def test_wake_accepts_only_the_exact_requested_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keyword: str
) -> None:
    config = bridge.load_config(configuration(tmp_path))
    fake_engine(monkeypatch, keyword=keyword)
    result = bridge.detect(config, request(wav(tmp_path / "audio.wav")))
    assert result["detected"] is False and result["score_kind"] == "decision"


def test_auto_uses_configured_cuda_without_claiming_gpu_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bridge.load_config(configuration(tmp_path, provider="cuda"))
    calls = fake_engine(monkeypatch)
    result = bridge.detect(config, request(wav(tmp_path / "audio.wav")))
    assert result["configured_provider"] == "cuda"
    assert calls["wake_kwargs"]["provider"] == "cuda"
    assert "runtime_verified" not in result


def test_rocm_is_rejected_before_engine_and_auto_falls_back_to_cpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bridge.load_config(configuration(tmp_path, provider="rocm"))
    audio = wav(tmp_path / "audio.wav")
    calls = fake_engine(monkeypatch)
    automatic = bridge.detect(config, request(audio))
    assert automatic["configured_provider"] == "rocm" and automatic["provider"] == "cpu"
    assert calls["wake_kwargs"]["provider"] == "cpu"
    imports = forbid_engine(monkeypatch)
    with pytest.raises(ValueError, match="不支持 ROCm"):
        bridge.detect(config, request(audio, requested_device="rocm"))
    assert imports == []


def test_rocm_preflight_reports_unavailable_without_claiming_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bridge.load_config(configuration(tmp_path, provider="rocm"))
    assert config.provider == "rocm"
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: object())
    explicit = bridge.inspect_config(config, requested_device="rocm")
    assert explicit["available"] is False and explicit["provider_unavailable"] is True
    assert explicit["reason_code"] == "provider_unavailable"
    assert explicit["effective_provider"] == "rocm" and explicit["provider_fallback"] is False
    assert explicit["runtime_verified"] is False
    assert "不支持 ROCm" in explicit["reason"]
    automatic = bridge.inspect_config(config)
    assert automatic["available"] is True and automatic["provider_fallback"] is True
    assert automatic["effective_provider"] == "cpu"
    assert automatic["provider_unavailable"] is False
    assert "不支持 ROCm" in automatic["reason"]
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    missing = bridge.inspect_config(config, requested_device="rocm")
    assert missing["available"] is False and missing["provider_unavailable"] is False
    assert "未安装 sherpa-onnx" in missing["reason"]


def test_devices_thresholds_and_request_shape_fail_before_engine_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bridge.load_config(configuration(tmp_path))
    baseline = request(wav(tmp_path / "audio.wav"))
    imports = forbid_engine(monkeypatch)
    invalid: list[dict[str, Any]] = [
        {"operation": "transcribe"},
        {"requested_device": "rocm"},
        {"requested_device": "cuda"},
        {"sample_rate": True},
        {"sample_rate": 48001},
        {"threshold": True},
        {"threshold": float("nan")},
        {"threshold": float("inf")},
        {"threshold": -0.1},
        {"threshold": 1.1},
        {"threshold": None},
        {"wake_word": ""},
        {"wake_word": "词" * 121},
    ]
    for changes in invalid:
        with pytest.raises(ValueError):
            bridge.detect(config, {**baseline, **changes})
    assert imports == []


@pytest.mark.parametrize(
    "damage", ["channels", "width", "rate", "empty", "truncated", "duration", "bytes"]
)
def test_audio_is_complete_bounded_pcm_before_native_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    options: dict[str, Any] = {}
    config_values: dict[str, Any] = {}
    if damage == "channels":
        options["channels"] = 2
    elif damage == "width":
        options["width"] = 1
    elif damage == "rate":
        options["rate"] = 8000
    elif damage == "empty":
        options["samples"] = ()
    elif damage == "duration":
        options["samples"] = (1,) * 16001
        config_values["max_audio_seconds"] = 1
    elif damage == "bytes":
        options["samples"] = (1,) * 1000
        config_values["max_audio_bytes"] = 1024
    audio = wav(tmp_path / "audio.wav", **options)
    if damage == "truncated":
        audio.write_bytes(audio.read_bytes()[:-2])
    config = bridge.load_config(configuration(tmp_path, **config_values))
    imports = forbid_engine(monkeypatch)
    with pytest.raises((ValueError, EOFError, wave.Error)):
        bridge.detect(config, request(audio))
    assert imports == []


def test_both_speaker_recordings_are_validated_before_loading_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bridge.load_config(configuration(tmp_path))
    audio = wav(tmp_path / "audio.wav")
    reference = wav(tmp_path / "reference.wav", channels=2)
    imports = forbid_engine(monkeypatch)
    with pytest.raises(ValueError):
        bridge.detect(config, request(audio, operation="verify", reference_path=str(reference)))
    with pytest.raises(ValueError):
        bridge.detect(config, request(audio, operation="verify", reference_path=None))
    assert imports == []


@pytest.mark.parametrize("threshold,verified", [(0.59, True), (0.61, False)])
def test_speaker_uses_two_real_streams_and_cosine_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, threshold: float, verified: bool
) -> None:
    path = configuration(tmp_path, wake=None)
    audio = wav(tmp_path / "audio.wav")
    reference = wav(tmp_path / "reference.wav", samples=(8192,))
    calls = fake_engine(monkeypatch)
    result = bridge.detect(
        bridge.load_config(path),
        request(audio, operation="verify", reference_path=str(reference), threshold=threshold),
    )
    assert result["verified"] is verified and result["score"] == pytest.approx(0.6)
    assert result["configured_provider"] == "cpu"
    assert calls["speaker_kwargs"]["num_threads"] == 1
    assert calls["speaker_kwargs"]["provider"] == "cpu"
    assert Path(calls["speaker_kwargs"]["model"]) == path.parent / "speaker.local"
    assert calls["computed"] == 2 and len(calls["speaker_streams"]) == 2
    assert all(stream.finished for stream in calls["speaker_streams"])
    assert "wake_kwargs" not in calls


def test_negative_cosine_is_zero_and_invalid_embeddings_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = bridge.load_config(configuration(tmp_path, wake=None))
    audio, reference = wav(tmp_path / "audio.wav"), wav(tmp_path / "reference.wav")
    payload = request(audio, operation="verify", reference_path=str(reference), threshold=0.75)
    fake_engine(monkeypatch, vectors=([1.0, 0.0], [-1.0, 0.0]))
    result = bridge.detect(config, payload)
    assert result["score"] == 0.0 and result["verified"] is False
    invalid: list[tuple[object, object]] = [
        ([], []),
        ([1.0], [1.0, 0.0]),
        ([0.0, 0.0], [1.0, 0.0]),
        ([float("nan")], [1.0]),
        ([float("inf")], [1.0]),
    ]
    for vectors in invalid:
        fake_engine(monkeypatch, vectors=vectors)
        with pytest.raises(ValueError):
            bridge.detect(config, payload)
    for options in ({"speaker_ready": False}, {"speaker_valid": False}):
        fake_engine(monkeypatch, **options)
        with pytest.raises(ValueError):
            bridge.detect(config, payload)


def test_check_is_metadata_only_and_reports_missing_dependency_without_loading_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = configuration(tmp_path)
    imports = forbid_engine(monkeypatch)
    opened: list[Path] = []
    original_open = bridge.os.open

    def config_only(candidate: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(Path(candidate))
        assert Path(candidate) == path, "--check must never read model weights"
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(bridge.os, "open", config_only)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    missing = bridge.check_config(path)
    assert missing["available"] is False and missing["runtime_verified"] is False
    assert missing["wake_configured"] is True and missing["speaker_configured"] is True
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    found = bridge.check_config(path)
    assert found["available"] is True and found["runtime_verified"] is False
    assert imports == [] and opened == [path, path]


def test_config_rejects_missing_sections_invalid_limits_remote_or_missing_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imports = forbid_engine(monkeypatch)
    path = configuration(tmp_path)
    original = json.loads(path.read_text(encoding="utf-8"))
    invalid: tuple[dict[str, Any], ...] = (
        {"provider": "hip"},
        {"provider": True},
        {"wake": None, "speaker": None},
        {"max_audio_bytes": True},
        {"max_audio_seconds": 0},
        {"unknown": True},
    )
    for changes in invalid:
        path.write_text(json.dumps({**original, **changes}), encoding="utf-8")
        with pytest.raises(ValueError):
            bridge.load_config(path)
    for model in ("https://models.invalid/remote.onnx", "missing.local", ""):
        path.write_text(json.dumps({"speaker": {"model": model}}), encoding="utf-8")
        with pytest.raises((ValueError, OSError)):
            bridge.check_config(path)
    path.write_text(" " * 32769, encoding="utf-8")
    with pytest.raises(ValueError):
        bridge.load_config(path)
    assert imports == []


def test_cli_writes_one_json_and_check_never_reads_audio_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = configuration(tmp_path)
    audio = wav(tmp_path / "audio.wav")
    fake_engine(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["acoustic_bridge", "--config", str(path)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request(audio))))
    assert bridge.main() is None
    output = capsys.readouterr()
    assert output.err == "" and json.loads(output.out)["detected"] is True

    class NoInput(io.StringIO):
        def read(self, *args: Any, **kwargs: Any) -> str:
            raise AssertionError("--check must not read request audio or stdin")

    forbid_engine(monkeypatch)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(sys, "argv", ["acoustic_bridge", "--config", str(path), "--check"])
    monkeypatch.setattr(sys, "stdin", NoInput())
    assert bridge.main() is None
    output = capsys.readouterr()
    assert output.err == "" and json.loads(output.out)["available"] is False


def test_cli_failure_keeps_stdout_empty_and_does_not_echo_private_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = configuration(tmp_path)
    forbid_engine(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["acoustic_bridge", "--config", str(path)])
    for content in ("PRIVATE-REQUEST-INVALID-JSON", json.dumps([]), "PRIVATE-LARGE-REQUEST" * 1000):
        monkeypatch.setattr(sys, "stdin", io.StringIO(content))
        with pytest.raises(SystemExit) as stopped:
            bridge.main()
        assert stopped.value.code == 1
        output = capsys.readouterr()
        assert output.out == "" and output.err.strip()
        assert "PRIVATE" not in output.err and str(path) not in output.err
