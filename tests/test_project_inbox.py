from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, ValidationError
from test_attention import attention_inputs, eventually, offline_runtime, reminder
from test_file_inbox import FakeWatch
from test_file_inbox_api import inbox_api as inbox_api

import muli.file_inbox as file_inbox_module
from muli.attention import AttentionInbox, _ProjectTaskSnapshotChanged
from muli.auth import hash_password
from muli.commitments import Commitments
from muli.evidence import EvidenceError
from muli.file_inbox import (
    _NAME_BYTES,
    _OBJECT_BYTES,
    BINDING_KIND,
    EVENT_TYPE,
    POLICY_KIND,
    RECORD_KIND,
    SETTINGS_ID,
    SETTINGS_KIND,
    FileInbox,
    FileInboxError,
    FileInboxSettings,
    _checked_settings,
    _signature,
    read_record,
)
from muli.platforms.file_watch import FileSignal
from muli.proactive import FrozenRouting
from muli.project_inbox import (
    WEB_PROJECT_ROUTING,
    ProjectInboxBatch,
    ProjectInboxProof,
    ProjectTaskSnapshot,
    project_batch_current,
    read_task_snapshot,
)
from muli.runtime import Runtime
from muli.store import SQLITE_MAX_INTEGER


async def test_authenticated_settings_bind_existing_task(
    inbox_api: tuple[FastAPI, Runtime, httpx.AsyncClient], tmp_path: Path
) -> None:
    app, runtime, client = inbox_api
    directory = tmp_path / "project-inbox"
    directory.mkdir()
    task = await app.state.local_services.commitments.save(
        "项目资料复核",
        brief={"goal": "对照完整要求复核新资料", "constraints": ["只读，不外发"]},
    )
    runtime.config.update({"server": {"password_hash": hash_password("project-inbox-test")}})
    login = await client.post("/api/auth/login", json={"password": "project-inbox-test"})
    assert login.status_code == 200, login.text
    changes = {"enabled": True, "path": str(directory)}
    generic = await client.put("/api/file-inbox/settings", json=changes)
    assert generic.status_code == 200, generic.text

    bound = await client.put("/api/file-inbox/settings", json={**changes, "project_id": task["id"]})
    assert bound.status_code == 200, bound.text
    assert bound.json()["desired"]["project_id"] == task["id"]


class LegacyFileInboxSettings(BaseModel):
    """The original strict, complete settings shape, before project_id existed."""

    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    path: str
    coalesce_seconds: float
    candidate_timeout_seconds: float
    max_candidates: int
    max_records: int
    window_seconds: float
    max_notices_per_window: int


@dataclass
class ProjectCase:
    runtime: Runtime
    source: FileInbox
    attention: AttentionInbox
    ledger: Commitments
    directory: Path
    task: dict[str, Any]


@pytest.fixture
async def project_case(tmp_path: Path) -> AsyncIterator[ProjectCase]:
    runtime = offline_runtime(tmp_path)
    runtime.config.update({"workspace_roots": [str(tmp_path)]})
    ledger = Commitments(runtime.store, runtime.bus)
    task = await ledger.save(
        "明确绑定的资料复核",
        brief={"goal": "完整要求正文不进入收件事件", "constraints": ["只读复核"]},
    )
    directory = tmp_path / "inbox"
    directory.mkdir()
    source = FileInbox(runtime, watch_factory=FakeWatch)
    source.settings_store.replace(
        FileInboxSettings(
            enabled=True, path=str(directory), project_id=task["id"], coalesce_seconds=0
        )
    )
    attention = AttentionInbox(runtime, file_inbox_settings=source.settings_state)
    case = ProjectCase(runtime, source, attention, ledger, directory, task)
    try:
        await source.start()
        assert source.status()["running"]
        yield case
    finally:
        await attention.close()
        await source.close()
        await runtime.close()


async def observed(case: ProjectCase, name: str = "new.txt") -> dict[str, Any]:
    (case.directory / name).write_text("新正文只允许工具按需读取", encoding="utf-8")
    await case.source.ingest([FileSignal("moved_to", name)])

    def record() -> dict[str, Any] | None:
        return next(
            (
                item
                for item in case.runtime.store.list(RECORD_KIND)
                if item["kind"] == "candidate" and item["metadata"]["name"] == name
            ),
            None,
        )

    await eventually(lambda: record() is not None)
    result = record()
    assert result is not None
    return result


def canonical_event(case: ProjectCase, record: dict[str, Any]) -> dict[str, Any]:
    event = case.runtime.store.events(record["event_id"] - 1, limit=1)[0]
    assert event["id"] == record["event_id"] and event["type"] == EVENT_TYPE
    return event


def batch_for(case: ProjectCase, record: dict[str, Any]) -> ProjectInboxBatch:
    proof = ProjectInboxProof.load(record["project_proof"])
    snapshot = read_task_snapshot(case.runtime.store, case.task["id"], max_bytes=16_384)
    assert proof is not None and snapshot is not None
    return ProjectInboxBatch((proof,), snapshot)


async def test_binding_rejects_missing_incomplete_and_ended_tasks_atomically(
    project_case: ProjectCase,
) -> None:
    case = project_case
    incomplete = await case.ledger.save("没有完整任务说明")
    done = await case.ledger.save(
        "已完成", status="done", evidence="验收已完成", brief={"goal": "旧目标", "constraints": []}
    )
    cancelled = await case.ledger.save(
        "已取消",
        status="cancelled",
        evidence="主人取消",
        brief={"goal": "旧目标", "constraints": []},
    )
    before = case.runtime.store.get(SETTINGS_KIND, SETTINGS_ID)
    binding = case.runtime.store.get(BINDING_KIND, SETTINGS_ID)
    position = case.runtime.store.event_position()
    for project_id in ("missing", incomplete["id"], done["id"], cancelled["id"]):
        with pytest.raises(FileInboxError, match="file_inbox_project_unavailable"):
            case.source.settings_store.replace(
                case.source.desired_settings().model_copy(update={"project_id": project_id})
            )
        assert case.runtime.store.get(SETTINGS_KIND, SETTINGS_ID) == before
        assert case.runtime.store.get(BINDING_KIND, SETTINGS_ID) == binding
        assert case.runtime.store.event_position() == position


async def test_binding_uses_exact_existing_identifier_and_paused_task_snapshot(
    project_case: ProjectCase,
) -> None:
    case = project_case
    project_id = "明确项目:保留原ID"
    case.runtime.store.put(
        "commitment",
        {
            **case.task,
            "id": project_id,
            "control": {"epoch": 2, "approved_epoch": 1, "replan_request_id": None, "plan": None},
        },
    )
    desired = case.source.desired_settings().model_copy(update={"project_id": project_id})
    case.source.settings_store.replace(desired)
    await case.source.reconfigure()
    record = await observed(case)
    proof = ProjectInboxProof.load(record["project_proof"])
    assert proof is not None and proof.project_id == project_id
    snapshot = read_task_snapshot(case.runtime.store, project_id, max_bytes=16_384)
    assert snapshot == ProjectTaskSnapshot(project_id, 1, "open", 2, 1)
    assert case.runtime.store.get("commitment", project_id)["control"]["approved_epoch"] == 1


async def test_binding_and_settings_roll_back_in_one_transaction(project_case: ProjectCase) -> None:
    case = project_case
    before = case.runtime.store.get(SETTINGS_KIND, SETTINGS_ID)
    binding = case.runtime.store.get(BINDING_KIND, SETTINGS_ID)
    position = case.runtime.store.event_position()
    case.runtime.store.db.execute(
        "CREATE TRIGGER reject_project_binding BEFORE INSERT ON objects "
        "WHEN NEW.kind='file_inbox_project_binding' BEGIN SELECT RAISE(ABORT,'fixture'); END"
    )
    with pytest.raises(FileInboxError, match="file_inbox_settings_write_failed"):
        case.source.settings_store.replace(FileInboxSettings())
    assert case.runtime.store.get(SETTINGS_KIND, SETTINGS_ID) == before
    assert case.runtime.store.get(BINDING_KIND, SETTINGS_ID) == binding
    assert case.runtime.store.event_position() == position


@pytest.mark.parametrize("version", [True, 2])
async def test_bad_or_future_binding_is_never_overwritten(
    project_case: ProjectCase, version: bool | int
) -> None:
    case = project_case
    binding = case.runtime.store.get(BINDING_KIND, SETTINGS_ID)
    assert binding is not None
    corrupted = {**binding, "schema_version": version}
    case.runtime.store.put(BINDING_KIND, corrupted)
    before = case.runtime.store.get(SETTINGS_KIND, SETTINGS_ID)
    position = case.runtime.store.event_position()
    with pytest.raises(FileInboxError):
        case.source.settings_store.replace(FileInboxSettings())
    assert case.runtime.store.get(SETTINGS_KIND, SETTINGS_ID) == before
    assert case.runtime.store.get(BINDING_KIND, SETTINGS_ID) == corrupted
    assert case.runtime.store.event_position() == position
    assert not case.source.settings_state().enabled


async def test_null_settings_and_signature_keep_existing_generic_fact(
    project_case: ProjectCase,
) -> None:
    case = project_case
    desired = case.source.desired_settings().model_copy(update={"project_id": None})
    legacy = desired.model_dump(mode="json", exclude={"project_id"})
    case.source.settings_store.replace(desired)
    await case.source.reconfigure()
    original = case.source.settings_state()
    record = await observed(case)
    assert "project_proof" not in record
    event = canonical_event(case, record)
    for changes in ({}, {"project_id": None}):
        replacement = FileInboxSettings.model_validate({**legacy, **changes})
        case.source.settings_store.replace(replacement)
        await case.source.reconfigure()
        raw = case.runtime.store.get(SETTINGS_KIND, SETTINGS_ID)
        assert raw is not None and raw["settings"] == legacy
        LegacyFileInboxSettings.model_validate(raw["settings"])
        expected = hashlib.sha256(
            json.dumps(
                [legacy, case.runtime.config.settings.workspace_roots], sort_keys=True
            ).encode()
        ).hexdigest()
        assert _signature(replacement, case.runtime.config.settings.workspace_roots) == expected
        assert case.source.settings_state().scope == original.scope
        assert case.source.settings_state().revision == original.revision
        assert read_record(case.runtime.store, event, case.source.settings_state()) == record
    await case.attention.start()
    await eventually(lambda: len(attention_inputs(case.runtime)) == 1)
    assert "attention_project" not in attention_inputs(case.runtime)[0]
    await case.attention.reconfigure()
    barrier = await case.runtime.bus.emit("project-test.generic-barrier", {})
    await eventually(lambda: case.attention.cursor >= barrier["id"])
    assert len(attention_inputs(case.runtime)) == 1


async def test_nonempty_binding_rejects_legacy_model_and_unbind_rebind_revokes_old_proof(
    project_case: ProjectCase,
) -> None:
    case = project_case
    old = await observed(case)
    original = ProjectInboxProof.load(old["project_proof"])
    assert original is not None
    raw = case.runtime.store.get(SETTINGS_KIND, SETTINGS_ID)
    assert raw is not None
    with pytest.raises(ValidationError):
        LegacyFileInboxSettings.model_validate(raw["settings"])
    bound = case.source.desired_settings()
    case.source.settings_store.replace(bound.model_copy(update={"project_id": None}))
    await case.source.reconfigure()
    raw = case.runtime.store.get(SETTINGS_KIND, SETTINGS_ID)
    assert raw is not None
    LegacyFileInboxSettings.model_validate(raw["settings"])
    case.source.settings_store.replace(bound)
    await case.source.reconfigure()
    assert case.source.settings_state().binding_revision != original.binding_revision
    assert (
        read_record(case.runtime.store, canonical_event(case, old), case.source.settings_state())
        is None
    )


async def test_source_proof_is_immutable_and_success_snapshot_is_separate(
    project_case: ProjectCase,
) -> None:
    case = project_case
    record = await observed(case)
    proof = record["project_proof"]
    assert set(proof) == {
        "version",
        "record_id",
        "event_id",
        "scope",
        "revision",
        "binding_revision",
        "project_id",
    }
    assert record["routing"] == WEB_PROJECT_ROUTING.dump()
    assert "完整要求正文" not in json.dumps(record, ensure_ascii=False)
    assert "新正文只允许" not in json.dumps(record, ensure_ascii=False)
    updated = await case.ledger.save(
        "新要求", identifier=case.task["id"], next_step="只读核对当前版本"
    )
    await case.attention.start()
    await eventually(lambda: len(attention_inputs(case.runtime)) == 1)
    accepted = attention_inputs(case.runtime)[0]
    batch = ProjectInboxBatch.load(accepted["attention_project"])
    assert batch is not None and batch.proofs[0].dump() == proof
    assert batch.snapshot.revision == updated["revision"] == 2
    receipt = next(
        item
        for item in case.runtime.store.list("attention_receipt")
        if item["record_id"] == record["id"]
    )
    assert receipt["project_proof"] == proof
    assert receipt["project_snapshot"] == batch.snapshot.dump()
    assert case.runtime.store.get(RECORD_KIND, record["id"])["project_proof"] == proof
    await case.ledger.save(
        "之后的正常修改", identifier=case.task["id"], next_step="不重复旧收件事实"
    )
    barrier = await case.runtime.bus.emit("project-test.after-accepted", {})
    await eventually(lambda: case.attention.cursor >= barrier["id"])
    assert len(attention_inputs(case.runtime)) == 1


@pytest.mark.parametrize(
    "damage", ["future", "wrong_event", "missing", "channel", "extra_web_field"]
)
async def test_bad_project_proof_and_nonexact_web_route_never_become_generic(
    project_case: ProjectCase, damage: str
) -> None:
    case = project_case
    record = await observed(case)
    damaged = {**record}
    if damage == "future":
        damaged["project_proof"] = {**record["project_proof"], "version": 2}
    elif damage == "wrong_event":
        damaged["project_proof"] = {**record["project_proof"], "event_id": record["event_id"] + 1}
    elif damage == "missing":
        del damaged["project_proof"]
    elif damage == "channel":
        damaged["routing"] = {**record["routing"], "destination": {"kind": "channel", "route": {}}}
    else:
        damaged["routing"] = {**record["routing"], "destination": {"kind": "web", "route": {}}}
    case.runtime.store.put(RECORD_KIND, damaged)
    assert (
        read_record(case.runtime.store, canonical_event(case, record), case.source.settings_state())
        is None
    )
    await case.attention.start()
    barrier = await case.runtime.bus.emit("project-test.bad-proof", {})
    await eventually(lambda: case.attention.cursor >= barrier["id"])
    assert not attention_inputs(case.runtime)


async def test_project_batch_rejects_future_duplicates_mixed_sources_and_budgets(
    project_case: ProjectCase,
) -> None:
    case = project_case
    first = await observed(case)
    batch = batch_for(case, first)
    assert ProjectInboxBatch.load(batch.dump()) == batch
    assert ProjectInboxBatch.load({**batch.dump(), "version": True}) is None
    assert ProjectInboxBatch.load({**batch.dump(), "version": 2}) is None
    assert ProjectInboxBatch.load(batch.dump(), max_bytes=1) is None
    assert ProjectInboxBatch.load(batch.dump(), max_records=0) is None
    assert ProjectInboxBatch.load({**batch.dump(), "proofs": [batch.proofs[0].dump()] * 2}) is None
    second = replace(batch.proofs[0], record_id="b" * 32, event_id=batch.proofs[0].event_id + 1)
    mixed = replace(second, binding_revision="c" * 32)
    assert (
        ProjectInboxBatch.load(ProjectInboxBatch((batch.proofs[0], mixed), batch.snapshot).dump())
        is None
    )
    assert (
        ProjectInboxBatch.load(ProjectInboxBatch((batch.proofs[0], second), batch.snapshot).dump())
        is not None
    )
    with pytest.raises(EvidenceError, match="字节预算"):
        read_task_snapshot(
            case.runtime.store, case.task["id"], max_bytes=len(case.task["id"].encode("utf-8"))
        )


async def test_snapshot_change_before_enqueue_refreshes_without_another_file_event(
    project_case: ProjectCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = project_case
    record = await observed(case)
    before = case.attention.cursor
    constructed, release, rejected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = case.runtime.submit_attention
    snapshots: list[ProjectTaskSnapshot] = []

    async def gated_submit(content: str, event_id: str, **kwargs: Any) -> dict[str, Any] | None:
        project = kwargs["project"]
        assert isinstance(project, ProjectInboxBatch)
        snapshots.append(project.snapshot)
        if len(snapshots) == 1:
            constructed.set()
            await release.wait()
        try:
            return await original(content, event_id, **kwargs)
        except _ProjectTaskSnapshotChanged:
            assert case.attention.cursor == before
            assert not case.runtime.store.list("attention_receipt")
            assert not attention_inputs(case.runtime)
            rejected.set()
            raise

    monkeypatch.setattr(case.runtime, "submit_attention", gated_submit)
    await case.attention.start()
    async with asyncio.timeout(3):
        await constructed.wait()
    async with case.runtime.configuration_lock:
        current = await case.ledger.save(
            "当前完整要求",
            identifier=case.task["id"],
            brief={"goal": "锁屏障内更新后的当前完整要求", "constraints": ["仍仅只读"]},
        )
    release.set()
    async with asyncio.timeout(3):
        await rejected.wait()
    await eventually(lambda: len(attention_inputs(case.runtime)) == 1)
    assert [snapshot.revision for snapshot in snapshots] == [1, current["revision"]]
    accepted = ProjectInboxBatch.load(attention_inputs(case.runtime)[0]["attention_project"])
    assert accepted is not None and accepted.snapshot.revision == current["revision"]
    assert accepted.proofs[0].dump() == record["project_proof"]
    assert len([event for event in case.runtime.store.events() if event["type"] == EVENT_TYPE]) == 1
    assert len(case.runtime.store.list("attention_receipt")) == 1


async def test_two_snapshot_conflicts_wait_for_a_real_bus_revision(
    project_case: ProjectCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = project_case
    await observed(case)
    before = case.attention.cursor
    conflicted = asyncio.Event()
    original = case.runtime.submit_attention
    snapshots: list[ProjectTaskSnapshot] = []

    async def changing_submit(content: str, event_id: str, **kwargs: Any) -> dict[str, Any] | None:
        project = kwargs["project"]
        assert isinstance(project, ProjectInboxBatch)
        snapshots.append(project.snapshot)
        if len(snapshots) <= 2:
            current = case.ledger.get(case.task["id"])
            # Deliberately silent fixture mutation isolates the retry ceiling;
            # ordinary task changes use the real emitting ledger in the test above.
            case.runtime.store.put("commitment", {**current, "revision": current["revision"] + 1})
        try:
            return await original(content, event_id, **kwargs)
        except _ProjectTaskSnapshotChanged:
            assert case.attention.cursor == before
            assert not case.runtime.store.list("attention_receipt")
            assert not attention_inputs(case.runtime)
            if len(snapshots) == 2:
                conflicted.set()
            raise

    monkeypatch.setattr(case.runtime, "submit_attention", changing_submit)
    await case.attention.start()
    async with asyncio.timeout(3):
        await conflicted.wait()
    await eventually(lambda: case.attention.status()["waiting"] == "project_changed")
    await asyncio.sleep(0)
    assert len(snapshots) == 2
    assert not attention_inputs(case.runtime)
    await case.runtime.bus.notify()
    await eventually(lambda: len(attention_inputs(case.runtime)) == 1)
    assert [snapshot.revision for snapshot in snapshots] == [1, 2, 3]


@pytest.mark.parametrize("termination", ["done", "cancelled", "missing", "unbind"])
async def test_terminated_project_fact_does_not_block_other_attention(
    project_case: ProjectCase, termination: str
) -> None:
    case = project_case
    record = await observed(case)
    if termination in {"done", "cancelled"}:
        await case.ledger.save(
            "已终止", identifier=case.task["id"], status=termination, evidence="真实终止说明"
        )
    elif termination == "missing":
        case.runtime.store.delete("commitment", case.task["id"])
    else:
        case.source.settings_store.replace(
            case.source.desired_settings().model_copy(update={"project_id": None})
        )
        await case.source.reconfigure()
    await reminder(case.runtime, "仍需送达的普通提醒")
    await case.attention.start()
    await eventually(lambda: len(attention_inputs(case.runtime)) == 1)
    assert case.attention.cursor >= record["event_id"]
    accepted = attention_inputs(case.runtime)[0]
    assert "仍需送达的普通提醒" in accepted["content"]
    assert "attention_project" not in accepted
    assert all(
        item["record_id"] != record["id"] for item in case.runtime.store.list("attention_receipt")
    )


@pytest.mark.parametrize("damage", ["record", "event", "binding", "snapshot", "project"])
async def test_direct_attention_rejects_shaped_but_forged_project_proof(
    project_case: ProjectCase, damage: str
) -> None:
    case = project_case
    record = await observed(case)
    batch = batch_for(case, record)
    proof = batch.proofs[0]
    if damage == "record":
        bad = replace(batch, proofs=(replace(proof, record_id="f" * 32),))
    elif damage == "event":
        bad = replace(batch, proofs=(replace(proof, event_id=proof.event_id + 1),))
    elif damage == "binding":
        bad = replace(batch, proofs=(replace(proof, binding_revision="f" * 32),))
    elif damage == "snapshot":
        bad = replace(batch, snapshot=replace(batch.snapshot, revision=batch.snapshot.revision + 1))
    else:
        bad = ProjectInboxBatch(
            (replace(proof, project_id="another-existing-shape"),),
            replace(batch.snapshot, project_id="another-existing-shape"),
        )
    assert ProjectInboxBatch.load(bad.dump()) == bad
    roots = case.runtime.config.settings.workspace_roots
    assert project_batch_current(
        case.runtime.store, batch, workspace_roots=roots, max_task_bytes=16_384
    )
    assert not project_batch_current(
        case.runtime.store, bad, workspace_roots=roots, max_task_bytes=16_384
    )
    before = case.attention.cursor
    with pytest.raises(ValueError):
        await case.runtime.submit_attention(
            "只有形状、没有正确来源",
            f"forged:{damage}",
            objects=[],
            routing=WEB_PROJECT_ROUTING,
            project=bad,
        )
    assert case.attention.cursor == before
    assert not attention_inputs(case.runtime)
    assert not case.runtime.store.list("attention_receipt")


async def test_normal_source_reopen_keeps_proof_and_accepts_it_once(
    project_case: ProjectCase,
) -> None:
    case = project_case
    record = await observed(case)
    binding = case.runtime.store.get(BINDING_KIND, SETTINGS_ID)
    policy = case.runtime.store.get(POLICY_KIND, SETTINGS_ID)
    assert policy is not None
    await case.source.reconfigure()
    assert case.source.generation > record["proof"]["watcher_generation"]
    assert case.runtime.store.get(BINDING_KIND, SETTINGS_ID) == binding
    assert case.runtime.store.get(POLICY_KIND, SETTINGS_ID)["revision"] == policy["revision"]
    assert (
        read_record(case.runtime.store, canonical_event(case, record), case.source.settings_state())
        == record
    )
    await case.attention.start()
    await eventually(lambda: len(attention_inputs(case.runtime)) == 1)
    await case.source.reconfigure()
    await case.attention.reconfigure()
    barrier = await case.runtime.bus.emit("project-test.reopen", {})
    await eventually(lambda: case.attention.cursor >= barrier["id"])
    assert len(attention_inputs(case.runtime)) == 1
    assert (
        case.runtime.store.get(RECORD_KIND, record["id"])["project_proof"]
        == record["project_proof"]
    )


async def test_binding_rejects_project_id_that_cannot_fit_canonical_record(
    project_case: ProjectCase,
) -> None:
    case = project_case
    actual = await observed(case, "r" * 200 + ".txt")
    one_character = case.source.desired_settings().model_copy(update={"project_id": "p"})
    settings_shell = {
        "id": SETTINGS_ID,
        "schema_version": 1,
        "settings": one_character.model_dump(mode="json"),
    }
    task_shell = {**case.task, "id": "p"}
    occupied = max(
        len(json.dumps(settings_shell, ensure_ascii=False).encode("utf-8")),
        len(json.dumps(task_shell, ensure_ascii=False).encode("utf-8")),
    )
    project_id = "p" * (_OBJECT_BYTES - occupied + 1)
    task = {**case.task, "id": project_id}
    case.runtime.store.put("commitment", task)
    desired = one_character.model_copy(update={"project_id": project_id})
    _checked_settings(desired)
    assert read_task_snapshot(case.runtime.store, project_id, max_bytes=_OBJECT_BYTES) is not None
    candidate = {
        **actual,
        "project_proof": {**actual["project_proof"], "project_id": project_id},
    }
    assert len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > _OBJECT_BYTES
    before = case.runtime.store.get(SETTINGS_KIND, SETTINGS_ID)
    binding = case.runtime.store.get(BINDING_KIND, SETTINGS_ID)
    position = case.runtime.store.event_position()
    with pytest.raises(FileInboxError, match="file_inbox_project_record_budget"):
        case.source.settings_store.replace(desired)
    assert case.runtime.store.get(SETTINGS_KIND, SETTINGS_ID) == before
    assert case.runtime.store.get(BINDING_KIND, SETTINGS_ID) == binding
    assert case.runtime.store.event_position() == position


@pytest.mark.skipif(sys.platform == "win32", reason="Windows filenames reject control characters")
async def test_record_budget_keeps_escaped_legal_name_and_boundary_project_id_readable(
    project_case: ProjectCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = project_case
    actual = await observed(case, "seed.txt")
    maximum = 2 * SQLITE_MAX_INTEGER + 1
    reserved = {
        **actual,
        "proof": {**actual["proof"], "watcher_generation": SQLITE_MAX_INTEGER},
        "event_id": SQLITE_MAX_INTEGER,
        "observed_at": sys.float_info.min,
        "metadata": {
            **{key: maximum for key in actual["metadata"] if key != "name"},
            "name": "\x01" * _NAME_BYTES,
        },
        "project_proof": {
            **actual["project_proof"],
            "project_id": "p",
            "event_id": SQLITE_MAX_INTEGER,
        },
    }
    size = len(json.dumps(reserved, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    project_id = "p" * (_OBJECT_BYTES - size + 1)
    assert len(project_id) > len(case.task["id"])
    case.runtime.store.put("commitment", {**case.task, "id": project_id})
    desired = case.source.desired_settings().model_copy(update={"project_id": project_id})
    selected_default: list[bool] = []
    original = FrozenRouting.independent

    def select_default(*args: Any, **kwargs: Any) -> FrozenRouting:
        selected_default.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(FrozenRouting, "independent", select_default)
    case.source.settings_store.replace(desired)
    await case.source.reconfigure()
    record = await observed(case, "\x01" * 200 + ".txt")
    assert record["project_proof"]["project_id"] == project_id
    assert "\\u0001" in json.dumps(record, ensure_ascii=False)
    assert len(json.dumps(record, ensure_ascii=False).encode("utf-8")) <= _OBJECT_BYTES
    assert (
        read_record(case.runtime.store, canonical_event(case, record), case.source.settings_state())
        == record
    )
    assert not selected_default


async def test_commit_over_budget_rolls_back_candidate_and_keeps_one_readable_gap(
    project_case: ProjectCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = project_case
    actual = await observed(case, "seed.txt")
    gap = {**actual, "kind": "gap", "metadata": None, "code": "coverage_incomplete"}
    policy = case.runtime.store.get(POLICY_KIND, SETTINGS_ID)
    assert policy is not None
    paused = {**policy, "code": "coverage_incomplete", "gap_id": "0" * 32, "window_count": 32}
    required = [gap, paused]
    for kind in (SETTINGS_KIND, BINDING_KIND):
        item = case.runtime.store.get(kind, SETTINGS_ID)
        assert item is not None
        required.append(item)
    limit = max(len(json.dumps(item, ensure_ascii=False).encode("utf-8")) for item in required) + 32
    assert limit < len(json.dumps(actual, ensure_ascii=False).encode("utf-8"))
    # Tighten only the serialization budget to exercise the independent commit
    # gate after a previously valid configuration; resource limits never change.
    monkeypatch.setattr(file_inbox_module, "_OBJECT_BYTES", limit)
    before = case.runtime.store.event_position()
    document = case.directory / "over-budget.txt"
    document.write_text("temporary metadata candidate", encoding="utf-8")
    await case.source.ingest([FileSignal("moved_to", document.name)])
    await eventually(lambda: case.source.status()["code"] == "coverage_incomplete")
    after = case.runtime.store.events(before)
    assert len(after) == 1 and after[0]["type"] == EVENT_TYPE
    records = case.runtime.store.list(RECORD_KIND)
    gaps = [item for item in records if item["kind"] == "gap"]
    assert len(gaps) == 1
    assert all(
        item["metadata"] is None or item["metadata"]["name"] != document.name for item in records
    )
    assert not case.source.status()["running"]
    assert len(json.dumps(gaps[0], ensure_ascii=False).encode("utf-8")) <= limit
    assert read_record(case.runtime.store, after[0], case.source.settings_state()) == gaps[0]
