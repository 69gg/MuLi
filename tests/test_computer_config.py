from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from muli.config import ConfigFile, Settings


@pytest.mark.parametrize("field", ["operation_timeout_seconds", "snapshot_ttl_seconds"])
@pytest.mark.parametrize("value", [0, -1, True, "12", float("inf"), float("nan")])
def test_computer_deadlines_reject_nonfinite_coerced_or_nonpositive_values(
    field: str, value: Any
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"computer": {field: value}})


@pytest.mark.parametrize(
    ("field", "limit"), [("operation_timeout_seconds", 120), ("snapshot_ttl_seconds", 300)]
)
def test_computer_deadlines_remain_bounded(field: str, limit: int) -> None:
    settings = Settings.model_validate({"computer": {field: limit}})
    assert getattr(settings.computer, field) == limit
    with pytest.raises(ValidationError):
        Settings.model_validate({"computer": {field: limit + 1}})


def test_old_computer_configuration_loads_and_new_deadlines_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"computer": {"enabled": False}}), encoding="utf-8")
    config = ConfigFile(path)
    assert config.settings.computer.operation_timeout_seconds == 12
    assert config.settings.computer.snapshot_ttl_seconds == 60
    config.update({"computer": {"operation_timeout_seconds": 2.5, "snapshot_ttl_seconds": 30}})
    restored = ConfigFile(path)
    assert not restored.settings.computer.enabled
    assert restored.settings.computer.operation_timeout_seconds == 2.5
    assert restored.settings.computer.snapshot_ttl_seconds == 30


def test_backend_default_is_platform_selection_and_explicit_old_choice_is_preserved(
    tmp_path: Path,
) -> None:
    assert Settings().computer.backend == "auto"
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"computer": {"backend": "atspi"}}), encoding="utf-8")
    config = ConfigFile(path)
    config.update({"computer": {"max_nodes": 50}})
    assert ConfigFile(path).settings.computer.backend == "atspi"
    config.update({"computer": {"backend": "ax"}})
    assert ConfigFile(path).settings.computer.backend == "ax"
    with pytest.raises(ValidationError):
        config.update({"computer": {"backend": "uia"}})
