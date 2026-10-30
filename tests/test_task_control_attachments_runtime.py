from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_task_control_attachments import upload
from test_task_control_management import login, pause_request
from test_task_control_runtime import wait_agent

from muli.local_services import LocalServices
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.tools import Tool, parameters


def call(identifier: str, name: str, /, **arguments: Any) -> ToolCall:
    return ToolCall(identifier, name, json.dumps(arguments, ensure_ascii=False))


def result(messages: list[Message], identifier: str) -> dict[str, Any]:
    message = next(message for message in messages if message.tool_call_id == identifier)
    value = json.loads(message.content)
    assert isinstance(value, dict)
    return value


async def test_uploaded_evidence_reaches_the_original_teammate_only_after_main_read_and_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("此旅程不允许真实网络")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "server": {"password": "local-control-test"},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
        }
    )
    entered, release = asyncio.Event(), asyncio.Event()
    main_seen: list[list[Message]] = []
    worker_seen: list[list[Message]] = []
    effects: list[str] = []
    main_round, worker_round = 0, 0
    task_id, worker_id, original_id, additional_id = "", "", "", ""
    old_body, new_body = "ORIGINAL-EVIDENCE-BODY", "OWNER-ADDITIONAL-EVIDENCE-BODY"
    goal, original_constraint = "核查完整目标并交回证据", "保留原始文件与完整限制"
    additional_constraint = "同时核对主人新增资料"

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_round
            main_round += 1
            main_seen.append(list(messages))
            if main_round == 1:
                turn = ModelTurn(
                    tool_calls=[call("read-task", "commitment_read", identifier=task_id)]
                )
            elif main_round == 2:
                brief = json.loads(result(messages, "read-task")["text"])
                assert brief["evidence_refs"] == [
                    {"kind": "attachment", "id": original_id},
                    {"kind": "attachment", "id": additional_id},
                ]
                rendered = str(messages)
                assert all(
                    text in rendered
                    for text in (
                        goal,
                        original_constraint,
                        additional_constraint,
                        original_id,
                        additional_id,
                    )
                )
                assert '"epoch":2' in rendered
                assert old_body not in rendered and new_body not in rendered
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "approve-original",
                            "task_approve_plan",
                            observed_epoch=2,
                            plan="按完整原目标核对两份资料",
                            targets=[worker_id],
                        )
                    ]
                )
            else:
                assert main_round < 8
                turn = ModelTurn(content="已按原任务交给原队友，并保留完整资料")
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_round
            worker_round += 1
            worker_seen.append(list(messages))
            if worker_round == 1:
                assert original_id in str(messages) and additional_id not in str(messages)
                entered.set()
                await release.wait()
                turn = ModelTurn(tool_calls=[call("old-write", "probe")])
            elif worker_round == 2:
                rendered = str(messages)
                assert all(
                    text in rendered
                    for text in (
                        goal,
                        original_constraint,
                        additional_constraint,
                        original_id,
                        additional_id,
                    )
                )
                assert '"epoch":2' in rendered
                assert old_body not in rendered and new_body not in rendered
                names = {schema["function"]["name"] for schema in tools}
                assert "task_approve_plan" not in names
                turn = ModelTurn(
                    tool_calls=[
                        call("read-own-task", "commitment_read"),
                        call("read-original", "attachment_read", id=original_id),
                        call("read-additional", "attachment_read", id=additional_id),
                    ]
                )
            else:
                assert worker_round == 3
                brief = json.loads(result(messages, "read-own-task")["text"])
                assert brief["goal"] == goal
                assert result(messages, "read-original")["content"] == old_body
                assert result(messages, "read-additional")["content"] == new_body
                turn = ModelTurn(content="原任务的两份资料已核查")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Worker([], worker_seen) if config.model == "worker" else Main([], main_seen)

    async def probe(args: dict[str, Any]) -> bool:
        effects.append("old write executed")
        return True

    runtime.client_factory = factory  # type: ignore[assignment]
    runtime.tools["probe"] = Tool("probe", "test", parameters({}), probe, "write")
    app = create_app(runtime.config, runtime.data_dir, runtime)
    expected_brief: dict[str, Any] = {}
    final_generation = 0
    try:
        async with app.router.lifespan_context(app):
            local: LocalServices = app.state.local_services
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
            ) as client:
                await login(client)
                original = await upload(client, "original.txt", old_body)
                additional = await upload(client, "additional.txt", new_body)
                original_id, additional_id = original["id"], additional["id"]
                item = await local.commitments.save(
                    "原受控任务",
                    brief={
                        "goal": goal,
                        "constraints": [original_constraint],
                        "evidence_refs": [{"kind": "attachment", "id": original_id}],
                    },
                )
                task_id = item["id"]
                worker = await runtime.team.spawn(
                    "原核查队友", "按原始说明核查", commitment_id=task_id
                )
                worker_id = worker["id"]
                await asyncio.wait_for(entered.wait(), 2)
                path = f"/api/commitments/{task_id}/control"
                pause = await client.post(path, json=pause_request(0, "pause-old-run"))
                assert pause.status_code == 200
                release.set()
                paused = await wait_agent(runtime, worker_id, "paused")
                assert paused["generation"] == 1 and not effects and main_round == 0
                assert len(worker_seen) == 1
                response = await client.post(
                    path,
                    json={
                        "action": "request_replan",
                        "expected_epoch": 1,
                        "request_id": "add-real-evidence",
                        "additional_requirements": additional_constraint,
                        "additional_attachment_ids": [additional_id],
                    },
                )
                assert response.status_code == 200, response.text
                receipt = response.json()["receipt"]
                main_id = runtime.main_session()["id"]

                def claimed_replan_run() -> str | None:
                    ingress = runtime.store.db.execute(
                        "SELECT session_id,payload,run_id FROM ingress WHERE id=?",
                        (receipt["ingress_id"],),
                    ).fetchone()
                    assert ingress is not None and ingress["session_id"] == main_id
                    payload = json.loads(ingress["payload"])
                    assert payload["task_control"]["receipt_id"] == receipt["id"]
                    return ingress["run_id"]

                async with asyncio.timeout(2):
                    async with runtime.bus.changed:
                        await runtime.bus.changed.wait_for(lambda: claimed_replan_run() is not None)
                    main_run = claimed_replan_run()
                    assert main_run is not None
                    main_record = runtime.store.get("run", main_run)
                    assert main_record is not None and main_record["status"] != "failed", (
                        main_record
                    )
                    await runtime.wait(main_run)
                completed = await wait_agent(runtime, worker_id, "completed")
                assert completed["id"] == worker_id and completed["generation"] == 2
                assert completed["completion_revision"] == 2 and not effects
                final_generation = completed["generation"]
                assert len(worker_seen) == 3 and len(main_seen) >= 2
                deliveries = runtime.store.list("task_plan_delivery")
                assert len(deliveries) == 1 and deliveries[0]["target_session_id"] == worker_id
                assert deliveries[0]["state"] == "delivered"
                expected_brief = local.commitments.get(task_id)["brief"]
                assert expected_brief["constraints"] == [original_constraint, additional_constraint]
                assert len(expected_brief["evidence_refs"]) == 2
    finally:
        release.set()
        await runtime.close()

    reopened = Runtime(
        runtime.config,
        runtime.data_dir,
        client_factory=factory,  # type: ignore[arg-type]
    )
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    async with reopened_app.router.lifespan_context(reopened_app):
        restored: LocalServices = reopened_app.state.local_services
        assert restored.commitments.get(task_id)["brief"] == expected_brief
        assert reopened.team.get(worker_id)["generation"] == final_generation
        scope = (await reopened._prepare_request(worker_id)).execution_scope
        assert scope is not None and scope.epoch == scope.approved_epoch == 2
        assert json.loads(scope.brief_json) == expected_brief
        assert restored.attachments.path(original_id).read_text(encoding="utf-8") == old_body
        assert restored.attachments.path(additional_id).read_text(encoding="utf-8") == new_body
