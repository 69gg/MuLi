"""Opt-in local acoustic bridges, finite recording and configured ASR/TTS services."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import sys
import time
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from muli import system_dependencies
from muli.config import VoiceSettings
from muli.maintenance import Admission
from muli.media import MediaService
from muli.platforms.files import LocalFiles, artifact
from muli.process import run_process
from muli.voice_health import VOICE_LISTENER_FAILED, VOICE_UNAVAILABLE

VoiceCallback = Callable[[dict[str, Any]], Awaitable[None]]


class Recorder(Protocol):
    def status(self) -> dict[str, Any]: ...

    async def record(self, path: Path, seconds: float) -> None: ...


class AcousticDetector(Protocol):
    def status(self) -> dict[str, Any]: ...

    async def detect(self, path: Path, *, reference: Path | None = None) -> bool: ...


class Player(Protocol):
    def status(self) -> dict[str, Any]: ...

    async def play(self, path: Path, mime_type: str) -> None: ...


class SystemPlayer:
    def __init__(self, *, platform: str | None = None) -> None:
        self.platform = platform or sys.platform

    def _candidates(self, mime_type: str) -> list[str]:
        if self.platform == "linux":
            return ["pw-play", *(["aplay"] if mime_type == "audio/wav" else []), "ffplay"]
        if self.platform == "darwin":
            return ["afplay", "ffplay"]
        if self.platform == "win32":
            return ["ffplay", *(["powershell"] if mime_type == "audio/wav" else [])]
        return []

    def _backend(self, mime_type: str) -> str:
        return next((name for name in self._candidates(mime_type) if shutil.which(name)), "")

    def status(self) -> dict[str, Any]:
        backend = self._backend("audio/wav")
        candidates = self._candidates("audio/wav")
        missing = [] if backend else system_dependencies.missing_tools(candidates)
        return {
            "available": bool(backend),
            "backend": backend,
            "missing_tools": missing,
            "install_hint": None
            if backend or not missing
            else system_dependencies.install_hint(candidates, platform=self.platform),
            "runtime_verified": False,
            "reason": "" if backend else "没有已安装的本地音频播放后端",
        }

    async def play(self, path: Path, mime_type: str) -> None:
        backend = self._backend(mime_type)
        if not backend:
            raise ValueError("没有支持该音频格式的播放后端")
        environment: dict[str, str] | None = None
        if backend == "powershell":
            # No file path is interpolated into executable PowerShell source.
            environment = {**os.environ, "MULI_VOICE_PLAYBACK_FILE": str(path)}
            arguments = [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$p=New-Object System.Media.SoundPlayer;"
                "$p.SoundLocation=$env:MULI_VOICE_PLAYBACK_FILE;$p.PlaySync()",
            ]
        elif backend == "ffplay":
            arguments = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", str(path)]
        else:
            arguments = [backend, *(["-q"] if backend == "aplay" else []), str(path)]
        response = await run_process(arguments, timeout=20, limit=16000, env=environment)
        if response["exit_code"]:
            raise ValueError("音频播放失败，请检查输出设备或权限")


def devices(requested: str) -> dict[str, Any]:
    """Probe device visibility only; do not import ML libraries or allocate a model."""
    visible = {
        "cpu": True,
        "rocm": Path("/dev/kfd").exists() and Path("/dev/dri").exists(),
        "cuda": Path("/dev/nvidiactl").exists() or shutil.which("nvidia-smi") is not None,
    }
    selected = requested
    if requested == "auto":
        selected = next((name for name in ("rocm", "cuda") if visible[name]), "cpu")
    fallback = selected != "cpu" and not visible.get(selected, False)
    if fallback:
        selected = "cpu"
    return {
        "requested": requested,
        "selected": selected,
        "visible": visible,
        "fallback": fallback,
        "runtime_verified": False,
        "reason": "设备不可见，回退 CPU"
        if fallback
        else "仅探测主机设备；声学桥接须自行验证支持并可回退 CPU",
    }


class SystemRecorder:
    def __init__(
        self, config: VoiceSettings, files: LocalFiles, *, platform: str | None = None
    ) -> None:
        self.config, self.files = config, files
        self.platform = platform or sys.platform

    def _candidates(self) -> list[str]:
        return ["arecord", "ffmpeg"] if self.platform == "linux" else ["ffmpeg"]

    def status(self) -> dict[str, Any]:
        backend = ""
        reason = "缺少已安装的 arecord 或 ffmpeg 录音后端"
        if self.platform == "linux" and shutil.which("arecord"):
            backend = "arecord"
        elif self.platform in {"linux", "darwin", "win32"} and shutil.which("ffmpeg"):
            if self.platform != "linux" and not self.config.input_device:
                reason = "此平台需要显式配置 voice.input_device"
            else:
                backend = "ffmpeg"
        candidates = self._candidates()
        missing = [] if backend else system_dependencies.missing_tools(candidates)
        return {
            "available": bool(backend),
            "backend": backend,
            "input_device": self.config.input_device,
            "permission_verified": False,
            "missing_tools": missing,
            "install_hint": None
            if backend or not missing
            else system_dependencies.install_hint(candidates, platform=self.platform),
            "reason": "" if backend else reason,
        }

    async def record(self, path: Path, seconds: float) -> None:
        if not math.isfinite(seconds) or not 0 < seconds <= self.config.max_utterance_seconds:
            raise ValueError("录音时长超过配置限制")
        status = self.status()
        if not status["available"]:
            raise ValueError(status["reason"])
        if status["backend"] == "arecord":
            arguments = [
                "arecord",
                "-q",
                "-D",
                self.config.input_device or "default",
                "-t",
                "wav",
                "-f",
                "S16_LE",
                "-c",
                "1",
                "-r",
                str(self.config.sample_rate),
                "-d",
                str(math.ceil(seconds)),
                str(path),
            ]
        else:
            arguments = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
            if self.platform == "linux":
                arguments += ["-f", "pulse", "-i", self.config.input_device or "default"]
            elif self.platform == "darwin":
                arguments += ["-f", "avfoundation", "-i", "none:" + self.config.input_device]
            else:
                arguments += ["-f", "dshow", "-i", "audio=" + self.config.input_device]
            arguments += [
                "-t",
                str(seconds),
                "-ac",
                "1",
                "-ar",
                str(self.config.sample_rate),
                "-c:a",
                "pcm_s16le",
                "-fs",
                str(self.config.max_file_bytes),
                str(path),
            ]
        await self.files.run_output(run_process, arguments, path, timeout=seconds + 10)


class CommandDetector:
    """A user-provided acoustic model command, receiving paths through JSON, never a shell."""

    def __init__(self, config: VoiceSettings, operation: str) -> None:
        self.config, self.operation = config, operation

    @property
    def command(self) -> list[str]:
        return self.config.wake_command if self.operation == "wake" else self.config.speaker_command

    def status(self) -> dict[str, Any]:
        configured = bool(self.command)
        available = configured and shutil.which(self.command[0]) is not None
        if (
            available
            and len(self.command) == 5
            and self.command[1:4] == ["-m", "muli.acoustic_bridge", "--config"]
            # Keep venv paths: distinct venvs can symlink to the same binary but
            # have different installed packages. Never preflight the wrong one.
            and Path(shutil.which(self.command[0]) or "").absolute()
            == Path(sys.executable).absolute()
        ):
            from muli.acoustic_bridge import check_config

            try:
                report = check_config(Path(self.command[4]), requested_device=self.config.device)
                operation = "wake" if self.operation == "wake" else "speaker"
                available = report["available"] and report[f"{operation}_configured"]
                if (
                    self.config.device != "auto"
                    and self.config.device != report["configured_provider"]
                ):
                    available = False
                    report["reason"] = "声学 provider 与请求设备不一致"
                if (
                    self.operation == "wake"
                    and report["wake_sample_rate"] != self.config.sample_rate
                ):
                    available = False
                    report["reason"] = "录音与唤醒模型的采样率不一致"
                return {
                    **report,
                    "configured": configured,
                    "available": available,
                    "backend": "sherpa-onnx",
                    "reason": (
                        report["reason"] or f"尚未配置{operation}声学模型" if not available else ""
                    ),
                }
            except (OSError, ValueError, ImportError):
                return {
                    "configured": configured,
                    "available": False,
                    "backend": "sherpa-onnx",
                    "runtime_verified": False,
                    "reason": "本地声学配置或模型文件不可用；不会录音或自动下载",
                }
        return {
            "configured": configured,
            "available": available,
            "backend": "command",
            "runtime_verified": False,
            "reason": "" if available else "未配置可执行的声学模型桥接命令",
        }

    async def detect(self, path: Path, *, reference: Path | None = None) -> bool:
        if not self.status()["available"]:
            raise ValueError("声学模型桥接不可用")
        if self.operation == "verify" and reference is None:
            raise ValueError("请先显式录制声纹参考")
        request = {
            "operation": self.operation,
            "audio_path": str(path),
            "sample_rate": self.config.sample_rate,
            "input_device": self.config.input_device,
            "device": devices(self.config.device)["selected"],
            "requested_device": self.config.device,
            "wake_word": self.config.wake_word,
            "reference_path": str(reference) if reference is not None else None,
            "threshold": (
                self.config.wake_threshold
                if self.operation == "wake"
                else self.config.speaker_threshold
            ),
        }
        response = await run_process(
            list(self.command),
            timeout=15,
            limit=16000,
            input_text=json.dumps(request, ensure_ascii=False),
            merge_stderr=False,
        )
        if response["exit_code"]:
            raise ValueError("声学模型桥接运行失败")
        try:
            payload = json.loads(response["output"])
        except ValueError:
            raise ValueError("声学模型桥接须返回 JSON 对象") from None
        flag = "detected" if self.operation == "wake" else "verified"
        if not isinstance(payload, dict) or type(payload.get(flag)) is not bool:
            raise ValueError("声学模型桥接缺少明确布尔结果")
        if payload.get("score_kind") == "decision":
            threshold = payload.get("threshold")
            if (
                self.operation != "wake"
                or isinstance(threshold, bool)
                or not isinstance(threshold, (int, float))
                or threshold != self.config.wake_threshold
                or not math.isfinite(threshold)
            ):
                raise ValueError("唤醒决定须使用当前配置的真实模型阈值")
            return bool(payload[flag])
        if payload.get("score_kind", "confidence") != "confidence":
            raise ValueError("未知声学模型得分类型")
        score = payload.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (float, int))
            or not 0 <= score <= 1
            or not math.isfinite(score)
        ):
            raise ValueError("声学模型桥接置信度须为 0 至 1 的有限数")
        threshold = (
            self.config.wake_threshold
            if self.operation == "wake"
            else self.config.speaker_threshold
        )
        return bool(payload[flag] and score >= threshold)


class VoiceWorker:
    def __init__(
        self,
        config: VoiceSettings,
        data_dir: Path,
        media: MediaService,
        on_text: VoiceCallback,
        *,
        recorder: Recorder | None = None,
        wake: AcousticDetector | None = None,
        speaker: AcousticDetector | None = None,
        player: Player | None = None,
        on_status: VoiceCallback | None = None,
        admission: Admission | None = None,
        on_fault: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config, self.media, self.on_text = config, media, on_text
        self.admission = admission or Admission()
        self.files = LocalFiles(data_dir / "voice", config.max_file_bytes, config.max_total_bytes)
        self.recorder = recorder or SystemRecorder(config, self.files)
        self.wake = wake or CommandDetector(config, "wake")
        self.speaker = speaker or CommandDetector(config, "verify")
        self.player, self.on_status = player or SystemPlayer(), on_status
        self.on_fault = on_fault
        self._health: dict[str, str | None] = {
            "state": "idle",
            "code": None,
            "occurrence_id": None,
        }
        self._task: asyncio.Task[None] | None = None
        self._setup_task: asyncio.Task[dict[str, Any]] | None = None
        self._setup: dict[str, Any] = {"state": "idle"}
        self._operations: set[asyncio.Task[Any]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._stopping = False
        self._stops = 0
        self._closed = False
        self._reply_epoch = 0
        self._reply_changed = asyncio.Event()
        self._capture_lock = asyncio.Lock()
        self._reference: Path | None = self._load_reference()
        self._last_detection = float("-inf")
        self.last_error = ""
        self.last_prompt: dict[str, Any] = {}
        self.last_reply: dict[str, Any] = {}

    @property
    def reply_epoch(self) -> int:
        return self._reply_epoch

    @property
    def reply_changed(self) -> asyncio.Event:
        return self._reply_changed

    def _load_reference(self) -> Path | None:
        try:
            pointer = self.files.root / "enrollment.txt"
            if pointer.is_symlink():
                return None
            identifier = pointer.read_text(encoding="utf-8").strip()
            if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
                return None
            path = self.files.root / (identifier + ".wav")
            if not path.is_symlink() and path.is_file():
                self.files.validate(path)
                return path
        except (OSError, ValueError):
            pass
        return None

    def status(self) -> dict[str, Any]:
        recorder, wake, speaker = self.recorder.status(), self.wake.status(), self.speaker.status()
        asr = self.media.status().get("asr") is True
        available = bool(recorder.get("available") and wake.get("available") and asr)
        if speaker.get("configured") and (not speaker.get("available") or self._reference is None):
            available = False
        available = available and not self._closed and not self._stopping
        return {
            "enabled": self.config.enabled,
            "running": self._task is not None and not self._task.done(),
            "closed": self._closed,
            "stopping": self._stopping,
            "available": available,
            "wake_word": self.config.wake_word,
            "recorder": recorder,
            "wake": wake,
            "speaker": {**speaker, "enrolled": self._reference is not None},
            "asr": asr,
            "tts": self.config.tts_enabled and self.media.status().get("tts") is True,
            "device": devices(self.config.device),
            "last_error": self.last_error,
            "playback": self.player.status(),
            "last_prompt": self.last_prompt,
            "last_reply": dict(self.last_reply),
            "setup": dict(self._setup),
            "health": dict(self._health),
            "reason": "语音服务已关闭"
            if self._closed
            else "语音服务正在停止"
            if self._stopping
            else ""
            if available
            else "需要录音后端、声学唤醒桥接和 ASR；启用声纹时还需参考录音",
        }

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise ValueError("语音服务已关闭")
            if self._stopping:
                raise ValueError("语音服务正在停止")
            if not self.config.enabled:
                return
            if self._task is not None and not self._task.done():
                return
            status = self.status()
            if not status["available"]:
                self.last_error = str(status["reason"])
                self._background_fault(VOICE_UNAVAILABLE)
                return
            self._task = asyncio.create_task(self._loop())

    def _background_current(self) -> bool:
        return (
            self.config.enabled
            and not self._closed
            and not self._stopping
            and self._task is asyncio.current_task()
        )

    def _background_fault(self, code: str) -> None:
        if (
            not self.config.enabled
            or self._closed
            or self._stopping
            or self._health["state"] == "failed"
        ):
            return
        occurrence = uuid4().hex
        self._health = {"state": "failed", "code": code, "occurrence_id": occurrence}
        if self.on_fault is not None:
            try:
                self.on_fault(code, occurrence)
            except Exception:
                # Notification is best effort and cannot change recording/retry behavior.
                pass

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        if self._closed or self._stopping:
            raise ValueError("语音服务已关闭" if self._closed else "语音服务正在停止")
        task = asyncio.current_task()
        assert task is not None
        nested = task in self._operations
        self._operations.add(task)
        try:
            await self.admission.wait()
            if self._closed or self._stopping:
                raise ValueError("语音服务已关闭" if self._closed else "语音服务正在停止")
            with self.admission.operation():
                yield
        finally:
            if not nested:
                self._operations.discard(task)

    async def stop(self) -> None:
        self._reply_epoch += 1
        self._reply_changed.set()
        self._reply_changed = asyncio.Event()
        caller = asyncio.current_task()
        assert caller is not None

        async def settle() -> None:
            async with self._lifecycle_lock:
                self._stops += 1
                self._stopping = True
                tasks = self._operations.copy()
                if self._task is not None:
                    tasks.add(self._task)
                if self._setup_task is not None:
                    tasks.add(self._setup_task)
                self._task = None
                tasks.discard(caller)
                for task in tasks:
                    if task is not self._setup_task or not task.cancelling():
                        task.cancel()
            try:
                # A callback may itself call stop; do not hold its lifecycle lock while joining it.
                await asyncio.gather(*tasks, return_exceptions=True)
                if self._setup_task is not None and self._setup_task.cancelled():
                    self._setup = {**self._setup, "state": "cancelled"}
            finally:
                async with self._lifecycle_lock:
                    self._stops -= 1
                    self._stopping = self._stops > 0

        cleanup = asyncio.create_task(settle())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    async def aclose(self) -> None:
        self._closed = True
        await self.stop()

    async def _record(self, seconds: float, *, _setup: bool = False) -> Path:
        if not self.config.enabled and not (_setup and asyncio.current_task() is self._setup_task):
            raise ValueError("语音采集尚未启用")
        path = self.files.allocate(".wav")
        try:
            await asyncio.wait_for(self.recorder.record(path, seconds), timeout=seconds + 12)
            self.files.validate(path)
            with wave.open(str(path), "rb") as source:
                if source.getnchannels() != 1 or source.getsampwidth() != 2:
                    raise ValueError("录音须为单声道 PCM16 WAV")
                if source.getframerate() != self.config.sample_rate or source.getnframes() <= 0:
                    raise ValueError("录音采样率无效或内容为空")
                if source.getnframes() / source.getframerate() > math.ceil(seconds) + 0.1:
                    raise ValueError("录音超过时长限制")
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def _enrollment_seconds(self, seconds: float) -> None:
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not 1 <= seconds <= min(15, self.config.max_utterance_seconds)
            or not math.isfinite(seconds)
        ):
            raise ValueError("声纹参考时长须为 1 至 15 秒且不超过录音上限")

    def _save_reference(self, path: Path) -> None:
        pointer = self.files.root / "enrollment.txt"
        try:
            if pointer.is_symlink():
                raise ValueError("声纹参考索引不能是符号链接")
            if self.files.used_bytes() + 32 > self.config.max_total_bytes:
                raise ValueError("声纹参考超过本地容量限制")
            pointer.write_text(path.stem, encoding="utf-8")
            pointer.chmod(0o600)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self._reference = path

    async def enroll(self, seconds: float = 5) -> dict[str, Any]:
        self._enrollment_seconds(seconds)
        async with self._operation(), self._capture_lock, self.files.lock:
            path = await self._record(seconds)
            self._save_reference(path)
            return artifact(
                path,
                "audio/wav",
                acoustic_verification_available=self.speaker.status().get("available", False),
            )

    def _setup_current(self) -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        if self._closed or self._stopping:
            raise ValueError("语音服务已关闭或正在停止")
        if self.config.enabled:
            raise ValueError("请先关闭后台语音唤醒再进行设置录音")

    def _setup_ready(self, operation: str) -> None:
        self._setup_current()
        if not self.recorder.status().get("available"):
            raise ValueError("录音后端不可用")
        speaker = self.speaker.status()
        if operation == "enroll":
            if not speaker.get("configured") or not speaker.get("available"):
                raise ValueError("声纹模型尚未就绪")
        else:
            if not self.wake.status().get("available"):
                raise ValueError("唤醒模型尚未就绪")
            if speaker.get("configured"):
                if not speaker.get("available") or self._reference is None:
                    raise ValueError("声纹模型或参考录音尚未就绪")
                self.files.validate(self._reference)

    async def _capture_setup(
        self, operation: str, seconds: float, guard: Callable[[], None] | None
    ) -> dict[str, Any]:
        try:
            async with self._operation(), self._capture_lock, self.files.lock:
                self._setup_ready(operation)
                if guard is not None:
                    guard()
                path = await self._record(seconds, _setup=True)
                retained = False
                try:
                    self._setup_ready(operation)
                    if guard is not None:
                        guard()
                    if operation == "enroll":
                        self._save_reference(path)
                        retained = True
                        result = {"enrolled": True}
                    else:
                        self._setup = {**self._setup, "state": "comparing"}
                        detected = await self.wake.detect(path)
                        self._setup_current()
                        if guard is not None:
                            guard()
                        configured = bool(self.speaker.status().get("configured"))
                        verified = False
                        if configured:
                            verified = await self.speaker.detect(path, reference=self._reference)
                            self._setup_current()
                            if guard is not None:
                                guard()
                        result = {
                            "wake_detected": bool(detected),
                            "speaker_configured": configured,
                            "speaker_verified": bool(verified),
                        }
                    self._setup = {**self._setup, "state": "completed", **result}
                    return dict(self._setup)
                finally:
                    if not retained:
                        path.unlink(missing_ok=True)
        except asyncio.CancelledError:
            self._setup = {**self._setup, "state": "cancelled"}
            raise
        except Exception:
            self._setup = {**self._setup, "state": "error"}
            raise

    async def _run_setup(
        self,
        operation: str,
        seconds: float,
        guard: Callable[[], None] | None,
        identifier: str | None,
    ) -> dict[str, Any]:
        if self._setup_task is not None and not self._setup_task.done():
            raise ValueError("已有语音设置操作正在进行")
        self._enrollment_seconds(seconds)
        self._setup = {
            "id": identifier or str(uuid4()),
            "operation": operation,
            "state": "error",
            "requested_seconds": seconds,
        }
        self._setup_ready(operation)
        if guard is not None:
            guard()
        self._setup = {**self._setup, "state": "recording"}
        task = asyncio.create_task(self._capture_setup(operation, seconds, guard))
        self._setup_task = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._cancel_setup_task(task)
            raise
        finally:
            if self._setup_task is task:
                self._setup_task = None

    async def _cancel_setup_task(self, task: asyncio.Task[dict[str, Any]]) -> bool:
        if not task.cancelling():
            task.cancel()

        async def settle() -> None:
            await asyncio.gather(task, return_exceptions=True)

        cleanup = asyncio.create_task(settle())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if task.cancelled() and self._setup_task is task:
            self._setup = {**self._setup, "state": "cancelled"}
        return cancelled

    async def setup_enroll(
        self,
        seconds: float = 5,
        *,
        guard: Callable[[], None] | None = None,
        identifier: str | None = None,
    ) -> dict[str, Any]:
        """Explicit finite enrollment while background voice remains disabled."""
        return await self._run_setup("enroll", seconds, guard, identifier)

    async def setup_verify(
        self,
        seconds: float = 5,
        *,
        guard: Callable[[], None] | None = None,
        identifier: str | None = None,
    ) -> dict[str, Any]:
        """Return acoustic decisions without ASR, playback or owner submission."""
        return await self._run_setup("verify", seconds, guard, identifier)

    async def cancel_setup(self, identifier: str | None = None) -> dict[str, Any]:
        receipt = dict(self._setup)
        if identifier is not None and receipt.get("id") != identifier:
            raise ValueError("语音设置已变化，未取消其它操作")
        task = self._setup_task
        if task is not None and not task.done():
            if await self._cancel_setup_task(task):
                raise asyncio.CancelledError
            if task.cancelled():
                return {**receipt, "state": "cancelled"}
            if task.exception() is not None:
                return {**receipt, "state": "error"}
            return task.result()
        return receipt

    async def listen_once(self) -> dict[str, Any] | None:
        if not self.config.enabled:
            raise ValueError("语音采集尚未启用")
        if not self.status()["available"]:
            raise ValueError(str(self.status()["reason"]))
        async with self._operation():
            event = await self._listen_captured()
            if event is not None:
                # Reply callbacks may synthesize speech; release the capture/file
                # locks first while keeping the whole operation cancellable.
                await self.on_text(event)
                self.last_error = ""
            return event

    async def _listen_captured(self) -> dict[str, Any] | None:
        async with self._capture_lock, self.files.lock:
            chunk = await self._record(
                min(self.config.chunk_seconds, self.config.max_utterance_seconds)
            )
            utterance: Path | None = None
            try:
                detected = await self.wake.detect(chunk)
                if (
                    not detected
                    or time.monotonic() - self._last_detection < self.config.cooldown_seconds
                ):
                    return None
                self._last_detection = time.monotonic()
                await self._prompt()
                utterance = await self._utterance(chunk)
                verified = False
                if self.speaker.status().get("configured"):
                    verified = await self.speaker.detect(utterance, reference=self._reference)
                    if not verified:
                        return None
                text = await self.media.transcribe(
                    utterance.read_bytes(), utterance.name, "audio/wav", language="zh"
                )
                if not text.strip():
                    return None
                event: dict[str, Any] = {
                    "text": text.strip(),
                    "wake_word": self.config.wake_word,
                    "speaker_verified": verified,
                    "source": "voice",
                }
                return event
            finally:
                chunk.unlink(missing_ok=True)
                if utterance is not None:
                    utterance.unlink(missing_ok=True)

    async def _utterance(self, chunk: Path) -> Path:
        # Preserve the wake chunk: a one-breath "沐璃，..." request must not lose its first words.
        with wave.open(str(chunk), "rb") as source:
            duration = source.getnframes() / source.getframerate()
        remaining = self.config.max_utterance_seconds - math.ceil(duration)
        if remaining <= 0:
            return chunk
        tail = await self._record(remaining)
        combined: Path | None = None
        try:
            combined = self.files.allocate(".wav")
            with wave.open(str(combined), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(self.config.sample_rate)
                for path in (chunk, tail):
                    with wave.open(str(path), "rb") as source:
                        output.writeframes(source.readframes(source.getnframes()))
            self.files.validate(combined)
            return combined
        except BaseException:
            if combined is not None:
                combined.unlink(missing_ok=True)
            raise
        finally:
            tail.unlink(missing_ok=True)

    async def synthesize(self, text: str) -> dict[str, Any]:
        if not self.config.enabled or not self.config.tts_enabled:
            raise ValueError("语音合成尚未启用")
        async with self._operation():
            speech = await self.media.synthesize(text)
            if speech.extension not in {".mp3", ".opus", ".aac", ".flac", ".wav", ".pcm"}:
                raise ValueError("语音合成返回未知文件格式")
            async with self.files.lock:
                path = self.files.save(speech.data, speech.extension)
            return artifact(path, speech.mime_type)

    async def reply(self, text: str, *, admission_guard: Callable[[], None]) -> None:
        """One locally admitted answer; saved audio is never retried implicitly."""
        if not self.config.enabled or not self.config.tts_enabled or not self.config.reply_enabled:
            raise ValueError("自动语音回复尚未启用")
        async with self._operation(), self._capture_lock:
            admission_guard()
            self.last_reply["state"] = "synthesizing"
            speech = await self.media.synthesize(text)
            if speech.extension not in {".mp3", ".opus", ".aac", ".flac", ".wav", ".pcm"}:
                raise ValueError("语音合成返回未知文件格式")
            async with self.files.lock:
                path = self.files.save(speech.data, speech.extension)
                self.last_reply["audio"] = artifact(path, speech.mime_type)
            admission_guard()
            self.last_reply["state"] = "playing"
            await self.player.play(path, speech.mime_type)
            self.last_reply["state"] = "played"
            self.last_error = ""

    async def _prompt(self) -> None:
        played = False
        errors: list[str] = []
        kind = "status"
        path: Path | None = None
        if self.config.tts_enabled and self.media.status().get("tts") is True:
            try:
                speech = await self.media.synthesize("请说")
                if speech.extension not in {".mp3", ".opus", ".aac", ".flac", ".wav", ".pcm"}:
                    raise ValueError("提示语音返回未知格式")
                path = self.files.save(speech.data, speech.extension)
                await self.player.play(path, speech.mime_type)
                played, kind = True, "speech"
            except Exception as exc:
                errors.append(str(exc))
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)
                    path = None
        if not played and self.player.status().get("available"):
            try:
                path = self.files.allocate(".wav")
                samples = min(
                    int(self.config.sample_rate * 0.12), (self.config.max_file_bytes - 44) // 2
                )
                audio = bytearray()
                for index in range(samples):
                    envelope = min(1, index / 80, (samples - index) / 80)
                    sample = int(
                        4000
                        * envelope
                        * math.sin(2 * math.pi * 880 * index / self.config.sample_rate)
                    )
                    audio.extend(sample.to_bytes(2, "little", signed=True))
                with wave.open(str(path), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(self.config.sample_rate)
                    output.writeframes(audio)
                self.files.validate(path)
                await self.player.play(path, "audio/wav")
                played, kind = True, "tone"
            except Exception as exc:
                errors.append(str(exc))
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)
        if not played and not errors:
            errors.append(str(self.player.status().get("reason", "音频播放不可用")))
        self.last_prompt = {
            "type": "voice.prompt",
            "prompt": "请说",
            "played": played,
            "kind": kind,
            "reason": "；".join(errors),
        }
        if self.on_status is not None:
            await self.on_status(self.last_prompt)

    async def _loop(self) -> None:
        while self.config.enabled and self._task is asyncio.current_task():
            try:
                await self.listen_once()
                if self._background_current():
                    self._health = {"state": "ready", "code": None, "occurrence_id": None}
                    self.last_error = ""
                await asyncio.sleep(0)  # Let a maintenance request claim the safe cycle boundary.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                if self._background_current():
                    self._background_fault(VOICE_LISTENER_FAILED)
                # A missing device/model never creates a busy retry loop.
                await asyncio.sleep(max(2, self.config.cooldown_seconds))
