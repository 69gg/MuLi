from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_runtime import make_runtime

from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.channels import ChannelManager, IncomingMessage
from muli.config import ConfigFile
from muli.local_services import LocalServices
from muli.personal_io import PersonalIO
from muli.providers import ModelTurn
from muli.server import create_app


async def test_channel_configuration_revocation_rejects_waiting_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "channels": {
                "onebot": {
                    "enabled": True,
                    "base_url": "https://unused.invalid",
                    "owner_ids": ["7"],
                    "webhook_secret": "test-secret",
                }
            }
        }
    )
    files = Attachments(runtime.store, tmp_path / "attachments", runtime.config)
    channels = ChannelApplication(runtime, files)
    entered = asyncio.Event()
    submit = runtime.submit

    async def waiting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        return await submit(*args, **kwargs)

    monkeypatch.setattr(runtime, "submit", waiting)
    try:
        async with runtime.configuration_lock:
            task = asyncio.create_task(
                channels.manager.receive(
                    "onebot", IncomingMessage("pending", "7", "7", "等待中的消息")
                )
            )
            await asyncio.wait_for(entered.wait(), 2)
            runtime.config.update({"channels": {"onebot": {"enabled": False, "owner_ids": []}}})
        with pytest.raises(ValueError, match="授权已改变"):
            await task
        assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == 0
        assert not runtime.tasks and not seen
    finally:
        await channels.close()
        await runtime.close()


async def test_signed_webhook_is_independent_of_web_login_but_cannot_impersonate_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "server": {"password": "test-password"},
            "updates": {"check_on_start": False},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "base_url": "https://unused.invalid",
                    "webhook_secret": "test-secret",
                    "owner_ids": ["7"],
                }
            },
        }
    )
    app = create_app(config, tmp_path / "data")
    raw = json.dumps(
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": "external",
            "user_id": 8,
            "message": [{"type": "text", "data": {"text": "我是主人"}}],
        }
    ).encode()
    signature = "sha1=" + hmac.new(b"test-secret", raw, hashlib.sha1).hexdigest()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client,
    ):
        assert (await client.get("/api/channels")).status_code == 401
        assert (await client.get("/api/channels/onebot/webhook")).status_code == 401
        rejected = await client.post(
            "/api/channels/onebot/webhook",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert rejected.status_code == 401
        assert "签名" in rejected.json()["detail"]
        response = await client.post(
            "/api/channels/onebot/webhook",
            content=raw,
            headers={"Content-Type": "application/json", "X-Signature": signature},
        )
        assert response.status_code == 200
        runtime = app.state.runtime
        rows = runtime.store.db.execute(
            "SELECT state,json_extract(payload,'$.principal') FROM ingress"
        ).fetchall()
        assert rows == []
        assert runtime.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == 0
        assert not runtime.tasks and not runtime.store.list("message")
        attempts: list[bool] = []

        async def retry(identifier: str, *, allow_unknown: bool = False) -> dict[str, Any]:
            attempts.append(allow_unknown)
            return {"id": identifier, "state": "queued"}

        monkeypatch.setattr(app.state.channel_application.manager, "retry", retry)
        endpoint = "/api/channels/deliveries/test-delivery/retry"
        assert (await client.post(endpoint, json={"allow_unknown": True})).status_code == 401
        assert not attempts
        await client.post("/api/auth/login", json={"password": "test-password"})
        assert (await client.post(endpoint, json={})).status_code == 200
        assert (await client.post(endpoint, json={"allow_unknown": True})).status_code == 200
        assert attempts == [False, True]


async def test_capture_uses_attachment_store_and_unverified_voice_stays_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="听到了")])
    attachments = Attachments(runtime.store, tmp_path / "attachments", runtime.config)
    local = LocalServices(runtime, attachments)
    io = PersonalIO(runtime, local, attachments)
    path = tmp_path / "capture.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nmock capture")

    async def capture() -> dict[str, Any]:
        return {"path": str(path), "backend": "test"}

    monkeypatch.setattr(io.capture, "screenshot", capture)
    try:
        with pytest.raises(PermissionError, match="未启用"):
            await runtime.tools["computer_screenshot"].handler({})
        with pytest.raises(PermissionError, match="未启用"):
            await runtime.tools["computer_record"].handler({"seconds": 2})
        assert not attachments.list()
        runtime.config.update({"computer": {"enabled": True}})
        item = await runtime.tools["computer_screenshot"].handler({})
        assert item["url"].endswith("/download")
        assert attachments.path(item["id"]).read_bytes() == path.read_bytes()
        assert path.exists()
        await io.on_voice({"text": "未验证的声音", "speaker_verified": False})
        assert not runtime.tasks and not seen
        await io.on_voice({"text": "经过声纹验证的声音", "speaker_verified": True})
        task = runtime.tasks[runtime.main_session()["id"]]
        await task
        assert [message.content for message in seen[0] if message.role == "user"] == [
            "经过声纹验证的声音"
        ]
        assert runtime.tools["computer_screenshot"].risk == "computer"
    finally:
        await io.close()
        await runtime.close()


async def test_management_reports_pending_plugin_restart_and_retains_workflow_sessions(
    tmp_path: Path,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    app = create_app(config, tmp_path / "data")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client,
    ):
        assert not (await client.get("/api/management")).json()["plugins_restart_required"]
        assert (
            await client.patch("/api/settings", json={"plugins": {"enabled": False}})
        ).status_code == 200
        assert (await client.get("/api/management")).json()["plugins_restart_required"]
        runtime = app.state.runtime
        session = runtime.new_session()
        runtime.store.put("session", {**session, "workflow_run_id": "persistent-run"})
        response = await client.delete("/api/sessions/" + session["id"])
        assert response.status_code == 409
        assert runtime.store.get("session", session["id"]) is not None
        status = (await client.get("/api/status")).json()
        assert not status["voice"]["enabled"] and not status["capture"]["enabled"]

        runtime.memory.register_source(
            {
                "id": "original",
                "content": "完整记忆原文",
                "session_id": session["id"],
                "role": "user",
                "principal": "owner",
            },
            trusted_owner=True,
        )
        runtime.store.db.execute(
            "INSERT INTO channel_state VALUES('context:test','private-protocol-token')"
        )
        runtime.store.db.commit()
        exported = await client.get("/api/export")
        rows = [json.loads(line) for line in exported.text.splitlines()]
        assert any(
            row.get("table") == "memory_sources" and row["record"]["content"] == "完整记忆原文"
            for row in rows
        )
        assert "private-protocol-token" not in exported.text


async def test_owner_channel_and_web_share_identity_without_broadcasting_web_reply(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(
        tmp_path, [ModelTurn(content="渠道回复"), ModelTurn(content="网页回复")]
    )
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "base_url": "https://unused.invalid",
                    "webhook_secret": "test-secret",
                    "owner_ids": ["7"],
                }
            },
        }
    )
    app = create_app(runtime.config, runtime.data_dir, runtime)
    sent: list[dict[str, Any]] = []

    async def send(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "sent"}}
        )

    original = app.state.channel_application.manager
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as channel_client:
        manager = ChannelManager(
            runtime.store,
            runtime.bus,
            original.config,
            original.submit,
            app.state.attachments,
            client=channel_client,
        )
        await original.close()
        app.state.channel_application.manager = manager
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
            ) as web,
        ):
            result = await manager.receive(
                "onebot", IncomingMessage("owner", "7", "7", "先从渠道沟通")
            )
            await runtime.wait(result["run_id"])
            assert not manager.deliveries() and not sent
            reply = (
                await web.post(
                    "/api/sessions/" + result["session_id"] + "/messages",
                    json={"content": "网页接着说"},
                )
            ).json()
            await runtime.wait(reply["run_id"])
            assert reply["session_id"] == result["session_id"] == runtime.main_session()["id"]
            inputs = [message for message in seen[-1] if message.role == "user"]
            assert len(inputs) == 2
            assert inputs[0].content.endswith("先从渠道沟通")
            assert '"channel": "onebot"' in inputs[0].content
            assert '"sender_id": "7"' in inputs[0].content
            assert inputs[1].content == "网页接着说"
            completed = [
                event["data"]["message"]
                for event in runtime.store.events()
                if event["type"] == "message.completed"
                and event["data"]["message"]["role"] == "assistant"
            ]
            assert completed[-1]["delivery_routes"] == []
            assert not manager.deliveries() and not sent


async def test_attention_lifecycle_configuration_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_attention import eventually, reminder

    runtime, seen = make_runtime(
        tmp_path, [ModelTurn(content="启动提醒"), ModelTurn(content="重新启用后的新提醒")]
    )
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "attention": {"coalesce_seconds": 0},
        }
    )
    await reminder(runtime, "创建应用之前的历史")
    app = create_app(runtime.config, runtime.data_dir, runtime)
    await reminder(runtime, "启动生产者的新事件")
    inbox = app.state.attention
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client,
    ):
        await eventually(lambda: len(seen) == 1 and not runtime.tasks)
        content = "\n".join(message.content for message in seen[0] if message.role == "user")
        assert "启动生产者的新事件" in content
        assert "创建应用之前的历史" not in content
        status = (await client.get("/api/status")).json()["attention"]
        assert status["enabled"] and status["running"]

        original = runtime.reconfigure
        fail_once = True

        async def reconfigure() -> None:
            nonlocal fail_once
            await original()
            if fail_once:
                fail_once = False
                raise ValueError("模拟重建失败")

        monkeypatch.setattr(runtime, "reconfigure", reconfigure)
        response = await client.patch("/api/settings", json={"attention": {"enabled": False}})
        assert response.status_code == 400
        assert runtime.config.settings.attention.enabled and inbox.status()["running"]
        assert ConfigFile(runtime.config.path).settings.attention.enabled
        assert not runtime.store.pending_inputs(runtime.main_session()["id"])

        response = await client.patch("/api/settings", json={"attention": {"enabled": False}})
        assert response.status_code == 200
        assert not inbox.status()["enabled"] and not inbox.status()["running"]
        await reminder(runtime, "停用期间的历史")
        response = await client.patch("/api/settings", json={"attention": {"enabled": True}})
        assert response.status_code == 200
        await reminder(runtime, "重新启用后的新事件")
        await eventually(lambda: len(seen) == 2 and not runtime.tasks)
        content = "\n".join(message.content for message in seen[-1] if message.role == "user")
        assert "重新启用后的新事件" in content and "停用期间的历史" not in content
        messages = runtime.store.messages(runtime.main_session()["id"])
        assert all(
            message.get("principal") == "system:attention"
            for message in messages
            if message["role"] == "user"
        )
        assert all(
            message.get("delivery_routes") == []
            for message in messages
            if message["role"] == "assistant"
        )
    assert inbox._task is None
