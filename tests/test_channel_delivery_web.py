"""The WebUI reads actual saved deliveries without sending or retrying them."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_channel_history_policy import delivery
from test_runtime import make_runtime

from muli.channel_history import CHANNEL_HISTORY_PAGE_LIMIT, ChannelHistory
from muli.maintenance_api import passive_request_allowed, request_has_effect
from muli.runtime import Runtime
from muli.server import create_app


async def test_saved_unknown_delivery_is_readable_through_the_web_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def no_network(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append("network")
        raise AssertionError("读取本地发送记录不能访问真实平台")

    async def no_send_or_retry(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("send_or_retry")
        raise AssertionError("读取本地发送记录不能发送或重试")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "saved-bot",
                    "base_url": "https://onebot.invalid",
                    "owner_ids": ["7"],
                }
            }
        }
    )
    app = create_app(runtime.config, runtime.data_dir, runtime)
    manager = app.state.channel_application.manager
    monkeypatch.setattr(manager, "enqueue", no_send_or_retry)
    monkeypatch.setattr(manager, "retry", no_send_or_retry)
    try:
        main_id = runtime.main_session()["id"]
        runtime.store.add_message(main_id, "assistant", "普通网页答复不属于平台发送记录")
        text = "实际保存的发送正文：中文与🙂。\n" * 48
        identifier = delivery(
            runtime.store,
            account="saved-bot",
            state="unknown",
            confirmed=1,
            parts=[{"text": text}, {"attachment_id": "saved-file-reference"}],
        )
        existing = ChannelHistory(runtime.store).read_delivery(
            "onebot",
            "saved-bot",
            "7",
            main_id,
            delivery_id=identifier,
            max_output_chars=runtime.config.settings.limits.max_output_chars,
        )
        assert existing["content"] == text
        assert existing["state"] == "unknown" and existing["confirmed_parts"] == 1
        before = runtime.store.db.total_changes
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as web:
            page = await web.get(
                "/api/channels/deliveries", params={"channel": "onebot", "owner_id": "7"}
            )
            body = await web.get(
                f"/api/channels/deliveries/{identifier}/content",
                params={"channel": "onebot", "owner_id": "7"},
            )
        assert calls == [] and seen == []
        assert runtime.store.db.total_changes == before
        assert page.status_code == 200
        assert [item["id"] for item in page.json()["items"]] == [identifier]
        assert page.json()["items"][0]["attachment_ids"] == ["saved-file-reference"]
        assert body.status_code == 200
        assert body.json()["delivery_id"] == identifier
        assert body.json()["content"] == text
        assert body.json()["state"] == "unknown" and body.json()["confirmed_parts"] == 1
    finally:
        await app.state.close()


@pytest.fixture
async def channel_web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient]]:
    calls: list[str] = []

    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        calls.append("external_or_send")
        raise AssertionError("网页发送记录只允许读取本地证据")

    def no_model(*args: Any, **kwargs: Any) -> NoReturn:
        calls.append("model")
        raise AssertionError("网页发送记录不能调用模型")

    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "saved-bot",
                    "base_url": "https://onebot.invalid",
                    "owner_ids": ["7", "8"],
                }
            }
        }
    )
    app = create_app(runtime.config, runtime.data_dir, runtime)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(runtime, "client_factory", no_model)
    monkeypatch.setattr(app.state.channel_application.manager, "enqueue", forbidden)
    monkeypatch.setattr(app.state.channel_application.manager, "retry", forbidden)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as web:
            yield runtime, web
        assert calls == []
    finally:
        await app.state.close()


async def test_web_delivery_projection_keeps_current_scope_and_existing_raw_summary(
    channel_web: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, web = channel_web
    main_id = runtime.main_session()["id"]
    runtime.config.update({"disabled_tools": ["channel_history"]})
    identifier = delivery(runtime.store, account="saved-bot")
    outside = [
        delivery(runtime.store, account="saved-bot", owner="8"),
        delivery(runtime.store, account="retired-bot"),
        delivery(runtime.store, channel="weixin", account="saved-bot"),
    ]
    params = {"channel": "onebot", "owner_id": "7"}
    before = runtime.store.db.total_changes
    response = await web.get(
        "/api/channels/deliveries",
        params={**params, "account_id": "retired-bot", "main_id": "client-main"},
    )
    assert response.status_code == 200
    page = response.json()
    header = {
        "source": "local",
        "coverage": "saved_by_muli",
        "consistency": "live",
        "channel": "onebot",
        "account_id": "saved-bot",
        "owner_id": "7",
        "session_id": main_id,
    }
    assert {key: page[key] for key in header} == header
    assert [item["id"] for item in page["items"]] == [identifier]
    assert set(page["items"][0]) == {
        "id",
        "kind",
        "role",
        "state",
        "confirmed_parts",
        "total_parts",
        "created_at",
        "attachment_ids",
    }
    assert "incoming" not in page and "outgoing" not in page
    assert page["omitted"] == len(outside) and page["has_more"] is False
    assert all(
        secret not in response.text
        for secret in ("PRIVATE-", "route", "payload", "receipts", "context_token", "reply_to")
    )
    for other in [*outside, "missing-delivery"]:
        result = await web.get(f"/api/channels/deliveries/{other}/content", params=params)
        assert result.status_code == 404
        assert result.json()["detail"]["code"] == "evidence_not_found"
    raw = await web.get("/api/channels")
    assert raw.status_code == 200
    assert {"route", "payload", "receipts"} <= raw.json()["deliveries"][0].keys()
    assert runtime.store.db.total_changes == before


@pytest.mark.parametrize("disabled", [False, True])
async def test_web_delivery_reads_reject_revoked_or_disabled_bindings(
    channel_web: tuple[Runtime, httpx.AsyncClient], disabled: bool
) -> None:
    runtime, web = channel_web
    runtime.main_session()
    identifier = delivery(runtime.store, account="saved-bot")
    runtime.config.update(
        {
            "channels": {
                "onebot": {
                    "enabled": not disabled,
                    "owner_ids": ["7", "8"] if disabled else ["8"],
                }
            }
        }
    )
    before = runtime.store.db.total_changes
    for path in (
        "/api/channels/deliveries",
        f"/api/channels/deliveries/{identifier}/content",
    ):
        result = await web.get(path, params={"channel": "onebot", "owner_id": "7"})
        assert result.status_code == 403
    assert runtime.store.db.total_changes == before


async def test_web_delivery_reads_do_not_construct_a_missing_main(
    channel_web: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, web = channel_web
    # Runtime certifies a newly created database with a main identity. Model the
    # real missing-marker state only after that fixture initialization finishes.
    with runtime.store.db:
        runtime.store.db.execute("DELETE FROM runtime_state WHERE name='main_session'")
    assert runtime.store.existing_main_session_id() is None
    before = runtime.store.db.total_changes
    for path in (
        "/api/channels/deliveries",
        "/api/channels/deliveries/missing-delivery/content",
    ):
        result = await web.get(path, params={"channel": "onebot", "owner_id": "7"})
        assert result.status_code == 404
        assert result.json()["detail"]["code"] == "evidence_not_found"
    assert runtime.store.existing_main_session_id() is None
    assert runtime.store.db.total_changes == before


async def test_web_delivery_empty_window_and_account_change_keep_reader_cursor_semantics(
    channel_web: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, web = channel_web
    runtime.main_session()
    runtime.config.update({"limits": {"max_search_results": 1}})
    identifier = delivery(runtime.store, account="saved-bot")
    delivery(runtime.store, account="saved-bot", owner="8")
    params = {"channel": "onebot", "owner_id": "7", "limit": 20}
    before = runtime.store.db.total_changes
    response = await web.get("/api/channels/deliveries", params=params)
    assert response.status_code == 200
    first = response.json()
    assert first["items"] == [] and first["omitted"] == 1 and first["has_more"] is True
    assert isinstance(first["next_cursor"], str)
    tail = await web.get(
        "/api/channels/deliveries", params={**params, "cursor": first["next_cursor"]}
    )
    assert tail.status_code == 200
    assert [item["id"] for item in tail.json()["items"]] == [identifier]
    assert tail.json()["next_cursor"] is None and tail.json()["has_more"] is False
    assert runtime.store.db.total_changes == before
    runtime.config.update({"channels": {"onebot": {"account_id": "replacement-bot"}}})
    stale = await web.get(
        "/api/channels/deliveries", params={**params, "cursor": first["next_cursor"]}
    )
    assert stale.status_code == 400
    assert stale.json()["detail"]["code"] == "record_cursor_invalid"
    old = await web.get(
        f"/api/channels/deliveries/{identifier}/content",
        params={"channel": "onebot", "owner_id": "7"},
    )
    assert old.status_code == 404
    assert runtime.store.db.total_changes == before


async def test_web_delivery_content_preserves_utf8_revision_and_attachment_parts(
    channel_web: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, web = channel_web
    runtime.main_session()
    runtime.config.update({"limits": {"max_output_chars": 1000}})
    text = "甲🙂" * 400
    identifier = delivery(
        runtime.store,
        account="saved-bot",
        state="unknown",
        confirmed=1,
        parts=[{"text": text}, {"attachment_id": "exact-file-reference"}],
    )
    path = f"/api/channels/deliveries/{identifier}/content"
    params = {"channel": "onebot", "owner_id": "7"}
    first = await web.get(path, params=params)
    assert first.status_code == 200
    page = first.json()
    assert page["has_more"] is True and page["offset_bytes"] == 0
    revision = page["revision"]
    content = page["content"]
    assert content.encode("utf-8") == text.encode("utf-8")[: page["next_offset_bytes"]]
    assert "PRIVATE-" not in first.text and "receipts" not in first.text
    with runtime.store.db:
        runtime.store.db.execute(
            "UPDATE channel_outbox SET state='sent',next_part=2 WHERE id=?", (identifier,)
        )
    before = runtime.store.db.total_changes
    for _ in range(8):
        if not page["has_more"]:
            break
        offset = page["next_offset_bytes"]
        result = await web.get(
            path, params={**params, "offset_bytes": offset, "revision": revision}
        )
        assert result.status_code == 200
        page = result.json()
        assert page["revision"] == revision and page["offset_bytes"] == offset
        assert page["state"] == "sent" and page["confirmed_parts"] == 2
        content += page["content"]
    else:
        pytest.fail("有界测试正文未在有限页面内读完")
    assert content == text
    attachment = await web.get(path, params={**params, "part": 1})
    assert attachment.status_code == 200
    assert attachment.json()["attachment_ids"] == ["exact-file-reference"]
    assert attachment.json()["content"] == "" and attachment.json()["total_bytes"] == 0
    assert attachment.json()["has_more"] is False
    for invalid in ({"offset_bytes": 1}, {"offset_bytes": 1, "revision": revision}):
        result = await web.get(path, params={**params, **invalid})
        assert result.status_code == 400
    assert runtime.store.db.total_changes == before
    with runtime.store.db:
        runtime.store.db.execute(
            "UPDATE channel_outbox SET payload=json_set(payload,'$.parts[0].text',?) WHERE id=?",
            (text.replace("甲", "乙", 1), identifier),
        )
    changed = await web.get(path, params={**params, "revision": revision})
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "evidence_changed"


async def test_web_delivery_query_and_reader_errors_remain_distinct(
    channel_web: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, web = channel_web
    runtime.main_session()
    identifier = delivery(runtime.store, account="saved-bot")
    params = {"channel": "onebot", "owner_id": "7"}
    for invalid in (
        {"cursor": "not-a-cursor"},
        {"cursor": "x" * (runtime.config.settings.limits.max_output_chars + 1)},
    ):
        result = await web.get("/api/channels/deliveries", params={**params, **invalid})
        assert result.status_code == 400
    for invalid in ({"limit": 0}, {"limit": CHANNEL_HISTORY_PAGE_LIMIT + 1}, {"limit": "many"}):
        result = await web.get("/api/channels/deliveries", params={**params, **invalid})
        assert result.status_code == 422
    missing_owner = await web.get("/api/channels/deliveries", params={"channel": "onebot"})
    assert missing_owner.status_code == 422
    result = await web.get(
        f"/api/channels/deliveries/{identifier}/content", params={**params, "part": -1}
    )
    assert result.status_code == 422


async def test_paused_delivery_reads_remain_passive_and_retry_stays_blocked(
    channel_web: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, web = channel_web
    runtime.main_session()
    identifier = delivery(runtime.store, account="saved-bot", state="unknown")
    runtime.maintenance.admission.pause()
    before = runtime.store.db.total_changes
    params = {"channel": "onebot", "owner_id": "7"}
    for path in (
        "/api/channels/deliveries",
        f"/api/channels/deliveries/{identifier}/content",
    ):
        response = await web.get(path, params=params)
        assert response.status_code == 200
    retry = await web.post(
        f"/api/channels/deliveries/{identifier}/retry", json={"allow_unknown": True}
    )
    assert retry.status_code == 409 and retry.json()["detail"] == "execution_paused"
    assert (await web.get("/api/channels/future-read")).status_code == 409
    assert not runtime.maintenance.admission.allowed
    assert runtime.store.db.total_changes == before


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("GET", "/api/channels/deliveries", True),
        ("GET", "/api/channels/deliveries/exact-id/content", True),
        ("POST", "/api/channels/deliveries", False),
        ("POST", "/api/channels/deliveries/exact-id/retry", False),
        ("GET", "/api/channels/deliveries/exact-id/retry", False),
        ("GET", "/api/channels/deliveries//content", False),
        ("GET", "/api/channels/deliveries/exact-id/content/extra", False),
    ],
)
def test_only_exact_delivery_read_routes_are_passive(method: str, path: str, allowed: bool) -> None:
    assert passive_request_allowed(method, path, None) is allowed
    assert request_has_effect(method, path, None) is not allowed


async def test_delivery_read_routes_require_existing_web_authentication(
    channel_web: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, web = channel_web
    runtime.config.update({"server": {"password_hash": "configured"}})
    for path in (
        "/api/channels/deliveries",
        "/api/channels/deliveries/exact-id/content",
    ):
        result = await web.get(path, params={"channel": "onebot", "owner_id": "7"})
        assert result.status_code == 401
