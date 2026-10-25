"""Real task/publication prerequisites before the missing read-only review entry."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_task_control import Rig
from test_task_control import rig as rig
from test_task_control_runtime import wait_agent
from test_task_inspection import read_body
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.auth import hash_password
from muli.config import ConfigFile
from muli.evidence import EvidenceError, json_size
from muli.memory import Memory
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.task_delivery import delivery_prefix
from muli.task_inspection import TaskInspection


@pytest.mark.parametrize("change", ["goal-and-constraints", "reordered-duplicates"])
async def test_changed_requirements_review_reaches_model_after_real_delivery_and_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("变更复核仅允许临时资料与本地ASGI")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, _ = make_runtime(tmp_path, [])
    password = "temporary-change-review"
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "server": {"password_hash": hash_password(password)},
        }
    )
    await runtime.reconfigure()
    main_id, instance_id = runtime.main_session()["id"], runtime.store.instance_id
    old_brief = {
        "goal": "整理模拟错题索引",
        "constraints": ["只用模拟资料", "不发文件", "只用模拟资料"],
    }
    new_brief = {
        "goal": "整理模拟错题原因及补练建议"
        if change == "goal-and-constraints"
        else old_brief["goal"],
        "constraints": (
            ["只用模拟资料", "每题补充错因", "只用模拟资料"]
            if change == "goal-and-constraints"
            else ["不发文件", "只用模拟资料", "只用模拟资料"]
        ),
    }
    judgments = {
        "goal": {"status": "satisfied", "reason": "模型认为草稿已有索引，不代表主人验收。"},
        "constraints": [
            {"index": 0, "status": "satisfied", "reason": "模型按草稿说明判断仅用模拟资料。"},
            {"index": 1, "status": "satisfied", "reason": "本轮没有发送附件。"},
            {"index": 2, "status": "unverified", "reason": "重复要求仍保留独立原索引。"},
        ],
    }
    source = tmp_path / "mistake-index.txt"
    text = "模拟错题索引：第一题计算，第二题读题。尚无逐题错因和补练建议。"
    raw = text.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    replies = {
        "setup": "已登记并派队友整理本地索引。",
        "delivery": "已登记原要求下的交付依据。",
        "edit": "已按主人明确输入修改要求，未重派队友。",
    }
    task_id = worker_id = attachment_id = ""
    phase = "setup"
    active = runtime
    rounds: dict[str, int] = {}
    runs: dict[str, str] = {}
    delivery: dict[str, Any] = {}
    review_page: dict[str, Any] = {}
    model_review: dict[str, Any] = {}
    review_input: list[Message] = []
    answer = ""
    worker_rounds = 0
    worker_entered, release_worker = asyncio.Event(), asyncio.Event()

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal task_id, worker_id, delivery, review_page, model_review, review_input, answer
            turn_number = rounds[phase] = rounds.get(phase, 0) + 1
            runs[phase] = active.run_ids[main_id]
            if phase == "setup":
                if turn_number == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "save-task",
                                "commitment_save",
                                title="模拟错题整理",
                                brief=old_brief,
                            )
                        ]
                    )
                elif turn_number == 2:
                    task_id = tool_result(messages, "save-task")["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-worker",
                                "spawn_agent",
                                task_name="错题索引队友",
                                prompt="只在临时目录写模拟索引并发布副本，不发送文件。",
                                commitment_id=task_id,
                            )
                        ]
                    )
                else:
                    assert turn_number == 3
                    worker_id = tool_result(messages, "spawn-worker")["id"]
                    turn = ModelTurn(content=replies[phase])
            elif phase == "delivery":
                if turn_number == 1:
                    turn = ModelTurn(
                        tool_calls=[call("read-copy", "attachment_read", id=attachment_id)]
                    )
                elif turn_number == 2:
                    assert tool_result(messages, "read-copy") == {
                        "id": attachment_id,
                        "content": text,
                        "truncated": False,
                    }
                    agent = active.team.get(worker_id)
                    report = active.store.get("team_report", agent["run_id"])
                    assert report is not None and report["execution_status"] == "completed"
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "record-delivery",
                                "task_delivery_record",
                                json.dumps(
                                    {
                                        "identifier": task_id,
                                        "observed_revision": 1,
                                        "observed_epoch": 0,
                                        "report_id": report["id"],
                                        "attachment_id": attachment_id,
                                        "idempotency_key": "original-requirements",
                                        "judgments": judgments,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        ]
                    )
                else:
                    assert turn_number == 3
                    delivery = tool_result(messages, "record-delivery")
                    turn = ModelTurn(content=replies[phase])
            elif phase == "edit":
                if turn_number == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "edit-task",
                                "commitment_save",
                                json.dumps(
                                    {
                                        "identifier": task_id,
                                        "title": "模拟错题整理",
                                        "brief": new_brief,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        ]
                    )
                else:
                    assert turn_number == 2
                    assert tool_result(messages, "edit-task")["revision"] == 2
                    turn = ModelTurn(content=replies[phase])
            else:
                assert phase == "review"
                if turn_number == 1:
                    assert any(tool["function"]["name"] == "commitment_inspect" for tool in tools)
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "review-delivery",
                                "commitment_inspect",
                                json.dumps(
                                    {
                                        "identifier": task_id,
                                        "section": "delivery_review",
                                        "record_id": delivery["id"],
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        ]
                    )
                else:
                    assert turn_number == 2
                    review_input = deepcopy(messages)
                    review_page = tool_result(messages, "review-delivery")
                    if "content" not in review_page:
                        # End normally so the missing read contract fails outside
                        # the model, after every real publication prerequisite.
                        answer = "复核工具尚未返回结构化变化，不能据此断言成果可用。"
                    else:
                        assert review_page["has_more"] is False
                        model_review = json.loads(review_page["content"])
                        current = model_review["current"]["brief"]
                        answer = (
                            f"宿主变化事实：当前目标为{current['goal']}；当前限制按顺序为"
                            + "、".join(current["constraints"])
                            + "。模型判断：原索引可作进一步核查的资料，尚不能确认满足新要求；"
                            "需逐项复核，原单判断只针对登记时要求，不是主人验收。"
                        )
                    turn = ModelTurn(content=answer)
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_rounds, attachment_id
            worker_rounds += 1
            if worker_rounds == 1:
                worker_entered.set()
                await release_worker.wait()
                turn = ModelTurn(
                    tool_calls=[call("write-index", "fs_write", path=str(source), content=text)]
                )
            elif worker_rounds == 2:
                assert tool_result(messages, "write-index")["path"] == str(source)
                turn = ModelTurn(
                    tool_calls=[call("publish-index", "file_publish", path=str(source))]
                )
            else:
                assert worker_rounds == 3
                published = tool_result(messages, "publish-index")
                attachment_id = published["id"]
                assert published["sha256"] == digest and published["size"] == len(raw)
                turn = ModelTurn(content="原要求下的模拟索引已发布本地副本。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return (
            Worker([], [])
            if config.model == "worker"
            else Main([], [])
            if config.model == "main"
            else FakeClient([], [])
        )

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert (
                await web.post("/api/auth/login", json={"password": password})
            ).status_code == 200
            started = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": "整理模拟错题索引，只存本地副本。"},
            )
            assert started.status_code == 200
            assert await runtime.wait(started.json()["run_id"]) == replies["setup"]
            await asyncio.wait_for(worker_entered.wait(), 3)
            phase = "delivery"
            release_worker.set()
            finished = await wait_agent(runtime, worker_id, "completed")
            try:
                await eventually(runtime, lambda: bool(completed(runtime, replies["delivery"])))
            except TimeoutError as error:
                run_id = runs.get("delivery", "")
                run = (
                    runtime.store.bounded_object("run", run_id, max_bytes=32768) if run_id else None
                )
                run = run or {}
                failure = run.get("failure_diagnostic")
                failure = failure if isinstance(failure, dict) else {}
                row = runtime.store.db.execute(
                    "SELECT substr(json_extract(o.data,'$.tool_call_id'),1,80),"
                    "substr(CAST(json_extract(o.data,'$.content') AS BLOB),1,300) "
                    "FROM message_order m JOIN objects o ON o.kind='message' AND o.id=m.message_id "
                    "WHERE m.session_id=? AND json_extract(o.data,'$.role')='tool' "
                    "AND json_extract(o.data,'$.content_provenance')='accepted' "
                    "ORDER BY m.seq DESC LIMIT 1",
                    (main_id,),
                ).fetchone()
                diagnostic = {
                    "rounds": dict(rounds),
                    "worker_rounds": worker_rounds,
                    "run_id": run_id,
                    "status": str(run.get("status", ""))[:32],
                    "error": str(run.get("error", ""))[:120],
                    "exception_type": str(failure.get("exception_type", ""))[:80],
                    "last_accepted_call": row[0] if row is not None else None,
                    "last_accepted_result": bytes(row[1] or b"").decode("utf-8", errors="replace")
                    if row is not None
                    else None,
                }
                encoded = json.dumps(diagnostic, ensure_ascii=False)
                if len(encoded.encode("utf-8")) > 1024:
                    # Fixed fallback, never serialize the Store/run/object graph.
                    for field in ("error", "last_accepted_result", "exception_type"):
                        diagnostic[field] = str(diagnostic[field])[:40]
                    encoded = json.dumps(diagnostic, ensure_ascii=True)
                assert len(encoded.encode("utf-8")) <= 1024
                raise AssertionError(encoded) from error
            assert await runtime.wait(runs["delivery"]) == replies["delivery"]
            assert delivery.get("id"), delivery
            assert delivery["brief"] == {"version": 1, **old_brief, "evidence_refs": []}
            assert delivery["model_judgments"] == judgments
            assert delivery["run_id"] == finished["run_id"] and delivery["agent_id"] == worker_id
            proof = runtime.store.get("task_file_publication", attachment_id)
            assert proof is not None and proof["state"] == "recorded" and proof["sha256"] == digest
            assert app.state.attachments.path(attachment_id).read_bytes() == raw
            phase = "edit"
            edited = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={
                    "content": "明确修改此任务完整要求为："
                    + json.dumps(new_brief, ensure_ascii=False)
                },
            )
            assert edited.status_code == 200
            assert await runtime.wait(edited.json()["run_id"]) == replies["edit"]
            task = deepcopy(app.state.local_services.commitments.get(task_id))
            assert task["revision"] == 2 and task["brief"] == {
                "version": 1,
                **new_brief,
                "evidence_refs": [],
            }
            assert task["status"] == "open"
            assert app.state.channel_application.manager.deliveries() == []
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        await runtime.close()

    phase = "review"
    reopened = Runtime(ConfigFile(runtime.config.path), runtime.data_dir, client_factory=factory)  # type: ignore[arg-type]
    active = reopened
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    try:
        async with (
            reopened_app.router.lifespan_context(reopened_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=reopened_app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert (
                reopened.store.instance_id == instance_id
                and reopened.main_session()["id"] == main_id
            )
            frozen_delivery = deepcopy(reopened.store.get("task_delivery", delivery["id"]))
            frozen_agent = deepcopy(reopened.team.get(worker_id))
            assert frozen_delivery == delivery
            assert (
                await web.post("/api/auth/login", json={"password": password})
            ).status_code == 200
            inspected = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={
                    "content": "要求变了，这份成果还能用在哪、还需补什么？核对准确旧单，区分宿主事实与模型判断。"
                },
            )
            assert inspected.status_code == 200
            await reopened.wait(inspected.json()["run_id"])
            assert (
                rounds == {"setup": 3, "delivery": 3, "edit": 2, "review": 2} and worker_rounds == 3
            )
            assert tool_result(review_input, "review-delivery") == review_page
            accepted = [
                message
                for message in reopened.store.messages(main_id)
                if message.get("tool_call_id") == "review-delivery"
            ]
            assert len(accepted) == 1 and accepted[0]["content_provenance"] == "accepted"
            assert json.loads(accepted[0]["content"]) == review_page
            assert reopened_app.state.local_services.commitments.get(task_id) == task
            assert reopened.store.get("task_delivery", delivery["id"]) == frozen_delivery
            assert reopened.team.get(worker_id) == frozen_agent
            assert reopened_app.state.channel_application.manager.deliveries() == []
            # Target red is the current tool's accepted missing-section result,
            # not a fabricated report, missing import or model assertion timeout.
            assert "content" in review_page and "error" not in review_page, review_page
            assert (
                review_page["current"] is False
                and review_page["file_verification"] == "at_record_time"
            )
            assert model_review == {
                "version": 1,
                "commitment_id": task_id,
                "delivery_id": delivery["id"],
                "recorded": {"task_revision": 1, "epoch": 0, "brief": delivery["brief"]},
                "current": {"task_revision": 2, "epoch": 0, "brief": task["brief"]},
                "changes": {
                    "goal": change == "goal-and-constraints",
                    "constraints": True,
                    "evidence_refs": False,
                },
                "host_facts": delivery["host_facts"],
                "historical_model_judgments": judgments,
                "judgment_scope": "recorded_requirements_only",
            }
            assert model_review["host_facts"]["sha256"] == digest
            assert model_review["host_facts"]["verification"] == "at_record_time"
            visible = completed(reopened, answer)
            assert len(visible) == 1 and new_brief["goal"] in visible[0]["content"]
            assert "模型判断" in visible[0]["content"] and "不是主人验收" in visible[0]["content"]
            assert reopened_app.state.attachments.path(attachment_id).read_bytes() == raw
    finally:
        await reopened.close()


@pytest.fixture
def review_reader(rig: Rig) -> tuple[TaskInspection, dict[str, Any]]:
    """Reader-only stored material, not a substitute for the real journey above."""
    task = rig.ledger.get(rig.identifier)
    record: dict[str, Any] = {
        "id": delivery_prefix(rig.identifier) + "a" * 32,
        "version": 1,
        "commitment_id": rig.identifier,
        "task_revision": task["revision"],
        "epoch": 0,
        "agent_id": "reader-fixture-agent",
        "run_id": "reader-fixture-run",
        "generation": 1,
        "completion_revision": 1,
        "brief": deepcopy(task["brief"]),
        "host_facts": {"verification": "at_record_time"},
        "model_judgments": {"goal": {"status": "unverified", "reason": "仅读取夹具"}},
        "recorded_by": {},
        "created_at": 1.0,
    }
    rig.store.put("task_delivery", record)
    return TaskInspection(rig.ledger, rig.control, Memory(rig.store)), record


async def test_progress_only_revision_has_no_requirement_changes(
    rig: Rig, review_reader: tuple[TaskInspection, dict[str, Any]]
) -> None:
    inspection, record = review_reader
    frozen = deepcopy(record)
    await rig.ledger.save("持续任务", identifier=rig.identifier, next_step="核对第二项进度")
    changes = rig.store.db.total_changes
    body = read_body(inspection, rig.identifier, "delivery_review", record_id=record["id"])
    assert body["recorded"] == {"task_revision": 1, "epoch": 0, "brief": frozen["brief"]}
    assert body["current"] == {"task_revision": 2, "epoch": 0, "brief": frozen["brief"]}
    assert body["changes"] == {"goal": False, "constraints": False, "evidence_refs": False}
    assert body["historical_model_judgments"] == frozen["model_judgments"]
    assert body["judgment_scope"] == "recorded_requirements_only"
    assert rig.store.db.total_changes == changes
    assert rig.store.get("task_delivery", record["id"]) == frozen


@pytest.mark.parametrize("side", ["current-task", "historical-delivery"])
def test_review_continuation_binds_both_complete_records(
    rig: Rig, review_reader: tuple[TaskInspection, dict[str, Any]], side: str
) -> None:
    inspection, record = review_reader
    first = inspection.read(
        rig.identifier, section="delivery_review", record_id=record["id"], length_bytes=51
    )
    assert first["has_more"] and json_size(first) <= rig.limits["output"]
    if side == "current-task":
        task = rig.ledger.get(rig.identifier)
        task["revision"] += 1
        task["brief"]["goal"] = "主人更正后的目标"
        rig.store.put("commitment", task)
    else:
        changed = deepcopy(record)
        changed["model_judgments"]["goal"]["reason"] = "持久记录正文已发生变化"
        rig.store.put("task_delivery", changed)
    changes = rig.store.db.total_changes
    with pytest.raises(EvidenceError) as error:
        inspection.read(
            rig.identifier,
            section="delivery_review",
            record_id=record["id"],
            revision=first["revision"],
            record_revision=first["record_revision"],
            offset_bytes=first["next_offset_bytes"],
            length_bytes=51,
        )
    assert error.value.code == "evidence_changed"
    assert rig.store.db.total_changes == changes


@pytest.mark.parametrize(
    "damage", ["oversized-record", "missing-brief-field", "bad-brief-version", "bad-unicode"]
)
def test_review_rejects_unavailable_material_without_writing(
    rig: Rig, review_reader: tuple[TaskInspection, dict[str, Any]], damage: str
) -> None:
    inspection, record = review_reader
    changed = deepcopy(record)
    expected = "content_unavailable"
    if damage == "oversized-record":
        changed["host_facts"]["unexpected"] = "x" * (rig.limits["output"] * 4)
        expected = "output_budget"
    elif damage == "missing-brief-field":
        del changed["brief"]["evidence_refs"]
    elif damage == "bad-brief-version":
        changed["brief"]["version"] = True
    else:
        changed["brief"]["goal"] = "\ud800"
    if damage == "bad-unicode":
        # Persist escaped JSON so SQLite accepts it; the real bounded reader
        # must reject the decoded unpaired surrogate before producing a page.
        rig.store.db.execute(
            "UPDATE objects SET data=? WHERE kind='task_delivery' AND id=?",
            (json.dumps(changed, ensure_ascii=True), record["id"]),
        )
        rig.store.db.commit()
    else:
        rig.store.put("task_delivery", changed)
    frozen = rig.store.get("task_delivery", record["id"])
    changes = rig.store.db.total_changes
    with pytest.raises(EvidenceError) as error:
        inspection.read(rig.identifier, section="delivery_review", record_id=record["id"])
    assert error.value.code == expected
    assert rig.store.db.total_changes == changes
    assert rig.store.get("task_delivery", record["id"]) == frozen


def test_review_preserves_deleted_source_locations_without_reading_source_bodies(
    rig: Rig, review_reader: tuple[TaskInspection, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    inspection, record = review_reader
    references = [
        {"kind": "message", "id": "deleted-message", "session_id": rig.main, "seq": 23},
        {"kind": "attachment", "id": "deleted-attachment"},
    ]
    task = rig.ledger.get(rig.identifier)
    task["brief"]["evidence_refs"] = deepcopy(references)
    record["brief"]["evidence_refs"] = deepcopy(references)
    rig.store.put("commitment", task)
    rig.store.put("task_delivery", record)
    original_get = rig.store.get

    def no_source_body(kind: str, identifier: str) -> dict[str, Any] | None:
        assert kind not in {"message", "attachment"}, "review must not resolve source bodies"
        return original_get(kind, identifier)

    monkeypatch.setattr(rig.store, "get", no_source_body)
    changes = rig.store.db.total_changes
    body = read_body(inspection, rig.identifier, "delivery_review", record_id=record["id"])
    assert body["recorded"]["brief"]["evidence_refs"] == references
    assert body["current"]["brief"]["evidence_refs"] == references
    assert body["changes"]["evidence_refs"] is False
    assert rig.store.db.total_changes == changes
    monkeypatch.setattr(rig.store, "get", original_get)
    with pytest.raises(ValueError, match="不存在或已删除"):
        rig.ledger._validate_brief(task["brief"], stored=True)


@pytest.mark.parametrize(
    "damage", ["not-stored", "bool-seq", "unknown-field", "attachment-location"]
)
def test_declaration_only_validation_cannot_relax_creation_or_reference_shape(
    rig: Rig, damage: str
) -> None:
    brief = deepcopy(rig.ledger.get(rig.identifier)["brief"])
    brief["evidence_refs"] = [
        {"kind": "message", "id": "missing", "session_id": rig.main, "seq": 3}
    ]
    stored = damage != "not-stored"
    if damage == "bool-seq":
        brief["evidence_refs"][0]["seq"] = True
    elif damage == "unknown-field":
        brief["evidence_refs"][0]["body"] = "不得装作规范定位"
    elif damage == "attachment-location":
        brief["evidence_refs"][0]["kind"] = "attachment"
    with pytest.raises(ValueError):
        rig.ledger._validate_brief(brief, stored=stored, resolve_references=False)
