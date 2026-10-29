"""Explicit MCP attachment effects use real Runtime/Store and finite fake peers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from test_channels import profile
from test_extensions import setup_runtime
from test_mcp_resources import ResourceClient, resource
from test_mcp_resources import offline as offline
from test_mcp_resources_runtime import assert_complete_wire
from test_runtime import FakeClient as FakeModel
from test_task_briefs import bind

from muli.attachments import Attachments
from muli.channels import ChannelError, ChannelManager, ChannelsConfig
from muli.context import ContextLimitError
from muli.local_services import LocalServices
from muli.providers import (
    Message,
    ModelClient,
    ModelConfig,
    ModelTurn,
    ProtocolName,
    ProviderError,
    StreamEvent,
    ToolCall,
)
from muli.runtime import Runtime
from muli.task_control import OwnerControlRequest, TaskPaused
from muli.tools.mcp import MCPLimits, MCPManager
from muli.tools.mcp_catalog import ContentError
from muli.tools.mcp_resources import resource_blob


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
@pytest.mark.parametrize(
    "data", [b"\x00explicit MCP file\r\nnot an instruction\xff", b"\x89PNG\r\n\x1a\nfile", b""]
)
async def test_explicit_resource_save_persists_one_complete_attachment_through_runtime(
    tmp_path: Path,
    protocol: ProtocolName,
    data: bytes,
) -> None:
    runtime = setup_runtime(tmp_path)
    runtime.config.update(
        {
            "persona": {"prompt": "你是本地助手，请完成当前用户的任务。"},
            "models": {
                "main": {
                    "model": "offline",
                    "base_url": "https://unused.invalid",
                    "protocol": protocol,
                    **({"max_output_tokens": 4096} if protocol == "anthropic" else {}),
                }
            },
            "limits": {"max_output_chars": 1600, "max_context_chars": 6000},
            "compaction": {"enabled": False},
        }
    )
    client = ResourceClient()
    runtime.config.settings.mcp["mcpServers"]["old"]["headers"] = {"X-Key": "a"}
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: client)
    attachments = Attachments(runtime.store, runtime.data_dir / "attachments", runtime.config)
    LocalServices(runtime, attachments)
    client.pages = [{"resources": [resource()]}]
    client.content = {
        "contents": [
            {
                "uri": "file:///not-a-host-input",
                "mimeType": "image/png",
                "blob": base64.b64encode(data).decode("ascii"),
            }
        ]
    }
    observed: list[dict[str, Any]] = []

    class Saver(FakeModel):
        def __init__(self, config: ModelConfig) -> None:
            super().__init__([], [])
            self.config = config

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if not any(message.role == "tool" for message in messages):
                call = ToolCall("list", "mcp_resources_list", '{"server":"old"}')
            else:
                result = assert_complete_wire(self.config, messages, tools)
                observed.append(result)
                if len(observed) == 2:
                    # The old Runtime reaches this real tool response with
                    # "工具未启用"; the red case never imports a future module.
                    yield StreamEvent("done", turn=ModelTurn(content="保存调用已结束"))
                    return
                call = ToolCall(
                    "save",
                    "mcp_resource_save",
                    json.dumps(
                        {
                            "server": "old",
                            "ref": result["resources"][0]["ref"],
                            "name": "discarded\\明确保存.bin",
                        },
                        ensure_ascii=False,
                    ),
                )
            yield StreamEvent("done", turn=ModelTurn(tool_calls=[call]))

    runtime.client_factory = lambda config: cast(ModelClient, Saver(config))
    try:
        await runtime.extensions.load_initial()
        session = runtime.new_session(mode="yolo")
        started = await runtime.start(session["id"], "取回并保存选中的资源文件", mode="yolo")
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == "保存调用已结束"
        assert len(observed) == 2
        receipt = observed[1]
        assert receipt.get("saved") is True, receipt
        assert receipt["is_error"] is False and receipt["untrusted"] is True
        item = receipt["attachment"]
        assert item == {
            "id": item["id"],
            "mime_type": "image/png" if data.startswith(b"\x89PNG") else "application/octet-stream",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        metadata = attachments.get(item["id"])
        assert metadata["name"] == "明确保存.bin" and metadata["source"] == "mcp"
        assert attachments.path(item["id"]).read_bytes() == data
        assert len(runtime.store.list("attachment")) == 1
        assert client.resource_cursors == [None] and client.reads == [resource()["uri"]]
        assert len(list(attachments.directory.iterdir())) == 1 and attachments.reserved == 0
        assert runtime.store.list("channel_outbox") == []
    finally:
        await runtime.close()


async def file_runtime(tmp_path: Path) -> tuple[Runtime, ResourceClient, Attachments, str, str]:
    runtime = setup_runtime(tmp_path)
    client = ResourceClient()
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: client)
    attachments = Attachments(runtime.store, runtime.data_dir / "attachments", runtime.config)
    LocalServices(runtime, attachments)
    client.pages = [{"resources": [resource(size=1)]}]
    client.content = {"contents": [{"uri": "custom:///selected", "blob": "YWJj"}]}
    await runtime.extensions.load_initial()
    session = runtime.new_session(mode="yolo")
    runtime.run_ids[session["id"]] = "offline-owner"
    result = await runtime.tools["mcp_resources_list"].handler({"server": "old"})
    return runtime, client, attachments, session["id"], result["resources"][0]["ref"]


async def save_call(runtime: Runtime, session_id: str, ref: str, name: str = "file.bin") -> Any:
    return await runtime._tool(
        ToolCall(
            "save", "mcp_resource_save", json.dumps({"server": "old", "ref": ref, "name": name})
        ),
        runtime.session_tools(session_id),
        "yolo",
        session_id,
        "offline-owner",
    )


@pytest.mark.parametrize(
    "block",
    [
        {"uri": "test:///bad", "blob": "@bad"},
        {"uri": "test:///bad", "blob": "中文"},
        {"uri": "test:///bad", "blob": "YW Jj"},
        {"uri": "test:///bad", "blob": 3},
        {"uri": "test:///bad", "blob": "YWJj", "text": "ambiguous"},
        {"uri": "test:///bad", "text": "text"},
        {"uri": "test:///bad", "blob": "YWJj", "mimeType": 3},
        {"uri": None, "blob": "YWJj"},
    ],
)
async def test_invalid_blob_has_no_attachment_effect(tmp_path: Path, block: dict[str, Any]) -> None:
    runtime, client, attachments, session_id, ref = await file_runtime(tmp_path)
    client.content = {"contents": [block]}
    try:
        result = await save_call(runtime, session_id, ref)
        assert result["is_error"] and result["saved"] is False
        assert runtime.store.list("attachment") == []
        assert list(attachments.directory.iterdir()) == [] and attachments.reserved == 0
        assert len(client.reads) == 1
    finally:
        await runtime.close()


@pytest.mark.parametrize("contents", [[], [{"uri": "test:///one", "blob": ""}] * 2, None])
def test_save_requires_exactly_one_block(contents: Any) -> None:
    with pytest.raises(ContentError):
        resource_blob({"contents": contents}, 100)


@pytest.mark.parametrize("limit", [1, 2])
def test_decoded_size_cannot_bypass_single_file_limit(limit: int) -> None:
    with pytest.raises(ContentError, match="resource_file_budget_exceeded"):
        resource_blob({"contents": [{"uri": "file:///never-opened", "blob": "YWJj"}]}, limit)


@pytest.mark.parametrize(
    "boundary", ["invalid_ref", "invalid_name", "owner", "closing", "disabled", "maintenance"]
)
async def test_host_admission_rejects_before_sdk_or_save(tmp_path: Path, boundary: str) -> None:
    runtime, client, attachments, session_id, ref = await file_runtime(tmp_path)
    try:
        name = "file.bin"
        if boundary == "invalid_ref":
            ref = "never-shown"
        elif boundary == "invalid_name":
            name = " "
        elif boundary == "owner":
            runtime.run_ids[session_id] = "different-owner"
        elif boundary == "closing":
            runtime._closing = True
        elif boundary == "disabled":
            runtime.config.settings.disabled_tools.append("mcp_resource_save")
        else:
            runtime.maintenance.admission.pause()
        result = await save_call(runtime, session_id, ref, name)
        assert "error" in result or result.get("status") == "not_executed"
        assert client.reads == [] and runtime.store.list("attachment") == []
        assert list(attachments.directory.iterdir()) == []
    finally:
        runtime._closing = False
        runtime.maintenance.admission.release()
        await runtime.close()


async def test_save_without_real_runtime_context_is_rejected(tmp_path: Path) -> None:
    runtime, client, attachments, _, ref = await file_runtime(tmp_path)
    try:
        with pytest.raises(PermissionError, match="真实执行上下文"):
            await runtime.tools["mcp_resource_save"].handler(
                {"server": "old", "ref": ref, "name": "x"}
            )
        assert client.reads == [] and list(attachments.directory.iterdir()) == []
    finally:
        await runtime.close()


@pytest.mark.parametrize("delta", [0, -1])
async def test_receipt_budget_checked_before_save(tmp_path: Path, delta: int) -> None:
    runtime, client, attachments, session_id, ref = await file_runtime(tmp_path)
    receipt = {
        "is_error": False,
        "saved": True,
        "untrusted": True,
        "attachment": {
            "id": "0" * 32,
            "mime_type": "application/octet-stream",
            "size": 3,
            "sha256": hashlib.sha256(b"abc").hexdigest(),
        },
    }
    # A new generation freezes the exact receipt budget; obtain its own fresh ref.
    client.pages = [{"resources": [{"uri": "x:a", "name": ""}]}]
    candidate = await runtime.mcp.prepare(
        runtime.config.settings.mcp,
        limits=MCPLimits(max_output_chars=len(json.dumps(receipt)) + delta),
    )
    runtime.mcp.publish(candidate)
    tools = {tool.name: tool for tool in runtime.mcp.tools}
    result = await tools["mcp_resources_list"].handler({"server": "old"})
    assert result["is_error"] is False, result
    ref = result["resources"][0]["ref"]
    runtime.tools.update(tools)
    try:
        result = await save_call(runtime, session_id, ref)
        assert result.get("saved") is (delta == 0), result
        assert len(runtime.store.list("attachment")) == int(delta == 0)
        assert attachments.reserved == 0
    finally:
        await runtime.close()


@pytest.mark.parametrize("boundary", ["network", "capacity"])
async def test_entered_operation_keeps_original_lease_through_generation_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    runtime, client, attachments, session_id, ref = await file_runtime(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    save_count = 0
    original_save = attachments.save

    async def observe_save(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal save_count
        save_count += 1
        if boundary == "capacity":
            entered.set()
        return await original_save(*args, **kwargs)

    async def read_gate() -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(attachments, "save", observe_save)
    if boundary == "network":
        client.read_gate = read_gate
    else:
        for _ in range(runtime.config.settings.uploads.max_concurrent):
            await attachments.capacity.acquire()
    operation = asyncio.create_task(save_call(runtime, session_id, ref))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        connection = runtime.mcp._connections["old"]
        assert connection.active_calls == 1
        candidate = await runtime.mcp.prepare({"mcpServers": {}})
        runtime.mcp.publish(candidate)
        assert client.closed == 0 and connection.active_calls == 1
        release.set()
        if boundary == "capacity":
            attachments.capacity.release()
        result = await asyncio.wait_for(operation, 2)
        assert result["saved"] is True
        assert attachments.path(result["attachment"]["id"]).read_bytes() == b"abc"
        assert len(client.reads) == save_count == 1
        assert connection.active_calls == 0
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await runtime.close()


async def test_revoked_generation_never_enters_sdk_or_saver(tmp_path: Path) -> None:
    runtime, client, attachments, session_id, ref = await file_runtime(tmp_path)
    old_tools = runtime.session_tools(session_id)
    try:
        candidate = await runtime.mcp.prepare({"mcpServers": {}})
        runtime.mcp.publish(candidate)
        result = await runtime._tool(
            ToolCall(
                "save", "mcp_resource_save", json.dumps({"server": "old", "ref": ref, "name": "x"})
            ),
            old_tools,
            "yolo",
            session_id,
            "offline-owner",
        )
        assert result["status"] == "not_executed" and result["code"] == "generation_changed"
        assert client.reads == [] and list(attachments.directory.iterdir()) == []
    finally:
        await runtime.close()


@pytest.mark.parametrize("boundary", ["quota", "projection"])
async def test_saver_failure_is_unknown_without_automatic_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    runtime, client, attachments, session_id, ref = await file_runtime(tmp_path)
    if boundary == "quota":
        runtime.config.settings.uploads.max_total_bytes = 1
    else:

        def fail_projection(item: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("secret server value must not escape")

        monkeypatch.setattr(attachments, "public", fail_projection)
    try:
        result = await save_call(runtime, session_id, ref)
        assert result == {"is_error": True, "status": "unknown", "code": "resource_save_unknown"}
        assert len(client.reads) == 1 and attachments.reserved == 0
        assert list(attachments.directory.iterdir()) == []
        assert len(runtime.store.list("attachment")) == int(boundary == "projection")
    finally:
        await runtime.close()


@pytest.mark.parametrize("boundary", ["network", "capacity"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_single_deadline_or_cancel_releases_lease_and_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    cancel: bool,
) -> None:
    runtime, client, attachments, session_id, ref = await file_runtime(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_save = attachments.save

    async def read_gate() -> None:
        entered.set()
        await release.wait()

    async def save_gate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        return await original_save(*args, **kwargs)

    connection = runtime.mcp._connections["old"]
    connection.timeout = 1 if cancel else 0.05
    if boundary == "network":
        client.read_gate = read_gate
    else:
        for _ in range(runtime.config.settings.uploads.max_concurrent):
            await attachments.capacity.acquire()
        monkeypatch.setattr(attachments, "save", save_gate)
    operation = asyncio.create_task(save_call(runtime, session_id, ref))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if cancel:
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            result = await asyncio.wait_for(operation, 2)
            assert result["code"] == (
                "resource_timeout" if boundary == "network" else "resource_save_unknown"
            )
        assert len(client.reads) == 1 and connection.active_calls == 0
        assert attachments.reserved == 0 and list(attachments.directory.iterdir()) == []
    finally:
        release.set()
        await runtime.close()


@pytest.mark.parametrize("change", ["generation", "disabled"])
async def test_real_external_approval_does_not_bypass_final_save_admission(
    tmp_path: Path,
    change: str,
) -> None:
    runtime, client, attachments, session_id, ref = await file_runtime(tmp_path)
    runtime.run_ids.pop(session_id)
    runtime.config.update(
        {"models": {"main": {"model": "offline", "base_url": "https://unused.invalid"}}}
    )
    model = FakeModel(
        [
            ModelTurn(
                tool_calls=[
                    ToolCall(
                        "save",
                        "mcp_resource_save",
                        json.dumps({"server": "old", "ref": ref, "name": "explicit.bin"}),
                    )
                ]
            ),
            ModelTurn(content="原调用未执行"),
        ],
        [],
    )
    runtime.client_factory = lambda _: cast(ModelClient, model)
    try:
        started = await runtime.start(session_id, "请求批准取回和保存", mode="ask")
        async with asyncio.timeout(3):
            async with runtime.bus.changed:
                await runtime.bus.changed.wait_for(lambda: bool(runtime.permissions.pending))
            approval = next(iter(runtime.permissions.pending))
            stored = runtime.store.get("approval", approval)
            assert stored is not None and stored["risk"] == "external"
            assert stored["tool"] == "mcp_resource_save"
            if change == "generation":
                target = runtime.config.settings.model_copy(deep=True)
                target.mcp = {"mcpServers": {}}
                async with runtime.configuration_lock:
                    await runtime.extensions.apply_locked(target, persist=True)
            else:
                runtime.config.settings.disabled_tools.append("mcp_resource_save")
            await runtime.permissions.resolve(approval, "approve")
            assert await runtime.wait(started["run_id"]) == "原调用未执行"
        assert client.reads == [] and list(attachments.directory.iterdir()) == []
        result = next(
            message for message in runtime.store.messages(session_id) if message["role"] == "tool"
        )
        receipt = json.loads(result["content"])
        assert (
            receipt.get("code") == "generation_changed"
            if change == "generation"
            else receipt.get("error") == "工具未启用"
        )
    finally:
        await runtime.close()


async def test_paused_scope_rejects_save_before_sdk(tmp_path: Path) -> None:
    runtime, client, attachments, _, ref = await file_runtime(tmp_path)
    assert runtime.task_control is not None
    item = await runtime.task_control.commitments.save(
        "受控资源保存", brief={"goal": "保存附件", "constraints": []}
    )
    worker = bind(runtime, item["id"])
    runtime.run_ids[worker["id"]] = "offline-owner"
    try:
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        with pytest.raises(TaskPaused):
            await save_call(runtime, worker["id"], ref)
        assert client.reads == [] and list(attachments.directory.iterdir()) == []
    finally:
        await runtime.close()


async def test_saved_id_requires_explicit_vision_and_channel_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muli.media import MediaService

    runtime, client, attachments, session_id, ref = await file_runtime(tmp_path)
    data = b"\x89PNG\r\n\x1a\noriginal\x00\xff"
    client.content = {
        "contents": [
            {
                "uri": "https://no-extra-fetch.invalid/a",
                "blob": base64.b64encode(data).decode("ascii"),
            }
        ]
    }
    calls: list[dict[str, Any]] = []
    vision: list[bytes] = []

    async def response(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "offline-sent"}}
        )

    async def submit(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("MCP attachment save must not submit channel input")

    async def describe(self: MediaService, image: bytes, mime: str, prompt: str) -> str:
        vision.append(image)
        assert mime == "image/png"
        return "离线描述"

    http = httpx.AsyncClient(transport=httpx.MockTransport(response))
    channels = ChannelManager(
        runtime.store,
        runtime.bus,
        ChannelsConfig(onebot=profile()),
        submit,
        attachments,
        client=http,
    )
    try:
        receipt = await save_call(runtime, session_id, ref, "original.png")
        identifier = receipt["attachment"]["id"]
        assert calls == vision == [] and runtime.store.list("channel_outbox") == []
        with pytest.raises(ProviderError, match="vision"):
            await runtime.tools["image_describe"].handler({"id": identifier})
        monkeypatch.setattr(MediaService, "describe_image", describe)
        assert (await runtime.tools["image_describe"].handler({"id": identifier}))[
            "description"
        ] == "离线描述"
        assert vision == [data] and calls == []
        await channels.start()
        queued = await channels.enqueue("onebot", "7", attachment_ids=[identifier])
        sent = await asyncio.wait_for(channels.wait(queued["id"]), 2)
        assert sent is not None and sent["state"] == "sent"
        upload = next(call for call in calls if "file" in call)
        assert base64.b64decode(upload["file"].removeprefix("base64://")) == data
        attachments.path(identifier).write_bytes(b"changed")
        with pytest.raises(ChannelError, match="内容已变化"):
            await channels._file(identifier, "onebot")
        channels.config = replace(
            channels.config, onebot=replace(channels.config.onebot, max_file_bytes=1)
        )
        with pytest.raises(ChannelError, match="max_file_bytes"):
            await channels.enqueue("onebot", "7", attachment_ids=[identifier])
        assert len(client.reads) == 1
    finally:
        await channels.close()
        await http.aclose()
        await runtime.close()


@pytest.mark.parametrize("boundary", ["write", "fsync"])
async def test_cancel_during_real_file_io_keeps_lease_until_thread_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    import muli.attachments as module

    runtime, client, attachments, session_id, ref = await file_runtime(tmp_path)
    original_file_io = module.file_io
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()
    calls = 0
    selected = 1 if boundary == "write" else 2

    async def gated_file_io(operation: Callable[[], Any]) -> Any:
        nonlocal calls
        calls += 1
        if calls != selected:
            return await original_file_io(operation)

        def thread_operation() -> Any:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise AssertionError("test must release the real file I/O thread")
            try:
                return operation()
            finally:
                finished.set()

        # Retain the production shield/to_thread implementation. Only pause its
        # actual worker operation, before a real write or fsync on the temp file.
        return await original_file_io(thread_operation)

    monkeypatch.setattr(module, "file_io", gated_file_io)
    operation = asyncio.create_task(save_call(runtime, session_id, ref))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        connection = runtime.mcp._connections["old"]
        assert connection.active_calls == 1 and attachments.reserved == 3
        assert len(list(attachments.directory.iterdir())) == 1
        assert runtime.store.list("attachment") == []
        operation.cancel()
        await asyncio.sleep(0)
        assert operation.done() is False and finished.is_set() is False
        candidate = await runtime.mcp.prepare({"mcpServers": {}})
        runtime.mcp.publish(candidate)
        assert connection.active_calls == 1 and client.closed == 0
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 2)
        assert finished.is_set() is True and calls == selected
        assert connection.active_calls == 0 and attachments.reserved == 0
        assert list(attachments.directory.iterdir()) == []
        assert runtime.store.list("attachment") == [] and len(client.reads) == 1
    finally:
        release.set()
        if not operation.done():
            await asyncio.gather(operation, return_exceptions=True)
        await runtime.close()


async def test_cancel_after_real_metadata_commit_preserves_file_and_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, client, attachments, session_id, ref = await file_runtime(tmp_path)
    original_save = attachments.save
    entered, release = asyncio.Event(), asyncio.Event()
    committed: list[dict[str, Any]] = []

    async def gate_receipt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        item = await original_save(*args, **kwargs)
        # The original writer has committed metadata and the target file. Pause
        # before its caller receives that result; do not invent a transaction.
        committed.append(item)
        entered.set()
        await release.wait()
        return item

    monkeypatch.setattr(attachments, "save", gate_receipt)
    operation = asyncio.create_task(save_call(runtime, session_id, ref))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        connection = runtime.mcp._connections["old"]
        assert connection.active_calls == 1 and attachments.reserved == 0
        assert len(committed) == 1
        identifier = committed[0]["id"]
        assert attachments.path(identifier).read_bytes() == b"abc"
        candidate = await runtime.mcp.prepare({"mcpServers": {}})
        runtime.mcp.publish(candidate)
        assert connection.active_calls == 1 and client.closed == 0
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 2)
        assert connection.active_calls == 0 and attachments.reserved == 0
        assert len(committed) == len(client.reads) == 1
        assert attachments.path(identifier).read_bytes() == b"abc"
        assert attachments.get(identifier)["source"] == "mcp"
        assert len(runtime.store.list("attachment")) == 1
        assert [path.name for path in attachments.directory.iterdir()] == [identifier]
        assert not any(event["type"] == "tool.completed" for event in runtime.store.events())
    finally:
        release.set()
        if not operation.done():
            await asyncio.gather(operation, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
async def test_saved_receipt_batch_that_cannot_fit_context_stops_without_undoing_files(
    tmp_path: Path,
    protocol: ProtocolName,
) -> None:
    runtime, client, attachments, session_id, ref = await file_runtime(tmp_path)
    runtime.run_ids.pop(session_id)
    runtime.config.update(
        {
            "persona": {"prompt": "遵守主人指令"},
            "models": {
                "main": {
                    "protocol": protocol,
                    **({"max_output_tokens": 4096} if protocol == "anthropic" else {}),
                    "model": "offline",
                    "base_url": "https://unused.invalid",
                }
            },
            "limits": {"max_output_chars": 1600, "max_context_chars": 4000},
            "compaction": {"enabled": False},
        }
    )
    requests = 0
    count = 8

    class Saver(FakeModel):
        async def stream(
            self,
            messages: list[Message],
            tools: list[dict[str, Any]],
        ) -> AsyncIterator[StreamEvent]:
            nonlocal requests
            requests += 1
            assert requests == 1, "the complete saved receipt batch must not be silently excerpted"
            yield StreamEvent(
                "done",
                turn=ModelTurn(
                    tool_calls=[
                        ToolCall(
                            f"save-{index}",
                            "mcp_resource_save",
                            json.dumps(
                                {"server": "old", "ref": ref, "name": f"explicit-{index}.bin"}
                            ),
                        )
                        for index in range(count)
                    ]
                ),
            )

    runtime.client_factory = lambda _: cast(ModelClient, Saver([], []))
    try:
        started = await runtime.start(session_id, "明确保存这八个资源副本", mode="yolo")
        with pytest.raises(ContextLimitError, match="需完整保留的工具结果"):
            await asyncio.wait_for(runtime.wait(started["run_id"]), 5)
        assert requests == 1 and len(client.reads) == count
        stored = [
            message for message in runtime.store.messages(session_id) if message["role"] == "tool"
        ]
        assert len(stored) == count
        identifiers: set[str] = set()
        for message in stored:
            receipt = json.loads(message["content"])
            assert receipt["saved"] is True and receipt["is_error"] is False
            assert (
                message["content_provenance"] == "accepted" and message["preserve_content"] is True
            )
            item = receipt["attachment"]
            identifiers.add(item["id"])
            assert item["sha256"] == hashlib.sha256(b"abc").hexdigest()
            assert attachments.path(item["id"]).read_bytes() == b"abc"
        assert len(identifiers) == len(runtime.store.list("attachment")) == count
        assert len(list(attachments.directory.iterdir())) == count and attachments.reserved == 0
        run = runtime.store.get("run", started["run_id"])
        assert run is not None and run["status"] == "failed"
    finally:
        await runtime.close()
