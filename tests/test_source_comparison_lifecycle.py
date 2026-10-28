"""Exercise comparison admission and shutdown through owned real subprocesses."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from test_runtime import make_runtime
from test_source_comparison_integration import arguments, saved

import muli.source_comparison as comparison
from muli.document_settings import DocumentSettings, DocumentSettingsStore
from muli.evidence import EvidenceError
from muli.runtime import Runtime
from muli.server import create_app


@dataclass
class PausedWorker:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    children: list[asyncio.subprocess.Process] = field(default_factory=list)


def paused_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PausedWorker:
    """Observe a real child's ready marker without stealing the runner's stdout."""
    worker = tmp_path / "comparison_lifecycle_worker.py"
    worker.write_text(
        "import sys, time\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(b'comparison-worker-entered\\n')\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    state = PausedWorker()
    marker = b"comparison-worker-entered\n"
    observed = bytearray()
    spawn = asyncio.create_subprocess_exec
    read = asyncio.StreamReader.read

    async def record(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        child = await spawn(*args, **kwargs)
        if str(worker) in args:
            state.children.append(child)
        return child

    async def observe(stream: asyncio.StreamReader, n: int = -1) -> bytes:
        chunk = await read(stream, n)
        if any(child.stdout is stream for child in state.children):
            observed.extend(chunk)
            if marker in observed:
                state.entered.set()
            del observed[: -len(marker)]
        return chunk

    monkeypatch.setattr(comparison, "_WORKER", worker)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    monkeypatch.setattr(asyncio.StreamReader, "read", observe)
    return state


def application(tmp_path: Path) -> tuple[Runtime, FastAPI]:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"enabled": False},
            "limits": {"max_parallel_agents": 1},
        }
    )
    return runtime, create_app(runtime.config, runtime=runtime)


async def test_service_capacity_close_reaps_worker_and_reconfigure_reopens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, app = application(tmp_path)
    service = app.state.memory_application
    left = saved(runtime, "a" * 32, "共同条款\n旧要求🙂\n")
    right = saved(runtime, "b" * 32, "共同条款\n新要求🙂\n")
    selected = arguments(left, right)
    module = comparison._WORKER
    worker = paused_worker(tmp_path, monkeypatch)
    request = asyncio.create_task(service.compare_sources(selected))
    try:
        await asyncio.wait_for(worker.entered.wait(), timeout=5)
        assert len(worker.children) == 1 and worker.children[0].returncode is None
        with pytest.raises(EvidenceError) as busy:
            await service.compare_sources(selected)
        assert busy.value.code == "comparison_busy"
        assert len(worker.children) == 1

        await asyncio.wait_for(service.close(), timeout=5)
        with pytest.raises(asyncio.CancelledError):
            await request
        assert all(child.returncode is not None for child in worker.children)
        assert not service._comparisons
        assert runtime.maintenance.admission.idle
        with pytest.raises(EvidenceError) as closed:
            await service.compare_sources(selected)
        assert closed.value.code == "comparison_changed"
        assert len(worker.children) == 1

        monkeypatch.setattr(comparison, "_WORKER", module)
        await service.reconfigure()
        page = await service.compare_sources(selected)
        assert page["status"] == "changed" and not page["has_more"]
        assert page["items"][0]["left"]["content"] == "旧要求🙂\n"
        assert page["items"][0]["right"]["content"] == "新要求🙂\n"
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await app.state.close()


async def test_api_close_reaps_inflight_worker_before_runtime_closes_and_rejects_new_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, app = application(tmp_path)
    left = saved(runtime, "a" * 32, "旧要求🙂\n")
    right = saved(runtime, "b" * 32, "新要求🙂\n")
    selected = arguments(left, right)
    worker = paused_worker(tmp_path, monkeypatch)
    closing_phase, release_close = asyncio.Event(), asyncio.Event()
    settle = runtime.maintenance.settle_activation

    async def block_later_cleanup() -> None:
        closing_phase.set()
        await release_close.wait()
        await settle()

    monkeypatch.setattr(runtime.maintenance, "settle_activation", block_later_cleanup)
    pending: asyncio.Task[httpx.Response] | None = None
    closing: asyncio.Task[None] | None = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1:8765",
    ) as client:
        try:
            pending = asyncio.create_task(client.post("/api/memory/sources/compare", json=selected))
            await asyncio.wait_for(worker.entered.wait(), timeout=5)
            assert len(worker.children) == 1 and worker.children[0].returncode is None
            closing = asyncio.create_task(app.state.close())
            await asyncio.wait_for(closing_phase.wait(), timeout=5)
            assert all(child.returncode is not None for child in worker.children)
            # The early shutdown gate must work while Store and Runtime are still usable.
            assert not runtime._closing
            assert runtime.store.db.execute("SELECT 1").fetchone()[0] == 1
            outcome = (
                await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=5)
            )[0]
            assert isinstance(outcome, asyncio.CancelledError) or (
                isinstance(outcome, httpx.Response) and outcome.status_code >= 400
            )
            rejected = await client.post("/api/memory/sources/compare", json=selected)
            assert rejected.status_code == 503, rejected.text
            assert rejected.json() == {
                "detail": "transaction_unavailable",
                "reason": "service_closing",
            }
            assert len(worker.children) == 1
            assert not app.state.memory_application._comparisons
        finally:
            release_close.set()
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await app.state.close()
            if closing is not None:
                await closing


@pytest.mark.parametrize("setting", ["runtime_limit", "document_policy"])
async def test_api_rejects_completed_worker_result_if_policy_changed_during_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    runtime, app = application(tmp_path)
    left = saved(runtime, "a" * 32, "共同条款\n旧要求🙂\n")
    right = saved(runtime, "b" * 32, "共同条款\n新要求🙂\n")
    selected = arguments(left, right)
    completed, release_result = asyncio.Event(), asyncio.Event()
    run_process = comparison.run_process
    results: list[dict[str, Any]] = []

    async def hold_real_result(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await run_process(*args, **kwargs)
        results.append(result)
        completed.set()
        await release_result.wait()
        return result

    monkeypatch.setattr(comparison, "run_process", hold_real_result)
    pending: asyncio.Task[httpx.Response] | None = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        try:
            pending = asyncio.create_task(client.post("/api/memory/sources/compare", json=selected))
            await asyncio.wait_for(completed.wait(), timeout=5)
            assert len(results) == 1 and results[0]["exit_code"] == 0
            if setting == "runtime_limit":
                previous = runtime.config.settings.limits.max_output_chars
                runtime.config.update({"limits": {"max_output_chars": previous - 1}})
            else:
                documents = DocumentSettingsStore(runtime.store)
                previous_documents = documents.read()
                documents.replace(
                    DocumentSettings.model_validate(
                        {
                            **previous_documents.model_dump(),
                            "timeout_seconds": previous_documents.timeout_seconds + 1,
                        }
                    )
                )
            release_result.set()
            rejected = await asyncio.wait_for(pending, timeout=5)
            assert rejected.status_code == 400, rejected.text
            assert rejected.json()["detail"]["code"] == "comparison_changed"
            assert "旧要求" not in rejected.text and "新要求" not in rejected.text
            refreshed = await client.post("/api/memory/sources/compare", json=selected)
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json()["status"] == "changed"
            assert len(results) == 2 and results[1]["exit_code"] == 0
        finally:
            release_result.set()
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await app.state.close()
