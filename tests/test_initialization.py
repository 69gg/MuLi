from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from muli.auth import verify_password
from muli.initialization import initialize, prompt_settings, start_background
from muli.service import UserService


def test_interactive_settings_preserve_optional_wire_parameters() -> None:
    answers = iter(
        [
            "小林",
            "openai_responses",
            "example-model",
            "https://model.invalid/v1",
            "y",
            "high",
            "{}",
            '{"summary":"auto"}',
            '{"vendor_budget":4096}',
            "0.0.0.0",
            "8899",
        ]
    )
    secrets = iter(["test-api-key", "access-password", "access-password"])
    result = prompt_settings(lambda _: next(answers), lambda _: next(secrets))
    profile = result["models"]["main"]
    assert profile["max_output_tokens"] is None
    assert profile["thinking"] == {}
    assert profile["vision"] is True and profile["reasoning_effort"] == "high"
    assert profile["extra_body"] == {"vendor_budget": 4096}
    assert result["persona"]["user_name"] == "小林"
    assert result["server"]["port"] == 8899


def test_initialize_anthropic_requires_and_saves_explicit_output_limit(tmp_path: Path) -> None:
    answers = iter(
        [
            "用户",
            "anthropic",
            "claude",
            "https://model.invalid/v1",
            "2048",
            "n",
            "",
            "{}",
            "{}",
            "{}",
            "127.0.0.1",
            "8765",
        ]
    )
    prompts: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    path = tmp_path / "config.json"
    settings = initialize(path, tmp_path / "data", read=read, secret=lambda _: "")
    assert settings.models.main.protocol == "anthropic"
    assert settings.models.main.max_output_tokens == 2048
    assert settings.models.reviewer.max_output_tokens is None
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["models"]["main"]["max_output_tokens"] == 2048
    assert sum("max tokens" in prompt for prompt in prompts) == 1


@pytest.mark.parametrize(
    "value", ["", "0", "-1", "+128", "128.5", "1e3", "true", "１２８", "64", "131073"]
)
def test_initialize_anthropic_rejects_invalid_output_limit_without_files(
    tmp_path: Path, value: str
) -> None:
    answers = iter(["用户", "anthropic", "claude", "https://model.invalid/v1", value])
    path = tmp_path / "config.json"
    data = tmp_path / "data"
    with pytest.raises(ValueError, match="max_output_tokens"):
        initialize(path, data, read=lambda _: next(answers), secret=lambda _: "")
    assert not path.exists()
    assert not data.exists()


def test_initialize_never_overwrites_and_creates_private_home(tmp_path: Path) -> None:
    root = tmp_path / "MuLi"
    settings = initialize(root / "config.json", root / "data", defaults=True)
    assert settings.onboarding_pending
    assert not settings.models.main.configured
    assert all((root / name).is_dir() for name in ("data", "plugins", "prompts", "logs"))
    original = (root / "config.json").read_bytes()
    with pytest.raises(ValueError, match="未覆盖"):
        initialize(root / "config.json", root / "data", defaults=True)
    assert (root / "config.json").read_bytes() == original


def test_initialize_hashes_password_before_save(tmp_path: Path) -> None:
    answers = iter(
        [
            "用户",
            "openai_chat",
            "test-model",
            "https://test.invalid/v1",
            "n",
            "",
            "{}",
            "{}",
            "{}",
            "0.0.0.0",
            "8765",
        ]
    )
    secrets = iter(["", "test-password", "test-password"])
    path = tmp_path / "config.json"
    settings = initialize(
        path, tmp_path / "data", read=lambda _: next(answers), secret=lambda _: next(secrets)
    )
    assert settings.models.main.max_output_tokens is None
    assert verify_password("test-password", settings.server.password_hash)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "password" not in data["server"]
    assert "test-password" not in path.read_text(encoding="utf-8")


def test_invalid_parameters_create_no_partial_configuration(tmp_path: Path) -> None:
    answers = iter(["用户", "openai_chat", "test", "https://test.invalid/v1", "n", "", "[]"])
    with pytest.raises(ValueError, match="JSON 对象"):
        initialize(
            tmp_path / "config.json",
            tmp_path / "data",
            read=lambda _: next(answers),
            secret=lambda _: "",
        )
    assert not (tmp_path / "config.json").exists()


@pytest.mark.parametrize("failure", [None, "enable"])
async def test_background_setup_reports_partial_failure_without_losing_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    manager = UserService(tmp_path / "config.json", tmp_path / "data", tmp_path / "units")
    calls: list[str] = []

    async def action(name: str) -> dict[str, Any]:
        calls.append(name)
        if name == failure:
            raise ValueError("模拟平台拒绝")
        if name == "install":
            manager.path.parent.mkdir(parents=True, exist_ok=True)
            manager.path.write_text(manager.render())
        return {"accepted": True}

    async def status() -> dict[str, Any]:
        return {"active": False}

    monkeypatch.setattr(manager, "capability", lambda: {"supported": True})
    monkeypatch.setattr(manager, "action", action)
    monkeypatch.setattr(manager, "status", status)
    result = await start_background(manager)
    if failure is None:
        assert calls == ["install", "enable", "start"]
        assert result["accepted"] and not result["status"]["active"]
    else:
        assert calls == ["install", "enable"]
        assert not result["accepted"] and result["completed"] == ["install"]
