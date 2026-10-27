"""Optional, one-request local acoustic bridge; never installs or downloads a model."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import math
import os
import stat
import sys
import wave
from array import array
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, redirect_stdout
from itertools import islice
from pathlib import Path
from typing import Any, BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class WakeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tokens: str
    encoder: str
    decoder: str
    joiner: str
    keywords_file: str
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    feature_dim: int = Field(default=80, ge=1, le=256)
    max_active_paths: int = Field(default=4, ge=1, le=8)
    keywords_score: float = Field(default=1.0, gt=0, le=10, allow_inf_nan=False)
    num_trailing_blanks: int = Field(default=1, ge=1, le=20)


class SpeakerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str


class BridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: Literal["cpu", "cuda"] = "cpu"
    wake: WakeModel | None = None
    speaker: SpeakerModel | None = None
    max_audio_bytes: int = Field(default=2_000_000, ge=1024, le=16_000_000)
    max_audio_seconds: int = Field(default=120, ge=1, le=120)
    _root: Path = PrivateAttr(default_factory=Path.cwd)

    def model_path(self, value: str) -> Path:
        if not value.strip() or "://" in value:
            raise ValueError("声学模型必须是非空本地路径")
        path = Path(value).expanduser()
        path = path if path.is_absolute() else self._root / path
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            raise ValueError("声学模型文件须为非空普通文件")
        return path.absolute()


@contextmanager
def _file(path: Path, maximum: int) -> Iterator[BinaryIO]:
    # O_NONBLOCK prevents a replaced FIFO from blocking even before fstat.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError("输入须为限额内普通文件")
        yield source


def load_config(path: Path) -> BridgeConfig:
    with _file(path.expanduser(), 32_768) as source:
        data = source.read(32_769)
    if len(data) > 32_768:
        raise ValueError("声学配置超过 32 KiB")
    config = BridgeConfig.model_validate_json(data)
    config._root = path.expanduser().absolute().parent
    if config.wake is None and config.speaker is None:
        raise ValueError("请配置唤醒模型或声纹模型")
    return config


def _paths(config: BridgeConfig, operation: str) -> dict[str, str]:
    if operation == "wake":
        if config.wake is None:
            raise ValueError("尚未配置唤醒模型")
        return {
            name: str(config.model_path(getattr(config.wake, name)))
            for name in ("tokens", "encoder", "decoder", "joiner", "keywords_file")
        }
    if config.speaker is None:
        raise ValueError("尚未配置声纹模型")
    return {"model": str(config.model_path(config.speaker.model))}


def check_config(path: Path) -> dict[str, Any]:
    """Metadata-only inspection; does not import native code or inspect model weights."""
    return inspect_config(load_config(path))


def inspect_config(config: BridgeConfig) -> dict[str, Any]:
    """Share the same metadata preflight with the owner configuration workflow."""
    if config.wake is not None:
        _paths(config, "wake")
    if config.speaker is not None:
        _paths(config, "verify")
    installed = importlib.util.find_spec("sherpa_onnx") is not None
    return {
        "available": installed,
        "configured_provider": config.provider,
        "wake_configured": config.wake is not None,
        "wake_sample_rate": config.wake.sample_rate if config.wake else None,
        "speaker_configured": config.speaker is not None,
        "runtime_verified": False,
        "reason": "" if installed else "当前解释器未安装 sherpa-onnx；不会自动安装",
    }


def _audio(config: BridgeConfig, value: object, sample_rate: int) -> array[float]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("缺少本地 WAV 路径")
    path = Path(value).expanduser()
    with _file(path, config.max_audio_bytes) as source:
        with wave.open(source, "rb") as audio:
            frames = audio.getnframes()
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getcomptype() != "NONE"
                or audio.getframerate() != sample_rate
                or not 0 < frames <= sample_rate * config.max_audio_seconds
            ):
                raise ValueError("音频须为采样率一致、时长限额内的单声道 PCM16 WAV")
            raw = audio.readframes(frames + 1)
            if len(raw) != frames * 2:
                raise ValueError("音频 WAV 数据不完整")
    pcm = array("h")
    pcm.frombytes(raw)
    if sys.byteorder != "little":
        pcm.byteswap()
    return array("f", (sample / 32768 for sample in pcm))


def _similarity(left: object, right: object) -> float:
    # Native extractor returns a sequence. Reject malformed output rather than
    # converting arbitrary nested arrays or silently shortening mismatched vectors.
    if any(
        not isinstance(vector, Iterable) or isinstance(vector, (str, bytes, dict))
        for vector in (left, right)
    ):
        raise ValueError("声纹向量无效")
    assert isinstance(left, Iterable) and isinstance(right, Iterable)
    try:
        a = [float(value) for value in islice(left, 4097)]
        b = [float(value) for value in islice(right, 4097)]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("声纹向量无效") from exc
    if not a or len(a) > 4096 or len(a) != len(b) or not all(math.isfinite(x) for x in (*a, *b)):
        raise ValueError("声纹向量维度或数值无效")
    # Normalize first to avoid overflow from untrusted/nonstandard engine output.
    norms = math.hypot(*a), math.hypot(*b)
    if any(not math.isfinite(n) or n == 0 for n in norms):
        raise ValueError("声纹向量不能为零或非有限数")
    score = math.fsum(x / norms[0] * (y / norms[1]) for x, y in zip(a, b, strict=True))
    return max(0.0, min(1.0, score))


def detect(config: BridgeConfig, request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation")
    if operation not in ("wake", "verify"):
        raise ValueError("未知声学操作")
    requested = request.get("requested_device", "auto")
    if requested not in ("auto", "cpu", "cuda"):
        raise ValueError("此声学适配器不支持请求的设备（包括 ROCm）")
    if requested != "auto" and requested != config.provider:
        raise ValueError("请求的设备与声学配置 provider 不一致")
    sample_rate, threshold = request.get("sample_rate"), request.get("threshold")
    if type(sample_rate) is not int or not 8000 <= sample_rate <= 48000:
        raise ValueError("声学采样率无效")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 <= threshold <= 1
        or not math.isfinite(threshold)
    ):
        raise ValueError("声学阈值必须为 0 至 1 的有限数")
    paths = _paths(config, operation)
    samples = _audio(config, request.get("audio_path"), sample_rate)
    reference = (
        _audio(config, request.get("reference_path"), sample_rate)
        if operation == "verify"
        else None
    )
    word = request.get("wake_word")
    if operation == "wake" and (
        config.wake is None
        or config.wake.sample_rate != sample_rate
        or not isinstance(word, str)
        or not word.strip()
        or len(word) > 120
    ):
        raise ValueError("唤醒词或模型采样率不匹配")
    # Only the short-lived, already bounded bridge process imports native code.
    try:
        engine = importlib.import_module("sherpa_onnx")
    except ImportError as exc:
        raise ValueError("当前解释器未安装可用的 sherpa-onnx；不会自动安装") from exc
    result: dict[str, Any] = {"configured_provider": config.provider}
    if operation == "wake":
        assert config.wake is not None
        options = config.wake.model_dump(exclude=set(paths))
        detector = engine.KeywordSpotter(
            **paths,
            **options,
            num_threads=1,
            provider=config.provider,
            keywords_threshold=threshold,
        )
        stream = detector.create_stream()
        stream.accept_waveform(sample_rate, samples)
        stream.accept_waveform(sample_rate, array("f", [0.0]) * int(sample_rate * 0.66))
        stream.input_finished()
        detected = False
        while detector.is_ready(stream):
            detector.decode_stream(stream)
            keyword = detector.get_result(stream)
            if keyword:
                detected = detected or keyword == word
                detector.reset_stream(stream)
        result.update(detected=detected, score_kind="decision", threshold=threshold)
    else:
        model = engine.SpeakerEmbeddingExtractorConfig(
            **paths, num_threads=1, debug=False, provider=config.provider
        )
        if not model.validate():
            raise ValueError("声纹模型配置无效")
        extractor = engine.SpeakerEmbeddingExtractor(model)
        embeddings: list[Any] = []
        for waveform in (samples, reference):
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=sample_rate, waveform=waveform)
            stream.input_finished()
            if not extractor.is_ready(stream):
                raise ValueError("音频不足以提取声纹")
            embeddings.append(extractor.compute(stream))
        score = _similarity(*embeddings)
        result.update(verified=score >= threshold, score=score)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="MuLi 可选本地声学桥接", allow_abbrev=False)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="只检查配置，不加载模型或录音")
    args = parser.parse_args()
    try:
        if args.check:
            result = check_config(args.config)
        else:
            raw = sys.stdin.read(16_385)
            if len(raw) > 16_384:
                raise ValueError("声学请求超过限额")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("声学请求必须为 JSON 对象")
            with redirect_stdout(sys.stderr):
                result = detect(load_config(args.config), request)
    except Exception:
        # Engine exceptions can include local paths or model text. Keep protocol
        # stdout empty on failure and avoid echoing input/configuration secrets.
        print("声学桥接失败：请检查本地配置、模型、依赖、设备及 WAV 格式", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
