"""Unknown-session archive lookup through real owner, Team and evidence paths.

Only model decisions and external HTTP are controlled. Search, authority,
ownership, SQLite, compaction and the original body reader remain real.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from test_channel_ownership_policy import assigned, receive
from test_history_find_runtime import preserved_reply
from test_multichannel_ownership_boundaries import finished
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.evidence import EvidenceError, json_size
from muli.history_evidence import HistoryEvidence
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import new_id
from muli.tools import Tool, parameters

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        raise AssertionError(f"历史寻回不得请求外部 HTTP：{request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_http)


async def configured(tmp_path: Path) -> Runtime:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"enabled": False, "auto_extract": False},
            "attention": {"enabled": False},
            "extensions": {"watch_enabled": False},
            "compaction": {"enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
        }
    )
    await runtime.reconfigure()
    return runtime


def execution(runtime: Runtime) -> tuple[str, str]:
    session_id = next(
        identifier for identifier, task in runtime.tasks.items() if task is asyncio.current_task()
    )
    return session_id, runtime.run_ids[session_id]


async def settled(runtime: Runtime) -> None:
    main_id = runtime.main_session()["id"]
    # A terminal child has not necessarily finished flushing its report. Keep
    # the current offline client installed until real publication and all
    # resulting parent/main runs have consumed their durable ingress.
    async with asyncio.timeout(4):
        # Team._done removes completed tasks without publishing a bus revision.
        # Join those tasks so an independent parent's suppressed report cannot
        # leave an event-only idle waiter asleep after the final notification.
        while tasks := tuple(runtime.team._tasks | set(runtime.team._stopping_tasks)):
            await asyncio.wait(tasks)
        await eventually(
            runtime,
            lambda: (
                runtime.team.idle
                and not runtime.tasks
                and runtime.store.pending_inputs(main_id) == 0
                and not runtime.team.has_deliverable_reports()
            ),
        )


async def run_tool(
    runtime: Runtime,
    name: str,
    arguments: dict[str, Any],
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    await settled(runtime)
    previous = runtime.client_factory
    call_id = "history-scope-" + new_id()
    target_session = session_id or runtime.main_session()["id"]
    target_run: str | None = None
    result: dict[str, Any] = {}
    rounds: dict[tuple[str, str], int] = {}

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal target_run
            current = execution(runtime)
            if current[0] == target_session and target_run is None:
                target_run = current[1]
            if current != (target_session, target_run):
                yield StreamEvent("done", turn=ModelTurn(content="队友报告已核对。"))
                return
            rounds[current] = rounds.get(current, 0) + 1
            if rounds[current] == 1:
                turn = ModelTurn(tool_calls=[call(call_id, name, **arguments)])
            else:
                assert rounds[current] == 2
                result.update(tool_result(messages, call_id))
                turn = ModelTurn(content="本次历史工具回执已核对。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: Client([], [])  # type: ignore[assignment]
    try:
        submitted = (
            await runtime.submit("只核对已保存历史，不恢复旧工作。", mode="yolo")
            if session_id is None
            else await runtime.start(session_id, "只核对已保存历史。", mode="yolo")
        )
        assert (
            await asyncio.wait_for(runtime.wait(submitted["run_id"]), 5)
            == "本次历史工具回执已核对。"
        )
        await settled(runtime)
    finally:
        runtime.client_factory = previous
    assert target_run is not None
    assert rounds[(target_session, target_run)] == 2 and result
    return result


async def completed_agent(
    runtime: Runtime,
    name: str,
    parent_id: str,
    *,
    role: Literal["worker", "reviewer"] = "worker",
) -> str:
    previous = runtime.client_factory

    class Completion(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            # A real parent/report run can claim another report after a normal
            # final and continue in the same client, without a tool decision.
            yield StreamEvent("done", turn=ModelTurn(content="合成资料核对已完成。"))

    runtime.client_factory = lambda _: Completion([], [])  # type: ignore[assignment]
    try:
        agent = await runtime.team.spawn(
            name, "只核对合成资料，不外发。", parent_id=parent_id, role=role
        )
        await runtime.team.wait([agent["id"]], timeout=3)
        await finished(runtime, agent["id"], 1)
        await settled(runtime)
        return str(agent["id"])
    finally:
        runtime.client_factory = previous


async def test_owner_finds_completed_delegated_original_after_compaction_and_normal_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = "校验尾部 N7🙂"
    original = {"text": "前" * 8400 + query + "｜只回读原件，不重跑工具"}
    original_calls = 0
    consultant_rounds = 0
    worker_rounds = 0
    denied_consult: dict[str, Any] = {}

    async def capture(arguments: dict[str, Any]) -> dict[str, str]:
        nonlocal original_calls
        assert arguments == {}
        original_calls += 1
        return original

    async with assigned(tmp_path, monkeypatch, max_output_chars=16000) as host:
        first, _app, web, child_id = host
        root_id = first.main_session()["id"]
        instance_id = first.store.instance_id
        first.config.update(
            {
                # Keep the archived receipt within the original fixture budget;
                # shipped persona growth is outside this compaction/reopen journey.
                "persona": {"prompt": "你是本地助手，请完成当前用户的任务。"},
                "memory": {"enabled": False, "auto_extract": False},
                "compaction": {"enabled": False},
            }
        )
        await first.reconfigure()
        first.add_tool_provider(
            lambda _: [Tool("capture_once", "保存合成原回执", parameters({}), capture)]
        )

        class Writer(FakeClient):
            def __init__(self, role: str) -> None:
                super().__init__([], [])
                self.role = role

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal consultant_rounds, worker_rounds
                if self.role == "worker":
                    worker_rounds += 1
                    if worker_rounds == 1:
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "history-consult",
                                    "team_consult",
                                    question="请只核对本次权限边界，不执行旧任务。",
                                    wait_seconds=2,
                                )
                            ]
                        )
                    elif worker_rounds == 2:
                        consultation = tool_result(messages, "history-consult")
                        assert "error" not in consultation
                        turn = ModelTurn(
                            tool_calls=[call("capture-history-source", "capture_once")]
                        )
                    else:
                        assert worker_rounds == 3
                        assert tool_result(messages, "capture-history-source") == original
                        turn = ModelTurn(content="原回执已保存；本次没有外发。")
                else:
                    assert self.role == "main"
                    consultant_rounds += 1
                    if consultant_rounds == 1:
                        # This is a real Team consultation control run in main,
                        # not an owner run manufactured with ContextVar tokens.
                        current = first.run_ids[root_id]
                        triggers = [
                            item
                            for item in first.store.messages(root_id)
                            if item.get("run_id") == current and item["role"] == "user"
                        ]
                        assert len(triggers) == 1
                        assert triggers[0]["principal"] == "agent:" + child_id
                        turn = ModelTurn(
                            tool_calls=[call("non-owner-find", "history_find", query=query)]
                        )
                    else:
                        assert consultant_rounds == 2
                        denied_consult.update(tool_result(messages, "non-owner-find"))
                        assert "error" in denied_consult and "items" not in denied_consult
                        turn = ModelTurn(content="本次仅核对权限边界；没有扩大历史范围。")
                yield StreamEvent("done", turn=turn)

        def writer(config: ModelConfig) -> Writer:
            return Writer(config.model)

        first.client_factory = writer  # type: ignore[assignment]
        incoming = "接管后的主人私聊：请保存合成原回执，正常完成，不外发。"
        await receive(web, incoming, number=51001)
        await finished(first, child_id, 2)
        await eventually(first, lambda: root_id not in first.tasks)
        child = first.team.get(child_id)
        assert child["parent_id"] == root_id and child["status"] == "completed"
        assert original_calls == 1 and worker_rounds == 3 and consultant_rounds == 2
        records = first.store.messages(child_id)
        accepted_input = next(item for item in records if item["content"] == incoming)
        assert accepted_input["source"] == "channel:onebot:123"
        assert accepted_input["principal"] == "owner"
        record = next(
            item for item in records if item.get("tool_call_id") == "capture-history-source"
        )
        assert record["role"] == "tool" and record["content_provenance"] == "accepted"
        assert json.loads(record["content"]) == original
        first.config.update(
            {"compaction": {"enabled": True, "mode": "extractive", "trigger_ratio": 0.5}}
        )
        await first.reconfigure()
        first.compaction.observe(child_id)
        await first.compaction.finish(child_id)
        assert first.compaction.status(child_id)["covered_seq"] >= record["seq"]
        child_state = {key: child[key] for key in ("run_id", "generation", "completion_revision")}
        assert first.live_approvals() == []
        assert _app.state.channel_application.manager.deliveries() == []

    runtime, seen = make_runtime(tmp_path, [])
    responses: list[dict[str, Any]] = []
    rounds = 0
    schema_available = False
    final = "已从旧归档核对原回执，没有重跑。"
    try:
        runtime.config.update(
            {
                "memory": {"enabled": False, "auto_extract": False},
                "compaction": {"enabled": False},
                "limits": {"max_output_chars": 2400},
            }
        )
        await runtime.reconfigure()
        runtime.add_tool_provider(
            lambda _: [Tool("capture_once", "原工具不得重跑", parameters({}), capture)]
        )
        assert runtime.main_session()["id"] == root_id
        assert runtime.store.instance_id == instance_id

        class Reader(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds, schema_available
                rounds += 1
                seen.append(list(messages))
                if rounds == 1:
                    functions = {item["function"]["name"]: item["function"] for item in tools}
                    schema_available = "history_find" in functions
                    if not schema_available:
                        # The frozen baseline must complete this real journey
                        # before failing at the unknown-session feature oracle.
                        yield StreamEvent("done", turn=ModelTurn(content=final))
                        return
                    schema = functions["history_find"]["parameters"]
                    assert set(schema["required"]) == {"query"}
                    assert set(schema["properties"]) == {"query", "cursor", "limit"}
                    assert runtime.tools["history_find"].preserve_output is True
                    turn = ModelTurn(
                        tool_calls=[call("locate-old-history", "history_find", query=query)]
                    )
                elif rounds == 2:
                    found = preserved_reply(messages, "locate-old-history")
                    responses.append(found)
                    assert found["consistency"] == "bounded_live"
                    assert len(found["items"]) == 1 and not found["has_more"]
                    hit = found["items"][0]
                    assert hit["session_id"] == child_id and hit["message_id"] == record["id"]
                    assert hit["seq"] == record["seq"]
                    assert hit["revision"] == record["evidence_revision"]
                    assert hit["content_provenance"] == "accepted" and hit["role"] == "tool"
                    for name in ("source", "principal"):
                        if name in record:
                            assert hit[name] == record[name]
                        else:
                            assert name not in hit
                    start, length = hit["offset_bytes"], hit["length_bytes"]
                    assert record["content"].encode()[start : start + length] == query.encode()
                    assert hit["read_arguments"]["session_id"] == hit["session_id"]
                    assert "content" not in hit
                    # The reader receives only arguments emitted by the actual
                    # locator; old fixture IDs are assertions, never requests.
                    turn = ModelTurn(
                        tool_calls=[
                            call("read-old-history", "conversation_read", **hit["read_arguments"])
                        ]
                    )
                elif rounds == 3:
                    page = preserved_reply(messages, "read-old-history")
                    responses.append(page)
                    assert page["content"].startswith(query)
                    assert page["message_id"] == record["id"]
                    assert page["revision"] == record["evidence_revision"]
                    assert page["content_provenance"] == "accepted"
                    turn = ModelTurn(tool_calls=[call("find-again", "history_find", query=query)])
                else:
                    assert rounds == 4
                    found = preserved_reply(messages, "find-again")
                    responses.append(found)
                    assert not found["has_more"]
                    assert [
                        (item["session_id"], item["message_id"]) for item in found["items"]
                    ] == [(child_id, record["id"])]
                    turn = ModelTurn(content=final)
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda _: Reader([], [])  # type: ignore[assignment]
        app = create_app(runtime.config, runtime.data_dir, runtime)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            before_outbox = list(app.state.channel_application.manager.deliveries())
            before_jobs = runtime.store.db.execute("SELECT count(*) FROM memory_jobs").fetchone()[0]
            response = await web.post(
                f"/api/sessions/{runtime.main_session()['id']}/messages",
                json={"content": f"找回‘{query}’那次原回执，不重新运行。", "mode": "yolo"},
            )
            assert response.status_code == 200
            receipt = response.json()
            assert await asyncio.wait_for(runtime.wait(receipt["run_id"]), 5) == final
            assert schema_available, "未知来源会话时，缺少只需 query 的 history_find 定位入口"
            assert rounds == 4 and len(responses) == 3
            assert all(json_size(value) <= 2400 for value in responses)
            assert original_calls == 1
            unchanged = runtime.store.get("message", record["id"])
            assert unchanged is not None and unchanged["content"] == record["content"]
            assert unchanged["evidence_revision"] == record["evidence_revision"]
            current = runtime.team.get(child_id)
            assert {key: current[key] for key in child_state} == child_state
            assert current["status"] == "completed" and child_id not in runtime.tasks
            assert app.state.channel_application.manager.deliveries() == before_outbox
            assert runtime.live_approvals() == []
            assert (
                runtime.store.db.execute("SELECT count(*) FROM memory_jobs").fetchone()[0]
                == before_jobs
            )
    finally:
        await runtime.close()


async def test_history_scope_rejects_independent_teammate_and_forwarded_handler_calls(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    query = "仅主助手可发现的旧原件🙂"
    try:
        main_id = runtime.main_session()["id"]
        runtime.store.add_message(main_id, "user", query, source="web", principal="owner")
        allowed = await run_tool(runtime, "history_find", {"query": query})
        assert len(allowed["items"]) == 1
        independent = runtime.new_session(mode="yolo")["id"]
        rejected = await run_tool(runtime, "history_find", {"query": query}, session_id=independent)
        assert "error" in rejected and "items" not in rejected
        parent_id = await completed_agent(runtime, "孙代的真实父队友", main_id)
        previous = runtime.client_factory
        rejected_children: dict[str, dict[str, Any]] = {}
        rounds: dict[tuple[str, str], int] = {}

        class Probe(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                current = execution(runtime)
                caller = current[0]
                rounds[current] = rounds.get(current, 0) + 1
                if rounds[current] == 1:
                    turn = ModelTurn(tool_calls=[call("child-find", "history_find", query=query)])
                else:
                    assert rounds[current] == 2
                    rejected_children[caller] = tool_result(messages, "child-find")
                    turn = ModelTurn(content="本次只核对权限拒绝，没有扩域。")
                yield StreamEvent("done", turn=turn)

        def probe(config: ModelConfig) -> FakeClient:
            return (
                Probe([], [])
                if config.model == "worker"
                else FakeClient([ModelTurn(content="资料状态已收到。")], [])
            )

        runtime.client_factory = probe  # type: ignore[assignment]
        try:
            for ancestor in (main_id, parent_id):
                child = await runtime.team.spawn(
                    "真实权限调用方", "只核对权限。", parent_id=ancestor
                )
                await runtime.team.wait([child["id"]], timeout=3)
                await finished(runtime, child["id"], 1)
                denied = rejected_children[child["id"]]
                assert "error" in denied and "items" not in denied
            await settled(runtime)
        finally:
            runtime.client_factory = previous

        async def relay(arguments: dict[str, Any]) -> Any:
            return await runtime.tools["history_find"].handler(arguments)

        runtime.add_tool_provider(
            lambda _: [
                Tool(
                    "relay_history",
                    "转发实际 handler 的权限反例",
                    parameters({"query": {"type": "string"}}, ("query",)),
                    relay,
                )
            ]
        )
        forwarded = await run_tool(runtime, "relay_history", {"query": query})
        assert "error" in forwarded and "items" not in forwarded
        with pytest.raises((EvidenceError, PermissionError)):
            await runtime.tools["history_find"].handler({"query": query})
    finally:
        await runtime.close()


async def test_history_scope_checks_real_team_edges_before_loading_foreign_bodies(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    try:
        main_id = runtime.main_session()["id"]
        worker = await completed_agent(runtime, "直属资料", main_id)
        reviewer = await completed_agent(runtime, "直属审查资料", main_id, role="reviewer")
        grandchild = await completed_agent(runtime, "孙代资料", worker)
        independent = runtime.new_session(mode="yolo")["id"]
        foreign_child = await completed_agent(runtime, "独立会话的队友", independent)
        query = "相同正文仍须保留不同出处🙂"
        allowed_sessions = {main_id, worker, reviewer}
        records = {
            session: runtime.store.add_message(
                session, "user", query, source="web", principal="owner"
            )
            for session in (main_id, worker, reviewer, grandchild, independent, foreign_child)
        }
        forbidden_ids = {
            record["id"] for session, record in records.items() if session not in allowed_sessions
        }
        statements: list[str] = []
        runtime.store.db.set_trace_callback(statements.append)
        try:
            result = await run_tool(runtime, "history_find", {"query": query})
        finally:
            runtime.store.db.set_trace_callback(None)
        assert not result["has_more"]
        assert {item["session_id"] for item in result["items"]} == allowed_sessions
        assert {item["message_id"] for item in result["items"]} == {
            records[session]["id"] for session in allowed_sessions
        }
        assert all(
            item["source"] == "web" and item["principal"] == "owner" for item in result["items"]
        )
        assert result["omitted_total"] == 0
        # Mapping seeks may return foreign IDs. No foreign original is then
        # selected by ID, including through the size/raw-object loading paths.
        assert not [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
            and any("'" + identifier + "'" in statement for identifier in forbidden_ids)
        ]
        evidence = HistoryEvidence(runtime.store)
        for item in result["items"]:
            page = evidence.read_content(**item["read_arguments"], max_output_chars=2400)
            assert page["content"] == query
    finally:
        await runtime.close()


@pytest.mark.parametrize("bad_role", [[], {}], ids=["list", "object"])
async def test_history_scope_bad_team_role_is_an_omission_with_live_progress(
    tmp_path: Path, bad_role: list[object] | dict[str, object]
) -> None:
    runtime = await configured(tmp_path)
    query = "坏来源不能阻断旧合法原文🙂"
    pages: list[dict[str, Any]] = []
    rounds = 0
    try:
        main_id = runtime.main_session()["id"]
        child_id = await completed_agent(runtime, "真实完成后损坏角色的来源", main_id)
        good = runtime.store.add_message(main_id, "user", query, source="web", principal="owner")
        runtime.store.add_message(child_id, "user", query, source="web", principal="owner")
        child_session = runtime.store.get("session", child_id)
        assert child_session is not None and child_session["kind"] == "team"
        runtime.store.put("session", {**child_session, "role": bad_role})
        runtime.config.update({"limits": {"max_search_results": 2}})
        await runtime.reconfigure()

        class Reader(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds
                rounds += 1
                assert rounds <= 12, "少量坏来源映射仍须取得有限进展"
                arguments: dict[str, Any] = {"query": query}
                if rounds > 1:
                    page = tool_result(messages, "bad-role-page-" + str(rounds - 1))
                    pages.append(page)
                    if "error" in page or not page["has_more"]:
                        yield StreamEvent("done", turn=ModelTurn(content="坏来源回执已核对。"))
                        return
                    arguments["cursor"] = page["next_cursor"]
                yield StreamEvent(
                    "done",
                    turn=ModelTurn(
                        tool_calls=[
                            call("bad-role-page-" + str(rounds), "history_find", **arguments)
                        ]
                    ),
                )

        runtime.client_factory = lambda _: Reader([], [])  # type: ignore[assignment]
        submitted = await runtime.submit("只按有限窗口找旧合法原件。", mode="yolo")
        assert await asyncio.wait_for(runtime.wait(submitted["run_id"]), 5) == "坏来源回执已核对。"
        assert pages and all("error" not in page for page in pages), pages
        hits = [item for page in pages for item in page["items"]]
        assert [(item["session_id"], item["message_id"]) for item in hits] == [
            (main_id, good["id"])
        ]
        assert sum(page["omitted"]["invalid_mapping"] for page in pages) >= 1
        assert pages[-1]["omitted_total"] >= 1 and not pages[-1]["has_more"]
        cursors = [page["next_cursor"] for page in pages if page["has_more"]]
        assert cursors and len(set(cursors)) == len(cursors)
        assert all(page["scanned"] <= 2 for page in pages)
    finally:
        await runtime.close()


async def test_history_scope_empty_candidate_pages_resume_and_cursors_expire_in_new_runs(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    query = "🙂重叠🙂"
    pages: list[dict[str, Any]] = []
    rounds = 0
    try:
        runtime.config.update({"limits": {"max_search_results": 2}})
        await runtime.reconfigure()
        main_id = runtime.main_session()["id"]
        target = runtime.store.add_message(
            main_id, "tool", "🙂重叠🙂重叠🙂", content_provenance="accepted"
        )
        outside = runtime.new_session(mode="yolo")["id"]
        for _ in range(5):
            runtime.store.add_message(outside, "user", query, source="web", principal="owner")

        class Paging(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds
                rounds += 1
                assert rounds <= 12, "有限合成映射必须在有限页内结束"
                arguments: dict[str, Any] = {"query": query, "limit": 1}
                if rounds > 1:
                    page = preserved_reply(messages, "scope-page-" + str(rounds - 1))
                    pages.append(page)
                    assert "error" not in page and page["consistency"] == "bounded_live"
                    if not page["has_more"]:
                        yield StreamEvent("done", turn=ModelTurn(content="有限历史窗口已核对。"))
                        return
                    assert page["next_cursor"]
                    arguments["cursor"] = page["next_cursor"]
                yield StreamEvent(
                    "done",
                    turn=ModelTurn(
                        tool_calls=[call("scope-page-" + str(rounds), "history_find", **arguments)]
                    ),
                )

        runtime.client_factory = lambda _: Paging([], [])  # type: ignore[assignment]
        submitted = await runtime.submit("只按有限窗口逐页找旧原文。", mode="yolo")
        assert (
            await asyncio.wait_for(runtime.wait(submitted["run_id"]), 5) == "有限历史窗口已核对。"
        )
        await eventually(runtime, lambda: main_id not in runtime.tasks)
        assert pages[0]["items"] == [] and pages[0]["scanned"] == 2
        assert pages[0]["stopped"] == "candidate_budget" and pages[0]["has_more"]
        assert all(page["scanned"] <= 2 and page["omitted_total"] == 0 for page in pages)
        assert all(json_size(page) <= 6000 for page in pages)
        hits = [item for page in pages for item in page["items"]]
        assert [(item["message_id"], item["offset_bytes"]) for item in hits] == [
            (target["id"], 0),
            (target["id"], len("🙂重叠".encode())),
        ]
        assert len({page["through_seq"] for page in pages}) == 1
        assert not pages[-1]["has_more"] and pages[-1]["next_cursor"] is None
        token = pages[0]["next_cursor"]
        expired = await run_tool(runtime, "history_find", {"query": query, "cursor": token})
        assert expired.get("code") == "history_find_cursor_expired" and "items" not in expired
    finally:
        await runtime.close()

    reopened = await configured(tmp_path)
    try:
        expired = await run_tool(reopened, "history_find", {"query": query, "cursor": token})
        assert expired.get("code") == "history_find_cursor_expired" and "items" not in expired
    finally:
        await reopened.close()


async def test_history_scope_new_owner_ingress_keeps_the_first_run_exclusion_boundary(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    query = "同一运行的最早边界🙂"
    rounds = 0
    target_run: str | None = None
    executions: list[tuple[str, str]] = []
    followup: dict[str, Any] = {}
    responses: list[dict[str, Any]] = []
    observations: list[dict[str, str]] = []
    try:
        main_id = runtime.main_session()["id"]
        original = runtime.store.add_message(
            main_id, "user", query + "｜旧原件", source="web", principal="owner"
        )

        async def observe_owner_request(arguments: dict[str, Any]) -> dict[str, str]:
            assert arguments == {} and runtime.tool_session_id == main_id
            request = runtime.request_context(main_id)
            assert request is not None
            input_id = request.reply_origin.input_id
            assert isinstance(input_id, str) and input_id == followup["message"]["id"]
            assert execution(runtime) == (main_id, target_run)
            observed = {"input_id": input_id, "run_id": runtime.run_ids[main_id]}
            observations.append(observed)
            return observed

        runtime.add_tool_provider(
            lambda _: [
                Tool(
                    "scope_owner_request",
                    "只读核对本轮真实主人输入身份",
                    parameters({}),
                    observe_owner_request,
                )
            ]
        )

        class Reader(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds, target_run
                current = execution(runtime)
                if current[0] == main_id and target_run is None:
                    target_run = current[1]
                if current != (main_id, target_run):
                    yield StreamEvent("done", turn=ModelTurn(content="资料状态已核对。"))
                    return
                rounds += 1
                executions.append(current)
                assert rounds <= 5
                if rounds == 1:
                    turn = ModelTurn(
                        tool_calls=[call("boundary-first-find", "history_find", query=query)]
                    )
                elif rounds == 2:
                    found = preserved_reply(messages, "boundary-first-find")
                    responses.append(found)
                    assert [item["message_id"] for item in found["items"]] == [original["id"]]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "boundary-original-read",
                                "conversation_read",
                                **found["items"][0]["read_arguments"],
                            )
                        ]
                    )
                elif rounds == 3:
                    page = preserved_reply(messages, "boundary-original-read")
                    responses.append(page)
                    assert page["content"].startswith(query)
                    followup.update(await runtime.submit("新增主人补充：继续找‘" + query + "’。"))
                    assert followup["queued"] and followup["run_id"] == executions[0][1]
                    assert runtime.store.pending_inputs(main_id) == 1
                    # A normal model final lets the actual driver claim the new
                    # owner ingress at its replay-safe boundary in the same run.
                    turn = ModelTurn(content="已收到补充，下一安全轮继续只读核查。")
                elif rounds == 4:
                    assert runtime.store.pending_inputs(main_id) == 0
                    assert any(
                        message.source_message_id == followup["message"]["id"]
                        for message in messages
                    )
                    turn = ModelTurn(
                        tool_calls=[
                            call("boundary-owner-request", "scope_owner_request"),
                            call("boundary-second-find", "history_find", query=query),
                        ]
                    )
                else:
                    assert tool_result(messages, "boundary-owner-request") == {
                        "input_id": followup["message"]["id"],
                        "run_id": target_run,
                    }
                    found = preserved_reply(messages, "boundary-second-find")
                    responses.append(found)
                    assert not found["has_more"]
                    assert [
                        (item["session_id"], item["message_id"]) for item in found["items"]
                    ] == [(main_id, original["id"])]
                    turn = ModelTurn(content="新输入与工具回包仍被排除，只核对旧原件。")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda _: Reader([], [])  # type: ignore[assignment]
        submitted = await runtime.submit("请找回‘" + query + "’那次旧原件。", mode="yolo")
        assert (
            await asyncio.wait_for(runtime.wait(submitted["run_id"]), 5)
            == "新输入与工具回包仍被排除，只核对旧原件。"
        )
        await settled(runtime)
        assert rounds == 5 and len(responses) == 3
        assert set(executions) == {(main_id, submitted["run_id"])}
        assert observations == [
            {"input_id": followup["message"]["id"], "run_id": submitted["run_id"]}
        ]
        history = runtime.store.messages(main_id)
        first_input = next(item for item in history if item["id"] == submitted["message"]["id"])
        next_input = next(item for item in history if item["id"] == followup["message"]["id"])
        reading = next(
            item for item in history if item.get("tool_call_id") == "boundary-original-read"
        )
        assert first_input["run_id"] == next_input["run_id"] == submitted["run_id"]
        assert original["seq"] < first_input["seq"] < reading["seq"] < next_input["seq"]
        assert query in first_input["content"] and query in reading["content"]
    finally:
        await runtime.close()


@pytest.mark.parametrize("change", ["parent", "delete"])
async def test_history_scope_current_cursor_rechecks_the_direct_agent_link(
    tmp_path: Path, change: Literal["parent", "delete"]
) -> None:
    runtime = await configured(tmp_path)
    query = "中文🙂中文"
    rounds = 0
    results: list[dict[str, Any]] = []
    executions: list[tuple[str, str]] = []
    try:
        main_id = runtime.main_session()["id"]
        child_id = await completed_agent(runtime, "续页前须重核关联的直属来源", main_id)
        unrelated = runtime.new_session(mode="yolo")["id"]
        original = runtime.store.add_message(
            child_id, "tool", "中文🙂中文🙂中文", content_provenance="accepted"
        )

        class Reader(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds
                rounds += 1
                executions.append(execution(runtime))
                if rounds == 1:
                    turn = ModelTurn(
                        tool_calls=[call("link-first", "history_find", query=query, limit=1)]
                    )
                elif rounds == 2:
                    first = preserved_reply(messages, "link-first")
                    results.append(first)
                    assert first["has_more"] and first["next_cursor"]
                    assert len(first["items"]) == 1
                    hit = first["items"][0]
                    assert hit["message_id"] == original["id"] and hit["offset_bytes"] == 0
                    assert hit["session_id"] == child_id
                    agent = runtime.store.get("agent", child_id)
                    assert agent is not None and agent["parent_id"] == main_id
                    if change == "parent":
                        runtime.store.put("agent", {**agent, "parent_id": unrelated})
                    else:
                        runtime.store.delete("agent", child_id)
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "link-next",
                                "history_find",
                                query=query,
                                cursor=first["next_cursor"],
                                limit=1,
                            )
                        ]
                    )
                else:
                    assert rounds == 3
                    changed = preserved_reply(messages, "link-next")
                    results.append(changed)
                    assert changed.get("code") == "history_find_changed"
                    assert "items" not in changed
                    turn = ModelTurn(content="关联已变化，本轮不拼接旧来源后续片段。")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda _: Reader([], [])  # type: ignore[assignment]
        submitted = await runtime.submit("只逐页核对旧直属来源。", mode="yolo")
        assert (
            await asyncio.wait_for(runtime.wait(submitted["run_id"]), 5)
            == "关联已变化，本轮不拼接旧来源后续片段。"
        )
        await settled(runtime)
        assert rounds == 3 and len(results) == 2
        assert set(executions) == {(main_id, submitted["run_id"])}
        unchanged = runtime.store.get("message", original["id"])
        assert unchanged is not None and unchanged["content"] == original["content"]
        assert unchanged["evidence_revision"] == original["evidence_revision"]
    finally:
        await runtime.close()


async def test_history_scope_metadata_and_original_share_one_raw_budget(tmp_path: Path) -> None:
    runtime = await configured(tmp_path)
    query = "范围身份与原件共用预算🙂"
    try:
        main_id = runtime.main_session()["id"]
        child_id = await completed_agent(runtime, "共享预算的真实直属来源", main_id)
        raw_budget = runtime.config.settings.limits.max_context_chars * 4
        metadata_bytes = 0
        for kind in ("session", "agent"):
            item = runtime.store.get(kind, child_id)
            assert item is not None
            runtime.store.put(kind, {**item, "scope_padding": "x" * (raw_budget // 3)})
            size = runtime.store.db.execute(
                "SELECT length(CAST(data AS BLOB)) FROM objects WHERE kind=? AND id=?",
                (kind, child_id),
            ).fetchone()[0]
            assert size < raw_budget
            metadata_bytes += size
        original = runtime.store.add_message(child_id, "tool", query + "字" * (raw_budget // 6))
        original_bytes = runtime.store.db.execute(
            "SELECT length(CAST(data AS BLOB)) FROM objects WHERE kind='message' AND id=?",
            (original["id"],),
        ).fetchone()[0]
        assert metadata_bytes < raw_budget and original_bytes < raw_budget
        assert metadata_bytes + original_bytes > raw_budget
        result = await run_tool(runtime, "history_find", {"query": query})
        assert result["items"] == [] and result["stopped"] == "raw_budget"
        assert result["has_more"] and result["next_cursor"]
        assert metadata_bytes <= result["json_bytes"] <= raw_budget
        assert result["omitted_total"] == 0
    finally:
        await runtime.close()
