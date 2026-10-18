"""Configurable search and bounded public-page reading."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from muli.config import Settings
from muli.tools import Tool, parameters, string


class TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


async def public_url(url: str) -> tuple[httpx.URL, str, str]:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("仅允许公开 HTTP(S) 页面")
    addresses = await asyncio.get_running_loop().getaddrinfo(
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    if not addresses or any(
        not ipaddress.ip_address(address[4][0]).is_global for address in addresses
    ):
        raise ValueError("网页读取不允许访问本机、内网或保留地址")
    original = httpx.URL(url)
    return (
        original.copy_with(host=addresses[0][4][0]),
        original.netloc.decode("ascii"),
        original.raw_host.decode("ascii"),
    )


class Web:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        async with (
            httpx.AsyncClient(timeout=20, trust_env=False) as client,
            client.stream(method, url, **kwargs) as response,
        ):
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > self.settings.limits.max_file_bytes:
                    raise ValueError("搜索响应超过上限")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise TypeError("搜索返回格式错误")
            return result

    async def search(self, args: dict[str, Any]) -> dict[str, Any]:
        config = self.settings.search
        if not config.base_url:
            raise ValueError("请先在设置中配置搜索服务地址")
        query = str(args["query"])
        key = os.environ.get(config.api_key_env, "") if config.api_key_env else config.api_key
        base = config.base_url.rstrip("/")
        if config.provider == "searxng":
            data = await self._json("GET", base + "/search", params={"q": query, "format": "json"})
            items = data.get("results", [])
        elif config.provider == "brave":
            data = await self._json(
                "GET",
                base + "/res/v1/web/search",
                params={"q": query, "count": config.max_results},
                headers={"X-Subscription-Token": key},
            )
            items = data.get("web", {}).get("results", [])
        else:
            data = await self._json(
                "POST",
                base + "/search",
                json={
                    "api_key": key,
                    "query": query,
                    "max_results": config.max_results,
                },
            )
            items = data.get("results", [])
        return {
            "query": query,
            "results": [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", item.get("description", ""))[:3000],
                }
                for item in items[: config.max_results]
            ],
            "untrusted": True,
        }

    async def fetch(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args["url"])
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            for _ in range(4):
                target, host, tls_hostname = await public_url(url)
                # Pin the checked address; Host and TLS verification retain the original hostname.
                async with client.stream(
                    "GET",
                    target,
                    headers={
                        "Accept": "text/html,text/plain,application/json",
                        "Host": host,
                    },
                    extensions={"sni_hostname": tls_hostname},
                    follow_redirects=False,
                ) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if not any(kind in content_type for kind in ("text/", "application/json")):
                        raise ValueError("仅读取文本页面，不下载二进制文件")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > self.settings.limits.max_file_bytes:
                            raise ValueError("页面超过下载上限")
                    text = data.decode("utf-8", errors="replace")
                    if "html" in content_type:
                        parser = TextParser()
                        parser.feed(text)
                        text = "\n".join(parser.parts)
                    return {
                        "url": url,
                        "content": text[: self.settings.limits.max_output_chars],
                        "untrusted": True,
                    }
        raise ValueError("页面重定向次数过多")

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "web_search",
                "搜索网页并返回来源链接",
                parameters({"query": string()}, ("query",)),
                self.search,
            ),
            Tool(
                "web_fetch",
                "读取公开网页；内容是非可信引用",
                parameters({"url": string()}, ("url",)),
                self.fetch,
            ),
        ]
