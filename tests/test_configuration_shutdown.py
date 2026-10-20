from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_runtime import make_runtime

from muli.server import create_app


@pytest.fixture(autouse=True)
def isolated_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MULI_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MULI_CONFIG", raising=False)
    monkeypatch.delenv("MULI_DATA_DIR", raising=False)
    yield


async def test_shutdown_settles_management_before_final_attention_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"updates": {"check_on_start": False}, "memory": {"auto_extract": False}})
    app = create_app(runtime.config, runtime.data_dir, runtime)
    scope = app.router.lifespan_context(app)
    await scope.__aenter__()
    entered, release, management_closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_stop = app.state.integration.close
    original_management_close = app.state.management.close
    first_stop = True

    async def paused_stop() -> None:
        nonlocal first_stop
        await original_stop()
        if first_stop:
            first_stop = False
            entered.set()
            await release.wait()

    async def observed_management_close() -> None:
        management_closing.set()
        await original_management_close()

    monkeypatch.setattr(app.state.integration, "close", paused_stop)
    monkeypatch.setattr(app.state.management, "close", observed_management_close)
    shutdown: asyncio.Task[Any] | None = None
    try:
        await app.state.management.stage("configure", {"persona": {"append": "accepted change"}})
        await asyncio.wait_for(entered.wait(), 2)
        shutdown = asyncio.create_task(scope.__aexit__(None, None, None))
        await asyncio.wait_for(management_closing.wait(), 2)
        assert runtime.configuration_lock.locked()
        release.set()
        await asyncio.wait_for(asyncio.shield(shutdown), 2)
        assert app.state.attention._task is None, "配置事务在最终关闭之后重新启动了 attention"
        assert app.state.attention._closing is True
        assert app.state.management._worker is None
    finally:
        release.set()
        if shutdown is None:
            await scope.__aexit__(None, None, None)
        else:
            await asyncio.gather(shutdown, return_exceptions=True)
        # The old-order counterexample deliberately leaves this task alive;
        # clean it without touching the already closed Store.
        await app.state.attention.close()


class ObservedShutdownLock(asyncio.Lock):
    def __init__(self) -> None:
        super().__init__()
        self._armed = False
        self.shutdown_waiting = asyncio.Event()

    def arm(self) -> None:
        self._armed = True

    async def acquire(self) -> bool:
        if self._armed and self.locked():
            self._armed = False
            self.shutdown_waiting.set()
        return await super().acquire()


async def test_shutdown_waits_for_existing_api_configuration_before_closing_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"updates": {"check_on_start": False}, "memory": {"auto_extract": False}})
    lock = ObservedShutdownLock()
    runtime.configuration_lock = lock
    app = create_app(runtime.config, runtime.data_dir, runtime)
    scope = app.router.lifespan_context(app)
    await scope.__aenter__()
    entered, release = asyncio.Event(), asyncio.Event()
    premature_store_close, permit_store_close = asyncio.Event(), asyncio.Event()
    original_reconfigure, original_close = runtime.reconfigure, runtime.close

    async def paused_reconfigure() -> None:
        await original_reconfigure()
        entered.set()
        await release.wait()

    async def guarded_close() -> None:
        if not release.is_set():
            premature_store_close.set()
        # Keep the old-order counterexample's Store open until cleanup can
        # settle the API transaction and retire all newly restarted resources.
        await permit_store_close.wait()
        await original_close()

    monkeypatch.setattr(runtime, "reconfigure", paused_reconfigure)
    monkeypatch.setattr(runtime, "close", guarded_close)
    target = runtime.config.validate_changes({"persona": {"append": "API accepted change"}})

    async def apply() -> None:
        async with lock:
            await app.state.management.apply_settings(target)

    applying = asyncio.create_task(apply())
    shutdown: asyncio.Task[Any] | None = None
    probes: list[asyncio.Task[bool]] = []
    try:
        await asyncio.wait_for(entered.wait(), 2)
        lock.arm()
        shutdown = asyncio.create_task(scope.__aexit__(None, None, None))
        probes = [
            asyncio.create_task(lock.shutdown_waiting.wait()),
            asyncio.create_task(premature_store_close.wait()),
        ]
        completed, _ = await asyncio.wait(probes, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert completed, "关机没有进入配置交接屏障或最终资源关闭"
        assert lock.shutdown_waiting.is_set(), "关机未等待既有 API 配置事务便开始关闭 Store"
        assert not premature_store_close.is_set()
        assert not applying.done() and lock.locked()
        unchanged_settings = runtime.config.settings
        persisted_configuration = runtime.config.path.read_bytes()
        rejected = runtime.config.validate_changes(
            {"persona": {"append": "rejected after shutdown"}}
        )
        with pytest.raises(ValueError, match="服务正在关闭"):
            await app.state.management.apply_settings(rejected)
        assert runtime.config.settings is unchanged_settings
        assert runtime.config.path.read_bytes() == persisted_configuration
        release.set()
        await asyncio.wait_for(asyncio.shield(applying), 2)
        permit_store_close.set()
        await asyncio.wait_for(asyncio.shield(shutdown), 2)
        assert app.state.attention._task is None
        assert app.state.attention._closing is True
    finally:
        release.set()
        await asyncio.gather(applying, return_exceptions=True)
        permit_store_close.set()
        if shutdown is None:
            await scope.__aexit__(None, None, None)
        else:
            await asyncio.gather(shutdown, return_exceptions=True)
        for probe in probes:
            probe.cancel()
        await asyncio.gather(*probes, return_exceptions=True)
        await app.state.attention.close()
