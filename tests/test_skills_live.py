"""Offline request-boundary publication of Skills without replacing MCP tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NoReturn, cast

import httpx
import pytest
from test_extensions import fragment, server, setup_runtime
from test_extensions_watcher import observe_waits
from test_mcp import FakeClient
from test_skills import Host, payload, read_turn

import muli.skills as skill_module
from muli.config import Settings, SkillsSettings
from muli.extensions import ExtensionError
from muli.providers import Message, ModelClient, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.skills import SkillSnapshot
from muli.tools.mcp import MCPManager


def source(root: Path, name: str = "example", body: str = "旧指导") -> Path:
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: 离线指导\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    if not skill_module._secure_read_supported():
        pytest.skip("Skills 安全文件读取需 dir_fd/O_NOFOLLOW")

    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("Skills publication tests must not use networks or subprocesses")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    source(tmp_path)
    instance = setup_runtime(tmp_path)
    instance.config.settings.extensions.watch_enabled = True
    instance.config.settings.extensions.scan_interval_seconds = 300
    instance.config.settings.compaction.enabled = False
    instance.config.settings.models.main.model = "offline-skills"
    instance.config.settings.models.main.base_url = "https://unused.invalid"
    instance.mcp = MCPManager(instance.config.settings.mcp, client_factory=lambda *_: FakeClient())
    try:
        await instance.extensions.load_initial()
        assert instance.extensions.phase == "loaded"
        yield instance
    finally:
        await instance.close()


@asynccontextmanager
async def busy(runtime: Runtime) -> AsyncIterator[None]:
    async def hold() -> str:
        await asyncio.Event().wait()
        return "unused"

    task = asyncio.create_task(hold())
    runtime.tasks["offline-busy"] = task
    try:
        yield
    finally:
        runtime.tasks.pop("offline-busy")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_busy_request_keeps_materialized_guidance_and_next_request_observes_new_revision(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = runtime.extensions
    old = ext.skills._snapshot.entries[0]
    initial_generation = ext.generation
    seen: list[list[Message]] = []
    entered, release = asyncio.Event(), asyncio.Event()
    mcp_calls: list[str] = []

    class GatedModel:
        async def __aenter__(self) -> GatedModel:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(messages)
            if len(seen) == 1:
                entered.set()
                await release.wait()
                turn = read_turn(old)
            else:
                turn = ModelTurn(content="继续使用新目录")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> ModelClient:
        return cast(ModelClient, GatedModel())

    async def forbidden_mcp(*args: Any, **kwargs: Any) -> NoReturn:
        mcp_calls.append("unexpected")
        raise AssertionError("Skills-only publication must not prepare or retire MCP")

    def forbidden_publish(*args: Any, **kwargs: Any) -> NoReturn:
        mcp_calls.append("publish")
        raise AssertionError("Skills-only publication must not publish MCP")

    runtime.client_factory = factory
    session_id = runtime.new_session(mode="yolo")["id"]
    initial = await runtime._prepare_request(session_id)
    activated = await runtime._tool(
        read_turn(old).tool_calls[0],
        runtime.session_tools(session_id),
        "yolo",
        session_id,
        "offline-activation",
        prepared=initial,
    )
    assert activated["status"] == "activated"
    await runtime.start(session_id, "检查当前指导")
    task = runtime.tasks[session_id]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert runtime.busy and payload(seen[0])["active"][0]["body"] == old.body
        registry, mcp_tools = runtime.tools, runtime.mcp.tools
        with monkeypatch.context() as patch:
            patch.setattr(runtime.mcp, "prepare", forbidden_mcp)
            patch.setattr(runtime.mcp, "publish", forbidden_publish)
            patch.setattr(runtime.mcp, "retire", forbidden_mcp)
            waits = observe_waits(patch)
            source(tmp_path, body="新版完整指导")
            ext.start()
            assert await asyncio.wait_for(waits.get(), 2) == 300
            assert ext.skills._snapshot.entries[0].body == "新版完整指导"
            assert ext.generation == initial_generation + 1 and ext.phase == "loaded"
            assert ext.status()["last_apply"] == {
                "scope": "skills",
                "mcp": "unchanged",
                "configuration": "unchanged",
                "published": True,
                "retirement": "unneeded",
            }
            assert runtime.tools is registry and runtime.mcp.tools is mcp_tools and not mcp_calls
            # Skills publication does not revoke a previously materialized MCP handler.
            assert not (await mcp_tools[0].handler({"q": "仍在同一 MCP 代"}))["is_error"]
            assert payload(seen[0])["active"][0]["body"] == old.body
            assert "新版完整指导" not in json.dumps(payload(seen[0]), ensure_ascii=False)
            release.set()
            assert await asyncio.wait_for(task, 2) == "继续使用新目录"
        receipt = json.loads(next(item.content for item in seen[1] if item.role == "tool"))
        assert receipt == {"status": "not_activated", "code": "skill_changed"}
        next_context = payload(seen[1])
        assert next_context["active"] == []
        assert next_context["changed_requires_reread_or_forget"] == [old.name]
        assert next_context["catalog"][0]["revision"] != old.revision
        assert runtime.store.get("skill_state", session_id) is None
        assert all(old.body not in item.content for item in seen[1])
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_unrelated_skill_publication_keeps_resource_read_object_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path)
    try:
        path = host.source(body="A 完整指导")
        host.source("other", "B 旧指导")
        old, _ = await host.load()
        (path.parent / "note.txt").write_text("A 的资源", encoding="utf-8")
        host.source("other", "B 新指导")
        snapshot = await host.skills.prepare(host.config.settings.skills)

        async def switched(operation: Any) -> Any:
            result = operation()
            host.skills.publish(snapshot)
            return result

        monkeypatch.setattr(skill_module, "file_io", switched)
        result = await host.call(
            "skill_resource", name=old.name, revision=old.revision, path="note.txt"
        )
        assert result.get("content") == "A 的资源"
        assert host.skills._skill(old.name, old.revision) is old
    finally:
        host.store.close()


@pytest.mark.parametrize("change", ["body", "directory"])
async def test_resource_read_rejects_changed_skill_or_replaced_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    host = Host(tmp_path)
    operation: asyncio.Task[dict[str, Any]] | None = None
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        path = host.source(body="原指导")
        (old,) = await host.load()
        (path.parent / "note.txt").write_text("原资源", encoding="utf-8")
        original = skill_module.file_io

        async def reading(read: Callable[[], Any]) -> Any:
            result = await original(read)
            if not entered.is_set():
                entered.set()
                await release.wait()
            return result

        monkeypatch.setattr(skill_module, "file_io", reading)
        operation = asyncio.create_task(
            host.call("skill_resource", name=old.name, revision=old.revision, path="note.txt")
        )
        await asyncio.wait_for(entered.wait(), 2)
        if change == "body":
            host.source(body="真实新指导")
        else:
            path.parent.rename(path.parent.with_name("retired"))
            host.source(body="原指导")
            (path.parent / "note.txt").write_text("新包资源", encoding="utf-8")
            # The retired package is outside the watched root, not a second Skill.
            path.parent.with_name("retired").rename(tmp_path / "retired")
        snapshot = await host.skills.prepare(host.config.settings.skills)
        if change == "directory":
            assert snapshot.entries[0].revision == old.revision
            assert snapshot.entries[0].directory_identity != old.directory_identity

        host.skills.publish(snapshot)
        release.set()
        assert await asyncio.wait_for(operation, 2) == {
            "status": "unavailable",
            "code": "skill_changed",
        }
        assert host.skills._snapshot.entries[0] is not old
    finally:
        release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        host.store.close()


@pytest.mark.parametrize("change", ["manual", "catalog", "mcp", "inline", "skills", "budget"])
async def test_extension_or_explicit_changes_publish_full_generation_while_busy(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    ext = runtime.extensions
    old_snapshot, registry, mcp_tools = ext.skills._snapshot, runtime.tools, runtime.mcp.tools
    generation = ext.generation
    original = ext.apply_locked
    full_calls: list[None] = []

    async def full(target: Settings, *, persist: bool) -> None:
        full_calls.append(None)
        await original(target, persist=persist)

    monkeypatch.setattr(ext, "apply_locked", full)
    waits = observe_waits(monkeypatch)
    source(tmp_path, body="待完整发布")
    if change == "mcp":
        fragment(tmp_path, "new.json", {"mcpServers": {"new": server("new")}})
    elif change == "inline":
        runtime.config.settings.mcp["mcpServers"]["old"]["url"] = "https://example.test/new"
    elif change == "skills":
        runtime.config.settings.skills.max_active_skills += 1
    elif change == "budget":
        runtime.config.settings.extensions.max_mcp_tools += 1
    async with busy(runtime):
        if change == "manual":
            # After observing the directory change, the explicit request is newer.
            ext._observed = await ext.fingerprint(runtime.config.settings)
            ext.request_reload(refresh_catalogs=False)
        elif change == "catalog":
            ext._catalog_changed("old")
        ext.start()
        assert await asyncio.wait_for(waits.get(), 2) == 300
        assert runtime.busy and ext.phase == "loaded" and not ext._dirty
        assert ext.generation == generation + 1 and ext.skills._snapshot is not old_snapshot
        assert ext.skills._snapshot.entries[0].body == "待完整发布"
        assert full_calls == [None] and runtime.tools is not registry
        assert runtime.mcp.tools is not mcp_tools
        assert "scope" not in ext.status()["last_apply"] and not ext._refresh
        denied = await mcp_tools[0].handler({"q": "未派发旧代"})
        assert denied["status"] == "not_executed" and denied["code"] == "generation_changed"


@pytest.mark.parametrize("with_mcp", [True, False], ids=["connected", "no-connections"])
async def test_directory_discovery_cannot_downgrade_an_unfulfilled_manual_reload(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_mcp: bool
) -> None:
    ext = runtime.extensions
    if not with_mcp:
        target = runtime.config.validate_changes({"mcp": {}})
        async with runtime.configuration_lock:
            await ext.apply_locked(target, persist=True)
        assert runtime.mcp.status() == []
        assert await runtime.mcp.retire(tuple(runtime.mcp._retiring))
    snapshot, generation = ext.skills._snapshot, ext.generation
    waits = observe_waits(monkeypatch)
    async with busy(runtime):
        # This request has no refresh set even though it still requires full reload.
        ext.request_reload(refresh_catalogs=False)
        assert not ext._refresh
        source(tmp_path, body="手动完整重载仍未履行")
        ext.start()
        assert await asyncio.wait_for(waits.get(), 2) == 300
        assert runtime.busy and ext.skills._snapshot is not snapshot
        assert ext.skills._snapshot.entries[0].body == "手动完整重载仍未履行"
        assert ext.phase == "loaded" and ext.generation == generation + 1 and not ext._dirty
        assert "scope" not in ext.status()["last_apply"]


@pytest.mark.parametrize("point", ["prepare", "final-scan"])
@pytest.mark.parametrize("arrival", ["manual", "catalog"])
async def test_request_or_mcp_change_during_skills_prepare_is_not_acknowledged(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arrival: str, point: str
) -> None:
    ext = runtime.extensions
    snapshot, generation, registry = ext.skills._snapshot, ext.generation, runtime.tools
    original = ext.skills.prepare
    original_fingerprint = ext.skills.fingerprint
    scans = 0

    def arrive() -> None:
        if arrival == "manual":
            ext.request_reload(refresh_catalogs=False)
        else:
            ext._catalog_changed("old")

    async def prepare(settings: SkillsSettings) -> SkillSnapshot:
        candidate = await original(settings)
        if point == "prepare":
            arrive()
        return candidate

    async def fingerprint(settings: SkillsSettings) -> str:
        nonlocal scans
        result = await original_fingerprint(settings)
        scans += 1
        if point == "final-scan" and scans == 2:
            arrive()
        return result

    monkeypatch.setattr(ext.skills, "prepare", prepare)
    monkeypatch.setattr(ext.skills, "fingerprint", fingerprint)
    waits = observe_waits(monkeypatch)
    source(tmp_path, body="不能过期发布")
    async with busy(runtime):
        ext.start()
        assert await asyncio.wait_for(waits.get(), 2) == 300
        assert ext.skills._snapshot is snapshot and ext.generation == generation
        assert runtime.tools is registry and ext._dirty and ext.phase == "queued"
        assert ext.status()["last_apply"]["published"] is False
        if arrival == "catalog":
            assert ext._refresh == {"old"}
        assert ext._directory_request != ext._requested


async def test_mcp_file_changed_during_prepare_keeps_the_old_generation(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = runtime.extensions
    snapshot, generation = ext.skills._snapshot, ext.generation
    original = ext.skills.prepare

    async def prepare(settings: SkillsSettings) -> SkillSnapshot:
        candidate = await original(settings)
        fragment(tmp_path, "mixed.json", {"mcpServers": {"new": server("new")}})
        return candidate

    monkeypatch.setattr(ext.skills, "prepare", prepare)
    waits = observe_waits(monkeypatch)
    source(tmp_path, body="混合来源不能局部确认")
    async with busy(runtime):
        ext.start()
        assert await asyncio.wait_for(waits.get(), 2) == 300
        assert ext.skills._snapshot is snapshot and ext.generation == generation
        assert ext._dirty and ext.phase == "queued"


@pytest.mark.parametrize("gate", ["configuration", "maintenance", "format", "closing", "source"])
async def test_final_await_rechecks_all_publication_gates(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    ext = runtime.extensions
    snapshot, generation, registry = ext.skills._snapshot, ext.generation, runtime.tools
    original = ext.skills.fingerprint
    calls = 0

    def reject_configuration() -> NoReturn:
        raise ExtensionError("transaction_in_progress")

    async def fingerprint(settings: SkillsSettings) -> str:
        nonlocal calls
        result = await original(settings)
        calls += 1
        # The first call is discovery; the second is the last candidate await.
        if calls == 2:
            if gate == "configuration":
                runtime.config.settings.limits.max_output_chars += 1
            elif gate == "maintenance":
                monkeypatch.setattr(
                    runtime.maintenance, "require_configuration", reject_configuration
                )
            elif gate == "format":
                assert runtime.store.format_state is not None
                with runtime.store.db:
                    runtime.store.db.execute(
                        "DELETE FROM runtime_state WHERE name='format_state_v1'"
                    )
            elif gate == "closing":
                runtime._closing = True
            else:
                return "changed-after-prepare"
        return result

    monkeypatch.setattr(ext.skills, "fingerprint", fingerprint)
    waits = observe_waits(monkeypatch)
    source(tmp_path, body="必须最后核验")
    async with busy(runtime):
        try:
            ext.start()
            if gate == "closing":
                assert ext._worker is not None
                await asyncio.wait_for(ext._worker, 2)
            else:
                assert await asyncio.wait_for(waits.get(), 2) == 300
            assert calls == 2 and ext.skills._snapshot is snapshot
            assert ext.generation == generation and runtime.tools is registry
            assert ext.status()["last_apply"]["published"] is False
            assert "PRIVATE_" not in json.dumps(ext.status())
            if gate == "maintenance":
                assert ext._dirty and ext.phase == "queued"
            if gate == "source":
                assert ext.status()["error"] == {"code": "source_changed", "source": "skills"}
        finally:
            runtime._closing = False


async def test_failed_candidate_keeps_old_skills_and_notifies_once_without_busy_retry(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = runtime.extensions
    snapshot, generation = ext.skills._snapshot, ext.generation
    notices: list[None] = []
    calls = 0
    original = ext.skills.prepare

    async def prepare(settings: SkillsSettings) -> SkillSnapshot:
        nonlocal calls
        calls += 1
        return await original(settings)

    monkeypatch.setattr(ext.skills, "prepare", prepare)
    monkeypatch.setattr(
        runtime.system_events, "extension_watch_failed", lambda: notices.append(None)
    )
    waits = observe_waits(monkeypatch)
    source(tmp_path).write_text("---\nname: [未写完 PRIVATE_SECRET", encoding="utf-8")
    async with busy(runtime):
        ext.start()
        for _ in range(3):
            assert await asyncio.wait_for(waits.get(), 2) == 300
            assert ext.skills._snapshot is snapshot and ext.generation == generation
            assert ext.phase == "failed" and not ext._dirty
            assert notices == [None] and calls == 1
            ext.wake()
        assert "PRIVATE_SECRET" not in json.dumps(ext.status())
        source(tmp_path, body="随后有效指导")
        ext.wake()
        assert await asyncio.wait_for(waits.get(), 2) == 300
        assert ext.phase == "loaded" and ext.skills._snapshot.entries[0].body == "随后有效指导"


@pytest.mark.parametrize("initial", [True, False], ids=["initial", "later-full"])
async def test_failed_full_mcp_candidate_does_not_gain_loaded_status_from_skills_only(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial: bool
) -> None:
    ext = runtime.extensions
    if initial:
        # load_initial still serves its best-effort core/Skills result.
        await runtime.mcp.close()
        runtime.mcp = MCPManager(
            runtime.config.settings.mcp, client_factory=lambda *_: FakeClient(failure=True)
        )
        await ext.load_initial()
    else:

        async def failed(*args: Any, **kwargs: Any) -> NoReturn:
            raise ExtensionError("connection_failed", "old")

        monkeypatch.setattr(runtime.mcp, "prepare", failed)
        async with runtime.configuration_lock:
            with pytest.raises(ExtensionError, match="connection_failed"):
                await ext.apply_locked(runtime.config.settings, persist=False)
    snapshot, generation = ext.skills._snapshot, ext.generation
    waits = observe_waits(monkeypatch)
    source(tmp_path, body="不能掩盖 MCP 失败")
    async with busy(runtime):
        ext.start()
        assert await asyncio.wait_for(waits.get(), 2) == 300
        assert ext.skills._snapshot is snapshot and ext.generation == generation
        assert not ext._dirty and ext.phase == "failed"
        assert ext.status()["error"] is not None


async def test_cancelled_skills_preparation_keeps_owned_old_generation(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = runtime.extensions
    snapshot, generation, registry = ext.skills._snapshot, ext.generation, runtime.tools
    entered = asyncio.Event()

    async def prepare(settings: SkillsSettings) -> SkillSnapshot:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled preparation cannot finish")

    monkeypatch.setattr(ext.skills, "prepare", prepare)
    source(tmp_path, body="取消的候选")
    async with busy(runtime):
        ext.start()
        await asyncio.wait_for(entered.wait(), 2)
        assert ext._worker is not None
        ext._worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ext._worker
        assert ext.skills._snapshot is snapshot and ext.generation == generation
        assert runtime.tools is registry and ext._dirty
