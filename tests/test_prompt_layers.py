"""Prompt layers keep safety and host facts outside user-controlled overrides."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from muli import prompt_layers
from muli.config import Persona
from muli.plugins import PluginContext, PluginManager
from muli.providers import Message


def _build(
    tmp_path: Path,
    persona: Persona,
    *,
    host_facts: str = "HOST_FACTS",
    plugin: str = "PLUGIN_TEXT",
) -> str:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    return prompt_layers.build(
        persona,
        host_facts=host_facts,
        plugin=plugin,
        user_directory=prompts,
    )


def test_default_layers_render_in_fixed_order(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "persona.md").write_text("USER_PERSONA", encoding="utf-8")
    (prompts / "append.md").write_text("USER_APPEND", encoding="utf-8")
    persona = Persona(append="CONFIG_APPEND")
    text = prompt_layers.build(
        persona,
        host_facts="HOST_FACTS",
        plugin="PLUGIN_TEXT",
        user_directory=prompts,
    )

    ordered = [
        "安全边界（宿主固定",
        "先理解用户意图",
        "你是沐璃，生活在用户电脑中的数字生命与本地私人助手",
        "CONFIG_APPEND",
        "USER_PERSONA",
        "USER_APPEND",
        "HOST_FACTS",
        "外部插件资料",
        "PLUGIN_TEXT",
    ]
    positions = [text.index(value) for value in ordered]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    ("scope", "custom", "has_base", "has_persona"),
    [
        ("full", "CUSTOM_PROMPT", False, False),
        ("base", "CUSTOM_PROMPT", False, True),
        ("persona", "CUSTOM_PROMPT", True, False),
    ],
)
def test_prompt_scope_replaces_only_the_selected_layer(
    tmp_path: Path, scope: str, custom: str, has_base: bool, has_persona: bool
) -> None:
    text = _build(
        tmp_path,
        Persona(prompt=custom, prompt_scope=scope, append="APPEND"),
        host_facts="HOST_FACTS",
        plugin="PLUGIN_TEXT",
    )

    assert "安全边界（宿主固定" in text
    assert custom in text
    assert ("先理解用户意图" in text) is has_base
    assert ("你是沐璃，生活在用户电脑中的数字生命与本地私人助手" in text) is has_persona
    assert "APPEND" in text and "HOST_FACTS" in text and "PLUGIN_TEXT" in text


def test_user_directory_never_reads_safety_or_follows_symlinks(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "safety.md").write_text("SHOULD_NOT_LOAD", encoding="utf-8")
    target = tmp_path / "outside.md"
    target.write_text("OUTSIDE", encoding="utf-8")
    (prompts / "persona.md").symlink_to(target)

    with pytest.raises(prompt_layers.PromptLayerError):
        prompt_layers.read_user_prompt(prompts, "persona.md")
    with pytest.raises(prompt_layers.PromptLayerError):
        prompt_layers.read_user_prompt(prompts, "safety.md")
    with pytest.raises(prompt_layers.PromptLayerError):
        _build(tmp_path, Persona())

    (prompts / "persona.md").unlink()
    assert "SHOULD_NOT_LOAD" not in _build(tmp_path, Persona())


def test_user_directory_rejects_invalid_utf8_and_oversized_files(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "persona.md").write_bytes(b"\xff\xfe")
    with pytest.raises(prompt_layers.PromptLayerError, match="UTF-8"):
        prompt_layers.read_user_prompt(prompts, "persona.md")

    (prompts / "persona.md").write_text("x" * 33, encoding="utf-8")
    with pytest.raises(prompt_layers.PromptLayerError, match="字节限制"):
        prompt_layers.read_user_prompt(prompts, "persona.md", max_bytes=32)


def test_plugin_prompt_is_appended_after_host_system_not_prepended(tmp_path: Path) -> None:
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            settings=SimpleNamespace(plugins=SimpleNamespace(hook_timeout=1.0))
        )
    )
    context = PluginContext("demo", tmp_path, runtime, app=None)  # type: ignore[arg-type]
    context.prompt = "PLUGIN_TEXT"
    manager = PluginManager.__new__(PluginManager)
    manager.contexts = [context]
    manager.records = [{"id": "demo"}]
    manager.runtime = runtime  # type: ignore[assignment]

    messages = asyncio.run(
        manager.context("session", [Message(role="system", content="HOST_SYSTEM")])
    )

    assert len(messages) == 1
    assert messages[0].content.startswith("HOST_SYSTEM")
    assert messages[0].content.index("HOST_SYSTEM") < messages[0].content.index("外部插件资料")
    assert "PLUGIN_TEXT" in messages[0].content
