"""iLink 扫码登录：二维码、后台轮询与自动写入渠道配置。"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from pathlib import Path
from typing import Any

import pytest
from test_runtime import make_runtime

from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.config import ConfigFile
from muli.local_services import LocalServices
from muli.providers import ModelTurn, ToolCall
from muli.runtime import Runtime
from muli.weixin_binding import WeixinBinding

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lY1GAAAAAElFTkSuQmCC"
)
CONFIRMED = {
    "status": "confirmed",
    "ilink_bot_id": "bot@im.bot",
    "bot_token": "secret-token",
    "ilink_user_id": "wxid_owner",
    "baseurl": "https://ilinkai.weixin.qq.com",
}


class Adapter:
    def __init__(self, statuses: list[dict[str, Any]], *, needs_code: bool = False) -> None:
        self.statuses = statuses
        self.needs_code = needs_code
        self.calls: list[tuple[str, str, str]] = []

    async def qr_login_start(self) -> tuple[str, str]:
        return "qr-code", "https://weixin.example/login/abc"

    async def qr_login_poll(
        self, qrcode: str, base_url: str, *, verify_code: str = ""
    ) -> dict[str, Any]:
        self.calls.append((qrcode, base_url, verify_code))
        if self.needs_code and not verify_code:
            return {"status": "need_verifycode"}
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]


def rig(
    tmp_path: Path, statuses: list[dict[str, Any]], *, needs_code: bool = False
) -> tuple[Runtime, Attachments, Adapter, dict[str, Any]]:
    config = ConfigFile(tmp_path / "config.json")
    runtime = Runtime(config, tmp_path / "data")
    attachments = Attachments(runtime.store, tmp_path / "attachments", config)
    adapter = Adapter(statuses, needs_code=needs_code)
    staged: dict[str, Any] = {}
    return runtime, attachments, adapter, staged


async def test_qr_login_binds_owner_and_enables_weixin(tmp_path: Path) -> None:
    runtime, attachments, adapter, staged = rig(
        tmp_path, [{"status": "wait"}, {"status": "scaned"}, CONFIRMED]
    )
    binding = WeixinBinding(
        runtime, attachments, lambda: adapter, _stage(staged), poll_interval=0.01
    )
    try:
        session = await binding.start()
        item = attachments.get(session["attachment_id"])
        data = attachments.path(item["id"]).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", data[16:24])
        assert width == height > 20
        assert attachments.public(item)["preview_url"].endswith("/content")
        assert "secret-token" not in json.dumps(session)

        result = await binding.wait(session["session_id"])
        assert result["state"] == "bound"
        assert result["plan_id"] == "plan-1"
        assert result["owner_id"] == "wxid_owner"
        assert "secret-token" not in json.dumps(result)
        weixin = staged["changes"]["channels"]["weixin"]
        assert staged["action"] == "configure" and staged["apply_mode"] == "in_process"
        assert weixin == {
            "enabled": True,
            "token": "secret-token",
            "token_env": "",
            "account_id": "bot@im.bot",
            "user_id": "wxid_owner",
            "base_url": "https://ilinkai.weixin.qq.com",
            "owner_ids": ["wxid_owner"],
        }
        assert any(event["type"] == "weixin.bound" for event in runtime.store.events())
    finally:
        await binding.close()
        await runtime.close()


async def test_qr_login_accepts_verify_code(tmp_path: Path) -> None:
    runtime, attachments, adapter, staged = rig(
        tmp_path, [{"status": "wait"}, CONFIRMED], needs_code=True
    )
    binding = WeixinBinding(
        runtime, attachments, lambda: adapter, _stage(staged), poll_interval=0.01
    )
    try:
        session = await binding.start()
        for _ in range(200):
            current = await binding.status(session["session_id"])
            if current["state"] == "need_verifycode":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("扫码会话没有进入验证码状态")
        await binding.verify(session["session_id"], "123456")
        result = await binding.wait(session["session_id"])
        assert result["state"] == "bound"
        assert any(call[2] == "123456" for call in adapter.calls)
    finally:
        await binding.close()
        await runtime.close()


async def test_expired_qr_does_not_stage_configuration(tmp_path: Path) -> None:
    runtime, attachments, adapter, staged = rig(
        tmp_path, [{"status": "wait"}, {"status": "expired"}]
    )
    binding = WeixinBinding(
        runtime, attachments, lambda: adapter, _stage(staged), poll_interval=0.01
    )
    try:
        session = await binding.start()
        result = await binding.wait(session["session_id"])
        assert result["state"] == "expired"
        assert staged == {}
        assert not any(event["type"] == "weixin.bound" for event in runtime.store.events())
    finally:
        await binding.close()
        await runtime.close()


async def test_cancel_stops_background_polling(tmp_path: Path) -> None:
    runtime, attachments, adapter, staged = rig(tmp_path, [{"status": "wait"}])
    binding = WeixinBinding(
        runtime, attachments, lambda: adapter, _stage(staged), poll_interval=0.01
    )
    try:
        session = await binding.start()
        result = await binding.cancel(session["session_id"])
        assert result["state"] == "cancelled"
        calls = len(adapter.calls)
        await asyncio.sleep(0.05)
        assert len(adapter.calls) == calls
    finally:
        await binding.close()
        await runtime.close()


def _stage(staged: dict[str, Any]):
    async def stage(action: str, changes: dict[str, Any], *, apply_mode: str) -> dict[str, Any]:
        staged.update(action=action, changes=changes, apply_mode=apply_mode)
        return {"id": "plan-1"}

    return stage


async def test_channel_bind_tool_requires_an_owner_round(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    runtime = Runtime(config, tmp_path / "data")
    attachments = Attachments(runtime.store, tmp_path / "attachments", config)
    application = ChannelApplication(runtime, attachments)
    try:
        tool = runtime.tools["channel_bind"]
        with pytest.raises(PermissionError):
            await tool.handler({"action": "weixin_qr"})
    finally:
        await application.close()
        await runtime.close()


async def test_published_image_is_attached_to_the_assistant_message(tmp_path: Path) -> None:
    image = tmp_path / "figure.png"
    image.write_bytes(PNG)
    turns = [
        ModelTurn(
            tool_calls=[
                ToolCall("c1", "file_publish", json.dumps({"path": str(image)})),
            ]
        ),
        ModelTurn(content="图片已发布"),
    ]
    runtime, _ = make_runtime(tmp_path, turns)
    attachments = Attachments(runtime.store, tmp_path / "attachments", runtime.config)
    LocalServices(runtime, attachments)
    try:
        session = runtime.new_session(mode="yolo")
        await runtime.start(session["id"], "发一张图")
        await runtime.tasks[session["id"]]
        messages = runtime.store.messages(session["id"])
        final = next(
            message
            for message in messages
            if message["role"] == "assistant" and message["content"] == "图片已发布"
        )
        assert len(final["attachment_ids"]) == 1
        item = attachments.get(final["attachment_ids"][0])
        assert item["mime_type"] == "image/png"
        assert attachments.public(item)["preview_url"].endswith("/content")
    finally:
        await runtime.close()
