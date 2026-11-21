from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from muli.config import ConfigFile, Settings
from muli.server import create_app


async def test_application_startup_fills_nested_defaults_once(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = {
        "models": {
            "main": {
                "api_key": "owner-key",
                "anthropic_prompt_cache": False,
                "max_output_tokens": None,
                "budget": 2048,
                "budget_send_or_not": False,
                "extra_body": {"vendor": {"values": [0, False, None]}},
            }
        },
        "updates": {"check_on_start": False, "interval_hours": 0},
        "disabled_tools": [],
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    config = ConfigFile(path)
    assert json.loads(path.read_bytes()) == original  # Read-only callers do not write.
    app = create_app(config, tmp_path / "data")
    async with app.router.lifespan_context(app):
        written = path.read_bytes()
        data = json.loads(written)
        for key, value in original["models"]["main"].items():
            assert data["models"]["main"][key] == value
        assert data["models"]["main"]["anthropic_adaptive_thinking"] is True
        assert "thinking_budget" not in data["models"]["main"]
        assert "send_thinking_budget" not in data["models"]["main"]
        assert data["updates"]["check_on_start"] is False
        assert data["updates"]["interval_hours"] == 0
        assert data["updates"]["metadata_url"] == Settings().updates.metadata_url
        assert data["disabled_tools"] == []
        assert Settings.model_validate(data) == config.settings
        assert set(data) == set(config.private())
        assert data["models"]["worker"] == Settings().models.worker.model_dump()
        mtime = path.stat().st_mtime_ns
        assert ConfigFile(path).sync_defaults() == 0
        assert path.stat().st_mtime_ns == mtime
        assert path.read_bytes() == written


def test_startup_sync_preserves_conflicting_edits_and_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    assert ConfigFile(path).sync_defaults() == 0
    assert not path.exists()
    path.write_text("{}", encoding="utf-8")
    config = ConfigFile(path)
    changed = b'{"default_mode":"ask"}'
    path.write_bytes(changed)
    with pytest.raises(ValueError, match="configuration_changed"):
        config.sync_defaults()
    assert path.read_bytes() == changed
    path.write_bytes(b"{")
    with pytest.raises(ValidationError):
        ConfigFile(path)
    assert path.read_bytes() == b"{"
