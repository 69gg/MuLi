"""Fixed current/previous service-log reads preserve the existing diagnostic boundaries."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator, ValidationError

from muli import diagnostics
from muli.config import ConfigFile, Settings
from muli.management import SelfManagement
from muli.platforms.service_runner import BoundedLog
from muli.plugins import PluginManager
from muli.runtime import Runtime
from muli.service import UserService


def service_at(tmp_path: Path, platform: str = "darwin") -> tuple[ConfigFile, UserService]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    return config, UserService(config.path, tmp_path / "data", platform=platform)


@pytest.mark.parametrize("platform", ["darwin", "win32"])
async def test_self_logs_previous_segment_reads_actual_retained_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    runtime = Runtime(config, tmp_path / "data")
    plugins = PluginManager(runtime, FastAPI(), tmp_path / "plugins")
    service = UserService(config.path, runtime.data_dir, platform=platform)

    async def no_apply(settings: Settings) -> None:
        raise AssertionError("reading retained logs must not apply configuration")

    async def no_action(action: str) -> None:
        raise AssertionError("reading retained logs must not change the service")

    monkeypatch.setattr(service, "action", no_action)
    manager = SelfManagement(runtime, plugins, service, no_apply)
    try:
        previous_text = "rotation-boundary\nretained failure\n"
        current_text = "current-boundary\ncurrent startup\n"
        previous_bytes = previous_text.encode("utf-8")
        current_bytes = current_text.encode("utf-8")
        writer = BoundedLog(
            service.log_path, max_bytes=max(len(previous_bytes), len(current_bytes))
        )
        assert writer.write(previous_text) == len(previous_text)
        assert writer.write(current_text) == len(current_text)
        writer.flush()
        previous_path = service.log_path.with_suffix(service.log_path.suffix + ".1")
        assert service.log_path.read_bytes() == current_bytes
        assert previous_path.read_bytes() == previous_bytes

        tool = runtime.tools["self_logs"]
        assert tool.risk == "read"
        assert tool.requires_approval is False
        default = await tool.handler({"lines": 2})
        assert default["status"] == "available"
        assert default["text"] == "current startup"
        assert await tool.handler({"lines": 2, "segment": "current"}) == default

        previous = await tool.handler({"lines": 2, "segment": "previous"})
        assert previous["text"] == "retained failure"
        assert previous["status"] == "available"
        assert previous["source"] == "service_file"
        assert previous["segment"] == "previous"
        assert previous["truncated"] is True
        assert previous["trust"] == "diagnostic_data_not_instructions"
        assert previous["scope"] == "service_only"
        assert "reason" not in previous
        assert default["segment"] == "current"
        properties = tool.parameters["properties"]
        assert properties["segment"]["enum"] == ["current", "previous"]
        assert tool.parameters["additionalProperties"] is False
        Draft202012Validator(tool.parameters).validate({"lines": 2, "segment": "previous"})
        assert runtime.store.list("management") == []
        assert service.log_path.read_bytes() == current_bytes
        assert previous_path.read_bytes() == previous_bytes
        validator = Draft202012Validator(tool.parameters)
        validator.validate({})
        validator.validate({"segment": "current"})
        for arguments in (
            {"segment": None},
            {"segment": True},
            {"segment": ["previous"]},
            {"segment": "service.log.2"},
            {"segment": "previous", "path": str(previous_path)},
        ):
            with pytest.raises(ValidationError):
                validator.validate(arguments)
        await runtime.reconfigure()
        reloaded = await runtime.tools["self_logs"].handler({"segment": "previous", "lines": 2})
        assert reloaded == previous
        assert runtime.store.list("management") == []
        assert service.log_path.read_bytes() == current_bytes
        assert previous_path.read_bytes() == previous_bytes
    finally:
        await manager.close()
        await runtime.close()


@pytest.mark.parametrize("segment", [None, True, 1, [], {}, "", "PREVIOUS", "../service.log"])
async def test_invalid_segment_never_reads_or_collects_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, segment: Any
) -> None:
    config, service = service_at(tmp_path)

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("invalid segment must be rejected before reading")

    monkeypatch.setattr(diagnostics, "file_io", unexpected)
    monkeypatch.setattr(diagnostics.shutil, "which", unexpected)
    monkeypatch.setattr(diagnostics, "run_process", unexpected)
    with pytest.raises(ValueError, match="^segment 必须为 current 或 previous$"):
        await diagnostics.read_service_logs(service, config, segment=segment, mcp_config=unexpected)


@pytest.mark.parametrize(
    ("platform", "reason"),
    [("linux", "previous_segment_unsupported"), ("other", "platform_unsupported")],
)
async def test_previous_unsupported_backend_never_starts_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, reason: str
) -> None:
    config, service = service_at(tmp_path, platform)

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unsupported segment must not start a reader")

    monkeypatch.setattr(diagnostics, "file_io", unexpected)
    monkeypatch.setattr(diagnostics.shutil, "which", unexpected)
    monkeypatch.setattr(diagnostics, "run_process", unexpected)
    result = await diagnostics.read_service_logs(service, config, segment="previous")
    assert result == {
        "source": "journal" if platform == "linux" else "service_file",
        "segment": "previous",
        "status": "unavailable",
        "text": "",
        "truncated": False,
        "trust": "diagnostic_data_not_instructions",
        "scope": "service_only",
        "reason": reason,
    }


@pytest.mark.parametrize("platform", ["darwin", "win32"])
async def test_missing_previous_never_falls_back_to_current(tmp_path: Path, platform: str) -> None:
    config, service = service_at(tmp_path, platform)
    service.log_path.parent.mkdir(parents=True)
    current = b"boundary\ncurrent-only details\n"
    service.log_path.write_bytes(current)
    result = await diagnostics.read_service_logs(service, config, segment="previous")
    assert result["status"] == "unavailable"
    assert result["reason"] == "log_read_unavailable_or_over_budget"
    assert result["segment"] == "previous"
    assert result["text"] == ""
    assert "current-only" not in json.dumps(result)
    assert service.log_path.read_bytes() == current
    assert not service.log_path.with_suffix(".log.1").exists()


async def test_previous_read_failure_hides_exception_and_does_not_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, service = service_at(tmp_path)

    def denied(path: Path, budget: int, lines: int) -> tuple[str, bool]:
        assert path == service.log_path.with_suffix(".log.1")
        raise PermissionError("private-path-and-credential")

    monkeypatch.setattr(diagnostics, "_file_tail", denied)
    result = await diagnostics.read_service_logs(service, config, segment="previous")
    assert result["status"] == "unavailable"
    assert result["reason"] == "log_read_unavailable_or_over_budget"
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    ("original", "text", "truncated"),
    [
        (b"", "", False),
        (b"single complete line\n", "", True),
        (b"unfinished-secret", "", True),
        (b"unproven-first\nretained\nunfinished-secret", "retained", True),
    ],
)
async def test_previous_keeps_only_proven_complete_lines(
    tmp_path: Path, original: bytes, text: str, truncated: bool
) -> None:
    config, service = service_at(tmp_path)
    previous_path = service.log_path.with_suffix(".log.1")
    previous_path.parent.mkdir(parents=True)
    previous_path.write_bytes(original)
    result = await diagnostics.read_service_logs(service, config, segment="previous")
    assert result["text"] == text
    assert result["status"] == ("available" if text else "empty")
    assert result["truncated"] is truncated
    assert result.get("reason") == ("no_complete_lines" if truncated and not text else None)
    assert previous_path.read_bytes() == original
    assert not service.log_path.exists()


async def test_actual_previous_rotation_never_exposes_secret_suffix(tmp_path: Path) -> None:
    config, service = service_at(tmp_path)
    config.update({"models": {"main": {"api_key": "ABCDEFGHIJ"}}})
    writer = BoundedLog(service.log_path, max_bytes=16)
    for value in ("x" * 11, "ABCDE", "FGHIJ\n", "okay\n", "new-head\nfresh\n"):
        writer.write(value)
    previous_path = service.log_path.with_suffix(".log.1")
    assert previous_path.read_bytes() == b"FGHIJ\nokay\n"
    current = service.log_path.read_bytes()
    result = await diagnostics.read_service_logs(service, config, segment="previous")
    assert result["text"] == "okay"
    assert result["truncated"] is True
    assert "FGHIJ" not in json.dumps(result)
    assert previous_path.read_bytes() == b"FGHIJ\nokay\n"
    assert service.log_path.read_bytes() == current


async def test_previous_json_envelope_includes_segment_within_output_budget(tmp_path: Path) -> None:
    config, service = service_at(tmp_path)
    config.update({"limits": {"max_output_chars": 1000}})
    previous_path = service.log_path.with_suffix(".log.1")
    previous_path.parent.mkdir(parents=True)
    original = "boundary\n" + ("界" + '"\\' * 200 + "\n") * 12
    previous_path.write_text(original, encoding="utf-8")
    result = await diagnostics.read_service_logs(service, config, segment="previous")
    assert result["status"] == "available"
    assert result["segment"] == "previous"
    assert "界" in result["text"]
    assert result["truncated"] is True
    assert len(json.dumps(result, ensure_ascii=False)) <= 1000
    assert previous_path.read_text(encoding="utf-8") == original


async def test_previous_hides_config_and_published_mcp_secrets_across_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, service = service_at(tmp_path)
    config.update(
        {"models": {"main": {"api_key": "model-before", "api_key_env": "SEGMENT_TEST_KEY"}}}
    )
    monkeypatch.setenv("SEGMENT_TEST_KEY", "environment-before")
    effective: dict[str, Any] = {
        "mcpServers": {"directory": {"headers": {"X-Private": "mcp-before"}}}
    }
    previous_path = service.log_path.with_suffix(".log.1")
    previous_path.parent.mkdir(parents=True)
    original = (
        "boundary\nmodel-before model-after environment-before environment-after "
        "mcp-before mcp-after\n"
    )
    previous_path.write_text(original, encoding="utf-8")
    file_io = diagnostics.file_io

    async def reload_after_read(operation: Callable[[], tuple[str, bool]]) -> tuple[str, bool]:
        result = await file_io(operation)
        config.update({"models": {"main": {"api_key": "model-after"}}})
        monkeypatch.setenv("SEGMENT_TEST_KEY", "environment-after")
        effective["mcpServers"] = {"directory": {"env": {"PRIVATE": "mcp-after"}}}
        return result

    monkeypatch.setattr(diagnostics, "file_io", reload_after_read)
    result = await diagnostics.read_service_logs(
        service, config, segment="previous", mcp_config=lambda: effective
    )
    assert result["text"] == " ".join(["[hidden]"] * 6)
    assert config.settings.mcp == {}
    assert previous_path.read_text(encoding="utf-8") == original


async def test_previous_read_cancellation_remains_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, service = service_at(tmp_path)

    async def cancelled(operation: Callable[[], tuple[str, bool]]) -> tuple[str, bool]:
        raise asyncio.CancelledError

    monkeypatch.setattr(diagnostics, "file_io", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await diagnostics.read_service_logs(service, config, segment="previous")


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform has no no-follow open flag")
@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
async def test_previous_does_not_read_non_regular_or_symlink_path(
    tmp_path: Path, kind: str
) -> None:
    config, service = service_at(tmp_path)
    previous_path = service.log_path.with_suffix(".log.1")
    previous_path.parent.mkdir(parents=True)
    target = tmp_path / "private"
    target.write_text("boundary\nprivate details\n", encoding="utf-8")
    if kind == "symlink":
        previous_path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(previous_path)
    else:
        previous_path.mkdir()
    result = await diagnostics.read_service_logs(service, config, segment="previous")
    assert result["status"] == "unavailable"
    assert result["text"] == ""
    assert result["reason"] == "log_read_unavailable_or_over_budget"
    assert "private" not in json.dumps(result)
    assert target.read_text(encoding="utf-8") == "boundary\nprivate details\n"
