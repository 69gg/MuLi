"""实跑当前来源旅程，并证明新格式在旧消费者业务恢复前被拒绝。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import test_workflow_source_journey as journey
from test_channel_reference_format import frozen_e139_reader

import muli.runtime as runtime_module
import muli.workflows as workflows_module
from muli.events import EventBus
from muli.format_admission import CURRENT_FORMAT_STATE, PRE_CHANNEL_REFERENCE_FORMAT_STATE
from muli.format_evidence import read_format_evidence
from muli.providers import Message, ModelTurn
from muli.runtime import Runtime
from muli.store import Store

JsonObject = dict[str, Any]
_FIXTURES = Path(__file__).parent / "fixtures/workflow_source_v2_138"
# 主 agent 从已单独核验 Good 签名的 5186d29 原字节提取；只保留历史源码证据。
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


def test_historical_workflow_consumer_source_evidence_stays_frozen() -> None:
    """共享依赖摘要属于历史 2.139 同格式证明，不据此放行当前 ingress6。"""
    manifest = json.loads(frozen_bytes("manifest.json"))
    assert manifest["baseline_commit"] == "5186d29a0748590d5c16c51823acf8f1d2c8e54f"
    assert manifest["baseline_release"] == "2.138.0"
    assert json.loads(frozen_bytes("compatibility.json"))["release"] == "2.138.0"
    for name, checksum in manifest["frozen_files"].items():
        assert checksum == hashlib.sha256(frozen_bytes(name)).hexdigest()


def object_rows(db: sqlite3.Connection, keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for kind, identifier in keys:
        row = db.execute(
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
    return object_rows(store.db, keys)


def outbox_rows(db: sqlite3.Connection) -> list[JsonObject]:
    return [dict(row) for row in db.execute("SELECT * FROM channel_outbox ORDER BY id")]


@dataclass(frozen=True, slots=True)
class CrashSnapshot:
    run: JsonObject
    source: dict[tuple[str, str], str]
    outbox: list[JsonObject]


@pytest.mark.parametrize("unknown_outbound", [False, True], ids=["ordinary", "unknown-outbound"])
async def test_current_workflow_source_journey_refuses_old_reader_before_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unknown_outbound: bool,
) -> None:
    """真实当前 DAG 的完整备份被旧 admission 拒绝，不构造旧 Runtime 绕门。"""
    snapshot_dir = tmp_path / "unfinished-snapshot"
    snapshot_dir.mkdir()
    snapshot: CrashSnapshot | None = None
    runtimes: list[Runtime] = []
    make_runtime = journey.make_runtime
    drive = Runtime._drive
    emit = EventBus.emit

    def observe_runtime(path: Path, turns: list[ModelTurn]) -> tuple[Runtime, list[list[Message]]]:
        runtime, seen = make_runtime(path, turns)
        assert type(runtime.workflows) is workflows_module.WorkflowEngine
        assert runtime._drive.__func__ is drive
        assert runtime.store.format_state == CURRENT_FORMAT_STATE
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
                outbox=outbox_rows(bus.store.db),
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

    old_reader = frozen_e139_reader()
    assert old_reader.CURRENT_FORMAT_STATE == PRE_CHANNEL_REFERENCE_FORMAT_STATE
    assert CURRENT_FORMAT_STATE.formats.ingress == 6
    database = snapshot_dir / "muli.sqlite3"
    database_before = database.read_bytes()
    config = runtimes[0].config.path
    configuration_before = config.read_bytes()
    effects: list[str] = []

    def no_recovery(_runtime: Runtime) -> None:
        effects.append("recovery")
        raise AssertionError("旧 admission 必须在业务恢复前拒绝新格式")

    def no_model(_client: object, *args: Any, **kwargs: Any) -> None:
        effects.append("model")
        raise AssertionError("旧 admission 不得启动模型或重做普通节点")

    async def no_send(
        _transport: httpx.AsyncHTTPTransport, _request: httpx.Request
    ) -> httpx.Response:
        effects.append("send")
        raise AssertionError("旧 admission 不得重放原 unknown 或发布未收口的新结果")

    monkeypatch.setattr(Runtime, "_recover", no_recovery)
    monkeypatch.setattr(runtime_module.ModelClient, "__init__", no_model)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_send)
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        before = tuple(db.iterdump())
        evidence = read_format_evidence(db)
        assert evidence.state == CURRENT_FORMAT_STATE
        with pytest.raises(old_reader.FormatAdmissionError, match="^store_format_unsupported$"):
            old_reader.require_current_format(evidence)
        assert db.total_changes == 0
        assert tuple(db.iterdump()) == before
        assert object_rows(db, list(snapshot.source)) == snapshot.source
        assert outbox_rows(db) == snapshot.outbox
        row = db.execute(
            "SELECT data FROM objects WHERE kind='workflow_run' AND id=?", (snapshot.run["id"],)
        ).fetchone()
        assert row is not None and json.loads(row["data"]) == snapshot.run
    # 整个备份覆盖业务对象、输入账本、来源和 outbox，保留每个原始持久化字节。
    assert database.read_bytes() == database_before
    assert config.read_bytes() == configuration_before
    assert effects == []
