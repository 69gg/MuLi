from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_runtime import make_runtime

from muli.commitments import Commitments
from muli.file_inbox import project_snapshot
from muli.format_admission import CURRENT_FORMAT_STATE, FormatAdmissionError
from muli.proactive import FrozenRouting
from muli.project_review import validate_project_review
from muli.reply_origin import ReplyOrigin
from muli.store import new_id, now


async def test_conversation_project_enqueue_rejects_current_format_without_residue(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    try:
        assert runtime.store.format_state == CURRENT_FORMAT_STATE
        assert CURRENT_FORMAT_STATE.formats.objects == CURRENT_FORMAT_STATE.formats.ingress == 1
        project = await Commitments(runtime.store, runtime.bus).save(
            "项目格式门禁", brief={"goal": "核查项目资料", "constraints": ["仅本地只读"]}
        )
        snapshot = project_snapshot(runtime, project["id"])
        assert snapshot is not None
        # These are finite, shape-valid source locators, not a watcher/source E2E proof.
        marker = validate_project_review(
            {
                "version": 1,
                "project_id": project["id"],
                "binding_revision": new_id(),
                "sources": [
                    {
                        "record_id": new_id(),
                        "event_id": runtime.store.event_position(),
                        "scope": new_id(),
                        "revision": new_id(),
                        "watcher_generation": 1,
                    }
                ],
                "task_snapshot": snapshot,
            }
        )
        session_id = runtime.main_session()["id"]

        def persisted() -> tuple[list[tuple[Any, ...]], ...]:
            return tuple(
                [tuple(row) for row in runtime.store.db.execute(query)]
                for query in (
                    "SELECT * FROM ingress ORDER BY seq",
                    "SELECT * FROM objects ORDER BY kind,id",
                    "SELECT * FROM events ORDER BY id",
                )
            )

        before = persisted()
        with pytest.raises(FormatAdmissionError, match="^project_review_format_required$"):
            runtime.conversation.enqueue(
                session_id,
                "只读核查当前项目资料",
                source="system:attention",
                principal="system:attention",
                mode="ask",
                max_pending=runtime.config.settings.limits.max_pending_runs,
                event_id="project-format-gate",
                internal=True,
                objects=[("attention_receipt", {"id": new_id(), "status": "accepted"})],
                attention_routing=FrozenRouting.bound(ReplyOrigin(kind="web")),
                project_review=marker,
            )
        assert persisted() == before
        assert runtime.store.format_state == CURRENT_FORMAT_STATE
        assert not runtime.tasks and seen == []
    finally:
        await runtime.close()


async def test_claim_project_marker_on_current_format_keeps_whole_batch_unchanged(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    try:
        assert runtime.store.format_state == CURRENT_FORMAT_STATE
        assert CURRENT_FORMAT_STATE.formats.objects == CURRENT_FORMAT_STATE.formats.ingress == 1
        project = await Commitments(runtime.store, runtime.bus).save(
            "项目存档格式门禁", brief={"goal": "核查项目资料", "constraints": ["仅本地只读"]}
        )
        snapshot = project_snapshot(runtime, project["id"])
        assert snapshot is not None
        marker = validate_project_review(
            {
                "version": 1,
                "project_id": project["id"],
                "binding_revision": new_id(),
                "sources": [
                    {
                        "record_id": new_id(),
                        "event_id": runtime.store.event_position(),
                        "scope": new_id(),
                        "revision": new_id(),
                        "watcher_generation": 1,
                    }
                ],
                "task_snapshot": snapshot,
            }
        )
        session_id = runtime.main_session()["id"]
        routing = FrozenRouting.bound(ReplyOrigin(kind="web"))
        ordinary = runtime.conversation.payload(
            session_id,
            "后续普通本地宿主事项",
            source="system:attention",
            principal="system:attention",
            mode="ask",
            event_id="ordinary-after-project",
            attention_routing=routing,
        )
        project_input = runtime.conversation.payload(
            session_id,
            "格式不兼容的项目事项",
            source="system:attention",
            principal="system:attention",
            mode="ask",
            event_id="raw-project-format-gate",
            attention_routing=routing,
            project_review=marker,
        )
        # Emulate persisted input without calling or mocking an admission gate.
        # Project-first grouping must reject before any archive/delivery DML;
        # the following ordinary item also stays pending for its own later batch.
        with runtime.store.db:
            for payload in (project_input, ordinary):
                runtime.store.db.execute(
                    "INSERT INTO ingress(id,session_id,state,payload,received) VALUES(?,?,?,?,?)",
                    (
                        payload["id"],
                        session_id,
                        "pending",
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        now(),
                    ),
                )

        def persisted() -> tuple[list[tuple[Any, ...]], ...]:
            return tuple(
                [tuple(row) for row in runtime.store.db.execute(query)]
                for query in (
                    "SELECT * FROM ingress ORDER BY seq",
                    "SELECT * FROM objects ORDER BY kind,id",
                    "SELECT * FROM events ORDER BY id",
                    "SELECT * FROM message_order ORDER BY seq",
                )
            )

        before = persisted()
        with pytest.raises(FormatAdmissionError, match="^project_review_format_required$"):
            runtime.store.claim_inputs(
                session_id, new_id(), limit=runtime.config.settings.limits.max_pending_runs
            )
        assert persisted() == before
        assert runtime.store.pending_inputs(session_id) == 2
        assert runtime.store.format_state == CURRENT_FORMAT_STATE
        assert not runtime.tasks and seen == []
    finally:
        await runtime.close()


async def test_claim_ordinary_input_before_project_does_not_require_project_format(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    try:
        assert runtime.store.format_state == CURRENT_FORMAT_STATE
        assert CURRENT_FORMAT_STATE.formats.objects == CURRENT_FORMAT_STATE.formats.ingress == 1
        project = await Commitments(runtime.store, runtime.bus).save(
            "项目不能阻塞前序普通事项",
            brief={"goal": "核查项目资料", "constraints": ["仅本地只读"]},
        )
        snapshot = project_snapshot(runtime, project["id"])
        assert snapshot is not None
        # Shape-valid finite locators exercise the claim boundary, not source E2E admission.
        marker = validate_project_review(
            {
                "version": 1,
                "project_id": project["id"],
                "binding_revision": new_id(),
                "sources": [
                    {
                        "record_id": new_id(),
                        "event_id": runtime.store.event_position(),
                        "scope": new_id(),
                        "revision": new_id(),
                        "watcher_generation": 1,
                    }
                ],
                "task_snapshot": snapshot,
            }
        )
        session_id = runtime.main_session()["id"]
        routing = FrozenRouting.bound(ReplyOrigin(kind="web"))
        ordinary = runtime.conversation.payload(
            session_id,
            "先处理普通本地事项",
            source="system:attention",
            principal="system:attention",
            mode="ask",
            event_id="ordinary-claim-boundary",
            attention_routing=routing,
        )
        project_input = runtime.conversation.payload(
            session_id,
            "下一批才检查项目格式",
            source="system:attention",
            principal="system:attention",
            mode="ask",
            event_id="project-next-claim-boundary",
            attention_routing=routing,
            project_review=marker,
        )
        with runtime.store.db:
            for payload in (ordinary, project_input):
                runtime.store.db.execute(
                    "INSERT INTO ingress(id,session_id,state,payload,received) VALUES(?,?,?,?,?)",
                    (
                        payload["id"],
                        session_id,
                        "pending",
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        now(),
                    ),
                )
        original_project = tuple(
            runtime.store.db.execute(
                "SELECT * FROM ingress WHERE id=?", (project_input["id"],)
            ).fetchone()
        )
        run_id = new_id()

        # Real format 1 must suffice for this ordinary batch, despite the same-route
        # project input immediately behind it. No gate is mocked or upgraded.
        claimed = runtime.store.claim_inputs(
            session_id, run_id, limit=runtime.config.settings.limits.max_pending_runs
        )
        assert [message["id"] for message in claimed] == [ordinary["id"]]
        assert claimed[0]["run_id"] == run_id and "project_review" not in claimed[0]
        assert runtime.store.get("message", ordinary["id"]) == claimed[0]
        assert runtime.store.get("message", project_input["id"]) is None
        assert (
            tuple(
                runtime.store.db.execute(
                    "SELECT * FROM ingress WHERE id=?", (project_input["id"],)
                ).fetchone()
            )
            == original_project
        )
        assert [item["id"] for item in runtime.conversation.pending(session_id)] == [
            project_input["id"]
        ]
        assert runtime.store.format_state == CURRENT_FORMAT_STATE
        assert not runtime.tasks and seen == []
    finally:
        await runtime.close()
