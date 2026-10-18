from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from muli.config import ConfigFile, Settings
from muli.events import EventBus
from muli.integrations.chiliarch import Envelope
from muli.permissions import Permissions
from muli.process import run_process
from muli.store import Store
from muli.tools import web
from muli.tools.atspi_helper import verify_identity
from muli.tools.computer import Computer
from muli.tools.files import Files


def test_inbox_conflicts_are_rejected_without_duplicate_notification(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db")
    try:
        first = Envelope(type="agent.message", payload={"message": "first"}).to_dict()
        assert store.ingest(first)
        assert not store.ingest(dict(reversed(list(first.items()))))
        with pytest.raises(ValueError, match="内容不一致"):
            store.ingest({**first, "payload": {"message": "different"}})
        assert len(store.list("notification")) == 1
        assert json.loads(store.db.execute("SELECT envelope FROM inbox").fetchone()[0]) == first
        if os.name == "posix":
            assert (tmp_path / "state.db").stat().st_mode & 0o777 == 0o600
    finally:
        store.close()


def test_configuration_views_hide_secrets_and_support_response_limits(
    tmp_path: Path,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "models": {
                "main": {
                    "api_key": "secret-key",
                    "extra_headers": {"Authorization": "other-secret"},
                    "stream": False,
                    "max_response_bytes": 2048,
                }
            },
            "mcp": {"mcpServers": {"local": {"env": {"TOKEN": "mcp-secret"}}}},
            "chiliarch": {"credential": "agent-secret"},
        }
    )
    public = json.dumps(config.public())
    assert all(
        secret not in public
        for secret in ("secret-key", "other-secret", "mcp-secret", "agent-secret")
    )
    assert config.settings.models.main.max_response_bytes == 2048
    assert not config.settings.models.main.stream


def test_mcp_redacted_roundtrip_preserves_values_and_supports_deletion(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "mcp": {
                "mcpServers": {
                    "local": {
                        "enabled": False,
                        "command": "already-installed",
                        "args": [],
                        "env": {"TOKEN": "env-secret", "EMPTY": ""},
                    },
                    "remote": {
                        "enabled": False,
                        "url": "https://example.invalid/mcp?token=query-secret",
                        "headers": {"Authorization": "Bearer header-secret"},
                    },
                }
            }
        }
    )
    public = config.public()["mcp"]
    assert public["mcpServers"]["local"]["env"] == {"TOKEN": "••••", "EMPTY": "••••"}
    assert public["mcpServers"]["remote"]["url"] == "https://example.invalid/mcp?••••"
    assert "secret" not in json.dumps(public)
    config.update({"mcp": public})
    assert config.settings.mcp["mcpServers"]["local"]["env"]["TOKEN"] == "env-secret"
    assert config.settings.mcp["mcpServers"]["local"]["env"]["EMPTY"] == ""
    assert config.settings.mcp["mcpServers"]["remote"]["url"].endswith("token=query-secret")
    assert (
        config.settings.mcp["mcpServers"]["remote"]["headers"]["Authorization"]
        == "Bearer header-secret"
    )
    public["mcpServers"].pop("remote")
    public["mcpServers"]["local"]["env"].pop("TOKEN")
    config.update({"mcp": public})
    assert set(config.settings.mcp["mcpServers"]) == {"local"}
    assert config.settings.mcp["mcpServers"]["local"]["env"] == {"EMPTY": ""}
    config.update({"mcp": {}})
    assert config.settings.mcp == {}
    assert "secret" not in config.path.read_text()


@pytest.mark.parametrize(
    "mcp",
    [
        {"unknown": {}},
        {"mcpServers": []},
        {"mcpServers": {"bad": []}},
        {"mcpServers": {"bad": {"enabled": "true"}}},
        {"mcpServers": {"bad": {"env": {"TOKEN": 123}}}},
        {"mcpServers": {"bad": {"headers": []}}},
        {"mcpServers": {"bad": {"headers": {"Authorization": "••••"}}}},
        {"mcpServers": {"bad": {"transport": []}}},
        {"mcpServers": {"bad": {"url": "https://user:secret@example.invalid/mcp"}}},
        {"mcpServers": {"bad": {"url": "https://example.invalid/mcp#secret"}}},
    ],
)
def test_invalid_mcp_is_rejected_before_persisting(tmp_path: Path, mcp: dict[str, Any]) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"mcp": {}})
    before = config.path.read_bytes()
    with pytest.raises(ValueError):
        config.update({"mcp": mcp})
    assert config.path.read_bytes() == before
    assert config.settings.mcp == {}


def test_mcp_query_placeholder_cannot_move_secret_to_another_endpoint(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {"mcp": {"mcpServers": {"remote": {"url": "https://example.invalid/mcp?token=secret"}}}}
    )
    public = config.public()["mcp"]
    public["mcpServers"]["remote"]["url"] = "https://another.invalid/mcp?••••"
    with pytest.raises(ValueError, match="重新填写"):
        config.update({"mcp": public})
    assert "example.invalid" in config.settings.mcp["mcpServers"]["remote"]["url"]


def test_event_heartbeat_does_not_hold_publisher_lock(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "state.db")
        bus = EventBus(store, heartbeat_seconds=0.001)
        stream = bus.stream()
        try:
            assert (await anext(stream)).startswith(": keepalive")
            await asyncio.wait_for(bus.emit("new", {"ok": True}), 0.2)
            assert '"type": "new"' in await anext(stream)
        finally:
            await stream.aclose()
            store.close()

    asyncio.run(scenario())


def test_permission_cancellation_removes_waiter_and_cannot_be_replayed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "state.db")

        async def review(_: str, __: dict[str, Any], ___: str) -> tuple[bool, str]:
            return False, "需批准"

        permission = Permissions(store, EventBus(store), review)
        checking = asyncio.create_task(permission.check("ask", "fs_read", {}, "read", "s", "r"))
        await asyncio.sleep(0)
        identifier = next(iter(permission.pending))
        checking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await checking
        assert not permission.pending
        assert store.get("approval", identifier)["status"] == "cancelled"  # type: ignore[index]
        with pytest.raises(ValueError):
            await permission.resolve(identifier, "approve")
        store.close()

    asyncio.run(scenario())


def test_fetch_pins_dns_address_and_retains_tls_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        resolutions = 0
        requests: list[httpx.Request] = []

        async def resolve(*_: Any, **__: Any) -> list[tuple[Any, ...]]:
            nonlocal resolutions
            resolutions += 1
            ip = "93.184.216.34" if resolutions == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200, headers={"Content-Type": "text/plain"}, text="public content"
            )

        client_class = httpx.AsyncClient
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        monkeypatch.setattr(
            web.httpx,
            "AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs),
        )
        result = await web.Web(Settings()).fetch({"url": "https://example.org/page"})
        assert result["content"] == "public content"
        assert resolutions == 1
        assert requests[0].url.host == "93.184.216.34"
        assert requests[0].headers["host"] == "example.org"
        assert requests[0].extensions["sni_hostname"] == "example.org"

    asyncio.run(scenario())


def test_fetch_redirect_revalidates_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        requests = 0

        async def resolve(host: str, *_: Any, **__: Any) -> list[tuple[Any, ...]]:
            ip = "127.0.0.1" if host == "localhost" else "93.184.216.34"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]

        def handle(_: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(302, headers={"Location": "http://localhost/private"})

        client_class = httpx.AsyncClient
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        monkeypatch.setattr(
            web.httpx,
            "AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs),
        )
        with pytest.raises(ValueError, match="内网"):
            await web.Web(Settings()).fetch({"url": "https://example.org/"})
        assert requests == 1

    asyncio.run(scenario())


def test_files_reject_symlink_escape(
    tmp_path: Path,
) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.write_text("private")
    try:
        (root / "escape").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"当前平台不能创建符号链接：{type(exc).__name__}")
    files = Files(Settings(workspace_roots=[str(root)]), tmp_path / "data")
    with pytest.raises(PermissionError):
        files._execute("read", {"path": str(root / "escape")})


def test_file_search_bounds_empty_directory_traversal(tmp_path: Path) -> None:
    settings = Settings(workspace_roots=[str(tmp_path)], limits={"max_search_entries": 3})  # type: ignore[arg-type]
    files = Files(settings, tmp_path / "data")
    for index in range(6):
        (tmp_path / f"empty-{index}").mkdir()
    assert files._execute("search", {"path": str(tmp_path)})["truncated"]


def test_unicode_configuration_and_trash_records_use_utf8(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "配置.json")
    config.update({"models": {"main": {"model": "中文模型"}}})
    assert "中文模型" in config.path.read_bytes().decode("utf-8")
    assert ConfigFile(config.path).settings.models.main.model == "中文模型"
    files = Files(Settings(workspace_roots=[str(tmp_path)]), tmp_path / "data")
    source = tmp_path / "中文文件.txt"
    source.write_text("保留内容", encoding="utf-8")
    record = files._execute("trash", {"path": str(source)})
    metadata = (files.trash / f"{record['id']}.json").read_bytes().decode("utf-8")
    assert "中文文件" in metadata
    files._execute("restore", {"id": record["id"]})
    assert source.read_text(encoding="utf-8") == "保留内容"


def test_no_overwrite_write_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "result.txt"
    files = Files(Settings(workspace_roots=[str(tmp_path)]), tmp_path / "data")
    original_link = os.link

    def competing_writer(source: Any, destination: Any, **kwargs: Any) -> None:
        target.write_text("other writer")
        original_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", competing_writer)
    with pytest.raises(FileExistsError):
        files._execute("write", {"path": str(target), "content": "new", "overwrite": False})
    assert target.read_text() == "other writer"
    assert not list(tmp_path.glob(".muli-*"))


def test_no_overwrite_copy_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = tmp_path / "source.txt", tmp_path / "target.txt"
    source.write_text("copy me")
    files = Files(Settings(workspace_roots=[str(tmp_path)]), tmp_path / "data")
    original_link = os.link

    def competing_writer(origin: Any, destination: Any, **kwargs: Any) -> None:
        target.write_text("other writer")
        original_link(origin, destination, **kwargs)

    monkeypatch.setattr(os, "link", competing_writer)
    with pytest.raises(FileExistsError):
        files._execute("copy", {"path": str(source), "destination": str(target)})
    assert target.read_text() == "other writer"
    assert source.read_text() == "copy me"
    assert not list(tmp_path.glob(".muli-*"))


async def test_file_operations_share_lock_across_tool_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = Files(Settings(workspace_roots=[str(tmp_path)]), tmp_path / "data")
    started, release = threading.Event(), threading.Event()
    active, maximum = 0, 0

    def execute(operation: str, arguments: dict[str, Any], cancelled: threading.Event) -> None:
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        started.set()
        release.wait(1)
        active -= 1

    monkeypatch.setattr(files, "_execute", execute)
    tools = {tool.name: tool for tool in files.tools()}
    first = asyncio.create_task(tools["fs_copy"].handler({}))
    second = asyncio.create_task(tools["fs_move"].handler({}))
    try:
        for _ in range(50):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        await asyncio.sleep(0.01)
        assert maximum == 1
    finally:
        release.set()
        await asyncio.gather(first, second)


async def test_process_separate_stderr_preserves_json_and_common_limit() -> None:
    result = await run_process(
        [
            sys.executable,
            "-c",
            "import sys; print('{\"nodes\": []}'); print('warning', file=sys.stderr)",
        ],
        merge_stderr=False,
        limit=100,
    )
    assert json.loads(result["output"]) == {"nodes": []}
    assert result["stderr"] == f"warning{os.linesep}"
    with pytest.raises(ValueError, match="输出超过"):
        await run_process(
            [sys.executable, "-c", "import sys; print('o'*60); print('e'*60, file=sys.stderr)"],
            merge_stderr=False,
            limit=100,
        )


async def test_computer_protocol_uses_stdout_without_warning_pollution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muli.tools import computer as computer_module

    async def subprocess_with_warning(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        assert kwargs["merge_stderr"] is False
        return await run_process(
            [
                sys.executable,
                "-c",
                "import sys; print('{\"nodes\": []}'); print('DeprecationWarning: sample', file=sys.stderr)",
            ],
            **kwargs,
        )

    monkeypatch.setattr(computer_module, "run_process", subprocess_with_warning)
    monkeypatch.setattr(computer_module.sys, "platform", "linux")
    computer = Computer(Settings(computer={"enabled": True}))  # type: ignore[arg-type]
    assert await computer.call({"operation": "snapshot"}) == {"nodes": []}


def test_cancelled_file_worker_finishes_before_cancellation_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        files = Files(Settings(workspace_roots=[str(tmp_path)]), tmp_path / "data")
        started, finished = threading.Event(), threading.Event()

        def execute(_: str, __: dict[str, Any], cancelled: threading.Event | None = None) -> None:
            assert cancelled is not None
            started.set()
            while not cancelled.wait(0.001):
                pass
            finished.set()
            raise InterruptedError("cancelled")

        monkeypatch.setattr(files, "_execute", execute)
        tool = next(tool for tool in files.tools() if tool.name == "fs_search")
        task = asyncio.create_task(tool.handler({"path": str(tmp_path)}))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(scenario())


def test_atspi_identity_rejects_same_label_replacement() -> None:
    class Node:
        path = "/org/a11y/atspi/accessible/new"
        app = SimpleNamespace(bus_name=":1.42")

        def get_name(self) -> str:
            return "确认"

        def get_role_name(self) -> str:
            return "push button"

        def get_process_id(self) -> int:
            return 42

    with pytest.raises(ValueError, match="身份"):
        verify_identity(
            Node(),
            {
                "expected_identity": {
                    "object_path": "/org/a11y/atspi/accessible/old",
                    "bus_name": ":1.42",
                },
                "expected_name": "确认",
                "expected_role": "push button",
                "expected_pid": 42,
            },
        )


def test_concurrent_computer_actions_cannot_reuse_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        computer = Computer(Settings())
        computer.snapshot_id, computer.snapshot_time = "snapshot", time.monotonic()
        computer.nodes = {
            "n0": {
                "name": "确认",
                "role": "button",
                "pid": 42,
                "path": [0],
                "identity": {"object_path": "/button/1", "bus_name": ":1.42"},
                "actions": ["click"],
            }
        }
        calls = 0

        async def call(_: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return {"ok": True}

        monkeypatch.setattr(computer, "call", call)
        arguments = {"snapshot_id": "snapshot", "node_id": "n0", "action": "click"}
        results = await asyncio.gather(
            computer.action(arguments),
            computer.action(arguments),
            return_exceptions=True,
        )
        assert calls == 1
        assert sum(isinstance(item, ValueError) for item in results) == 1

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc verifies descendant cleanup")
def test_process_timeout_kills_descendant_after_parent_exits(tmp_path: Path) -> None:
    async def scenario() -> None:
        pid_file = tmp_path / "child.pid"
        child = "import time; time.sleep(30)"
        parent = "import subprocess,sys,pathlib; child=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
        with pytest.raises(TimeoutError):
            await run_process([sys.executable, "-c", parent, str(pid_file), child], timeout=0.15)
        assert pid_file.exists()
        identifier = int(pid_file.read_text())
        for _ in range(100):
            status = Path(f"/proc/{identifier}/stat")
            if not status.exists() or status.read_text().split()[2] == "Z":
                return
            await asyncio.sleep(0.005)
        pytest.fail("子进程在超时后仍在运行")

    asyncio.run(scenario())
