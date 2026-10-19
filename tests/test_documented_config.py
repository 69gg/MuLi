from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from muli.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_configuration_example_uses_the_current_application_schema() -> None:
    settings = Settings.model_validate_json(
        (ROOT / "config.json.example").read_text(encoding="utf-8")
    )
    assert not settings.models.main.configured
    assert not settings.computer.enabled and not settings.computer.capture.enabled
    assert not settings.voice.enabled and not settings.web_voice.enabled
    assert not settings.notifications.enabled
    assert all(
        not getattr(settings.channels, name).enabled
        for name in ("onebot", "telegram", "qq", "weixin")
    )


@pytest.mark.parametrize("name", ["providers", "channels", "voice"])
def test_documented_model_and_channel_examples_use_application_settings(name: str) -> None:
    document = (ROOT / "docs" / f"{name}.md").read_text(encoding="utf-8")
    examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", document, re.S)]
    settings_examples = [
        item
        for item in examples
        if isinstance(item, dict) and {"models", "media", "channels", "web_voice"} & item.keys()
    ]
    assert settings_examples, f"{name} 缺少应用配置示例"
    for example in settings_examples:
        Settings.model_validate(example)


def test_documented_optional_dependencies_are_declared_by_the_distribution() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = set(project["optional-dependencies"])
    document = (ROOT / "docs/channels.md").read_text(encoding="utf-8")
    requirements = re.findall(r"muli-agent\[([a-z,]+)\]", document)
    assert requirements
    for requirement in requirements:
        assert set(requirement.split(",")) <= extras
