"""Freeze an explicit local wheelhouse and build one retained uv candidate.

This module does not discover a current installation, approve a request, start
processes, change a selector or publish a transaction. The host supplies its
existing verified current-source observation and owns the execution backend.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
import sys
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import parse_wheel_filename
from pydantic import Field, TypeAdapter

from muli.candidate_policy import (
    CandidateError,
    CandidateLimits,
    CandidateSettings,
    ExpectedPolicy,
    ProcessOutput,
    ProcessSpec,
)
from muli.compatibility import FormatState
from muli.install_profile import InstallIntent, ReceiptOptions, RegistryRequirement
from muli.management_transaction import Digest, Identifier, StrictRecord, _object
from muli.platforms.install_receipt import observe_install_receipt
from muli.platforms.installation import FileFingerprint, FileIdentity, _canonical
from muli.platforms.managed_deployment import (
    SlotIdentity,
    register_local_slot,
    verify_slot_files,
)
from muli.platforms.managed_launcher import directory, python_fingerprint

if TYPE_CHECKING:
    from muli.managed_preparation import LocalSlotPaths
    from muli.platforms.install_receipt import ManagedInstallationReceipt

_DIGEST = TypeAdapter(Digest)
_IDENTIFIER = TypeAdapter(Identifier)
_CHUNK = 65_536
_EOCD = struct.Struct("<4s4H2IH")
_UV_VERSIONS = frozenset({"0.13.0", "0.13.1"})
_UV_OUTPUT = re.compile(r"uv (0\.13\.[01])(?: \([a-zA-Z0-9 .:+_-]+\))?\n?\Z")


class WheelEvidence(StrictRecord):
    name: str = Field(min_length=1, max_length=255)
    fingerprint: FileFingerprint = Field(repr=False)
    expanded_bytes: int = Field(ge=0)
    entries: int = Field(ge=1)


class WheelhouseEvidence(StrictRecord):
    path: str = Field(repr=False)
    identity: FileIdentity = Field(repr=False)
    wheels: tuple[WheelEvidence, ...] = Field(repr=False)

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical(self.model_dump(mode="json"))).hexdigest()


class CandidateEvidence(StrictRecord):
    """One bounded, self-contained private plan attachment, never a ledger."""

    schema_version: int = Field(default=1, ge=1, le=1)
    request_id: Identifier
    source_installation: Digest = Field(repr=False)
    binding_revision: Digest = Field(repr=False)
    original_intent: InstallIntent = Field(repr=False)
    deployed_installation: Digest = Field(repr=False)
    deployed_receipt: FileFingerprint = Field(repr=False)
    deployed_intent: InstallIntent = Field(repr=False)
    version: str
    uv_version: str
    policy: ExpectedPolicy
    materials: Digest = Field(repr=False)

    def to_bytes(self, limit: int) -> bytes:
        data = _canonical(self.model_dump(mode="json"))
        if len(data) > limit:
            raise CandidateError("candidate_evidence_budget")
        return data

    @classmethod
    def from_bytes(cls, data: bytes, limit: int) -> CandidateEvidence:
        if type(data) is not bytes or len(data) > limit:
            raise CandidateError("candidate_evidence_budget")
        try:
            _object(data)
            return cls.model_validate_json(data)
        except ValueError:
            raise CandidateError("candidate_evidence_invalid") from None


@dataclass(frozen=True, slots=True)
class CandidateSource:
    """Created by the host's existing current-binding observer, never a model.

    verify rechecks the original lease, Store, exact projection and live binding.
    previous is loaded only through current record.request_id's private plan
    attachment; the core never searches controllers or follows ancestor plans.
    """

    slot: SlotIdentity = field(repr=False)
    receipt: ManagedInstallationReceipt = field(repr=False)
    binding_revision: str = field(repr=False)
    verify: Callable[[], None] = field(repr=False, compare=False)
    previous: CandidateEvidence | None = field(default=None, repr=False)
    protected_roots: tuple[Path, ...] = field(default=(), repr=False)

    def original_intent(self) -> InstallIntent:
        self.verify()
        self.receipt.verify(self.slot)
        if self.receipt.installation != self.slot.digest:
            raise CandidateError("candidate_source_changed")
        if self.previous is None:
            return self.receipt.parsed.intent
        previous = self.previous
        if (
            previous.deployed_installation != self.slot.digest
            or previous.deployed_receipt != self.receipt.receipt
            or previous.deployed_intent != self.receipt.parsed.intent
        ):
            raise CandidateError("candidate_intent_chain_changed")
        return previous.original_intent


@dataclass(frozen=True, slots=True)
class FrozenPolicy:
    settings: CandidateSettings = field(repr=False)
    source: CandidateSource = field(repr=False)
    original_intent: InstallIntent = field(repr=False)
    materials: WheelhouseEvidence = field(repr=False)
    uv: FileFingerprint = field(repr=False)
    expected: ExpectedPolicy

    def verify(self, expected: ExpectedPolicy, *, deadline: float | None = None) -> None:
        if self.expected != expected:
            raise CandidateError("candidate_policy_changed")
        repeated = inspect_policy(self.settings, self.source, deadline=deadline)
        if repeated.expected != self.expected:
            raise CandidateError("candidate_policy_changed")


def _platform() -> None:
    if sys.platform != "linux":
        raise CandidateError("candidate_platform_unsupported")


def _before(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise CandidateError("candidate_deadline_exceeded")


def _owned(info: os.stat_result) -> None:
    if info.st_uid not in {0, os.geteuid()} or stat.S_IMODE(info.st_mode) & 0o022:
        raise CandidateError("candidate_material_ownership")


def _archive(
    source: BinaryIO, size: int, limits: CandidateLimits, deadline: float
) -> tuple[int, int]:
    # Bound the central directory before ZipFile builds its complete ZipInfos.
    source.seek(max(0, size - (_EOCD.size + 65_535)))
    tail = source.read(_EOCD.size + 65_535)
    position = tail.rfind(b"PK\x05\x06")
    if position < 0 or position + _EOCD.size > len(tail):
        raise CandidateError("candidate_wheel_invalid")
    _, disk, central_disk, disk_count, count, central_size, offset, comment = _EOCD.unpack_from(
        tail, position
    )
    if (
        disk != 0
        or central_disk != 0
        or disk_count != count
        or count == 0
        or count == 65_535
        or count > limits.archive_entries
        or central_size > limits.central_directory_bytes
        or offset + central_size != size - _EOCD.size - comment
        or position + _EOCD.size + comment != len(tail)
    ):
        raise CandidateError("candidate_archive_budget")
    source.seek(0)
    expanded = 0
    names: set[str] = set()
    try:
        with zipfile.ZipFile(source, "r") as archive:
            entries = archive.infolist()
            if len(entries) != count:
                raise CandidateError("candidate_wheel_invalid")
            for entry in entries:
                _before(deadline)
                name = entry.filename.rstrip("/")
                path = PurePosixPath(name)
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (
                    not name
                    or "\\" in name
                    or entry.orig_filename != entry.filename
                    or path.is_absolute()
                    or ".." in path.parts
                    or path.as_posix() != name
                    or name in names
                    or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or entry.flag_bits & 1
                    or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                ):
                    raise CandidateError("candidate_wheel_unsupported")
                names.add(name)
                expanded += entry.file_size
                if expanded > limits.expanded_bytes:
                    raise CandidateError("candidate_archive_budget")
    except (zipfile.BadZipFile, NotImplementedError, UnicodeError, struct.error):
        raise CandidateError("candidate_wheel_invalid") from None
    return expanded, count


def _wheel(
    root: Path, descriptor: int, name: str, limits: CandidateLimits, deadline: float
) -> WheelEvidence:
    parse_wheel_filename(name)
    handle = os.open(
        name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
    )
    with os.fdopen(handle, "rb") as source:
        before = os.fstat(source.fileno())
        _owned(before)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limits.wheel_bytes:
            raise CandidateError("candidate_wheel_budget")
        digest, consumed = hashlib.sha256(), 0
        while chunk := source.read(min(_CHUNK, limits.wheel_bytes - consumed + 1)):
            _before(deadline)
            consumed += len(chunk)
            if consumed > limits.wheel_bytes:
                raise CandidateError("candidate_wheel_budget")
            digest.update(chunk)
        if consumed != before.st_size:
            raise CandidateError("candidate_material_changed")
        expanded, entries = _archive(source, consumed, limits, deadline)
        identity = FileIdentity.of(before)
        if identity != FileIdentity.of(os.fstat(source.fileno())) or identity != FileIdentity.of(
            os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        ):
            raise CandidateError("candidate_material_changed")
    return WheelEvidence(
        name=name,
        fingerprint=FileFingerprint(
            path=str(root / name), identity=identity, digest=digest.hexdigest()
        ),
        expanded_bytes=expanded,
        entries=entries,
    )


def inspect_wheelhouse(
    path: Path, limits: CandidateLimits, *, deadline: float | None = None
) -> WheelhouseEvidence:
    _platform()
    end = (
        min(deadline, time.monotonic() + limits.preflight_seconds)
        if deadline is not None
        else (time.monotonic() + limits.preflight_seconds)
    )
    try:
        with directory(path) as descriptor:
            before = os.fstat(descriptor)
            _owned(before)
            names: list[str] = []
            with os.scandir(descriptor) as scan:
                for entry in scan:
                    _before(end)
                    if len(names) >= limits.wheels or not entry.name.endswith(".whl"):
                        raise CandidateError("candidate_wheelhouse_budget")
                    names.append(entry.name)
            if not names:
                raise CandidateError("candidate_material_missing")
            wheels: list[WheelEvidence] = []
            compressed = expanded = entries = 0
            for name in sorted(names):
                _before(end)
                wheel = _wheel(path, descriptor, name, limits, end)
                compressed += wheel.fingerprint.identity.size
                expanded += wheel.expanded_bytes
                entries += wheel.entries
                if (
                    compressed > limits.compressed_bytes
                    or expanded > limits.expanded_bytes
                    or entries > limits.archive_entries
                ):
                    raise CandidateError("candidate_wheelhouse_budget")
                wheels.append(wheel)
            _before(end)
            if FileIdentity.of(before) != FileIdentity.of(os.fstat(descriptor)):
                raise CandidateError("candidate_material_changed")
            return WheelhouseEvidence(
                path=str(path), identity=FileIdentity.of(before), wheels=tuple(wheels)
            )
    except (OSError, ValueError) as exc:
        if isinstance(exc, CandidateError):
            raise
        raise CandidateError("candidate_material_unavailable") from None


def inspect_policy(
    settings: CandidateSettings, source: CandidateSource, *, deadline: float | None = None
) -> FrozenPolicy:
    _platform()
    if not settings.enabled:
        raise CandidateError("candidate_source_not_configured")
    end = time.monotonic() + settings.limits.preflight_seconds
    if deadline is not None:
        end = min(end, deadline)
    try:
        _DIGEST.validate_python(source.binding_revision, strict=True)
        intent = source.original_intent()
        _supported_intent(intent, settings)
        _before(end)
        materials = inspect_wheelhouse(Path(settings.wheelhouse), settings.limits, deadline=end)
        uv = FileFingerprint.model_validate(
            python_fingerprint(Path(settings.uv_executable), settings.limits.executable_bytes)
        )
        if (
            uv.identity.uid not in {0, os.geteuid()}
            or stat.S_IMODE(uv.identity.mode) & 0o022
            or not stat.S_IMODE(uv.identity.mode) & 0o111
        ):
            raise CandidateError("candidate_uv_unsupported")
        source.verify()
        source.receipt.verify(source.slot)
        _before(end)
        revision = hashlib.sha256(
            _canonical(
                {
                    "settings": settings.model_dump(mode="json"),
                    "binding": source.binding_revision,
                    "slot": source.slot.digest,
                    "receipt": source.receipt.receipt.model_dump(mode="json"),
                    "intent": intent.model_dump(mode="json"),
                    "materials": materials.digest,
                    "uv": uv.model_dump(mode="json"),
                }
            )
        ).hexdigest()
        return FrozenPolicy(
            settings,
            source,
            intent,
            materials,
            uv,
            ExpectedPolicy(
                source=settings.source,
                limits=settings.limits,
                disk_mode=settings.disk_mode,
                revision=revision,
            ),
        )
    except (OSError, ValueError) as exc:
        if isinstance(exc, CandidateError):
            raise
        raise CandidateError("candidate_source_changed") from None


def _requirement(value: RegistryRequirement) -> str:
    """Validate each typed field before compiling a PEP 508 argument/file line."""
    if value.index is not None:
        raise CandidateError("candidate_context_unproven")
    try:
        specifier = str(SpecifierSet(value.specifier or ""))
        marker = str(Marker(value.marker)) if value.marker else None
        extras = f"[{','.join(value.extras)}]" if value.extras else ""
        text = f"{value.name}{extras}{specifier}" + (f"; {marker}" if marker else "")
        parsed = Requirement(text)
        if (
            parsed.url is not None
            or parsed.name != value.name
            or parsed.extras != set(value.extras)
        ):
            raise ValueError
        return text
    except ValueError:
        raise CandidateError("candidate_requirement_unsupported") from None


def _supported_intent(intent: InstallIntent, settings: CandidateSettings) -> None:
    if intent.main.name != "muli-agent" or intent.main.marker:
        raise CandidateError("candidate_requirement_unsupported")
    for requirement in (
        intent.main,
        *intent.with_requirements,
        *intent.constraints,
        *intent.overrides,
    ):
        _requirement(requirement)
    options = intent.options
    if options is not None and (
        options.index_url not in {None, "https://pypi.org/simple", "https://pypi.org/simple/"}
        or options.extra_index_urls
        or (options.find_links and options.find_links != (Path(settings.wheelhouse).as_uri(),))
    ):
        raise CandidateError("candidate_context_unproven")


def _write(path: Path, data: bytes) -> FileFingerprint:
    """Create once and fsync; never replace an earlier preparation attempt."""
    with directory(path.parent) as parent:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
            identity = FileIdentity.of(os.fstat(target.fileno()))
        os.fsync(parent)
    return FileFingerprint(
        path=str(path), identity=identity, digest=hashlib.sha256(data).hexdigest()
    )


def _copy_materials(policy: FrozenPolicy, target: Path, deadline: float) -> WheelhouseEvidence:
    limits = policy.settings.limits
    with directory(Path(policy.materials.path)) as source_directory:
        for wheel in policy.materials.wheels:
            _before(deadline)
            descriptor = os.open(
                wheel.name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=source_directory,
            )
            with os.fdopen(descriptor, "rb") as source:
                if FileIdentity.of(os.fstat(source.fileno())) != wheel.fingerprint.identity:
                    raise CandidateError("candidate_material_changed")
                with directory(target) as destination:
                    output = os.open(
                        wheel.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=destination,
                    )
                    hashed, consumed = hashlib.sha256(), 0
                    with os.fdopen(output, "wb") as writer:
                        while chunk := source.read(min(_CHUNK, limits.wheel_bytes - consumed + 1)):
                            _before(deadline)
                            consumed += len(chunk)
                            if consumed > limits.wheel_bytes:
                                raise CandidateError("candidate_wheel_budget")
                            writer.write(chunk)
                            hashed.update(chunk)
                        writer.flush()
                        os.fsync(writer.fileno())
                    os.fsync(destination)
                if (
                    consumed != wheel.fingerprint.identity.size
                    or hashed.hexdigest() != wheel.fingerprint.digest
                    or FileIdentity.of(os.fstat(source.fileno())) != wheel.fingerprint.identity
                    or FileIdentity.of(
                        os.stat(wheel.name, dir_fd=source_directory, follow_symlinks=False)
                    )
                    != wheel.fingerprint.identity
                ):
                    raise CandidateError("candidate_material_changed")
    return inspect_wheelhouse(target, limits, deadline=deadline)


_PROBE = """from __future__ import annotations
import importlib.metadata
import json
import sys
from pathlib import Path
import muli
from muli.config import ConfigFile, Settings
from muli.runtime import Runtime
Settings.model_validate_json(Path(sys.argv[1]).read_bytes())
scratch: Path = Path(sys.argv[2])
runtime: Runtime = Runtime(ConfigFile.from_bytes(scratch / 'config.json', b'{}'), scratch / 'data')
try:
    state = runtime.store.format_state
    if state is None:
        raise ValueError('candidate_fresh_format_missing')
    result: dict[str, object] = dict(
        python=sys.executable, prefix=sys.prefix, base_prefix=sys.base_prefix,
        package=str(Path(muli.__file__).absolute().parent),
        version=importlib.metadata.version('muli-agent'), module_version=muli.__version__,
        main_session=runtime.main_session()['id'], format=state.model_dump(mode='json'))
finally:
    runtime.discard_unstarted()
print(json.dumps(result))
"""


@dataclass(frozen=True, slots=True)
class ReadyCandidate:
    slot: SlotIdentity = field(repr=False)
    evidence: CandidateEvidence = field(repr=False)
    evidence_file: FileFingerprint = field(repr=False)

    @property
    def paths(self) -> LocalSlotPaths:
        from muli.managed_preparation import LocalSlotPaths

        return LocalSlotPaths(
            Path(self.slot.prefix), Path(self.slot.lexical_python), Path(self.slot.package.root)
        )


@dataclass(slots=True, repr=False)
class CandidateAttempt:
    """Host-owned preparation. No cleanup, process launch or selector effect here.

    The host keeps this object/path on all failures, including uncertain backend
    cleanup. Each specification is single-use; a failed install is never resent.
    Source.verify must include re-reading the host's independent policy object.
    """

    policy: FrozenPolicy
    request_id: str
    version: str
    root: Path
    identity: FileIdentity
    deadline: float
    copied: WheelhouseEvidence
    metadata: tuple[FileFingerprint, ...]
    _stage: str = "new"
    _uv_version: str | None = None

    @property
    def prefix(self) -> Path:
        return self.root / "tools/muli-agent"

    @property
    def python(self) -> Path:
        return self.prefix / "bin/python"

    def _check(self) -> None:
        _before(self.deadline)
        self.policy.source.verify()
        with directory(self.root, private=True) as descriptor:
            actual = os.fstat(descriptor)
            if (actual.st_dev, actual.st_ino) != (self.identity.device, self.identity.inode):
                raise CandidateError("candidate_attempt_changed")
        current_uv = FileFingerprint.model_validate(
            python_fingerprint(
                Path(self.policy.settings.uv_executable),
                self.policy.settings.limits.executable_bytes,
            )
        )
        if current_uv != self.policy.uv:
            raise CandidateError("candidate_uv_changed")
        with (
            directory(self.root / "credentials", private=True) as descriptor,
            os.scandir(descriptor) as entries,
        ):
            if next(entries, None) is not None:
                raise CandidateError("candidate_credentials_changed")

    def observe(self) -> None:
        """Bounded periodic byte threshold check; never claims a disk quota."""
        self._check()
        limits = self.policy.settings.limits
        end = min(self.deadline, time.monotonic() + limits.scan_seconds)
        totals: dict[str, int] = {"candidate": 0, "cache": 0, "materials": 0, "metadata": 0}
        count = 0

        def visit(descriptor: int, category: str, depth: int) -> None:
            nonlocal count
            if depth > 32:
                raise CandidateError("candidate_tree_budget")
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    _before(end)
                    count += 1
                    if count > limits.scan_entries:
                        raise CandidateError("candidate_tree_budget")
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue  # uv may concurrently retire its own temporary files.
                    group = category or (
                        "cache"
                        if entry.name in {"cache", "tmp"}
                        else "materials"
                        if entry.name == "wheelhouse"
                        else "candidate"
                        if entry.name in {"tools", "entrypoints", "python", "scratch"}
                        else "metadata"
                    )
                    if stat.S_ISDIR(info.st_mode):
                        try:
                            child = os.open(
                                entry.name,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=descriptor,
                            )
                        except FileNotFoundError:
                            continue
                        try:
                            current = os.fstat(child)
                            if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                                visit(child, group, depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                        totals[group] += info.st_size
                    else:
                        raise CandidateError("candidate_tree_unsupported")

        with directory(self.root, private=True) as descriptor:
            visit(descriptor, "", 0)
        if (
            totals["candidate"] > limits.candidate_bytes
            or totals["cache"] > limits.cache_bytes
            or totals["materials"] > limits.compressed_bytes
            or totals["metadata"] > limits.metadata_bytes
        ):
            raise CandidateError("candidate_disk_threshold")
        with directory(self.root, private=True) as descriptor:
            space = os.fstatvfs(descriptor)
            if space.f_bavail * space.f_frsize < limits.spare_bytes:
                raise CandidateError("candidate_disk_threshold")
        _before(end)

    def _materials(self) -> None:
        if (
            inspect_wheelhouse(
                self.root / "wheelhouse", self.policy.settings.limits, deadline=self.deadline
            )
            != self.copied
        ):
            raise CandidateError("candidate_material_changed")
        from muli.platforms.managed_launcher import fingerprint

        for expected in self.metadata:
            actual = FileFingerprint.model_validate(
                fingerprint(Path(expected.path), self.policy.settings.limits.metadata_bytes)
            )
            if actual != expected:
                raise CandidateError("candidate_attempt_changed")

    def _environment(self) -> tuple[tuple[str, str], ...]:
        root = self.root
        return tuple(
            sorted(
                {
                    "PATH": os.defpath,
                    "NETRC": str(root / "empty-netrc"),
                    "UV_TOOL_DIR": str(root / "tools"),
                    "UV_TOOL_BIN_DIR": str(root / "entrypoints"),
                    "UV_CACHE_DIR": str(root / "cache"),
                    "UV_PYTHON_INSTALL_DIR": str(root / "python"),
                    "UV_CREDENTIALS_DIR": str(root / "credentials"),
                    "UV_KEYRING_PROVIDER": "disabled",
                    "UV_LINK_MODE": "copy",
                    "UV_NO_PROGRESS": "1",
                    "TMPDIR": str(root / "tmp"),
                    "TEMP": str(root / "tmp"),
                    "TMP": str(root / "tmp"),
                    "MULI_HOME": str(root / "scratch/home"),
                }.items()
            )
        )

    def _spec(
        self, expected: str, pending: str, argv: tuple[str, ...], *, preflight: bool = False
    ) -> ProcessSpec:
        if self._stage != expected:
            raise CandidateError("candidate_stage_invalid")
        self.observe()
        self._materials()
        self.policy.verify(self.policy.expected, deadline=self.deadline)
        deadline = self.deadline
        if preflight:
            deadline = min(
                deadline, time.monotonic() + self.policy.settings.limits.preflight_seconds
            )
        self._stage = pending
        return ProcessSpec(
            argv,
            self._environment(),
            self.root,
            self.root,
            deadline,
            self.policy.settings.limits,
            self.policy.settings.disk_mode,
            self.observe,
        )

    def _accept(self, stage: str, output: ProcessOutput) -> None:
        if self._stage != stage:
            raise CandidateError("candidate_stage_invalid")
        if output.returncode != 0:
            self._stage = "failed"
            raise CandidateError("candidate_process_failed")
        if (
            len(output.stdout.encode()) + len(output.stderr.encode())
            > self.policy.settings.limits.output_bytes
        ):
            raise CandidateError("candidate_output_budget")
        self.observe()
        self._materials()

    def uv_version_spec(self) -> ProcessSpec:
        return self._spec(
            "new",
            "version_pending",
            (self.policy.settings.uv_executable, "--version"),
            preflight=True,
        )

    def accept_uv_version(self, output: ProcessOutput) -> None:
        self._accept("version_pending", output)
        match = _UV_OUTPUT.fullmatch(output.stdout)
        if match is None or match[1] not in _UV_VERSIONS:
            raise CandidateError("candidate_uv_unsupported")
        self._uv_version, self._stage = match[1], "version_verified"

    def install_spec(self) -> ProcessSpec:
        argv = [
            self.policy.settings.uv_executable,
            "tool",
            "install",
            "--offline",
            "--no-config",
            "--no-index",
            "--find-links",
            str(self.root / "wheelhouse"),
            "--no-build",
            "--no-python-downloads",
            "--no-compile-bytecode",
            "--keyring-provider",
            "disabled",
            "--python",
            self.policy.source.slot.lexical_python,
            "--constraints",
            str(self.root / "constraints.txt"),
        ]
        intent = self.policy.original_intent
        if intent.options is not None and intent.options.index_strategy is not None:
            argv.extend(("--index-strategy", intent.options.index_strategy))
        if intent.overrides:
            argv.extend(("--overrides", str(self.root / "overrides.txt")))
        for requirement in intent.with_requirements:
            argv.extend(("--with", _requirement(requirement)))
        argv.append(_requirement(intent.main))
        return self._spec("version_verified", "install_pending", tuple(argv))

    def accept_install(self, output: ProcessOutput) -> None:
        self._accept("install_pending", output)
        self._stage = "installed"

    def dependency_spec(self) -> ProcessSpec:
        return self._spec(
            "installed",
            "dependencies_pending",
            (
                self.policy.settings.uv_executable,
                "pip",
                "check",
                "--offline",
                "--no-config",
                "--python",
                str(self.python),
            ),
            preflight=True,
        )

    def accept_dependencies(self, output: ProcessOutput) -> None:
        self._accept("dependencies_pending", output)
        self._stage = "dependencies_verified"

    def probe_spec(self) -> ProcessSpec:
        return self._spec(
            "dependencies_verified",
            "probe_pending",
            (
                str(self.python),
                "-I",
                "-B",
                "-c",
                _PROBE,
                str(self.root / "configuration.json"),
                str(self.root / "scratch"),
            ),
            preflight=True,
        )

    def finish(self, output: ProcessOutput) -> ReadyCandidate:
        self._accept("probe_pending", output)
        try:
            value = _object(output.stdout.encode())
            if set(value) != {
                "python",
                "prefix",
                "base_prefix",
                "package",
                "version",
                "module_version",
                "main_session",
                "format",
            }:
                raise ValueError
            _IDENTIFIER.validate_python(value["main_session"], strict=True)
            FormatState.model_validate(value["format"])
            package = Path(value["package"])
            if (
                value["python"] != str(self.python)
                or value["prefix"] != str(self.prefix)
                or value["prefix"] == value["base_prefix"]
                or not package.is_relative_to(self.prefix)
                or value["version"] != self.version
                or value["module_version"] != self.version
            ):
                raise ValueError
            slot = register_local_slot(
                self.prefix,
                self.python,
                package,
                limits=self.policy.source.slot.package_limits,
                python_bytes=self.policy.source.slot.python_bytes,
            )
            if slot.compatibility.release != self.version or self._uv_version is None:
                raise ValueError

            def verify() -> None:
                self._check()
                verify_slot_files(slot)

            observation = observe_install_receipt(
                self.prefix / "uv-receipt.toml",
                uv_version=self._uv_version,
                verify=verify,
                limits=self.policy.source.receipt.limits,
            )
            _verify_deployed_intent(observation.parsed.intent, self)
            self.policy.verify(self.policy.expected, deadline=self.deadline)
            evidence = CandidateEvidence(
                request_id=self.request_id,
                source_installation=self.policy.source.slot.digest,
                binding_revision=self.policy.source.binding_revision,
                original_intent=self.policy.original_intent,
                deployed_installation=slot.digest,
                deployed_receipt=observation.receipt,
                deployed_intent=observation.parsed.intent,
                version=self.version,
                uv_version=self._uv_version,
                policy=self.policy.expected,
                materials=self.policy.materials.digest,
            )
            fingerprint = _write(
                self.root / "evidence.json",
                evidence.to_bytes(self.policy.settings.limits.metadata_bytes),
            )
            self.observe()
            self._stage = "ready"
            return ReadyCandidate(slot, evidence, fingerprint)
        except (OSError, ValueError, TypeError, KeyError):
            raise CandidateError("candidate_validation_failed") from None

    def verify_ready(self, ready: ReadyCandidate) -> None:
        """Recheck inside the host configuration lock immediately before switch."""
        if (
            self._stage != "ready"
            or ready.evidence.request_id != self.request_id
            or ready.slot.prefix != str(self.prefix)
            or ready.slot.lexical_python != str(self.python)
            or ready.evidence_file.path != str(self.root / "evidence.json")
        ):
            raise CandidateError("candidate_stage_invalid")
        self.observe()
        self._materials()
        self.policy.verify(self.policy.expected, deadline=self.deadline)
        verify_slot_files(ready.slot)

        def verify() -> None:
            self._check()
            verify_slot_files(ready.slot)

        observation = observe_install_receipt(
            self.prefix / "uv-receipt.toml",
            uv_version=ready.evidence.uv_version,
            verify=verify,
            limits=self.policy.source.receipt.limits,
        )
        from muli.platforms.managed_launcher import fingerprint

        if (
            observation.receipt != ready.evidence.deployed_receipt
            or observation.parsed.intent != ready.evidence.deployed_intent
            or ready.evidence.deployed_installation != ready.slot.digest
            or FileFingerprint.model_validate(
                fingerprint(
                    Path(ready.evidence_file.path), self.policy.settings.limits.metadata_bytes
                )
            )
            != ready.evidence_file
        ):
            raise CandidateError("candidate_evidence_changed")
        from muli.platforms.managed_launcher import read_file

        data, _ = read_file(
            self.root / "evidence.json", self.policy.settings.limits.metadata_bytes, private=True
        )
        if (
            CandidateEvidence.from_bytes(data, self.policy.settings.limits.metadata_bytes)
            != ready.evidence
        ):
            raise CandidateError("candidate_evidence_changed")


def _verify_deployed_intent(actual: InstallIntent, attempt: CandidateAttempt) -> None:
    original = attempt.policy.original_intent
    pin = RegistryRequirement(name="muli-agent", specifier=f"=={attempt.version}")
    if (
        _requirement(actual.main) != _requirement(original.main)
        or tuple(map(_requirement, actual.with_requirements))
        != tuple(map(_requirement, original.with_requirements))
        or tuple(map(_requirement, actual.constraints))
        != tuple(map(_requirement, (*original.constraints, pin)))
        or tuple(map(_requirement, actual.overrides))
        != tuple(map(_requirement, original.overrides))
        or actual.python != attempt.policy.source.slot.lexical_python
        or actual.options
        != ReceiptOptions(
            no_index=True,
            no_build=True,
            find_links=((attempt.root / "wheelhouse").as_uri(),),
            index_strategy=original.options.index_strategy if original.options else None,
        )
    ):
        raise CandidateError("candidate_receipt_mismatch")


def create_attempt(
    policy: FrozenPolicy,
    *,
    expected: ExpectedPolicy,
    request_id: str,
    version: str,
    configuration: bytes,
) -> CandidateAttempt:
    """Allocate once at the final path; errors leave owned material for the host.

    The deterministic path is known before this call: retained_root/candidate-ID.
    An existing path is never adopted, overwritten, deleted or used for a retry.
    """
    from muli.updates import release_version

    _platform()
    try:
        _IDENTIFIER.validate_python(request_id, strict=True)
        release_version(version)
        if (
            type(configuration) is not bytes
            or len(configuration) > policy.settings.limits.metadata_bytes
        ):
            raise CandidateError("candidate_configuration_budget")
        deadline = time.monotonic() + policy.settings.limits.seconds
        policy.verify(expected, deadline=deadline)
        root = Path(policy.settings.retained_root)
        path = root / f"candidate-{request_id}"
        protected = (
            *policy.source.protected_roots,
            Path(policy.source.slot.prefix),
            Path(policy.materials.path),
        )
        if any(
            path == old or path.is_relative_to(old) or old.is_relative_to(path) for old in protected
        ):
            raise CandidateError("candidate_path_conflict")
        with directory(root, private=True) as descriptor:
            free = os.fstatvfs(descriptor)
            limits = policy.settings.limits
            required = (
                limits.candidate_bytes
                + limits.cache_bytes
                + limits.spare_bytes
                + sum(w.fingerprint.identity.size for w in policy.materials.wheels)
            )
            if free.f_bavail * free.f_frsize < required:
                raise CandidateError("candidate_space_unavailable")
            os.mkdir(path.name, mode=0o700, dir_fd=descriptor)
            os.fsync(descriptor)
        with directory(path, private=True) as descriptor:
            identity = FileIdentity.of(os.fstat(descriptor))
            for name in (
                "tools",
                "entrypoints",
                "cache",
                "tmp",
                "wheelhouse",
                "python",
                "credentials",
                "scratch",
            ):
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            os.fsync(descriptor)
        intent = policy.original_intent
        pin = RegistryRequirement(name="muli-agent", specifier=f"=={version}")
        constraints = "\n".join(map(_requirement, (*intent.constraints, pin))) + "\n"
        overrides = "\n".join(map(_requirement, intent.overrides)) + "\n"
        metadata = tuple(
            _write(path / name, data)
            for name, data in (
                ("empty-netrc", b""),
                ("constraints.txt", constraints.encode()),
                ("overrides.txt", overrides.encode()),
                ("configuration.json", configuration),
            )
        )
        copied = _copy_materials(policy, path / "wheelhouse", deadline)
        result = CandidateAttempt(
            policy, request_id, version, path, identity, deadline, copied, metadata
        )
        result.observe()
        policy.verify(expected, deadline=deadline)
        return result
    except CandidateError:
        raise
    except (ValueError, OSError):
        raise CandidateError("candidate_attempt_unavailable") from None
