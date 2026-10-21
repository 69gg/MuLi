"""Accept one approved wheel into the existing owned offline candidate attempt."""

from __future__ import annotations

import hashlib
import os
import zipfile
from collections.abc import Callable
from email.parser import BytesParser
from pathlib import Path
from typing import Literal, Self

import httpx
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version
from pydantic import model_validator

from muli.candidate_online import ArtifactApproval, OnlineSettings, check_approval, receive
from muli.candidate_policy import CandidateError
from muli.candidate_preparation import (
    CandidateAttempt,
    FrozenPolicy,
    WheelEvidence,
    _before,
    _wheel,
)
from muli.compatibility import CompatibilityManifest, assess_compatibility
from muli.management_transaction import StrictRecord
from muli.platforms.installation import FileFingerprint, FileIdentity, _canonical
from muli.platforms.managed_launcher import directory


class DownloadState(StrictRecord):
    """One attachment to the original management plan; never a retry permit."""

    protocol: int = 1
    phase: Literal["pending", "accepted"]
    file: FileFingerprint | None = None

    @model_validator(mode="after")
    def complete(self) -> Self:
        if self.protocol != 1 or (self.file is not None) != (self.phase == "accepted"):
            raise ValueError("candidate_download_state_invalid")
        return self


def check_local_materials(policy: FrozenPolicy, approval: ArtifactApproval) -> None:
    """A version pin alone does not bind uv to an approved build or tag."""
    for wheel in policy.materials.wheels:
        name, version, _, _ = parse_wheel_filename(wheel.name)
        if name == "muli-agent" and version == Version(approval.version):
            raise CandidateError("candidate_online_material_conflict")
    if sum(w.fingerprint.identity.size for w in policy.materials.wheels) + approval.size > (
        policy.settings.limits.compressed_bytes
    ):
        raise CandidateError("candidate_wheelhouse_budget")


def verify_wheel(
    attempt: CandidateAttempt, approval: ArtifactApproval, path: Path
) -> WheelEvidence:
    """Read bounded ZIP metadata, without importing or executing the candidate."""
    limits = attempt.policy.settings.limits
    with directory(path.parent, private=True) as parent:
        observed = _wheel(
            path.parent, parent, path.name, limits, attempt.deadline, filename=approval.filename
        )
        if observed.fingerprint.digest != approval.sha256 or (
            observed.fingerprint.identity.size != approval.size
        ):
            raise CandidateError("candidate_online_hash_mismatch")
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
        )
        with os.fdopen(descriptor, "rb") as source:
            if FileIdentity.of(os.fstat(source.fileno())) != observed.fingerprint.identity:
                raise CandidateError("candidate_material_changed")
            # _wheel already bounded the central directory and every declared
            # expanded size before ZipFile allocates its member list here.
            with zipfile.ZipFile(source) as archive:
                dist, version, _ = approval.filename.split("-", 2)
                metadata_name = f"{dist}-{version}.dist-info/METADATA"
                remaining = limits.metadata_bytes

                def read(name: str) -> bytes:
                    nonlocal remaining
                    _before(attempt.deadline)
                    item = archive.getinfo(name)
                    if item.file_size > remaining:
                        raise CandidateError("candidate_online_metadata_budget")
                    with archive.open(item) as member:
                        data = member.read(remaining + 1)
                    if len(data) > remaining or len(data) != item.file_size:
                        raise CandidateError("candidate_online_metadata_budget")
                    remaining -= len(data)
                    return data

                names = [
                    i.filename
                    for i in archive.infolist()
                    if i.filename.endswith(".dist-info/METADATA")
                ]
                if names != [metadata_name]:
                    raise CandidateError("candidate_online_metadata_invalid")
                metadata = BytesParser().parsebytes(read(metadata_name), headersonly=True)
                fields: list[str | None] = []
                for key in ("Name", "Version", "Requires-Python"):
                    values = metadata.get_all(key, [])
                    if len(values) > 1 or any(type(value) is not str for value in values):
                        raise CandidateError("candidate_online_metadata_invalid")
                    fields.append(str(values[0]) if values else None)
                name, declared_version, python = fields
                if (
                    metadata.defects
                    or name is None
                    or canonicalize_name(name) != "muli-agent"
                    or declared_version != approval.version
                    or SpecifierSet(python or "") != SpecifierSet(approval.requires_python or "")
                ):
                    raise CandidateError("candidate_online_metadata_invalid")
                declaration = CompatibilityManifest.from_json(read("muli/compatibility.json"))
                assessment = assess_compatibility(
                    attempt.policy.source.slot.compatibility,
                    declaration,
                    attempt.policy.source.observed_format,
                    handoff_protocol=2,
                )
                if declaration.release != approval.version or not assessment.format_compatible:
                    raise CandidateError("candidate_online_format_incompatible")
            if FileIdentity.of(os.fstat(source.fileno())) != observed.fingerprint.identity or (
                FileIdentity.of(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
                != observed.fingerprint.identity
            ):
                raise CandidateError("candidate_material_changed")
        _before(attempt.deadline)
        return observed


async def download_artifact(
    attempt: CandidateAttempt,
    settings: OnlineSettings,
    approval: ArtifactApproval,
    *,
    persist: Callable[[DownloadState], None],
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Single use. On any failure retain material; never retry/adopt a .part.

    Persist pending before opening the network. Accepted is only written after
    file+directory durability, full bounded validation and core material checks.
    A lost persist return intentionally prevents all later process effects.
    """
    policy = attempt.policy
    check_approval(settings, approval, policy, attempt.version)
    check_local_materials(policy, approval)
    if policy.settings.disk_mode != "threshold":
        raise CandidateError("candidate_disk_quota_unsupported")
    part = attempt.root / "tmp" / f"{approval.filename}.part"

    def verify() -> None:
        attempt.observe()
        policy.source.verify()
        check_approval(settings, approval, policy, attempt.version)

    verify()
    persist(DownloadState(phase="pending"))
    try:
        with directory(part.parent, private=True) as parent:
            descriptor = os.open(
                part.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(descriptor, "wb") as target:
                os.fsync(parent)
                consumed, digest = 0, hashlib.sha256()

                def consume(data: bytes) -> None:
                    nonlocal consumed
                    consumed += len(data)
                    if consumed > approval.size:
                        raise CandidateError("candidate_online_body_budget")
                    target.write(data)
                    target.flush()
                    digest.update(data)
                    # Charge the newly written chunk, not only the previous
                    # scan. The same attempt thresholds cover .part from byte 1.
                    verify()

                await receive(
                    approval.url,
                    settings=settings,
                    limit=min(approval.size, settings.download_bytes),
                    deadline=attempt.deadline,
                    close_seconds=policy.settings.limits.stop_seconds,
                    media_types=frozenset(
                        {"application/octet-stream", "application/zip", "binary/octet-stream"}
                    ),
                    consume=consume,
                    verify=verify,
                    transport=transport,
                )
                if consumed != approval.size or digest.hexdigest() != approval.sha256:
                    raise CandidateError("candidate_online_hash_mismatch")
                os.fsync(target.fileno())
            verify_wheel(attempt, approval, part)
            verify()
            with directory(attempt.root / "wheelhouse", private=True) as materials:
                # link+unlink cannot overwrite a prior wheel. There is no await
                # between publication and removal of our own temporary name.
                os.link(
                    part.name,
                    approval.filename,
                    src_dir_fd=parent,
                    dst_dir_fd=materials,
                    follow_symlinks=False,
                )
                os.unlink(part.name, dir_fd=parent)
                os.fsync(materials)
                os.fsync(parent)
                accepted = _wheel(
                    attempt.root / "wheelhouse",
                    materials,
                    approval.filename,
                    policy.settings.limits,
                    attempt.deadline,
                )
            if accepted.fingerprint.digest != approval.sha256:
                raise CandidateError("candidate_material_changed")
            attempt.accept_download(accepted, _canonical(approval.model_dump(mode="json")))
            verify()
            persist(DownloadState(phase="accepted", file=accepted.fingerprint))
    except CandidateError:
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError):
        raise CandidateError("candidate_online_material_failed") from None
