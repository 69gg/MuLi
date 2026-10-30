from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_runtime import make_runtime
from test_task_briefs import attach_services
from test_task_control import durable_state

from muli.evidence import EvidenceError, json_size
from muli.local_services import LocalServices
from muli.runtime import Runtime
from muli.task_inspection import TaskInspection


@pytest.fixture
async def wait_case(tmp_path: Path) -> AsyncIterator[tuple[Runtime, LocalServices, str]]:
    runtime, _ = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    task = await services.commitments.save(
        "当前等待", brief={"goal": "私密目标不进入等待页", "constraints": []}
    )
    try:
        yield runtime, services, task["id"]
    finally:
        await runtime.close()


def member(
    runtime: Runtime,
    task_id: str,
    identifier: str,
    *,
    parent: str | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    runtime.store.put(
        "session",
        {
            "id": identifier,
            "kind": "team",
            "principal": "owner",
            "mode": "ask",
            "role": "worker",
            "status": "idle",
        },
    )
    return runtime.store.put(
        "agent",
        {
            "id": identifier,
            "session_id": identifier,
            "parent_id": parent or runtime.main_session()["id"],
            "commitment_id": task_id,
            "mode": "ask",
            "role": "worker",
            "status": status,
            "generation": 1,
            "completion_revision": 1,
            "result": "等待主人补充，含私密结果正文",
        },
    )


def read(services: LocalServices, task_id: str, **args: Any) -> dict[str, Any]:
    return services.task_inspection.read(task_id, section="current_wait", **args)


def test_candidate_window_can_be_empty_with_a_real_next_page(
    wait_case: tuple[Runtime, LocalServices, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, services, task_id = wait_case
    member(runtime, "other-task", "a-other")
    member(runtime, task_id, "z-own")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "current_wait cannot enumerate global state or resolve unbounded chains"
        )

    before, changes = durable_state(runtime.store), runtime.store.db.total_changes
    statements: list[str] = []
    runtime.store.db.set_trace_callback(statements.append)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(runtime.store, "get", forbidden)
            patch.setattr(runtime.store, "list", forbidden)
            patch.setattr(services.task_control, "resolve_scope", forbidden)
            patch.setattr(runtime.team, "_parent_stopping", forbidden)
            patch.setattr(runtime.team, "start", forbidden)
            patch.setattr(runtime.team, "cancel_shell_continuation", forbidden)
            first = read(services, task_id, limit=1)
            assert first["items"] == [] and first["omitted"] == 1
            assert first["has_more"] and first["next_cursor"]
            assert "a-other" not in base64.urlsafe_b64decode(first["next_cursor"]).decode()
            second = read(services, task_id, limit=1, cursor=first["next_cursor"])
    finally:
        runtime.store.db.set_trace_callback(None)
    assert [item["agent_id"] for item in second["items"]] == ["z-own"]
    assert second["items"][0]["execution"]["state"] == "none"
    assert second["owner_questions"] == {
        "coverage": "explicit_only",
        "read_arguments": {
            "identifier": task_id,
            "section": "owner_questions",
            "question_status": "open",
        },
    }
    assert not second["has_more"] and second["next_cursor"] is None
    assert "私密" not in json.dumps(second, ensure_ascii=False)
    assert durable_state(runtime.store) == before and runtime.store.db.total_changes == changes
    assert not any("WITH RECURSIVE" in sql or "FROM events" in sql for sql in statements)
    plan = runtime.store.db.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM objects WHERE kind='agent' AND id>? ORDER BY id LIMIT ?",
        ("", 2),
    ).fetchall()
    assert any("SEARCH" in row[3] and "INDEX" in row[3] for row in plan)


@pytest.mark.parametrize("change", ["cycle", "conflict", "missing", "oversized", "hops"])
def test_bad_candidate_lineage_is_unavailable_and_does_not_expose_identity(
    wait_case: tuple[Runtime, LocalServices, str], change: str
) -> None:
    runtime, services, task_id = wait_case
    agent = member(runtime, task_id, "private-child", parent="private-parent")
    parent = member(runtime, task_id, "private-parent")
    if change == "cycle":
        runtime.store.put("agent", {**parent, "parent_id": agent["id"]})
    elif change == "conflict":
        runtime.store.put("agent", {**parent, "commitment_id": "different-private-task"})
    elif change == "missing":
        runtime.store.delete("session", parent["id"])
    elif change == "oversized":
        runtime.store.put("agent", {**parent, "result": "secret" * 50000})
    else:
        runtime.config.update({"limits": {"max_search_results": 1}})
    page = read(services, task_id, limit=1)
    assert page["items"] == [] and page["unavailable"] == 1
    assert page["has_more"] and page["next_cursor"]
    assert "private-child" not in json.dumps({**page, "next_cursor": None})
    assert "different-private-task" not in json.dumps(page)


@pytest.mark.parametrize("change", ["reader", "output", "candidates", "limit", "task"])
def test_wait_cursor_binds_reader_instance_task_and_query_budgets(
    wait_case: tuple[Runtime, LocalServices, str], change: str
) -> None:
    runtime, services, task_id = wait_case
    member(runtime, task_id, "a")
    member(runtime, task_id, "b")
    first = read(services, task_id, limit=1)
    assert first["has_more"]
    inspection = services.task_inspection
    limit = 1
    if change == "reader":
        inspection = TaskInspection(
            services.commitments,
            services.task_control,
            runtime.memory,
            max_handoff_records=lambda: runtime.config.settings.limits.max_search_results,
            current_wait=runtime.inspect_task_wait,
        )
    elif change == "output":
        runtime.config.update(
            {"limits": {"max_output_chars": runtime.config.settings.limits.max_output_chars - 1}}
        )
    elif change == "candidates":
        runtime.config.update({"limits": {"max_search_results": 2}})
    elif change == "limit":
        limit = 2
    else:
        task = services.commitments.get(task_id)
        runtime.store.put("commitment", {**task, "revision": task["revision"] + 1})
    assert inspection.store.instance_id == services.task_inspection.store.instance_id
    with pytest.raises(EvidenceError) as error:
        inspection.read(task_id, section="current_wait", limit=limit, cursor=first["next_cursor"])
    assert error.value.code == "evidence_changed"


async def test_same_database_reopen_rejects_old_wait_cursor(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    task = await services.commitments.save("重开核查", brief={"goal": "只读", "constraints": []})
    member(runtime, task["id"], "a")
    member(runtime, task["id"], "b")
    first = read(services, task["id"], limit=1)
    instance = runtime.store.instance_id
    await runtime.close()
    reopened, _ = make_runtime(tmp_path, [])
    reopened_services = attach_services(reopened, tmp_path)
    try:
        assert reopened.store.instance_id == instance
        with pytest.raises(EvidenceError) as error:
            read(reopened_services, task["id"], limit=1, cursor=first["next_cursor"])
        assert error.value.code == "evidence_changed"
        fresh = read(reopened_services, task["id"], limit=1)
        assert fresh["items"][0]["execution"]["state"] == "none"
    finally:
        await reopened.close()


def test_wait_first_item_must_fit_with_its_cursor(
    wait_case: tuple[Runtime, LocalServices, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, services, task_id = wait_case
    member(runtime, task_id, "a" * 200)
    member(runtime, task_id, "b")
    full = read(services, task_id, limit=1)
    maximum = json_size(full) - 1
    # Task, candidate and session still fit their byte gate; only the full output fails.
    monkeypatch.setattr(services.commitments, "max_output_chars", lambda: maximum)
    with pytest.raises(EvidenceError) as error:
        read(services, task_id, limit=1)
    assert error.value.code == "output_budget"


async def test_registered_wait_tool_uses_bounded_actual_caller_scope(
    wait_case: tuple[Runtime, LocalServices, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, services, task_id = wait_case
    worker = member(runtime, task_id, "worker")
    other = await services.commitments.save(
        "另一任务", brief={"goal": "other-secret", "constraints": []}
    )
    handler = runtime.session_tools(runtime.main_session()["id"])["commitment_inspect"].handler

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("caller scope must use bounded handoff_read_scope")

    token = runtime._tool_session.set(worker["id"])
    try:
        with monkeypatch.context() as patch:
            patch.setattr(services.task_control, "resolve_scope", forbidden)
            own = await handler({"section": "current_wait"})
            with pytest.raises(PermissionError):
                await handler({"identifier": other["id"], "section": "current_wait"})
            runtime.store.put("agent", {**worker, "parent_id": worker["id"]})
            with pytest.raises(EvidenceError):
                await handler({"section": "current_wait"})
        assert own["id"] == task_id
    finally:
        runtime._tool_session.reset(token)


@pytest.mark.parametrize(
    "args", [{"agent_id": "x"}, {"query": "x"}, {"record_id": "x"}, {"length_bytes": 1}]
)
def test_wait_directory_rejects_body_and_search_arguments(
    wait_case: tuple[Runtime, LocalServices, str], args: dict[str, Any]
) -> None:
    _, services, task_id = wait_case
    with pytest.raises(EvidenceError) as error:
        read(services, task_id, **args)
    assert error.value.code == "invalid_arguments"
