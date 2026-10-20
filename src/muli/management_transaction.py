"""Private Linux transaction records; no Runtime, Store or service integration.

This is a protocol primitive, not a recoverable restart entry point. The host must
validate Settings, own the database lease and supply real projection receipts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Phase = Literal[
    "prepared",
    "draining",
    "activating",
    "verifying",
    "rolling_back",
    "committed",
    "rolled_back",
    "aborted",
    "recovery_required",
]
Intent = Literal[
    "none",
    "stop_base",
    "install_target",
    "start_target",
    "stop_target",
    "restore_base",
    "start_base",
    "verify_base",
]
Activation = Literal["pending", "released", "released_degraded", "blocked"]
Role = Literal["base", "target"]
Code = Literal[
    "cancelled",
    "drain_timeout",
    "deadline_exceeded",
    "clock_invalid",
    "foreign_state",
    "health_failed",
    "backend_failed",
    "ambiguous_start",
    "activation_failed",
    "snapshot_invalid",
]


class TransactionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


class JournalLimits(StrictRecord):
    max_snapshot_bytes: int = Field(default=1_048_576, ge=1024, le=4_194_304)
    max_record_bytes: int = Field(default=32_768, ge=4096, le=131_072)
    max_entries: int = Field(default=32, ge=8, le=128)


class ClockReading(StrictRecord):
    boot_id: Identifier
    wall: float = Field(ge=0, allow_inf_nan=False)
    monotonic: float = Field(ge=0, allow_inf_nan=False)


class Deadline(StrictRecord):
    created: ClockReading
    seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)
    rollback_reserve: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_reserve(self) -> Self:
        if self.rollback_reserve >= self.seconds:
            raise ValueError("invalid_reserve")
        return self

    def remaining(self, now: ClockReading) -> float:
        if now.boot_id == self.created.boot_id:
            elapsed = now.monotonic - self.created.monotonic
        else:
            elapsed = now.wall - self.created.wall
        if elapsed < 0:
            raise TransactionError("clock_invalid")
        return max(0.0, self.seconds - elapsed)


class ServiceIdentity(StrictRecord):
    installation: Digest
    service: Digest
    store_instance: Identifier
    main_session: Identifier
    origin_boot: Identifier


class SnapshotReference(StrictRecord):
    digest: Digest
    semantic_digest: Digest
    size: int = Field(ge=2, le=4_194_304)


class HealthProof(StrictRecord):
    generation: Identifier
    challenge: Identifier = Field(repr=False)
    boot: Identifier
    installation: Digest
    service: Digest
    store_instance: Identifier
    main_session: Identifier
    configuration: Digest
    core_ready: bool
    admission: Literal["paused", "released", "released_degraded", "blocked"]


class Revision(StrictRecord):
    generation: Identifier
    revision: int = Field(ge=1)


class ProjectionReceipt(Revision):
    store_instance: Identifier


class TransactionRecord(StrictRecord):
    protocol: Literal[1] = 1
    request_id: Identifier
    generation: Identifier
    revision: int = Field(ge=1)
    action: Literal["configure", "restart"]
    identity: ServiceIdentity
    base: SnapshotReference
    target: SnapshotReference
    deadline: Deadline
    phase: Phase = "prepared"
    intent: Intent = "none"
    challenge: Identifier = Field(repr=False)
    rollback_challenge: Identifier | None = Field(default=None, repr=False)
    rollback_attempted: bool = False
    health: HealthProof | None = Field(default=None, repr=False)
    activation: Activation = "pending"
    code: Code | None = None
    failure_code: Code | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        intents = {
            "activating": {"stop_base", "install_target", "start_target"},
            "rolling_back": {"stop_target", "restore_base", "start_base", "verify_base"},
        }
        if self.intent not in intents.get(self.phase, {"none"}):
            raise ValueError("invalid_intent")
        if self.rollback_attempted != (self.rollback_challenge is not None):
            raise ValueError("invalid_rollback")
        if self.phase in {"rolling_back", "rolled_back"} and not self.rollback_attempted:
            raise ValueError("missing_rollback")
        if self.rollback_attempted and self.phase not in {
            "rolling_back",
            "rolled_back",
            "recovery_required",
        }:
            raise ValueError("invalid_rollback_phase")
        if self.phase not in {"committed", "rolled_back"} and self.activation != "pending":
            raise ValueError("invalid_activation")
        if self.phase in {"committed", "rolled_back"} and self.health is None:
            raise ValueError("missing_health")
        if self.health is not None:
            role: Role = "base" if self.phase == "rolled_back" else "target"
            released = self.activation in {"released", "released_degraded"}
            if self.phase not in {"committed", "rolled_back"} or not self.accepts(
                self.health, role, released=released
            ):
                raise ValueError("invalid_health")
            if released and self.activation != self.health.admission:
                raise ValueError("invalid_release")
        return self

    @property
    def position(self) -> Revision:
        return Revision(generation=self.generation, revision=self.revision)

    @property
    def resolved(self) -> bool:
        return self.phase == "aborted" or (
            self.phase in {"committed", "rolled_back"}
            and self.activation in {"released", "released_degraded"}
        )

    def public(self) -> dict[str, str | int | bool | None]:
        return {
            "request_id": self.request_id,
            "generation": self.generation,
            "revision": self.revision,
            "phase": self.phase,
            "activation": self.activation,
            "resolved": self.resolved,
            "code": self.code,
            "failure_code": self.failure_code,
        }

    def accepts(self, proof: HealthProof, role: Role, *, released: bool = False) -> bool:
        challenge = self.challenge if role == "target" else self.rollback_challenge
        reference = self.target if role == "target" else self.base
        return (
            proof.generation == self.generation
            and proof.challenge == challenge
            and proof.installation == self.identity.installation
            and proof.service == self.identity.service
            and proof.store_instance == self.identity.store_instance
            and proof.main_session == self.identity.main_session
            and proof.configuration == reference.digest
            and proof.core_ready
            and proof.boot != self.identity.origin_boot
            and (
                proof.admission in {"released", "released_degraded"}
                if released
                else proof.admission == "paused"
            )
        )


class Transition(StrictRecord):
    phase: Phase
    intent: Intent = "none"
    activation: Activation = "pending"
    code: Code | None = None
    proof: HealthProof | None = None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("invalid_constant")


def _object(data: bytes) -> dict[str, Any]:
    try:
        value: Any = json.loads(data, object_pairs_hook=_unique, parse_constant=_constant)
        if not isinstance(value, dict):
            raise ValueError("not_object")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise TransactionError("invalid_json") from None


def _snapshot(data: bytes, limit: int) -> SnapshotReference:
    if len(data) > limit:
        raise TransactionError("snapshot_budget")
    value = _object(data)
    try:
        semantic = json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    except (ValueError, RecursionError):
        raise TransactionError("invalid_json") from None
    return SnapshotReference(
        digest=_digest(data), semantic_digest=_digest(semantic.encode()), size=len(data)
    )


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _Directory(AbstractContextManager["_Directory"]):
    """Anchor private files to an fd; never open a FIFO or follow a final link."""

    def __init__(self, path: Path) -> None:
        if sys.platform != "linux":
            raise TransactionError("platform_unsupported")
        self.path = path.absolute()
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in self.path.parts[1:]:
                try:
                    next_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    next_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=descriptor,
                    )
                os.close(descriptor)
                descriptor = next_fd
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise TransactionError("directory_not_private")
            self.fd = descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def __exit__(self, *args: object) -> None:
        os.close(self.fd)

    def check(self) -> None:
        if _identity(os.stat(self.path, follow_symlinks=False)) != _identity(os.fstat(self.fd)):
            raise TransactionError("directory_changed")

    def read(self, name: str, limit: int) -> bytes:
        self.check()
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd
        )
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise TransactionError("file_not_private_regular")
            if before.st_size > limit:
                raise TransactionError("file_budget")
            data = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
        current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if (
            len(data) > limit
            or _file_identity(before) != _file_identity(after)
            or _file_identity(current) != _file_identity(before)
        ):
            raise TransactionError("file_changed")
        self.check()
        return data

    def replace(self, name: str, data: bytes) -> None:
        self.check()
        temporary = ".tmp-" + secrets.token_hex(16)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.fd,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self.check()
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass


class FixedLock(AbstractContextManager["FixedLock"]):
    """A non-blocking, non-inheritable flock on an inode that is never replaced."""

    def __init__(self, directory: _Directory, name: str) -> None:
        self.directory, self.name = directory, name
        self.fd: int | None = None

    def __enter__(self) -> Self:
        import fcntl

        if self.fd is not None:
            raise TransactionError("lock_already_owned")
        self.directory.check()
        descriptor = os.open(
            self.name,
            os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory.fd,
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise TransactionError("invalid_lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TransactionError("lock_busy") from None
            self.fd = descriptor
            self.check()
            os.fsync(descriptor)
            os.fsync(self.directory.fd)
        except BaseException:
            self.fd = None
            os.close(descriptor)
            raise
        return self

    def check(self) -> None:
        self.directory.check()
        if self.fd is None or _identity(os.fstat(self.fd)) != _identity(
            os.stat(self.name, dir_fd=self.directory.fd, follow_symlinks=False)
        ):
            raise TransactionError("lock_not_owned")

    def __exit__(self, *args: object) -> None:
        if self.fd is not None:
            descriptor, self.fd = self.fd, None
            os.close(descriptor)


_EDGES: dict[str, set[str]] = {
    "prepared": {"draining", "aborted", "recovery_required"},
    "draining": {"prepared", "activating", "aborted", "recovery_required"},
    "activating": {"activating", "verifying", "rolling_back", "recovery_required"},
    "verifying": {"committed", "rolling_back", "recovery_required"},
    "rolling_back": {"rolling_back", "rolled_back", "recovery_required"},
    "committed": {"committed"},
    "rolled_back": {"rolled_back"},
}
_INTENTS = {
    "stop_base": "install_target",
    "install_target": "start_target",
    "stop_target": "restore_base",
    "restore_base": "start_base",
    "start_base": "verify_base",
}


class TransactionJournal(AbstractContextManager["TransactionJournal"]):
    def __init__(self, root: Path, database: Path, limits: JournalLimits | None = None) -> None:
        self.limits = limits or JournalLimits()
        self.directory = _Directory(root)
        try:
            # Resolve DB aliases before choosing its independent lease inode.
            path = database.resolve()
            self.writer_directory = _Directory(path.parent / ("." + path.name + "-writer"))
        except BaseException:
            self.directory.__exit__()
            raise

    def __exit__(self, *args: object) -> None:
        self.writer_directory.__exit__()
        self.directory.__exit__()

    def writer_lease(self) -> FixedLock:
        return FixedLock(self.writer_directory, "lease")

    def locked(self) -> JournalControl:
        return JournalControl(self)

    def read(self) -> TransactionRecord | None:
        try:
            data = self.directory.read("state.json", self.limits.max_record_bytes)
        except FileNotFoundError:
            return None
        try:
            return TransactionRecord.model_validate(_object(data))
        except ValueError:
            raise TransactionError("invalid_record") from None

    def snapshot(self, record: TransactionRecord, role: Role) -> bytes:
        reference = record.base if role == "base" else record.target
        data = self.directory.read(
            f"{record.generation}-{role}.json", self.limits.max_snapshot_bytes
        )
        if _snapshot(data, self.limits.max_snapshot_bytes) != reference:
            raise TransactionError("snapshot_invalid")
        return data


class JournalControl(FixedLock):
    def __init__(self, journal: TransactionJournal) -> None:
        super().__init__(journal.directory, "lock")
        self.journal = journal
        self._poisoned = False

    def check(self) -> None:
        super().check()
        if self._poisoned:
            raise TransactionError("journal_unconfirmed")

    def read(self) -> TransactionRecord | None:
        self.check()
        record = self.journal.read()
        # A previous writer may have died after replace but before directory
        # fsync. A lock owner confirms the visible record before any effect;
        # ordinary lock-free observers never claim this durability barrier.
        try:
            os.fsync(self.directory.fd)
        except BaseException:
            self._poisoned = True
            raise
        return record

    def _replace(self, name: str, data: bytes) -> None:
        self.check()
        try:
            self.directory.replace(name, data)
        except BaseException:
            self._poisoned = True
            raise

    def _write(self, record: TransactionRecord) -> TransactionRecord:
        self.check()
        data = record.model_dump_json().encode()
        if len(data) > self.journal.limits.max_record_bytes:
            raise TransactionError("record_budget")
        self._replace("state.json", data)
        return record

    def prepare(
        self,
        *,
        request_id: str,
        action: Literal["configure", "restart"],
        identity: ServiceIdentity,
        base: bytes,
        target: bytes,
        deadline: Deadline,
        writer: FixedLock,
        projected: ProjectionReceipt | None = None,
    ) -> TransactionRecord:
        self.check()
        writer.check()
        if writer.name != "lease" or _identity(os.fstat(writer.directory.fd)) != _identity(
            os.fstat(self.journal.writer_directory.fd)
        ):
            raise TransactionError("writer_lease_required")
        base_ref = _snapshot(base, self.journal.limits.max_snapshot_bytes)
        target_ref = _snapshot(target, self.journal.limits.max_snapshot_bytes)
        if action == "restart" and base_ref != target_ref:
            raise TransactionError("restart_changes_configuration")
        previous = self.read()
        if previous is not None:
            if previous.request_id == request_id:
                if (
                    previous.identity != identity
                    or previous.action != action
                    or previous.base != base_ref
                    or previous.target != target_ref
                ):
                    raise TransactionError("request_conflict")
                return previous
            if not previous.resolved:
                raise TransactionError("transaction_in_progress")
            if projected != ProjectionReceipt(
                **previous.position.model_dump(), store_instance=previous.identity.store_instance
            ):
                raise TransactionError("projection_required")
            if previous.identity.store_instance != identity.store_instance:
                raise TransactionError("store_changed")
        record = TransactionRecord(
            request_id=request_id,
            action=action,
            generation=secrets.token_hex(16),
            revision=1,
            identity=identity,
            base=base_ref,
            target=target_ref,
            deadline=deadline,
            challenge=secrets.token_hex(32),
        )
        entries: list[str] = []
        with os.scandir(self.directory.fd) as iterator:
            for entry in iterator:
                entries.append(entry.name)
                if len(entries) > self.journal.limits.max_entries:
                    raise TransactionError("directory_budget")
        # Only orphaned snapshots/temporary files are collectible. The currently
        # referenced generation remains intact until its successor is durable.
        for name in entries:
            if re.fullmatch(
                r"(?:[a-f0-9]{32}-(?:base|target)\.json|\.tmp-[a-f0-9]{32})", name
            ) and not (previous and name.startswith(previous.generation + "-")):
                self.directory.read(name, self.journal.limits.max_snapshot_bytes)
                os.unlink(name, dir_fd=self.directory.fd)
        os.fsync(self.directory.fd)
        self._replace(f"{record.generation}-base.json", base)
        self._replace(f"{record.generation}-target.json", target)
        return self._write(record)

    def advance(self, expected: Revision, transition: Transition) -> TransactionRecord:
        self.check()
        record = self.read()
        if record is None or record.position != expected:
            raise TransactionError("stale_revision")
        if transition.phase not in _EDGES.get(record.phase, set()):
            raise TransactionError("invalid_transition")
        if (
            transition.phase == record.phase
            and record.phase in {"activating", "rolling_back"}
            and transition.intent != _INTENTS.get(record.intent)
        ):
            raise TransactionError("invalid_intent_transition")
        if (
            transition.phase == "activating"
            and record.phase != "activating"
            and transition.intent != "stop_base"
        ):
            raise TransactionError("invalid_intent_transition")
        if transition.phase == "verifying" and record.intent != "start_target":
            raise TransactionError("invalid_intent_transition")
        if (
            transition.phase == "rolled_back"
            and record.phase != "rolled_back"
            and record.intent != "verify_base"
        ):
            raise TransactionError("invalid_intent_transition")
        updates: dict[str, Any] = transition.model_dump(exclude={"proof"})
        updates["revision"] = record.revision + 1
        if record.failure_code is None and transition.code is not None:
            updates["failure_code"] = transition.code
        if transition.phase == "rolling_back" and record.phase != "rolling_back":
            if record.rollback_attempted or transition.intent != "stop_target":
                raise TransactionError("rollback_already_attempted")
            updates.update(rollback_attempted=True, rollback_challenge=secrets.token_hex(32))
        if transition.phase in {"committed", "rolled_back"}:
            role: Role = "target" if transition.phase == "committed" else "base"
            proof = transition.proof
            decided = transition.phase == record.phase
            if decided and transition.activation == "blocked":
                if transition.code is None or record.activation != "pending" or proof is not None:
                    raise TransactionError("invalid_release_proof")
            else:
                released = transition.activation in {"released", "released_degraded"}
                if proof is None or not record.accepts(proof, role, released=released):
                    raise TransactionError("invalid_health_proof")
                if decided:
                    if (
                        record.activation not in {"pending", "blocked"}
                        or record.health is None
                        or (released and transition.activation != proof.admission)
                        or (
                            not released
                            and record.activation == "blocked"
                            and proof.boot == record.health.boot
                        )
                    ):
                        raise TransactionError("invalid_release_proof")
                elif transition.activation != "pending":
                    raise TransactionError("invalid_activation")
                updates["health"] = proof.model_dump()
        return self._write(TransactionRecord.model_validate({**record.model_dump(), **updates}))
