from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from test_mcp import FakeClient, settings, stdio_settings

from muli.tools.mcp import MCPLimits, MCPManager, MCPPrepareError


@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_only_published_tokens_can_call_and_catalog_refresh_revokes_old_handlers(
    transport: str,
) -> None:
    client = FakeClient()
    config = settings() if transport == "http" else stdio_settings()
    manager = MCPManager(config, client_factory=lambda *_: client)
    try:
        old = (await manager.start())[0]
        candidate = await manager.prepare(manager.config, refresh={"test"})
        assert (await candidate.tools[0].handler({"q": "未发布"}))["code"] == "generation_changed"
        assert not (await old.handler({"q": "仍获授权"}))["is_error"]
        assert manager.publish(candidate) == ()
        assert (await old.handler({"q": "旧目录"}))["status"] == "not_executed"
        assert (await old.handler({"q": "旧目录"}))["code"] == "generation_changed"
        assert not (await manager.tools[0].handler({"q": "新目录"}))["is_error"]
        assert len(client.calls) == 2 and client.closed == 0 and client.entered == 1
    finally:
        await manager.close()


@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_discard_or_failed_candidate_keeps_the_original_token_and_bounds_preparation(
    transport: str,
) -> None:
    old, new = FakeClient(), FakeClient(failure=True)
    clients = iter((old, new))
    config = settings() if transport == "http" else stdio_settings()
    target = (
        settings(url="https://unused.invalid/new")
        if transport == "http"
        else stdio_settings(args=["changed.py"])
    )
    manager = MCPManager(config, client_factory=lambda *_: next(clients))
    try:
        tool = (await manager.start())[0]
        candidate = await manager.prepare(manager.config)
        with pytest.raises(MCPPrepareError, match="candidate_pending"):
            await manager.prepare(target)
        assert new.entered == 0
        await manager.discard(candidate)
        assert old.closed == 0 and not (await tool.handler({"q": "丢弃后"}))["is_error"]
        with pytest.raises(MCPPrepareError):
            await manager.prepare(target)
        assert new.closed == 1 and old.closed == 0
        assert not (await tool.handler({"q": "候选失败后"}))["is_error"]
    finally:
        await manager.close()


@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_cancelled_catalog_candidate_settles_and_releases_the_next_preparation(
    transport: str,
) -> None:
    listing, closing, finish_close = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class CancelledCandidate(FakeClient):
        async def list_tools(self) -> list[Any]:
            listing.set()
            await asyncio.Event().wait()
            raise AssertionError("the cancelled catalog cannot finish")

        async def close(self) -> None:
            closing.set()
            await finish_close.wait()
            await super().close()

    old, cancelled, replacement = FakeClient(), CancelledCandidate(), FakeClient(names=["new"])
    clients = iter((old, cancelled, replacement))
    config = settings() if transport == "http" else stdio_settings()
    manager = MCPManager(config, client_factory=lambda *_: next(clients))
    preparing: asyncio.Task[Any] | None = None
    try:
        tool = (await manager.start())[0]
        target = (
            settings(url="https://unused.invalid/new")
            if transport == "http"
            else stdio_settings(args=["changed.py"])
        )
        preparing = asyncio.create_task(manager.prepare(target))
        await asyncio.wait_for(listing.wait(), 2)
        preparing.cancel()
        await asyncio.wait_for(closing.wait(), 2)
        assert not preparing.done() and manager._candidate is not None
        assert not (await tool.handler({"q": "候选取消，旧代仍可用"}))["is_error"]
        assert old.closed == 0 and cancelled.closed == 0
        with pytest.raises(MCPPrepareError, match="generation_draining"):
            await manager.prepare(target)
        finish_close.set()
        with pytest.raises(asyncio.CancelledError):
            await preparing
        assert cancelled.closed == cancelled.exited == 1
        assert manager._candidate is None and manager.pending_cleanup == 0
        assert not (await tool.handler({"q": "候选清理完成，旧代仍可用"}))["is_error"]
        retired = manager.publish(await manager.prepare(target))
        assert not (await manager.tools[0].handler({"q": "重新准备"}))["is_error"]
        assert replacement.entered == 1 and replacement.calls[0]["name"] == "new"
        await manager.retire(retired)
    finally:
        finish_close.set()
        if preparing is not None:
            preparing.cancel()
            await asyncio.gather(preparing, return_exceptions=True)
        await manager.close()


@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_two_admitted_calls_drain_before_close_and_prevent_a_third_client_set(
    transport: str,
) -> None:
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]

    class Gated(FakeClient):
        async def call_tool(
            self, name: str, arguments: dict[str, Any], *, timeout: float, raise_on_error: bool
        ) -> Any:
            index = arguments["index"]
            entered[index].set()
            await release[index].wait()
            return await super().call_tool(
                name, arguments, timeout=timeout, raise_on_error=raise_on_error
            )

    old, new = Gated(), FakeClient()
    factory_calls: list[bool] = []

    def factory(source: dict[str, Any], _: float) -> FakeClient:
        factory_calls.append(True)
        return old if len(factory_calls) == 1 else new

    config = settings() if transport == "http" else stdio_settings()
    target = (
        settings(url="https://unused.invalid/new")
        if transport == "http"
        else stdio_settings(args=["changed.py"])
    )
    third = (
        settings(url="https://unused.invalid/third")
        if transport == "http"
        else stdio_settings(args=["third.py"])
    )
    manager = MCPManager(config, client_factory=factory)
    tasks: list[asyncio.Task[Any]] = []
    try:
        tool = (await manager.start())[0]
        tasks = [asyncio.create_task(tool.handler({"index": index})) for index in range(2)]
        async with asyncio.timeout(2):
            await asyncio.gather(*(item.wait() for item in entered))
        candidate = await manager.prepare(target)
        retired = manager.publish(candidate)
        assert manager.draining == 1 and manager.pending_cleanup == 1 and old.closed == 0
        assert (await tool.handler({"index": 0}))["code"] == "generation_changed"
        with pytest.raises(MCPPrepareError, match="generation_draining"):
            await manager.prepare(third)
        assert len(factory_calls) == 2
        assert not (await manager.tools[0].handler({"q": "新代继续服务"}))["is_error"]
        release[0].set()
        await asyncio.wait_for(tasks[0], 2)
        assert old.closed == 0 and manager.draining == 1
        release[1].set()
        await asyncio.wait_for(tasks[1], 2)
        assert await asyncio.wait_for(manager.retire(retired), 2)
        assert old.closed == old.exited == 1 and manager.pending_cleanup == 0
        assert len(old.calls) == 2 and new.closed == 0
        candidate = await manager.prepare(manager.config)
        assert manager.publish(candidate) == ()
        assert len(factory_calls) == 2
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()


@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_delayed_result_keeps_old_secret_redaction_and_old_output_limit(
    transport: str,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    secret = "old-fixture-secret"

    class TextValue:
        def __str__(self) -> str:
            return secret

    result = SimpleNamespace(
        is_error=False,
        content=[SimpleNamespace(type="text", text=f"Bearer {secret} " + "值" * 300)],
        structured_content={"nested": [secret, TextValue()], TextValue(): "same-policy"},
    )

    class Gated(FakeClient):
        async def call_tool(
            self, name: str, arguments: dict[str, Any], *, timeout: float, raise_on_error: bool
        ) -> Any:
            entered.set()
            await release.wait()
            return await super().call_tool(
                name, arguments, timeout=timeout, raise_on_error=raise_on_error
            )

    new_secret = "new-fixture-secret"
    old = Gated(result=result)
    new = FakeClient(
        result=SimpleNamespace(
            is_error=False,
            content=[SimpleNamespace(type="text", text=new_secret)],
            structured_content=None,
        )
    )
    clients = iter((old, new))
    config = (
        settings(headers={"Authorization": f"Bearer {secret}"})
        if transport == "http"
        else stdio_settings(env={"TOKEN": secret})
    )
    target = (
        settings(url="https://unused.invalid/new", headers={"Authorization": new_secret})
        if transport == "http"
        else stdio_settings(env={"TOKEN": new_secret})
    )
    manager = MCPManager(
        config,
        max_output_chars=512,
        client_factory=lambda *_: next(clients),
    )
    task: asyncio.Task[Any] | None = None
    try:
        tool = (await manager.start())[0]
        task = asyncio.create_task(tool.handler({"q": "迟到"}))
        await asyncio.wait_for(entered.wait(), 2)
        candidate = await manager.prepare(target, limits=MCPLimits(max_output_chars=128))
        retired = manager.publish(candidate)
        current = await manager.tools[0].handler({"q": "新代脱敏"})
        assert current["content"][0]["text"] == "••••"
        assert len(json.dumps(current, ensure_ascii=False)) <= 128
        release.set()
        value = await asyncio.wait_for(task, 2)
        encoded = json.dumps(value, ensure_ascii=False)
        assert secret not in encoded and "••••" in encoded
        assert 128 < len(encoded) <= 512 and not value.get("truncated", False)
        assert value["structured_content"] == {"nested": ["••••", "••••"], "••••": "same-policy"}
        await manager.retire(retired)
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()


@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_cancelled_sdk_must_settle_before_retired_connection_closes(transport: str) -> None:
    entered, cancelling, settle = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Gated(FakeClient):
        async def call_tool(
            self, name: str, arguments: dict[str, Any], *, timeout: float, raise_on_error: bool
        ) -> Any:
            self.calls.append({"name": name})
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await settle.wait()
                raise

    old, new = Gated(), FakeClient()
    clients = iter((old, new))
    config = settings() if transport == "http" else stdio_settings()
    target = (
        settings(url="https://unused.invalid/new")
        if transport == "http"
        else stdio_settings(args=["changed.py"])
    )
    manager = MCPManager(config, client_factory=lambda *_: next(clients))
    task: asyncio.Task[Any] | None = None
    try:
        tool = (await manager.start())[0]
        task = asyncio.create_task(tool.handler({"q": "取消"}))
        await asyncio.wait_for(entered.wait(), 2)
        retired = manager.publish(await manager.prepare(target))
        task.cancel()
        await asyncio.wait_for(cancelling.wait(), 2)
        assert manager.draining == 1 and old.closed == 0
        settle.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(manager.retire(retired), 2)
        assert manager.draining == 0 and old.closed == 1 and len(old.calls) == 1
    finally:
        settle.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()


async def test_shutdown_revokes_new_calls_without_closing_an_admitted_call() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Gated(FakeClient):
        async def call_tool(
            self, name: str, arguments: dict[str, Any], *, timeout: float, raise_on_error: bool
        ) -> Any:
            entered.set()
            await release.wait()
            return self.result

    client = Gated()
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    task: asyncio.Task[Any] | None = None
    closing: asyncio.Task[None] | None = None
    try:
        tool = (await manager.start())[0]
        task = asyncio.create_task(tool.handler({"q": "在途"}))
        await asyncio.wait_for(entered.wait(), 2)
        closing = asyncio.create_task(manager.close())
        async with asyncio.timeout(2):
            while not manager._closed:
                await asyncio.sleep(0)
        denied = await tool.handler({"q": "关闭后"})
        assert denied["status"] == "not_executed" and denied["code"] == "manager_closed"
        assert client.closed == 0 and not closing.done()
        release.set()
        assert not (await task)["is_error"]
        await asyncio.wait_for(closing, 2)
        assert client.closed == client.exited == 1
    finally:
        release.set()
        await asyncio.gather(*(item for item in (task, closing) if item), return_exceptions=True)
        await manager.close()
