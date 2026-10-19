"""Chiliarch 1.0 的出站 SSE 桥；业务持久化由调用者负责。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

JsonObject = dict[str, Any]


class ChiliarchError(Exception):
    """不包含凭据或远端响应正文的桥接错误。"""


class ChiliarchProtocolError(ChiliarchError):
    """认证、配置或协议错误；必须修正后再启动。"""


class ChiliarchConnectionError(ChiliarchError):
    """可以退避重连的网络或服务错误。"""


class _PersistenceError(ChiliarchConnectionError):
    """持久化失败时不 ACK，让平台按原消息身份重投。"""


@dataclass(frozen=True)
class ChiliarchConfig:
    base_url: str
    credential: str = field(default="", repr=False)
    agent_id: str = ""
    name: str = "MuLi"
    machine: str = ""
    capabilities: tuple[str, ...] = ()
    introduction: str = ""
    heartbeat_seconds: float = 15.0
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    request_timeout_seconds: float = 15.0
    stream_timeout_seconds: float = 60.0
    max_message_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.base_url)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
            )
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("chiliarch base_url 无效") from exc
        if not valid or any(char.isspace() for char in self.base_url):
            raise ValueError("chiliarch base_url 必须是无凭据、查询参数及片段的 HTTP(S) 地址")
        for value in (
            self.heartbeat_seconds,
            self.reconnect_initial_seconds,
            self.reconnect_max_seconds,
            self.request_timeout_seconds,
            self.stream_timeout_seconds,
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("chiliarch 时间限制必须是有限正数")
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise ValueError("最大重连等待不能小于初始等待")
        if self.max_message_bytes < 1024:
            raise ValueError("max_message_bytes 不能小于 1024")
        if not self.name.strip() or len(self.name) > 256:
            raise ValueError("chiliarch agent 名称必须为 1–256 个字符")
        if len(self.machine) > 256 or len(self.capabilities) > 256:
            raise ValueError("chiliarch machine 或 capabilities 超过协议限制")
        if not isinstance(self.introduction, str) or len(self.introduction) > 4000:
            raise ValueError("chiliarch introduction 必须是不超过 4000 字符的文本")


def _text(value: object, name: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ChiliarchProtocolError(f"协议字段 {name} 无效")
    return value


def _timestamp(value: object, name: str) -> str:
    text = _text(value, name, 128)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ChiliarchProtocolError(f"协议字段 {name} 不是 ISO 时间") from exc
    if parsed.tzinfo is None:
        raise ChiliarchProtocolError(f"协议字段 {name} 必须包含时区")
    return text


@dataclass(frozen=True)
class Envelope:
    """保持同一 ID、时间与内容重投；payload 为协议 JSON 对象。"""

    type: str
    payload: JsonObject
    id: str = field(default_factory=lambda: str(uuid4()))
    correlation_id: str | None = None
    protocol_version: str = "1.0"
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    expires_at: str | None = None

    def __post_init__(self) -> None:
        _text(self.id, "id")
        _text(self.type, "type")
        if self.protocol_version != "1.0":
            raise ChiliarchProtocolError("仅支持 chiliarch 协议 1.0")
        _timestamp(self.timestamp, "timestamp")
        if self.correlation_id is not None:
            _text(self.correlation_id, "correlation_id")
        if self.expires_at is not None:
            _timestamp(self.expires_at, "expires_at")
        if not isinstance(self.payload, dict):
            raise ChiliarchProtocolError("payload 必须是 JSON 对象")
        try:
            json.dumps(self.payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ChiliarchProtocolError("payload 必须包含有效 JSON 数据") from exc

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "type": self.type,
            "correlation_id": self.correlation_id,
            "protocol_version": self.protocol_version,
            "timestamp": self.timestamp,
            "expires_at": self.expires_at,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, value: object) -> Envelope:
        if not isinstance(value, dict):
            raise ChiliarchProtocolError("信封必须是 JSON 对象")
        allowed = {
            "id",
            "type",
            "correlation_id",
            "protocol_version",
            "timestamp",
            "expires_at",
            "payload",
        }
        if set(value) - allowed or not {"id", "type", "timestamp", "payload"} <= set(value):
            raise ChiliarchProtocolError("信封字段缺失或含未知字段")
        return cls(
            id=value["id"],
            type=value["type"],
            payload=value["payload"],
            correlation_id=value.get("correlation_id"),
            protocol_version=value.get("protocol_version", "1.0"),
            timestamp=value["timestamp"],
            expires_at=value.get("expires_at"),
        )


@dataclass(frozen=True)
class PairingResult:
    agent_id: str
    credential: str = field(repr=False)
    protocol_version: str = "1.0"


@dataclass(frozen=True)
class DeliveryReceipt:
    ack: Envelope | None
    duplicate: bool


@dataclass(frozen=True)
class _SSEFrame:
    event: str
    data: str


class _SSEDecoder:
    """支持 LF/CRLF/CR 与分块 UTF-8，并在累积行之前限制消息大小。"""

    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.buffer = bytearray()
        self.event = "message"
        self.data: list[str] = []
        self.size = 0
        self.skip_lf = False
        self.first_line = True

    def feed(self, chunk: bytes) -> list[_SSEFrame]:
        frames: list[_SSEFrame] = []
        for part in chunk.splitlines(keepends=True):
            if self.skip_lf:
                if part.startswith(b"\n"):
                    part = part[1:]
                self.skip_lf = False
            self.size += len(part)
            if self.size > self.maximum:
                raise ChiliarchProtocolError("SSE 消息超过 max_message_bytes")
            self.buffer.extend(part)
            if not part or part[-1:] not in {b"\r", b"\n"}:
                continue
            self.skip_lf = part.endswith(b"\r")
            raw = bytes(self.buffer).rstrip(b"\r\n")
            self.buffer.clear()
            try:
                line = raw.decode("utf-8-sig" if self.first_line else "utf-8")
            except UnicodeDecodeError as exc:
                raise ChiliarchProtocolError("SSE 数据不是 UTF-8") from exc
            self.first_line = False
            if not line:
                if self.data or self.event == "revoked":
                    frames.append(_SSEFrame(self.event, "\n".join(self.data)))
                self.event, self.data, self.size = "message", [], 0
            elif not line.startswith(":"):
                name, separator, value = line.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if name == "event":
                    self.event = value or "message"
                elif name == "data":
                    self.data.append(value)
        return frames


class ChiliarchBridge:
    """单出站连接；callback 返回代表已提交且可安全重放的业务接收。"""

    def __init__(
        self,
        config: ChiliarchConfig,
        on_envelope: Callable[[Envelope], Awaitable[None]],
        *,
        client: httpx.AsyncClient | None = None,
        heartbeat_metadata: Callable[[], JsonObject] | None = None,
    ) -> None:
        self.config = config
        self.on_envelope = on_envelope
        self.heartbeat_metadata = heartbeat_metadata
        self._injected_client = client
        self._active_client: httpx.AsyncClient | None = None
        self._stop_event = asyncio.Event()
        self._running = False
        self.status: Literal["stopped", "connecting", "connected", "retrying", "error"] = "stopped"
        self.last_error: str | None = None

    def _url(self, path: str) -> str:
        return self.config.base_url.rstrip("/") + path

    def _headers(self) -> dict[str, str]:
        if not self.config.credential:
            raise ChiliarchProtocolError("请先配对并保存 chiliarch agent 凭据")
        return {"Authorization": f"Bearer {self.config.credential}"}

    @contextlib.asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        client = self._active_client or self._injected_client
        if client is not None:
            yield client
        else:
            async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds) as owned:
                yield owned

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            raise ChiliarchConnectionError(f"chiliarch 暂时不可用（HTTP {response.status_code}）")
        raise ChiliarchProtocolError(f"chiliarch 请求被拒绝（HTTP {response.status_code}）")

    @staticmethod
    def _json(data: bytes) -> JsonObject:
        try:
            value: object = json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChiliarchProtocolError("chiliarch 返回了无效 JSON") from exc
        if not isinstance(value, dict):
            raise ChiliarchProtocolError("chiliarch 响应必须是 JSON 对象")
        return value

    async def _post(
        self, path: str, body: bytes, headers: dict[str, str] | None = None
    ) -> JsonObject:
        if len(body) > self.config.max_message_bytes:
            raise ChiliarchProtocolError("上行信封超过 max_message_bytes")
        async with (
            self._http() as client,
            client.stream(
                "POST",
                self._url(path),
                content=body,
                headers={"Content-Type": "application/json", **(headers or {})},
                timeout=self.config.request_timeout_seconds,
                follow_redirects=False,
            ) as response,
        ):
            self._check(response)
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > self.config.max_message_bytes:
                    raise ChiliarchProtocolError("chiliarch 响应超过 max_message_bytes")
                data.extend(chunk)
            return self._json(bytes(data))

    async def pair(self, code: str) -> PairingResult:
        """消费一次性配对码；调用方必须保存返回的凭据后再启动 run。"""
        if self._running:
            raise ChiliarchProtocolError("请先停止桥接器再更换身份")
        if not code or len(code) > 256 or not self.config.machine.strip():
            raise ValueError("配对需要有效 code 与 machine")
        value = await self._post(
            "/api/agent/pair",
            json.dumps(
                {
                    "code": code,
                    "name": self.config.name,
                    "machine": self.config.machine,
                    "capabilities": list(self.config.capabilities),
                },
                ensure_ascii=False,
            ).encode(),
        )
        if value.get("protocol_version") != "1.0":
            raise ChiliarchProtocolError("配对返回了不支持的协议版本")
        result = PairingResult(
            _text(value.get("agent_id"), "agent_id"),
            _text(value.get("credential"), "credential", 4096),
        )
        self.config = replace(self.config, agent_id=result.agent_id, credential=result.credential)
        return result

    async def send(self, envelope: Envelope) -> DeliveryReceipt:
        """只尝试一次；业务出站应先持久化信封，并以原 ID/内容重试。"""
        body = json.dumps(envelope.to_dict(), ensure_ascii=False, allow_nan=False).encode()
        value = await self._post("/api/agent/messages", body, self._headers())
        if not isinstance(value.get("duplicate"), bool) or "ack" not in value:
            raise ChiliarchProtocolError("上行确认格式无效")
        ack = Envelope.from_dict(value["ack"]) if value["ack"] is not None else None
        if envelope.type == "ack":
            if ack is not None:
                raise ChiliarchProtocolError("业务 ACK 不应收到另一个 ACK")
        elif ack is None or ack.type != "ack" or ack.payload.get("message_id") != envelope.id:
            raise ChiliarchProtocolError("上行 ACK 与发送的信封不匹配")
        return DeliveryReceipt(ack=ack, duplicate=value["duplicate"])

    async def _heartbeat(self) -> None:
        while not self._stop_event.is_set():
            metadata = dict(self.heartbeat_metadata() if self.heartbeat_metadata else {})
            metadata["introduction"] = self.config.introduction
            await self.send(
                Envelope(
                    type="heartbeat",
                    payload={
                        "status": "ready",
                        "metadata": metadata,
                    },
                )
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), self.config.heartbeat_seconds)
            except TimeoutError:
                pass

    async def _read(self, client: httpx.AsyncClient) -> None:
        timeout = httpx.Timeout(
            self.config.request_timeout_seconds, read=self.config.stream_timeout_seconds
        )
        async with client.stream(
            "GET",
            self._url("/api/agent/events"),
            headers={
                **self._headers(),
                "Accept": "text/event-stream",
                "Accept-Encoding": "identity",
            },
            timeout=timeout,
            follow_redirects=False,
        ) as response:
            self._check(response)
            if (
                response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "text/event-stream"
            ):
                raise ChiliarchProtocolError("chiliarch 未返回 SSE 数据流")
            self.status, self.last_error = "connected", None
            decoder = _SSEDecoder(self.config.max_message_bytes)
            async for chunk in response.aiter_bytes():
                for frame in decoder.feed(chunk):
                    if frame.event == "revoked":
                        raise ChiliarchProtocolError("chiliarch agent 凭据已吊销")
                    if frame.event != "message":
                        continue
                    try:
                        envelope = Envelope.from_dict(json.loads(frame.data))
                    except (ValueError, RecursionError) as exc:
                        raise ChiliarchProtocolError("SSE 信封格式无效") from exc
                    try:
                        await self.on_envelope(envelope)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise _PersistenceError("消息持久化失败，未发送 ACK") from exc
                    await self.send(
                        Envelope(
                            type="ack",
                            correlation_id=envelope.id,
                            payload={"message_id": envelope.id},
                        )
                    )
        raise ChiliarchConnectionError("chiliarch SSE 连接已结束")

    async def _connection(self, client: httpx.AsyncClient) -> None:
        tasks = [
            asyncio.create_task(self._read(client)),
            asyncio.create_task(self._heartbeat()),
            asyncio.create_task(self._stop_event.wait()),
        ]
        try:
            completed, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self) -> None:
        """运行至 stop/取消；认证或协议错误抛出，网络故障退避重连。"""
        if self._running:
            raise RuntimeError("chiliarch 桥接器已在运行")
        self._headers()
        self._running = True
        self._stop_event.clear()
        delay = self.config.reconnect_initial_seconds
        try:
            async with self._http() as client:
                self._active_client = client
                while not self._stop_event.is_set():
                    self.status = "connecting"
                    started_at = asyncio.get_running_loop().time()
                    try:
                        await self._connection(client)
                    except (httpx.TransportError, ChiliarchConnectionError) as exc:
                        self.last_error = (
                            str(exc)
                            if isinstance(exc, ChiliarchError)
                            else "chiliarch 网络连接失败"
                        )
                        if self._stop_event.is_set():
                            break
                        self.status = "retrying"
                        if (
                            asyncio.get_running_loop().time() - started_at
                            >= self.config.heartbeat_seconds
                        ):
                            delay = self.config.reconnect_initial_seconds
                        wait = min(
                            self.config.reconnect_max_seconds,
                            delay * random.uniform(0.8, 1.2),
                        )
                        try:
                            await asyncio.wait_for(self._stop_event.wait(), wait)
                        except TimeoutError:
                            pass
                        delay = min(self.config.reconnect_max_seconds, delay * 2)
                    except ChiliarchProtocolError as exc:
                        self.status, self.last_error = "error", str(exc)
                        raise
        finally:
            self._active_client = None
            self._running = False
            if self.status != "error":
                self.status = "stopped"

    def stop(self) -> None:
        """唤醒并取消当前流与心跳；调用方随后 await run 的 task。"""
        self._stop_event.set()


__all__ = [
    "ChiliarchBridge",
    "ChiliarchConfig",
    "ChiliarchConnectionError",
    "ChiliarchError",
    "ChiliarchProtocolError",
    "DeliveryReceipt",
    "Envelope",
    "PairingResult",
]
