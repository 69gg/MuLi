"""A real teammate page stays attributable and requires an explicit owner write."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from test_attention import eventually
from test_document_reading import docx
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call, completed, tool_result
from test_vision_archives_policy import Host
from test_vision_archives_policy import host as host

from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent

_REPORT_ACK = "已收到队友报告，未修改阅读位置。"


async def test_old_team_docx_page_can_be_saved_by_owner_and_shared_read_only(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from muli import document_reading

    runtime = host.runtime
    runtime.config.update(
        {
            "limits": {"max_output_chars": 1800},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
        }
    )
    extractions = 0
    extract_docx = document_reading.extract_docx

    def extract(*args: Any, **kwargs: Any) -> bytes:
        nonlocal extractions
        extractions += 1
        return extract_docx(*args, **kwargs)

    monkeypatch.setattr(document_reading, "extract_docx", extract)
    item = await host.save(docx("队友真正提取的资料🙂\n" * 500), "队友资料.docx")
    assert (await host.drive("reading_bookmark_list", limit=1))["items"] == []
    main_id = runtime.main_session()["id"]
    worker_round = 0
    worker_seen: list[list[Message]] = []
    old_page: dict[str, Any] = {}

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_round
            worker_round += 1
            worker_seen.append(list(messages))
            names = {tool["function"]["name"] for tool in tools}
            assert {"reading_bookmark_list", "reading_bookmark_read"} <= names
            assert {"reading_bookmark_set", "reading_bookmark_delete"}.isdisjoint(names)
            if worker_round == 1:
                turn = ModelTurn(
                    tool_calls=[call("team-docx-page", "document_read", id=item["id"])]
                )
            elif worker_round == 2:
                result = tool_result(messages, "team-docx-page")
                assert result["format"] == "docx" and result["has_more"]
                turn = ModelTurn(content="第一代次的真实文档页已归档。")
            elif worker_round == 3:
                turn = ModelTurn(content="第二代次已完成，旧页仍是第一代次的历史。")
            elif worker_round == 4:
                turn = ModelTurn(
                    tool_calls=[
                        call("team-bookmark-list", "reading_bookmark_list", limit=1),
                        call("team-bookmark-read", "reading_bookmark_read", id=item["id"]),
                    ]
                )
            elif worker_round == 5:
                listing = tool_result(messages, "team-bookmark-list")
                reading = tool_result(messages, "team-bookmark-read")
                assert len(listing["items"]) == 1
                assert listing["items"][0]["id"] == reading["id"] == item["id"]
                assert reading["generation"] == 1
                assert reading["source_page"]["message_id"] == old_page["id"]
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "team-forbidden-set",
                            "reading_bookmark_set",
                            id=item["id"],
                            page_message_id=old_page["id"],
                            page_message_revision=old_page["evidence_revision"],
                            position="start",
                            expected_generation=1,
                        ),
                        call(
                            "team-forbidden-delete",
                            "reading_bookmark_delete",
                            id=item["id"],
                            expected_generation=1,
                        ),
                    ]
                )
            else:
                assert worker_round == 6
                assert tool_result(messages, "team-forbidden-set").get("error")
                assert tool_result(messages, "team-forbidden-delete").get("error")
                turn = ModelTurn(content="队友只能查询位置，未保存或清除主人的书签。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        if config.model == "worker":
            return Worker([], worker_seen)
        # Drain genuine team reports separately from the owner's scripted drive.
        turns = host.turns or [ModelTurn(content=_REPORT_ACK)]
        return FakeClient(turns, host.seen)

    runtime.client_factory = factory  # type: ignore[assignment]
    agent = await runtime.team.spawn(
        "文档阅读队友",
        "读取已交付文档的一页并报告，不保存主人书签。",
        parent_id=main_id,
        attachment_ids=[item["id"]],
    )
    await runtime.team.wait([agent["id"]], timeout=2)
    await eventually(
        lambda: (
            len(completed(runtime, _REPORT_ACK)) == 1 and runtime.team.idle and not runtime.tasks
        )
    )
    first = runtime.team.get(agent["id"])
    assert first["status"] == "completed" and first["generation"] == 1
    old_page = next(
        message
        for message in runtime.store.messages(agent["id"])
        if message.get("tool_call_id") == "team-docx-page" and message["role"] == "tool"
    )
    assert old_page["content_provenance"] == "accepted"
    proof = old_page["reading_page"]
    assert proof["source_session_id"] == proof["source_agent_id"] == agent["id"]
    assert proof["source_run_id"] == first["run_id"] and proof["source_generation"] == 1
    assert proof["trigger"]["principal"] == f"agent:{main_id}"
    assert extractions == 1

    await runtime.team.send(agent["id"], "进入下一代次，只报告完成。", wake=True)
    await runtime.team.wait([agent["id"]], timeout=2)
    await eventually(
        lambda: (
            len(completed(runtime, _REPORT_ACK)) == 2 and runtime.team.idle and not runtime.tasks
        )
    )
    second = runtime.team.get(agent["id"])
    assert second["status"] == "completed" and second["generation"] == 2
    assert second["run_id"] != first["run_id"] and worker_round == 3
    assert (await host.drive("reading_bookmark_list", limit=1))["items"] == []
    saved = await host.drive(
        "reading_bookmark_set",
        id=item["id"],
        page_message_id=old_page["id"],
        page_message_revision=old_page["evidence_revision"],
        position="next",
        expected_generation=0,
    )
    assert saved["state"] == "active" and saved["generation"] == 1
    assert saved["reader"] == "document_read" and saved["verification"] == "not_checked"
    assert saved["offset_bytes"] == json.loads(old_page["content"])["next_offset_bytes"]
    assert saved["source_page"] == {
        "session_id": agent["id"],
        "agent_id": agent["id"],
        "run_id": first["run_id"],
        "generation": 1,
        "message_id": old_page["id"],
        "message_revision": old_page["evidence_revision"],
    }
    assert extractions == 1

    await runtime.team.send(agent["id"], "查询明确书签并核查写入口不可用。", wake=True)
    await runtime.team.wait([agent["id"]], timeout=2)
    await eventually(
        lambda: (
            len(completed(runtime, _REPORT_ACK)) == 3 and runtime.team.idle and not runtime.tasks
        )
    )
    third = runtime.team.get(agent["id"])
    assert third["status"] == "completed" and third["generation"] == 3
    assert worker_round == 6
    messages = runtime.store.messages(agent["id"])
    for identifier in ("team-bookmark-list", "team-bookmark-read"):
        message = next(item for item in messages if item.get("tool_call_id") == identifier)
        assert message["content_provenance"] == "accepted" and message["preserve_content"]
    for identifier in ("team-forbidden-set", "team-forbidden-delete"):
        message = next(item for item in messages if item.get("tool_call_id") == identifier)
        assert json.loads(message["content"]).get("error")
    assert (await host.drive("reading_bookmark_read", id=item["id"])) == saved
    assert extractions == 1 and host.requests == []
    assert (
        runtime.store.db.execute(
            "SELECT count(*) FROM events WHERE type='reading_bookmark.set'"
        ).fetchone()[0]
        == 1
    )
    assert (
        runtime.store.db.execute(
            "SELECT count(*) FROM events WHERE type='reading_bookmark.deleted'"
        ).fetchone()[0]
        == 0
    )
