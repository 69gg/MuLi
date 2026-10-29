"""A real memory job must recheck its source before a provider retry POST."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import FakeClient

from muli.attachment_memory_source import attachment_source
from muli.attachments import Attachments
from muli.config import ConfigFile
from muli.local_services import LocalServices
from muli.memory import source_digest
from muli.memory_application import MemoryApplication
from muli.providers import ModelClient, ModelConfig, ModelTurn
from muli.providers import client as provider_client
from muli.runtime import Runtime


class _ProviderRetryBarrier:
    """Pause this module's retry sleep without changing global asyncio members."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.delays: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.entered.set()
        await self.release.wait()

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("source-attempt tests must not use a real HTTP transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)


@pytest.mark.parametrize("initial_status", [429, 503])
async def test_forgotten_source_is_not_posted_again_after_provider_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_status: int
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "models": {
                "main": {"model": "attempt-main", "base_url": "https://unused.invalid/v1"},
                "memory_extractor": {
                    "model": "attempt-extractor",
                    "base_url": "https://extractor.invalid/v1",
                    "stream": False,
                },
            },
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
        }
    )
    retry = _ProviderRetryBarrier()
    monkeypatch.setattr(provider_client, "asyncio", retry)
    requests: list[httpx.Request] = []
    profiles: list[ModelConfig] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://extractor.invalid/v1/chat/completions"
        assert json.loads(request.content)["model"] == "attempt-extractor"
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(initial_status)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"memories":[],"aliases":{}}',
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        runtime = Runtime(config, tmp_path / "data")

        def factory(profile: ModelConfig) -> ModelClient:
            if profile.model == "attempt-extractor":
                profiles.append(profile)
                return ModelClient(profile, http)
            # Only the foreground strategy is fake; extraction keeps the real client.
            return FakeClient([ModelTurn(content="已收到")], [])  # type: ignore[return-value]

        runtime.client_factory = factory
        attachments = Attachments(runtime.store, tmp_path / "attachments", config)
        local = LocalServices(runtime, attachments)
        memory = MemoryApplication(runtime, local.media)
        try:
            await memory.start()
            assert memory._recovery is not None
            await asyncio.wait_for(asyncio.shield(memory._recovery), timeout=4)
            receipt = await runtime.submit("我喜欢雨后的青瓷蓝。", mode="yolo")
            source_id = receipt["message"]["id"]
            assert await asyncio.wait_for(runtime.wait(receipt["run_id"]), timeout=4) == "已收到"
            run = runtime.store.get("run", receipt["run_id"])
            assert run is not None and run["status"] == "completed"
            await asyncio.wait_for(retry.entered.wait(), timeout=4)

            # This is the application's own worker, source and real provider backoff.
            source = runtime.memory.source(source_id)
            job = memory.pipeline.job(source_id)
            worker = memory.pipeline._worker
            assert source is not None and source["trusted_owner"] is True
            assert source["content"] == "我喜欢雨后的青瓷蓝。"
            assert job is not None and job["state"] == "processing" and job["attempts"] == 1
            assert "extraction" not in job["checkpoint"]
            assert worker is not None and not worker.done()
            assert len(requests) == 1 and len(profiles) == 1
            profile = profiles[0]
            configured = config.settings.models.memory_extractor
            assert profile.max_retries == configured.max_retries and profile.max_retries > 0
            assert profile.timeout == configured.timeout
            assert retry.delays == [min(profile.retry_backoff, profile.timeout)]
            sent = json.loads(requests[0].content)
            sent_source = json.loads(sent["messages"][-1]["content"])
            assert sent_source["id"] == source_id and sent_source["digest"] == source["digest"]

            async with memory.pipeline.changed:
                assert runtime.memory.forget_source(source_id)
                assert memory.pipeline.job(source_id) is None
                retry.release.set()
                # wait(source_id) would already return on absence; wait for the job's publish.
                await asyncio.wait_for(memory.pipeline.changed.wait(), timeout=4)

            assert all(request.content == requests[0].content for request in requests)
            assert len(requests) == 1, "遗忘后的 provider 重试仍 POST 了旧来源正文"
            assert runtime.memory.source(source_id) is None
            assert memory.pipeline.job(source_id) is None
            assert not runtime.memory.search("青瓷蓝")
        finally:
            retry.release.set()
            await memory.close()
            await runtime.close()


@dataclass
class _Attempt:
    runtime: Runtime
    memory: MemoryApplication
    retry: _ProviderRetryBarrier
    requests: list[httpx.Request]
    source_id: str
    response_entered: asyncio.Event
    response_release: asyncio.Event


@asynccontextmanager
async def _memory_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_kind: str = "owner",
    wait_success: bool = False,
    response_status: int = 200,
    memory_attempts: int | None = None,
) -> AsyncIterator[_Attempt]:
    """Keep the real application extractor; control only HTTP and await barriers."""
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "models": {
                "memory_extractor": {
                    "model": "attempt-extractor",
                    "base_url": "https://extractor.invalid/v1",
                    "stream": False,
                }
            },
            "updates": {"check_on_start": False},
            "attention": {"enabled": False},
        }
    )
    if memory_attempts is not None:
        config.update({"memory": {"max_attempts": memory_attempts}})
    retry = _ProviderRetryBarrier()
    monkeypatch.setattr(provider_client, "asyncio", retry)
    requests: list[httpx.Request] = []
    response_entered, response_release = asyncio.Event(), asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://extractor.invalid/v1/chat/completions"
        requests.append(request)
        if len(requests) == 1:
            if wait_success:
                response_entered.set()
                await response_release.wait()
            else:
                return httpx.Response(503)
        return httpx.Response(
            response_status,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"memories":[],"aliases":{}}',
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        runtime = Runtime(config, tmp_path / "data")

        def factory(profile: ModelConfig) -> ModelClient:
            assert profile.model == "attempt-extractor"
            return ModelClient(profile, http)

        runtime.client_factory = factory
        attachments = Attachments(runtime.store, tmp_path / "attachments", config)
        local = LocalServices(runtime, attachments)
        memory = MemoryApplication(runtime, local.media)
        text = "这份青瓷蓝资料只用于核验来源尝试。"
        try:
            if source_kind == "owner":
                receipt, _ = runtime.conversation.enqueue(
                    runtime.main_session()["id"],
                    text,
                    source="web",
                    principal="owner",
                    mode="auto",
                    max_pending=8,
                    event_id="attempt-owner",
                )
                source_id = receipt["id"]
            else:
                assert source_kind in {"text", "asr"}

                async def chunks() -> AsyncIterator[bytes]:
                    yield text.encode("utf-8")

                # Exercise the production source builders, not import-tool authority.
                item = await attachments.save(
                    chunks(),
                    "attempt.txt",
                    "text/plain",
                    source="asr" if source_kind == "asr" else "web",
                )
                source = attachment_source(
                    item["id"],
                    text,
                    item["sha256"],
                    item["created_at"],
                    transcript=source_kind == "asr",
                )
                source_id = source["id"]
                memory.pipeline.ingest(source, trusted_owner=False)
            await memory.start()
            assert memory._recovery is not None
            await asyncio.wait_for(asyncio.shield(memory._recovery), 4)
            yield _Attempt(
                runtime, memory, retry, requests, source_id, response_entered, response_release
            )
        finally:
            retry.release.set()
            response_release.set()
            await memory.close()
            await runtime.close()


@pytest.mark.parametrize("source_kind", ["owner", "text", "asr"])
async def test_unchanged_source_retries_with_its_existing_admission_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    async with _memory_job(tmp_path, monkeypatch, source_kind=source_kind) as rig:
        await asyncio.wait_for(rig.retry.entered.wait(), 4)
        rig.runtime.maintenance.admission.pause()
        assert not rig.runtime.maintenance.admission.idle
        async with rig.memory.pipeline.changed:
            rig.retry.release.set()
            await asyncio.wait_for(rig.memory.pipeline.changed.wait(), 4)
        job = rig.memory.pipeline.job(rig.source_id)
        assert job is not None and job["state"] == "done" and job["attempts"] == 1
        assert len(rig.requests) == 2
        assert rig.requests[0].content == rig.requests[1].content
        assert rig.memory.pipeline._worker is not None and not rig.memory.pipeline._worker.done()


@pytest.mark.parametrize("source_kind", ["text", "asr"])
async def test_forgotten_external_source_does_not_retry_and_worker_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    async with _memory_job(tmp_path, monkeypatch, source_kind=source_kind) as rig:
        await asyncio.wait_for(rig.retry.entered.wait(), 4)
        async with rig.memory.pipeline.changed:
            assert rig.runtime.memory.forget_source(rig.source_id)
            rig.retry.release.set()
            await asyncio.wait_for(rig.memory.pipeline.changed.wait(), 4)
        assert len(rig.requests) == 1
        assert rig.memory.pipeline.job(rig.source_id) is None
        worker = rig.memory.pipeline._worker
        assert worker is not None and not worker.done()
        receipt, _ = rig.runtime.conversation.enqueue(
            rig.runtime.main_session()["id"],
            "接下来只整理这份仍有效的海盐绿资料。",
            source="web",
            principal="owner",
            mode="auto",
            max_pending=8,
            event_id="following-owner",
        )
        rig.memory.ingest(receipt, True)
        job = await asyncio.wait_for(rig.memory.pipeline.wait(receipt["id"]), 4)
        assert job is not None and job["state"] == "done"
        assert len(rig.requests) == 2
        posted = json.loads(json.loads(rig.requests[1].content)["messages"][-1]["content"])
        assert posted["id"] == receipt["id"] and posted["id"] != rig.source_id
        assert rig.memory.pipeline._worker is worker and not worker.done()


@pytest.mark.parametrize(
    ("wait_success", "response_status"),
    [(False, 200), (True, 200), (True, 401)],
    ids=["retry", "late_success", "late_failure"],
)
async def test_replaced_source_job_is_untouched_by_the_old_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait_success: bool, response_status: int
) -> None:
    async with _memory_job(
        tmp_path, monkeypatch, wait_success=wait_success, response_status=response_status
    ) as rig:
        entered = rig.response_entered if wait_success else rig.retry.entered
        await asyncio.wait_for(entered.wait(), 4)
        rig.runtime.maintenance.admission.pause()
        source = rig.runtime.memory.source(rig.source_id)
        assert source is not None
        replacement = {**source, "content": "后续版本只保留海盐绿资料。"}
        revision = source_digest(replacement, trusted_owner=source["trusted_owner"])
        # The public API refuses same-ID edits; fault injection tests the stale fence.
        with rig.runtime.store.db:
            rig.runtime.store.db.execute(
                "UPDATE memory_sources SET content=?,digest=? WHERE id=?",
                (replacement["content"], revision, rig.source_id),
            )
            rig.runtime.store.db.execute(
                "UPDATE memory_jobs SET state='queued',attempts=0,checkpoint='{}',last_error='' "
                "WHERE source_id=?",
                (rig.source_id,),
            )
        expected = rig.memory.pipeline.job(rig.source_id)
        before = [
            tuple(row)
            for row in rig.runtime.store.db.execute(
                "SELECT m.id,m.content,x.version FROM memories m JOIN memory_meta x ON x.memory_id=m.id "
                "WHERE x.source_id=? ORDER BY m.id",
                (rig.source_id,),
            )
        ]
        async with rig.memory.pipeline.changed:
            rig.response_release.set()
            rig.retry.release.set()
            await asyncio.wait_for(rig.memory.pipeline.changed.wait(), 4)
        assert len(rig.requests) == 1
        assert rig.memory.pipeline.job(rig.source_id) == expected
        current = rig.runtime.memory.source(rig.source_id)
        assert current is not None and current["digest"] == revision
        after = [
            tuple(row)
            for row in rig.runtime.store.db.execute(
                "SELECT m.id,m.content,x.version FROM memories m JOIN memory_meta x ON x.memory_id=m.id "
                "WHERE x.source_id=? ORDER BY m.id",
                (rig.source_id,),
            )
        ]
        assert after == before


@pytest.mark.parametrize("window", ["backoff", "client_exit", "late_401"])
async def test_close_window_does_not_retry_or_fail_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: str
) -> None:
    exit_entered, exit_release = asyncio.Event(), asyncio.Event()
    original_exit = ModelClient.__aexit__

    async def exit_client(client: ModelClient, *args: object) -> None:
        exit_entered.set()
        await exit_release.wait()
        await original_exit(client, *args)

    if window == "client_exit":
        monkeypatch.setattr(ModelClient, "__aexit__", exit_client)
    # A single job attempt exposes an erroneous permanent failure; provider
    # retry counts, backoff and timeouts retain their configured defaults.
    async with _memory_job(
        tmp_path,
        monkeypatch,
        wait_success=window in {"client_exit", "late_401"},
        response_status=401 if window == "late_401" else 200,
        memory_attempts=1,
    ) as rig:
        if window == "client_exit":
            rig.response_release.set()
            await asyncio.wait_for(exit_entered.wait(), 4)
        elif window == "late_401":
            await asyncio.wait_for(rig.response_entered.wait(), 4)
        else:
            await asyncio.wait_for(rig.retry.entered.wait(), 4)
        entered, release = asyncio.Event(), asyncio.Event()
        original_close = rig.memory.watches.close

        async def close_watches() -> None:
            entered.set()
            await release.wait()
            await original_close()

        monkeypatch.setattr(rig.memory.watches, "close", close_watches)
        expected = rig.memory.pipeline.job(rig.source_id)
        source = rig.runtime.memory.source(rig.source_id)
        assert source is not None
        worker = rig.memory.pipeline._worker
        closing = asyncio.create_task(rig.memory.close())
        try:
            await asyncio.wait_for(entered.wait(), 4)
            assert rig.memory._closed and not rig.memory.pipeline._index_closed
            async with rig.memory.pipeline.changed:
                rig.retry.release.set()
                exit_release.set()
                rig.response_release.set()
                await asyncio.wait_for(rig.memory.pipeline.changed.wait(), 4)
            assert len(rig.requests) == 1
            assert rig.memory.pipeline.job(rig.source_id) == expected
            assert rig.runtime.memory.source(rig.source_id) == source
            assert worker is not None and not worker.done()
        finally:
            exit_release.set()
            release.set()
            await asyncio.wait_for(closing, 4)
        assert worker is not None and worker.cancelled()


async def test_current_source_keeps_its_real_upstream_terminal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _memory_job(
        tmp_path, monkeypatch, wait_success=True, response_status=401, memory_attempts=1
    ) as rig:
        await asyncio.wait_for(rig.response_entered.wait(), 4)
        async with rig.memory.pipeline.changed:
            rig.response_release.set()
            await asyncio.wait_for(rig.memory.pipeline.changed.wait(), 4)
        job = rig.memory.pipeline.job(rig.source_id)
        assert len(rig.requests) == 1
        assert job is not None and job["state"] == "failed" and job["attempts"] == 1
        assert job["last_error"] == "ProviderError" and "extraction" not in job["checkpoint"]
        assert rig.runtime.memory.source(rig.source_id) is not None
        assert rig.memory.pipeline._worker is not None and not rig.memory.pipeline._worker.done()


@pytest.mark.parametrize("swallow", [False, True])
async def test_worker_cancel_during_backoff_propagates_without_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swallow: bool
) -> None:
    swallowed = asyncio.Event()
    if swallow:
        original_sleep = _ProviderRetryBarrier.sleep

        async def swallow_sleep(barrier: _ProviderRetryBarrier, delay: float) -> None:
            try:
                await original_sleep(barrier, delay)
            except asyncio.CancelledError:
                swallowed.set()

        monkeypatch.setattr(_ProviderRetryBarrier, "sleep", swallow_sleep)
    async with _memory_job(tmp_path, monkeypatch) as rig:
        await asyncio.wait_for(rig.retry.entered.wait(), 4)
        worker = rig.memory.pipeline._worker
        assert worker is not None
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 4)
        assert swallowed.is_set() is swallow
        assert len(rig.requests) == 1
        job = rig.memory.pipeline.job(rig.source_id)
        assert job is not None and job["state"] == "queued"
        assert "extraction" not in job["checkpoint"]


async def test_birth_pipeline_callback_cannot_borrow_its_replacement_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _memory_job(tmp_path, monkeypatch) as rig:
        await asyncio.wait_for(rig.retry.entered.wait(), 4)
        old_pipeline = rig.memory.pipeline
        old_callback = old_pipeline.extractor
        assert old_callback is not None
        rig.runtime.maintenance.admission.pause()
        await rig.memory.reconfigure()
        replacement = rig.memory.pipeline
        assert replacement is not old_pipeline
        # It is still the real callback, deliberately retained by a later worker.
        replacement.extractor = old_callback
        async with replacement.changed:
            rig.runtime.maintenance.admission.release()
            await asyncio.wait_for(replacement.changed.wait(), 4)
        assert len(rig.requests) == 1
        job = replacement.job(rig.source_id)
        assert job is not None and "extraction" not in job["checkpoint"]
        assert job["state"] == "processing" and job["last_error"] == ""
        assert replacement._worker is not None and not replacement._worker.done()


async def test_source_forgotten_during_client_entry_has_no_first_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def enter(client: ModelClient) -> ModelClient:
        entered.set()
        await release.wait()
        return client

    monkeypatch.setattr(ModelClient, "__aenter__", enter)
    try:
        async with _memory_job(tmp_path, monkeypatch) as rig:
            await asyncio.wait_for(entered.wait(), 4)
            async with rig.memory.pipeline.changed:
                assert rig.runtime.memory.forget_source(rig.source_id)
                release.set()
                await asyncio.wait_for(rig.memory.pipeline.changed.wait(), 4)
            assert not rig.requests
            assert rig.memory.pipeline.job(rig.source_id) is None
    finally:
        release.set()
