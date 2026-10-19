"""HTTP 响应共用的有界读取。"""

from __future__ import annotations

import httpx

from .types import ProviderError


async def read_response(response: httpx.Response, limit: int) -> bytes:
    """按解压后的字节数计费上限，不信任 Content-Length。"""
    data = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=65536):
        if len(data) + len(chunk) > limit:
            raise ProviderError("模型响应超过 max_response_bytes 限制")
        data.extend(chunk)
    return bytes(data)
