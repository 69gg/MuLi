"""固定签名前序消费者，实跑普通物化 DAG、父成功门和崩溃恢复。"""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from types import FunctionType, ModuleType
from typing import Any

import httpx
import pytest
import test_workflow_source_journey as journey
from test_channels import consumed

import muli.runtime as runtime_module
import muli.workflows as workflows_module
from muli.channel_application import ChannelApplication
from muli.events import EventBus
from muli.providers import Message, ModelConfig, ModelTurn
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import Store

JsonObject = dict[str, Any]
_FIXTURES = Path(__file__).parent / "fixtures/workflow_source_v2_138"
_ROOT = Path(__file__).parents[1]
# 主 agent 从已单独核验 Good 签名的 5186d29 原字节提取；不代表旧发行包运行。
_FROZEN_SHA256: dict[str, str] = {
    "workflows.py": "1e4ed692b78b7657d0a4395e4f44e45ee66b452256afad86cce96f27098cb271",
    "runtime_drive.py.txt": "26c7db72b38316b5ea127ca944ce376f4eb8f25e2aee6cf69866346c62a478bb",
    "compatibility.json": "1ec31684944205f165f23ace36e7723c45c29938d3c81e820684c32db9bc18b2",
    "manifest.json": "78f1a3584a633fd57315fb1424d303961c9f843867e002bf6e7592a437414d86",
}


def frozen_bytes(name: str) -> bytes:
    raw = (_FIXTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _FROZEN_SHA256[name]
    return raw


def runtime_member(raw: bytes, name: str) -> bytes:
    owner = next(node for node in ast.parse(raw).body if getattr(node, "name", None) == "Runtime")
    assert isinstance(owner, ast.ClassDef)
    member = next(node for node in owner.body if getattr(node, "name", None) == name)
    assert isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
    assert member.end_lineno is not None
    first = min([member.lineno, *(item.lineno for item in member.decorator_list)])
    return b"".join(raw.splitlines(keepends=True)[first - 1 : member.end_lineno])


@pytest.fixture
def legacy_consumers(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    manifest = json.loads(frozen_bytes("manifest.json"))
    assert manifest["baseline_commit"] == "5186d29a0748590d5c16c51823acf8f1d2c8e54f"
    assert manifest["baseline_release"] == "2.138.0"
    assert json.loads(frozen_bytes("compatibility.json"))["release"] == "2.138.0"
    for name, checksum in manifest["frozen_files"].items():
        assert checksum == hashlib.sha256(frozen_bytes(name)).hexdigest()
    for path, checksum in manifest["shared_files"].items():
        assert hashlib.sha256((_ROOT / path).read_bytes()).hexdigest() == checksum, path
    current_runtime = Path(runtime_module.__file__).read_bytes()
    for name, checksum in manifest["shared_runtime_members"].items():
        assert hashlib.sha256(runtime_member(current_runtime, name)).hexdigest() == checksum, name

    legacy = ModuleType("muli_workflow_source_v2_138")
    legacy.__file__ = str(_FIXTURES / "workflows.py")
    monkeypatch.setitem(sys.modules, legacy.__name__, legacy)
    exec(compile(frozen_bytes("workflows.py"), legacy.__file__, "exec"), vars(legacy))
    assert legacy.Store is Store and legacy.EventBus is EventBus
    # 只 dedent 原始完整方法，不改写等待、全成功门、发布和 finalizer 分支。
    # 外部名字明确共享当前 runtime 模块对象；相关未改方法已逐原字节核验。
    namespace = vars(runtime_module).copy()
    drive_path = str(_FIXTURES / "runtime_drive.py.txt")
    drive_source = dedent(frozen_bytes("runtime_drive.py.txt").decode())
    exec(compile(drive_source, drive_path, "exec"), namespace)
    drive = namespace["_drive"]
    assert isinstance(drive, FunctionType)
    assert drive.__globals__["_ExecutionResult"] is runtime_module._ExecutionResult
    assert drive.__globals__["now"] is runtime_module.now
    monkeypatch.setattr(workflows_module, "WorkflowEngine", legacy.WorkflowEngine)
    monkeypatch.setattr(Runtime, "_drive", drive)
    return legacy


def object_rows(store: Store, keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for kind, identifier in keys:
        row = store.db.execute(
            "SELECT data FROM objects WHERE kind=? AND id=?", (kind, identifier)
        ).fetchone()
        assert row is not None and isinstance(row["data"], str)
        result[kind, identifier] = row["data"]
    return result


def source_rows(store: Store, source_id: str) -> dict[tuple[str, str], str]:
    source = store.get("workflow_run", source_id)
    assert source is not None and source["status"] == "failed"
    sessions = {node["session_id"] for node in source["nodes"].values()}
    keys = [
        ("workflow_run", source_id),
        ("workflow", source["workflow_id"]),
        ("run", source["parent_run_id"]),
        *[("session", identifier) for identifier in sessions],
        *[("run", run["id"]) for run in store.list("run") if run["session_id"] in sessions],
    ]
    return object_rows(store, keys)


def outbox_rows(store: Store) -> list[JsonObject]:
    return [dict(row) for row in store.db.execute("SELECT * FROM channel_outbox ORDER BY id")]


@dataclass(frozen=True, slots=True)
class CrashSnapshot:
    run: JsonObject
    source: dict[tuple[str, str], str]
    outbox: list[JsonObject]
    event_position: int


@pytest.mark.parametrize("unknown_outbound", [False, True], ids=["ordinary", "unknown-outbound"])
async def test_old_consumers_execute_materialized_graph_and_only_interrupt_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_consumers: ModuleType,
    unknown_outbound: bool,
) -> None:
    """复用真实新写入旅程；观察备份不替换执行器，也不伪造状态或回执。"""
    snapshot_dir = tmp_path / "unfinished-snapshot"
    snapshot_dir.mkdir()
    snapshot: CrashSnapshot | None = None
    runtimes: list[Runtime] = []
    make_runtime = journey.make_runtime
    emit = EventBus.emit

    def observe_runtime(path: Path, turns: list[ModelTurn]) -> tuple[Runtime, list[list[Message]]]:
        runtime, seen = make_runtime(path, turns)
        assert type(runtime.workflows) is legacy_consumers.WorkflowEngine
        assert runtime._drive.__func__.__code__.co_filename == str(
            _FIXTURES / "runtime_drive.py.txt"
        )
        runtimes.append(runtime)
        return runtime, seen

    async def observe_event(
        bus: EventBus, event_type: str, data: JsonObject, session_id: str | None = None
    ) -> JsonObject:
        nonlocal snapshot
        event = await emit(bus, event_type, data, session_id)
        run, node_id = data.get("run"), data.get("node_id")
        if (
            snapshot is None
            and event_type == "workflow.updated"
            and isinstance(run, dict)
            and run.get("status") == "running"
            and node_id is not None
            and run["nodes"][node_id]["status"] == "running"
            and journey.SOURCE_NOTICE in run["nodes"][node_id]["prompt"]
        ):
            evidence = journey.materialized(run["nodes"][node_id]["prompt"])[0]
            assert evidence["run_id"] != run["id"]
            snapshot = CrashSnapshot(
                run=run,
                source=source_rows(bus.store, evidence["run_id"]),
                outbox=outbox_rows(bus.store),
                event_position=bus.store.event_position(),
            )
            with closing(sqlite3.connect(snapshot_dir / "muli.sqlite3")) as destination:
                bus.store.db.backup(destination)
        return event

    monkeypatch.setattr(journey, "make_runtime", observe_runtime)
    monkeypatch.setattr(EventBus, "emit", observe_event)
    # 原旅程真实完成：源失败门、新物化图/父运行成功、新 Web 来源和正常重开。
    await journey.test_workflow_source_full_runtime_journey(tmp_path, monkeypatch, unknown_outbound)
    assert len(runtimes) == 1 and snapshot is not None
    assert snapshot.run["status"] == "running"
    assert set(snapshot.run["nodes"]) == {"draft", "review"}
    assert all(
        node["status"] in {"pending", "queued", "running"}
        for node in snapshot.run["nodes"].values()
    )
    assert snapshot.run["reply_origin"]["kind"] == "web"
    if unknown_outbound:
        assert len(snapshot.outbox) == 1
        assert snapshot.outbox[0]["state"] == "unknown" and snapshot.outbox[0]["attempts"] == 1
    else:
        assert snapshot.outbox == []

    def no_model(_config: ModelConfig) -> journey.FakeClient:
        raise AssertionError("旧恢复不得启动模型或重做普通节点")

    async def no_send(
        _transport: httpx.AsyncHTTPTransport, _request: httpx.Request
    ) -> httpx.Response:
        raise AssertionError("旧恢复不得重放原 unknown 或发布未收口的新结果")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_send)
    recovered = Runtime(runtimes[0].config, snapshot_dir, client_factory=no_model)
    channels: ChannelApplication | None = None
    try:
        assert type(recovered.workflows) is legacy_consumers.WorkflowEngine
        restored = recovered.store.get("workflow_run", snapshot.run["id"])
        assert restored is not None and restored["status"] == "interrupted"
        assert "result_message_id" not in restored
        for key in (
            "id",
            "workflow_id",
            "parent_run_id",
            "parent_session_id",
            "mode",
            "reply_origin",
        ):
            assert restored[key] == snapshot.run[key]
        for identifier, old_node in snapshot.run["nodes"].items():
            node = restored["nodes"][identifier]
            assert node["status"] == "interrupted"
            for key in ("prompt", "session_id", "role", "depends_on"):
                assert node[key] == old_node[key]
            assert recovered.store.get("session", node["session_id"])["status"] == "idle"
        if unknown_outbound:
            app = create_app(recovered.config, recovered.data_dir, recovered)
            channels = app.state.channel_application
            await channels.start()
            await channels.manager._publish()
            await consumed(channels.manager, recovered.store.event_position())
        assert object_rows(recovered.store, list(snapshot.source)) == snapshot.source
        assert outbox_rows(recovered.store) == snapshot.outbox
        assert not recovered.tasks and not recovered.workflows.active_run_ids
        assert not recovered._session_finalizers
        assert not {
            event["type"] for event in recovered.store.events(after=snapshot.event_position)
        } & {"run.started", "tool.started", "workflow.updated", "message.completed"}
    finally:
        try:
            if channels is not None:
                await channels.close()
        finally:
            await recovered.close()
