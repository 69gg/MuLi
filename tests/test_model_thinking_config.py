"""Canonical thinking parameters remain compatible with legacy aliases."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from muli.config import ConfigFile, ModelProfile
from muli.providers import Message, ModelConfig
from muli.providers.codecs import build_request
from muli.runtime import Runtime


def _config(
    protocol: str = "openai_chat",
    **kwargs: object,
) -> ModelConfig:
    return ModelConfig(
        protocol,  # type: ignore[arg-type]
        "model",
        "https://model.test/v1",
        max_output_tokens=8192 if protocol == "anthropic" else None,
        stream=False,
        **kwargs,
    )


def test_legacy_aliases_load_without_rewriting_unknown_fields() -> None:
    profile = ModelProfile.model_validate(
        {"budget": 2048, "budget_send_or_not": False}
    )
    assert profile.thinking_budget == 2048
    assert profile.send_thinking_budget is False
    assert profile.thinking_param is None
    assert profile.model_dump()["thinking_budget"] == 2048

    with pytest.raises(ValidationError):
        ModelProfile.model_validate({"budget": 2048, "thinking_budget": 4096})
    with pytest.raises(ValidationError):
        ModelProfile.model_validate({"unknown_budget_alias": 2048})


@pytest.mark.parametrize(
    "changes",
    [
        {"thinking_param": "vendor_budget", "extra_body": {"vendor_budget": 1}},
        {"thinking_param": "vendor.budget", "extra_body": {"vendor": {"budget": 1}}},
        {"thinking_param": "vendor_budget", "thinking": {"type": "adaptive"}},
        {"thinking_param": "vendor_budget", "reasoning": {"effort": "high"}},
    ],
)
def test_conflicting_thinking_parameters_fail_at_load(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelProfile.model_validate(changes)


def test_anthropic_enabled_thinking_requires_budget_at_load() -> None:
    complete = {
        "protocol": "anthropic",
        "model": "claude",
        "base_url": "https://model.test/v1",
        "max_output_tokens": 8192,
    }
    with pytest.raises(ValidationError, match="budget_tokens"):
        ModelProfile.model_validate({**complete, "thinking": {"type": "enabled"}})
    profile = ModelProfile.model_validate(
        {**complete, "thinking": {"type": "enabled", "budget_tokens": 2048}}
    )
    assert profile.thinking["budget_tokens"] == 2048


@pytest.mark.parametrize("path", ["vendor_budget", "vendor.budget_tokens"])
def test_codecs_inject_thinking_budget_at_the_configured_path(path: str) -> None:
    body = build_request(
        _config(thinking_param=path, thinking_budget=2048),
        [Message("user", "q")],
        [],
        stream=False,
    )
    if "." in path:
        assert body["vendor"] == {"budget_tokens": 2048}
    else:
        assert body["vendor_budget"] == 2048


def test_codecs_skip_sending_or_reject_protected_and_missing_values() -> None:
    skipped = build_request(
        _config(
            thinking_param="vendor_budget",
            thinking_budget=2048,
            send_thinking_budget=False,
        ),
        [Message("user", "q")],
        [],
        stream=False,
    )
    assert "vendor_budget" not in skipped

    with pytest.raises(ValueError, match="协议核心字段"):
        build_request(
            _config(thinking_param="messages", thinking_budget=2048),
            [Message("user", "q")],
            [],
            stream=False,
        )
    with pytest.raises(ValueError, match="thinking_budget"):
        build_request(
            _config(thinking_param="vendor_budget"),
            [Message("user", "q")],
            [],
            stream=False,
        )


def test_runtime_passes_canonical_thinking_fields(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "models": {
                "main": {
                    "model": "main",
                    "base_url": "https://model.test/v1",
                    "thinking_param": "vendor_budget",
                    "thinking_budget": 2048,
                    "send_thinking_budget": False,
                }
            }
        }
    )
    runtime = Runtime.__new__(Runtime)
    runtime.config = config  # type: ignore[attr-defined]
    profile = Runtime.model_config(runtime, "main")
    assert profile.thinking_param == "vendor_budget"
    assert profile.thinking_budget == 2048
    assert profile.send_thinking_budget is False
