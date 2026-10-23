from __future__ import annotations

import json
from typing import Any

import pytest
from test_task_control import Rig, durable_state
from test_task_control import rig as rig

from muli.evidence import EvidenceError, json_size
from muli.memory import Memory
from muli.task_inspection import TaskInspection


@pytest.fixture
def inspection(rig: Rig) -> TaskInspection:
    return TaskInspection(rig.ledger, rig.control, Memory(rig.store))


def read_body(
    inspection: TaskInspection,
    identifier: str,
    section: str,
    *,
    record_id: str | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {"section": section, "length_bytes": 257}
    if record_id is not None:
        args["record_id"] = record_id
    page = inspection.read(identifier, **args)
    chunks: list[str] = []
    while True:
        assert json_size(page) <= inspection.commitments.max_output_chars()
        assert page["length_bytes"] == len(page["content"].encode("utf-8"))
        assert page["next_offset_bytes"] == page["offset_bytes"] + page["length_bytes"]
        chunks.append(page["content"])
        if not page["has_more"]:
            assert page["next_offset_bytes"] == page["total_bytes"]
            break
        assert page["next_offset_bytes"] > page["offset_bytes"]
        assert page["next_offset_bytes"] < page["total_bytes"]
        page = inspection.read(
            identifier,
            **args,
            revision=page["revision"],
            record_revision=page["record_revision"],
            offset_bytes=page["next_offset_bytes"],
        )
    return dict(json.loads("".join(chunks)))


def read_directory(
    inspection: TaskInspection, identifier: str, section: str
) -> list[dict[str, Any]]:
    page = inspection.read(identifier, section=section, limit=1)
    items = list(page["items"])
    while page["has_more"]:
        assert page["next_cursor"]
        page = inspection.read(identifier, section=section, limit=1, cursor=page["next_cursor"])
        items.extend(page["items"])
    return items


def test_full_current_task_and_plan_round_trip_as_utf8_pages(
    rig: Rig, inspection: TaskInspection
) -> None:
    rig.limits["output"] = 1400
    task = rig.ledger.get(rig.identifier)
    task["brief"]["goal"] = '完整目标🙂引号"反斜线\\\n控制\x01' * 60
    task["control"] = {
        "epoch": 2,
        "approved_epoch": 2,
        "replan_request_id": "request",
        "plan": {"epoch": 2, "text": "逐条核查中文计划🧭\n" * 80},
    }
    task["future_field"] = {"preserved": "不能遗漏的新字段"}
    rig.store.put("commitment", task)
    result = read_body(inspection, rig.identifier, "task")
    assert result == task
    assert result["brief"]["goal"].endswith("控制\x01")
    assert result["control"]["plan"] == task["control"]["plan"]
    first = inspection.read(rig.identifier, section="task", length_bytes=50)
    raw = json.dumps(task, ensure_ascii=False, sort_keys=True).encode("utf-8")
    middle = raw.index("🙂".encode()) + 1
    with pytest.raises(EvidenceError) as error:
        inspection.read(
            rig.identifier,
            section="task",
            revision=first["revision"],
            record_revision=first["record_revision"],
            offset_bytes=middle,
        )
    assert error.value.code == "invalid_offset"


@pytest.mark.parametrize("change", ["task", "epoch"])
def test_task_changes_invalidate_directory_cursor_and_body_revision(
    rig: Rig, inspection: TaskInspection, change: str
) -> None:
    rig.agent("worker-a")
    rig.agent("worker-b")
    directory = inspection.read(rig.identifier, section="agents", limit=1)
    body = inspection.read(rig.identifier, section="task", length_bytes=50)
    assert directory["has_more"] and body["has_more"]
    task = rig.ledger.get(rig.identifier)
    if change == "task":
        task["brief"]["goal"] = "修正后的目标"
        task["revision"] += 1
    else:
        task["control"] = {"epoch": 1, "approved_epoch": 0, "replan_request_id": None, "plan": None}
    rig.store.put("commitment", task)
    with pytest.raises(EvidenceError) as cursor_error:
        inspection.read(rig.identifier, section="agents", cursor=directory["next_cursor"])
    assert cursor_error.value.code == "evidence_changed"
    with pytest.raises(EvidenceError) as body_error:
        inspection.read(
            rig.identifier,
            section="task",
            revision=body["revision"],
            record_revision=body["record_revision"],
            offset_bytes=body["next_offset_bytes"],
        )
    assert body_error.value.code == "evidence_changed"


async def test_deleted_message_and_attachment_are_missing_without_losing_task(
    rig: Rig, inspection: TaskInspection
) -> None:
    message = rig.store.add_message(rig.main, "user", "原始任务资料")
    rig.store.put("attachment", {"id": "source-file", "name": "依据.txt"})
    task = await rig.ledger.save(
        "有原始依据的任务",
        identifier=rig.identifier,
        brief={
            "goal": "核查依据",
            "constraints": [],
            "evidence_refs": [
                {"kind": "message", "id": message["id"]},
                {"kind": "attachment", "id": "source-file"},
            ],
        },
    )
    available = inspection.read(rig.identifier, section="evidence")
    assert {item["availability"] for item in available["items"]} == {"available"}
    rig.store.delete("message", message["id"])
    rig.store.delete("attachment", "source-file")
    missing = inspection.read(rig.identifier, section="evidence")
    assert [item["availability"] for item in missing["items"]] == ["missing", "missing"]
    assert missing["items"][0]["session_id"] == rig.main
    assert missing["items"][0]["seq"] == message["seq"]
    assert inspection.read(rig.identifier)["id"] == rig.identifier
    assert read_body(inspection, rig.identifier, "task") == task


def test_pending_report_body_is_complete_and_record_changes_reject_old_page(
    rig: Rig, inspection: TaskInspection
) -> None:
    rig.limits["output"] = 1400
    rig.agent()
    report = rig.store.put(
        "team_report",
        {
            "id": "report",
            "agent_id": "worker",
            "commitment_id": rig.identifier,
            "status": "pending",
            "execution_status": "completed",
            "created_at": 1,
            "content": '尚待投递的完整报告🙂\n引号"' * 100,
            "delivery_attempts": 0,
        },
    )
    listing = inspection.read(rig.identifier, section="reports")
    assert listing["items"][0]["status"] == "pending"
    assert "content" not in listing["items"][0]
    assert read_body(inspection, rig.identifier, "reports", record_id="report") == report
    first = inspection.read(rig.identifier, section="reports", record_id="report", length_bytes=100)
    assert first["has_more"]
    rig.store.put("team_report", {**report, "content": "报告已更新，不能拼接旧页"})
    with pytest.raises(EvidenceError) as error:
        inspection.read(
            rig.identifier,
            section="reports",
            record_id="report",
            revision=first["revision"],
            record_revision=first["record_revision"],
            offset_bytes=first["next_offset_bytes"],
        )
    assert error.value.code == "evidence_changed"


def test_mixed_lineage_directories_filter_task_before_pagination(
    rig: Rig, inspection: TaskInspection, monkeypatch: pytest.MonkeyPatch
) -> None:
    for suffix in ("a", "b", "c"):
        rig.agent(f"a-other-{suffix}", commitment="other-task")
        rig.store.put("workflow_run", {"id": f"a-run-{suffix}", "commitment_id": "other-task"})
        rig.store.put("team_report", {"id": f"a-report-{suffix}", "commitment_id": "other-task"})
    rig.agent("z-parent")
    for run_id, parent, session, child in (
        ("z-run-1", "z-parent", "z-session-1", "z-child"),
        ("z-run-2", "z-child", "z-session-2", "z-grandchild"),
    ):
        rig.store.put(
            "workflow_run",
            {
                "id": run_id,
                "parent_session_id": parent,
                "status": "running",
                "nodes": {"private": "节点正文"},
            },
        )
        rig.store.put("session", {"id": session, "kind": "workflow", "workflow_run_id": run_id})
        agent = rig.agent(child, parent=session)
        agent.pop("commitment_id")
        rig.store.put("agent", {**agent, "prompt": "队友提示词正文", "result": "执行结果正文"})
    rig.store.put(
        "team_report",
        {
            "id": "z-inherited-report",
            "agent_id": "z-grandchild",
            "status": "pending",
            "content": "未投递的报告正文",
        },
    )
    rig.store.put(
        "team_report",
        {
            "id": "z-direct-report",
            "commitment_id": rig.identifier,
            "status": "pending",
            "content": "已删除队友仍留存的任务报告",
        },
    )

    def forbid_global_list(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("不能先读取全局目录再过滤任务")

    monkeypatch.setattr(rig.store, "list", forbid_global_list)
    agents = read_directory(inspection, rig.identifier, "agents")
    workflows = read_directory(inspection, rig.identifier, "workflows")
    reports = read_directory(inspection, rig.identifier, "reports")
    assert {item["id"] for item in agents} == {"z-parent", "z-child", "z-grandchild"}
    assert {item["id"] for item in workflows} == {"z-run-1", "z-run-2"}
    assert {item["id"] for item in reports} == {"z-inherited-report", "z-direct-report"}
    assert not {"prompt", "result", "nodes", "content"}.intersection(
        key for item in [*agents, *workflows, *reports] for key in item
    )
    inherited = read_body(inspection, rig.identifier, "reports", record_id="z-inherited-report")
    assert inherited["content"] == "未投递的报告正文"


async def test_directories_do_not_load_agent_session_or_evidence_bodies(
    rig: Rig, inspection: TaskInspection, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.agent()
    message = rig.store.add_message(rig.main, "user", "保留的消息正文")
    rig.store.put("attachment", {"id": "file", "content": "保留的附件正文"})
    await rig.ledger.save(
        "目录不加载正文",
        identifier=rig.identifier,
        brief={
            "goal": "只读目录",
            "constraints": [],
            "evidence_refs": [
                {"kind": "message", "id": message["id"]},
                {"kind": "attachment", "id": "file"},
            ],
        },
    )
    rig.store.put(
        "workflow_run", {"id": "workflow", "parent_session_id": "worker", "nodes": ["正文"]}
    )
    rig.store.put("team_report", {"id": "report", "agent_id": "worker", "content": "报告正文"})
    original_get = rig.store.get

    def metadata_only_get(kind: str, identifier: str) -> dict[str, Any] | None:
        assert kind not in {
            "agent",
            "session",
            "message",
            "attachment",
            "team_report",
            "workflow_run",
        }
        return original_get(kind, identifier)

    monkeypatch.setattr(rig.store, "get", metadata_only_get)
    assert [item["id"] for item in inspection.read(rig.identifier, section="agents")["items"]] == [
        "worker"
    ]
    assert [item["id"] for item in inspection.read(rig.identifier, section="reports")["items"]] == [
        "report"
    ]
    assert [
        item["id"] for item in inspection.read(rig.identifier, section="workflows")["items"]
    ] == ["workflow"]
    evidence = inspection.read(rig.identifier, section="evidence")["items"]
    assert [item["availability"] for item in evidence] == ["available", "available"]


@pytest.mark.parametrize("lineage", ["conflicting_binding", "missing_parent", "cycle"])
def test_invalid_lineage_fails_closed(rig: Rig, inspection: TaskInspection, lineage: str) -> None:
    if lineage == "conflicting_binding":
        rig.agent("foreign-parent", commitment="foreign-task")
        parent = "foreign-parent"
    elif lineage == "missing_parent":
        parent = "deleted-parent"
    else:
        parent = "invalid-worker"
    rig.agent("invalid-worker", parent=parent)
    with pytest.raises(EvidenceError) as error:
        inspection.read(rig.identifier, section="agents")
    assert error.value.code == "evidence_changed"


@pytest.mark.parametrize("query", [None, "", "   "])
def test_memory_inspection_requires_explicit_nonempty_query(
    rig: Rig, inspection: TaskInspection, query: str | None
) -> None:
    inspection.memory.save("最新但无关的记忆")
    with pytest.raises(EvidenceError) as error:
        inspection.read(rig.identifier, section="memories", query=query)
    assert error.value.code == "invalid_arguments"
    assert inspection.read(rig.identifier, section="memories", query="absent_token")["items"] == []


@pytest.mark.parametrize("change", ["edit", "forget"])
def test_memory_candidates_are_not_adopted_and_change_invalidates_cursor(
    rig: Rig, inspection: TaskInspection, change: str
) -> None:
    memory = inspection.memory
    candidates = [memory.save("核查 orchid 资料甲"), memory.save("核查 orchid 资料乙")]
    candidate_ids = sorted(item["id"] for item in candidates)
    memory.save("无关的最新日记")
    task = rig.ledger.get(rig.identifier)
    first = inspection.read(rig.identifier, section="memories", query="orchid", limit=1)
    assert first["has_more"]
    assert first["candidate_usage"] == "unknown"
    assert first["items"][0]["matched_by"] == "lexical"
    assert first["items"][0]["id"] == candidate_ids[0]
    assert first["memory_revision"] == memory.revision
    assert first["candidate_limit"] == memory.max_candidates
    second = inspection.read(
        rig.identifier, section="memories", query="orchid", cursor=first["next_cursor"], limit=1
    )
    assert [item["id"] for item in second["items"]] == candidate_ids[1:]
    assert rig.ledger.get(rig.identifier) == task
    with pytest.raises(EvidenceError) as changed_query:
        inspection.read(
            rig.identifier, section="memories", query="another", cursor=first["next_cursor"]
        )
    assert changed_query.value.code == "evidence_changed"
    if change == "edit":
        memory.edit(candidate_ids[0], content="核查 orchid 资料已经更正")
    else:
        assert memory.delete(candidate_ids[0])
    with pytest.raises(EvidenceError) as error:
        inspection.read(
            rig.identifier, section="memories", query="orchid", cursor=first["next_cursor"]
        )
    assert error.value.code == "evidence_changed"


async def test_tool_rechecks_call_time_scope_for_every_body_page(
    rig: Rig, inspection: TaskInspection
) -> None:
    scope: list[str | None] = [rig.identifier]
    tool = inspection.tool(lambda: scope[0])
    first = await tool.handler({"section": "task", "length_bytes": 50})
    assert first["has_more"]
    resume = {
        "section": "task",
        "identifier": rig.identifier,
        "revision": first["revision"],
        "record_revision": first["record_revision"],
        "offset_bytes": first["next_offset_bytes"],
    }
    scope[0] = "another-task"
    with pytest.raises(PermissionError):
        await tool.handler(resume)
    scope[0] = ""
    with pytest.raises(PermissionError):
        await tool.handler(resume)
    scope[0] = None
    assert (await tool.handler(resume))["id"] == rig.identifier
    with pytest.raises(EvidenceError) as error:
        await tool.handler({"identifier": rig.identifier, "approve": True})
    assert error.value.code == "invalid_arguments"


async def test_read_and_tool_do_not_write_select_replan_or_notify(
    rig: Rig, inspection: TaskInspection, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.replan()
    inspection.memory.save("orchid 候选记忆")
    before = durable_state(rig.store)
    changes = rig.store.db.total_changes
    memory_revision = inspection.memory.revision
    event_revision = rig.ledger.bus.revision

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("核查不允许选择重规划、确认或唤醒")

    for target, attribute in (
        (rig.control, "replan_snapshot"),
        (rig.control, "approve_plan"),
        (rig.control, "owner_control"),
        (rig.ledger.bus, "notify"),
        (rig.ledger.bus, "emit"),
    ):
        monkeypatch.setattr(target, attribute, forbidden)
    tool = inspection.tool(lambda: rig.identifier)
    for section in ("overview", "task", "agents", "reports", "workflows", "evidence"):
        await tool.handler({"section": section})
    candidates = await tool.handler({"section": "memories", "query": "orchid"})
    assert candidates["candidate_usage"] == "unknown"
    assert durable_state(rig.store) == before
    assert rig.store.db.total_changes == changes
    assert inspection.memory.revision == memory_revision
    assert rig.ledger.bus.revision == event_revision


@pytest.mark.parametrize("failure", [False, True])
def test_inspection_savepoint_preserves_outer_uncommitted_writes(
    rig: Rig, inspection: TaskInspection, failure: bool
) -> None:
    assert not rig.store.db.in_transaction
    rig.store.db.execute("BEGIN")
    rig.store._put("inspection-test", {"id": "pending", "value": "外层尚未提交"})
    if failure:
        with pytest.raises(EvidenceError):
            inspection.read(rig.identifier, section="unknown")
    else:
        assert inspection.read(rig.identifier)["id"] == rig.identifier
    assert rig.store.db.in_transaction
    assert rig.store.get("inspection-test", "pending") == {"id": "pending", "value": "外层尚未提交"}
    rig.store.db.rollback()
    assert rig.store.get("inspection-test", "pending") is None
