"""Native selection survives real sends, reopen, ingress and registered tool pages.

Only platform transport and model responses are controlled. This positive model
uses the new reference contract; the frozen legacy clarification model is a
separate product-red experiment and is not claimed to become green unchanged.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import terminal
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.channels.adapters import Telegram
from muli.providers import Message, ModelTurn, StreamEvent
from muli.reply_origin import ReplyOrigin
from muli.runtime import Runtime
from muli.server import create_app

SEND_PREFIX = "请把以下 JSON 中的方案依次单独发到指定主人私聊：\n"
READ_PREFIX = "请只读找回这个已保存引用，再写一周草稿：\n"
ORIGIN_PREFIX = "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
INCOMING = "把这个改为一周，只给草稿"
SEND_FINAL = "方案已提交显式发送；结果以实际平台确认记录为准。"
REFERENCE_UNAVAILABLE = "无法核验这次引用的已确认正文，请主人补充准确内容；本轮没有外发。"


def native_update(referenced: dict[str, Any]) -> dict[str, Any]:
    return {
        "update_id": 7001,
        "message": {
            "message_id": 7002,
            "date": 1800000001,
            "from": {"id": 7, "is_bot": False, "first_name": "Owner"},
            "chat": {"id": 7, "type": "private"},
            "text": INCOMING,
            "reply_to_message": dict(referenced),
        },
    }


class SendRequestedPlans(FakeClient):
    """The only plan source is the actual Web request supplied by Runtime."""

    def __init__(self) -> None:
        super().__init__([], [])
        self.rounds = 0
        self.envelope: dict[str, Any] = {}
        self.delivery_ids: list[str] = []

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        self.rounds += 1
        assert "channel_send" in {item["function"]["name"] for item in tools}
        if self.rounds == 1:
            request = next(
                item
                for item in reversed(messages)
                if item.role == "user" and item.content.startswith(SEND_PREFIX)
            )
            self.envelope = json.loads(request.content[len(SEND_PREFIX) :])
        else:
            receipt = tool_result(messages, f"send-plan-{self.rounds - 2}")
            assert "error" not in receipt, receipt
            assert receipt["name"] == self.envelope["channel"]
            self.delivery_ids.append(receipt["id"])
        index = self.rounds - 1
        if index < len(self.envelope["plans"]):
            turn = ModelTurn(
                tool_calls=[
                    call(
                        f"send-plan-{index}",
                        "channel_send",
                        channel=self.envelope["channel"],
                        target=self.envelope["target"],
                        text=self.envelope["plans"][index],
                        idempotency_key=f"native-reference-plan-{index}",
                    )
                ]
            )
        else:
            assert self.rounds == len(self.envelope["plans"]) + 1
            turn = ModelTurn(content=SEND_FINAL)
        if turn.content:
            yield StreamEvent("text_delta", text=turn.content)
        yield StreamEvent("done", turn=turn)


class ReadSelectedReference(FakeClient):
    """Select nothing from prior prompts, fixtures, a store handle or platform IDs.

    The host input ID comes from the current model request. All draft fields come
    from the complete actual reference tool body, after its final UTF-8 page.
    """

    def __init__(self) -> None:
        super().__init__([], [])
        self.rounds = 0
        self.input_id = ""
        self.origin: dict[str, Any] = {}
        self.owner_content = ""
        self.pending: dict[str, Any] = {}
        self.last_call_id = ""
        self.reads: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.chunks: list[str] = []
        self.body = ""
        self.error: dict[str, Any] | None = None
        self.final = ""

    def _next_read(self, *, offset_bytes: int = 0, revision: str | None = None) -> ModelTurn:
        self.pending = {
            "section": "reference",
            "input_id": self.input_id,
            "offset_bytes": offset_bytes,
        }
        if revision is not None:
            self.pending["revision"] = revision
        self.last_call_id = f"reference-page-{len(self.reads)}"
        return ModelTurn(tool_calls=[call(self.last_call_id, "channel_history", **self.pending)])

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        self.rounds += 1
        history = next(item for item in tools if item["function"]["name"] == "channel_history")
        assert "reference" in history["function"]["parameters"]["properties"]["section"]["enum"]
        if self.rounds == 1:
            owner = next(item for item in reversed(messages) if item.role == "user")
            self.owner_content = owner.content
            if owner.content.startswith(READ_PREFIX):
                request = json.loads(owner.content[len(READ_PREFIX) :])
                self.input_id = request["input_id"]
            else:
                header, separator, _body = owner.content.partition("\n")
                assert separator and header.startswith(ORIGIN_PREFIX)
                self.origin = json.loads(header[len(ORIGIN_PREFIX) :])
                assert self.origin["channel_reference"]["status"] == "identified"
                self.input_id = self.origin["input_id"]
                assert self.input_id == owner.source_message_id
            turn = self._next_read()
        else:
            page = tool_result(messages, self.last_call_id)
            self.reads.append((dict(self.pending), page))
            if "error" in page:
                self.error = page
                turn = ModelTurn(content=REFERENCE_UNAVAILABLE)
            else:
                assert page["section"] == "reference" and page["input_id"] == self.input_id
                assert page["evidence"] == "confirmed_text_part"
                assert page["scope"] == "single_delivery_part" and page["attachment_ids"] == []
                assert page["offset_bytes"] == self.pending["offset_bytes"]
                assert page["length_bytes"] == len(page["content"].encode("utf-8"))
                if "revision" in self.pending:
                    assert page["revision"] == self.pending["revision"]
                self.chunks.append(page["content"])
                if page["has_more"]:
                    assert page["next_offset_bytes"] > page["offset_bytes"]
                    assert len(self.reads) < 12
                    turn = self._next_read(
                        offset_bytes=page["next_offset_bytes"], revision=page["revision"]
                    )
                else:
                    self.body = "".join(self.chunks)
                    plan = json.loads(self.body)
                    daily = plan["daily"]
                    turn = ModelTurn(
                        content=(
                            f"一周草稿：将{plan['topic']}安排为 7 天，每天用{daily['pen']}"
                            f"{daily['method']} {daily['minutes']} 分钟；{daily['constraint']}。"
                            "只提供草稿，本轮没有外发。"
                        )
                    )
        if turn.content:
            self.final = turn.content
            yield StreamEvent("text_delta", text=turn.content)
        yield StreamEvent("done", turn=turn)


def long_plan(*, pen: str, minutes: int, method: str, constraint: str) -> str:
    # Put distinguishing fields after the long prefix: the final draft needs the
    # last actual tool page, rather than an early-page preview of either plan.
    result = json.dumps(
        {
            "notes": "离线资料🙂与复盘说明。\n" * 180,
            "duration_days": 14,
            "topic": "物理错题整理",
            "daily": {
                "pen": pen,
                "minutes": minutes,
                "method": method,
                "constraint": constraint,
            },
        },
        ensure_ascii=False,
    )
    assert 1800 < len(result) < 4000
    return result


async def settled_reply(runtime: Runtime, reader: ReadSelectedReference) -> str:
    await eventually(runtime, lambda: bool(reader.final))
    run_id = runtime.run_ids.get(runtime.main_session()["id"])
    if run_id is not None:
        assert await asyncio.wait_for(runtime.wait(run_id), 4) == reader.final
    return reader.final


def assert_full_reference_read(
    reader: ReadSelectedReference,
    *,
    input_id: str,
    delivery_id: str,
    platform_message_id: str,
    expected_body: str,
) -> None:
    assert reader.error is None and reader.body == expected_body
    assert reader.input_id == input_id and len(reader.reads) > 1
    assert reader.reads[0][0]["offset_bytes"] == 0
    assert reader.reads[-1][1]["has_more"] is False
    assert len({page["revision"] for _args, page in reader.reads}) == 1
    assert "".join(page["content"] for _args, page in reader.reads) == expected_body
    for index, (arguments, page) in enumerate(reader.reads):
        assert set(arguments) <= {"section", "input_id", "offset_bytes", "revision"}
        assert arguments["section"] == page["section"] == "reference"
        assert arguments["input_id"] == page["input_id"] == input_id
        assert page["delivery_id"] == delivery_id and page["part"] == 0
        assert page["platform_message_id"] == platform_message_id and page["state"] == "sent"
        assert page["confirmed_parts"] == page["total_parts"] == 1
        assert page["next_offset_bytes"] == page["offset_bytes"] + page["length_bytes"]
        if index:
            previous = reader.reads[index - 1][1]
            assert arguments["offset_bytes"] == previous["next_offset_bytes"]
            assert arguments["revision"] == previous["revision"]
    assert reader.reads[-1][1]["next_offset_bytes"] == len(expected_body.encode("utf-8"))


@pytest.mark.parametrize("selected_index", [0, 1], ids=["quote-earlier-a", "quote-later-b"])
async def test_native_reference_uses_selected_confirmed_long_part_across_reopens_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected_index: int
) -> None:
    async with asyncio.timeout(36):
        await reference_journey(
            tmp_path, monkeypatch, selected_index=selected_index, confirmed=True
        )


async def test_native_reference_cannot_read_an_unknown_send_without_platform_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with asyncio.timeout(28):
        await reference_journey(tmp_path, monkeypatch, selected_index=0, confirmed=False)


async def reference_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected_index: int,
    confirmed: bool,
) -> None:
    plans = [
        long_plan(pen="蓝笔", minutes=20, method="复盘", constraint="只整理老师标记的错题"),
        long_plan(pen="红笔", minutes=40, method="默写", constraint="必须保留原始解题过程"),
    ]
    if not confirmed:
        plans = plans[:1]
    web_input = SEND_PREFIX + json.dumps(
        {"channel": "telegram", "target": "7", "plans": plans}, ensure_ascii=False
    )
    phase = 0
    incoming_supplied = False
    hold_poll = asyncio.Event()
    platform_messages: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    poll_phases: list[int] = []

    async def platform(request: httpx.Request) -> httpx.Response:
        nonlocal incoming_supplied
        assert request.method == "POST" and request.url.host == "telegram.invalid"
        body = json.loads(request.content)
        if request.url.path == "/bot123:FAKE_REFERENCE_TOKEN/getUpdates":
            poll_phases.append(phase)
            if phase == 1 and not incoming_supplied:
                incoming_supplied = True
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": [native_update(platform_messages[selected_index])],
                    },
                )
            await hold_poll.wait()
            raise AssertionError("正常应用关闭必须取消真实 Telegram 长轮询")
        assert request.url.path == "/bot123:FAKE_REFERENCE_TOKEN/sendMessage"
        sends.append(body)
        assert phase == 0 and body["chat_id"] == "7"
        message = {
            "message_id": 6000 + len(sends),
            "date": 1800000000,
            "from": {"id": 123, "is_bot": True, "first_name": "MuLi"},
            "chat": {"id": 7, "type": "private"},
            "text": body["text"],
        }
        platform_messages.append(message)
        if not confirmed:
            # The simulated platform accepted a message, but its response was
            # lost. The real adapter/manager must preserve uncertain delivery.
            raise httpx.ReadTimeout("offline platform acknowledgement was lost", request=request)
        return httpx.Response(200, json={"ok": True, "result": message})

    transport = httpx.MockTransport(platform)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "extensions": {"watch_enabled": False},
            "limits": {
                "max_context_chars": 32000,
                "max_output_chars": 6000,
                "max_pending_runs": 3,
            },
            "channels": {
                "telegram": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://telegram.invalid",
                    "token": "123:FAKE_REFERENCE_TOKEN",
                    "owner_ids": ["7"],
                }
            },
        }
    )
    await runtime.reconfigure()
    sender = SendRequestedPlans()
    runtime.client_factory = lambda _config: sender
    main_id = runtime.main_session()["id"]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    deliveries: list[dict[str, Any]] = []
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        changed = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
        assert changed.status_code == 200
        accepted = await web.post(
            f"/api/sessions/{main_id}/messages", json={"content": web_input, "mode": "yolo"}
        )
        assert accepted.status_code == 200
        assert await asyncio.wait_for(runtime.wait(accepted.json()["run_id"]), 4) == SEND_FINAL
        manager = app.state.channel_application.manager
        assert len(sender.delivery_ids) == len(plans)
        for index, delivery_id in enumerate(sender.delivery_ids):
            delivery = await terminal(manager, delivery_id)
            assert delivery["state"] == ("sent" if confirmed else "unknown")
            assert delivery["receipts"] == ([str(6001 + index)] if confirmed else [])
            assert delivery["next_part"] == (1 if confirmed else 0)
            assert delivery["payload"]["parts"] == [{"text": plans[index]}]
            deliveries.append(delivery)
        assert [item["text"] for item in sends] == plans
        assert len(manager.deliveries()) == len(plans) and incoming_supplied is False

    phase = 1
    reopened, _ = make_runtime(tmp_path, [])
    # Sending first used a budget large enough for its real enqueue receipt.
    # Lower the read budget now so the same saved single part needs many pages.
    reopened.config.update({"limits": {"max_output_chars": 1800}})
    reader = ReadSelectedReference()
    reopened.client_factory = lambda _config: reader
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    input_id = ""
    canonical_raw = ""
    first_final = ""
    async with reopened_app.router.lifespan_context(reopened_app):
        assert reopened.main_session()["id"] == main_id
        manager = reopened_app.state.channel_application.manager
        adapter = manager.adapters["telegram"]
        assert isinstance(adapter, Telegram)
        if confirmed:
            normalized = [
                adapter.webhook(native_update(message))[0] for message in platform_messages
            ]
            assert normalized[0].channel_reference != normalized[1].channel_reference
            assert replace(normalized[0], channel_reference=None) == replace(
                normalized[1], channel_reference=None
            )
        first_final = await settled_reply(reopened, reader)
        owner_inputs = [item for item in reopened.store.messages(main_id) if item["role"] == "user"]
        assert [item["content"] for item in owner_inputs] == [web_input, INCOMING]
        incoming = owner_inputs[-1]
        input_id = incoming["id"]
        row = reopened.store.db.execute(
            "SELECT payload,state FROM ingress WHERE id=?", (input_id,)
        ).fetchone()
        assert row is not None and row["state"] == "delivered"
        canonical_raw = row["payload"]
        canonical = json.loads(canonical_raw)
        marker = {
            "version": 1,
            "status": "identified",
            "message_id": str(platform_messages[selected_index]["message_id"]),
        }
        assert canonical["channel_reference"] == incoming["channel_reference"] == marker
        assert canonical["content"] == incoming["content"] == INCOMING
        assert canonical["session_id"] == incoming["session_id"] == main_id
        assert canonical["principal"] == incoming["principal"] == "owner"
        assert canonical["route"] == incoming["route"]
        assert canonical["route"]["reply_to"] == "7002"
        assert canonical["owner_text"] == {"version": 1, "kind": "channel", "chars": len(INCOMING)}
        assert "channel_reference" not in canonical["route"]
        origin = ReplyOrigin.from_input(canonical)
        assert "channel_reference" not in origin.dump() and origin.input_id == input_id
        assert reader.input_id == reader.origin["input_id"] == input_id
        assert reader.origin["channel_reference"] == marker
        assert reader.owner_content.endswith(INCOMING)
        assert reader.origin["channel"] == "telegram" and reader.origin["account_id"] == "123"
        assert reader.origin["sender_id"] == reader.origin["target"] == "7"
        assert reader.origin["kind"] == "private"
        assert (
            reopened.store.db.execute(
                "SELECT count(*) FROM ingress WHERE id=?", (input_id,)
            ).fetchone()[0]
            == 1
        )
        assert reopened.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == 1
        assert {item["id"] for item in manager.deliveries()} == set(sender.delivery_ids)
        assert len(sends) == len(plans) and incoming_supplied and set(poll_phases) == {0, 1}
        if confirmed:
            selected_body = plans[selected_index]
            assert_full_reference_read(
                reader,
                input_id=input_id,
                delivery_id=deliveries[selected_index]["id"],
                platform_message_id=marker["message_id"],
                expected_body=selected_body,
            )
            selected = json.loads(selected_body)["daily"]
            other = json.loads(plans[1 - selected_index])["daily"]
            assert "一周" in first_final and "7 天" in first_final
            assert selected["pen"] in first_final and f"{selected['minutes']} 分钟" in first_final
            assert selected["method"] in first_final and selected["constraint"] in first_final
            assert other["pen"] not in first_final and f"{other['minutes']} 分钟" not in first_final
            assert other["constraint"] not in first_final
        else:
            assert first_final == REFERENCE_UNAVAILABLE and reader.body == ""
            assert len(reader.reads) == 1 and reader.error is not None
            assert reader.error["code"] == "channel_reference_not_found"
            assert manager.delivery(sender.delivery_ids[0])["receipts"] == []

    # A later genuine Web owner round must recover this same delivered input;
    # the model receives its input ID in the real prompt, never in its fixture.
    phase = 2
    final_runtime, _ = make_runtime(tmp_path, [])
    rereader = ReadSelectedReference()
    final_runtime.client_factory = lambda _config: rereader
    final_app = create_app(final_runtime.config, final_runtime.data_dir, final_runtime)
    async with (
        final_app.router.lifespan_context(final_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=final_app), base_url="http://localhost:8765"
        ) as web,
    ):
        assert final_runtime.main_session()["id"] == main_id
        archive = final_runtime.store.get("message", input_id)
        assert archive is not None and archive["content"] == INCOMING
        assert archive["channel_reference"] == json.loads(canonical_raw)["channel_reference"]
        assert archive["route"]["reply_to"] == "7002"
        assert (
            final_runtime.store.db.execute(
                "SELECT payload FROM ingress WHERE id=?", (input_id,)
            ).fetchone()[0]
            == canonical_raw
        )
        recheck = READ_PREFIX + json.dumps({"input_id": input_id}, ensure_ascii=False)
        accepted = await web.post(f"/api/sessions/{main_id}/messages", json={"content": recheck})
        assert accepted.status_code == 200
        assert (
            await asyncio.wait_for(final_runtime.wait(accepted.json()["run_id"]), 4) == first_final
        )
        assert rereader.final == first_final and rereader.input_id == input_id
        if confirmed:
            assert_full_reference_read(
                rereader,
                input_id=input_id,
                delivery_id=deliveries[selected_index]["id"],
                platform_message_id=str(platform_messages[selected_index]["message_id"]),
                expected_body=plans[selected_index],
            )
        else:
            assert rereader.error is not None
            assert rereader.error["code"] == "channel_reference_not_found"
        manager = final_app.state.channel_application.manager
        assert {item["id"] for item in manager.deliveries()} == set(sender.delivery_ids)
        assert len(sends) == len(plans)
        assert (
            sum(
                item["role"] == "user" and item["content"] == INCOMING
                for item in final_runtime.store.messages(main_id)
            )
            == 1
        )
        assert (
            final_runtime.store.db.execute(
                "SELECT count(*) FROM objects WHERE kind='session' "
                "AND json_extract(data,'$.kind')='main'"
            ).fetchone()[0]
            == 1
        )
