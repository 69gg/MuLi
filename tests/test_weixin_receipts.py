from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import Harness, profile, terminal

from muli.channels import ChannelError, ChannelsConfig, IncomingMessage
from muli.channels.weixin import Weixin
from muli.store import now


async def receive_context(harness: Harness) -> None:
    await harness.manager.receive(
        "weixin",
        IncomingMessage("context", "7", "7", "你好", context_token="PRIVATE_CONTEXT"),
    )


@pytest.mark.parametrize("field", ["ret", "errcode"])
@pytest.mark.parametrize(
    "raw_value",
    [
        pytest.param(b"null", id="null"),
        pytest.param(b"false", id="false"),
        pytest.param(b"true", id="true"),
        pytest.param(b'""', id="empty-string"),
        pytest.param(b'"0"', id="numeric-string"),
        pytest.param(b"[]", id="array"),
        pytest.param(b"{}", id="object"),
        pytest.param(b"NaN", id="nan"),
        pytest.param(b"Infinity", id="infinity"),
        pytest.param(b"-Infinity", id="negative-infinity"),
        pytest.param(b"1e9999", id="overflowing-number"),
    ],
)
async def test_malformed_send_code_is_unknown_and_never_automatically_retried(
    tmp_path: Path, field: str, raw_value: bytes
) -> None:
    sends: list[dict[str, Any]] = []
    accepted = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getupdates"):
            await asyncio.Event().wait()
        assert request.url.path.endswith("sendmessage")
        message = json.loads(request.content)["msg"]
        sends.append(message)
        if accepted or message["client_id"] != sends[0]["client_id"]:
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            content=(
                b'{"'
                + field.encode("ascii")
                + b'":'
                + raw_value
                + b',"errmsg":"PRIVATE_UPSTREAM_BODY"}'
            ),
        )

    harness = Harness(
        tmp_path,
        ChannelsConfig(
            weixin=profile(account_id="account", user_id="bot"), retry_backoff_seconds=0
        ),
        handler,
    )
    try:
        await receive_context(harness)
        await harness.manager.start()
        item = await harness.manager.enqueue("weixin", "7", "回复", event_id="original")
        result = await terminal(harness.manager, item["id"])
        assert result["state"] == "unknown"
        assert result["next_part"] == 0 and result["receipts"] == []
        assert result["attempts"] == 1 and len(sends) == 1
        assert "PRIVATE" not in str(result)
        with pytest.raises(ChannelError, match="明确允许"):
            await harness.manager.retry(item["id"])
        duplicate = await harness.manager.enqueue("weixin", "7", "回复", event_id="original")
        assert duplicate["state"] == "unknown"
        assert duplicate["id"] == item["id"] and len(sends) == 1

        # Let the real worker finish another delivery: an unknown predecessor
        # must stay out of the automatic work queue, even with zero backoff.
        barrier = await harness.manager.enqueue("weixin", "7", "下一条", event_id="barrier")
        assert (await terminal(harness.manager, barrier["id"]))["state"] == "sent"
        assert len(sends) == 2 and sends[0]["client_id"] != sends[1]["client_id"]

        accepted = True
        await harness.manager.retry(item["id"], allow_unknown=True)
        retried = await terminal(harness.manager, item["id"])
        assert retried["state"] == "sent" and retried["next_part"] == 1
        assert retried["receipts"] == [sends[0]["client_id"]]
        assert len(sends) == 3 and sends[2]["client_id"] == sends[0]["client_id"]
    finally:
        await harness.close()


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({}, id="official-empty-response"),
        pytest.param({"ret": 0}, id="ret-zero"),
        pytest.param({"ret": 0.0}, id="ret-float-zero"),
        pytest.param({"errcode": 0}, id="errcode-zero"),
        pytest.param({"errcode": 0.0}, id="errcode-float-zero"),
        pytest.param({"ret": 0, "errcode": 0}, id="both-zero"),
    ],
)
async def test_optional_numeric_success_preserves_official_send_contract(
    tmp_path: Path, response: dict[str, Any]
) -> None:
    sends: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getupdates"):
            await asyncio.Event().wait()
        assert request.url.path.endswith("sendmessage")
        sends.append(json.loads(request.content)["msg"])
        return httpx.Response(200, json=response)

    harness = Harness(
        tmp_path, ChannelsConfig(weixin=profile(account_id="account", user_id="bot")), handler
    )
    try:
        await receive_context(harness)
        item = await harness.manager.enqueue("weixin", "7", "回复", event_id="success")
        await harness.manager.start()
        result = await terminal(harness.manager, item["id"])
        assert result["state"] == "sent" and result["next_part"] == 1
        assert len(sends) == 1 and result["receipts"] == [sends[0]["client_id"]]
        duplicate = await harness.manager.enqueue("weixin", "7", "回复", event_id="success")
        assert duplicate["id"] == result["id"] and duplicate["state"] == "sent"
        assert len(sends) == 1
    finally:
        await harness.close()


@pytest.mark.parametrize(
    ("response", "stale"),
    [
        pytest.param({"ret": 1}, False, id="ret-nonzero"),
        pytest.param({"ret": 1.5}, False, id="ret-float-nonzero"),
        pytest.param({"errcode": 1}, False, id="errcode-nonzero"),
        pytest.param({"ret": 0, "errcode": 1}, False, id="ret-zero-errcode-nonzero"),
        pytest.param({"ret": -14}, True, id="ret-stale"),
        pytest.param({"ret": 0, "errcode": -14}, True, id="errcode-stale"),
        pytest.param({"ret": 10**400}, False, id="large-integer"),
    ],
)
async def test_explicit_numeric_rejection_remains_retryable_without_receipt(
    tmp_path: Path, response: dict[str, Any], stale: bool
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("sendmessage")
        return httpx.Response(200, json={**response, "errmsg": "PRIVATE_UPSTREAM_BODY"})

    harness = Harness(
        tmp_path,
        ChannelsConfig(
            weixin=profile(account_id="account", user_id="bot"), retry_backoff_seconds=0
        ),
        handler,
    )
    try:
        await receive_context(harness)
        item = await harness.manager.enqueue("weixin", "7", "回复")
        before = now()
        # One real dispatch lets us inspect -14's one-hour retry deadline
        # without a worker waiting for that deadline or automatically retrying.
        await harness.manager._send_item(item)
        result = harness.manager.delivery(item["id"])
        assert result is not None and result["state"] == "retry"
        assert result["next_part"] == 0 and result["receipts"] == []
        assert result["attempts"] == 1 and "PRIVATE" not in str(result)
        if stale:
            assert result["next_attempt"] >= before + 3600
            assert result["last_error"] == "iLink 令牌已失效"
        else:
            assert result["last_error"] == "iLink 拒绝请求"
    finally:
        await harness.close()


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({"ret": 0, "errcode": False}, id="good-ret-bad-errcode"),
        pytest.param({"ret": False, "errcode": 0}, id="bad-ret-good-errcode"),
        pytest.param({"ret": -14, "errcode": False}, id="rejection-with-bad-errcode"),
        pytest.param({"ret": False, "errcode": -14}, id="bad-ret-with-rejection"),
    ],
)
async def test_other_code_cannot_repair_a_malformed_send_field(
    response: dict[str, Any],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = Weixin(profile(account_id="account", user_id="bot"), client)
        with pytest.raises(ChannelError, match="发送回执格式错误") as caught:
            await adapter.send(
                {"target": "7", "context_token": "PRIVATE_CONTEXT"},
                "回复",
                None,
                key="delivery",
                sequence=0,
            )
        assert caught.value.uncertain is True and caught.value.retryable is False
        assert adapter.delivery_started is True


async def test_later_unknown_part_keeps_successful_checkpoint_and_client_id(
    tmp_path: Path,
) -> None:
    sends: list[dict[str, Any]] = []
    accepted = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getupdates"):
            await asyncio.Event().wait()
        assert request.url.path.endswith("sendmessage")
        message = json.loads(request.content)["msg"]
        sends.append(message)
        response = {"ret": 0} if accepted or len(sends) == 1 else {"ret": []}
        return httpx.Response(200, json=response)

    harness = Harness(
        tmp_path,
        ChannelsConfig(weixin=profile(account_id="account", user_id="bot", max_message_chars=1)),
        handler,
    )
    try:
        await receive_context(harness)
        item = await harness.manager.enqueue("weixin", "7", "两段", event_id="parts")
        await harness.manager.start()
        result = await terminal(harness.manager, item["id"])
        assert result["state"] == "unknown" and result["next_part"] == 1
        assert len(sends) == 2 and result["receipts"] == [sends[0]["client_id"]]
        accepted = True
        await harness.manager.retry(item["id"], allow_unknown=True)
        result = await terminal(harness.manager, item["id"])
        assert result["state"] == "sent" and result["next_part"] == 2
        assert result["receipts"] == [sends[0]["client_id"], sends[1]["client_id"]]
        assert len(sends) == 3 and sends[2]["client_id"] == sends[1]["client_id"]
        assert [message["item_list"][0]["text_item"]["text"] for message in sends] == [
            "两",
            "段",
            "段",
        ]
    finally:
        await harness.close()


@pytest.mark.parametrize("method", ["getupdates", "getuploadurl"])
async def test_other_ilink_methods_keep_their_existing_response_handling(method: str) -> None:
    response = {"ret": None, "errcode": [], "msgs": [], "upload_param": "UPLOAD"}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(method)
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = Weixin(profile(account_id="account", user_id="bot"), client)
        assert await adapter.call(method, {}) == response
        assert adapter.delivery_started is False
