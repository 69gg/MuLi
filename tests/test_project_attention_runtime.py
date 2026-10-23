"""Project attention host metadata and real Runtime execution boundaries."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, NoReturn, cast

import httpx
import pytest
from test_attention import attention_inputs, eventually, offline_runtime
from test_channels import onebot_body, onebot_event, profile, terminal
from test_report_origin import origin
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, tool_result

from muli.config import ConfigFile
from muli.format_admission import FormatAdmissionError
from muli.proactive import FrozenRouting
from muli.project_review import project_review_key, validate_project_review
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.reply_origin import ReplyOrigin
from muli.runtime import ClientFactory, PreparedRequest, Runtime
from muli.server import create_app
from muli.store import Store, new_id


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("项目复核只允许显式的离线 HTTP MockTransport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)


def host_review() -> dict[str, Any]:
    """An already verified host fact for the Runtime admission unit boundary."""
    return {
        "version": 1,
        "project_id": "已有的非UUID项目标识",
        "binding_revision": new_id(),
        "sources": [
            {
                "record_id": new_id(),
                "event_id": 1,
                "scope": new_id(),
                "revision": new_id(),
                "watcher_generation": 1,
            }
        ],
        "task_snapshot": {
            "revision": 1,
            "status": "open",
            "epoch": 0,
            "approved_epoch": 0,
        },
    }


async def test_project_review_host_metadata_is_atomic_and_archived(tmp_path: Path) -> None:
    runtime = offline_runtime(tmp_path)
    review = host_review()
    guards: list[bool] = []

    def guard() -> None:
        guards.append(runtime.configuration_lock.locked())

    try:
        receipt = await runtime.submit_attention(
            "项目新资料，仅本地复核；未读取正文。",
            "project-admission",
            objects=[("attention_receipt", {"id": "unit-receipt", "project_review": review})],
            admission_guard=guard,
            routing=FrozenRouting.bound(ReplyOrigin(kind="web")),
            project_review=review,
        )
        assert receipt is not None and guards == [True]
        assert receipt["project_review"] == review
        assert receipt["principal"] == "system:attention"
        assert receipt["mode"] == "ask" and receipt["route"] is None
        assert runtime.store.get("attention_receipt", "unit-receipt") == {
            "id": "unit-receipt",
            "project_review": review,
        }
        pending = runtime.conversation.pending(runtime.main_session()["id"])
        assert len(pending) == 1 and pending[0]["project_review"] == review
        claimed = runtime.store.claim_inputs(runtime.main_session()["id"], "unit-run", limit=8)
        assert len(claimed) == 1 and claimed[0]["project_review"] == review
        assert claimed[0]["reply_origin"] == ReplyOrigin(kind="web").dump()
        assert not runtime.tasks
    finally:
        await runtime.close()


async def test_project_review_rejects_channel_destination_before_commit(tmp_path: Path) -> None:
    runtime = offline_runtime(tmp_path)
    try:
        with pytest.raises(ValueError, match="项目复核"):
            await runtime.submit_attention(
                "不能把项目资料选到渠道",
                "project-channel-rejected",
                objects=[("attention_receipt", {"id": "must-not-exist"})],
                admission_guard=lambda: None,
                routing=FrozenRouting.bound(origin()),
                project_review=host_review(),
            )
        assert runtime.conversation.pending(runtime.main_session()["id"]) == []
        assert runtime.store.list("attention_receipt") == []
        assert not runtime.tasks
    finally:
        await runtime.close()


def test_project_review_marker_is_copied_and_reuses_attention_bounds() -> None:
    review = host_review()
    other = {**review["sources"][0], "record_id": new_id(), "event_id": 2, "watcher_generation": 2}
    review["sources"].append(other)
    result = validate_project_review(review, max_sources=2, max_chars=4000)
    assert result == review and (result is review) is False
    assert (result["sources"][0] is review["sources"][0]) is False
    assert (result["task_snapshot"] is review["task_snapshot"]) is False
    with pytest.raises(ValueError, match="项目复核"):
        validate_project_review(review, max_sources=1)
    with pytest.raises(ValueError, match="项目复核"):
        validate_project_review(review, max_chars=len(json.dumps(review, ensure_ascii=False)) - 1)
    review["task_snapshot"]["revision"] = 2
    assert result["task_snapshot"]["revision"] == 1


@pytest.mark.parametrize(
    "mutation",
    ["future", "empty-project", "boolean-epoch", "approved-ahead", "duplicate", "mixed-source"],
)
def test_project_review_marker_rejects_ambiguous_host_facts(mutation: str) -> None:
    review = deepcopy(host_review())
    if mutation == "future":
        review["version"] = 2
    elif mutation == "empty-project":
        review["project_id"] = " "
    elif mutation == "boolean-epoch":
        review["task_snapshot"]["epoch"] = True
    elif mutation == "approved-ahead":
        review["task_snapshot"]["approved_epoch"] = 1
    elif mutation == "duplicate":
        review["sources"].append(dict(review["sources"][0]))
    else:
        review["sources"].append(
            {**review["sources"][0], "event_id": 2, "record_id": new_id(), "scope": new_id()}
        )
    with pytest.raises(ValueError, match="项目复核"):
        validate_project_review(review)


def test_project_group_key_is_canonical_and_rejects_unknown_marker() -> None:
    review = host_review()
    reordered = dict(reversed(list(review.items())))
    reordered["sources"] = [dict(reversed(list(review["sources"][0].items())))]
    reordered["task_snapshot"] = dict(reversed(list(review["task_snapshot"].items())))
    key = project_review_key(review)
    assert project_review_key(reordered) == key
    assert json.loads(key) == review
    reordered["version"] = 2
    with pytest.raises(ValueError, match="项目复核"):
        project_review_key(reordered)


@pytest.mark.parametrize(
    "mutation",
    ["project", "binding", "source", "watcher-generation", "task-revision", "task-epoch"],
)
def test_project_group_key_separates_distinct_host_facts(mutation: str) -> None:
    review = host_review()
    other = deepcopy(review)
    if mutation == "project":
        other["project_id"] = "另一个已绑定任务"
    elif mutation == "binding":
        other["binding_revision"] = new_id()
    elif mutation == "source":
        other["sources"][0]["scope"] = new_id()
    elif mutation == "watcher-generation":
        other["sources"][0]["watcher_generation"] = 2
    elif mutation == "task-revision":
        other["task_snapshot"]["revision"] = 2
    else:
        other["task_snapshot"]["epoch"] = 1
    assert project_review_key(review) != project_review_key(other)


@pytest.mark.parametrize("other_kind", ["generic", "project", "snapshot"])
async def test_runtime_rejects_ambiguous_project_batch_before_format_admission(
    tmp_path: Path, other_kind: str
) -> None:
    """Defensive validation only, without admitting a project or mocking its gate."""
    runtime = offline_runtime(tmp_path)
    session_id, run_id = runtime.main_session()["id"], new_id()
    first: dict[str, Any] = {
        "id": new_id(),
        "session_id": session_id,
        "run_id": run_id,
        "role": "user",
        "source": "system:attention",
        "principal": "system:attention",
        "route": None,
        "attachment_ids": [],
        "reply_origin": ReplyOrigin(kind="web").dump(),
        "attention_routing": FrozenRouting.bound(ReplyOrigin(kind="web")).dump(),
        "project_review": host_review(),
    }
    other = deepcopy(first)
    other["id"] = new_id()
    if other_kind == "generic":
        del other["project_review"]
    elif other_kind == "project":
        other["project_review"]["project_id"] = "不同任务不能覆盖前一个证明"
    else:
        other["project_review"]["task_snapshot"]["revision"] = 2
    try:
        with pytest.raises(ValueError, match="同一项目复核批"):
            runtime._project_review(session_id, run_id, [first, other])
        assert runtime.conversation.pending(session_id) == [] and not runtime.tasks
    finally:
        await runtime.close()


async def test_project_review_unproven_format_has_no_partial_receipt(tmp_path: Path) -> None:
    # Existing unmarked data must stay unproven even when the binary later creates format 2.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    legacy = Store(data_dir / "muli.sqlite3")
    legacy.close()
    runtime = offline_runtime(tmp_path)
    try:
        assert runtime.store.format_state is None
        with pytest.raises(FormatAdmissionError, match="^project_review_format_required$"):
            await runtime.submit_attention(
                "已有库不能在接纳项目资料时隐式升级",
                "unproven-project",
                objects=[("attention_receipt", {"id": "must-not-commit"})],
                admission_guard=lambda: None,
                routing=FrozenRouting.bound(ReplyOrigin(kind="web")),
                project_review=host_review(),
            )
        assert runtime.conversation.pending(runtime.main_session()["id"]) == []
        assert runtime.store.list("attention_receipt") == []
        assert runtime.store.format_state is None and not runtime.tasks
    finally:
        await runtime.close()


async def test_owner_entry_cannot_forge_project_host_metadata(tmp_path: Path) -> None:
    runtime = offline_runtime(tmp_path)
    try:
        with pytest.raises(ValueError, match="项目复核"):
            runtime.conversation.enqueue(
                runtime.main_session()["id"],
                "不是宿主项目事实",
                source="web",
                principal="owner",
                mode="ask",
                max_pending=8,
                project_review=host_review(),
                attention_routing=FrozenRouting.bound(ReplyOrigin(kind="web")),
            )
        assert runtime.conversation.pending(runtime.main_session()["id"]) == []
        assert not runtime.tasks
    finally:
        await runtime.close()


async def test_regular_run_without_claim_ignores_historical_project_marker(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="普通主人新轮已完成")])
    main_id = runtime.main_session()["id"]
    old_run = new_id()
    # A raw historical recovery fixture, not a successful project admission or
    # format-2 installation. Generic Store writes are not provenance validators.
    historical = runtime.store.add_message(
        main_id,
        "user",
        "旧归档资料不能建立本轮项目段",
        source="system:attention",
        principal="system:attention",
        route=None,
        attachment_ids=[],
        run_id=old_run,
        reply_origin=ReplyOrigin(kind="web").dump(),
        attention_routing=FrozenRouting.bound(ReplyOrigin(kind="web")).dump(),
        project_review=host_review(),
    )
    runtime.store.complete_message(main_id, "旧本地记录", delivery_routes=[])
    requests: list[PreparedRequest] = []

    async def capture(session_id: str, messages: list[Message]) -> list[Message]:
        request = runtime.request_context(session_id)
        assert request is not None
        requests.append(request)
        return messages

    runtime.context_hooks.append(capture)
    try:
        assert runtime.conversation.pending(main_id) == []
        started = await runtime.start(main_id, "新输入按普通运行处理")
        assert started["run_id"] != old_run
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 4) == "普通主人新轮已完成"
        assert len(requests) == len(seen) == 1 and requests[0].project_review is None
        assert runtime.store.get("message", historical["id"]) == historical
        assert runtime.conversation.pending(main_id) == []
    finally:
        await runtime.close()


async def test_project_pending_reopens_from_its_durable_host_marker(tmp_path: Path) -> None:
    """Require real format admission before exercising a normal close and reopen."""
    runtime = offline_runtime(tmp_path)
    review = host_review()
    config_path, data_dir = runtime.config.path, runtime.data_dir
    main_id = runtime.main_session()["id"]
    instance = runtime.store.instance_id
    try:
        receipt = await runtime.submit_attention(
            "已接纳的项目资料，正文仍未读取。",
            "project-pending-reopen",
            objects=[],
            admission_guard=lambda: None,
            routing=FrozenRouting.bound(ReplyOrigin(kind="web")),
            project_review=review,
        )
        assert receipt is not None and receipt["state"] == "pending"
        row = runtime.store.db.execute(
            "SELECT payload FROM ingress WHERE id=?", (receipt["id"],)
        ).fetchone()
        assert row is not None
        original_payload = row[0]
        assert not runtime.tasks
    finally:
        await runtime.close()

    config = ConfigFile(config_path)
    config.update(
        {
            "models": {"main": {"model": "main", "base_url": "https://unused.invalid/v1"}},
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
        }
    )
    seen: list[list[Message]] = []
    turns = [ModelTurn(content="项目正文尚未核查"), ModelTurn(content="处理新主人输入")]
    requests: list[PreparedRequest] = []

    def factory(model: ModelConfig) -> FakeClient:
        assert model.model == "main"
        return FakeClient(turns, seen)

    reopened = Runtime(config, data_dir, client_factory=cast(ClientFactory, factory))

    async def capture(session_id: str, messages: list[Message]) -> list[Message]:
        request = reopened.request_context(session_id)
        assert request is not None
        requests.append(request)
        return messages

    reopened.context_hooks.append(capture)
    try:
        assert reopened.store.instance_id == instance
        assert reopened.main_session()["id"] == main_id
        pending = reopened.conversation.pending(main_id)
        assert len(pending) == 1 and pending[0]["id"] == receipt["id"]
        assert pending[0]["project_review"] == review
        assert not reopened.tasks and seen == []
        await reopened.wake_pending_inputs()
        project_run = reopened.run_ids[main_id]
        assert await asyncio.wait_for(reopened.wait(project_run), 4) == "项目正文尚未核查"
        assert len(requests) == 1 and requests[0].project_review == review
        assert requests[0].reply_origin == ReplyOrigin(kind="web")
        assert requests[0].attention_routing == FrozenRouting.bound(ReplyOrigin(kind="web"))
        row = reopened.store.db.execute(
            "SELECT state,payload,run_id FROM ingress WHERE id=?", (receipt["id"],)
        ).fetchone()
        assert row is not None and row["state"] == "delivered"
        assert row["payload"] == original_payload and row["run_id"] == project_run
        archived = reopened.store.get("message", receipt["id"])
        assert archived is not None and archived["project_review"] == review
        assert archived["run_id"] == project_run
        project_output = reopened.store.messages(main_id)[-1]
        assert project_output["delivery_routes"] == []
        assert project_output["reply_origin"]["kind"] == "web"

        owner = await reopened.submit(
            "主人新轮不能从旧项目 marker 恢复项目段", event_id="new-owner"
        )
        assert owner["run_id"] != project_run
        assert await asyncio.wait_for(reopened.wait(owner["run_id"]), 4) == "处理新主人输入"
        assert len(requests) == 2 and requests[1].project_review is None
        assert reopened.conversation.pending(main_id) == [] and not turns
    finally:
        await reopened.close()


@pytest.mark.skipif(sys.platform != "linux", reason="真实 Linux 文件来源")
@pytest.mark.parametrize(
    ("boundary", "generic_boundary"),
    [
        pytest.param("tool-batch", None, id="tool-batch"),
        pytest.param("no-tools", None, id="no-tools"),
        pytest.param("tool-batch", "tool-batch", id="generic-tool-batch"),
        pytest.param("no-tools", "no-tools", id="generic-no-tools"),
    ],
)
async def test_project_owner_input_waits_for_an_independent_run(
    tmp_path: Path, boundary: str, generic_boundary: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real source, permission, file and signed-phone composition; no answer scoring.

    With the current format-1 binary this intentionally stops at the real
    activation gate. Only a genuine format-2 installation can enter the barriers.
    """
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"coalesce_seconds": 0},
            "channels": {
                "onebot": asdict(profile(account_id="123")),
                "proactive": {
                    "external_enabled": True,
                    "default_destination": {
                        "kind": "channel",
                        "route": {
                            "name": "onebot",
                            "account_id": "123",
                            "kind": "private",
                            "target": "7",
                            "sender_id": "7",
                        },
                    },
                },
            },
        }
    )
    await runtime.reconfigure()
    directory = tmp_path / "project-inbox"
    directory.mkdir()
    document = directory / "incoming.txt"
    body = "PROJECT-REAL-FS-BODY-仅在已批准读取后出现"
    owner_text = "手机新要求：先处理这条原始输入"
    owner_reply = "已按手机原始要求处理"
    generic_text = "先完成一条普通本地事项，不将它当作项目资料"
    main_model = runtime.config.settings.models.main.model
    submit_attention = runtime.submit_attention
    phase = "setup"
    setup_round = project_round = 0
    task_id = ""
    project_run = ""
    generic_receipts: list[dict[str, Any]] = []
    generic_runs: list[str] = []
    project_schemas: list[set[str]] = []
    owner_runs: list[str] = []
    prepared_requests: list[tuple[str, PreparedRequest]] = []
    held = asyncio.Event()
    release = asyncio.Event()
    generic_held = asyncio.Event()
    generic_release = asyncio.Event()
    real_reads: list[dict[str, Any]] = []
    sent: list[str] = []

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal setup_round, project_round, task_id, project_run, phase
            if phase == "setup":
                setup_round += 1
                if setup_round == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "save-project",
                                "commitment_save",
                                title="资料复核项目",
                                brief={
                                    "goal": "核对真实资料，不自动发布",
                                    "constraints": [],
                                    "evidence_refs": [],
                                },
                            )
                        ]
                    )
                else:
                    assert setup_round == 2
                    task_id = tool_result(messages, "save-project")["id"]
                    turn = ModelTurn(content="任务已保存")
            elif phase == "generic":
                assert generic_boundary is not None and not generic_runs
                request_run, request = prepared_requests[-1]
                assert request_run == runtime.run_ids[runtime.main_session()["id"]]
                assert request.project_review is None
                assert messages[-1].role == "user" and messages[-1].content == generic_text
                generic_runs.append(runtime.run_ids[runtime.main_session()["id"]])
                generic_held.set()
                await generic_release.wait()
                if generic_boundary == "tool-batch":
                    assert "file_inbox_list" in {item["function"]["name"] for item in tools}
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "ordinary-not-started",
                                "file_inbox_list",
                                limit=1,
                            )
                        ]
                    )
                else:
                    assert generic_boundary == "no-tools"
                    turn = ModelTurn(content="普通本地事项已结束")
                phase = "project"
            elif phase == "project":
                project_round += 1
                project_run = runtime.run_ids[runtime.main_session()["id"]]
                request_run, request = prepared_requests[-1]
                assert request_run == project_run and request.project_review is not None
                project_schemas.append({item["function"]["name"] for item in tools})
                assert owner_text not in str(messages)
                if project_round == 1:
                    notice = next(
                        item.content for item in reversed(messages) if item.role == "user"
                    )
                    facts = json.loads(notice.rsplit("\n", 1)[1])
                    assert len(facts) == 1 and facts[0]["project"]["project_id"] == task_id
                    assert request.project_review["project_id"] == facts[0]["project"]["project_id"]
                    assert (
                        request.project_review["sources"][0]["record_id"] == facts[0]["record_id"]
                    )
                    locator = facts[0]["project"]["read"]
                    assert locator == {"identifier": task_id, "revision": 1}
                    if boundary == "no-tools":
                        held.set()
                        await release.wait()
                        turn = ModelTurn(content="项目正文尚未读取")
                    else:
                        assert body not in str(messages)
                        turn = ModelTurn(
                            tool_calls=[
                                call("current-task", "commitment_read", **locator),
                                call("current-file", "file_inbox_get", id=facts[0]["record_id"]),
                            ]
                        )
                else:
                    assert boundary == "tool-batch" and project_round == 2
                    assert "核对真实资料" in tool_result(messages, "current-task")["text"]
                    current = tool_result(messages, "current-file")
                    assert current["current_file_verified"] is False
                    assert body not in str(messages)
                    turn = ModelTurn(
                        tool_calls=[
                            call("read-real-file", "fs_read", path=current["path"]),
                            call("not-started", "fs_info", path=current["path"]),
                        ]
                    )
            else:
                assert phase == "owner"
                assert messages[-1].role == "user" and messages[-1].content == owner_text
                owner_runs.append(runtime.run_ids[runtime.main_session()["id"]])
                turn = ModelTurn(content=owner_reply)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> Main:
        assert config.model == "main"
        return Main([], [])

    async def capture(session_id: str, messages: list[Message]) -> list[Message]:
        request = runtime.request_context(session_id)
        assert request is not None
        prepared_requests.append((runtime.run_ids[session_id], request))
        return messages

    async def send(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/send_private_msg"
        payload = json.loads(request.content)
        assert payload["user_id"] == "7"
        text = "".join(segment["data"]["text"] for segment in payload["message"])
        sent.append(text)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "sent"}}
        )

    async def submit_with_preceding_generic(
        content: str,
        event_id: str,
        *,
        objects: list[tuple[str, dict[str, Any]]],
        admission_guard: Callable[[], None] | None = None,
        routing: FrozenRouting | None = None,
        project_review: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        # Interleave two real host admissions after Attention's backpressure
        # check. The original project guard, source proof and format gate run.
        assert project_review is not None and admission_guard is not None
        assert not generic_receipts and not runtime.config.settings.models.main.configured
        generic = await submit_attention(
            generic_text,
            "ordinary-before-project",
            objects=[],
            routing=FrozenRouting.bound(ReplyOrigin(kind="web")),
        )
        assert generic is not None and "project_review" not in generic
        generic_receipts.append(generic)
        return await submit_attention(
            content,
            event_id,
            objects=objects,
            admission_guard=admission_guard,
            routing=routing,
            project_review=project_review,
        )

    runtime.client_factory = cast(ClientFactory, factory)
    app = create_app(runtime.config, runtime=runtime)
    runtime.context_hooks.append(capture)
    manager = app.state.channel_application.manager
    try:
        async with AsyncExitStack() as stack:
            channel_client = await stack.enter_async_context(
                httpx.AsyncClient(transport=httpx.MockTransport(send))
            )
            for adapter in manager.adapters.values():
                adapter.http.client = channel_client
            await stack.enter_async_context(app.router.lifespan_context(app))
            web = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
                )
            )
            main_id = runtime.main_session()["id"]
            assigned = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": "建立复核项目", "mode": "yolo"},
            )
            assert assigned.status_code == 200
            await asyncio.wait_for(runtime.wait(assigned.json()["run_id"]), 4)
            assert task_id and not runtime.tasks and not sent
            configured = await web.put(
                "/api/file-inbox/settings",
                json={
                    "enabled": True,
                    "path": str(directory),
                    "project_id": task_id,
                    "coalesce_seconds": 0,
                },
            )
            assert configured.status_code == 200 and app.state.file_inbox.status()["running"]

            if generic_boundary is not None:
                paused = await web.patch("/api/settings", json={"models": {"main": {"model": ""}}})
                assert (
                    paused.status_code == 200 and not runtime.config.settings.models.main.configured
                )
                monkeypatch.setattr(runtime, "submit_attention", submit_with_preceding_generic)
                phase = "generic"
                document.write_bytes(body.encode("utf-8"))
                await eventually(lambda: len(attention_inputs(runtime)) == 2)
                queued = runtime.conversation.pending(main_id)
                assert len(queued) == 2
                assert all(item["state"] == "pending" for item in queued)
                assert queued[0]["id"] == generic_receipts[0]["id"]
                assert queued[0]["content"] == generic_text and "project_review" not in queued[0]
                assert queued[1]["project_review"]["project_id"] == task_id
                assert queued[0]["seq"] < queued[1]["seq"] and not runtime.tasks
                monkeypatch.setattr(runtime, "submit_attention", submit_attention)
                restored = await web.patch(
                    "/api/settings", json={"models": {"main": {"model": main_model}}}
                )
                assert (
                    restored.status_code == 200 and runtime.config.settings.models.main.configured
                )
                await asyncio.wait_for(generic_held.wait(), 4)
                manager = app.state.channel_application.manager
                for adapter in manager.adapters.values():
                    adapter.http.client = channel_client
                remaining = runtime.conversation.pending(main_id)
                assert len(remaining) == 1 and remaining[0]["id"] == queued[1]["id"]

            real_handler = runtime.tools["fs_read"].handler

            async def hold_real_read(arguments: dict[str, Any]) -> Any:
                result = await real_handler(arguments)
                assert isinstance(result, dict) and result["content"] == body
                real_reads.append(result)
                held.set()
                await release.wait()
                return result

            runtime.tools["fs_read"] = replace(runtime.tools["fs_read"], handler=hold_real_read)
            if generic_boundary is None:
                phase = "project"
                document.write_bytes(body.encode("utf-8"))
            else:
                generic_release.set()
            if boundary == "tool-batch":
                for expected_tool in ("commitment_read", "file_inbox_get", "fs_read"):

                    def waiting_for_approval(tool_name: str = expected_tool) -> bool:
                        return any(
                            (runtime.store.get("approval", identifier) or {}).get("tool")
                            == tool_name
                            for identifier in runtime.permissions.pending
                        )

                    await eventually(waiting_for_approval)
                    approval = next(
                        identifier
                        for identifier in runtime.permissions.pending
                        if runtime.store.get("approval", identifier)["tool"] == expected_tool
                    )
                    approved = await web.post(
                        f"/api/approvals/{approval}", json={"decision": "approve"}
                    )
                    assert approved.status_code == 200
            await asyncio.wait_for(held.wait(), 4)
            notices = [item for item in attention_inputs(runtime) if "project_review" in item]
            assert len(notices) == 1 and notices[0]["state"] == "delivered"
            assert notices[0]["project_review"]["project_id"] == task_id
            assert notices[0]["mode"] == "ask" and runtime.main_session()["mode"] == "yolo"
            assert all(
                "channel_send" not in names and "spawn_agent" not in names
                for names in project_schemas
            )

            raw, headers = onebot_body(
                {
                    **onebot_event(),
                    "self_id": 123,
                    "message_id": "project-owner-input",
                    "message": [{"type": "text", "data": {"text": owner_text}}],
                }
            )
            assert await manager.webhook("onebot", raw, headers) == {"ok": True}
            pending = runtime.conversation.pending(main_id)
            assert len(pending) == 1 and pending[0]["content"] == owner_text
            assert pending[0]["principal"] == "owner" and pending[0]["mode"] == "yolo"
            assert pending[0]["route"]["account_id"] == "123"
            assert "project_review" not in pending[0]
            row = runtime.store.db.execute(
                "SELECT payload FROM ingress WHERE id=?", (pending[0]["id"],)
            ).fetchone()
            assert row is not None
            original_payload = row[0]
            assert await manager.webhook("onebot", raw, headers) == {"ok": True}
            assert runtime.conversation.pending(main_id) == pending
            assert runtime.run_ids[main_id] == project_run
            phase = "owner"
            release.set()
            await eventually(lambda: bool(owner_runs) and not runtime.tasks)
            assert len(owner_runs) == 1 and owner_runs[0] != project_run
            original_run = runtime.store.get("run", project_run)
            assert original_run is not None and original_run["status"] == "completed"
            assert "被新输入中断" in original_run["result"] and "尚未完成" in original_run["result"]
            messages = runtime.store.messages(main_id)
            project_input = next(item for item in messages if item["id"] == notices[0]["id"])
            delivered = next(item for item in messages if item["id"] == pending[0]["id"])
            assert project_input["run_id"] == project_run
            assert delivered["run_id"] == owner_runs[0]
            project_messages = [
                item for item in messages if project_input["seq"] <= item["seq"] < delivered["seq"]
            ]
            project_outputs = [item for item in project_messages if item["role"] == "assistant"]
            assert project_outputs and all(
                item["delivery_routes"] == [] for item in project_outputs
            )
            assert all(item["reply_origin"]["kind"] == "web" for item in project_outputs)
            requests = [request for run, request in prepared_requests if run == project_run]
            if generic_boundary is not None:
                assert generic_runs == [project_run]
                assert requests[0].project_review is None
                ordinary = next(
                    item for item in messages if item["id"] == generic_receipts[0]["id"]
                )
                assert ordinary["run_id"] == project_run and ordinary["seq"] < project_input["seq"]
                ordinary_messages = [
                    item
                    for item in messages
                    if ordinary["seq"] <= item["seq"] < project_input["seq"]
                ]
                if generic_boundary == "tool-batch":
                    ordinary_calls = [
                        entry for item in ordinary_messages for entry in item.get("tool_calls", [])
                    ]
                    ordinary_results = [
                        item for item in ordinary_messages if item["role"] == "tool"
                    ]
                    assert len(ordinary_calls) == len(ordinary_results) == 1
                    assert (
                        ordinary_calls[0]["id"]
                        == ordinary_results[0]["tool_call_id"]
                        == "ordinary-not-started"
                    )
                    assert json.loads(ordinary_results[0]["content"])["status"] == "not_executed"
                else:
                    assert not any(
                        item.get("tool_calls") or item["role"] == "tool"
                        for item in ordinary_messages
                    )
                requests = requests[1:]
            else:
                assert not generic_receipts and not generic_runs
            assert len(requests) == (2 if boundary == "tool-batch" else 1)
            assert all(
                request.project_review == notices[0]["project_review"] for request in requests
            )
            assert all(request.reply_origin.delivery_routes() == [] for request in requests)
            assert all(
                request.project_review is None
                for run, request in prepared_requests
                if run == owner_runs[0]
            )
            assert (
                delivered["run_id"] == owner_runs[0] and delivered["route"] == pending[0]["route"]
            )
            row = runtime.store.db.execute(
                "SELECT state,payload FROM ingress WHERE id=?", (pending[0]["id"],)
            ).fetchone()
            assert (
                row is not None
                and row["state"] == "delivered"
                and row["payload"] == original_payload
            )
            if boundary == "tool-batch":
                assert len(real_reads) == 1 and real_reads[0]["content"] == body
                batch = next(
                    item
                    for item in project_messages
                    if any(entry["id"] == "read-real-file" for entry in item.get("tool_calls", []))
                )
                results = {
                    item["tool_call_id"]: json.loads(item["content"])
                    for item in project_messages
                    if item["role"] == "tool"
                    and item.get("tool_call_id") in {"read-real-file", "not-started"}
                }
                assert len(batch["tool_calls"]) == len(results) == 2
                assert results["read-real-file"]["content"] == body
                assert results["not-started"]["status"] == "not_executed"
            else:
                assert real_reads == [] and not runtime.permissions.pending

            final = next(
                item
                for item in messages
                if item["role"] == "assistant" and item["content"] == owner_reply
            )
            event_id = next(
                event["id"]
                for event in runtime.store.events(session_id=main_id)
                if event["type"] == "message.completed"
                and event["data"]["message"]["id"] == final["id"]
            )

            def consumed() -> bool:
                current = runtime.store.db.execute(
                    "SELECT value FROM channel_state WHERE name='events'"
                ).fetchone()
                return current is not None and int(current[0]) >= event_id

            async with asyncio.timeout(4), manager.changed:
                await manager.changed.wait_for(consumed)
            deliveries = manager.deliveries()
            assert len(deliveries) == 1
            delivery = await terminal(manager, deliveries[0]["id"])
            assert delivery["state"] == "sent" and delivery["attempts"] == 1
            assert sent == [owner_reply]
    finally:
        monkeypatch.setattr(runtime, "submit_attention", submit_attention)
        release.set()
        generic_release.set()
        try:
            await app.state.close()
        finally:
            try:
                await runtime.close()
            finally:
                if runtime.config.settings.models.main.model != main_model:
                    runtime.config.update({"models": {"main": {"model": main_model}}})
