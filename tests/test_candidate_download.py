from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import time
import zipfile
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from test_candidate_online import FILENAME, VERSION, Chunks, metadata, response
from test_candidate_online import no_network as no_network
from test_candidate_preparation import install_fixture, wheel
from test_candidate_preparation import no_processes as no_processes
from test_candidate_preparation import policy as policy

from muli.candidate_download import DownloadState, download_artifact
from muli.candidate_online import ArtifactApproval, OnlineSettings, preview_artifact
from muli.candidate_policy import CandidateError
from muli.candidate_preparation import (
    CandidateAttempt,
    FrozenPolicy,
    create_attempt,
    inspect_policy,
)
from muli.format_admission import CURRENT_FORMAT_STATE
from muli.platforms.installation import _canonical

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux owned candidate material")


def artifact_body(policy: FrozenPolicy, *, change: str = "", payload: int = 1024) -> bytes:
    declaration = policy.source.slot.compatibility.model_dump(mode="json")
    declaration["release"] = VERSION
    if change == "format":
        declaration["formats"]["objects"] = {"reads": [2], "writes": 2}
    elif change == "migration":
        declaration["requires_migration"] = True
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        name = "other" if change == "name" else "muli-agent"
        version = "2.32.0" if change == "version" else VERSION
        python = ">=99" if change == "python" else ">=3.12"
        archive.writestr(
            f"muli_agent-{VERSION}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nRequires-Python: {python}\n\n",
        )
        if change != "missing-declaration":
            archive.writestr("muli/compatibility.json", _canonical(declaration))
        archive.writestr("../unsafe" if change == "path" else "muli/fixture.py", b"x" * payload)
    return data.getvalue()


def observed(policy: FrozenPolicy) -> FrozenPolicy:
    return replace(policy, source=replace(policy.source, observed_format=CURRENT_FORMAT_STATE))


async def approved(policy: FrozenPolicy, body: bytes) -> ArtifactApproval:
    return await preview_artifact(
        OnlineSettings(enabled=True),
        policy,
        VERSION,
        verify=lambda: None,
        transport=httpx.MockTransport(
            lambda _: response(
                json.dumps(
                    metadata(
                        size=len(body),
                        digests={"sha256": hashlib.sha256(body).hexdigest()},
                    )
                ).encode()
            )
        ),
    )


def allocate(policy: FrozenPolicy, approval: ArtifactApproval) -> CandidateAttempt:
    return create_attempt(
        policy,
        expected=policy.expected,
        request_id="online-request",
        version=VERSION,
        configuration=b"{}",
        deadline=time.monotonic() + 10,
        additional_material_bytes=approval.size,
    )


def transport_for(body: bytes) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            stream=Chunks(body),
        )
    )


async def test_accepted_wheel_is_frozen_before_specs_and_in_final_private_evidence(
    policy: FrozenPolicy,
) -> None:
    policy = observed(policy)
    original = policy.materials
    body = artifact_body(policy)
    approval = await approved(policy, body)
    attempt = allocate(policy, approval)
    end = attempt.deadline
    states: list[DownloadState] = []
    with pytest.raises(CandidateError, match="candidate_stage_invalid"):
        attempt.uv_version_spec()
    await download_artifact(
        attempt,
        OnlineSettings(enabled=True),
        approval,
        persist=states.append,
        transport=transport_for(body),
    )
    assert [state.phase for state in states] == ["pending", "accepted"]
    assert states[-1].file is not None
    assert states[-1].file.path == str(attempt.root / "wheelhouse" / FILENAME)
    assert states[-1].file.digest == approval.sha256
    assert (attempt.root / "wheelhouse" / FILENAME).read_bytes() == body
    assert not list((attempt.root / "tmp").iterdir())
    assert (attempt.root / "online-artifact.json").read_bytes() == _canonical(
        approval.model_dump(mode="json")
    )
    assert policy.materials == original and len(attempt.copied.wheels) == 2
    assert attempt.deadline == end
    ready = install_fixture(attempt)  # Fake process receipts, actual retained files.
    assert ready.evidence.materials == attempt.material_digest != original.digest
    attempt.verify_ready(ready)
    assert attempt.deadline == end


@pytest.mark.parametrize(
    "change", ["name", "version", "python", "format", "migration", "missing-declaration", "path"]
)
async def test_unverified_metadata_never_yields_a_process_spec(
    policy: FrozenPolicy,
    change: str,
) -> None:
    policy = observed(policy)
    body = artifact_body(policy, change=change)
    approval = await approved(policy, body)
    attempt = allocate(policy, approval)
    states: list[DownloadState] = []
    with pytest.raises(CandidateError):
        await download_artifact(
            attempt,
            OnlineSettings(enabled=True),
            approval,
            persist=states.append,
            transport=transport_for(body),
        )
    assert [state.phase for state in states] == ["pending"]
    assert not (attempt.root / "wheelhouse" / FILENAME).exists()
    assert list((attempt.root / "tmp").glob("*.part"))
    with pytest.raises(CandidateError, match="candidate_stage_invalid"):
        attempt.uv_version_spec()


@pytest.mark.parametrize("change", ["hash", "truncated", "oversize"])
async def test_body_size_and_hash_are_actual_not_content_length(
    policy: FrozenPolicy,
    change: str,
) -> None:
    policy = observed(policy)
    body = artifact_body(policy)
    approval = await approved(policy, body)
    attempt = allocate(policy, approval)
    if change == "hash":
        body = body[:-1] + bytes([body[-1] ^ 1])
    elif change == "truncated":
        body = body[:-1]
    else:
        body += b"unexpected"
    states: list[DownloadState] = []
    with pytest.raises(CandidateError):
        await download_artifact(
            attempt,
            OnlineSettings(enabled=True),
            approval,
            persist=states.append,
            transport=transport_for(body),
        )
    assert len(states) == 1
    assert not (attempt.root / "wheelhouse" / FILENAME).exists()


@pytest.mark.parametrize(
    "filename",
    [
        FILENAME,
        f"muli_agent-{VERSION}-1-py3-none-any.whl",
        f"muli_agent-{VERSION}-cp312-cp312-manylinux_2_17_x86_64.whl",
    ],
    ids=["same-name", "other-build", "other-tag"],
)
async def test_local_competing_target_is_rejected_without_a_network_request(
    policy: FrozenPolicy,
    filename: str,
) -> None:
    wheel(Path(policy.materials.path) / filename)
    policy = inspect_policy(policy.settings, observed(policy).source)
    body = artifact_body(policy)
    approval = await approved(policy, body)
    attempt = allocate(policy, approval)

    def forbidden(_: httpx.Request) -> httpx.Response:
        pytest.fail("a competing local target must fail before HTTP or uv")

    with pytest.raises(CandidateError, match="candidate_online_material_conflict"):
        await download_artifact(
            attempt,
            OnlineSettings(enabled=True),
            approval,
            persist=lambda _: pytest.fail("no pending"),
            transport=httpx.MockTransport(forbidden),
        )


async def test_aggregate_expanded_budget_includes_existing_materials(policy: FrozenPolicy) -> None:
    policy = observed(policy)
    body = artifact_body(policy)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        expanded = sum(item.file_size for item in archive.infolist())
    limits = policy.settings.limits.model_copy(update={"expanded_bytes": expanded})
    policy = inspect_policy(policy.settings.model_copy(update={"limits": limits}), policy.source)
    approval = await approved(policy, body)
    attempt = allocate(policy, approval)
    states: list[DownloadState] = []
    with pytest.raises(CandidateError, match="candidate_wheelhouse_budget"):
        await download_artifact(
            attempt,
            OnlineSettings(enabled=True),
            approval,
            persist=states.append,
            transport=transport_for(body),
        )
    assert [s.phase for s in states] == ["pending"]
    with pytest.raises(CandidateError, match="candidate_stage_invalid"):
        attempt.uv_version_spec()


@pytest.mark.parametrize("phase", ["pending", "accepted"])
async def test_lost_persistence_return_retains_facts_and_never_automatically_executes(
    policy: FrozenPolicy,
    phase: str,
) -> None:
    policy = observed(policy)
    body = artifact_body(policy)
    approval = await approved(policy, body)
    attempt = allocate(policy, approval)
    durable: list[DownloadState] = []

    def persist(value: DownloadState) -> None:
        durable.append(value)
        if value.phase == phase:
            raise RuntimeError("simulated commit succeeded but return was lost")

    with pytest.raises((CandidateError, RuntimeError)):
        await download_artifact(
            attempt,
            OnlineSettings(enabled=True),
            approval,
            persist=persist,
            transport=transport_for(body),
        )
    assert durable[-1].phase == phase and attempt.root.exists()
    if phase == "accepted":
        assert durable[-1].file is not None
        assert (attempt.root / "wheelhouse" / FILENAME).read_bytes() == body
    else:
        assert not (attempt.root / "wheelhouse" / FILENAME).exists()
    with pytest.raises(CandidateError, match="candidate_attempt_unavailable"):
        allocate(policy, approval)  # Existing attempts never authorize a retry.


async def test_mutation_after_acceptance_is_caught_before_first_spec(policy: FrozenPolicy) -> None:
    policy = observed(policy)
    body = artifact_body(policy)
    approval = await approved(policy, body)
    attempt = allocate(policy, approval)
    await download_artifact(
        attempt,
        OnlineSettings(enabled=True),
        approval,
        persist=lambda _: None,
        transport=transport_for(body),
    )
    path = attempt.root / "wheelhouse" / FILENAME
    path.write_bytes(artifact_body(policy, change="name"))
    with pytest.raises(CandidateError, match="candidate_material_changed"):
        attempt.uv_version_spec()


async def test_repeated_cancel_waits_for_http_close_and_retains_bounded_part(
    policy: FrozenPolicy,
) -> None:
    policy = observed(policy)
    body = artifact_body(policy, payload=70_000)
    approval = await approved(policy, body)
    attempt = allocate(policy, approval)
    reached, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Hanging(Chunks):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield body[:65_536]
            reached.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            closing.set()
            await release.wait()
            self.closed = True

    stream = Hanging()
    states: list[DownloadState] = []
    task = asyncio.create_task(
        download_artifact(
            attempt,
            OnlineSettings(enabled=True),
            approval,
            persist=states.append,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "application/octet-stream"}, stream=stream
                )
            ),
        )
    )
    try:
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert [s.phase for s in states] == ["pending"]
        part = attempt.root / "tmp" / f"{FILENAME}.part"
        assert part.stat().st_size == 65_536
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert stream.closed and part.stat().st_size <= approval.size
        assert not (attempt.root / "wheelhouse" / FILENAME).exists()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
