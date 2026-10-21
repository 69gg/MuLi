from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

import muli.skills as module
from muli.config import ConfigFile
from muli.context import pack_context
from muli.providers import Message, ModelClient, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import ContextHook, PreparedRequest, Runtime, ToolProvider
from muli.skills import Skill, SkillLoadError, Skills
from muli.store import Store
from muli.task_control import TaskSnapshot
from muli.tools import Tool


class Host:
    """Exercise the public Skills contract without a model or extension watcher."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = ConfigFile(root / "config.json")
        self.config.settings.skills.directory = str(root / "skills")
        self.store = Store(root / "state.db")
        self.store.put("session", {"id": "main", "kind": "main"})
        self.tool_session_id: str | None = "main"
        self.context_hooks: list[ContextHook] = []
        self.skills_context: ContextHook | None = None
        self.tools: dict[str, Tool] = {}
        self.skills = Skills(cast(Runtime, self))

    def add_tool_provider(self, factory: ToolProvider, *, session_scoped: bool = False) -> None:
        assert session_scoped
        self.tools.update((tool.name, tool) for tool in factory(cast(Runtime, self)))

    def request_context(self, session_id: str) -> PreparedRequest | None:
        return None

    def source(self, name: str = "example", body: str = "完整指导🙂") -> Path:
        path = self.root / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: 按需读取示例\n---\n{body}", encoding="utf-8"
        )
        return path

    async def load(self) -> tuple[Skill, ...]:
        snapshot = await self.skills.prepare(self.config.settings.skills)
        self.skills.publish(snapshot)
        return snapshot.entries

    async def call(self, tool_name: str, **arguments: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self.tools[tool_name].handler(arguments))

    async def activate(self, entry: Skill) -> dict[str, Any]:
        return await self.call("skill_read", name=entry.name, revision=entry.revision)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    if not module._secure_read_supported():
        pytest.skip("This platform lacks the required secure Skills file API")
    monkeypatch.setenv("MULI_HOME", str(tmp_path))
    runtime = Host(tmp_path)
    try:
        yield runtime
    finally:
        runtime.store.close()


def payload(messages: list[Message]) -> dict[str, Any]:
    message = next(item for item in messages if item.content.startswith(module._CONTEXT_PREFIX))
    return cast(dict[str, Any], json.loads(message.content.split("\n", 2)[2]))


async def test_constructor_only_registers_and_catalog_does_not_inject_body(host: Host) -> None:
    path = host.source(body="不应在目录阶段注入的独特正文")
    assert host.skills.status()["count"] == 0
    assert set(host.tools) == {"skill_read", "skill_resource", "skill_forget"}
    assert host.context_hooks == [] and host.skills_context is not None
    assert all(tool.risk == "read" and not tool.requires_approval for tool in host.tools.values())
    (entry,) = await host.load()
    messages = await host.skills.context("main", [Message(role="system", content="主人规则")])
    directory = payload(messages)
    assert directory["catalog"] == [
        {
            "name": "example",
            "description": "按需读取示例",
            "location": str(path),
            "revision": entry.revision,
        }
    ]
    assert directory["active"] == [] and entry.body not in messages[1].content
    assert host.store.get("skill_state", "main") is None


async def test_yaml_compatibility_and_indented_separator_are_not_hand_parsed(host: Host) -> None:
    path = host.source()
    path.write_text(
        "---\nname: example\ndescription: |\n  第一行\n  ---\n  第三行\n"
        "license: MIT\ncompatibility: 需要已有命令\nmetadata: {author: test}\n"
        "allowed-tools: shell\nclaude-only: value\n---\n不执行脚本",
        encoding="utf-8",
    )
    scripts = path.parent / "scripts"
    scripts.mkdir()
    (scripts / "fail.py").write_text("raise RuntimeError('不得自动执行')", encoding="utf-8")
    (entry,) = await host.load()
    assert entry.description == "第一行\n---\n第三行\n"
    assert host.skills.status()["warnings"] == [
        {"source": "example/SKILL.md", "code": "unsupported_metadata_fields"}
    ]
    result = await host.activate(entry)
    assert result["status"] == "activated" and result["delivery"] == "next_model_context"
    assert "content" not in result
    assert payload(await host.skills.context("main", []))["active"][0]["body"] == "不执行脚本"
    assert set(host.tools) == {"skill_read", "skill_resource", "skill_forget"}


async def test_full_guidance_survives_compaction_restart_and_repeated_preparation(
    host: Host,
) -> None:
    host.source(body="必须完整保留的指导" * 20)
    (entry,) = await host.load()
    assert (await host.activate(entry))["status"] == "activated"
    assert (await host.activate(entry))["status"] == "activated"
    messages = [Message(role="system", content="基础规则"), Message(role="user", content="新问题")]
    prepared = await host.skills.context("main", messages)
    assert await host.skills.context("main", prepared) == prepared
    packed = pack_context(prepared, 4000)
    assert payload(packed)["active"][0]["body"] == entry.body
    assert len(payload(packed)["active"]) == 1
    host.store.close()
    host.store = Store(host.root / "state.db")
    restored = Skills(cast(Runtime, host))
    restored.publish(await restored.prepare(host.config.settings.skills))
    assert payload(await restored.context("main", messages))["active"][0]["body"] == entry.body


async def test_half_written_candidate_keeps_accepted_body_and_valid_change_invalidates(
    host: Host,
) -> None:
    path = host.source(body="旧版完整正文")
    (old,) = await host.load()
    await host.activate(old)
    old_status = host.skills.status()
    path.write_text("---\nname: example\ndescription: [未完成秘密", encoding="utf-8")
    with pytest.raises(SkillLoadError) as failure:
        await host.skills.prepare(host.config.settings.skills)
    assert "未完成秘密" not in str(failure.value)
    assert failure.value.source == "example/SKILL.md"
    assert host.skills.status() == old_status
    assert (await host.activate(old))["status"] == "activated"
    assert payload(await host.skills.context("main", []))["active"][0]["body"] == "旧版完整正文"
    host.source(body="新版完整正文")
    candidate = await host.skills.prepare(host.config.settings.skills)
    assert host.skills.status() == old_status
    host.skills.publish(candidate)
    assert await host.activate(old) == {"status": "not_activated", "code": "skill_changed"}
    changed = payload(await host.skills.context("main", []))
    assert changed["active"] == []
    assert changed["changed_requires_reread_or_forget"] == ["example"]
    assert (await host.activate(candidate.entries[0]))["status"] == "activated"
    assert payload(await host.skills.context("main", []))["active"][0]["body"] == "新版完整正文"


async def test_default_directory_empty_and_disabled_sources(host: Host) -> None:
    host.config.settings.skills.directory = ""
    assert await host.load() == ()
    host.source()
    assert len(await host.load()) == 1
    host.config.settings.skills.enabled = False
    host.source().write_text("bad yaml", encoding="utf-8")
    assert await host.load() == ()
    messages = [Message(role="user", content="原输入")]
    assert await host.skills.context("main", messages) == messages


@pytest.mark.parametrize(
    ("header", "code"),
    [
        ("name: example\ndescription: 42", "invalid_metadata_type"),
        ("name: example\ndescription: ' '", "invalid_description"),
        ("name: different\ndescription: 描述", "invalid_name"),
        ("name: example\nname: example\ndescription: 描述", "duplicate_metadata_key"),
        ("name: example\ndescription: &value 描述\nlicense: *value", "yaml_alias_forbidden"),
        ("name: example\ndescription: 描述\nmetadata: {x: 1}", "invalid_metadata_type"),
        ("name: example\ndescription: 描述\nmetadata: {x: a, x: b}", "duplicate_metadata_key"),
        (
            "name: example\ndescription: 描述\nlicense: !!python/object:bad {}",
            "invalid_metadata_type",
        ),
        ("name: example\ndescription: [未完成秘密", "source_invalid"),
    ],
)
async def test_invalid_yaml_rejected_without_source_echo(
    host: Host, header: str, code: str
) -> None:
    path = host.source()
    path.write_text(f"---\n{header}\n---\n原始秘密正文", encoding="utf-8")
    with pytest.raises(SkillLoadError) as failure:
        await host.load()
    assert failure.value.code == code
    assert str(failure.value) == code
    assert failure.value.source == "example/SKILL.md"
    assert host.skills.status()["count"] == 0


@pytest.mark.parametrize(
    ("setting", "limit", "code"),
    [
        ("max_source_bytes", 16, "source_budget"),
        ("max_total_source_bytes", 16, "total_source_budget"),
        ("max_frontmatter_bytes", 16, "frontmatter_budget"),
        ("max_catalog_chars", 16, "catalog_budget"),
        ("max_yaml_depth", 1, "yaml_depth_budget"),
        ("max_yaml_nodes", 3, "yaml_node_budget"),
    ],
)
async def test_source_parser_and_catalog_budgets_are_distinct(
    host: Host, setting: str, limit: int, code: str
) -> None:
    host.source().write_text(
        "---\nname: example\ndescription: 示例\nmetadata: {key: value}\n---\n正文",
        encoding="utf-8",
    )
    settings = host.config.settings.skills.model_copy(update={setting: limit})
    with pytest.raises(SkillLoadError) as failure:
        await host.skills.prepare(settings)
    assert failure.value.code == code


@pytest.mark.parametrize(
    ("setting", "code"), [("max_entries", "scan_budget"), ("max_skills", "skill_count_budget")]
)
async def test_directory_count_budgets(host: Host, setting: str, code: str) -> None:
    host.source("first")
    host.source("second")
    with pytest.raises(SkillLoadError) as failure:
        await host.skills.prepare(host.config.settings.skills.model_copy(update={setting: 1}))
    assert failure.value.code == code


async def test_oversized_full_body_is_not_partially_activated(host: Host) -> None:
    host.source(body='不能截断"\\' * 300)
    (entry,) = await host.load()
    host.config.settings.limits.max_output_chars = 1000
    result = await host.activate(entry)
    assert result == {"status": "not_activated", "code": "skill_too_large"}
    assert host.store.get("skill_state", "main") is None


async def test_active_count_does_not_evict_and_sessions_are_isolated(host: Host) -> None:
    host.config.settings.skills.max_active_skills = 1
    host.source("first")
    host.source("second")
    first, second = await host.load()
    assert (await host.activate(first))["status"] == "activated"
    assert await host.activate(second) == {"status": "not_activated", "code": "active_skill_budget"}
    assert payload(await host.skills.context("main", []))["active"][0]["name"] == "first"
    host.store.put("session", {"id": "worker", "kind": "worker"})
    host.tool_session_id = "worker"
    assert (await host.activate(second))["status"] == "activated"
    assert payload(await host.skills.context("worker", []))["active"][0]["name"] == "second"
    assert await host.call("skill_forget", name="second") == {"status": "cleared"}
    assert host.store.get("skill_state", "worker") is None
    assert host.store.get("skill_state", "main") is not None
    host.tool_session_id = "missing"
    assert await host.activate(first) == {"status": "not_activated", "code": "caller_unavailable"}


async def test_context_budget_rejection_preserves_previous_active_state(host: Host) -> None:
    host.source(body="长" * 3900)
    (entry,) = await host.load()
    host.config.settings.limits.max_context_chars = 4000
    assert await host.activate(entry) == {
        "status": "not_activated",
        "code": "active_context_budget",
    }
    assert host.store.get("skill_state", "main") is None


async def test_changed_budget_can_be_cleared_explicitly_instead_of_silent_eviction(
    host: Host,
) -> None:
    host.source()
    (entry,) = await host.load()
    await host.activate(entry)
    settings = host.config.settings.skills.model_copy(update={"max_active_chars": 1})
    host.skills.publish(await host.skills.prepare(settings))
    suspended = payload(await host.skills.context("main", []))
    assert suspended["guidance_status"] == "suspended" and suspended["active"] == []
    assert host.store.get("skill_state", "main") is not None
    assert await host.call("skill_forget") == {"status": "cleared"}
    assert payload(await host.skills.context("main", []))["active"] == []


@pytest.mark.parametrize(
    "relative", ["../secret", "/etc/passwd", "a/../secret", "./x", "C:/x", "a\\b"]
)
async def test_resource_paths_cannot_escape_package(host: Host, relative: str) -> None:
    host.source()
    (entry,) = await host.load()
    assert await host.call(
        "skill_resource", name=entry.name, revision=entry.revision, path=relative
    ) == {
        "status": "unavailable",
        "code": "resource_path_invalid",
    }


@pytest.mark.skipif(os.name == "nt", reason="Windows 用真实 junction 门控覆盖，不要求 symlink 特权")
async def test_resource_symlinks_and_package_replacement_are_rejected(host: Host) -> None:
    path = host.source()
    (entry,) = await host.load()
    outside = host.root / "outside"
    outside.mkdir()
    (outside / "secret").write_text("外部秘密", encoding="utf-8")
    (path.parent / "link").symlink_to(outside, target_is_directory=True)
    arguments = {"name": entry.name, "revision": entry.revision, "path": "link/secret"}
    assert (await host.call("skill_resource", **arguments))["code"] == "symlink_forbidden"
    path.parent.rename(path.parent.with_name("retired"))
    path.parent.symlink_to(outside, target_is_directory=True)
    arguments["path"] = "secret"
    assert (await host.call("skill_resource", **arguments))["code"] == "symlink_forbidden"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="平台不支持命名管道")
async def test_nonregular_resource_is_rejected_without_opening_pipe(host: Host) -> None:
    path = host.source()
    (entry,) = await host.load()
    os.mkfifo(path.parent / "pipe")
    result = await host.call(
        "skill_resource", name=entry.name, revision=entry.revision, path="pipe"
    )
    assert result == {"status": "unavailable", "code": "source_not_regular"}


async def test_resource_pages_budget_complete_json_and_detect_between_page_edits(
    host: Host,
) -> None:
    path = host.source()
    (entry,) = await host.load()
    text = '多字节🙂"\\\n' * 160
    (path.parent / "reference.txt").write_text(text, encoding="utf-8")
    host.config.settings.limits.max_output_chars = 1000
    arguments: dict[str, Any] = {
        "name": entry.name,
        "revision": entry.revision,
        "path": "reference.txt",
    }
    pieces: list[str] = []
    for _ in range(20):
        page = await host.call("skill_resource", **arguments)
        assert len(json.dumps(page, ensure_ascii=False)) <= 1000
        assert "code" not in page
        pieces.append(page["content"])
        if not page["truncated"]:
            break
        assert page["next_offset"] > page["offset"]
        arguments.update(offset=page["next_offset"], resource_revision=page["revision"])
    else:
        pytest.fail("资源页没有在有限次数内结束")
    assert "".join(pieces) == text
    assert (
        await host.call(
            "skill_resource",
            name=entry.name,
            revision=entry.revision,
            path="reference.txt",
            offset=1,
        )
    )["code"] == "resource_revision_required"
    (path.parent / "reference.txt").write_text("已改变", encoding="utf-8")
    assert (await host.call("skill_resource", **arguments))["code"] == "resource_changed"


async def test_removed_skill_notice_and_explicit_forget_do_not_keep_old_guidance(
    host: Host,
) -> None:
    path = host.source()
    (entry,) = await host.load()
    await host.activate(entry)
    path.unlink()
    await host.load()
    notice = payload(await host.skills.context("main", []))
    assert notice["active"] == [] and notice["changed_requires_reread_or_forget"] == ["example"]
    assert host.store.get("skill_state", "main") is None
    assert await host.skills.context("main", []) == []


@pytest.mark.parametrize("session_id", ["main", "worker"])
@pytest.mark.parametrize("change", ["removed", "changed"])
async def test_observed_invalidation_does_not_reactivate_restored_old_skill(
    host: Host, session_id: str, change: str
) -> None:
    host.store.put("session", {"id": session_id})
    host.tool_session_id = session_id
    path = host.source(body="原始指导")
    host.source("retained", body="继续有效的指导")
    original, retained = await host.load()
    assert (await host.activate(original))["status"] == "activated"
    assert (await host.activate(retained))["status"] == "activated"
    if change == "removed":
        path.unlink()
    else:
        host.source(body="新修订指导")
    await host.load()
    changed = payload(await host.skills.context(session_id, []))
    assert changed["changed_requires_reread_or_forget"] == ["example"]
    assert [item["name"] for item in changed["active"]] == ["retained"]
    host.source(body="原始指导")
    restored, _ = await host.load()
    current = payload(await host.skills.context(session_id, []))
    assert [item["name"] for item in current["active"]] == ["retained"]
    assert (await host.activate(restored))["status"] == "activated"
    assert {
        item["name"] for item in payload(await host.skills.context(session_id, []))["active"]
    } == {
        "example",
        "retained",
    }


@pytest.mark.parametrize("change", ["removed", "changed"])
async def test_unobserved_intermediate_skill_change_keeps_identical_activation(
    host: Host, change: str
) -> None:
    path = host.source(body="原始指导")
    host.source("retained")
    original, _ = await host.load()
    assert (await host.activate(original))["status"] == "activated"
    if change == "removed":
        path.unlink()
    else:
        host.source(body="中间修订")
    await host.load()
    # This session did not prepare context while the activation was invalid.
    host.source(body="原始指导")
    await host.load()
    current = payload(await host.skills.context("main", []))
    assert current["changed_requires_reread_or_forget"] == []
    assert current["active"][0]["revision"] == original.revision
    assert current["active"][0]["body"] == original.body


async def test_invalidated_skill_pruning_keeps_valid_suspended_guidance(host: Host) -> None:
    host.source(body="旧指导")
    host.source("retained", body="有效但暂停的完整指导")
    original, retained = await host.load()
    assert (await host.activate(original))["status"] == "activated"
    assert (await host.activate(retained))["status"] == "activated"
    host.source(body="新指导")
    reduced = host.config.settings.skills.model_copy(update={"max_active_chars": 1})
    host.skills.publish(await host.skills.prepare(reduced))
    suspended = payload(await host.skills.context("main", []))
    assert suspended["guidance_status"] == "suspended" and suspended["active"] == []
    state = host.store.get("skill_state", "main")
    assert state is not None
    assert [item["name"] for item in state["skills"]] == ["retained"]
    assert state["skills"][0]["body"] == retained.body
    await host.load()
    resumed = payload(await host.skills.context("main", []))
    assert resumed["guidance_status"] == "available"
    assert [item["name"] for item in resumed["active"]] == ["retained"]


async def test_fingerprint_tracks_accepted_sources_not_recursive_resources(host: Host) -> None:
    path = host.source()
    fingerprint = await host.skills.fingerprint(host.config.settings.skills)
    assert await host.skills.fingerprint(host.config.settings.skills) == fingerprint
    resources = path.parent / "references"
    resources.mkdir()
    (resources / "large.txt").write_text("资源独立读取", encoding="utf-8")
    assert await host.skills.fingerprint(host.config.settings.skills) == fingerprint
    replacement = path.with_name("next.md")
    replacement.write_text(
        path.read_text(encoding="utf-8").replace("完整指导", "新指导"), encoding="utf-8"
    )
    replacement.replace(path)
    assert await host.skills.fingerprint(host.config.settings.skills) != fingerprint


async def test_prepare_cancellation_is_not_swallowed_or_published(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.source()
    await host.load()
    previous = host.skills.status()

    async def cancelled(operation: Callable[[], Any]) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(module, "file_io", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await host.skills.prepare(host.config.settings.skills)
    assert host.skills.status() == previous


async def test_unsupported_safe_read_does_not_disable_empty_or_disabled_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MULI_HOME", str(tmp_path))
    monkeypatch.setattr(module, "_secure_read_supported", lambda: False)
    runtime = Host(tmp_path)
    try:
        assert await runtime.load() == ()
        assert runtime.skills.status()["secure_file_reads"] is False
        runtime.source()
        with pytest.raises(SkillLoadError, match="secure_read_unavailable"):
            await runtime.load()
        runtime.config.settings.skills.enabled = False
        assert await runtime.load() == ()
    finally:
        runtime.store.close()


@pytest.mark.parametrize("source_read", [False, True])
@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd 门控；Windows 有独立原生句柄竞争回归")
async def test_parent_swap_gate_never_opens_external_file(
    host: Host, monkeypatch: pytest.MonkeyPatch, source_read: bool
) -> None:
    path = host.source(body="内部正文")
    (entry,) = await host.load()
    references = path.parent / "references"
    references.mkdir()
    (references / "note.txt").write_text("内部资料", encoding="utf-8")
    outside = host.root / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("不得读取的外部秘密", encoding="utf-8")
    (outside / "SKILL.md").write_text(
        "---\nname: example\ndescription: 描述\n---\n不得读取的外部秘密", encoding="utf-8"
    )
    original_open = os.open
    gate_name = "example" if source_read else "references"
    victim = path.parent if source_read else references
    opened: list[str] = []
    swapped = False

    def gated_open(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if str(target) == gate_name and dir_fd is not None and not swapped:
            victim.rename(victim.with_name("retired"))
            victim.symlink_to(outside, target_is_directory=True)
            swapped = True
        opened.append(str(target))
        return original_open(target, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(module, "_secure_read_supported", lambda: True)
    monkeypatch.setattr(os, "open", gated_open)
    if source_read:
        with pytest.raises(SkillLoadError):
            await host.skills.prepare(host.config.settings.skills)
        assert "SKILL.md" not in opened
    else:
        result = await host.call(
            "skill_resource", name=entry.name, revision=entry.revision, path="references/note.txt"
        )
        assert result == {"status": "unavailable", "code": "resource_unavailable"}
        assert "note.txt" not in opened
    assert swapped


async def test_resource_generation_rechecked_after_file_io(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = host.source(body="旧代")
    (entry,) = await host.load()
    (path.parent / "note.txt").write_text("旧代资源", encoding="utf-8")
    host.source(body="新代")
    next_snapshot = await host.skills.prepare(host.config.settings.skills)

    async def switched(operation: Callable[[], Any]) -> Any:
        result = operation()
        host.skills.publish(next_snapshot)
        return result

    monkeypatch.setattr(module, "file_io", switched)
    assert await host.call(
        "skill_resource", name=entry.name, revision=entry.revision, path="note.txt"
    ) == {"status": "unavailable", "code": "skill_changed"}


class OfflineModel:
    def __init__(self, turns: list[ModelTurn], seen: list[list[Message]]) -> None:
        self.turns, self.seen = turns, seen

    async def __aenter__(self) -> OfflineModel:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        self.seen.append(messages)
        yield StreamEvent("done", turn=self.turns.pop(0))


def real_runtime(host: Host) -> tuple[Runtime, Skills, list[ModelTurn], list[list[Message]]]:
    turns: list[ModelTurn] = []
    seen: list[list[Message]] = []
    host.config.settings.persona.prompt = "遵守主人指令"
    host.config.settings.models.main.model = "offline-test"
    host.config.settings.models.main.base_url = "https://unused.invalid"
    host.config.settings.compaction.enabled = False
    host.config.settings.limits.max_context_chars = 12000

    def factory(config: ModelConfig) -> ModelClient:
        return cast(ModelClient, OfflineModel(turns, seen))

    runtime = Runtime(host.config, host.root / "runtime", client_factory=factory)
    extensions = getattr(runtime, "extensions", None)
    skills = cast(Skills, extensions.skills) if extensions else Skills(runtime)
    return runtime, skills, turns, seen


def read_turn(entry: Skill) -> ModelTurn:
    return ModelTurn(
        tool_calls=[
            ToolCall(
                "read-skill",
                "skill_read",
                json.dumps({"name": entry.name, "revision": entry.revision}),
            )
        ]
    )


async def test_runtime_prepared_request_delivers_full_body_only_once(host: Host) -> None:
    body = "独特完整指导" + "指" * 2300
    host.source(body=body)
    runtime, skills, turns, seen = real_runtime(host)
    try:
        snapshot = await skills.prepare(host.config.settings.skills)
        skills.publish(snapshot)
        turns.extend([read_turn(snapshot.entries[0]), ModelTurn(content="已读取")])
        host.config.settings.limits.max_context_chars = 6000
        session = runtime.new_session(mode="yolo")
        await runtime.start(session["id"], "读取指导")
        assert await runtime.tasks[session["id"]] == "已读取"
        assert payload(seen[1])["active"][0]["body"] == body
        assert sum(body in item.content for item in seen[1]) == 1
        receipt = json.loads(next(item.content for item in seen[1] if item.role == "tool"))
        assert receipt["delivery"] == "next_model_context" and "content" not in receipt
        assert len(json.dumps([asdict(item) for item in seen[1]], ensure_ascii=False)) <= 6000
    finally:
        await runtime.close()


async def test_runtime_preflight_counts_other_pinned_context(host: Host) -> None:
    host.source(body="指导" * 1000)
    runtime, skills, turns, seen = real_runtime(host)

    async def fixed(session_id: str, messages: list[Message]) -> list[Message]:
        return [*messages, Message(role="system", content="其它固定资料" * 600)]

    runtime.context_hooks.append(fixed)
    try:
        snapshot = await skills.prepare(host.config.settings.skills)
        skills.publish(snapshot)
        turns.extend([read_turn(snapshot.entries[0]), ModelTurn(content="未激活，仍可继续")])
        host.config.settings.limits.max_context_chars = 6000
        session = runtime.new_session(mode="yolo")
        await runtime.start(session["id"], "读取指导")
        assert await runtime.tasks[session["id"]] == "未激活，仍可继续"
        receipt = json.loads(next(item.content for item in seen[1] if item.role == "tool"))
        assert receipt == {"status": "not_activated", "code": "active_context_budget"}
        assert runtime.store.get("skill_state", session["id"]) is None
    finally:
        await runtime.close()


@pytest.mark.parametrize("shrink", ["active", "context"])
async def test_runtime_budget_shrink_suspends_and_can_clear_with_tool(
    host: Host, shrink: str
) -> None:
    body = "必须保持完整" * 750
    host.source(body=body)
    runtime, skills, turns, seen = real_runtime(host)
    try:
        snapshot = await skills.prepare(host.config.settings.skills)
        skills.publish(snapshot)
        turns.extend([read_turn(snapshot.entries[0]), ModelTurn(content="已读取")])
        session = runtime.new_session(mode="yolo")
        await runtime.start(session["id"], "读取指导")
        assert await runtime.tasks[session["id"]] == "已读取"
        if shrink == "active":
            settings = host.config.settings.skills.model_copy(update={"max_active_chars": 1})
            skills.publish(await skills.prepare(settings))
        else:
            host.config.settings.limits.max_context_chars = 4000
        turns.extend(
            [
                ModelTurn(tool_calls=[ToolCall("clear-skill", "skill_forget", "{}")]),
                ModelTurn(content="已清除，可以继续"),
            ]
        )
        await runtime.start(session["id"], "清除已暂停指导后继续")
        assert await runtime.tasks[session["id"]] == "已清除，可以继续"
        suspended = payload(seen[2])
        assert suspended["guidance_status"] == "suspended" and suspended["active"] == []
        assert suspended["suspended_active_count"] == 1
        assert all(body not in item.content for item in seen[2])
        assert runtime.store.get("skill_state", session["id"]) is None
        assert payload(seen[3])["guidance_status"] == "available"
    finally:
        await runtime.close()


async def test_final_decoration_preserves_later_hooks_and_authoritative_task_snapshot(
    host: Host,
) -> None:
    body = "完整 Skill 指导" * 220
    host.source(body=body)
    runtime, skills, _, _ = real_runtime(host)
    ordinary_runs = 0
    fixed_body = "后加入的固定插件资料" * 90

    async def later_plugin(session_id: str, messages: list[Message]) -> list[Message]:
        nonlocal ordinary_runs
        ordinary_runs += 1
        return [*messages, Message(role="system", content=fixed_body)]

    try:
        candidate = await skills.prepare(host.config.settings.skills)
        skills.publish(candidate)
        session_id = runtime.new_session(mode="yolo")["id"]
        initial = await runtime._prepare_request(session_id)
        result = await runtime._tool(
            read_turn(candidate.entries[0]).tool_calls[0],
            runtime.session_tools(session_id),
            "yolo",
            session_id,
            "budget-test",
            prepared=initial,
        )
        assert result["status"] == "activated"
        runtime.context_hooks.append(later_plugin)
        scope = TaskSnapshot(
            session_id,
            "task-evidence",
            1,
            0,
            0,
            None,
            json.dumps({"evidence": "必须完整的权威证据" * 160}, ensure_ascii=False),
            "null",
        )
        host.config.settings.limits.max_context_chars = 6000
        prepared = PreparedRequest(session_id, [], scope)
        token = runtime._request_context.set(prepared)
        try:
            messages = await runtime.prepare_context(
                session_id,
                [Message(role="system", content="基础规则"), Message(role="user", content="继续")],
            )
            assert ordinary_runs == 1
            assert payload(messages)["guidance_status"] == "suspended"
            assert any(item.content == fixed_body for item in messages)
            assert any(scope.context() in item.content for item in messages)
            assert all(body not in item.content for item in messages)
            assert runtime.store.get("skill_state", session_id) is not None
            cleared = await runtime._tool(
                ToolCall("clear", "skill_forget", "{}"),
                runtime.session_tools(session_id),
                "yolo",
                session_id,
                "budget-test",
                prepared=PreparedRequest(session_id, messages, scope),
            )
            assert cleared == {"status": "cleared"}
            resumed = await runtime.prepare_context(
                session_id,
                [Message(role="system", content="基础规则"), Message(role="user", content="继续")],
            )
            assert ordinary_runs == 2
            assert payload(resumed)["guidance_status"] == "available"
            assert any(scope.context() in item.content for item in resumed)
        finally:
            runtime._request_context.reset(token)
    finally:
        await runtime.close()
