"""iLink 扫码登录会话：后台轮询，确认后自动写入凭据并启用微信渠道。"""

from __future__ import annotations

import asyncio
import struct
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from muli.attachments import Attachments
from muli.channels.types import ChannelError
from muli.channels.weixin import DEFAULT_WEIXIN_BASE_URL, qr_base_url
from muli.store import new_id

if TYPE_CHECKING:
    from muli.runtime import Runtime

_SESSION_TTL_SECONDS = 300.0
_TERMINAL_RETENTION_SECONDS = 600.0
_MAX_VERIFY_CODE = 8


@dataclass
class _Login:
    session_id: str
    qrcode: str
    qr_content: str
    base_url: str
    attachment_id: str
    created_at: float
    expires_at: float
    state: str = "waiting"
    message: str = "等待扫码"
    error: str = ""
    plan_id: str = ""
    account_id: str = ""
    owner_id: str = ""
    credentials: dict[str, str] = field(default_factory=dict, repr=False)
    verify_code: str = field(default="", repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "message": self.message,
            "error": self.error,
            "plan_id": self.plan_id,
            "account_id": self.account_id,
            "owner_id": self.owner_id,
            "attachment_id": self.attachment_id,
            "qr_content": self.qr_content,
            "expires_at": self.expires_at,
        }


class WeixinBinding:
    """One explicit QR login at a time; tokens never enter events or tool results."""

    def __init__(
        self,
        runtime: Runtime,
        attachments: Attachments,
        adapter: Callable[[], Any],
        stage_configuration: Callable[..., Awaitable[dict[str, Any]]],
        *,
        poll_interval: float = 1.5,
    ) -> None:
        self.runtime = runtime
        self.attachments = attachments
        self._adapter = adapter
        self._stage = stage_configuration
        self.poll_interval = max(0.05, poll_interval)
        self._sessions: dict[str, _Login] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            if self._closed:
                raise ValueError("微信绑定服务已关闭")
            self._prune()
            if any(
                item.state in {"waiting", "scaned", "need_verifycode"}
                for item in self._sessions.values()
            ):
                raise ValueError("已有微信扫码会话，请先完成或取消")
            qrcode, content = await self._adapter().qr_login_start()
            attachment = await self._save_qr(content)
            self.runtime.record_published_attachment(attachment["id"])
            identifier = new_id()
            now = time.monotonic()
            session = _Login(
                identifier,
                qrcode,
                content,
                DEFAULT_WEIXIN_BASE_URL,
                attachment["id"],
                now,
                now + _SESSION_TTL_SECONDS,
            )
            self._sessions[identifier] = session
            session.task = asyncio.create_task(
                self._watch(identifier), name="weixin-bind-" + identifier
            )
            return session.public()

    async def status(self, session_id: str) -> dict[str, Any]:
        return self._get(session_id).public()

    async def wait(self, session_id: str) -> dict[str, Any]:
        session = self._get(session_id)
        if session.task is not None:
            await asyncio.shield(session.task)
        return session.public()

    async def verify(self, session_id: str, code: str) -> dict[str, Any]:
        session = self._get(session_id)
        normalized = code.strip()
        if not normalized.isdigit() or not 1 <= len(normalized) <= _MAX_VERIFY_CODE:
            raise ValueError("微信验证码只能是最多 8 位数字")
        if session.state not in {"need_verifycode", "scaned", "waiting"}:
            raise ValueError("当前扫码会话不需要验证码")
        session.verify_code = normalized
        session.state = "waiting"
        session.message = "已提交验证码，等待微信确认"
        return session.public()

    async def cancel(self, session_id: str) -> dict[str, Any]:
        session = self._get(session_id)
        await self._stop(session)
        session.state = "cancelled"
        session.message = "扫码会话已取消"
        return session.public()

    async def apply(self, session_id: str) -> dict[str, Any]:
        """Retry staging the confirmed credentials after a queue or config error."""
        session = self._get(session_id)
        if not session.credentials:
            raise ValueError("扫码尚未确认，没有可应用的凭据")
        await self._apply(session)
        return session.public()

    async def close(self) -> None:
        self._closed = True
        for session in list(self._sessions.values()):
            await self._stop(session)
        self._sessions.clear()

    def _get(self, session_id: str) -> _Login:
        session = self._sessions.get(str(session_id).strip())
        if session is None:
            raise ValueError("微信扫码会话不存在或已过期")
        return session

    async def _save_qr(self, content: str) -> dict[str, Any]:
        data = _qr_png(content)

        async def chunks() -> Any:
            yield data

        return await self.attachments.save(
            chunks(), "weixin-login.png", "image/png", source="weixin-bind"
        )

    async def _watch(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        while not self._closed and time.monotonic() < session.expires_at:
            try:
                payload = await self._adapter().qr_login_poll(
                    session.qrcode,
                    session.base_url,
                    verify_code=session.verify_code,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a transient poll must not end the session
                session.error = _safe_error(error)
                await asyncio.sleep(self.poll_interval)
                continue
            if session.verify_code and payload.get("status") in {
                "scaned",
                "confirmed",
                "expired",
                "verify_code_blocked",
                "binded_redirect",
            }:
                session.verify_code = ""
            status = payload.get("status")
            if status == "scaned_but_redirect":
                try:
                    session.base_url = _redirect_base(payload.get("redirect_host"))
                except ChannelError as error:
                    session.error = str(error)
                else:
                    session.error = ""
                await asyncio.sleep(self.poll_interval)
                continue
            if status == "confirmed":
                try:
                    session.credentials = _credentials(payload)
                except ValueError as error:
                    session.state = "failed"
                    session.error = str(error)
                    session.message = "扫码已确认，但登录结果不完整"
                    return
                session.account_id = session.credentials["account_id"]
                session.owner_id = session.credentials["user_id"]
                session.state = "confirmed"
                session.message = "扫码已确认，正在写入配置"
                await self._apply(session)
                return
            if status == "expired":
                session.state = "expired"
                session.message = "二维码已过期，请重新生成"
                return
            if status == "binded_redirect":
                session.state = "already_bound"
                session.message = "该微信账号已绑定，无需重复扫码"
                return
            if status == "verify_code_blocked":
                session.state = "blocked"
                session.message = "微信验证码尝试次数过多，请稍后再试"
                return
            if status == "need_verifycode":
                session.state = "need_verifycode"
                session.message = "需要微信数字验证码"
            elif status == "scaned":
                session.state = "scaned"
                session.message = "已扫码，等待手机确认"
            elif status == "wait":
                session.state = "waiting"
                session.message = "等待扫码"
            else:
                session.error = "iLink 返回未知扫码状态"
            await asyncio.sleep(self.poll_interval)
        if session.state in {"waiting", "scaned", "need_verifycode"}:
            session.state = "expired"
            session.message = "二维码已过期，请重新生成"

    async def _apply(self, session: _Login) -> None:
        credentials = session.credentials
        if not credentials:
            raise ValueError("扫码尚未确认，没有可应用的凭据")
        changes = {
            "channels": {
                "weixin": {
                    "enabled": True,
                    "token": credentials["token"],
                    "token_env": "",
                    "account_id": credentials["account_id"],
                    "user_id": credentials["user_id"],
                    "base_url": credentials["base_url"],
                    "owner_ids": [credentials["user_id"]],
                }
            }
        }
        try:
            plan = await self._stage("configure", changes, apply_mode="in_process")
        except Exception as error:  # noqa: BLE001 - report a retryable staging failure
            session.state = "confirmed"
            session.error = _safe_error(error)
            session.message = "扫码已确认，但配置排队失败；可稍后重试应用"
            return
        session.plan_id = str(plan.get("id") or "")
        session.state = "bound"
        session.message = "微信已绑定，配置将在空闲时应用"
        session.error = ""
        await self.runtime.bus.emit(
            "weixin.bound",
            {
                "session_id": session.session_id,
                "plan_id": session.plan_id,
                "account_id": session.account_id,
                "message": session.message,
            },
        )

    async def _stop(self, session: _Login) -> None:
        task, session.task = session.task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def _prune(self) -> None:
        now = time.monotonic()
        for identifier, session in list(self._sessions.items()):
            if session.task is not None and not session.task.done():
                continue
            if now - session.expires_at > _TERMINAL_RETENTION_SECONDS:
                self._sessions.pop(identifier, None)


def _qr_png(content: str) -> bytes:
    try:
        import qrcode
    except ImportError:
        raise ValueError("微信扫码需要二维码依赖 qrcode") from None
    try:
        code = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            border=2,
            box_size=6,
        )
        code.add_data(content)
        code.make(fit=True)
        matrix = [[bool(value) for value in row] for row in code.get_matrix()]
    except (ValueError, TypeError) as error:
        raise ValueError("iLink 二维码内容无法生成图片") from error
    scale = max(1, int(code.box_size))
    scaled: list[list[bool]] = []
    for row in matrix:
        expanded = [value for value in row for _ in range(scale)]
        scaled.extend([expanded] * scale)
    return _png(scaled)


def _png(matrix: list[list[bool]]) -> bytes:
    height = len(matrix)
    width = len(matrix[0]) if height else 0
    if not width or any(len(row) != width for row in matrix):
        raise ValueError("二维码矩阵无效")
    raw = bytearray()
    for row in matrix:
        raw.append(0)
        byte = bits = 0
        for black in row:
            byte = (byte << 1) | (0 if black else 1)
            bits += 1
            if bits == 8:
                raw.append(byte)
                byte = bits = 0
        if bits:
            raw.append(byte << (8 - bits))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _redirect_base(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ChannelError("iLink 登录跳转地址无效")
    from urllib.parse import urlsplit

    parsed = urlsplit("//" + value)
    if (
        parsed.hostname is None
        or parsed.path not in {"", "/"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
    ):
        raise ChannelError("iLink 登录跳转地址无效")
    return "https://" + parsed.hostname


def _credentials(payload: dict[str, Any]) -> dict[str, str]:
    account_id = _required(payload, "ilink_bot_id", 128)
    token = _required(payload, "bot_token", 4096)
    user_id = _required(payload, "ilink_user_id", 200)
    raw_base = payload.get("baseurl")
    base_url = (
        qr_base_url(raw_base)
        if isinstance(raw_base, str) and raw_base.strip()
        else DEFAULT_WEIXIN_BASE_URL
    )
    return {
        "account_id": account_id,
        "token": token,
        "user_id": user_id,
        "base_url": base_url,
    }


def _required(payload: dict[str, Any], key: str, limit: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"iLink 登录结果缺少 {key}")
    return value.strip()


def _safe_error(error: BaseException) -> str:
    text = str(error).strip()
    return (text or type(error).__name__)[:300]
