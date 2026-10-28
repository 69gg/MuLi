"""Prepare private acoustic snapshots; applying settings remains a separate action."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from muli.acoustic_bridge import BridgeConfig, _file, inspect_config
from muli.evidence import EvidenceError, json_size
from muli.platforms.files import LocalFiles


async def prepare_acoustic(
    settings: dict[str, Any],
    files: LocalFiles,
    *,
    max_output_chars: int,
    guard: Callable[[], None],
) -> dict[str, Any]:
    """Validate first, then save one immutable private file under the capture quota."""
    guard()
    config = BridgeConfig.model_validate(settings)
    if config.wake is None and config.speaker is None:
        raise ValueError("请至少配置唤醒或声纹模型")
    values = config.model_dump(mode="json")
    for section, names in (
        ("wake", ("tokens", "encoder", "decoder", "joiner", "keywords_file")),
        ("speaker", ("model",)),
    ):
        model = values[section]
        if model is None:
            continue
        for name in names:
            path = Path(model[name]).expanduser()
            if not path.is_absolute():
                raise ValueError("准备声学配置时模型路径须为明确的绝对路径")
            model[name] = str(path)
    config = BridgeConfig.model_validate(values)
    data = json.dumps(values, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > 32_768:
        raise ValueError("声学配置超过 32 KiB")
    preflight = inspect_config(config)
    digest = hashlib.sha256(data).hexdigest()
    path = files.root.absolute() / f"acoustic-{digest}.json"
    command = [sys.executable, "-m", "muli.acoustic_bridge", "--config", str(path)]
    changes: dict[str, Any] = {"enabled": False, "device": config.provider}
    for name, model in (("wake", config.wake), ("speaker", config.speaker)):
        if model is not None:
            changes[f"{name}_command"] = command
    if config.wake is not None:
        changes["sample_rate"] = config.wake.sample_rate
    result = {
        "prepared": True,
        "applied": False,
        "configuration_path": str(path),
        "revision": digest,
        "preflight": preflight,
        "changes": {"voice": changes},
        "next_tool": "self_configure",
    }
    if json_size(result) > max_output_chars:
        raise EvidenceError("output_budget", "声学配置回执超过工具输出预算，未保存")
    async with files.lock:
        guard()
        if path.is_symlink():
            raise ValueError("声学配置快照不能是符号链接")
        if path.exists():
            with _file(path, 32_768) as source:
                if source.read(32_769) != data:
                    raise ValueError("已有声学配置快照已变化，未覆盖")
            return result
        temporary = files.save(data, ".json")
        try:
            guard()
            # Hard-link creation is atomic and never overwrites an existing
            # snapshot. Both names are in the same private directory/filesystem.
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return result


def setup_guidance(status: dict[str, Any]) -> dict[str, Any]:
    """A next action derived from current evidence, not a claim of acoustic success."""
    if status.get("closed") or status.get("stopping"):
        next_step = "wait_for_service"
    elif status.get("enabled"):
        next_step = "disable_background_before_setup"
    elif not status.get("recorder", {}).get("available"):
        next_step = "configure_recorder"
    elif not status.get("wake", {}).get("available"):
        next_step = "configure_wake"
    elif status.get("speaker", {}).get("configured") and not status["speaker"].get("available"):
        next_step = "configure_speaker"
    elif not status.get("speaker", {}).get("configured"):
        next_step = "configure_speaker_for_owner"
    elif not status["speaker"].get("enrolled"):
        next_step = "voice_setup_enroll"
    elif not status.get("asr"):
        next_step = "configure_asr"
    elif (
        status.get("setup", {}).get("operation") == "verify"
        and status["setup"].get("state") == "completed"
        and status["setup"].get("wake_detected") is True
        and status["setup"].get("speaker_verified") is True
    ):
        next_step = "enable_background_when_requested"
    else:
        next_step = "voice_setup_verify"
    return {"status": status, "next_step": next_step, "records_audio": False}
