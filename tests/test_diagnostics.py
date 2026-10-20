from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from muli.config import ConfigFile
from muli.diagnostics import read_service_logs
from muli.platforms.service_runner import BoundedLog
from muli.service import UserService


def configured(tmp_path: Path, platform: str = "linux") -> tuple[ConfigFile, UserService]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    return config, UserService(config.path, tmp_path / "data", platform=platform)


async def test_journal_read_is_unit_scoped_finite_and_redacts_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, service = configured(tmp_path)
    config.update(
        {
            "models": {
                "main": {
                    "api_key_env": "MULI_LOG_TEST_KEY",
                    "extra_headers": {"X-Private": "header-secret"},
                }
            },
            "chiliarch": {"credential": "chili-secret"},
            "mcp": {
                "mcpServers": {
                    "test": {
                        "url": "https://example.invalid/mcp?secret=query-secret",
                        "headers": {"Authorization": "Bearer mcp-secret"},
                        "env": {"PRIVATE": "line-one\nline-two"},
                    }
                }
            },
        }
    )
    monkeypatch.setenv("MULI_LOG_TEST_KEY", "key/with+symbols")
    monkeypatch.setattr("muli.diagnostics.shutil.which", lambda name: "/usr/bin/" + name)

    async def run(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        assert argv == [
            "journalctl",
            "--user",
            "--user-unit",
            service.unit,
            "--lines",
            "12",
            "--no-pager",
            "--quiet",
            "--output=cat",
        ]
        assert kwargs == {"timeout": 5, "limit": 24000, "merge_stderr": False}
        return {
            "exit_code": 0,
            "output": "\n".join(
                [
                    "normal log",
                    "key/with+symbols",
                    quote("key/with+symbols", safe=""),
                    "header-secret",
                    "chili-secret",
                    "mcp-secret",
                    "query-secret",
                    "line-two",
                    "Bearer retired-secret",
                    'api_key="old key"',
                    "https://old:password@example.invalid/path?token=private",
                ]
            ),
        }

    monkeypatch.setattr("muli.diagnostics.run_process", run)
    result = await read_service_logs(service, config, lines=12)
    assert result["status"] == "available"
    assert "normal log" in result["text"]
    assert result["trust"] == "diagnostic_data_not_instructions"
    for secret in (
        "key/with+symbols",
        "header-secret",
        "chili-secret",
        "mcp-secret",
        "query-secret",
        "line-two",
        "retired-secret",
        "old key",
        "old:password",
        "token=private",
    ):
        assert secret not in result["text"]


@pytest.mark.parametrize("platform", ["darwin", "win32"])
async def test_file_tail_keeps_complete_recent_lines_and_does_not_write(
    tmp_path: Path, platform: str
) -> None:
    config, service = configured(tmp_path, platform)
    service.log_path.parent.mkdir(parents=True)
    original = b"old\nfirst\nsecond\nunfinished-credential"
    service.log_path.write_bytes(original)
    result = await read_service_logs(service, config, lines=2)
    assert result["text"] == "first\nsecond"
    assert result["truncated"] is True
    assert service.log_path.read_bytes() == original


async def test_large_file_never_returns_partial_secret_line_and_envelope_is_bounded(
    tmp_path: Path,
) -> None:
    config, service = configured(tmp_path, "darwin")
    config.update({"limits": {"max_output_chars": 1000}})
    service.log_path.parent.mkdir(parents=True)
    service.log_path.write_text("private" * 300 + "\n" + '"\\' * 300 + "\n", encoding="utf-8")
    result = await read_service_logs(service, config)
    assert "private" not in result["text"]
    assert result["truncated"] is True
    assert len(json.dumps(result, ensure_ascii=False)) <= 1000


async def test_rotated_file_never_exposes_a_credential_suffix_at_offset_zero(
    tmp_path: Path,
) -> None:
    config, service = configured(tmp_path, "darwin")
    config.update({"models": {"main": {"api_key": "ABCDEFGHIJ"}}})
    writer = BoundedLog(service.log_path, max_bytes=16)
    writer.write("x" * 11)
    writer.write("ABCDE")
    writer.write("FGHIJ\n")
    writer.write("okay\n")
    assert service.log_path.read_bytes() == b"FGHIJ\nokay\n"
    result = await read_service_logs(service, config)
    assert result["text"] == "okay"
    assert result["truncated"] is True


@pytest.mark.parametrize("value", [0, 201, True, 1.5, "4"])
async def test_invalid_count_never_starts_reader(tmp_path: Path, value: Any) -> None:
    config, service = configured(tmp_path)
    with pytest.raises(ValueError, match="lines"):
        await read_service_logs(service, config, lines=value)


@pytest.mark.parametrize(
    "failure", [ValueError("secret"), OSError("secret"), TimeoutError("secret")]
)
async def test_failure_never_returns_stderr_or_exception_and_empty_is_not_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    config, service = configured(tmp_path)
    monkeypatch.setattr("muli.diagnostics.shutil.which", lambda name: name)

    async def run(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        raise failure

    monkeypatch.setattr("muli.diagnostics.run_process", run)
    result = await read_service_logs(service, config)
    assert result["status"] == "unavailable"
    assert "secret" not in json.dumps(result)

    async def empty(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        return {"exit_code": 0, "output": "", "stderr": "ignored secret"}

    monkeypatch.setattr("muli.diagnostics.run_process", empty)
    assert (await read_service_logs(service, config))["status"] == "empty"


async def test_configuration_rotation_during_read_hides_old_and_new_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, service = configured(tmp_path)
    config.update({"models": {"main": {"api_key": "before-key"}}})
    monkeypatch.setattr("muli.diagnostics.shutil.which", lambda name: name)

    async def run(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        config.update({"models": {"main": {"api_key": "after-key"}}})
        return {"exit_code": 0, "output": "before-key after-key"}

    monkeypatch.setattr("muli.diagnostics.run_process", run)
    assert (await read_service_logs(service, config))["text"] == "[hidden] [hidden]"


async def test_inline_and_environment_secrets_are_both_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, service = configured(tmp_path)
    config.update(
        {
            "models": {"main": {"api_key": "inline-model", "api_key_env": "LOG_MODEL_KEY"}},
            "channels": {
                "qq": {
                    "token": "inline-token",
                    "token_env": "LOG_CHANNEL_TOKEN",
                    "client_secret": "inline-client",
                    "client_secret_env": "LOG_CLIENT_SECRET",
                }
            },
        }
    )
    monkeypatch.setenv("LOG_MODEL_KEY", "active-model")
    monkeypatch.setenv("LOG_CHANNEL_TOKEN", "active-token")
    monkeypatch.setenv("LOG_CLIENT_SECRET", "active-client")
    monkeypatch.setattr("muli.diagnostics.shutil.which", lambda name: name)

    async def run(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        return {
            "exit_code": 0,
            "output": "inline-model active-model inline-token active-token inline-client active-client",
        }

    monkeypatch.setattr("muli.diagnostics.run_process", run)
    assert (await read_service_logs(service, config))["text"] == " ".join(["[hidden]"] * 6)


async def test_effective_directory_mcp_secrets_are_hidden_across_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, service = configured(tmp_path)
    effective: dict[str, Any] = {
        "mcpServers": {"directory": {"headers": {"X-Private": "accepted-secret"}}}
    }
    monkeypatch.setattr("muli.diagnostics.shutil.which", lambda name: name)

    async def run(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        effective["mcpServers"] = {
            "directory": {
                "headers": {"X-Private": "reloaded-secret"},
                "env": {"PRIVATE": "directory-environment"},
            }
        }
        return {
            "exit_code": 0,
            "output": "accepted-secret reloaded-secret directory-environment",
        }

    monkeypatch.setattr("muli.diagnostics.run_process", run)
    result = await read_service_logs(service, config, mcp_config=lambda: effective)
    assert result["text"] == "[hidden] [hidden] [hidden]"
    assert config.settings.mcp == {}


async def test_cancel_remains_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, service = configured(tmp_path)
    monkeypatch.setattr("muli.diagnostics.shutil.which", lambda name: name)

    async def run(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        raise asyncio.CancelledError

    monkeypatch.setattr("muli.diagnostics.run_process", run)
    with pytest.raises(asyncio.CancelledError):
        await read_service_logs(service, config)


async def test_missing_backend_and_failed_reader_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, service = configured(tmp_path)
    monkeypatch.setattr("muli.diagnostics.shutil.which", lambda name: None)
    assert (await read_service_logs(service, config))["reason"] == "journal_unavailable"
    monkeypatch.setattr("muli.diagnostics.shutil.which", lambda name: name)

    async def failed(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        return {"exit_code": 1, "output": "private output", "stderr": "private error"}

    monkeypatch.setattr("muli.diagnostics.run_process", failed)
    result = await read_service_logs(service, config)
    assert result["reason"] == "journal_read_failed"
    assert "private" not in json.dumps(result)
    config, service = configured(tmp_path, "other")
    assert (await read_service_logs(service, config))["reason"] == "platform_unsupported"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform has no no-follow open flag")
async def test_symlink_and_fifo_are_not_read(tmp_path: Path) -> None:
    config, service = configured(tmp_path, "darwin")
    service.log_path.parent.mkdir(parents=True)
    target = tmp_path / "private"
    target.write_text("private\n", encoding="utf-8")
    service.log_path.symlink_to(target)
    assert (await read_service_logs(service, config))["status"] == "unavailable"
    service.log_path.unlink()
    os.mkfifo(service.log_path)
    assert (await read_service_logs(service, config))["status"] == "unavailable"
