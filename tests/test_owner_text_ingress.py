"""Owner text evidence is admitted by real host entrances, before added context."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, NoReturn, cast

import httpx
import pytest
from fastapi import FastAPI
from test_runtime import make_runtime

from muli.channels import ChannelError, FileContent, IncomingMessage, RemoteFile
from muli.owner_text import owner_text_prefix
from muli.permissions import Mode
from muli.runtime import Runtime
from muli.server import create_app


@pytest.fixture
async def host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Runtime, FastAPI, httpx.AsyncClient]]:
    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("owner text ingress verification must stay offline")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "memory": {"auto_extract": False},
            "extensions": {"watch_enabled": False},
            "onboarding_pending": True,
            "limits": {"max_pending_runs": 16},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://platform.invalid",
                    "owner_ids": ["7"],
                    "receive_files": True,
                    "max_attachments": 1,
                }
            },
        }
    )

    def keep_pending(session: dict[str, Any], role: str, mode: Mode) -> None:
        # These regressions inspect durable admission, without model execution.
        return None

    monkeypatch.setattr(runtime, "_launch", keep_pending)
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        yield runtime, app, web


def persisted(runtime: Runtime, input_id: str) -> dict[str, Any]:
    row = runtime.store.db.execute("SELECT payload FROM ingress WHERE id=?", (input_id,)).fetchone()
    assert row is not None
    return cast(dict[str, Any], json.loads(row["payload"]))


async def test_web_message_persists_exact_owner_unicode_prefix(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient],
) -> None:
    runtime, _app, web = host
    text = "  主人🌱e\u0301原文\n "
    response = await web.post(
        f"/api/sessions/{runtime.main_session()['id']}/messages", json={"content": text}
    )
    assert response.status_code == 200
    message = response.json()["message"]
    payload = persisted(runtime, message["id"])
    assert payload["content"] == message["content"] == text
    assert payload["owner_text"] == {"version": 1, "kind": "web", "chars": len(text)}


async def test_channel_marks_original_text_before_attachment_warnings_and_placeholder(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, app, _web = host
    manager = app.state.channel_application.manager

    async def failed_download(remote: RemoteFile, route: Mapping[str, Any]) -> FileContent:
        raise ChannelError("只存在于追加告警中的词")

    monkeypatch.setattr(manager.adapters["onebot"], "download", failed_download)
    files = (RemoteFile("附件正文词.txt"), RemoteFile("超额附件词.txt"))
    text = "  主人🌱原始文字\n "
    result = await manager.receive(
        "onebot", IncomingMessage("owner-original", "7", "7", text, files=files)
    )
    payload = persisted(runtime, result["message"]["id"])
    assert payload["content"].startswith(text)
    assert "超过 max_attachments" in payload["content"]
    assert "只存在于追加告警中的词" in payload["content"]
    assert payload["owner_text"] == {"version": 1, "kind": "channel", "chars": len(text)}
    assert payload["content"][: payload["owner_text"]["chars"]] == text

    attachment_only = await manager.receive(
        "onebot", IncomingMessage("owner-attachment-only", "7", "7", "", files=files)
    )
    attachment_payload = persisted(runtime, attachment_only["message"]["id"])
    assert "收到附件" in attachment_payload["content"]
    assert "owner_text" not in attachment_payload


async def test_onboarding_and_default_submit_do_not_claim_owner_text(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient],
) -> None:
    runtime, _app, _web = host
    rows = runtime.store.db.execute("SELECT payload FROM ingress ORDER BY seq").fetchall()
    onboarding = [json.loads(row["payload"]) for row in rows]
    assert len(onboarding) == 1 and onboarding[0]["source"] == "onboarding"
    assert "owner_text" not in onboarding[0]
    result = await runtime.submit("默认内部调用，即使 principal 为 owner 也不是真人证明")
    assert "owner_text" not in persisted(runtime, result["message"]["id"])


@pytest.mark.parametrize("text", ["", " \t\n"])
async def test_blank_web_attachment_does_not_mark_replacement_text(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient], text: str
) -> None:
    runtime, _app, web = host
    upload = await web.post(
        "/api/uploads",
        content=b"only attachment content",
        headers={"x-file-name": "owner-note.txt", "Content-Type": "text/plain"},
    )
    assert upload.status_code == 200
    response = await web.post(
        f"/api/sessions/{runtime.main_session()['id']}/messages",
        json={"content": text, "attachment_ids": [upload.json()["id"]]},
    )
    assert response.status_code == 200
    payload = persisted(runtime, response.json()["message"]["id"])
    assert payload["content"] == "请查看附件"
    assert "owner_text" not in payload
    assert owner_text_prefix(payload) is None


async def test_blank_channel_attachment_does_not_mark_placeholder(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, app, _web = host
    manager = app.state.channel_application.manager

    async def failed_download(remote: RemoteFile, route: Mapping[str, Any]) -> FileContent:
        raise ChannelError("附件占位和告警均不属于主人文字")

    monkeypatch.setattr(manager.adapters["onebot"], "download", failed_download)
    result = await manager.receive(
        "onebot",
        IncomingMessage("blank-owner-file", "7", "7", " \t\n", files=(RemoteFile("a.txt"),)),
    )
    payload = persisted(runtime, result["message"]["id"])
    assert "收到附件" in payload["content"]
    assert "owner_text" not in payload
    assert owner_text_prefix(payload) is None


@pytest.mark.parametrize("chars", [None, True, False, 0, -1, 100, 1.0, "1"])
async def test_invalid_private_text_length_does_not_mark_admitted_input(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient], chars: Any
) -> None:
    runtime, _app, _web = host
    result = await runtime.submit("主人文字", _owner_text_chars=chars)
    payload = persisted(runtime, result["message"]["id"])
    assert payload["content"] == "主人文字"
    assert "owner_text" not in payload


@pytest.mark.parametrize(
    "extra",
    [
        {"source": "onboarding"},
        {"source": "system:attention", "principal": "system:attention"},
        {"source": "workflow:test", "principal": "owner"},
        {"principal": "external:test"},
        {"event_id": "host-scripted-event"},
        {"route": {"name": "onebot", "account_id": "123"}},
        {"source": "channel:onebot:123", "route": {"name": "onebot", "account_id": "123"}},
    ],
)
async def test_scripted_sources_cannot_claim_owner_text_from_length_alone(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient], extra: dict[str, Any]
) -> None:
    runtime, _app, _web = host
    result = await runtime.submit("主人文字", _owner_text_chars=4, **extra)
    assert "owner_text" not in persisted(runtime, result["message"]["id"])


def test_reader_returns_original_unicode_prefix_without_added_context() -> None:
    text = "  🌱e\u0301原文\n "
    payload = {
        "content": text + "[后来追加的附件和告警]",
        "source": "channel:onebot:123",
        "route": {"name": "onebot", "account_id": "123"},
        "owner_text": {"version": 1, "kind": "channel", "chars": len(text)},
    }
    prefix = owner_text_prefix(payload)
    assert prefix == text
    assert prefix is not None and "后来追加" not in prefix


@pytest.mark.parametrize(
    "marker",
    [
        None,
        [],
        {},
        {"version": True, "kind": "web", "chars": 1},
        {"version": 2, "kind": "web", "chars": 1},
        {"version": 1, "kind": [], "chars": 1},
        {"version": 1, "kind": "other", "chars": 1},
        {"version": 1, "kind": "channel", "chars": 1},
        {"version": 1, "kind": "web", "chars": True},
        {"version": 1, "kind": "web", "chars": 0},
        {"version": 1, "kind": "web", "chars": -1},
        {"version": 1, "kind": "web", "chars": 5},
        {"version": 1, "kind": "web", "chars": 1.0},
        {"version": 1, "kind": "web", "chars": "1"},
        {"version": 1, "kind": "web", "chars": 1, "text": "伪造全文"},
    ],
)
def test_reader_rejects_corrupted_metadata_without_scanning_content(marker: Any) -> None:
    assert owner_text_prefix({"content": "主人文字", "source": "web", "owner_text": marker}) is None


@pytest.mark.parametrize(
    "updates",
    [
        {"content": " \t\n"},
        {"content": None},
        {"source": "onboarding"},
        {"source": "channel:onebot:123"},
        {"route": {}},
    ],
)
def test_reader_rejects_blank_or_mismatched_web_source(updates: dict[str, Any]) -> None:
    payload = {
        "content": "主人文字",
        "source": "web",
        "owner_text": {"version": 1, "kind": "web", "chars": 1},
        **updates,
    }
    assert owner_text_prefix(payload) is None


@pytest.mark.parametrize("shape", ["stable", "legacy"])
async def test_old_channel_input_without_marker_replays_without_upgrade_or_second_id(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient], shape: str
) -> None:
    runtime, app, _web = host
    manager = app.state.channel_application.manager
    incoming = IncomingMessage("old-owner-input", "7", "7", "旧主人文字")
    first = await manager.receive("onebot", incoming)
    payload = persisted(runtime, first["message"]["id"])
    original_id = payload["id"]
    original_seq = first["message"]["seq"]
    payload.pop("owner_text")
    inbound_id = runtime.store.db.execute(
        "SELECT id FROM channel_inbound WHERE name='onebot'"
    ).fetchone()["id"]
    if shape == "legacy":
        payload["id"] = hashlib.sha256(
            f"{payload['session_id']}\0{payload['source']}\0{inbound_id}".encode()
        ).hexdigest()
    raw_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with runtime.store.db:
        runtime.store.db.execute(
            "UPDATE ingress SET id=?,payload=? WHERE id=?",
            (payload["id"], raw_payload, original_id),
        )
        runtime.store.db.execute(
            "UPDATE channel_inbound SET receipt=NULL WHERE id=?", (inbound_id,)
        )
    count = runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
    recovered = await manager.receive("onebot", incoming)
    assert recovered["duplicate"] is True
    assert recovered["message"]["id"] == payload["id"]
    assert recovered["message"]["seq"] == original_seq
    assert "owner_text" not in recovered["message"]
    assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == count
    assert (
        runtime.store.db.execute(
            "SELECT payload FROM ingress WHERE id=?", (payload["id"],)
        ).fetchone()["payload"]
        == raw_payload
    )
    assert await manager.receive("onebot", incoming) == recovered


async def test_current_channel_receipt_replay_keeps_original_marker_and_sequence(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient],
) -> None:
    runtime, app, _web = host
    manager = app.state.channel_application.manager
    incoming = IncomingMessage("repeat-owner-text", "7", "7", "主人原始文字")
    first = await manager.receive("onebot", incoming)
    count = runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
    replay = await manager.receive("onebot", incoming)
    assert replay == first
    assert replay["message"]["owner_text"] == {
        "version": 1,
        "kind": "channel",
        "chars": len(incoming.text),
    }
    assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == count
    with runtime.store.db:
        runtime.store.db.execute("UPDATE channel_inbound SET receipt=NULL WHERE name='onebot'")
    recovered = await manager.receive("onebot", incoming)
    assert recovered["duplicate"] is True
    assert recovered["message"]["owner_text"] == first["message"]["owner_text"]
    assert recovered["message"]["id"] == first["message"]["id"]
    assert recovered["message"]["seq"] == first["message"]["seq"]
    assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == count


async def test_stale_channel_configuration_cannot_admit_owner_text(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient],
) -> None:
    runtime, app, _web = host
    manager = app.state.channel_application.manager
    runtime.config.update({"channels": {"onebot": {"owner_ids": ["8"]}}})
    count = runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
    with pytest.raises(ValueError, match="配置或授权已改变"):
        await manager.receive("onebot", IncomingMessage("stale-owner-text", "7", "7", "旧绑定文字"))
    assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == count


async def test_delegated_channel_owner_text_is_not_main_owner_text(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient],
) -> None:
    runtime, app, _web = host
    # This fixture inspects admission only, so close worker execution before
    # establishing a genuine assignment through the existing host control.
    await runtime.team.close()
    main_id = runtime.main_session()["id"]
    child = runtime.new_session("接管渠道的队友")
    runtime.store.put("session", {**child, "kind": "teammate", "principal": "owner"})
    runtime.store.put(
        "agent",
        {
            "id": child["id"],
            "session_id": child["id"],
            "parent_id": main_id,
            "role": "worker",
            "status": "completed",
            "mode": child["mode"],
            "generation": 1,
        },
    )
    assignment = runtime.channel_ownership.control(
        main_id,
        {"action": "assign", "channel": "onebot", "scope": "account", "agent_id": child["id"]},
    )
    assert assignment["state"] == "active"
    result = await app.state.channel_application.manager.receive(
        "onebot", IncomingMessage("delegated-owner-text", "7", "7", "已归属队友的主人文字")
    )
    payload = persisted(runtime, result["message"]["id"])
    assert payload["session_id"] == child["id"] and payload["principal"] == "owner"
    assert payload["channel_ownership"]["agent_id"] == child["id"]
    assert "owner_text" not in payload
    assert owner_text_prefix(payload) is None


async def test_web_child_session_does_not_admit_main_owner_text(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient],
) -> None:
    runtime, _app, web = host
    child = runtime.new_session("普通独立会话")
    response = await web.post(
        f"/api/sessions/{child['id']}/messages", json={"content": "非主会话文字"}
    )
    assert response.status_code == 200
    assert "owner_text" not in response.json()["message"]
    assert runtime.store.pending_inputs(child["id"]) == 0


async def test_public_chat_schema_does_not_offer_host_owner_text_fields(
    host: tuple[Runtime, FastAPI, httpx.AsyncClient],
) -> None:
    _runtime, _app, web = host
    schema = (await web.get("/api/openapi.json")).json()
    fields = schema["components"]["schemas"]["ChatInput"]["properties"]
    assert "owner_text" not in fields and "_owner_text_chars" not in fields
