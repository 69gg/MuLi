"""Question authority and transaction boundaries through actual Runtime tools."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError
from test_runtime import FakeClient
from test_team_progress_policy import progress_runtime as progress_runtime
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli import owner_questions as owner_questions_module
from muli.evidence import EvidenceError, json_size
from muli.local_services import LocalServices
from muli.owner_questions import OwnerQuestions, owner_question_intent, question_prefix
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.task_control import OwnerControlRequest

QUESTION_ID = question_prefix("synthetic-task") + "a" * 64


@dataclass
class CancelReadProbe:
    metadata_columns: list[tuple[str, ...]] = field(default_factory=list)
    projected_payload_bytes: list[int] = field(default_factory=list)
    gated_payloads: list[tuple[int, bool]] = field(default_factory=list)

    def row(self, cursor: sqlite3.Cursor, values: tuple[Any, ...]) -> sqlite3.Row:
        columns = tuple(column[0] for column in cursor.description or ())
        if {"seq", "id", "session_id", "state"} <= set(columns):
            self.metadata_columns.append(columns)
        if "payload" in columns:
            payload = values[columns.index("payload")]
            if isinstance(payload, str):
                self.projected_payload_bytes.append(len(payload.encode("utf-8")))
        if columns == ("storage_type", "size", "data"):
            self.gated_payloads.append((values[1], values[2] is None))
        return sqlite3.Row(cursor, values)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"/回答问题 {QUESTION_ID}\n只补出处。",
            ("answer", QUESTION_ID, "record_only", "只补出处。"),
        ),
        (
            f"/回答并继续 {QUESTION_ID}\n原答\n第二行",
            ("answer", QUESTION_ID, "dispatch", "原答\n第二行"),
        ),
        (f"/继续问题 {QUESTION_ID}", ("dispatch", QUESTION_ID, None, None)),
        (f"/取消问题 {QUESTION_ID}", ("cancel", QUESTION_ID, None, None)),
    ],
)
def test_owner_intent_requires_one_complete_original_command(
    text: str, expected: tuple[str, str, str | None, str | None]
) -> None:
    assert owner_question_intent(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "好的",
        "请看看还有哪个问题",
        f"> /继续问题 {QUESTION_ID}",
        f"转述：/回答问题 {QUESTION_ID}\n模型写的答案",
        f" /继续问题 {QUESTION_ID}",
        f"/继续问题 {QUESTION_ID}\n",
        f"/回答问题 {QUESTION_ID}\n  ",
        f"/继续问题 {QUESTION_ID}\n/取消问题 {QUESTION_ID}",
        "/继续问题 aaaaaaaa",
    ],
)
def test_prose_quotes_blank_answers_and_short_ids_have_no_question_authority(text: str) -> None:
    with pytest.raises(PermissionError):
        owner_question_intent(text)


@dataclass
class QuestionRig:
    runtime: Runtime
    services: LocalServices
    task_id: str = ""
    worker_id: str = ""
    question_id: str = ""
    retry_register: bool = False
    multiple_questions: bool = False
    owner_calls: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    register_results: list[dict[str, Any]] = field(default_factory=list)
    worker_continuations: list[str] = field(default_factory=list)

    @property
    def coordinator(self) -> OwnerQuestions:
        return self.runtime.owner_questions

    def install(self) -> None:
        rig = self

        class Worker(FakeClient):
            def __init__(self) -> None:
                super().__init__([], [])
                self.round = 0

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                self.round += 1
                if rig.question_id:
                    rig.worker_continuations.append(
                        "\n".join(message.content for message in messages)
                    )
                    turn = ModelTurn(content="已按主人原答完成本地补充，未执行外发。")
                elif self.round <= (2 if rig.retry_register or rig.multiple_questions else 1):
                    if self.round == 2:
                        rig.register_results.append(tool_result(messages, "register-1"))
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                f"register-{self.round}",
                                "send_input",
                                target=rig.runtime.main_session()["id"],
                                message="请确认只补出处还是还需对照表？",
                                question={
                                    "action": "register",
                                    "key": "clarify-extra"
                                    if rig.multiple_questions and self.round == 2
                                    else "clarify-scope",
                                },
                            )
                        ]
                    )
                else:
                    result = tool_result(messages, f"register-{self.round - 1}")
                    rig.register_results.append(result)
                    assert result.get("accepted") is True, result
                    rig.question_id = result["question_id"]
                    turn = ModelTurn(content="问题已登记，本轮停靠，等待主人明确补充。")
                yield StreamEvent("done", turn=turn)

        class Main(FakeClient):
            def __init__(self) -> None:
                super().__init__([], [])
                self.previous: str | None = None

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                if self.previous is not None:
                    rig.results.append(tool_result(messages, self.previous))
                if rig.owner_calls:
                    args = rig.owner_calls.pop(0)
                    self.previous = f"owner-call-{len(rig.results)}"
                    turn = ModelTurn(tool_calls=[call(self.previous, "send_input", **args)])
                else:
                    self.previous = None
                    turn = ModelTurn(content="已核查本轮问题操作回执；任务完成仍需另查。")
                yield StreamEvent("done", turn=turn)

        def factory(config: ModelConfig) -> FakeClient:
            return Worker() if config.model == "worker" else Main()

        self.runtime.client_factory = factory

    async def register(self) -> None:
        self.install()
        task = await self.services.commitments.save(
            "合成问题事务", brief={"goal": "核查合成草稿", "constraints": ["不外发"]}
        )
        self.task_id = task["id"]
        worker = await self.runtime.team.spawn(
            "问题队友", "核查合成草稿并显式登记澄清", commitment_id=self.task_id
        )
        self.worker_id = worker["id"]
        await eventually(
            self.runtime, lambda: self.runtime.team.get(self.worker_id)["status"] == "completed"
        )
        await eventually(self.runtime, lambda: self.worker_id not in self.runtime.team._active)
        assert self.question_id

    async def owner(
        self,
        text: str,
        *,
        action: str = "answer",
        delivery: str | None = "record_only",
        interpretation: str = "AI 转述：主人的准确原答已另存。",
        repeats: int = 1,
    ) -> list[dict[str, Any]]:
        # Ordinary question notices are not owner inputs. Cancel only this
        # fixture's idle main mailbox before submitting its exact owner round.
        self.runtime.store.cancel_inputs(self.runtime.main_session()["id"])
        if self.runtime.config.settings.models.main.model != "main":
            self.runtime.config.update({"models": {"main": {"model": "main"}}})
            await self.runtime.reconfigure()
        question: dict[str, Any] = {"action": action, "id": self.question_id}
        if action == "answer":
            question["delivery"] = delivery
        args = {"target": self.worker_id, "message": interpretation, "question": question}
        before = len(self.results)
        self.owner_calls = [dict(args) for _ in range(repeats)]
        submitted = await self.runtime.submit(text, mode="yolo", _owner_text_chars=len(text))
        assert submitted["run_id"]
        await self.runtime.wait(submitted["run_id"])
        await eventually(
            self.runtime, lambda: self.runtime.main_session()["id"] not in self.runtime.tasks
        )
        return self.results[before:]


async def test_question_register_retry_is_read_only_in_the_original_real_generation(
    progress_runtime: tuple[Runtime, LocalServices],
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services, retry_register=True)
    await rig.register()
    assert len(rig.register_results) == 2
    first, retry = rig.register_results
    assert first["duplicate"] is False and retry["duplicate"] is True
    assert first["question_id"] == retry["question_id"]
    records = runtime.store.list("owner_question")
    assert len(records) == 1
    assert records[0]["author"]["generation"] == 1
    assert records[0]["author"]["run_id"] == runtime.team.get(rig.worker_id)["run_id"]
    events = [
        event for event in runtime.store.events() if event["type"] == "owner_question.registered"
    ]
    assert len(events) == 1
    assert runtime.store.pending_inputs(rig.worker_id) == 0


async def test_real_owner_prose_cannot_be_upgraded_to_a_fabricated_answer(
    progress_runtime: tuple[Runtime, LocalServices],
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    result = (await rig.owner("好的", interpretation="AI 自造：主人让队友继续"))[0]
    assert "error" in result
    record = rig.coordinator._record(rig.question_id)
    assert record["status"] == "open" and record["answer"] is None
    assert runtime.store.pending_inputs(rig.worker_id) == 0


async def test_record_only_keeps_owner_original_separate_and_dispatches_one_mailbox(
    progress_runtime: tuple[Runtime, LocalServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    original = f"/回答问题 {rig.question_id}\n只补出处，保留原任务限制。"
    result = (await rig.owner(original, repeats=2))[0]
    assert result["answer_recorded"] is True and result["delivery_state"] == "not_dispatched"
    assert runtime.store.pending_inputs(rig.worker_id) == 0
    record = rig.coordinator._record(rig.question_id)
    assert record["answer"]["owner"]["original_text"] == original
    assert record["answer"]["owner"]["body"] == "只补出处，保留原任务限制。"
    assert record["answer"]["interpretation"] != record["answer"]["owner"]["body"]
    assert rig.coordinator.read(rig.task_id)["items"] == []
    starts: list[None] = []

    def observe_question_start() -> None:
        # Runtime also revisits the global scheduler when it claims inputs,
        # flushes plan deliveries and settles a run. Keep the worker dormant,
        # but count only this real owner tool's dispatch effect.
        active = runtime._active_tool.get()
        execution = runtime._tool_execution.get()
        main_id = runtime.main_session()["id"]
        if (
            active is not None
            and active.name == "send_input"
            and execution is not None
            and execution[0] == main_id
            and runtime.tasks.get(main_id) is asyncio.current_task()
        ):
            starts.append(None)

    with monkeypatch.context() as patch:
        patch.setattr(runtime.team, "start", observe_question_start)
        results = await rig.owner(f"/继续问题 {rig.question_id}", action="dispatch", repeats=2)
    assert [item["duplicate"] for item in results] == [False, True]
    assert len(starts) == 1
    pending = runtime.conversation.pending(rig.worker_id)
    assert len(pending) == 1
    assert pending[0]["owner_question_answer"]["question_id"] == rig.question_id
    assert pending[0]["principal"] == f"agent:{runtime.main_session()['id']}"
    assert original in pending[0]["content"]
    assert runtime.team.get(rig.worker_id)["generation"] == 1


async def test_answer_dispatch_capacity_failure_rolls_back_the_answer_fact(
    progress_runtime: tuple[Runtime, LocalServices],
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    worker = runtime.team.get(rig.worker_id)
    for index in range(runtime.config.settings.limits.max_pending_runs):
        runtime.conversation.enqueue(
            rig.worker_id,
            f"独立待处理输入 {index}",
            source="synthetic:capacity",
            principal="owner",
            mode=worker["mode"],
            internal=True,
            max_pending=runtime.config.settings.limits.max_pending_runs,
        )
    before = rig.coordinator._record(rig.question_id)
    result = (await rig.owner(f"/回答并继续 {rig.question_id}\n只补出处。", delivery="dispatch"))[0]
    assert "error" in result
    assert rig.coordinator._record(rig.question_id) == before
    assert (
        runtime.store.pending_inputs(rig.worker_id)
        == runtime.config.settings.limits.max_pending_runs
    )


async def test_cancelling_a_queued_answer_cancels_only_its_exact_ingress(
    progress_runtime: tuple[Runtime, LocalServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    with monkeypatch.context() as patch:
        patch.setattr(runtime.team, "start", lambda: None)
        assert (await rig.owner(f"/回答并继续 {rig.question_id}\n原答。", delivery="dispatch"))[0][
            "queued"
        ] is True
        unrelated, _ = runtime.conversation.enqueue(
            rig.worker_id,
            "另一个正常补充",
            source="synthetic:other",
            principal="owner",
            mode="yolo",
            internal=True,
            max_pending=runtime.config.settings.limits.max_pending_runs,
        )
        result = (await rig.owner(f"/取消问题 {rig.question_id}", action="cancel"))[0]
    assert result["delivery_state"] == "cancelled" and result["answer_recorded"] is True
    pending = runtime.conversation.pending(rig.worker_id)
    assert [item["id"] for item in pending] == [unrelated["id"]]
    record = rig.coordinator._record(rig.question_id)
    assert (
        runtime.store.db.execute(
            "SELECT state FROM ingress WHERE id=?", (record["delivery"]["input_id"],)
        ).fetchone()[0]
        == "cancelled"
    )


async def test_question_read_revision_survives_unrelated_tool_events_and_invalidates_on_answer(
    progress_runtime: tuple[Runtime, LocalServices],
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    page = rig.coordinator.read(rig.task_id, question_status="all")
    token = page["revision"]
    runtime.store.event("tool.completed", {"synthetic": True})
    assert (
        rig.coordinator.read(rig.task_id, question_status="all", revision=token)["revision"]
        == token
    )
    entry = page["items"][0]
    first = rig.coordinator.read(
        rig.task_id,
        record_id=entry["record_id"],
        record_revision=entry["record_revision"],
        length_bytes=64,
    )
    assert first["has_more"] is True
    await rig.owner(f"/回答问题 {rig.question_id}\n准确原答。")
    with pytest.raises(EvidenceError, match="已改变"):
        rig.coordinator.read(rig.task_id, question_status="all", revision=token)
    with pytest.raises(EvidenceError, match="已改变"):
        rig.coordinator.read(
            rig.task_id,
            record_id=entry["record_id"],
            record_revision=entry["record_revision"],
            offset_bytes=first["next_offset_bytes"],
        )


@pytest.mark.parametrize("combined", [True, False], ids=["combined", "separate-dispatch"])
async def test_public_question_pages_preserve_original_authority_without_repeated_sources(
    progress_runtime: tuple[Runtime, LocalServices],
    monkeypatch: pytest.MonkeyPatch,
    combined: bool,
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    command = f"/{'回答并继续' if combined else '回答问题'} {rig.question_id}\n按原要求补出处。"
    with monkeypatch.context() as patch:
        patch.setattr(runtime.team, "start", lambda: None)
        result = (await rig.owner(command, delivery="dispatch" if combined else "record_only"))[0]
        assert result["accepted"] is True
        if not combined:
            dispatched = (await rig.owner(f"/继续问题 {rig.question_id}", action="dispatch"))[0]
            assert dispatched["accepted"] is True
    record = rig.coordinator._record(rig.question_id)
    changes = runtime.store.db.total_changes
    events = runtime.store.event_position()
    generation = runtime.team.get(rig.worker_id)["generation"]
    arguments: dict[str, Any] = {
        "section": "owner_questions",
        "record_id": rig.question_id,
    }
    full = services.task_inspection.read(rig.task_id, **arguments)
    assert full["has_more"] is False
    public = json.loads(full["content"])
    assert public["message"] == record["message"]
    assert public["answer"]["owner"] == rig.coordinator._owner_view(record["answer"]["owner"])
    assert public["answer"]["owner"]["original_text"] == command
    assert public["delivery"]["input_id"] == record["delivery"]["input_id"]
    if combined:
        assert public["delivery"]["owner"] == {
            "input_id": record["answer"]["owner"]["input_id"],
            "same_as": "answer.owner",
        }
        assert public["delivery"]["owner"]["input_id"] != public["delivery"]["input_id"]
    else:
        assert public["delivery"]["owner"] == rig.coordinator._owner_view(
            record["delivery"]["owner"]
        )
        assert public["delivery"]["owner"]["original_text"] == f"/继续问题 {rig.question_id}"
        assert public["delivery"]["owner"]["input_id"] != public["answer"]["owner"]["input_id"]
    assert full["content"] == json.dumps(
        public, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    )
    fragments: list[str] = []
    offset = 0
    for _ in range(64):
        page = services.task_inspection.read(
            rig.task_id,
            **arguments,
            record_revision=full["record_revision"],
            offset_bytes=offset,
            length_bytes=257,
        )
        assert page["length_bytes"] == len(page["content"].encode("utf-8"))
        fragments.append(page["content"])
        if not page["has_more"]:
            break
        assert page["next_offset_bytes"] > offset
        offset = page["next_offset_bytes"]
    else:
        pytest.fail("complete question did not fit the finite paging fixture")
    assert "".join(fragments) == full["content"]
    assert rig.coordinator._record(rig.question_id) == record
    assert runtime.store.db.total_changes == changes
    assert runtime.store.event_position() == events
    assert runtime.team.get(rig.worker_id)["generation"] == generation
    if combined:
        # Matching input IDs do not replace the independent canonical checks
        # that precede any public same-source reference.
        record["delivery"]["owner"]["payload"]["source"] = "synthetic:forged-source"
        runtime.store.put("owner_question", record)
        changes = runtime.store.db.total_changes
        with pytest.raises(EvidenceError, match="不可|不一致|损坏"):
            services.task_inspection.read(rig.task_id, **arguments)
        assert runtime.store.db.total_changes == changes


async def test_real_registration_answer_and_dispatch_receipts_fit_the_1000_character_budget(
    progress_runtime: tuple[Runtime, LocalServices],
) -> None:
    runtime, services = progress_runtime
    runtime.config.update({"limits": {"max_output_chars": 1000}})
    await runtime.reconfigure()
    rig = QuestionRig(runtime, services)
    await rig.register()
    registered = rig.register_results[0]
    assert registered["accepted"] is True and registered["question_id"] == rig.question_id
    answered = (await rig.owner(f"/回答问题 {rig.question_id}\n小预算也要保存准确原答。"))[0]
    assert answered["accepted"] is True and answered["answer_recorded"] is True
    assert runtime.store.pending_inputs(rig.worker_id) == 0
    dispatched = (await rig.owner(f"/继续问题 {rig.question_id}", action="dispatch"))[0]
    assert dispatched["accepted"] is True
    for receipt in (registered, answered, dispatched):
        assert json_size(receipt) <= 1000
        assert receipt["question_id"] == rig.question_id
        assert receipt["read_arguments"] == {
            "identifier": rig.task_id,
            "section": "owner_questions",
            "record_id": rig.question_id,
        }
    await eventually(runtime, lambda: runtime.team.get(rig.worker_id)["generation"] == 2)
    await eventually(runtime, lambda: runtime.team.get(rig.worker_id)["status"] == "completed")
    assert rig.coordinator._record(rig.question_id)["delivery"]["state"] == "claimed"
    assert len(rig.worker_continuations) == 1


async def test_pure_pause_keeps_unanswered_question_visible_and_record_only_never_continues(
    progress_runtime: tuple[Runtime, LocalServices],
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    await runtime.submit_task_control(rig.task_id, OwnerControlRequest("pause", 0, "pure-pause"))
    page = rig.coordinator.read(rig.task_id)
    assert len(page["items"]) == 1
    assert page["items"][0]["status"] == "open" and page["items"][0]["binding"] == "paused"
    answered = (await rig.owner(f"/回答问题 {rig.question_id}\n暂停期间仅记录的原答。"))[0]
    assert answered["answer_recorded"] is True and answered["binding"] == "paused"
    assert runtime.store.pending_inputs(rig.worker_id) == 0
    continued = (await rig.owner(f"/继续问题 {rig.question_id}", action="dispatch"))[0]
    assert "error" in continued
    assert runtime.store.pending_inputs(rig.worker_id) == 0
    assert runtime.team.get(rig.worker_id)["generation"] == 1
    await runtime.submit_task_control(
        rig.task_id, OwnerControlRequest("request_replan", 1, "new-replan")
    )
    stale = rig.coordinator.read(rig.task_id, question_status="all")["items"][0]
    assert stale["binding"] == "stale" and stale["status"] == "stale"


async def test_dispatch_event_failure_rolls_back_answer_mailbox_and_queued_agent(
    progress_runtime: tuple[Runtime, LocalServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    original = runtime.store._event
    before = rig.coordinator._record(rig.question_id)
    agent_before = runtime.team.get(rig.worker_id)

    def fail_dispatch(
        event_type: str,
        data: dict[str, Any],
        session_id: str | None = None,
        *,
        objects: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        if event_type == "owner_question.dispatched":
            raise ValueError("synthetic atomic event failure")
        return original(event_type, data, session_id, objects=objects)

    with monkeypatch.context() as patch:
        patch.setattr(runtime.store, "_event", fail_dispatch)
        result = (
            await rig.owner(
                f"/回答并继续 {rig.question_id}\n原子保存失败时不得留下原答。", delivery="dispatch"
            )
        )[0]
    assert "synthetic atomic event failure" in result["error"]
    assert rig.coordinator._record(rig.question_id) == before
    assert runtime.team.get(rig.worker_id) == agent_before
    assert runtime.store.pending_inputs(rig.worker_id) == 0


async def test_queued_answer_becomes_stale_before_claim_and_never_reaches_the_model(
    progress_runtime: tuple[Runtime, LocalServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    with monkeypatch.context() as patch:
        patch.setattr(runtime.team, "start", lambda: None)
        assert (
            await rig.owner(f"/回答并继续 {rig.question_id}\n旧要求下的原答。", delivery="dispatch")
        )[0]["queued"] is True
        await services.commitments.save(
            "合成问题事务",
            identifier=rig.task_id,
            brief={"goal": "改变后的新要求", "constraints": ["不外发"]},
        )
    runtime.team.start()
    await eventually(runtime, lambda: runtime.team.get(rig.worker_id)["generation"] == 2)
    await eventually(runtime, lambda: runtime.team.get(rig.worker_id)["status"] == "completed")
    record = rig.coordinator._record(rig.question_id)
    assert record["delivery"]["state"] == "cancelled"
    assert record["delivery"]["reason"] == "binding_stale"
    assert record["delivery"]["claim"] is None
    assert rig.worker_continuations == []
    assert runtime.store.get("message", record["delivery"]["input_id"]) is None


async def test_same_persistent_worker_next_generation_claims_once_and_cancel_cannot_retract_it(
    progress_runtime: tuple[Runtime, LocalServices],
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    result = (
        await rig.owner(
            f"/回答并继续 {rig.question_id}\n只补出处，不做对照表。", delivery="dispatch"
        )
    )[0]
    assert result["delivery_state"] in {"queued", "claimed"}
    await eventually(runtime, lambda: runtime.team.get(rig.worker_id)["generation"] == 2)
    await eventually(runtime, lambda: runtime.team.get(rig.worker_id)["status"] == "completed")
    await eventually(runtime, lambda: rig.worker_id not in runtime.team._active)
    record = rig.coordinator._record(rig.question_id)
    assert record["delivery"]["state"] == "claimed"
    claim = record["delivery"]["claim"]
    archive = runtime.store.get("message", claim["message_id"])
    assert archive is not None
    assert archive["run_id"] == claim["run_id"] == runtime.team.get(rig.worker_id)["run_id"]
    assert archive["seq"] == claim["seq"] and archive["input_seq"] == claim["input_seq"]
    assert archive["owner_question_answer"]["question_id"] == rig.question_id
    assert len(rig.worker_continuations) == 1
    cancelled = (await rig.owner(f"/取消问题 {rig.question_id}", action="cancel"))[0]
    assert cancelled["claimed"] is True and cancelled["delivery_state"] == "claimed"
    assert runtime.team.get(rig.worker_id)["generation"] == 2
    assert rig.coordinator._record(rig.question_id)["delivery"]["claim"] == claim


async def test_cancel_all_questions_for_one_worker_restores_the_shared_dormant_activation(
    progress_runtime: tuple[Runtime, LocalServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services, multiple_questions=True)
    await rig.register()
    question_ids = [item["question_id"] for item in rig.register_results]
    assert len(question_ids) == 2 and question_ids[0] != question_ids[1]
    with monkeypatch.context() as patch:
        patch.setattr(runtime.team, "start", lambda: None)
        for identifier in question_ids:
            rig.question_id = identifier
            assert (
                await rig.owner(
                    f"/回答并继续 {identifier}\n同队友的独立原答。", delivery="dispatch"
                )
            )[0]["queued"] is True
        for identifier in question_ids:
            rig.question_id = identifier
            assert (await rig.owner(f"/取消问题 {identifier}", action="cancel"))[0][
                "delivery_state"
            ] == "cancelled"
    assert runtime.store.pending_inputs(rig.worker_id) == 0
    assert runtime.team.get(rig.worker_id)["status"] == "completed"
    assert runtime.team.get(rig.worker_id)["generation"] == 1
    runtime.team.start()
    assert rig.worker_id not in runtime.team._active
    assert rig.worker_continuations == []


async def test_cancel_pending_questions_seeks_metadata_without_prefetching_payloads(
    progress_runtime: tuple[Runtime, LocalServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services, multiple_questions=True)
    await rig.register()
    question_ids = [receipt["question_id"] for receipt in rig.register_results]
    assert len(question_ids) == 2 and question_ids[0] != question_ids[1]
    with monkeypatch.context() as patch:
        patch.setattr(runtime.team, "start", lambda: None)
        for identifier in question_ids:
            rig.question_id = identifier
            assert (
                await rig.owner(f"/回答并继续 {identifier}\n小型合成答案。", delivery="dispatch")
            )[0]["queued"] is True
        ordinary, _ = runtime.conversation.enqueue(
            rig.worker_id,
            "普通独立补充也应由正常停止收口。",
            source="synthetic:ordinary",
            principal="owner",
            mode="yolo",
            internal=True,
            max_pending=runtime.config.settings.limits.max_pending_runs,
        )
        assert runtime.store.pending_inputs(rig.worker_id) == len(question_ids) + 1
        probe = CancelReadProbe()
        original_factory = runtime.store.db.row_factory
        runtime.store.db.row_factory = probe.row
        try:
            runtime.store.cancel_inputs(rig.worker_id)
        finally:
            runtime.store.db.row_factory = original_factory
        assert probe.metadata_columns
        assert all("payload" not in columns for columns in probe.metadata_columns)
        assert probe.projected_payload_bytes == []
        assert runtime.store.pending_inputs(rig.worker_id) == 0
        assert runtime.team.get(rig.worker_id)["status"] == "completed"
        assert runtime.team.get(rig.worker_id)["generation"] == 1
        assert (
            runtime.store.db.execute(
                "SELECT state FROM ingress WHERE id=?", (ordinary["id"],)
            ).fetchone()[0]
            == "cancelled"
        )
        for identifier in question_ids:
            record = rig.coordinator._record(identifier)
            assert record["status"] == "answered"
            assert record["delivery"]["state"] == "cancelled"
            assert record["delivery"]["claim"] is None
    assert rig.worker_continuations == []


async def test_cancel_oversized_pending_answer_gates_bytes_before_decode_and_rolls_back(
    progress_runtime: tuple[Runtime, LocalServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    original_context = runtime.config.settings.limits.max_context_chars
    body = "合" * 2700
    assert (await rig.owner(f"/回答问题 {rig.question_id}\n{body}"))[0]["answer_recorded"] is True
    with monkeypatch.context() as patch:
        patch.setattr(runtime.team, "start", lambda: None)
        assert (await rig.owner(f"/继续问题 {rig.question_id}", action="dispatch"))[0][
            "queued"
        ] is True
        record = rig.coordinator._record(rig.question_id)
        input_id = record["delivery"]["input_id"]
        before = tuple(
            runtime.store.db.execute(
                "SELECT seq,state,delivered_seq,run_id,length(CAST(payload AS BLOB)) "
                "FROM ingress WHERE id=?",
                (input_id,),
            ).fetchone()
        )
        revision = runtime.store.db.execute(
            "SELECT json_extract(data,'$.revision'),updated FROM objects WHERE kind='owner_question' AND id=?",
            (rig.question_id,),
        ).fetchone()
        assert revision is not None
        before_revision = tuple(revision)
        runtime.config.update({"limits": {"max_context_chars": 4000}})
        byte_budget = rig.coordinator._bytes
        assert before[4] > byte_budget
        probe = CancelReadProbe()
        decoded_sizes: list[int] = []
        original_factory, original_decode = (
            runtime.store.db.row_factory,
            owner_questions_module._object,
        )

        def decode(raw: bytes) -> dict[str, Any]:
            decoded_sizes.append(len(raw))
            return original_decode(raw)

        patch.setattr(owner_questions_module, "_object", decode)
        runtime.store.db.row_factory = probe.row
        try:
            with pytest.raises(EvidenceError) as failure:
                runtime.store.cancel_inputs(rig.worker_id)
            assert failure.value.code == "output_budget"
        finally:
            runtime.store.db.row_factory = original_factory
            runtime.config.update({"limits": {"max_context_chars": original_context}})
        assert probe.metadata_columns
        assert all("payload" not in columns for columns in probe.metadata_columns)
        assert probe.projected_payload_bytes == []
        assert any(size > byte_budget and withheld for size, withheld in probe.gated_payloads)
        assert decoded_sizes == []
        after = tuple(
            runtime.store.db.execute(
                "SELECT seq,state,delivered_seq,run_id,length(CAST(payload AS BLOB)) "
                "FROM ingress WHERE id=?",
                (input_id,),
            ).fetchone()
        )
        assert after == before
        assert (
            tuple(
                runtime.store.db.execute(
                    "SELECT json_extract(data,'$.revision'),updated FROM objects WHERE kind='owner_question' AND id=?",
                    (rig.question_id,),
                ).fetchone()
            )
            == before_revision
        )
        assert runtime.store.pending_inputs(rig.worker_id) == 1
        assert runtime.team.get(rig.worker_id)["status"] == "queued"
        assert runtime.team.get(rig.worker_id)["generation"] == 1
    assert rig.worker_continuations == []


async def test_cancel_queued_answer_while_scheduler_waits_for_configuration_lock(
    progress_runtime: tuple[Runtime, LocalServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    await rig.owner(f"/回答问题 {rig.question_id}\n准确原答先保存。")
    tool_started, allow_handler, scheduler_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_emit, original_get = runtime.bus.emit, runtime.store.get

    async def emit(
        kind: str,
        data: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        result = await original_emit(kind, data, session_id)
        if (
            kind == "tool.started"
            and session_id == runtime.main_session()["id"]
            and data.get("name") == "send_input"
        ):
            tool_started.set()
            await allow_handler.wait()
        return result

    def get(kind: str, identifier: str) -> dict[str, Any] | None:
        result = original_get(kind, identifier)
        if (
            kind == "agent"
            and identifier == rig.worker_id
            and asyncio.current_task() is runtime.team._active.get(rig.worker_id)
        ):
            scheduler_read.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(runtime.bus, "emit", emit)
        patch.setattr(runtime.store, "get", get)
        owner_task = asyncio.create_task(
            rig.owner(f"/继续问题 {rig.question_id}", action="dispatch")
        )
        await asyncio.wait_for(tool_started.wait(), 4)
        await runtime.configuration_lock.acquire()
        try:
            allow_handler.set()
            await asyncio.wait_for(scheduler_read.wait(), 4)
            runtime.store.cancel_inputs(rig.worker_id)
            assert runtime.team.get(rig.worker_id)["status"] == "completed"
        finally:
            runtime.configuration_lock.release()
            allow_handler.set()
        await owner_task
        await eventually(runtime, lambda: rig.worker_id not in runtime.team._active)
    assert runtime.team.get(rig.worker_id)["generation"] == 1
    assert rig.worker_continuations == []
    record = rig.coordinator._record(rig.question_id)
    assert record["delivery"]["state"] == "cancelled"
    assert record["delivery"]["claim"] is None


@pytest.mark.parametrize("field", ["body", "original_text", "input_id"])
async def test_recorded_owner_answer_single_field_corruption_is_not_read_or_dispatched(
    progress_runtime: tuple[Runtime, LocalServices],
    field: str,
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    original_text = f"/回答问题 {rig.question_id}\n只补真实原答，不做外发。"
    assert (await rig.owner(original_text))[0]["answer_recorded"] is True
    record = rig.coordinator._record(rig.question_id)
    owner = record["answer"]["owner"]
    canonical_id = owner["input_id"]
    canonical = runtime.store.ingress_audit_bytes(
        owner["input_seq"], max_bytes=rig.coordinator._bytes
    )
    assert canonical is not None
    if field == "input_id":
        # Use another genuinely admitted owner input rather than inventing an
        # owner identity or changing the original canonical input/archive.
        await rig.owner("这是一条独立的真实主人输入。", repeats=0)
        other = next(
            message
            for message in reversed(runtime.store.messages(runtime.main_session()["id"]))
            if message.get("role") == "user"
            and message.get("content") == "这是一条独立的真实主人输入。"
        )
        owner[field] = other["id"]
    elif field == "body":
        owner[field] = "损坏的伪原答：请立即发布。"
    else:
        owner[field] = f"/回答问题 {rig.question_id}\n损坏的另一份原文。"
    runtime.store.put("owner_question", record)
    with pytest.raises(EvidenceError, match="不可|不一致|损坏"):
        rig.coordinator.read(rig.task_id, record_id=rig.question_id)
    result = (await rig.owner(f"/继续问题 {rig.question_id}", action="dispatch"))[0]
    assert "error" in result
    assert runtime.store.pending_inputs(rig.worker_id) == 0
    assert runtime.team.get(rig.worker_id)["generation"] == 1
    original_row = runtime.store.db.execute(
        "SELECT seq FROM ingress WHERE id=?", (canonical_id,)
    ).fetchone()
    assert original_row is not None
    assert (
        runtime.store.ingress_audit_bytes(original_row[0], max_bytes=rig.coordinator._bytes)
        == canonical
    )


@pytest.mark.parametrize("field", ["tool_message_id", "tool_seq", "input_content"])
async def test_asked_source_locator_corruption_is_not_exposed_or_used_for_dispatch(
    progress_runtime: tuple[Runtime, LocalServices],
    field: str,
) -> None:
    runtime, services = progress_runtime
    rig = QuestionRig(runtime, services)
    await rig.register()
    assert (await rig.owner(f"/回答问题 {rig.question_id}\n核对原发问凭据。"))[0][
        "accepted"
    ] is True
    record = rig.coordinator._record(rig.question_id)
    if field == "tool_message_id":
        final = next(
            message
            for message in reversed(runtime.store.messages(rig.worker_id))
            if message.get("role") == "assistant" and not message.get("tool_calls")
        )
        record["author"]["tool"]["message_id"] = final["id"]
    elif field == "tool_seq":
        record["author"]["tool"]["seq"] += 1
    else:
        record["author"]["input"]["content"] = "损坏的另一份发问来源。"
    runtime.store.put("owner_question", record)
    with pytest.raises(EvidenceError, match="不可|不一致|损坏"):
        rig.coordinator.read(rig.task_id, record_id=rig.question_id)
    result = (await rig.owner(f"/继续问题 {rig.question_id}", action="dispatch"))[0]
    assert "error" in result
    assert runtime.store.pending_inputs(rig.worker_id) == 0
    assert runtime.team.get(rig.worker_id)["generation"] == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"question": None},
        {"question": {"action": "register", "key": "key", "run_id": "forged"}},
        {"question": {"action": "register", "key": "key"}, "wake": False},
        {
            "question": {"action": "answer", "id": QUESTION_ID, "delivery": "record_only"},
            "attachment_ids": [],
        },
        {"question": {"action": "dispatch", "id": QUESTION_ID, "delivery": "dispatch"}},
    ],
)
async def test_bad_question_shapes_never_fall_back_to_ordinary_team_send(
    progress_runtime: tuple[Runtime, LocalServices],
    extra: dict[str, Any],
) -> None:
    runtime, _ = progress_runtime
    tool = next(
        tool
        for tool in runtime.team.tools(runtime.main_session()["id"], "yolo")
        if tool.name == "send_input"
    )
    args = {"target": "does-not-exist", "message": "不能作为普通补充投递", **extra}
    before = runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
    with pytest.raises(ValidationError):
        Draft202012Validator(tool.parameters).validate(args)
    with pytest.raises((ValueError, PermissionError)):
        await tool.handler(args)
    assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == before
