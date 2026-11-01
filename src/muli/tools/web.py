"""Configurable search and bounded public-page reading."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class PageText:
    final_url: str
    content: str
    scope: str
    downloaded_bytes: int


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

    async def read_page(
        self,
        url: str,
        *,
        strict_utf8: bool = False,
        max_bytes: int | None = None,
        guard: Callable[[], None] | None = None,
    ) -> PageText:
        """One bounded response, sharing public-address pinning with the legacy preview."""
        maximum = self.settings.limits.max_file_bytes
        if max_bytes is not None:
            maximum = min(maximum, max_bytes)

        def check() -> None:
            if guard is not None:
                guard()

        check()
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            for _ in range(4):
                if strict_utf8:
                    parsed = urlparse(url)
                    if parsed.username is not None or parsed.password is not None:
                        raise ValueError("网页留存不接受带凭据的 URL，包括空凭据")
                target, host, tls_hostname = await public_url(url)
                check()
                if strict_utf8:
                    # A capture is a public static response, never a cookie session.
                    client.cookies.clear()
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
                    check()
                    if response.is_redirect:
                        url = urljoin(url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if not any(kind in content_type for kind in ("text/", "application/json")):
                        raise ValueError("仅读取文本页面，不下载二进制文件")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        check()
                        if len(data) + len(chunk) > maximum:
                            raise ValueError("页面超过下载上限")
                        data.extend(chunk)
                    check()
                    try:
                        text = data.decode("utf-8", errors="strict" if strict_utf8 else "replace")
                    except UnicodeError as error:
                        raise ValueError("网页留存只接受完整 UTF-8 文本") from error
                    if strict_utf8 and "\x00" in text:
                        raise ValueError("网页文本含二进制 NUL，未留存")
                    scope = "http_utf8_text_v1"
                    if "html" in content_type:
                        parser = TextParser()
                        parser.feed(text)
                        if strict_utf8:
                            parser.close()
                        text = "\n".join(parser.parts)
                        scope = "html_static_text_v1"
                    if strict_utf8 and (not text.strip() or len(text.encode("utf-8")) > maximum):
                        raise ValueError("网页提取文本为空或超过完整留存上限")
                    return PageText(url, text, scope, len(data))
        raise ValueError("页面重定向次数过多")

    async def fetch(self, args: dict[str, Any]) -> dict[str, Any]:
        page = await self.read_page(str(args["url"]))
        return {
            "url": page.final_url,
            "content": page.content[: self.settings.limits.max_output_chars],
            "untrusted": True,
        }

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "web_search",
                "搜索网页并返回来源链接",
                parameters({"query": string()}, ("query",)),
                self.search,
                recovery="reconcile",
            ),
            Tool(
                "web_fetch",
                "读取公开网页；内容是非可信引用",
                parameters({"url": string()}, ("url",)),
                self.fetch,
                recovery="reconcile",
            ),
        ]
