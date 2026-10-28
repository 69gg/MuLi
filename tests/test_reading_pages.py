"""Real tool archives, historical team identity and bounded host page proofs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from test_document_reading import docx
from test_runtime import FakeClient
from test_vision_archives_policy import Host
from test_vision_archives_policy import host as host

from muli.evidence import EvidenceError
from muli.providers import ModelTurn, ToolCall
from muli.store import new_id


def archived(host: Host, call_id: str | None = None) -> dict[str, Any]:
    return next(
        item
        for item in reversed(host.runtime.store.messages(host.runtime.main_session()["id"]))
        if item["role"] == "tool" and (call_id is None or item.get("tool_call_id") == call_id)
    )


def source(host: Host, message: dict[str, Any], *, before_seq: int | None = None) -> dict[str, Any]:
    page, policy = host.services.reading_pages.source(
        message["id"],
        message["evidence_revision"],
        before_seq=message["seq"] + 1 if before_seq is None else before_seq,
        main_id=host.runtime.main_session()["id"],
    )
    assert policy is None
    return page


async def real_page(host: Host) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    item = await host.save(("真实分页证据🙂\n" * 500).encode(), "页证据.txt")
    result = await host.drive("attachment_read", id=item["id"], offset_bytes=0)
    message = archived(host)
    assert "reading_page" in message
    return item, result, message


async def test_complete_paginated_page_uses_global_batch_boundary_and_exact_identity(
    host: Host,
) -> None:
    item, result, message = await real_page(host)
    assert result["has_more"] and result["truncated"]
    page = source(host, message)
    assert set(page) == {
        "store_instance",
        "main_id",
        "attachment_id",
        "reader",
        "source_revision",
        "revision",
        "offset_bytes",
        "length_bytes",
        "next_offset_bytes",
        "total_bytes",
        "has_more",
        "source_session_id",
        "source_agent_id",
        "source_run_id",
        "source_generation",
        "source_message_id",
        "source_message_revision",
    }
    assert page["attachment_id"] == item["id"]
    assert page["source_revision"] == page["revision"] == item["sha256"]
    assert page["source_session_id"] == page["main_id"]
    assert page["source_agent_id"] is None and page["source_generation"] is None
    assert page["length_bytes"] == len(result["content"].encode("utf-8"))
    assert page["next_offset_bytes"] == result["next_offset_bytes"]
    proof = message["reading_page"]
    assert proof["trigger"]["principal"] == "owner"
    assert len(json.dumps(proof, ensure_ascii=False).encode("utf-8")) <= 2048
    for boundary in (message["seq"] - 1, message["seq"], True):
        with pytest.raises(EvidenceError, match="真实阅读凭据"):
            source(host, message, before_seq=boundary)


@pytest.mark.parametrize(
    "change",
    ["body", "version", "assistant_call", "coordinates", "schema", "role", "provenance", "omitted"],
)
async def test_archived_body_version_call_and_host_metadata_cannot_be_substituted(
    host: Host,
    change: str,
) -> None:
    _, _, message = await real_page(host)
    source(host, message)
    store = host.runtime.store
    if change == "version":
        store.put("message", dict(message))
    elif change == "assistant_call":
        # Preserve the assistant revision deliberately: checking the actual call
        # must still reject a changed reader independently of revision fencing.
        with store.db:
            store.db.execute(
                "UPDATE objects SET data=json_set(data,'$.tool_calls[0].name','document_read') "
                "WHERE kind='message' AND id=?",
                (message["reading_page"]["assistant_id"],),
            )
    else:
        if change == "body":
            result = json.loads(message["content"])
            result["content"] = "同形伪造正文"
            message["content"] = json.dumps(result, ensure_ascii=False)
        elif change == "coordinates":
            message["reading_page"]["offset_bytes"] = True
        elif change == "schema":
            message["reading_page"]["schema_version"] = 2
        elif change == "role":
            message["role"] = "assistant"
        elif change == "provenance":
            message["content_provenance"] = "runtime_excerpt"
        else:
            message["content"] = json.dumps({"result_omitted": True, "code": "output_budget"})
        store.put("message", message)
    with pytest.raises(EvidenceError) as caught:
        source(host, message)
    assert caught.value.code == "reading_page_unavailable"


async def test_copied_json_from_the_real_handler_loses_the_result_bound_candidate(
    host: Host,
) -> None:
    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    item = await host.save("实际读取后复制成同形 JSON".encode(), "复制.txt")
    tool = host.runtime.tools["attachment_read"]

    async def copied(arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(await tool.handler(arguments))

    host.runtime.add_tool_provider(
        lambda runtime: [replace(tool, handler=copied)],
        replace=True,
    )
    result = await host.drive("attachment_read", id=item["id"], offset_bytes=0)
    assert result["content"] == "实际读取后复制成同形 JSON"
    message = archived(host)
    assert "reading_page" not in message
    with pytest.raises(EvidenceError):
        source(host, message)


@pytest.mark.parametrize("preserve_output", [True, False])
async def test_runtime_output_replacement_never_persists_a_page_credential(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    preserve_output: bool,
) -> None:
    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    item = await host.save(("完整分页🙂" * 500).encode(), "缩紧预算.txt")
    tool = host.runtime.tools["attachment_read"]
    host.runtime.add_tool_provider(
        lambda runtime: [replace(tool, preserve_output=preserve_output)],
        replace=True,
    )
    completed = host.runtime._tool_completed

    async def shrink(call: ToolCall, result: Any, session_id: str) -> None:
        await completed(call, result, session_id)
        if call.id == "shrunk-page":
            assert len(json.dumps(result, ensure_ascii=False)) > 1000
            host.runtime.config.update({"limits": {"max_output_chars": 1000}})

    monkeypatch.setattr(host.runtime, "_tool_completed", shrink)
    host.turns.extend(
        [
            ModelTurn(
                tool_calls=[
                    ToolCall(
                        "shrunk-page",
                        "attachment_read",
                        json.dumps({"id": item["id"], "offset_bytes": 0}),
                    )
                ]
            ),
            ModelTurn(content="预算已经收紧。"),
        ]
    )
    receipt = await host.runtime.submit("读取一次后收紧输出预算", mode="yolo")
    await asyncio.wait_for(host.runtime.wait(receipt["run_id"]), 3)
    message = archived(host, "shrunk-page")
    assert message["content_provenance"] == "runtime_excerpt"
    assert "reading_page" not in message
    output = json.loads(message["content"])
    if preserve_output:
        assert output.get("result_omitted") is True
    else:
        assert output.get("truncated") is True
    with pytest.raises(EvidenceError):
        source(host, message)


async def test_source_projects_bounded_metadata_without_loading_provider_state(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, message = await real_page(host)
    expected = source(host, message)
    store = host.runtime.store
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=json_set(data,'$.provider_data',?) "
            "WHERE kind='message' AND id=?",
            ("与核验无关" * 4000, message["id"]),
        )

    def unbounded(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("page evidence must not load complete objects or enumerate history")

    pages = host.services.reading_pages
    with monkeypatch.context() as scoped:
        scoped.setattr(store, "get", unbounded)
        scoped.setattr(store, "messages", unbounded)
        current, policy = pages.source(
            message["id"],
            message["evidence_revision"],
            before_seq=message["seq"] + 1,
            main_id=expected["main_id"],
        )
    assert current == expected and policy is None


async def test_oversized_assistant_call_array_and_page_body_fail_closed(host: Host) -> None:
    _, _, message = await real_page(host)
    source(host, message)
    store = host.runtime.store
    assistant_id = message["reading_page"]["assistant_id"]
    assistant = store.get("message", assistant_id)
    original = json.dumps(assistant["tool_calls"])
    huge = json.dumps(assistant["tool_calls"] + [{"id": "other", "arguments": "x" * 8000}])
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=json_set(data,'$.tool_calls',json(?)) "
            "WHERE kind='message' AND id=?",
            (huge, assistant_id),
        )
    with pytest.raises(EvidenceError):
        source(host, message)
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=json_set(data,'$.tool_calls',json(?)) "
            "WHERE kind='message' AND id=?",
            (original, assistant_id),
        )
    assert source(host, message)["source_message_id"] == message["id"]
    host.runtime.config.update({"limits": {"max_output_chars": 1000}})
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=json_set(data,'$.content',?) WHERE kind='message' AND id=?",
            ("x" * 4001, message["id"]),
        )
    with pytest.raises(EvidenceError):
        source(host, message)


async def test_completed_team_page_keeps_its_real_root_run_and_old_generation(host: Host) -> None:
    item, _, main_page = await real_page(host)
    runtime = host.runtime
    runtime.config.update(
        {"models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}}}
    )
    worker_turns = [
        ModelTurn(
            tool_calls=[
                ToolCall(
                    "team-page",
                    "attachment_read",
                    json.dumps({"id": item["id"], "offset_bytes": 0}),
                )
            ]
        ),
        ModelTurn(content="队友第一页已归档。"),
        ModelTurn(content="队友已进入新的代次。"),
    ]
    parent_turns = [ModelTurn(content="已接到队友报告。") for _ in range(3)]
    runtime.client_factory = lambda config: FakeClient(
        worker_turns if config.model == "worker" else parent_turns,
        host.seen,
    )  # type: ignore[assignment,return-value]
    main_id = runtime.main_session()["id"]
    agent = await runtime.team.spawn(
        "阅读队友", "只读取已提供的附件", parent_id=main_id, attachment_ids=[item["id"]]
    )
    await runtime.team.wait([agent["id"]], timeout=2)
    first = runtime.team.get(agent["id"])
    assert first["status"] == "completed" and first["generation"] == 1
    message = next(
        item
        for item in runtime.store.messages(agent["id"])
        if item.get("tool_call_id") == "team-page"
    )
    page = source(host, message)
    assert page["source_session_id"] == page["source_agent_id"] == agent["id"]
    assert page["source_run_id"] == first["run_id"] and page["source_generation"] == 1
    assert message["reading_page"]["trigger"]["principal"] == f"agent:{main_id}"
    assert runtime.task_control is not None
    assert runtime.task_control.resolve_scope(agent["id"]) is None
    assert host.services.reading_pages.root(agent["id"]) == main_id
    assert source(host, main_page)["source_agent_id"] is None
    await runtime.team.send(agent["id"], "继续下一代次", wake=True)
    await runtime.team.wait([agent["id"]], timeout=2)
    second = runtime.team.get(agent["id"])
    assert second["status"] == "completed" and second["generation"] == 2
    assert second["run_id"] != first["run_id"]
    assert source(host, message) == page
    with pytest.raises(EvidenceError):
        source(host, message, before_seq=message["seq"])
    broken = dict(second, parent_id="missing-parent")
    runtime.store.put("agent", broken)
    with pytest.raises(EvidenceError):
        source(host, message)


@pytest.mark.parametrize("target", ["main_id", "source_id", "source_agent", "ancestor_agent"])
async def test_root_rejects_misaligned_ids_at_every_level(host: Host, target: str) -> None:
    store = host.runtime.store
    main_id = host.runtime.main_session()["id"]
    parent_id, child_id = new_id(), new_id()
    store.put_many(
        [
            ("session", {"id": identifier, "kind": "team", "principal": "owner"})
            for identifier in (parent_id, child_id)
        ]
        + [
            ("agent", {"id": parent_id, "session_id": parent_id, "parent_id": main_id}),
            ("agent", {"id": child_id, "session_id": child_id, "parent_id": parent_id}),
        ]
    )
    pages = host.services.reading_pages
    assert pages.root(child_id) == main_id
    kind, identifier, field = {
        "main_id": ("session", main_id, "id"),
        "source_id": ("session", child_id, "id"),
        "source_agent": ("agent", child_id, "session_id"),
        "ancestor_agent": ("agent", parent_id, "session_id"),
    }[target]
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=json_set(data,?,?) WHERE kind=? AND id=?",
            (f"$.{field}", "wrong-real-id", kind, identifier),
        )
    with pytest.raises(EvidenceError):
        pages.root(child_id)


async def test_document_policy_is_captured_before_the_reader_and_rechecked_after_it(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muli import local_services

    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    item = await host.save(docx("实际读取策略🙂" * 100), "文档.docx")
    original = local_services.read_document
    policy = host.services.document_settings
    initial = host.services.reading_pages.document_policy()

    async def changing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        policy.replace(policy.read().model_copy(update={"max_xml_depth": 64}))
        return await original(*args, **kwargs)

    monkeypatch.setattr(local_services, "read_document", changing)
    result = await host.drive("document_read", id=item["id"])
    assert result["format"] == "docx"
    assert host.services.reading_pages.document_policy() != initial
    assert "reading_page" not in archived(host)
    monkeypatch.setattr(local_services, "read_document", original)
    await host.drive("document_read", id=item["id"])
    message = archived(host)
    page, digest = host.services.reading_pages.source(
        message["id"],
        message["evidence_revision"],
        before_seq=message["seq"] + 1,
        main_id=host.runtime.main_session()["id"],
    )
    assert page["reader"] == "document_read"
    assert digest == host.services.reading_pages.document_policy()
