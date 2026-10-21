from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

import httpx
import pytest
from fastapi import FastAPI
from test_candidate_download import artifact_body
from test_candidate_management import no_subprocesses as no_subprocesses
from test_candidate_online import FILENAME, URL, VERSION, Chunks, metadata, response
from test_candidate_online import no_network as no_network
from test_candidate_update_capture import CaptureHost
from test_candidate_update_capture import capture_host as capture_host
from test_managed_preparation import host as host

import muli.management as management
from muli.candidate_download import DownloadState, download_artifact
from muli.candidate_online import ArtifactApproval, OnlineSettings, preview_artifact
from muli.candidate_policy import CandidateError, ProcessOutput, ProcessSpec
from muli.candidate_preparation import create_attempt
from muli.config import Settings
from muli.management import SelfManagement
from muli.management_projection import ProjectionError
from muli.platforms import candidate_process
from muli.plugins import PluginManager
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux actual managed source")


@dataclass
class OnlineHost:
    captured: CaptureHost
    manager: SelfManagement
    body: bytes
    calls: list[httpx.Request]
    metadata: dict[str, Any]
    transport: httpx.MockTransport

    async def preview(self) -> ArtifactApproval:
        tool = next(
            t for t in self.manager.tools(self.manager.runtime) if t.name == "self_update_preview"
        )
        assert tool.preserve_output and tool.risk == "read"
        assert "preserve_output" not in json.dumps(tool.schema())
        value = await tool.handler({"version": VERSION})
        assert value["dependencies"] == "local_wheelhouse_required"
        assert value["compatibility"] == "pending_artifact_validation"
        return ArtifactApproval.model_validate(value["expected_artifact"])

    async def stage(self, artifact: ArtifactApproval) -> dict[str, Any]:
        expected = (await self.captured.capture()).policy.expected
        return await self.manager.stage_update(VERSION, expected, artifact)


@pytest.fixture
async def online_host(
    capture_host: CaptureHost, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[OnlineHost]:
    host = capture_host.host

    async def no_in_place(settings: Settings) -> Never:
        pytest.fail("online candidate must not use an in-place installer")

    manager = SelfManagement(
        host.runtime,
        PluginManager(host.runtime, FastAPI(), host.root / "plugins"),
        host.service,
        no_in_place,
    )
    manager.online_policy.replace(OnlineSettings(enabled=True))
    captured = await capture_host.capture()
    body = artifact_body(captured.policy)
    facts = metadata(size=len(body), digests={"sha256": hashlib.sha256(body).hexdigest()})
    calls: list[httpx.Request] = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if str(request.url) == URL:
            return httpx.Response(
                200, headers={"content-type": "application/octet-stream"}, stream=Chunks(body)
            )
        return response(json.dumps(facts).encode())

    value = OnlineHost(capture_host, manager, body, calls, facts, httpx.MockTransport(send))

    async def preview(*args: Any, **kwargs: Any) -> ArtifactApproval:
        return await preview_artifact(*args, **kwargs, transport=value.transport)

    async def download(*args: Any, **kwargs: Any) -> None:
        await download_artifact(*args, **kwargs, transport=value.transport)

    monkeypatch.setattr(management, "preview_artifact", preview)
    monkeypatch.setattr(management, "download_artifact", download)
    try:
        yield value
    finally:
        await manager.close()


async def test_approval_and_same_plan_freeze_artifact_before_any_process(
    online_host: OnlineHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, manager = online_host, online_host.manager
    artifact = await value.preview()
    assert len(value.calls) == 1 and str(value.calls[0].url).endswith(f"/{VERSION}/json")
    origin = ReplyOrigin.load({"kind": "web", "input_id": "approved-web-input"})
    assert origin is not None
    monkeypatch.setattr(manager.runtime, "current_reply_origin", lambda: origin)
    tool = next(t for t in manager.tools(manager.runtime) if t.name == "self_update")
    assert tool.requires_approval and tool.risk == "external"
    expected = (await value.captured.capture()).policy.expected
    item = await tool.handler(
        {
            "version": VERSION,
            "expected_policy": expected.model_dump(mode="json"),
            "expected_artifact": artifact.model_dump(mode="json"),
        }
    )
    assert item["action"] == "switch" and item["operation"] == "update"
    assert item["routing"] == FrozenRouting.bound(origin).dump()
    assert item["online_artifact"] == artifact.model_dump(mode="json")
    assert len(value.calls) == 2  # Stage re-reads metadata, without selecting in the worker.
    effects: list[ProcessSpec] = []

    async def before_uv(spec: ProcessSpec, **kwargs: Any) -> ProcessOutput:
        effects.append(spec)
        saved = manager.runtime.store.get("management", item["id"])
        assert saved is not None and saved["online_artifact"] == item["online_artifact"]
        accepted = DownloadState.model_validate(saved["candidate_download"])
        assert accepted.phase == "accepted" and accepted.file is not None
        assert Path(accepted.file.path).read_bytes() == value.body
        assert "--version" in spec.argv
        spec.observe()
        raise CandidateError("test_stop_before_installer")

    monkeypatch.setattr(candidate_process, "run_candidate_process", before_uv)
    await manager._apply_candidate(item)
    assert len(effects) == 1 and len(value.calls) == 3
    assert str(value.calls[-1].url) == URL
    saved = manager.runtime.store.get("management", item["id"])
    assert saved is not None and saved["status"] == "failed"
    assert saved["version"] == VERSION and saved["routing"] == item["routing"]
    assert saved["candidate_download"]["phase"] == "accepted"
    assert manager.runtime.boot_session is not None
    assert manager.recovery.current()["transaction"]["resolved"]


@pytest.mark.parametrize("change", ["hash", "yanked", "policy"], ids=["hash", "yanked", "policy"])
async def test_stage_rechecks_approved_artifact_and_actual_online_policy(
    online_host: OnlineHost,
    change: str,
) -> None:
    value = online_host
    artifact = await value.preview()
    before = value.manager.plans()
    if change == "hash":
        value.metadata["urls"][0]["digests"] = {"sha256": "f" * 64}
    elif change == "yanked":
        value.metadata["urls"][0]["yanked"] = True
    else:
        value.manager.online_policy.replace(OnlineSettings())
    with pytest.raises(CandidateError):
        await value.stage(artifact)
    assert value.manager.plans() == before
    assert not list(Path(value.captured.settings.retained_root).iterdir())


async def test_online_mode_requires_matching_preview_not_a_version_only_approval(
    online_host: OnlineHost,
) -> None:
    manager = online_host.manager
    expected = (await online_host.captured.capture()).policy.expected
    with pytest.raises(CandidateError, match="candidate_online_preview_required"):
        await manager.stage_update(VERSION, expected)
    assert not online_host.calls
    assert len(manager.plans()) == 1  # Only the fixture's already projected adopt.


async def test_policy_revocation_while_http_is_pending_prevents_all_process_specs(
    online_host: OnlineHost,
) -> None:
    value = online_host
    item = await value.stage(await value.preview())
    reached, release = asyncio.Event(), asyncio.Event()

    class Stalled(Chunks):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            reached.set()
            await release.wait()
            yield value.body

    value.transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, stream=Stalled()
        )
    )
    applying = asyncio.create_task(value.manager._apply_candidate(item))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        async with value.manager.runtime.configuration_lock:
            value.manager.replace_online_policy(OnlineSettings())
        release.set()
        await asyncio.wait_for(applying, 2)
        saved = value.manager.runtime.store.get("management", item["id"])
        assert saved is not None and saved["status"] == "failed"
        assert saved["candidate_download"]["phase"] == "pending"
        assert "candidate_process" not in saved
    finally:
        release.set()
        if not applying.done():
            applying.cancel()
        await asyncio.gather(applying, return_exceptions=True)


async def test_policy_revocation_after_uv_version_prevents_install_spec(
    online_host: OnlineHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = online_host.manager
    item = await online_host.stage(await online_host.preview())
    calls: list[ProcessSpec] = []

    async def uv_only(spec: ProcessSpec, **kwargs: Any) -> ProcessOutput:
        calls.append(spec)
        assert "--version" in spec.argv
        async with manager.runtime.configuration_lock:
            manager.replace_online_policy(OnlineSettings())
        return ProcessOutput(0, stdout="uv 0.13.1")

    monkeypatch.setattr(candidate_process, "run_candidate_process", uv_only)
    await manager._apply_candidate(item)
    saved = manager.runtime.store.get("management", item["id"])
    assert len(calls) == 1 and saved is not None and saved["status"] == "failed"
    assert saved["candidate_download"]["phase"] == "accepted"


@pytest.mark.parametrize("phase", ["pending", "accepted"])
async def test_committed_download_receipt_survives_a_lost_store_return(
    online_host: OnlineHost,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    value, manager = online_host, online_host.manager
    item = await value.stage(await value.preview())
    event = manager.runtime.store.event
    lost: list[dict[str, Any]] = []

    def commit_then_fail(name: str, data: dict[str, Any], **kwargs: Any) -> Any:
        result = event(name, data, **kwargs)
        if not lost and data.get("candidate_download", {}).get("phase") == phase:
            lost.append(dict(data))
            raise OSError("synthetic lost commit return")
        return result

    monkeypatch.setattr(manager.runtime.store, "event", commit_then_fail)
    await manager._apply_candidate(item)
    saved = manager.runtime.store.get("management", item["id"])
    assert lost and saved is not None and saved["status"] == "failed"
    assert saved["candidate_download"] == lost[0]["candidate_download"]
    assert saved["online_artifact"] == item["online_artifact"]
    assert "candidate_process" not in saved
    root = Path(saved["candidate_attempt"])
    if phase == "accepted":
        assert (root / "wheelhouse" / FILENAME).read_bytes() == value.body
        assert len(value.calls) == 3
    else:
        assert not (root / "tmp" / f"{FILENAME}.part").exists()
        assert len(value.calls) == 2
    # A stale in-memory caller must not erase the committed attachment either.
    stale = {**saved, "candidate_download": {"protocol": 1, "phase": "pending", "file": None}}
    if phase == "accepted":
        await manager._finish_plan(stale, manager.directory / f"{item['id']}.json")
        assert (
            manager.runtime.store.get("management", item["id"])["candidate_download"]
            == (saved["candidate_download"])
        )


@pytest.mark.parametrize("phase", ["pending", "accepted"])
async def test_restart_does_not_resume_download_or_reinstall(
    online_host: OnlineHost,
    phase: str,
) -> None:
    value, manager = online_host, online_host.manager
    approval = await value.preview()
    item = await value.stage(approval)
    captured = manager._bind_online_policy(
        await value.captured.capture(),
        manager.online_policy.read(),
    )
    attempt = create_attempt(
        captured.policy,
        expected=captured.policy.expected,
        request_id=item["id"],
        version=VERSION,
        configuration=b"{}",
        additional_material_bytes=approval.size,
    )
    item.update(
        status="applying",
        phase="candidate_preparing",
        candidate_protocol=1,
        candidate_attempt=str(attempt.root),
    )
    state = DownloadState(phase="pending")
    if phase == "accepted":
        # Startup does not adopt even an accepted file as permission to install.
        states: list[DownloadState] = []
        await download_artifact(
            attempt,
            manager.online_policy.read(),
            approval,
            persist=states.append,
            transport=value.transport,
        )
        state = states[-1]
        assert state.phase == "accepted"
    item["candidate_download"] = state.model_dump(mode="json")
    manager.runtime.store.put("management", item)
    count = len(value.calls)
    await manager.start()
    saved = manager.runtime.store.get("management", item["id"])
    assert saved is not None and saved["status"] == "interrupted"
    assert saved["phase"] == "candidate_cleaned" and saved["version"] == VERSION
    assert saved["candidate_download"] == state.model_dump(mode="json")
    assert len(value.calls) == count and "candidate_process" not in saved


@pytest.mark.parametrize("mode", ["outer", "autocommit", "isolation-none"])
async def test_download_cannot_commit_a_callers_transaction_or_use_an_unsupported_mode(
    online_host: OnlineHost,
    mode: str,
) -> None:
    value, manager = online_host, online_host.manager
    approval = await value.preview()
    item = await value.stage(approval)
    captured = manager._bind_online_policy(
        await value.captured.capture(),
        manager.online_policy.read(),
    )
    attempt = create_attempt(
        captured.policy,
        expected=captured.policy.expected,
        request_id=item["id"],
        version=VERSION,
        configuration=b"{}",
        additional_material_bytes=approval.size,
    )
    item.update(
        status="applying",
        phase="candidate_preparing",
        candidate_protocol=1,
        candidate_attempt=str(attempt.root),
    )
    store = manager.runtime.store
    store.put("management", item)
    before, calls = store.event_position(), len(value.calls)
    db = store.db
    if mode == "outer":
        db.execute("BEGIN")
        db.execute("INSERT INTO runtime_state(name,value) VALUES ('test-owner','uncommitted')")
    elif mode == "autocommit":
        db.autocommit = True
    else:
        db.isolation_level = None
    try:
        with pytest.raises(
            CandidateError,
            match="^candidate_download_(outer_transaction|transaction_mode_unsupported)$",
        ):
            manager._persist_candidate_download(item, DownloadState(phase="pending"))
        # The live source verifier independently rejects these modes too. Its
        # earlier refusal must still leave the caller's transaction untouched.
        with pytest.raises((CandidateError, ProjectionError)):
            await download_artifact(
                attempt,
                manager.online_policy.read(),
                approval,
                persist=lambda state: manager._persist_candidate_download(item, state),
                transport=value.transport,
            )
        assert store.get("management", item["id"]) == item
        assert store.event_position() == before and len(value.calls) == calls
        assert not list((attempt.root / "tmp").iterdir())
        if mode == "outer":
            assert db.in_transaction
            assert (
                db.execute("SELECT value FROM runtime_state WHERE name='test-owner'").fetchone()[0]
                == "uncommitted"
            )
        else:
            assert not db.in_transaction
        with pytest.raises(CandidateError, match="candidate_stage_invalid"):
            attempt.uv_version_spec()
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")
        db.autocommit = sqlite3.LEGACY_TRANSACTION_CONTROL
        db.isolation_level = "DEFERRED"


@pytest.mark.parametrize("mode", ["outer", "autocommit", "isolation-none"])
async def test_full_candidate_failure_keeps_the_callers_transaction_and_durable_plan(
    online_host: OnlineHost,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    value, manager = online_host, online_host.manager
    item = await value.stage(await value.preview())
    store = manager.runtime.store
    saved = store.get("management", item["id"])
    assert saved is not None
    path = manager.directory / f"{item['id']}.json"
    data = path.read_bytes()
    position, requests = store.event_position(), len(value.calls)
    retained = Path(value.captured.settings.retained_root)
    entries = list(retained.iterdir())

    async def no_spec(spec: ProcessSpec, **kwargs: Any) -> Never:
        pytest.fail("an invalid caller transaction must not reach any candidate process")

    monkeypatch.setattr(candidate_process, "run_candidate_process", no_spec)
    db = store.db
    if mode == "outer":
        db.execute("BEGIN")
        db.execute("INSERT INTO runtime_state(name,value) VALUES ('test-owner','uncommitted')")
    elif mode == "autocommit":
        db.autocommit = True
    else:
        db.isolation_level = None
    error: CandidateError | None = None
    try:
        try:
            await manager._apply_candidate(item)
        except CandidateError as exc:
            error = exc
        assert manager._applying is None
        if mode == "outer":
            assert db.in_transaction, "candidate failure committed the caller's transaction"
            assert (
                db.execute("SELECT value FROM runtime_state WHERE name='test-owner'").fetchone()[0]
                == "uncommitted"
            )
        elif mode == "autocommit":
            assert db.autocommit is True and not db.in_transaction
        else:
            assert db.isolation_level is None and not db.in_transaction
        assert error is not None
        assert str(error) == (
            "candidate_finish_outer_transaction"
            if mode == "outer"
            else "candidate_finish_transaction_mode_unsupported"
        )
        assert store.get("management", saved["id"]) == saved
        assert store.event_position() == position
        assert path.read_bytes() == data
        assert len(value.calls) == requests and list(retained.iterdir()) == entries
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")
        db.autocommit = sqlite3.LEGACY_TRANSACTION_CONTROL
        db.isolation_level = "DEFERRED"
