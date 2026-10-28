from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from test_mcp import FakeClient
from test_runtime import FakeClient as ModelClient

from muli.config import ConfigFile, Settings, SkillsSettings, default_home
from muli.extensions import ExtensionError, MCPDirectory
from muli.management import SelfManagement
from muli.providers import Message, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app
from muli.skills import SkillLoadError, SkillSnapshot, _secure_read_supported
from muli.tools.mcp import MCPClient, MCPManager, MCPPrepareError


def server(url: str = "old", *, enabled: bool = True, transport: str = "http") -> dict[str, Any]:
    if transport == "stdio":
        return {
            "enabled": enabled,
            "transport": "stdio",
            "command": "python",
            "args": [f"{url}.py"],
            "env": {"TOKEN": "env-secret"},
        }
    return {"enabled": enabled, "url": f"https://example.test/{url}"}


def setup_runtime(tmp_path: Path, *, transport: str = "http") -> Runtime:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "skills": {"directory": str(tmp_path / "skills")},
            "extensions": {
                "mcp_directory": str(tmp_path / "mcp.d"),
                "watch_enabled": False,
                "debounce_seconds": 0,
                "scan_interval_seconds": 0.25,
            },
            "mcp": {"mcpServers": {"old": server(transport=transport)}},
            "updates": {"check_on_start": False},
        }
    )
    return Runtime(config, tmp_path / "data")


def fragment(tmp_path: Path, name: str, value: dict[str, Any]) -> Path:
    root = tmp_path / "mcp.d"
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


async def watch_until_reloaded(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    published = asyncio.Event()
    original = runtime.extensions.apply_locked

    async def observe(target: Settings, *, persist: bool) -> None:
        await original(target, persist=persist)
        published.set()

    monkeypatch.setattr(runtime.extensions, "apply_locked", observe)
    runtime.extensions.start()
    async with asyncio.timeout(2):
        await published.wait()


@pytest.mark.parametrize("valid_initial_source", [True, False])
async def test_initial_mcp_source_change_is_not_acknowledged_as_the_prepared_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid_initial_source: bool
) -> None:
    ready, release = asyncio.Event(), asyncio.Event()

    class GatedList(FakeClient):
        async def list_tools(self) -> list[Any]:
            ready.set()
            await release.wait()
            return await super().list_tools()

    runtime = setup_runtime(tmp_path)
    runtime.config.settings.extensions.watch_enabled = True
    path = fragment(tmp_path, "changing.json", {"mcpServers": {"before": server("before")}})
    if not valid_initial_source:
        path.write_text('{"mcpServers": {}, "mcpServers": {}}', encoding="utf-8")
    clients: list[FakeClient] = []

    def factory(config: dict[str, Any], timeout: float) -> MCPClient:
        client = GatedList() if not clients else FakeClient()
        clients.append(client)
        return client

    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=factory)
    operation: asyncio.Task[None] | None = None
    try:
        operation = asyncio.create_task(runtime.extensions.load_initial())
        async with asyncio.timeout(2):
            await ready.wait()
        fragment(tmp_path, "changing.json", {"mcpServers": {"after": server("after")}})
        release.set()
        async with asyncio.timeout(2):
            await operation
        await watch_until_reloaded(runtime, monkeypatch)
        assert {item["name"] for item in runtime.mcp.status()} == {"old", "after"}
        assert runtime.extensions.phase == "loaded"
        assert runtime.extensions.status()["error"] is None
    finally:
        release.set()
        if operation is not None:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await runtime.close()


@pytest.mark.skipif(not _secure_read_supported(), reason="Skills 安全读取需 dir_fd/O_NOFOLLOW")
async def test_initial_skill_source_change_is_not_acknowledged_as_the_prepared_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = setup_runtime(tmp_path)
    runtime.config.settings.extensions.watch_enabled = True
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: FakeClient())
    path = tmp_path / "skills" / "example" / "SKILL.md"
    path.parent.mkdir(parents=True)
    header = "---\nname: example\ndescription: 门控技能\n---\n"
    path.write_text(header + "旧指导", encoding="utf-8")
    original = runtime.extensions.skills.prepare
    ready, release = asyncio.Event(), asyncio.Event()

    async def paused_prepare(settings: SkillsSettings) -> SkillSnapshot:
        snapshot = await original(settings)
        if not ready.is_set():
            ready.set()
            await release.wait()
        return snapshot

    monkeypatch.setattr(runtime.extensions.skills, "prepare", paused_prepare)
    operation: asyncio.Task[None] | None = None
    try:
        operation = asyncio.create_task(runtime.extensions.load_initial())
        async with asyncio.timeout(2):
            await ready.wait()
        path.write_text(header + "新指导", encoding="utf-8")
        release.set()
        async with asyncio.timeout(2):
            await operation
        await watch_until_reloaded(runtime, monkeypatch)
        assert runtime.extensions.skills._snapshot.entries[0].body == "新指导"
        assert runtime.extensions.phase == "loaded"
    finally:
        release.set()
        if operation is not None:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await runtime.close()


async def test_unscannable_initial_directory_keeps_inline_tools_available(tmp_path: Path) -> None:
    runtime = setup_runtime(tmp_path)
    (tmp_path / "mcp.d").write_text("不是目录", encoding="utf-8")
    client = FakeClient()
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: client)
    try:
        await runtime.extensions.load_initial()
        assert client.entered == 1 and len(runtime.mcp.tools) == 1
        assert runtime.extensions.status()["error"]["code"] == "source_not_directory"
        assert runtime.extensions.phase == "failed"
    finally:
        await runtime.close()


@pytest.mark.parametrize("fingerprint_failure", ["none", "skill", "os"])
async def test_bad_initial_skills_source_does_not_block_inline_or_core_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fingerprint_failure: str
) -> None:
    runtime = setup_runtime(tmp_path)
    (tmp_path / "skills").write_text("不是技能目录", encoding="utf-8")
    client = FakeClient()
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: client)

    async def unavailable_fingerprint(settings: SkillsSettings) -> str:
        if fingerprint_failure == "skill":
            raise SkillLoadError("source_unavailable")
        raise OSError("temporary fingerprint failure")

    if fingerprint_failure != "none":
        monkeypatch.setattr(runtime.extensions.skills, "fingerprint", unavailable_fingerprint)
    try:
        await runtime.extensions.load_initial()
        assert client.entered == 1 and len(runtime.mcp.tools) == 1
        assert "skill_read" in runtime.tools
        assert runtime.extensions.status()["error"]["code"] == "directory_not_regular"
        assert runtime.extensions.phase == "failed"
    finally:
        await runtime.close()


async def test_unpublished_reconfiguration_reobserves_same_source_after_fingerprint_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = setup_runtime(tmp_path)
    runtime.config.settings.extensions.watch_enabled = True
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: FakeClient())
    try:
        await runtime.extensions.load_initial()
        await runtime.reconfigure()
        runtime.mcp = MCPManager(
            runtime.config.settings.mcp, client_factory=lambda *_: FakeClient()
        )
        original = runtime.extensions.fingerprint
        scans = 0

        async def interrupted_fingerprint(settings: Settings) -> str:
            nonlocal scans
            scans += 1
            if scans == 2:
                raise SkillLoadError("source_unavailable")
            return await original(settings)

        monkeypatch.setattr(runtime.extensions, "fingerprint", interrupted_fingerprint)
        await runtime.extensions.load_initial()
        assert runtime.extensions.phase == "failed"
        assert runtime.mcp.tools == []
        await watch_until_reloaded(runtime, monkeypatch)
        assert runtime.extensions.phase == "loaded"
        assert {item["name"] for item in runtime.mcp.status()} == {"old"}
    finally:
        await runtime.close()


async def test_initial_source_recheck_cannot_publish_after_extensions_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = setup_runtime(tmp_path)
    client = FakeClient()
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: client)
    original = runtime.extensions.fingerprint
    ready, release = asyncio.Event(), asyncio.Event()
    scans = 0

    async def paused_fingerprint(settings: Settings) -> str:
        nonlocal scans
        value = await original(settings)
        scans += 1
        if scans == 2:
            ready.set()
            await release.wait()
        return value

    monkeypatch.setattr(runtime.extensions, "fingerprint", paused_fingerprint)
    operation: asyncio.Task[None] | None = None
    try:
        operation = asyncio.create_task(runtime.extensions.load_initial())
        async with asyncio.timeout(2):
            await ready.wait()
        await runtime.extensions.close()
        release.set()
        async with asyncio.timeout(2):
            await operation
        assert runtime.extensions.generation == 0
        assert runtime.mcp.tools == []
        assert client.closed == 1
        assert runtime.extensions.status()["error"]["code"] == "runtime_closed"
    finally:
        release.set()
        if operation is not None:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await runtime.close()


async def test_full_configuration_cannot_discard_an_unclosed_mcp_generation(
    tmp_path: Path,
) -> None:
    class Unclosed(FakeClient):
        allow_close = False

        async def close(self) -> None:
            await super().close()
            if not self.allow_close:
                raise RuntimeError("private transport cleanup detail")

    runtime = setup_runtime(tmp_path)
    client = Unclosed()
    previous = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: client)
    runtime.mcp = previous
    app = create_app(runtime.config, runtime=runtime)
    original = runtime.config.settings.model_dump()
    try:
        await runtime.extensions.load_initial()
        target = runtime.config.validate_changes({"persona": {"append": "requires full rebuild"}})
        async with runtime.configuration_lock:
            with pytest.raises(MCPPrepareError, match="connection_cleanup_pending"):
                await app.state.management.apply_settings(target)
        assert runtime.mcp is previous
        assert runtime.config.settings.model_dump() == original
        assert ConfigFile(runtime.config.path).settings.model_dump() == original
        assert previous.pending_cleanup == 1
        assert previous.status()[0]["status"] == "close_failed"
        assert runtime.extensions.status()["cleanup_pending"] == 1
        assert "private transport" not in json.dumps(runtime.extensions.status())
        assert client.entered == 1
    finally:
        client.allow_close = True
        await previous.close()
        await runtime.close()


def directory_settings(tmp_path: Path, **limits: Any) -> Settings:
    return Settings.model_validate(
        {"extensions": {"mcp_directory": str(tmp_path / "mcp.d"), **limits}}
    )


def test_directory_uses_full_path_identity_when_direntry_has_windows_zero_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fragment(tmp_path, "windows.json", {"mcpServers": {"native": server()}})
    original_scandir = os.scandir

    class WindowsDirEntry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.entry, self.name = entry, entry.name

        def stat(self, *, follow_symlinks: bool = True) -> SimpleNamespace:
            info = self.entry.stat(follow_symlinks=follow_symlinks)
            return SimpleNamespace(
                st_dev=0,
                st_ino=0,
                st_nlink=0,
                st_mode=info.st_mode,
                st_size=info.st_size,
                st_mtime_ns=info.st_mtime_ns,
                st_ctime_ns=info.st_ctime_ns,
            )

    @contextmanager
    def windows_scandir(path: str | os.PathLike[str]) -> Iterator[Iterator[WindowsDirEntry]]:
        with original_scandir(path) as entries:
            yield (WindowsDirEntry(entry) for entry in entries)

    monkeypatch.setattr(os, "scandir", windows_scandir)
    snapshot = MCPDirectory(directory_settings(tmp_path)).prepare()
    assert snapshot.sources == ("windows.json",)
    assert snapshot.config["mcpServers"]["native"]["enabled"] is True


def _with_ctime(info: os.stat_result, nanoseconds: int) -> os.stat_result:
    return os.stat_result(
        info,
        {"st_mtime_ns": info.st_mtime_ns, "st_ctime_ns": nanoseconds},
    )


def test_directory_accepts_distinct_stable_path_and_handle_ctimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fragment(tmp_path, "windows.json", {"mcpServers": {"native": server()}})
    handle_ctime = path.lstat().st_ctime_ns + 1
    original_fstat = os.fstat
    calls = 0

    def fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        return _with_ctime(original_fstat(descriptor), handle_ctime)

    monkeypatch.setattr(os, "fstat", fstat)
    snapshot = MCPDirectory(directory_settings(tmp_path)).prepare()
    assert snapshot.sources == ("windows.json",)
    assert snapshot.config["mcpServers"]["native"]["enabled"] is True
    assert calls == 2


@pytest.mark.parametrize("api", ["fstat", "lstat"])
def test_directory_still_rejects_ctime_changes_within_each_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: str
) -> None:
    path = fragment(tmp_path, "changing.json", {"mcpServers": {"native": server()}})
    initial_ctime = path.lstat().st_ctime_ns
    calls = 0
    original_fstat, original_lstat = os.fstat, Path.lstat

    def fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        return _with_ctime(original_fstat(descriptor), initial_ctime + (calls > 1))

    def lstat(source: Path) -> os.stat_result:
        nonlocal calls
        info = original_lstat(source)
        if source == path:
            calls += 1
            return _with_ctime(info, initial_ctime + (calls > 1))
        return info

    if api == "fstat":
        monkeypatch.setattr(os, "fstat", fstat)
    else:
        monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ExtensionError, match="source_changed"):
        MCPDirectory(directory_settings(tmp_path)).prepare()
    assert calls == 2


async def test_unconfigured_directories_respect_muli_home_and_ignore_physical_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    physical_home = tmp_path / "physical-home"
    unwanted_mcp = physical_home / "MuLi" / "mcp.d"
    unwanted_mcp.mkdir(parents=True)
    (unwanted_mcp / "unexpected.json").write_text(
        json.dumps({"mcpServers": {"unexpected": server("unexpected")}}), encoding="utf-8"
    )
    unwanted_skill = physical_home / "MuLi" / "skills" / "unexpected"
    unwanted_skill.mkdir(parents=True)
    (unwanted_skill / "SKILL.md").write_text(
        "---\nname: unexpected\ndescription: physical home fixture\n---\nDo not load.\n",
        encoding="utf-8",
    )

    def home(cls: type[Path]) -> Path:
        return physical_home

    monkeypatch.setattr(Path, "home", classmethod(home))
    isolated = Path(os.environ["MULI_HOME"])
    assert default_home() == isolated and isolated != physical_home / "MuLi"
    config = ConfigFile(tmp_path / "config.json")
    runtime = Runtime(config, tmp_path / "data")
    connected: list[dict[str, Any]] = []

    def factory(config: dict[str, Any], timeout: float) -> MCPClient:
        connected.append(config)
        return FakeClient()

    runtime.mcp = MCPManager(config.settings.mcp, client_factory=factory)
    try:
        await runtime.extensions.load_initial()
        assert connected == []
        assert runtime.extensions.status()["skills"]["count"] == 0
        assert runtime.extensions.phase == "loaded"
    finally:
        await runtime.close()


def test_directory_enable_policy_does_not_change_legacy_inline_defaults(tmp_path: Path) -> None:
    fragment(
        tmp_path,
        "first.json",
        {"mcpServers": {"directory": {"url": "https://example.test/directory"}}},
    )
    fragment(
        tmp_path,
        "second.json",
        {"mcpServers": {"disabled": {"enabled": False, "command": "installed"}}},
    )
    settings = directory_settings(tmp_path)
    settings.mcp = {"mcpServers": {"legacy": {"url": "https://example.test/legacy"}}}
    snapshot = MCPDirectory(settings).prepare()
    servers = snapshot.config["mcpServers"]
    assert servers["directory"]["enabled"] is True
    assert servers["disabled"]["enabled"] is False
    assert "enabled" not in servers["legacy"]
    assert snapshot.sources == ("first.json", "second.json")


def test_duplicate_json_keys_and_cross_source_server_names_are_rejected(tmp_path: Path) -> None:
    path = fragment(tmp_path, "one.json", {})
    path.write_text('{"mcpServers": {}, "mcpServers": {}}', encoding="utf-8")
    with pytest.raises(ExtensionError, match="duplicate_json_key"):
        MCPDirectory(directory_settings(tmp_path)).prepare()
    path.write_text(json.dumps({"mcpServers": {"same": server()}}), encoding="utf-8")
    fragment(tmp_path, "two.json", {"mcpServers": {"same": server("different")}})
    with pytest.raises(ExtensionError, match="duplicate_server") as failed:
        MCPDirectory(directory_settings(tmp_path)).prepare()
    assert failed.value.source == "one.json / two.json"
    (tmp_path / "mcp.d" / "two.json").unlink()
    settings = directory_settings(tmp_path)
    settings.mcp = {"mcpServers": {"same": server()}}
    with pytest.raises(ExtensionError) as failed:
        MCPDirectory(settings).prepare()
    assert failed.value.source == "config.mcp / one.json"


@pytest.mark.parametrize(
    "limits,code",
    [
        ({"max_mcp_fragment_bytes": 1}, "fragment_budget_exceeded"),
        ({"max_mcp_source_bytes": 1}, "source_budget_exceeded"),
        ({"max_mcp_fragments": 1}, "fragment_budget_exceeded"),
        ({"max_entries": 1}, "directory_budget_exceeded"),
    ],
)
def test_source_budgets_fail_instead_of_accepting_a_partial_directory(
    tmp_path: Path, limits: dict[str, Any], code: str
) -> None:
    fragment(tmp_path, "one.json", {"mcpServers": {"one": server()}})
    fragment(tmp_path, "two.json", {"mcpServers": {"two": server()}})
    with pytest.raises(ExtensionError, match=code):
        MCPDirectory(directory_settings(tmp_path, **limits)).prepare()


def test_symlink_fragment_is_rejected_and_changing_source_is_not_a_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fragment(tmp_path, "one.json", {})
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    link = path.parent / "link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("当前平台未允许创建符号链接")
    with pytest.raises(ExtensionError, match="source_not_regular"):
        MCPDirectory(directory_settings(tmp_path)).prepare()
    link.unlink()
    monkeypatch.setattr(MCPDirectory, "fingerprint", lambda self: "changed")
    with pytest.raises(ExtensionError, match="source_changed"):
        MCPDirectory(directory_settings(tmp_path)).prepare()


@pytest.mark.parametrize("replacement", ["fifo", "symlink"])
def test_source_swapped_at_open_boundary_is_rejected_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    if replacement == "fifo" and (not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK")):
        pytest.skip("当前平台没有非阻塞 FIFO 接口")
    path = fragment(tmp_path, "gated.json", {})
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    original_open = os.open
    swapped = False

    def swap_at_open(value: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if Path(value) == path and not swapped:
            swapped = True
            path.unlink()
            if replacement == "fifo":
                assert flags & os.O_NONBLOCK
                os.mkfifo(path)
            else:
                try:
                    path.symlink_to(outside)
                except OSError:
                    pytest.skip("当前平台未允许创建符号链接")
                if hasattr(os, "O_NOFOLLOW"):
                    assert flags & os.O_NOFOLLOW
        return original_open(value, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_at_open)
    with pytest.raises(ExtensionError, match="source_changed"):
        MCPDirectory(directory_settings(tmp_path)).prepare()
    assert swapped


@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_failed_reload_keeps_config_catalog_and_active_connection(
    tmp_path: Path, transport: str
) -> None:
    runtime = setup_runtime(tmp_path, transport=transport)
    old, failed = FakeClient(), FakeClient(failure=True)
    clients = iter((old, failed))
    runtime.mcp = MCPManager(
        runtime.config.settings.mcp,
        client_factory=lambda *_: next(clients),
    )
    try:
        await runtime.extensions.load_initial()
        generation = runtime.extensions.generation
        original_config = runtime.config.path.read_bytes()
        original_tool = runtime.mcp.tools[0]
        target = runtime.config.validate_changes(
            {"mcp": {"mcpServers": {"old": server("new", transport=transport)}}}
        )
        async with runtime.configuration_lock:
            with pytest.raises(ExtensionError, match="server_unavailable"):
                await runtime.extensions.apply_locked(target, persist=True)
        assert runtime.config.path.read_bytes() == original_config
        assert runtime.extensions.generation == generation
        assert runtime.tools[original_tool.name] is original_tool
        assert old.closed == 0 and failed.closed == 1
        assert not (await original_tool.handler({"q": "retained"}))["is_error"]
        assert "secret" not in json.dumps(runtime.extensions.status())
    finally:
        await runtime.close()


async def test_invalid_initial_fragment_keeps_inline_tools_and_reports_failure(
    tmp_path: Path,
) -> None:
    runtime = setup_runtime(tmp_path)
    path = fragment(tmp_path, "invalid.json", {})
    path.write_text('{"mcpServers": {}, "mcpServers": {}}', encoding="utf-8")
    client = FakeClient()
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: client)
    try:
        await runtime.extensions.load_initial()
        assert client.entered == 1 and len(runtime.mcp.tools) == 1
        assert runtime.extensions.status()["error"]["code"] == "duplicate_json_key"
        assert runtime.extensions.phase == "failed"
    finally:
        await runtime.close()


@pytest.mark.parametrize("kind,status", [("agent", "queued"), ("workflow_run", "running")])
async def test_durable_busy_owners_allow_pure_extension_publication(
    tmp_path: Path, kind: str, status: str
) -> None:
    runtime = setup_runtime(tmp_path)
    client = FakeClient()
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: client)
    try:
        await runtime.extensions.load_initial()
        owner = {"id": "fixture-owner", "status": status}
        runtime.store.put(kind, owner)
        generation, tool = runtime.extensions.generation, runtime.mcp.tools[0]
        async with runtime.configuration_lock:
            await runtime.extensions.apply_locked(runtime.config.settings, persist=False)
        assert runtime.extensions.generation == generation + 1 and runtime.busy
        assert runtime.store.get(kind, owner["id"]) == owner
        denied = await tool.handler({"q": "旧代"})
        assert denied["status"] == "not_executed" and denied["code"] == "generation_changed"
        assert client.calls == []
        assert not (await runtime.mcp.tools[0].handler({"q": "新代"}))["is_error"]
        assert client.entered == 1 and client.closed == 0
    finally:
        await runtime.close()


@pytest.mark.parametrize("change", ["source", "shutdown"])
@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_changed_sources_or_shutdown_discard_the_prepared_candidate(
    tmp_path: Path, change: str, transport: str
) -> None:
    ready, release = asyncio.Event(), asyncio.Event()

    class GatedList(FakeClient):
        async def list_tools(self) -> list[Any]:
            ready.set()
            await release.wait()
            return await super().list_tools()

    runtime = setup_runtime(tmp_path, transport=transport)
    old, new = FakeClient(), GatedList()
    clients = iter((old, new))
    runtime.mcp = MCPManager(
        runtime.config.settings.mcp,
        client_factory=lambda *_: next(clients),
    )

    async def reload() -> None:
        async with runtime.configuration_lock:
            target = runtime.config.validate_changes(
                {"mcp": {"mcpServers": {"old": server("new", transport=transport)}}}
            )
            await runtime.extensions.apply_locked(target, persist=True)

    operation: asyncio.Task[None] | None = None
    try:
        await runtime.extensions.load_initial()
        generation = runtime.extensions.generation
        original_config = runtime.config.path.read_bytes()
        original_tool = runtime.mcp.tools[0]
        operation = asyncio.create_task(reload())
        await asyncio.wait_for(ready.wait(), timeout=2)
        if change == "source":
            fragment(tmp_path, "changed.json", {"mcpServers": {"another": server("another")}})
        else:
            await runtime.extensions.close()
        release.set()
        code = "source_changed" if change == "source" else "runtime_closed"
        with pytest.raises(ExtensionError, match=code):
            await operation
        assert runtime.extensions.generation == generation
        assert runtime.config.path.read_bytes() == original_config
        assert runtime.tools[original_tool.name] is original_tool
        assert old.closed == 0 and new.closed == 1
        assert set(runtime.config.settings.mcp["mcpServers"]) == {"old"}
        assert not (await original_tool.handler({"q": "候选丢弃后"}))["is_error"]
    finally:
        release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_config_save_failure_discards_candidate_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    runtime = setup_runtime(tmp_path, transport=transport)
    old, new = FakeClient(), FakeClient(names=["new"])
    clients = iter((old, new))
    runtime.mcp = MCPManager(
        runtime.config.settings.mcp,
        client_factory=lambda *_: next(clients),
    )
    try:
        await runtime.extensions.load_initial()
        generation = runtime.extensions.generation
        before = runtime.config.settings
        original_config = runtime.config.path.read_bytes()
        original_tool = runtime.mcp.tools[0]
        target = runtime.config.validate_changes(
            {"mcp": {"mcpServers": {"old": server("new", transport=transport)}}}
        )

        def fail_save(settings: Settings) -> Settings:
            raise OSError("private-fixture-value")

        monkeypatch.setattr(runtime.config, "replace", fail_save)
        async with runtime.configuration_lock:
            with pytest.raises(ExtensionError, match="config_save_failed"):
                await runtime.extensions.apply_locked(target, persist=True)
        assert runtime.config.settings is before
        assert runtime.config.path.read_bytes() == original_config
        assert runtime.extensions.generation == generation
        assert runtime.tools[original_tool.name] is original_tool
        assert old.closed == 0 and new.closed == 1
        assert not (await original_tool.handler({"q": "保存失败后"}))["is_error"]
        assert runtime.extensions.status()["last_apply"] == {
            "configuration": "save_failed",
            "published": False,
            "retirement": "unneeded",
        }
        assert "private-fixture-value" not in json.dumps(runtime.extensions.status())
    finally:
        await runtime.close()


@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_candidate_total_deadline_does_not_retry_or_close_old_connection(
    tmp_path: Path, transport: str
) -> None:
    class WaitingClient(FakeClient):
        async def list_tools(self) -> list[Any]:
            await asyncio.Event().wait()
            return []

    runtime = setup_runtime(tmp_path, transport=transport)
    old, waiting = FakeClient(), WaitingClient()
    clients = iter((old, waiting))
    runtime.mcp = MCPManager(
        runtime.config.settings.mcp,
        client_factory=lambda *_: next(clients),
    )
    try:
        await runtime.extensions.load_initial()
        target = runtime.config.validate_changes(
            {
                "mcp": {"mcpServers": {"old": server("new", transport=transport)}},
                "extensions": {"candidate_timeout_seconds": 0.05},
            }
        )
        async with runtime.configuration_lock:
            with pytest.raises(ExtensionError, match="candidate_timeout"):
                await runtime.extensions.apply_locked(target, persist=True)
        assert waiting.entered == 1 and waiting.closed == 1 and old.closed == 0
        assert runtime.config.settings.extensions.candidate_timeout_seconds == 30
    finally:
        await runtime.close()


@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_reload_publishes_while_busy_but_waits_for_the_configuration_lock(
    tmp_path: Path, transport: str
) -> None:
    runtime = setup_runtime(tmp_path)
    runtime.config.update({"mcp": {"mcpServers": {}}})
    fragment(tmp_path, "current.json", {"mcpServers": {"old": server(transport=transport)}})
    clients: list[FakeClient] = []

    def factory(config: dict[str, Any], timeout: float) -> MCPClient:
        client = FakeClient()
        clients.append(client)
        return client

    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=factory)
    release = asyncio.Event()

    async def run() -> str:
        await release.wait()
        return "finished"

    work = asyncio.create_task(run())
    runtime.tasks["busy-fixture"] = work
    work.add_done_callback(lambda _: runtime.tasks.pop("busy-fixture", None))
    try:
        await runtime.extensions.load_initial()
        generation = runtime.extensions.generation
        original_config = runtime.config.path.read_bytes()
        original_tool = runtime.mcp.tools[0]
        fragment(
            tmp_path,
            "current.json",
            {"mcpServers": {"old": server("new", transport=transport)}},
        )
        async with runtime.configuration_lock:
            runtime.extensions.start()
            runtime.extensions.request_reload()
            await asyncio.sleep(0)
            assert runtime.extensions.phase == "queued" and len(clients) == 1
            assert runtime.extensions.generation == generation and not work.done()
        async with asyncio.timeout(2):
            while runtime.extensions.generation == generation:
                await asyncio.sleep(0.01)
        assert len(clients) == 2
        async with asyncio.timeout(2):
            while runtime.mcp.pending_cleanup:
                await asyncio.sleep(0)
        assert clients[0].closed == 1 and clients[1].closed == 0
        assert not work.done() and runtime.busy
        assert {item["name"] for item in runtime.mcp.status()} == {"old"}
        assert runtime.config.path.read_bytes() == original_config
        assert (await original_tool.handler({"q": "旧目录"}))["code"] == "generation_changed"
        assert not (await runtime.mcp.tools[0].handler({"q": "新目录"}))["is_error"]
    finally:
        release.set()
        await work
        runtime.tasks.pop("busy-fixture", None)
        await runtime.close()


@pytest.mark.parametrize("mode", ["api", "automatic", "management"])
async def test_report_waking_main_during_prepare_does_not_block_pure_extension_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    ready, finish_prepare = asyncio.Event(), asyncio.Event()
    model_started, finish_model = asyncio.Event(), asyncio.Event()

    class GatedList(FakeClient):
        async def list_tools(self) -> list[Any]:
            ready.set()
            await finish_prepare.wait()
            return await super().list_tools()

    class WaitingModel(ModelClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            model_started.set()
            await finish_model.wait()
            async for event in super().stream(messages, tools):
                yield event

    runtime = setup_runtime(tmp_path)
    runtime.config.update(
        {"models": {"main": {"model": "fixture", "base_url": "https://unused.invalid/v1"}}}
    )
    model = WaitingModel([ModelTurn(content="offline report received")], [])
    monkeypatch.setattr(runtime, "client_factory", lambda _: model)
    old = FakeClient()
    candidates: list[GatedList] = []

    def factory(config: dict[str, Any], timeout: float) -> MCPClient:
        if config["url"].endswith("/old"):
            return old
        candidate = GatedList()
        candidates.append(candidate)
        return candidate

    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=factory)
    app = create_app(runtime.config, runtime=runtime)
    manager: SelfManagement = app.state.management
    published = asyncio.Event()
    apply_locked = runtime.extensions.apply_locked

    async def observe_publication(settings: Settings, *, persist: bool) -> None:
        await apply_locked(settings, persist=persist)
        published.set()

    monkeypatch.setattr(runtime.extensions, "apply_locked", observe_publication)
    operation: asyncio.Task[httpx.Response] | None = None
    try:
        await runtime.extensions.load_initial()
        generation, registry = runtime.extensions.generation, runtime.tools
        original_tool = runtime.mcp.tools[0]
        skills = runtime.extensions.skills.status()
        original_config = runtime.config.path.read_bytes()
        fragment(tmp_path, "new.json", {"mcpServers": {"new": server("new")}})
        changes = {"skills": {"enabled": False}}
        plan: dict[str, Any] | None = None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as api:
            if mode == "api":
                operation = asyncio.create_task(api.patch("/api/settings", json=changes))
            elif mode == "automatic":
                runtime.extensions.start()
                runtime.extensions.request_reload()
            else:
                plan = await manager.stage("configure", changes)
                await manager.start()
            await asyncio.wait_for(ready.wait(), timeout=2)
            assert runtime.configuration_lock.locked() and not runtime.busy

            # Report delivery is a real internal admission path outside configuration_lock.
            session = runtime.main_session()
            child = runtime.new_session("finished fixture teammate")
            child.update(kind="team", status="idle")
            runtime.store.put("session", child)
            runtime.store.put(
                "agent",
                {
                    "id": child["id"],
                    "session_id": child["id"],
                    "parent_id": session["id"],
                    "status": "completed",
                },
            )
            report = {
                "id": "fixture-report",
                "agent_id": child["id"],
                "parent_id": session["id"],
                "status": "pending",
                "content": "offline teammate result",
            }
            runtime.store.put("team_report", report)
            await runtime.team._deliver_report(report)
            await asyncio.wait_for(model_started.wait(), timeout=2)
            run_id = runtime.run_ids[session["id"]]
            assert runtime.busy
            finish_prepare.set()
            if operation is not None:
                response = await operation
                assert response.status_code == 200
            await asyncio.wait_for(published.wait(), timeout=2)
            assert runtime.busy and not runtime.tasks[session["id"]].done()
            assert len(candidates) == 1 and candidates[0].closed == 0 and old.closed == 0
            assert runtime.extensions.generation == generation + 1 and runtime.tools is not registry
            assert {item["name"] for item in runtime.mcp.status()} == {"old", "new"}
            denied = await original_tool.handler({"q": "旧报告轮次的工具"})
            assert denied["status"] == "not_executed" and denied["code"] == "generation_changed"
            assert old.calls == candidates[0].calls == []
            if mode == "automatic":
                assert runtime.config.path.read_bytes() == original_config
                assert runtime.extensions.skills.status() == skills
            else:
                assert runtime.config.path.read_bytes() != original_config
                assert runtime.config.settings.skills.enabled is False
            if plan is not None:

                def completed() -> bool:
                    assert plan is not None
                    saved = runtime.store.get("management", plan["id"])
                    return saved is not None and saved["status"] == "completed"

                async with asyncio.timeout(2), runtime.bus.changed:
                    await runtime.bus.changed.wait_for(completed)
                assert not (manager.directory / f"{plan['id']}.json").exists()
            finish_model.set()
            assert await runtime.wait(run_id) == "offline report received"
    finally:
        finish_prepare.set()
        finish_model.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await manager.close()
        await runtime.close()


async def test_failed_fingerprint_is_not_retried_until_an_explicit_request(tmp_path: Path) -> None:
    runtime = setup_runtime(tmp_path)
    old, failed = FakeClient(), FakeClient(failure=True)
    runtime.mcp = MCPManager(
        runtime.config.settings.mcp,
        client_factory=lambda config, timeout: old if config["url"].endswith("/old") else failed,
    )
    try:
        await runtime.extensions.load_initial()
        runtime.config.settings.extensions.watch_enabled = True
        fragment(tmp_path, "bad.json", {"mcpServers": {"bad": server("bad")}})
        runtime.extensions.start()
        async with asyncio.timeout(2):
            while runtime.extensions.phase != "failed":
                await asyncio.sleep(0.01)
        assert failed.entered == 1
        # Wait for one scanner turn, not repeated execution of the failing operation.
        await asyncio.sleep(runtime.config.settings.extensions.scan_interval_seconds + 0.02)
        assert failed.entered == 1
        runtime.extensions.request_reload()
        async with asyncio.timeout(2):
            while failed.entered == 1:
                await asyncio.sleep(0.01)
        assert failed.entered == 2 and old.closed == 0
    finally:
        await runtime.close()


async def test_extension_only_settings_use_narrow_apply_and_do_not_close_other_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = setup_runtime(tmp_path)
    client = FakeClient()
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: client)
    app = create_app(runtime.config, runtime=runtime)
    forbidden: list[str] = []

    async def full_reconfigure() -> None:
        forbidden.append("full")
        raise AssertionError("should use narrow extension apply")

    monkeypatch.setattr(runtime, "reconfigure", full_reconfigure)
    try:
        await runtime.extensions.load_initial()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as api:
            response = await api.patch("/api/settings", json={"extensions": {"max_mcp_pages": 4}})
            assert response.status_code == 200
            status = (await api.get("/api/extensions")).json()
            assert status["phase"] == "loaded" and status["last_apply"]["configuration"] == "saved"
            response = await api.post("/api/extensions/reload", json={})
            assert response.json()["phase"] == "queued"
        assert forbidden == [] and client.closed == 0 and client.entered == 1
    finally:
        await runtime.close()
