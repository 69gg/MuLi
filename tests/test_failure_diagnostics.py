from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import FunctionType
from typing import Any

import httpx
import pytest
from test_runtime import make_runtime

import muli.failure_diagnostics as failures
import muli.runtime as runtime_module
from muli.config import ConfigFile
from muli.failure_diagnostics import (
    MAX_FAILURE_BYTES,
    MAX_FAILURE_FRAMES,
    MAX_RUN_RECORD_BYTES,
    capture_failure,
    read_failure,
)
from muli.providers import ModelTurn, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import Store, new_id
from muli.task_control import TaskPaused


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("failure diagnostic tests forbid external effects")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    value = Store(tmp_path / "muli.sqlite3")
    try:
        yield value
    finally:
        value.close()


def diagnostic() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": "muli_package_only",
        "exception_type": "ValueError",
        "frames": [
            {"module": "muli.runtime", "function": "Runtime._drive", "line": 1}
            for _ in range(MAX_FAILURE_FRAMES)
        ],
        "truncated": True,
    }


async def test_runtime_failure_persists_bounded_location_for_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(ConfigFile(tmp_path / "config.json"), tmp_path / "data")
    run_id = new_id()

    async def fail(*args: Any, **kwargs: Any) -> str:
        private_local = "PRIVATE_LOCAL_AND_MESSAGE"
        raise ValueError(private_local)

    monkeypatch.setattr(runtime, "_execute", fail)
    try:
        session = runtime.new_session()
        with pytest.raises(ValueError, match="PRIVATE_LOCAL_AND_MESSAGE"):
            await runtime._drive(session["id"], run_id, "main", "auto")
        run = runtime.store.get("run", run_id)
        assert run is not None and run["status"] == "failed"
        diagnostic = run.get("failure_diagnostic")
        assert diagnostic is not None
        assert "PRIVATE_LOCAL_AND_MESSAGE" not in json.dumps(diagnostic)
        assert any(
            frame["module"] == "muli.runtime" and frame["function"] == "Runtime._drive"
            for frame in diagnostic["frames"]
        )
    finally:
        await runtime.close()
    reopened = Store(tmp_path / "data" / "muli.sqlite3")
    try:
        run = reopened.get("run", run_id)
        assert run is not None and run["failure_diagnostic"] == diagnostic
    finally:
        reopened.close()


def test_capture_never_formats_custom_exception_or_reads_its_attributes() -> None:
    class Hostile(ValueError):
        def __str__(self) -> str:
            raise AssertionError("str must not execute")

        def __repr__(self) -> str:
            raise AssertionError("repr must not execute")

        def __getattribute__(self, name: str) -> Any:
            raise AssertionError("exception attributes must not execute")

    try:
        private_local = "PRIVATE_LOCAL"
        raise Hostile("PRIVATE_MESSAGE", private_local) from ValueError("PRIVATE_CAUSE")
    except Hostile as exc:
        value = capture_failure(exc)
    encoded = json.dumps(value)
    assert value["exception_type"] == "unknown"
    assert value["frames"] == [{"module": "unknown", "function": "unknown", "line": None}]
    assert "PRIVATE" not in encoded and "test_failure" not in encoded
    assert len(encoded.encode("utf-8")) <= MAX_FAILURE_BYTES


def test_walk_is_actually_bounded_without_visiting_the_next_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = failures.walk_tb
    visited = 0

    def recursive(depth: int) -> None:
        if depth:
            recursive(depth - 1)
        raise RuntimeError("PRIVATE_DEEP_MESSAGE")

    def counted(traceback: Any) -> Iterator[Any]:
        nonlocal visited
        for item in original(traceback):
            visited += 1
            assert visited <= MAX_FAILURE_FRAMES
            yield item

    monkeypatch.setattr(failures, "walk_tb", counted)
    try:
        recursive(MAX_FAILURE_FRAMES * 3)
    except RuntimeError as exc:
        value = capture_failure(exc)
    assert visited == MAX_FAILURE_FRAMES
    assert len(value["frames"]) == MAX_FAILURE_FRAMES
    assert value["truncated"] is True
    assert len(json.dumps(value).encode()) <= MAX_FAILURE_BYTES


def test_real_package_filename_and_qualname_cannot_forge_code_ownership() -> None:
    def impostor() -> None:
        raise ValueError("PRIVATE_FORGED_LOCATION")

    code = impostor.__code__.replace(
        co_filename=runtime_module.__file__, co_qualname="Runtime._drive"
    )
    forged = FunctionType(code, vars(runtime_module))
    try:
        forged()
    except ValueError as exc:
        value = capture_failure(exc)
    assert all(frame["module"] == "unknown" for frame in value["frames"])
    assert "PRIVATE" not in json.dumps(value)


def test_exception_group_does_not_expand_children() -> None:
    value = capture_failure(ExceptionGroup("PRIVATE_GROUP", [ValueError("PRIVATE_CHILD")]))
    assert value["exception_type"] == "ExceptionGroup"
    assert value["frames"] == []
    assert "PRIVATE" not in json.dumps(value)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("queued", "run_not_finalized"),
        ("running", "run_not_finalized"),
        ("failed", "diagnostic_not_recorded"),
        ("interrupted", "diagnostic_not_recorded"),
        ("completed", "diagnostic_not_recorded"),
        ("cancelled", "diagnostic_not_recorded"),
        ("paused", "diagnostic_not_recorded"),
    ],
)
def test_legacy_or_not_finalized_is_never_reported_healthy(
    store: Store, status: str, reason: str
) -> None:
    run_id = new_id()
    store.put("run", {"id": run_id, "status": status, "error": "PRIVATE_ERROR"})
    result = read_failure(store, run_id, max_output_chars=1000)
    assert result["status"] == "unavailable" and result["reason"] == reason
    assert "PRIVATE" not in json.dumps(result)
    missing = read_failure(store, new_id(), max_output_chars=1000)
    assert missing["status"] == "not_found" and missing["reason"] == "run_not_found"


@pytest.mark.parametrize("identifier", ["", "a" * 33, "A" * 32, "0' OR 1=1--", 123])
def test_invalid_run_id_rejected_before_sql(store: Store, identifier: Any) -> None:
    queries: list[str] = []
    store.db.set_trace_callback(queries.append)
    with pytest.raises(ValueError, match="failure_run_id_invalid"):
        read_failure(store, identifier, max_output_chars=1000)
    assert queries == []


def test_oversize_original_is_not_parsed_or_loaded_through_store_get(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = new_id()
    store.put("run", {"id": run_id, "status": "failed", "result": "x" * (MAX_RUN_RECORD_BYTES * 2)})
    queries: list[str] = []
    store.db.set_trace_callback(queries.append)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unbounded or over-budget parsing must not execute")

    monkeypatch.setattr(store, "get", forbidden)
    monkeypatch.setattr("muli.management_projection._object", forbidden)
    result = read_failure(store, run_id, max_output_chars=1000)
    assert result["reason"] == "run_record_over_budget"
    assert result["status"] == "unavailable" and "diagnostic" not in result
    assert len(queries) == 1 and "substr(CAST(data AS BLOB)" in queries[0]
    assert "json_extract" not in queries[0] and "WHERE kind='run' AND id=" in queries[0]


@pytest.mark.parametrize(
    "raw", [b"{", b"\xff", b'{"id":1,"id":2}', b'{"n":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"]
)
def test_corrupt_or_deep_original_is_explicit(store: Store, raw: bytes) -> None:
    run_id = new_id()
    # Build legacy damaged evidence without the lifecycle index rejecting the
    # JSON during fixture insertion; the reader must still reject it itself.
    store.db.execute("DROP INDEX objects_unfinished_lifecycle")
    store.db.execute("INSERT INTO objects VALUES('run',?,?,0)", (run_id, raw))
    result = read_failure(store, run_id, max_output_chars=1000)
    assert result["reason"] == "run_record_invalid"


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"exception_type": "PRIVATE_EXCEPTION_NAME"},
        {"frames": [{"module": "/private/path", "function": "unsafe", "line": 1}]},
        {"frames": [{"module": "muli.runtime", "function": "<PRIVATE>", "line": 1}]},
        {"frames": [{"module": "muli.runtime", "function": "Runtime._drive", "line": True}]},
        {
            "frames": [{"module": "unknown", "function": "unknown", "line": None}]
            * (MAX_FAILURE_FRAMES + 1)
        },
        {"error": "PRIVATE_MESSAGE"},
    ],
)
def test_bad_diagnostic_is_not_forwarded(store: Store, changes: dict[str, Any]) -> None:
    run_id = new_id()
    value = {**diagnostic(), **changes}
    store.put("run", {"id": run_id, "status": "failed", "failure_diagnostic": value})
    result = read_failure(store, run_id, max_output_chars=1000)
    assert result["reason"] == "failure_diagnostic_invalid"
    assert "PRIVATE" not in json.dumps(result)


def test_complete_output_budget_and_read_only_evidence(store: Store) -> None:
    run_id = new_id()
    store.put("run", {"id": run_id, "status": "failed", "failure_diagnostic": diagnostic()})
    before = store.db.total_changes, store.event_position()
    result = read_failure(store, run_id, max_output_chars=1000)
    encoded = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "available" and len(encoded) <= 1000
    assert 0 < len(result["diagnostic"]["frames"]) < MAX_FAILURE_FRAMES
    assert result["diagnostic"]["truncated"] is True
    assert json.loads(encoded) == result
    assert (store.db.total_changes, store.event_position()) == before
    run = store.get("run", run_id)
    assert run is not None and len(run["failure_diagnostic"]["frames"]) == MAX_FAILURE_FRAMES
    with pytest.raises(ValueError, match="output_budget"):
        read_failure(store, run_id, max_output_chars=1)


async def test_disabled_collection_keeps_old_diagnostics_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(ConfigFile(tmp_path / "config.json"), tmp_path / "data")

    async def fail(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("PRIVATE_MESSAGE")

    monkeypatch.setattr(runtime, "_execute", fail)
    try:
        session = runtime.new_session()
        first, second = new_id(), new_id()
        with pytest.raises(RuntimeError):
            await runtime._drive(session["id"], first, "main", "auto")
        runtime.config.update({"system_events": {"enabled": False}})
        with pytest.raises(RuntimeError):
            await runtime._drive(session["id"], second, "main", "auto")
        assert read_failure(runtime.store, first, max_output_chars=1000)["status"] == "available"
        assert (
            read_failure(runtime.store, second, max_output_chars=1000)["reason"]
            == "diagnostic_not_recorded"
        )
    finally:
        await runtime.close()


async def test_legal_failed_run_larger_than_diagnostic_cap_remains_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(ConfigFile(tmp_path / "config.json"), tmp_path / "data")
    original = ValueError("\x00" * 1000)

    async def fail(*args: Any, **kwargs: Any) -> str:
        raise original

    monkeypatch.setattr(runtime, "_execute", fail)
    try:
        session = runtime.new_session()
        run_id = new_id()
        with pytest.raises(ValueError) as raised:
            await runtime._drive(session["id"], run_id, "main", "auto")
        assert raised.value is original
        run = runtime.store.get("run", run_id)
        assert run is not None
        size = len(json.dumps(run, ensure_ascii=False).encode("utf-8"))
        assert MAX_FAILURE_BYTES < size < MAX_RUN_RECORD_BYTES
        result = read_failure(runtime.store, run_id, max_output_chars=1000)
        assert result["status"] == "available" and "error" not in result
        assert "\\u0000" not in json.dumps(result)
    finally:
        await runtime.close()


async def test_capture_failure_cannot_replace_original_or_change_failed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(ConfigFile(tmp_path / "config.json"), tmp_path / "data")
    original = ValueError("ORIGINAL_FAILURE")

    async def fail(*args: Any, **kwargs: Any) -> str:
        raise original

    def broken(exc: BaseException) -> Any:
        raise RuntimeError("PRIVATE_CAPTURE_ERROR")

    monkeypatch.setattr(runtime, "_execute", fail)
    monkeypatch.setattr(runtime_module, "capture_failure", broken)
    try:
        session = runtime.new_session()
        run_id = new_id()
        with pytest.raises(ValueError) as raised:
            await runtime._drive(session["id"], run_id, "main", "auto")
        assert raised.value is original
        run = runtime.store.get("run", run_id)
        assert run is not None and run["status"] == "failed" and run["error"] == "ORIGINAL_FAILURE"
        assert "failure_diagnostic" not in run
        events = [event for event in runtime.store.events() if event["type"] == "run.failed"]
        assert len(events) == 1 and events[0]["data"] == {
            "run_id": run_id,
            "error": "ORIGINAL_FAILURE",
        }
    finally:
        await runtime.close()


@pytest.mark.parametrize("paused", [False, True])
async def test_cancel_or_pause_does_not_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paused: bool
) -> None:
    runtime = Runtime(ConfigFile(tmp_path / "config.json"), tmp_path / "data")

    async def stop(*args: Any, **kwargs: Any) -> str:
        if paused:
            raise TaskPaused("test_pause", "正常暂停")
        raise asyncio.CancelledError

    def forbidden(exc: BaseException) -> Any:
        pytest.fail("cancel/pause must not enter failure capture")

    monkeypatch.setattr(runtime, "_execute", stop)
    monkeypatch.setattr(runtime_module, "capture_failure", forbidden)
    try:
        session = runtime.new_session()
        run_id = new_id()
        with pytest.raises(TaskPaused if paused else asyncio.CancelledError):
            await runtime._drive(session["id"], run_id, "main", "auto")
        run = runtime.store.get("run", run_id)
        assert run is not None and "failure_diagnostic" not in run
    finally:
        await runtime.close()


async def test_real_tool_dispatch_preserves_complete_json_for_the_next_request(
    tmp_path: Path,
) -> None:
    run_id = new_id()
    runtime, seen = make_runtime(
        tmp_path,
        [
            ModelTurn(
                tool_calls=[
                    ToolCall("failure-read", "self_failure", json.dumps({"run_id": run_id}))
                ]
            ),
            ModelTurn(content="已读取位置"),
        ],
    )
    runtime.config.update({"limits": {"max_output_chars": 1000}, "memory": {"auto_extract": False}})
    app = create_app(runtime=runtime, config=runtime.config, data_dir=runtime.data_dir)
    runtime.store.put("run", {"id": run_id, "status": "failed", "failure_diagnostic": diagnostic()})
    try:
        tool = runtime.tools["self_failure"]
        assert tool.risk == "read" and tool.preserve_output is True
        session = runtime.new_session(mode="yolo")
        await runtime.start(session["id"], "读取已知失败位置")
        await runtime.tasks[session["id"]]
        message = next(item for item in seen[1] if item.tool_call_id == "failure-read")
        assert message.preserve_content is True
        assert len(message.content) <= 1000
        assert json.loads(message.content)["status"] == "available"
        assert not runtime.store.list("system_log") and not runtime.permissions.pending
        assert len(seen) == 2
        position = runtime.store.event_position()
        await tool.handler({"run_id": run_id})
        assert len(seen) == 2 and runtime.store.event_position() == position
        with pytest.raises(ValueError, match="failure_arguments_invalid"):
            await tool.handler({"run_id": run_id, "path": "forbidden"})
    finally:
        await app.state.close()
