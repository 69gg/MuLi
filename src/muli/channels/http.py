"""有界渠道 HTTP；不跟随重定向，不把上游响应写进错误信息。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from muli.providers import ProviderError
from muli.providers.http import read_response
from muli.tools.web import public_url

from .types import ChannelError, ChannelProfile, valid_url


class ChannelHTTP:
    def __init__(self, client: httpx.AsyncClient, profile: ChannelProfile) -> None:
        self.client, self.profile = client, profile

    async def request(
        self,
        method: str,
        url: str,
        *,
        delivery: bool = False,
        limit: int | None = None,
        timeout: float | None = None,
        remote: bool = False,
        **kwargs: Any,
    ) -> tuple[bytes, dict[str, str]]:
        valid_url(url, allow_query=True)
        maximum = self.profile.max_response_bytes if limit is None else limit
        duration = self.profile.timeout if timeout is None else timeout
        try:
            async with asyncio.timeout(duration):
                if remote:
                    target, host, tls = await public_url(url)
                    original_headers = kwargs.get("headers", {})
                    kwargs["headers"] = {"Host": host}
                    if "Content-Type" in original_headers:
                        kwargs["headers"]["Content-Type"] = original_headers["Content-Type"]
                    kwargs["extensions"] = {"sni_hostname": tls}
                else:
                    target = httpx.URL(url)
                async with self.client.stream(
                    method,
                    target,
                    timeout=duration,
                    follow_redirects=False,
                    **kwargs,
                ) as response:
                    if not 200 <= response.status_code < 300:
                        retryable = response.status_code == 429 or (
                            response.status_code >= 500 and not delivery
                        )
                        raise ChannelError(
                            f"渠道 HTTP {response.status_code}",
                            retryable=retryable,
                            uncertain=delivery and response.status_code >= 500,
                        )
                    data = await read_response(response, maximum)
                    return data, dict(response.headers)
        except ChannelError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise ChannelError("渠道连接未建立", retryable=True) from None
        except (TimeoutError, httpx.HTTPError, ProviderError):
            raise ChannelError(
                "渠道请求超时或响应无效", retryable=not delivery, uncertain=delivery
            ) from None
        except (ValueError, OSError):
            raise ChannelError("渠道远程地址或响应无效") from None

    async def json(
        self, method: str, url: str, *, delivery: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        data, _ = await self.request(method, url, delivery=delivery, **kwargs)
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError):
            raise ChannelError("渠道返回无效 JSON", uncertain=delivery) from None
        if not isinstance(result, dict):
            raise ChannelError("渠道返回格式错误", uncertain=delivery)
        return result
