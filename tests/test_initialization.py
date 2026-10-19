from __future__ import annotations

import json
from pathlib import Path

import pytest

from muli.auth import verify_password
from muli.initialization import initialize, prompt_settings


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
    assert profile["thinking"] == {}
    assert profile["vision"] is True and profile["reasoning_effort"] == "high"
    assert profile["extra_body"] == {"vendor_budget": 4096}
    assert result["persona"]["user_name"] == "小林"
    assert result["server"]["port"] == 8899


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
