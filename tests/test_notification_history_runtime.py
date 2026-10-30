from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_history_find_runtime import preserved_reply
from test_history_scope_runtime import configured, execution, run_tool, settled
from test_multichannel_ownership_boundaries import finished
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call, tool_result

from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"通知核查不应发起外部 HTTP：{request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)


def save(runtime: Runtime, identifier: str, created: int, **fields: Any) -> dict[str, Any]:
    return runtime.store.put(
        "notification",
        {
            "id": identifier,
            "title": identifier,
            "content": "保存正文",
            "source": "assistant",
            "read": False,
            "created_at": created,
            **fields,
        },
    )


async def test_real_owner_tool_round_finds_old_unread_and_preserves_raw_json_pages(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    rounds = 0
    phase = "directory"
    pages: list[dict[str, Any]] = []
    raw_pages: list[dict[str, Any]] = []
    try:
        runtime.config.update({"persona": {"prompt": "测试"}, "limits": {"max_output_chars": 1000}})
        await runtime.reconfigure()
        runtime.store.prepare_notification_history()
        original = save(
            runtime,
            "old-unread",
            1,
            content="a" * 4500 + "准确来源🙂",
            source="reminder",
            reminder_id="gone-reminder",
            routing={"channel": "saved-preference-only"},
        )
        for index in range(205):
            save(runtime, f"new-{index:03d}", index + 2, read=True)
        assert original["id"] not in {item["id"] for item in runtime.store.list("notification")}
        assert runtime.tools["notification_read"].risk == "read"
        assert runtime.tools["notification_read"].preserve_output is True

        class Reader(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds, phase
                schemas = {tool["function"]["name"]: tool["function"] for tool in tools}
                assert "notification_read" in schemas
                assert set(schemas["notification_read"]["parameters"]["properties"]) == {
                    "id",
                    "status",
                    "cursor",
                    "limit",
                    "offset_bytes",
                    "length_bytes",
                    "revision",
                }
                arguments: dict[str, Any] = {"status": "unread"}
                if rounds:
                    previous = preserved_reply(messages, f"notification-{rounds}")
                    assert len(json.dumps(previous, ensure_ascii=False)) <= 1000
                    if phase == "directory":
                        pages.append(previous)
                        if any(item["id"] == original["id"] for item in previous["items"]):
                            assert previous["has_more"] is False
                            phase = "first"
                            arguments = {"id": original["id"]}
                        else:
                            assert previous["items"] == [] and previous["has_more"]
                            arguments["cursor"] = previous["next_cursor"]
                    elif phase in {"first", "second"}:
                        raw_pages.append(previous)
                        assert previous["format"] == "json" and previous["has_more"]
                        arguments = {
                            "id": original["id"],
                            "offset_bytes": previous["next_offset_bytes"],
                            "revision": previous["revision"] if phase == "first" else "stale",
                        }
                        phase = "second" if phase == "first" else "stale"
                    elif phase == "stale":
                        assert previous["code"] == "evidence_changed"
                        phase = "mixed"
                        arguments = {"id": original["id"], "status": "unread"}
                    else:
                        assert phase == "mixed" and previous["code"] == "invalid_arguments"
                        yield StreamEvent(
                            "done", turn=ModelTurn(content="只读核查已保存通知，未恢复旧任务")
                        )
                        return
                rounds += 1
                assert rounds <= 8
                yield StreamEvent(
                    "done",
                    turn=ModelTurn(
                        tool_calls=[
                            ToolCall(
                                f"notification-{rounds}", "notification_read", json.dumps(arguments)
                            )
                        ]
                    ),
                )

        runtime.client_factory = lambda _: Reader([], [])  # type: ignore[assignment]
        submitted = await runtime.submit("找回旧通知，核对保存原文，不执行。", mode="yolo")
        assert await asyncio.wait_for(runtime.wait(submitted["run_id"]), 5) == (
            "只读核查已保存通知，未恢复旧任务"
        )
        assert len(pages) == 3 and len(raw_pages) == 2
        assert raw_pages[1]["offset_bytes"] == raw_pages[0]["next_offset_bytes"]
        assert raw_pages[0]["revision"] == raw_pages[1]["revision"]
        assert runtime.store.get("notification", original["id"]) == original
        receipts = [
            item
            for item in runtime.store.messages(runtime.main_session()["id"])
            if item["role"] == "tool"
        ]
        assert len(receipts) == rounds
        assert all(item["preserve_content"] for item in receipts)
        assert not runtime._selected_replans and not runtime.team._tasks
    finally:
        await runtime.close()


async def test_new_reader_is_hidden_and_denied_for_independent_child_and_consult(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    worker_rounds = consult_rounds = 0
    denied: dict[str, dict[str, Any]] = {}
    try:
        original = save(runtime, "private-global", 1, content="只给主人的跨来源原文")
        allowed = await run_tool(runtime, "notification_read", {"id": original["id"]})
        assert json.loads(allowed["content"]) == original
        independent = runtime.new_session(mode="yolo")["id"]
        rejected = await run_tool(
            runtime, "notification_read", {"id": original["id"]}, session_id=independent
        )
        assert "error" in rejected and "content" not in rejected
        main_id = runtime.main_session()["id"]

        class Probe(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal worker_rounds, consult_rounds
                functions = {tool["function"]["name"] for tool in tools}
                caller, current_run = execution(runtime)
                # PreparedRequest is installed only at handler entry, not in
                # provider.stream. Classify the real consultation by its host
                # archive metadata, independent of which model is configured.
                is_consult = any(
                    item["role"] == "user"
                    and item.get("run_id") == current_run
                    and str(item.get("principal", "")).startswith("agent:")
                    and item.get("source")
                    == "team-consult:" + item["principal"].removeprefix("agent:")
                    for item in runtime.store.messages(caller)
                )
                if is_consult:
                    assert caller == main_id
                    consult_rounds += 1
                    assert "notification_read" not in functions
                    if consult_rounds == 1:
                        turn = ModelTurn(
                            tool_calls=[
                                call("consult-read", "notification_read", id=original["id"])
                            ]
                        )
                    else:
                        assert consult_rounds == 2
                        denied["consult"] = tool_result(messages, "consult-read")
                        turn = ModelTurn(content="咨询没有主人授权，未读全局通知")
                elif caller != main_id:
                    worker_rounds += 1
                    assert "notification_read" not in functions
                    if worker_rounds == 1:
                        turn = ModelTurn(
                            tool_calls=[call("child-read", "notification_read", id=original["id"])]
                        )
                    elif worker_rounds == 2:
                        denied["child"] = tool_result(messages, "child-read")
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "consult",
                                    "team_consult",
                                    question="只核对通知权限",
                                    wait_seconds=2,
                                )
                            ]
                        )
                    else:
                        assert worker_rounds == 3
                        assert "error" not in tool_result(messages, "consult")
                        turn = ModelTurn(content="子任务只核权限，没有读取全局通知")
                else:
                    turn = ModelTurn(content="权限核查报告已收到")
                yield StreamEvent("done", turn=turn)

        def factory(config: ModelConfig) -> Probe:
            return Probe([], [])

        runtime.client_factory = factory  # type: ignore[assignment]
        child = await runtime.team.spawn("通知权限核查", "只做只读权限反例", parent_id=main_id)
        await runtime.team.wait([child["id"]], timeout=3)
        await finished(runtime, child["id"], 1)
        await settled(runtime)
        assert worker_rounds == 3 and consult_rounds == 2
        assert set(denied) == {"child", "consult"}
        assert all("error" in result and "content" not in result for result in denied.values())
        with pytest.raises(PermissionError):
            await runtime.tools["notification_read"].handler({"id": original["id"]})
        assert runtime.store.get("notification", original["id"]) == original
    finally:
        await runtime.close()
