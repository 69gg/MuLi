"""使用 HTTPX 的异步模型客户端。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Self

import httpx

from .codecs import build_request, parse_turn
from .http import read_response
from .streaming import StreamAccumulator, sse_events
from .types import (
    JsonObject,
    Message,
    ModelConfig,
    ModelTurn,
    ProviderError,
    StreamEvent,
)

_ENDPOINTS = {
    "openai_chat": "/chat/completions",
    "openai_responses": "/responses",
    "anthropic": "/messages",
}


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if not isinstance(date, datetime):
                return None
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return max(0.0, (date - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


async def _read_json(response: httpx.Response, limit: int) -> JsonObject:
    data = await read_response(response, limit)
    try:
        payload: Any = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ProviderError("上游模型返回无效 JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderError("上游模型响应必须是 JSON 对象")
    return payload


def _turn_events(turn: ModelTurn) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    if turn.reasoning:
        events.append(StreamEvent("reasoning_delta", text=turn.reasoning))
    if turn.content:
        events.append(StreamEvent("text_delta", text=turn.content))
    events.extend(
        StreamEvent("tool_call_delta", tool_call=call, index=index)
        for index, call in enumerate(turn.tool_calls)
    )
    return events


class ModelClient:
    """注入的 AsyncClient 由调用方关闭；内部创建的连接由本类关闭。"""

    def __init__(self, config: ModelConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient()
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        self._closed = True

    def _headers(self) -> httpx.Headers:
        try:
            headers = httpx.Headers(
                {
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream" if self.config.stream else "application/json",
                }
            )
            if self.config.protocol == "anthropic":
                headers["anthropic-version"] = "2023-06-01"
                if self.config.api_key:
                    headers["x-api-key"] = self.config.api_key
            elif self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            headers.update(self.config.extra_headers)
            return headers
        except (ValueError, UnicodeError):
            raise ProviderError("模型请求头配置无效") from None

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[JsonObject] = (),
        *,
        attempt_guard: Callable[[], None] | None = None,
    ) -> ModelTurn:
        """stream 配置控制上游请求方式，调用方始终获得完整回合。"""
        events = (
            self.stream(messages, tools)
            if attempt_guard is None
            else self.stream(messages, tools, attempt_guard=attempt_guard)
        )
        async for event in events:
            if event.kind == "done" and event.turn is not None:
                return event.turn
        raise ProviderError("模型没有返回完整回合")

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[JsonObject] = (),
        *,
        attempt_guard: Callable[[], None] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """done 前不执行工具；任何对外增量发出后均不自动重试。"""
        if self._closed:
            raise RuntimeError("模型客户端已经关闭")
        body = build_request(self.config, messages, tools, stream=self.config.stream)
        url = self.config.base_url.rstrip("/") + _ENDPOINTS[self.config.protocol]
        emitted = False
        for attempt in range(self.config.max_retries + 1):
            # A rejected source is not an upstream failure and must never retry.
            if attempt_guard is not None:
                attempt_guard()
            error: ProviderError
            try:
                async with self._client.stream(
                    "POST",
                    url,
                    json=body,
                    headers=self._headers(),
                    timeout=self.config.timeout,
                    follow_redirects=False,
                ) as response:
                    if response.status_code >= 300:
                        status = response.status_code
                        raise ProviderError(
                            f"模型请求失败（HTTP {status}）",
                            status_code=status,
                            retryable=status in {408, 409, 425, 429} or status >= 500,
                            retry_after=_retry_after(response),
                        )
                    if (
                        self.config.stream
                        and "text/event-stream" in response.headers.get("content-type", "").lower()
                    ):
                        accumulator = StreamAccumulator(self.config.protocol)
                        async for item in sse_events(response, self.config.max_response_bytes):
                            for event in accumulator.feed(item):
                                emitted = True
                                yield event
                            if accumulator.finished:
                                break
                        turn = accumulator.finish()
                    else:
                        turn = parse_turn(
                            await _read_json(response, self.config.max_response_bytes),
                            self.config.protocol,
                        )
                        for event in _turn_events(turn):
                            emitted = True
                            yield event
                yield StreamEvent("done", turn=turn)
                return
            except httpx.HTTPError:
                error = ProviderError("模型连接或传输失败", retryable=True, partial=emitted)
            except ProviderError as exc:
                error = exc
                error.partial = emitted
            if emitted or not error.retryable or attempt >= self.config.max_retries:
                raise error from None
            delay = (
                error.retry_after
                if error.retry_after is not None
                else self.config.retry_backoff * (2**attempt)
            )
            await asyncio.sleep(min(delay, self.config.timeout))
