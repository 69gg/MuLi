from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_runtime import make_runtime
from test_task_control import Rig, durable_state
from test_task_control import rig as rig

from muli.attachments import Attachments
from muli.evidence import EvidenceError, json_size
from muli.history_evidence import HistoryEvidence
from muli.local_services import LocalServices
from muli.memory import Memory
from muli.task_inspection import TaskInspection


@pytest.fixture
def inspection(rig: Rig) -> TaskInspection:
    return TaskInspection(rig.ledger, rig.control, Memory(rig.store))


def message(rig: Rig, identifier: str, **fields: Any) -> dict[str, Any]:
    return rig.store.put(
        "message",
        {
            "id": identifier,
            "session_id": "worker",
            "role": "user",
            "content": "原始资料正文不能默认混入接办包",
            **fields,
        },
    )


def execution(rig: Rig) -> None:
    agent = rig.agent(status="failed")
    rig.store.put("agent", {**agent, "run_id": "current-run"})
    rig.store.put("run", {"id": "current-run", "session_id": "worker", "status": "failed"})
    rig.store.put(
        "team_report",
        {
            "id": "current-run",
            "agent_id": "worker",
            "parent_id": rig.main,
            "commitment_id": rig.identifier,
            "status": "pending",
            "execution_status": "failed",
            "content": "工具结果不能证明远端操作成功",
        },
    )


def read(inspection: TaskInspection, rig: Rig, **kwargs: Any) -> dict[str, Any]:
    return inspection.read(rig.identifier, section="handoff", agent_id="worker", **kwargs)


def test_package_is_metadata_and_original_sequence_is_readable(
    rig: Rig,
    inspection: TaskInspection,
) -> None:
    execution(rig)
    stored = message(rig, "source", attachment_ids=["legacy-opaque", "legacy-opaque"])
    before, writes = durable_state(rig.store), rig.store.db.total_changes
    package = read(inspection, rig)
    assert durable_state(rig.store) == before and rig.store.db.total_changes == writes
    assert package["source_completeness"] == "recent_window_only"
    assert package["consistency"] == "live"
    assert package["execution"]["side_effect_status"] == "unknown"
    assert package["execution"]["run"]["id"] == "current-run"
    assert package["execution"]["report"]["execution_status"] == "failed"
    assert "原始资料正文" not in json.dumps(package, ensure_ascii=False)
    assert "远端操作成功" not in json.dumps(package, ensure_ascii=False)
    item = package["items"][0]
    assert item["attachment_ids"] == ["legacy-opaque", "legacy-opaque"]
    page = HistoryEvidence(rig.store).read_content(
        **item["read"],
        max_output_chars=rig.limits["output"],
    )
    assert page["content"] == stored["content"]
    assert package["task_read"]["section"] == "task"


def test_report_locator_rejects_content_changed_after_package(
    rig: Rig,
    inspection: TaskInspection,
) -> None:
    execution(rig)
    locator = read(inspection, rig)["execution"]["report_read"]
    original = inspection.read(**locator)
    assert "工具结果不能证明远端操作成功" in original["content"]
    report = rig.store.get("team_report", "current-run")
    assert report is not None
    rig.store.put("team_report", {**report, "content": "报告已更正，旧定位不可继续使用"})
    with pytest.raises(EvidenceError) as error:
        inspection.read(**locator)
    assert error.value.code == "evidence_changed"


@pytest.mark.parametrize(
    "change",
    [
        "task",
        "epoch",
        "agent",
        "parent",
        "run",
        "report",
        "report-delete",
        "report-created",
        "message",
        "message-delete",
        "message-append",
        "memory",
    ],
)
def test_revision_binds_current_facts_not_just_task_timestamp(
    rig: Rig,
    inspection: TaskInspection,
    change: str,
) -> None:
    execution(rig)
    message(rig, "source")
    if change == "report-created":
        rig.store.delete("team_report", "current-run")
    first = read(inspection, rig)
    if change in {"task", "epoch"}:
        task = rig.ledger.get(rig.identifier)
        if change == "task":
            task["brief"]["goal"] = "只交草稿，不得发布"
        else:
            task["control"] = {
                "epoch": 1,
                "approved_epoch": 0,
                "replan_request_id": None,
                "plan": None,
            }
        rig.store.put("commitment", task)
    elif change == "agent":
        rig.store.put("agent", {**rig.store.get("agent", "worker"), "generation": 4})
    elif change == "parent":
        rig.store.put("session", {**rig.store.get("session", rig.main), "new_field": "changed"})
    elif change == "run":
        rig.store.put("run", {**rig.store.get("run", "current-run"), "status": "completed"})
    elif change == "report":
        rig.store.put(
            "team_report", {**rig.store.get("team_report", "current-run"), "content": "改正文"}
        )
    elif change == "report-delete":
        rig.store.delete("team_report", "current-run")
    elif change == "report-created":
        rig.store.put(
            "team_report",
            {
                "id": "current-run",
                "agent_id": "worker",
                "parent_id": rig.main,
                "commitment_id": rig.identifier,
                "status": "pending",
                "execution_status": "failed",
            },
        )
    elif change == "message":
        message(rig, "source", content="最新资料")
    elif change == "message-delete":
        rig.store.delete("message", "source")
    elif change == "message-append":
        message(rig, "new-tail")
    else:
        inspection.memory.save("更正后的事实", source="owner")
    with pytest.raises(EvidenceError) as error:
        read(inspection, rig, revision=first["revision"])
    assert error.value.code == "evidence_changed"


def test_bad_mappings_consume_raw_window_and_never_backfill(
    rig: Rig,
    inspection: TaskInspection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.agent()
    message(rig, "older-good")
    rig.store.db.execute(
        "INSERT INTO message_order(message_id,session_id) VALUES(?,?)", ("missing", "worker")
    )
    rig.store.put(
        "message", {"id": "foreign", "session_id": rig.main, "role": "user", "content": "other"}
    )
    rig.store.db.execute("UPDATE message_order SET session_id='worker' WHERE message_id='foreign'")
    rig.store.db.commit()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("handoff cannot enumerate global objects, events or valid history")

    monkeypatch.setattr(rig.store, "list", forbidden)
    monkeypatch.setattr(rig.store, "events", forbidden)
    monkeypatch.setattr(rig.store, "message_evidence_max_seq", forbidden)
    monkeypatch.setattr(rig.control, "resolve_scope", forbidden)
    statements: list[str] = []
    rig.store.db.set_trace_callback(statements.append)
    try:
        package = read(inspection, rig, limit=2)
    finally:
        rig.store.db.set_trace_callback(None)
    assert [item["id"] for item in package["items"]] == ["foreign", "missing"]
    assert all(item["availability"] == "missing" for item in package["items"])
    assert package["window"]["has_older"] and not package["has_more"]
    assert not any("WITH RECURSIVE" in sql or "FROM events" in sql for sql in statements)
    plan = rig.store.db.execute(
        "EXPLAIN QUERY PLAN SELECT seq,message_id FROM message_order "
        "WHERE session_id=? ORDER BY seq DESC LIMIT ?",
        ("worker", 3),
    ).fetchall()
    assert any("message_order_session" in row[3] for row in plan)


@pytest.mark.parametrize(
    "expression,identifier",
    [
        pytest.param("?", b"blob-id", id="blob"),
        pytest.param("?", b"", id="empty-blob"),
        pytest.param("?", "", id="empty-text"),
        pytest.param("CAST(? AS TEXT)", b"\x80", id="invalid-utf8-text"),
    ],
)
def test_invalid_mapping_identity_is_unavailable_without_backfill(
    rig: Rig,
    inspection: TaskInspection,
    expression: str,
    identifier: str | bytes,
) -> None:
    rig.agent()
    message(rig, "older-good")
    inserted = rig.store.db.execute(
        f"INSERT INTO message_order(message_id,session_id) VALUES({expression},?)",
        (identifier, "worker"),
    )
    seq = inserted.lastrowid
    rig.store.db.commit()
    writes = rig.store.db.total_changes

    package = read(inspection, rig, limit=1)

    assert package["items"] == [{"id": None, "seq": seq, "availability": "unavailable"}]
    assert package["window"] == {
        "limit": 1,
        "through_seq": seq,
        "oldest_seq": seq,
        "has_older": True,
    }
    assert read(inspection, rig, limit=1, revision=package["revision"]) == package
    assert rig.store.db.total_changes == writes


@pytest.mark.parametrize("corruption", ["json", "attachment", "oversize-attachment"])
def test_damaged_metadata_is_counted_without_returning_partial_references(
    rig: Rig,
    inspection: TaskInspection,
    corruption: str,
) -> None:
    rig.agent()
    message(rig, "older")
    message(rig, "broken")
    if corruption == "json":
        rig.store.db.execute("UPDATE objects SET data='{' WHERE kind='message' AND id='broken'")
    else:
        payload: Any = ["\ud800"] if corruption == "attachment" else ["x" * 20000]
        rig.store.db.execute(
            "UPDATE objects SET data=json_set(data,'$.attachment_ids',json(?)) "
            "WHERE kind='message' AND id='broken'",
            (json.dumps(payload),),
        )
    rig.store.db.commit()
    package = read(inspection, rig, limit=1)
    assert package["window"]["has_older"]
    assert len(package["items"]) == 1
    assert package["items"][0]["availability"] == "unavailable"
    assert "attachment_ids" not in package["items"][0]


@pytest.mark.parametrize(
    "kind,identifier",
    [
        ("commitment", None),
        ("agent", "worker"),
        ("session", "worker"),
        ("session", "main"),
        ("run", "current-run"),
        ("team_report", "current-run"),
    ],
)
def test_complete_objects_are_sql_bounded_before_json_decode(
    rig: Rig,
    inspection: TaskInspection,
    kind: str,
    identifier: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution(rig)
    identifier = (
        rig.identifier if identifier is None else rig.main if identifier == "main" else identifier
    )
    item = rig.store.get(kind, identifier)
    assert item is not None
    rig.store.put(kind, {**item, "oversize": "BODY_MUST_NOT_DECODE" * 2000})
    original = json.loads

    def bounded_loads(value: Any, *args: Any, **kwargs: Any) -> Any:
        assert "BODY_MUST_NOT_DECODE" not in str(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", bounded_loads)
    with pytest.raises(EvidenceError) as error:
        read(inspection, rig)
    assert error.value.code == "output_budget"


@pytest.mark.parametrize(
    "kind,identifier",
    [
        ("commitment", None),
        ("agent", "worker"),
        ("session", "worker"),
        ("session", "main"),
        ("run", "current-run"),
        ("team_report", "current-run"),
    ],
)
def test_invalid_utf8_complete_object_has_a_fixed_evidence_error(
    rig: Rig,
    inspection: TaskInspection,
    kind: str,
    identifier: str | None,
) -> None:
    execution(rig)
    identifier = (
        rig.identifier if identifier is None else rig.main if identifier == "main" else identifier
    )
    item = rig.store.get(kind, identifier)
    assert item is not None
    raw = json.dumps(item).encode("utf-8")[:-1] + b',"invalid_encoding":"\x80"}'
    rig.store.db.execute(
        "UPDATE objects SET data=CAST(? AS TEXT) WHERE kind=? AND id=?",
        (raw, kind, identifier),
    )
    rig.store.db.commit()
    with pytest.raises(EvidenceError) as error:
        read(inspection, rig)
    assert error.value.code == "content_unavailable"


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_complete_object_does_not_autodetect_alternate_json_encodings(
    rig: Rig,
    inspection: TaskInspection,
    encoding: str,
) -> None:
    execution(rig)
    item = rig.store.get("commitment", rig.identifier)
    assert item is not None
    rig.store.db.execute(
        "UPDATE objects SET data=CAST(? AS TEXT) WHERE kind='commitment' AND id=?",
        (json.dumps(item).encode(encoding), rig.identifier),
    )
    rig.store.db.commit()
    with pytest.raises(EvidenceError) as error:
        read(inspection, rig)
    assert error.value.code == "content_unavailable"


@pytest.mark.parametrize("corrupt", ["run-session", "report-agent", "report-parent", "report-task"])
def test_current_execution_cannot_borrow_another_scope(
    rig: Rig,
    inspection: TaskInspection,
    corrupt: str,
) -> None:
    execution(rig)
    kind, key = {
        "run-session": ("run", "session_id"),
        "report-agent": ("team_report", "agent_id"),
        "report-parent": ("team_report", "parent_id"),
        "report-task": ("team_report", "commitment_id"),
    }[corrupt]
    rig.store.put(kind, {**rig.store.get(kind, "current-run"), key: "foreign"})
    with pytest.raises(EvidenceError) as error:
        read(inspection, rig)
    assert error.value.code == "evidence_not_found"


def test_queued_previous_success_and_random_paused_report_do_not_prove_current_execution(
    rig: Rig,
    inspection: TaskInspection,
) -> None:
    execution(rig)
    agent = rig.store.get("agent", "worker")
    rig.store.put("agent", {**agent, "status": "queued"})
    package = read(inspection, rig)
    assert package["execution"]["availability"] == "historical"
    assert package["execution"]["run"] is None and package["execution"]["report"] is None
    rig.store.put("agent", {**agent, "status": "paused", "run_id": None})
    package = read(inspection, rig)
    assert package["execution"]["availability"] == "unknown"
    assert package["execution"]["report"] is None


def test_configured_parent_budget_is_independent_of_requested_window(
    rig: Rig,
    inspection: TaskInspection,
) -> None:
    rig.agent()
    message(rig, "source")
    assert read(inspection, rig, limit=1)["items"]
    inspection.max_handoff_records = lambda: 1
    with pytest.raises(EvidenceError) as error:
        read(inspection, rig, limit=1)
    assert error.value.code == "output_budget"


def test_mixed_workflow_lineage_is_versioned_without_enumerating_other_tasks(
    rig: Rig,
    inspection: TaskInspection,
) -> None:
    rig.store.put(
        "session",
        {
            "id": "workflow-session",
            "kind": "workflow",
            "workflow_id": "definition",
            "workflow_run_id": "workflow-run",
        },
    )
    rig.store.put(
        "workflow_run",
        {
            "id": "workflow-run",
            "parent_session_id": rig.main,
            "commitment_id": rig.identifier,
            "status": "completed",
        },
    )
    agent = rig.agent(parent="workflow-session")
    rig.store.put("agent", {**agent, "commitment_id": None})
    first = read(inspection, rig)
    workflow = rig.store.get("workflow_run", "workflow-run")
    rig.store.put("workflow_run", {**workflow, "status": "failed"})
    with pytest.raises(EvidenceError) as error:
        read(inspection, rig, revision=first["revision"])
    assert error.value.code == "evidence_changed"
    rig.store.put("workflow_run", {**workflow, "parent_session_id": "worker"})
    with pytest.raises(EvidenceError) as error:
        read(inspection, rig)
    assert error.value.code == "evidence_changed"


def test_handoff_does_not_decode_native_blocks_or_large_message_body(
    rig: Rig,
    inspection: TaskInspection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.agent()
    marker = "MESSAGE_BODY_MUST_NOT_LOAD"
    message(rig, "huge-source", content=marker * 5000, native_blocks=[{"text": marker * 5000}])
    original = json.loads

    def bounded_loads(value: Any, *args: Any, **kwargs: Any) -> Any:
        assert marker not in str(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", bounded_loads)
    package = read(inspection, rig)
    assert package["items"][0]["availability"] == "available"
    assert marker not in str(package)


@pytest.mark.parametrize(
    "arguments",
    [
        {"agent_id": None},
        {"agent_id": ""},
        {"query": "资料"},
        {"record_id": "worker"},
        {"length_bytes": 10},
        {"offset_bytes": True},
        {"offset_bytes": False},
    ],
)
def test_handoff_rejects_body_or_memory_parameters(
    rig: Rig,
    inspection: TaskInspection,
    arguments: dict[str, Any],
) -> None:
    rig.agent()
    with pytest.raises(EvidenceError) as error:
        inspection.read(rig.identifier, **{"section": "handoff", "agent_id": "worker", **arguments})
    assert error.value.code == "invalid_arguments"


def test_window_pagination_and_version_ignore_unobserved_older_body(
    rig: Rig,
    inspection: TaskInspection,
) -> None:
    rig.agent()
    for index in range(12):
        message(rig, str(index), attachment_ids=["opaque" + "x" * 180] * 3)
    rig.limits["output"] = 2800
    first = read(inspection, rig, limit=8)
    assert first["has_more"] and first["window"]["has_older"]
    message(rig, "0", content="窗口外更新不在版本保证内")
    package = first
    items = list(package["items"])
    while package["has_more"]:
        package = read(inspection, rig, limit=8, cursor=package["next_cursor"])
        assert package["revision"] == first["revision"]
        assert json_size(package) <= rig.limits["output"]
        items.extend(package["items"])
    assert [item["id"] for item in items] == [str(index) for index in reversed(range(4, 12))]
    message(rig, "11", content="窗口内变化")
    with pytest.raises(EvidenceError) as error:
        read(inspection, rig, limit=8, cursor=first["next_cursor"])
    assert error.value.code == "evidence_changed"


async def test_real_local_handler_is_read_only_in_outer_transaction_and_bounds_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    services = LocalServices(
        runtime, Attachments(runtime.store, tmp_path / "attachments", runtime.config)
    )
    task = await services.commitments.save("只核查，不接班")
    main = runtime.store.existing_main_session_id()
    assert main is not None
    runtime.store.put("session", {"id": "worker", "kind": "team"})
    runtime.store.put(
        "agent",
        {
            "id": "worker",
            "session_id": "worker",
            "parent_id": main,
            "commitment_id": task["id"],
            "status": "paused",
        },
    )
    token = runtime._tool_session.set(main)
    try:

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("new handler cannot enter unbounded legacy scope or task reads")

        monkeypatch.setattr(services.task_control, "resolve_scope", forbidden)
        monkeypatch.setattr(services.commitments, "get", forbidden)
        handler = runtime.tools["commitment_inspect"].handler
        runtime.store.db.execute("BEGIN")
        runtime.store.db.execute(
            "INSERT INTO runtime_state(name,value) VALUES('test-uncommitted','x')"
        )
        result = await handler(
            {"identifier": task["id"], "section": "handoff", "agent_id": "worker"}
        )
        assert result["agent"]["id"] == "worker"
        assert runtime.store.db.in_transaction
        runtime.store.db.rollback()
        assert (
            runtime.store.db.execute(
                "SELECT 1 FROM runtime_state WHERE name='test-uncommitted'"
            ).fetchone()
            is None
        )
        runtime._tool_session.set("worker")
        runtime.config.settings.limits.max_search_results = 1
        with pytest.raises(EvidenceError) as error:
            await handler({"section": "handoff", "agent_id": "worker"})
        assert error.value.code == "output_budget"
    finally:
        runtime._tool_session.reset(token)
        await runtime.close()
