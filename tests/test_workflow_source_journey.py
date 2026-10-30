"""真实来源读取、再派工与重开；只替换模型提供商和离线 HTTP 传输。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import consumed, terminal
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, tool_result
from test_workflows import step

from muli.channel_application import ChannelApplication
from muli.channels import ChannelError, IncomingMessage
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app
from muli.workflow_sources import SOURCE_NOTICE, WorkflowSources

JsonObject = dict[str, Any]


def accepted(messages: list[Message], identifier: str) -> JsonObject:
    result = tool_result(messages, identifier)
    assert "error" not in result, f"真实工具失败：{identifier}/{result.get('error')}"
    return result


def materialized(prompt: str) -> list[JsonObject]:
    assert SOURCE_NOTICE in prompt, "新节点请求未收到宿主物化资料"
    result, _ = json.JSONDecoder().raw_decode(prompt.split(SOURCE_NOTICE, 1)[1])
    assert isinstance(result, list) and len(result) == 1
    assert isinstance(result[0], dict)
    return result


def successful_file_reads(runtime: Runtime, path: Path) -> int:
    events = runtime.store.events(limit=500)
    started = {
        (event["session_id"], event["data"]["call_id"])
        for event in events
        if event["type"] == "tool.started"
        and event["data"]["name"] == "fs_read"
        and event["data"]["arguments"].get("path") == str(path)
    }
    completed = {
        (event["session_id"], event["data"]["call_id"])
        for event in events
        if event["type"] == "tool.completed"
        and (event["session_id"], event["data"]["call_id"]) in started
        and event["data"]["status"] == "completed"
        and json.loads(event["data"]["result"]).get("path") == str(path)
    }
    assert len(started) == len(completed), "真实文件读取缺少成功回执"
    return len(completed)


@pytest.mark.parametrize(
    "unknown_outbound",
    [False, True],
    ids=["source-pages-to-local-review", "unknown-outbound-not-retried"],
)
async def test_workflow_source_full_runtime_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown_outbound: bool
) -> None:
    source_path, independent_path, draft_path = (
        tmp_path / name for name in ("source.txt", "independent.txt", "draft.txt")
    )
    original = "第一段：SOURCE_A，资料已经整理。\n\n第二段：SOURCE_B，完整原件必须包含本段。\n"
    independent = "独立资料：INDEPENDENT_OK。\n"
    source_path.write_text(original, encoding="utf-8")
    independent_path.write_text(independent, encoding="utf-8")
    source_name, continuation_name = "有来源的真实失败计划", "沿用资料补本地草稿"
    continue_prefix = "按准确旧运行补做：\n"
    requests: list[str] = []
    model_requests: list[str] = []
    failures: list[str] = []
    inspected_directories: list[JsonObject] = []
    inspected_pages: list[JsonObject] = []
    node_evidence: dict[str, list[JsonObject]] = {}
    unknown_ids: list[str] = []
    channels: ChannelApplication | None = None
    source_steps = [
        {**step("read"), "prompt": f"读取 {source_path}，完整返回原资料。"},
        {**step("independent"), "prompt": f"读取 {independent_path}，返回独立资料。"},
        {**step("compute", depends_on=["read"]), "prompt": "根据资料完成计算。"},
        {
            **step("review", depends_on=["compute", "independent"], role="reviewer"),
            "prompt": "独立核查结果及两份资料。",
        },
    ]

    async def transport(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        requests.append(request.url.path)
        assert unknown_outbound, "普通离线旅程不得访问网络"
        assert request.url.host == "platform.invalid"
        assert request.method == "POST" and request.url.path == "/send_private_msg"
        raise httpx.ReadTimeout("离线替身：远端可能已接收，回执未知", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)

    class Client(FakeClient):
        def __init__(self) -> None:
            super().__init__([], [])
            self.round_index = 0
            self.directory: JsonObject | None = None
            self.page_call: str | None = None
            self.pages: list[JsonObject] = []
            self.created = False
            self.sent = False
            self.send_observed = False

        def continue_turn(self, messages: list[Message], run_id: str) -> ModelTurn:
            if self.created:
                assert self.directory is not None
                result = accepted(messages, "continue-create")
                assert result["source"]["revision"] == self.directory["revision"]
                assert set(result["nodes"]) == {"draft", "review"}
                return ModelTurn(content="已安排补做，沿用旧资料，结果待独立核验。")
            if self.directory is None:
                if self.round_index == 1:
                    return ModelTurn(
                        tool_calls=[call("inspect-directory", "workflow_inspect", run_id=run_id)]
                    )
                self.directory = accepted(messages, "inspect-directory")
                assert self.directory["run_id"] == run_id and self.directory["status"] == "failed"
                assert self.directory["authority"] == "assistant_data"
                assert self.directory["source_completeness"] == "stored_record_only"
                inspected_directories.append(self.directory)
            offset = 0
            if self.page_call is not None:
                page = accepted(messages, self.page_call)
                assert page["revision"] == self.directory["revision"]
                assert page["run_id"] == run_id and page["node_id"] == "read"
                assert page["result_truncated"] is False
                assert page["authority"] == "assistant_data"
                assert page["source_completeness"] == "stored_record_only"
                assert isinstance(page["producing_run_id"], str)
                read_node = next(
                    node for node in self.directory["nodes"] if node["node_id"] == "read"
                )
                assert page["session_id"] == read_node["session_id"]
                assert page["producing_run_id"] == read_node["producing_run_id"]
                assert page["offset_bytes"] == sum(item["length_bytes"] for item in self.pages)
                assert page["length_bytes"] == len(page["content"].encode("utf-8")) > 0
                self.pages.append(page)
                inspected_pages.append(page)
                offset = page["next_offset_bytes"]
                if not page["has_more"]:
                    assert len(self.pages) >= 2
                    read_node = next(
                        node for node in self.directory["nodes"] if node["node_id"] == "read"
                    )
                    assert offset == page["total_bytes"] == read_node["content_bytes"]
                    assert (
                        len("".join(item["content"] for item in self.pages).encode("utf-8"))
                        == offset
                    )
                    self.created = True
                    return ModelTurn(
                        tool_calls=[
                            call(
                                "continue-create",
                                "workflow_create",
                                name=continuation_name,
                                steps=[
                                    {
                                        **step("draft"),
                                        "prompt": f"根据宿主附上的完整旧资料，写本地草稿到 {draft_path}，不要外发。",
                                    },
                                    {
                                        **step("review", depends_on=["draft"], role="reviewer"),
                                        "prompt": f"独立读取 {draft_path}，对照宿主附上的旧资料检查草稿；不要外发。",
                                    },
                                ],
                                source={
                                    "run_id": run_id,
                                    "revision": self.directory["revision"],
                                    "node_ids": ["read"],
                                    "targets": ["draft", "review"],
                                },
                            )
                        ]
                    )
            self.page_call = f"inspect-page-{len(self.pages)}"
            return ModelTurn(
                tool_calls=[
                    call(
                        self.page_call,
                        "workflow_inspect",
                        run_id=run_id,
                        node_id="read",
                        revision=self.directory["revision"],
                        offset_bytes=offset,
                        length_bytes=32,
                    )
                ]
            )

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            names = {item["function"]["name"] for item in tools}
            prompt = next(
                (
                    item.content
                    for item in messages
                    if item.role == "user" and item.content.startswith("工作流：")
                ),
                "",
            )
            first = self.round_index == 0
            self.round_index += 1
            assert self.round_index <= 16, "提供商旅程超出有限轮次"
            model_requests.append(prompt.split("\n", 2)[1] if prompt else "main")
            if prompt.startswith(f"工作流：{source_name}\n节点：compute\n"):
                assert "SOURCE_A" in prompt and "SOURCE_B" in prompt
                failures.append("实际依赖已到达后的受控提供商故障")
                raise RuntimeError("offline_controlled_compute_failure")
            if prompt.startswith(f"工作流：{source_name}\n节点：read\n"):
                if first:
                    assert "fs_read" in names
                    turn = ModelTurn(
                        tool_calls=[call("source-read", "fs_read", path=str(source_path))]
                    )
                else:
                    result = accepted(messages, "source-read")
                    assert result["content"] == original
                    turn = ModelTurn(content=result["content"])
            elif prompt.startswith(f"工作流：{source_name}\n节点：independent\n"):
                if first:
                    assert "fs_read" in names
                    turn = ModelTurn(
                        tool_calls=[call("independent-read", "fs_read", path=str(independent_path))]
                    )
                else:
                    result = accepted(messages, "independent-read")
                    assert result["content"] == independent
                    turn = ModelTurn(content=result["content"])
            elif prompt.startswith(f"工作流：{continuation_name}\n节点：draft\n"):
                evidence = materialized(prompt)
                node_evidence["draft"] = evidence
                # 草稿只从真实新节点请求取旧正文，不能从闭包的 original 偷渡。
                old_text = evidence[0]["output"]
                if first:
                    assert "fs_write" in names
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "draft-write",
                                "fs_write",
                                path=str(draft_path),
                                content="本地草稿\n" + old_text,
                            )
                        ]
                    )
                else:
                    result = accepted(messages, "draft-write")
                    assert result["path"] == str(draft_path) and result["bytes"] > 0
                    turn = ModelTurn(content=f"本次草稿：{draft_path}\n{old_text}")
            elif prompt.startswith(f"工作流：{continuation_name}\n节点：review\n"):
                evidence = materialized(prompt)
                node_evidence["review"] = evidence
                old_text = evidence[0]["output"]
                if first:
                    assert "fs_read" in names and "fs_write" not in names
                    turn = ModelTurn(
                        tool_calls=[call("review-read", "fs_read", path=str(draft_path))]
                    )
                else:
                    result = accepted(messages, "review-read")
                    assert result["content"] == "本地草稿\n" + old_text
                    turn = ModelTurn(
                        content="已沿用完整旧资料，独立读取并核验本次草稿。\n" + result["content"]
                    )
            else:
                assert not prompt, "遇到未计划的工作流节点"
                latest_owner = next(
                    item.content for item in reversed(messages) if item.role == "user"
                )
                if latest_owner.startswith(continue_prefix):
                    assert "workflow_inspect" in names and "workflow_create" in names
                    reference = json.loads(latest_owner.removeprefix(continue_prefix))
                    turn = self.continue_turn(messages, reference["run_id"])
                elif unknown_outbound and not self.sent:
                    assert "channel_send" in names
                    self.sent = True
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "source-send",
                                "channel_send",
                                channel="onebot",
                                target="7",
                                text="本次离线来源旅程的合成外发记录",
                                idempotency_key="source-outcome-unknown",
                            )
                        ]
                    )
                else:
                    if unknown_outbound and not self.send_observed:
                        assert channels is not None
                        queued = accepted(messages, "source-send")
                        delivered = await terminal(channels.manager, queued["id"])
                        assert delivered["state"] == "unknown" and delivered["attempts"] == 1
                        unknown_ids.append(delivered["id"])
                        self.send_observed = True
                    if not self.created:
                        assert "workflow_create" in names
                        self.created = True
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "source-create",
                                    "workflow_create",
                                    name=source_name,
                                    steps=source_steps,
                                )
                            ]
                        )
                    else:
                        result = accepted(messages, "source-create")
                        assert isinstance(result.get("workflow_id"), str)
                        turn = ModelTurn(content="已安排原计划，结果待核验。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model in {"main", "reviewer"}
        return Client()

    runtime, _ = make_runtime(tmp_path, [])
    preserved: list[tuple[str, str, JsonObject]] = []
    try:
        settings: JsonObject = {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"enabled": False, "auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "limits": {"max_run_seconds": 30},
        }
        if unknown_outbound:
            settings["channels"] = {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://platform.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7"],
                }
            }
        runtime.config.update(settings)
        await runtime.reconfigure()
        runtime.client_factory = factory
        if unknown_outbound:
            app = create_app(runtime.config, runtime.data_dir, runtime)
            channels = app.state.channel_application
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765"
            ) as web:
                changed = await web.patch(
                    f"/api/sessions/{runtime.main_session()['id']}", json={"mode": "yolo"}
                )
                assert changed.status_code == 200
            await channels.start()
            first = await channels.manager.receive(
                "onebot",
                IncomingMessage(
                    "source-owner-input", "7", "7", "先发合成记录，再建立本地资料计划。"
                ),
            )
        else:
            first = await runtime.submit("建立本地资料、计算与独立审查计划。", mode="yolo")
        assert isinstance(first["run_id"], str) and first["quarantined"] is False
        async with asyncio.timeout(10):
            with pytest.raises(ValueError, match="子工作流未成功完成"):
                await runtime.wait(first["run_id"])
        sources = [
            item
            for item in runtime.store.list("workflow_run")
            if item.get("parent_run_id") == first["run_id"]
        ]
        assert len(sources) == 1
        async with asyncio.timeout(10):
            source = await runtime.workflows.wait(sources[0]["id"])
        assert source["status"] == "failed" and len(failures) == 1
        assert {key: node["status"] for key, node in source["nodes"].items()} == {
            "read": "completed",
            "independent": "completed",
            "compute": "failed",
            "review": "skipped",
        }
        assert source["nodes"]["read"]["result"] == original
        assert successful_file_reads(runtime, source_path) == 1
        assert successful_file_reads(runtime, independent_path) == 1
        source_session_ids = {node["session_id"] for node in source["nodes"].values()}
        record_ids = [
            ("workflow_run", source["id"]),
            ("workflow", source["workflow_id"]),
            ("run", first["run_id"]),
            *[("session", identifier) for identifier in source_session_ids],
            *[
                ("run", item["id"])
                for item in runtime.store.list("run")
                if item["session_id"] in source_session_ids
            ],
        ]
        for kind, identifier in record_ids:
            record = runtime.store.get(kind, identifier)
            assert record is not None
            preserved.append((kind, identifier, record))
        assert runtime.store.get("run", first["run_id"])["status"] == "failed"
        assert not runtime.tasks and not runtime.workflows.active_run_ids
        assert not runtime._session_finalizers
        if unknown_outbound:
            assert channels is not None and len(unknown_ids) == 1
            old_delivery = channels.manager.delivery(unknown_ids[0])
            assert old_delivery is not None and old_delivery["state"] == "unknown"
            assert requests == ["/send_private_msg"]
            assert source["reply_origin"]["kind"] == "channel"
        else:
            assert requests == []
        second = await runtime.submit(
            continue_prefix
            + json.dumps(
                {
                    "run_id": source["id"],
                    "requirement": "沿用 read 的完整旧资料，只补本地草稿和独立评审，不要外发。",
                },
                ensure_ascii=False,
            )
        )
        assert second["run_id"] != first["run_id"] and second["quarantined"] is False
        async with asyncio.timeout(10):
            result = await runtime.wait(second["run_id"])
        continuations = [
            item
            for item in runtime.store.list("workflow_run")
            if item.get("parent_run_id") == second["run_id"]
        ]
        assert len(continuations) == 1
        async with asyncio.timeout(10):
            continued = await runtime.workflows.wait(continuations[0]["id"])
        assert continued["status"] == "completed" and set(continued["nodes"]) == {"draft", "review"}
        assert continued["mode"] == "yolo"
        assert all(node["status"] == "completed" for node in continued["nodes"].values())
        assert continued["parent_session_id"] == second["session_id"] == first["session_id"]
        assert continued["reply_origin"]["kind"] == "web"
        assert continued["reply_origin"]["input_id"] == second["message"]["id"]
        assert continued["reply_origin"]["input_id"] != source["reply_origin"]["input_id"]
        parent = runtime.store.get("run", second["run_id"])
        assert parent is not None and parent["status"] == "completed"
        assert "完整旧资料" in result and original in result
        assert draft_path.read_text(encoding="utf-8") == "本地草稿\n" + original
        assert successful_file_reads(runtime, draft_path) == 1
        assert successful_file_reads(runtime, source_path) == 1
        assert successful_file_reads(runtime, independent_path) == 1
        assert len(inspected_directories) == 1 and len(inspected_pages) >= 2
        assert "".join(page["content"] for page in inspected_pages) == original
        revision = inspected_directories[0]["revision"]
        for node_id in ("draft", "review"):
            evidence = node_evidence[node_id]
            assert evidence == materialized(continued["nodes"][node_id]["prompt"])
            assert evidence[0]["output"] == original and evidence[0]["truncated"] is False
            assert evidence[0]["run_id"] == source["id"] and evidence[0]["revision"] == revision
            assert evidence[0]["node_id"] == "read" and evidence[0]["authority"] == "assistant_data"
            assert evidence[0]["session_id"] == source["nodes"]["read"]["session_id"]
            assert evidence[0]["producing_run_id"] == inspected_pages[0]["producing_run_id"]
        new_session_ids = {node["session_id"] for node in continued["nodes"].values()}
        assert not source_session_ids & new_session_ids
        create_events = [
            event["data"]["arguments"]
            for event in runtime.store.events()
            if event["type"] == "tool.started"
            and event["data"]["name"] == "workflow_create"
            and "source" in event["data"]["arguments"]
        ]
        assert len(create_events) == 1 and original not in json.dumps(
            create_events[0], ensure_ascii=False
        )
        assert create_events[0]["source"] == {
            "run_id": source["id"],
            "revision": revision,
            "node_ids": ["read"],
            "targets": ["draft", "review"],
        }
        reports = [
            item
            for item in runtime.store.messages(first["session_id"])
            if item.get("workflow_run_id") is not None
        ]
        assert len(reports) == 1 and reports[0]["workflow_run_id"] == continued["id"]
        assert continued["result_message_id"] == reports[0]["id"]
        assert reports[0]["reply_origin"] == continued["reply_origin"]
        assert reports[0]["delivery_routes"] == []
        for kind, identifier, old_record in preserved:
            assert runtime.store.get(kind, identifier) == old_record
        assert not runtime.tasks and not runtime.workflows.active_run_ids
        assert not runtime._session_finalizers and not runtime.permissions.pending
        if unknown_outbound:
            assert channels is not None
            await consumed(channels.manager, runtime.store.event_position())
            assert channels.manager.delivery(unknown_ids[0]) == old_delivery
            assert len(channels.manager.deliveries()) == 1 and requests == ["/send_private_msg"]
        else:
            assert requests == []
        config, data_dir = runtime.config, runtime.data_dir
        calls_before_reopen = len(model_requests)
    finally:
        try:
            if channels is not None:
                await channels.close()
        finally:
            await runtime.close()

    def no_replay(config: ModelConfig) -> FakeClient:
        raise AssertionError("正常重开不得重演模型或工作流节点")

    reopened = Runtime(config, data_dir, client_factory=no_replay)
    reopened_channels: ChannelApplication | None = None
    try:
        if unknown_outbound:
            app = create_app(reopened.config, reopened.data_dir, reopened)
            reopened_channels = app.state.channel_application
            await reopened_channels.start()
            # 发布真实当前渠道状态，等待已启动消费者越过它；不用 sleep 冒充观察。
            await reopened_channels.manager._publish()
            await consumed(reopened_channels.manager, reopened.store.event_position())
            delivery = reopened_channels.manager.delivery(unknown_ids[0])
            assert delivery == old_delivery
            assert delivery["state"] == "unknown" and delivery["attempts"] == 1
            assert len(reopened_channels.manager.deliveries()) == 1
            with pytest.raises(ChannelError, match="结果未知"):
                await reopened_channels.manager.retry(unknown_ids[0])
            assert requests == ["/send_private_msg"]
        page = WorkflowSources(reopened).inspect(source["id"], node_id="read", revision=revision)
        assert page["content"] == original and page["has_more"] is False
        assert page["session_id"] == source["nodes"]["read"]["session_id"]
        saved = reopened.store.get("workflow_run", continued["id"])
        assert saved is not None and saved["status"] == "completed"
        assert materialized(saved["nodes"]["draft"]["prompt"])[0]["output"] == original
        assert materialized(saved["nodes"]["review"]["prompt"])[0]["revision"] == revision
        for kind, identifier, old_record in preserved:
            assert reopened.store.get(kind, identifier) == old_record
        assert successful_file_reads(reopened, source_path) == 1
        assert len(model_requests) == calls_before_reopen
        assert not reopened.tasks and not reopened.workflows.active_run_ids
        assert not reopened._session_finalizers
    finally:
        try:
            if reopened_channels is not None:
                await reopened_channels.close()
        finally:
            await reopened.close()
