"""Real main/team find-to-read journeys without lending find a reading proof."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, NoReturn

import httpx
import pytest
from test_attention import eventually
from test_document_reading import docx
from test_reading_bookmark_runtime import bookmark_arguments
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call, completed, tool_result
from test_vision_archives_policy import Host
from test_vision_archives_policy import host as host

from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent


@pytest.fixture
def external_requests(host: Host, monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Install after Host's controlled transport; these journeys need no HTTP."""
    requests: list[httpx.Request] = []

    async def forbidden(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> NoReturn:
        requests.append(request)
        raise AssertionError("find/read/bookmark journeys must not request external services")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    return requests


def stored_tool(host: Host, session_id: str, call_id: str) -> dict[str, Any]:
    return next(
        message
        for message in host.runtime.store.messages(session_id)
        if message["role"] == "tool" and message.get("tool_call_id") == call_id
    )


async def test_main_find_is_preserved_but_only_a_later_real_read_can_save_a_bookmark(
    host: Host, external_requests: list[httpx.Request]
) -> None:
    runtime = host.runtime
    runtime.config.update(
        {"limits": {"max_output_chars": 1800}, "updates": {"check_on_start": False}}
    )
    query = "查找目标"
    context = "只有真实阅读才会出现的上下文。"
    text = "前置背景🙂" * 80 + query + context + "后续资料🙂" * 300 + query
    item = await host.save(text.encode(), "主助手定位资料.txt")
    main_id = runtime.main_session()["id"]
    rounds = 0
    requested_models: list[str] = []

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            host.seen.append(list(messages))
            names = {tool["function"]["name"] for tool in tools}
            assert {"attachment_find", "attachment_read", "reading_bookmark_set"} <= names
            if rounds == 1:
                assert context not in str(messages)
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "find-main-locations",
                            "attachment_find",
                            id=item["id"],
                            reader="attachment_read",
                            query=query,
                            limit=1,
                        )
                    ]
                )
            elif rounds == 2:
                result = tool_result(messages, "find-main-locations")
                assert result["has_more"] and len(result["items"]) == 1
                assert context not in str(messages)
                page = stored_tool(host, main_id, "find-main-locations")
                assert page["content_provenance"] == "accepted" and page["preserve_content"]
                assert "reading_page" not in page
                replay = next(
                    message for message in messages if message.tool_call_id == "find-main-locations"
                )
                assert replay.preserve_content and replay.content == page["content"]
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "find-main-rejected-bookmark",
                            "reading_bookmark_set",
                            **bookmark_arguments(item, page),
                        )
                    ]
                )
            elif rounds == 3:
                rejected = tool_result(messages, "find-main-rejected-bookmark")
                assert rejected.get("error") and rejected["code"] == "reading_page_unavailable"
                assert (
                    runtime.store.db.execute(
                        "SELECT count(*) FROM events WHERE type='reading_bookmark.set'"
                    ).fetchone()[0]
                    == 0
                )
                result = tool_result(messages, "find-main-locations")
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "find-main-read",
                            result["reader"],
                            **result["items"][0]["read_arguments"],
                        )
                    ]
                )
            elif rounds == 4:
                read = tool_result(messages, "find-main-read")
                assert read["content"].startswith(query + context)
                page = stored_tool(host, main_id, "find-main-read")
                assert page["content_provenance"] == "accepted" and page["preserve_content"]
                proof = page["reading_page"]
                assert proof["source_session_id"] == main_id
                assert proof["source_agent_id"] is None and proof["source_generation"] is None
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "find-main-saved-bookmark",
                            "reading_bookmark_set",
                            **bookmark_arguments(item, page),
                        )
                    ]
                )
            else:
                assert rounds == 5
                saved = tool_result(messages, "find-main-saved-bookmark")
                read = tool_result(messages, "find-main-read")
                page = stored_tool(host, main_id, "find-main-read")
                assert saved["state"] == "active" and saved["generation"] == 1
                assert saved["offset_bytes"] == read["next_offset_bytes"]
                assert saved["source_page"]["message_id"] == page["id"]
                assert saved["source_page"]["message_revision"] == page["evidence_revision"]
                turn = ModelTurn(content="已定位并实际阅读，书签已保存。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        requested_models.append(config.model)
        assert config.model == "main"
        return Main([], host.seen)

    runtime.client_factory = factory  # type: ignore[assignment]
    receipt = await runtime.submit(
        "在交付资料里定位查找目标，实际阅读第一处，再保存本页末尾的续读位置。",
        mode="yolo",
        attachment_ids=[item["id"]],
    )
    await asyncio.wait_for(runtime.wait(receipt["run_id"]), 3)
    assert runtime.store.get("run", receipt["run_id"])["status"] == "completed"
    assert rounds == 5 and requested_models == ["main"]
    messages = runtime.store.messages(main_id)
    owners = [
        message
        for message in messages
        if message["role"] == "user" and message.get("principal") == "owner"
    ]
    assert len(owners) == 1
    find = stored_tool(host, main_id, "find-main-locations")
    read = stored_tool(host, main_id, "find-main-read")
    assert "reading_page" not in find
    assert read["reading_page"]["source_run_id"] == receipt["run_id"]
    assert json.loads(find["content"])["items"][0]["offset_bytes"] == len(
        ("前置背景🙂" * 80).encode()
    )
    assert (
        runtime.store.db.execute(
            "SELECT count(*) FROM events WHERE type='reading_bookmark.set'"
        ).fetchone()[0]
        == 1
    )
    assert external_requests == [] and host.requests == []


async def test_real_teammate_schema_finds_and_reads_the_delivered_docx_with_its_own_identity(
    host: Host, external_requests: list[httpx.Request]
) -> None:
    runtime = host.runtime
    runtime.config.update(
        {
            "limits": {"max_output_chars": 1800},
            "updates": {"check_on_start": False},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
        }
    )
    query = "队友目标"
    context = "TEAM-CONTEXT-ONLY-5b31"
    item = await host.save(
        docx("队友前文🙂" * 30 + query + context + "后续正文🙂" * 200),
        "传入队友的资料.docx",
    )
    main_id = runtime.main_session()["id"]
    main_round = 0
    agent_id: str | None = None
    worker_round = 0
    worker_stage = "not_started"
    worker_seen: list[list[Message]] = []
    requested_models: list[str] = []
    worker_release = asyncio.Event()
    assigned = "已将交付资料分派给正文定位队友。"
    acknowledgement = "收到队友定位及真实阅读报告，未保存书签。"

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_round, agent_id
            main_round += 1
            host.seen.append(list(messages))
            if main_round == 1:
                assert "spawn_agent" in {tool["function"]["name"] for tool in tools}
                assert item["id"] in str(messages)
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "find-team-spawn",
                            "spawn_agent",
                            task_name="正文定位队友",
                            prompt="在交付附件中找队友目标，使用返回参数实际阅读并报告，不保存书签。",
                            attachment_ids=[item["id"]],
                        )
                    ]
                )
            elif main_round == 2:
                spawned = tool_result(messages, "find-team-spawn")
                assert spawned["parent_id"] == main_id and spawned["mode"] == "yolo"
                agent_id = spawned["id"]
                turn = ModelTurn(content=assigned)
            else:
                assert main_round == 3 and agent_id is not None
                report = next(
                    message
                    for message in runtime.store.messages(main_id)
                    if message["role"] == "user"
                    and message.get("source") == f"team:{agent_id}"
                    and message.get("principal") == f"agent:{agent_id}"
                )
                assert "已在交付资料中定位并实际阅读目标正文" in report["content"]
                assert "已在交付资料中定位并实际阅读目标正文" in str(messages)
                turn = ModelTurn(content=acknowledgement)
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_round, worker_stage
            if worker_round == 0:
                # Delay only this scripted model response so the real report
                # must wake an idle main after its assignment run completed.
                worker_stage = "await_assignment_completion"
                await worker_release.wait()
            worker_round += 1
            worker_seen.append(list(messages))
            worker_stage = "tool_schema"
            functions = {tool["function"]["name"]: tool["function"] for tool in tools}
            assert {"attachment_find", "document_read"} <= functions.keys()
            assert {"reading_bookmark_set", "reading_bookmark_delete"}.isdisjoint(functions)
            schema = functions["attachment_find"]["parameters"]
            assert set(schema["required"]) == {"id", "reader", "query"}
            assert "document_read" in schema["properties"]["reader"]["enum"]
            if worker_round == 1:
                worker_stage = "delivered_attachment"
                assert item["id"] in str(messages) and context not in str(messages)
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "find-team-locations",
                            "attachment_find",
                            id=item["id"],
                            reader="document_read",
                            query=query,
                            limit=1,
                        )
                    ]
                )
            elif worker_round == 2:
                worker_stage = "find_result"
                result = tool_result(messages, "find-team-locations")
                assert result["id"] == item["id"] and result["format"] == "docx"
                assert result["scope"] == "docx_main_body_text" and len(result["items"]) == 1
                assert context not in str(messages)
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "find-team-read",
                            result["reader"],
                            **result["items"][0]["read_arguments"],
                        )
                    ]
                )
            else:
                worker_stage = "read_result"
                assert worker_round == 3
                read = tool_result(messages, "find-team-read")
                assert read["content"].startswith(query + context)
                assert read["format"] == "docx" and read["source_revision"] == item["sha256"]
                turn = ModelTurn(content="已在交付资料中定位并实际阅读目标正文，未修改书签。")
            worker_stage = "yielded_turn"
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        requested_models.append(config.model)
        if config.model == "worker":
            return Worker([], worker_seen)
        assert config.model == "main"
        return Main([], host.seen)

    runtime.client_factory = factory  # type: ignore[assignment]
    receipt = await runtime.submit(
        "让正文定位队友在交付资料中找队友目标，实际阅读命中后向我报告，不保存书签。",
        mode="yolo",
        attachment_ids=[item["id"]],
    )
    assert await asyncio.wait_for(runtime.wait(receipt["run_id"]), 3) == assigned
    assert runtime.store.get("run", receipt["run_id"])["status"] == "completed"
    assert runtime.main_session()["mode"] == "yolo"
    assert agent_id is not None
    agent = runtime.team.get(agent_id)
    assert agent["mode"] == "yolo"
    worker_release.set()

    def diagnostic() -> dict[str, Any]:
        current = runtime.team.get(agent["id"])
        run_id = current.get("run_id")
        run = runtime.store.get("run", run_id) if isinstance(run_id, str) else None
        report = runtime.store.get("team_report", run_id) if isinstance(run_id, str) else None
        main_run = runtime.store.db.execute(
            "SELECT json_extract(data,'$.status'),substr(json_extract(data,'$.error'),1,200) "
            "FROM objects WHERE kind='run' AND json_extract(data,'$.session_id')=? "
            "ORDER BY rowid DESC LIMIT 1",
            (main_id,),
        ).fetchone()
        return {
            "worker_status": current.get("status"),
            "worker_mode": current.get("mode"),
            "worker_round": worker_round,
            "worker_stage": worker_stage,
            "worker_result": str(current.get("result", ""))[:200],
            "worker_run_status": run.get("status") if run is not None else None,
            "worker_run_error": str(run.get("error", ""))[:200] if run is not None else None,
            "report_status": report.get("status") if report is not None else None,
            "main_run_status": main_run[0] if main_run is not None else None,
            "main_run_error": main_run[1] if main_run is not None else None,
            "main_active": main_id in runtime.tasks,
            "main_pending": bool(runtime.store.pending_inputs(main_id)),
            "main_round": main_round,
            "models": requested_models[:6],
        }

    await runtime.team.wait([agent["id"]], timeout=2)
    finished = runtime.team.get(agent["id"])
    assert finished["status"] == "completed", json.dumps(diagnostic(), ensure_ascii=False)
    assert worker_round == 3, json.dumps(diagnostic(), ensure_ascii=False)
    try:
        await eventually(
            lambda: (
                len(completed(runtime, acknowledgement)) == 1
                and runtime.team.idle
                and not runtime.tasks
            )
        )
    except TimeoutError:
        pytest.fail(
            "team report did not settle: " + json.dumps(diagnostic(), ensure_ascii=False),
            pytrace=False,
        )
    finished = runtime.team.get(agent["id"])
    assert finished["status"] == "completed" and finished["generation"] == 1
    assert finished["session_id"] == agent["id"] and finished["parent_id"] == main_id
    assert worker_round == 3 and main_round == 3
    assert sorted(requested_models) == ["main", "main", "worker"]
    owners = [
        message
        for message in runtime.store.messages(main_id)
        if message["role"] == "user" and message.get("principal") == "owner"
    ]
    assert len(owners) == 1 and owners[0]["attachment_ids"] == [item["id"]]
    initial = next(
        message
        for message in runtime.store.messages(agent["id"])
        if message["role"] == "user" and message.get("source") == f"team:{main_id}"
    )
    assert initial["attachment_ids"] == [item["id"]]
    assert initial["principal"] == f"agent:{main_id}"
    find = stored_tool(host, agent["id"], "find-team-locations")
    read = stored_tool(host, agent["id"], "find-team-read")
    assert "reading_page" not in find
    for page in [find, read]:
        assert page["session_id"] == agent["id"]
        assert page["content_provenance"] == "accepted" and page["preserve_content"]
        replay = next(
            message for message in worker_seen[-1] if message.tool_call_id == page["tool_call_id"]
        )
        assert replay.preserve_content and replay.content == page["content"]
    proof = read["reading_page"]
    assert proof["main_id"] == main_id and proof["attachment_id"] == item["id"]
    assert proof["source_session_id"] == proof["source_agent_id"] == agent["id"]
    assert proof["source_run_id"] == finished["run_id"] and proof["source_generation"] == 1
    assert proof["trigger"]["principal"] == f"agent:{main_id}"
    assert json.loads(find["content"])["revision"] == json.loads(read["content"])["revision"]
    assert (
        runtime.store.db.execute(
            "SELECT count(*) FROM events WHERE type='reading_bookmark.set'"
        ).fetchone()[0]
        == 0
    )
    assert external_requests == [] and host.requests == []
