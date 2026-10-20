from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_runtime import make_runtime

from muli.config import ConfigFile
from muli.server import create_app


async def test_shutdown_stage_failure_restores_prior_configuration_and_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"updates": {"check_on_start": False}, "memory": {"auto_extract": False}})
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with app.router.lifespan_context(app):
        before = runtime.config.settings.model_dump()
        original = app.state.integration.close
        fail_once = True

        async def close() -> None:
            nonlocal fail_once
            await original()
            if fail_once:
                fail_once = False
                raise ValueError("injected close failure")

        monkeypatch.setattr(app.state.integration, "close", close)
        target = runtime.config.validate_changes({"persona": {"append": "candidate guidance"}})
        async with runtime.configuration_lock:
            with pytest.raises(ValueError, match="injected close failure"):
                await app.state.management.apply_settings(target)
        assert runtime.config.settings.model_dump() == before
        assert ConfigFile(runtime.config.path).settings.model_dump() == before
        assert app.state.attention.status()["running"] is True
        assert runtime.started and not runtime._closing


@pytest.mark.parametrize("stage", ["stop", "start"])
async def test_cancelled_configuration_observer_keeps_lock_until_transaction_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"updates": {"check_on_start": False}, "memory": {"auto_extract": False}})
    app = create_app(runtime.config, runtime.data_dir, runtime)
    entered, release = asyncio.Event(), asyncio.Event()
    async with app.router.lifespan_context(app):
        component = app.state.integration if stage == "stop" else runtime
        method = "close" if stage == "stop" else "reconfigure"
        original = getattr(component, method)

        async def paused() -> None:
            await original()
            entered.set()
            await release.wait()

        monkeypatch.setattr(component, method, paused)
        target = runtime.config.validate_changes({"persona": {"append": "accepted guidance"}})

        async def apply() -> None:
            async with runtime.configuration_lock:
                await app.state.management.apply_settings(target)

        task = asyncio.create_task(apply())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done()
            assert runtime.configuration_lock.locked()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime.config.settings.model_dump() == target.model_dump()
            assert ConfigFile(runtime.config.path).settings.model_dump() == target.model_dump()
            assert app.state.attention.status()["running"] is True
            assert not runtime.configuration_lock.locked()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
