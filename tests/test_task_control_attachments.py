from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any, Literal

import httpx
import pytest
from test_task_briefs import bind
from test_task_control import Rig, durable_state
from test_task_control import rig as rig
from test_task_control_management import (
    ControlFixture,
    configure_without_execution,
    login,
    pause_request,
)
from test_task_control_management import (
    control_client as control_client,
)

from muli.attachments import MAX_ATTACHMENT_IDS
from muli.task_control import ControlResult, ControlUnavailable, OwnerControlRequest, _digest


async def upload(client: httpx.AsyncClient, name: str, content: str) -> dict[str, Any]:
    response = await client.post(
        "/api/uploads",
        content=content.encode("utf-8"),
        headers={"x-file-name": name, "content-type": "text/plain"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_owner_replan_appends_uploaded_evidence_without_replacing_original_brief(
    control_client: ControlFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, local, client, identifier = control_client
    await login(client)
    configure_without_execution(runtime, monkeypatch)
    old_file = await upload(client, "original.txt", "原始核查资料")
    new_file = await upload(client, "additional.txt", "主人补充的核查资料")
    original = await local.commitments.save(
        "核查本地项目",
        identifier=identifier,
        brief={
            "goal": "核查并整理完整结果",
            "constraints": ["保留原文件"],
            "evidence_refs": [{"kind": "attachment", "id": old_file["id"]}],
        },
    )
    worker = bind(runtime, identifier)
    path = f"/api/commitments/{identifier}/control"
    paused = await client.post(path, json=pause_request(0, "pause-before-evidence"))
    assert paused.status_code == 200
    assert paused.json()["current"]["scope"]["member_count"] == 1
    response = await client.post(
        path,
        json={
            "action": "request_replan",
            "expected_epoch": 1,
            "request_id": "owner-add-evidence",
            "additional_requirements": "保留两份资料，先核对再继续",
            "additional_attachment_ids": [new_file["id"]],
        },
    )
    assert response.status_code == 200, response.text
    current = local.commitments.get(identifier)
    assert current["brief"] == {
        "version": 1,
        "goal": original["brief"]["goal"],
        "constraints": ["保留原文件", "保留两份资料，先核对再继续"],
        "evidence_refs": [
            {"kind": "attachment", "id": old_file["id"]},
            {"kind": "attachment", "id": new_file["id"]},
        ],
    }
    assert (
        local.commitments.get(identifier, revision=original["revision"])["brief"]
        == original["brief"]
    )
    assert local.task_control.resolve_scope(worker["id"]) == identifier
    assert response.json()["current"]["control"]["epoch"] == 2
    assert response.json()["current"]["control"]["paused"] is True
    assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 1
    assert runtime.tasks == {}


@pytest.mark.parametrize("action", ["pause", "request_replan"])
def test_empty_attachment_request_preserves_the_historical_fingerprint(
    rig: Rig, action: Literal["pause", "request_replan"]
) -> None:
    epoch = 0
    if action == "request_replan":
        rig.pause()
        epoch = 1
    request = OwnerControlRequest(action, epoch, "legacy-empty", "")
    result = rig.command(request)
    assert result.receipt["fingerprint"] == _digest(
        {
            "commitment_id": rig.identifier,
            "action": action,
            "expected_epoch": epoch,
            "request_id": "legacy-empty",
            "additional_requirements": "",
        }
    )
    before = durable_state(rig.store)
    assert rig.command(request).receipt == result.receipt
    assert durable_state(rig.store) == before


@pytest.mark.parametrize(
    "identifiers",
    [
        ["a" * 32],
        ("invalid",),
        ("a" * 32, "a" * 32),
        tuple(f"{index:032x}" for index in range(MAX_ATTACHMENT_IDS + 1)),
    ],
)
def test_internal_attachment_request_requires_a_valid_frozen_bounded_sequence(
    identifiers: Any,
) -> None:
    with pytest.raises(ValueError):
        OwnerControlRequest("request_replan", 1, "invalid-list", "", identifiers)


def test_attachment_selection_is_immutable_and_pause_cannot_append_it() -> None:
    identifiers = ("a" * 32,)
    request = OwnerControlRequest("request_replan", 1, "frozen", "", identifiers)
    assert request.additional_attachment_ids == identifiers
    with pytest.raises(FrozenInstanceError):
        request.additional_attachment_ids = ()  # type: ignore[misc]
    with pytest.raises(ValueError, match="pause"):
        OwnerControlRequest("pause", 0, "pause", "", identifiers)


def test_nonempty_attachments_fail_closed_without_the_host_validator(rig: Rig) -> None:
    rig.pause()
    before = durable_state(rig.store)
    with pytest.raises(ControlUnavailable, match="attachments_unavailable"):
        rig.command(OwnerControlRequest("request_replan", 1, "no-files", "", ("a" * 32,)))
    assert durable_state(rig.store) == before


async def test_append_deduplicates_existing_refs_and_preserves_selection_order(
    control_client: ControlFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, local, client, identifier = control_client
    await login(client)
    configure_without_execution(runtime, monkeypatch)
    first = await upload(client, "first.txt", "旧资料")
    second = await upload(client, "second.txt", "第二份")
    third = await upload(client, "third.txt", "第三份")
    await local.commitments.save(
        "核查本地项目",
        identifier=identifier,
        brief={
            "goal": "完整目标",
            "constraints": ["原始限制"],
            "evidence_refs": [{"kind": "attachment", "id": first["id"]}],
        },
    )
    path = f"/api/commitments/{identifier}/control"
    assert (await client.post(path, json=pause_request(0, "pause"))).status_code == 200
    request = {
        "action": "request_replan",
        "expected_epoch": 1,
        "request_id": "append-ordered",
        "additional_attachment_ids": [third["id"], first["id"], second["id"]],
    }
    response = await client.post(path, json=request)
    assert response.status_code == 200, response.text
    assert local.commitments.get(identifier)["brief"] == {
        "version": 1,
        "goal": "完整目标",
        "constraints": ["原始限制"],
        "evidence_refs": [
            {"kind": "attachment", "id": item["id"]} for item in (first, third, second)
        ],
    }
    before = durable_state(runtime.store)
    request["additional_attachment_ids"] = [first["id"], third["id"], second["id"]]
    conflict = await client.post(path, json=request)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "request_id_conflict"
    assert durable_state(runtime.store) == before


async def test_lost_response_retries_the_original_receipt_before_rechecking_deleted_files(
    control_client: ControlFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, local, client, identifier = control_client
    await login(client)
    configure_without_execution(runtime, monkeypatch)
    item = await upload(client, "owner-evidence.txt", "只能追加一次")
    path = f"/api/commitments/{identifier}/control"
    assert (await client.post(path, json=pause_request(0, "pause"))).status_code == 200
    request = {
        "action": "request_replan",
        "expected_epoch": 1,
        "request_id": "lost-response",
        "additional_requirements": "保留原目标和资料",
        "additional_attachment_ids": [item["id"]],
    }
    original_validate = local.attachments.validate_references
    validated: list[list[str]] = []

    def validate(value: object) -> list[str]:
        assert runtime.store.db.in_transaction
        result = original_validate(value)
        validated.append(result)
        return result

    original_committed = runtime.task_control_committed

    async def lose_response(result: ControlResult) -> None:
        await original_committed(result)
        raise ConnectionError("response lost after commit")

    monkeypatch.setattr(local.attachments, "validate_references", validate)
    monkeypatch.setattr(runtime, "task_control_committed", lose_response)
    with pytest.raises(ConnectionError, match="response lost"):
        await client.post(path, json=request)
    assert validated == [[item["id"]]]
    receipt = next(
        receipt
        for receipt in runtime.store.list("task_control_receipt")
        if receipt["request_id"] == request["request_id"]
    )
    local.attachments.path(item["id"]).unlink()
    runtime.store.delete("attachment", item["id"])
    runtime.config.update({"models": {"main": {"model": ""}}})
    monkeypatch.setattr(runtime, "task_control_committed", original_committed)
    before = durable_state(runtime.store)
    retried = await client.post(path, json=request)
    assert retried.status_code == 200, retried.text
    assert retried.json()["receipt"] == receipt
    assert validated == [[item["id"]]]
    assert durable_state(runtime.store) == before
    changed = await client.post(path, json={**request, "additional_attachment_ids": []})
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "request_id_conflict"
    assert durable_state(runtime.store) == before


@pytest.mark.parametrize("failure", ["record", "file", "symlink", "directory"])
async def test_unavailable_additional_files_do_not_change_any_durable_task_facts(
    control_client: ControlFixture, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    runtime, local, client, identifier = control_client
    await login(client)
    configure_without_execution(runtime, monkeypatch)
    item = await upload(client, "invalidated.txt", "已登记的资料")
    path = f"/api/commitments/{identifier}/control"
    assert (await client.post(path, json=pause_request(0, "pause"))).status_code == 200
    attachment_path = local.attachments.path(item["id"])
    if failure == "record":
        runtime.store.delete("attachment", item["id"])
    else:
        attachment_path.unlink()
        if failure == "symlink":
            target = attachment_path.with_name("other-file")
            target.write_text("不能用符号链接替代", encoding="utf-8")
            try:
                attachment_path.symlink_to(target)
            except OSError:
                pytest.skip("平台不允许当前账户创建符号链接")
        elif failure == "directory":
            attachment_path.mkdir()
    before = durable_state(runtime.store)
    response = await client.post(
        path,
        json={
            "action": "request_replan",
            "expected_epoch": 1,
            "request_id": "bad-file",
            "additional_requirements": "不能部分追加",
            "additional_attachment_ids": [item["id"]],
        },
    )
    assert response.status_code == 400
    assert "附件不可用" in response.text
    assert durable_state(runtime.store) == before


@pytest.mark.parametrize("failure", ["epoch", "queue", "budget", "model"])
async def test_attachment_replan_keeps_original_atomic_capacity_and_epoch_guards(
    control_client: ControlFixture, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    runtime, _, client, identifier = control_client
    await login(client)
    configure_without_execution(runtime, monkeypatch)
    item = await upload(client, "bounded.txt", "仍需满足原容量约束")
    path = f"/api/commitments/{identifier}/control"
    assert (await client.post(path, json=pause_request(0, "pause"))).status_code == 200
    expected_status = {"epoch": 409, "queue": 429, "budget": 400, "model": 503}[failure]
    if failure == "queue":
        runtime.config.update({"limits": {"max_pending_runs": 1}})
        runtime.conversation.enqueue(
            runtime.main_session()["id"],
            "已有主人输入",
            source="web",
            principal="owner",
            mode="auto",
            max_pending=1,
        )
    elif failure == "budget":
        runtime.config.update({"limits": {"max_context_chars": 4000}})
    elif failure == "model":
        runtime.config.update({"models": {"main": {"model": ""}}})
    before = durable_state(runtime.store)
    response = await client.post(
        path,
        json={
            "action": "request_replan",
            "expected_epoch": 0 if failure == "epoch" else 1,
            "request_id": f"blocked-{failure}",
            "additional_requirements": "完整限制" * 1000,
            "additional_attachment_ids": [item["id"]],
        },
    )
    assert response.status_code == expected_status, response.text
    assert durable_state(runtime.store) == before


@pytest.mark.parametrize(
    "stage", ["ingress", "commitment", "commitment_revision", "task_control_receipt", "event"]
)
async def test_attachment_replan_rolls_back_all_writes_after_each_failure_stage(
    control_client: ControlFixture, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    runtime, _, client, identifier = control_client
    await login(client)
    configure_without_execution(runtime, monkeypatch)
    item = await upload(client, "transaction.txt", "不允许留下部分事务")
    path = f"/api/commitments/{identifier}/control"
    assert (await client.post(path, json=pause_request(0, "pause"))).status_code == 200
    before = durable_state(runtime.store)
    original_put = runtime.store._put
    original_event = runtime.store._event
    original_enqueue = runtime.store._enqueue_input

    def write(kind: str, value: dict[str, Any]) -> None:
        original_put(kind, value)
        if kind == stage:
            raise RuntimeError("injected write failure")

    def event(*args: Any, **kwargs: Any) -> dict[str, Any]:
        value = original_event(*args, **kwargs)
        if stage == "event":
            raise RuntimeError("injected event failure")
        return value

    def enqueue(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        value = original_enqueue(*args, **kwargs)
        if stage == "ingress":
            raise RuntimeError("injected ingress failure")
        return value

    monkeypatch.setattr(runtime.store, "_put", write)
    monkeypatch.setattr(runtime.store, "_event", event)
    monkeypatch.setattr(runtime.store, "_enqueue_input", enqueue)
    with pytest.raises(RuntimeError, match="injected"):
        await client.post(
            path,
            json={
                "action": "request_replan",
                "expected_epoch": 1,
                "request_id": "atomic-evidence",
                "additional_requirements": "原子新增完整要求",
                "additional_attachment_ids": [item["id"]],
            },
        )
    assert durable_state(runtime.store) == before


@pytest.mark.parametrize("identifiers", [["invalid"], ["a" * 32, "a" * 32], [False], "a" * 32])
async def test_http_attachment_input_reuses_existing_strict_reference_validation(
    control_client: ControlFixture, identifiers: Any
) -> None:
    runtime, _, client, identifier = control_client
    await login(client)
    before = durable_state(runtime.store)
    response = await client.post(
        f"/api/commitments/{identifier}/control",
        json={
            "action": "request_replan",
            "expected_epoch": 0,
            "request_id": "bad-reference-list",
            "additional_attachment_ids": identifiers,
        },
    )
    assert response.status_code == 422
    assert durable_state(runtime.store) == before


async def test_pause_cannot_append_an_uploaded_attachment(
    control_client: ControlFixture,
) -> None:
    runtime, _, client, identifier = control_client
    await login(client)
    item = await upload(client, "pause.txt", "暂停不能改变资料")
    before = durable_state(runtime.store)
    response = await client.post(
        f"/api/commitments/{identifier}/control",
        json={**pause_request(0, "pause-with-evidence"), "additional_attachment_ids": [item["id"]]},
    )
    assert response.status_code == 400
    assert durable_state(runtime.store) == before
