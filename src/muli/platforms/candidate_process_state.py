"""Private plan attachment for one candidate start, never a public capability."""

from __future__ import annotations

import hashlib
import inspect
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from muli.candidate_policy import CandidateError, CandidateLimits, ProcessSpec
from muli.management_transaction import Identifier, StrictRecord, _object
from muli.platforms.installation import FileFingerprint, _absolute, _canonical
from muli.platforms.linux_transaction import ProcessIdentity, UnitSnapshot, clock_reading
from muli.platforms.managed_launcher import fingerprint, read_file
from muli.platforms.transaction_files import _open_directory

_STATE_BYTES = 32_768


class AcceptedUnit(StrictRecord):
    """A manager has accepted this exact start; not permission to execute it."""

    invocation: str = Field(pattern=r"^[a-f0-9]{32}$")
    cgroup: str = Field(max_length=4096)
    pid: int = Field(ge=0)
    start_ticks: int | None = Field(default=None, ge=1)

    @classmethod
    def capture(cls, unit: UnitSnapshot, process: ProcessIdentity | None = None) -> Self:
        return cls(
            invocation=unit.invocation,
            cgroup=unit.cgroup,
            pid=unit.pid,
            start_ticks=process.start_ticks if process is not None else None,
        )


class PendingProcess(StrictRecord):
    """Only the authenticated host may read/write this existing plan field.

    It contains the fixed stdlib worker identity, not the candidate command,
    environment or gate nonce. Its phase never grants permission to run argv.
    """

    version: Literal[1] = 1
    phase: Literal["pending", "observed", "settled"] = "pending"
    unit: str = Field(pattern=r"^muli-candidate-[a-f0-9]{32}\.service$")
    boot: Identifier
    root: str = Field(max_length=4096)
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    uid: int = Field(ge=0)
    python: str = Field(max_length=4096)
    worker_file: FileFingerprint
    control_file: FileFingerprint
    deadline: float = Field(gt=0, allow_inf_nan=False)
    runtime_us: int = Field(ge=1, le=180_000_000)
    limits: CandidateLimits
    accepted: AcceptedUnit | None = None

    @model_validator(mode="after")
    def valid_binding(self) -> Self:
        root = Path(_absolute(self.root))
        _absolute(self.python)
        if (
            Path(self.worker_file.path).name != "_candidate_worker.py"
            or Path(self.control_file.path).parent != root
            or not re.fullmatch(r"process-[a-f0-9]{32}\.json", Path(self.control_file.path).name)
            or self.runtime_us > self.limits.seconds * 1_000_000
            or (self.phase == "pending" and self.accepted is not None)
            or (self.phase == "observed" and self.accepted is None)
        ):
            raise ValueError("candidate_process_state_invalid")
        if self.accepted is not None and (
            self.accepted.invocation == "0" * 32
            or (
                self.accepted.cgroup
                and (
                    not self.accepted.cgroup.startswith("/")
                    or Path(self.accepted.cgroup).name != self.unit
                    or any(p in {"", ".", ".."} for p in self.accepted.cgroup[1:].split("/"))
                )
            )
            or (self.accepted.start_ticks is not None and self.accepted.pid == 0)
        ):
            raise ValueError("candidate_process_state_invalid")
        return self

    @property
    def worker(self) -> tuple[str, ...]:
        return self.python, "-I", "-S", self.worker_file.path, self.control_file.path

    @property
    def binding_digest(self) -> str:
        return hashlib.sha256(
            _canonical(self.model_dump(mode="json", exclude={"phase", "accepted"}))
        ).hexdigest()

    @classmethod
    def from_json(cls, data: bytes, limit: int) -> Self:
        try:
            if type(limit) is not int or limit <= 0 or len(data) > min(limit, _STATE_BYTES):
                raise ValueError
            return cls.model_validate_json(_canonical(_object(data)))
        except (ValueError, TypeError, RecursionError):
            raise CandidateError("candidate_process_state_invalid") from None

    def check_files(self) -> None:
        descriptor = _open_directory(Path(self.root))
        try:
            info = os.fstat(descriptor)
            if (
                (info.st_dev, info.st_ino, info.st_uid) != (self.device, self.inode, self.uid)
                or self.uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise CandidateError("candidate_process_binding_changed")
        finally:
            os.close(descriptor)
        for expected, limit in (
            (self.worker_file, self.limits.executable_bytes),
            (self.control_file, self.limits.metadata_bytes),
        ):
            observed = FileFingerprint.model_validate(fingerprint(Path(expected.path), limit))
            if observed != expected:
                raise CandidateError("candidate_process_binding_changed")
        read_file(Path(self.control_file.path), self.limits.metadata_bytes, private=True)


PersistProcess = Callable[[PendingProcess], None]


def current_boot() -> str:
    """Use the existing bounded kernel boot-id reader, never a caller's claim."""
    return clock_reading().boot_id


class ProcessPersistence:
    def __init__(self, value: PendingProcess, persist: PersistProcess) -> None:
        self.value, self.persist = value, persist
        self.acknowledged = False

    @classmethod
    def create(
        cls,
        spec: ProcessSpec,
        name: str,
        worker: tuple[str, ...],
        runtime_us: int,
        persist: PersistProcess,
    ) -> Self:
        descriptor = _open_directory(spec.private_root)
        try:
            info = os.fstat(descriptor)
            value = PendingProcess(
                unit=name,
                boot=current_boot(),
                root=str(spec.private_root),
                device=info.st_dev,
                inode=info.st_ino,
                uid=info.st_uid,
                python=worker[0],
                worker_file=FileFingerprint.model_validate(
                    fingerprint(Path(worker[3]), spec.limits.executable_bytes)
                ),
                control_file=FileFingerprint.model_validate(
                    fingerprint(Path(worker[4]), spec.limits.metadata_bytes)
                ),
                deadline=spec.deadline,
                runtime_us=runtime_us,
                limits=spec.limits,
            )
        finally:
            os.close(descriptor)
        state = cls(value, persist)
        state.emit(value)
        return state

    def emit(self, value: PendingProcess) -> None:
        if value.binding_digest != self.value.binding_digest:
            raise CandidateError("candidate_process_binding_changed")
        if len(_canonical(value.model_dump(mode="json"))) > _STATE_BYTES:
            raise CandidateError("candidate_process_state_budget")
        self.value = value  # Keep the observed identity even if a commit loses its return.
        self.acknowledged = False
        result = cast(Callable[[PendingProcess], object], self.persist)(value)
        if result is not None:
            if inspect.iscoroutine(result):
                result.close()
            raise CandidateError("candidate_process_persist_must_be_sync")
        self.acknowledged = True

    def accept(self, accepted: AcceptedUnit) -> None:
        previous = self.value.accepted
        if previous is not None:
            if previous.invocation != accepted.invocation or (
                previous.cgroup and accepted.cgroup and previous.cgroup != accepted.cgroup
            ):
                raise CandidateError("candidate_process_binding_changed")
            return
        self.emit(self.value.model_copy(update={"phase": "observed", "accepted": accepted}))

    def settle(self) -> PendingProcess:
        value = self.value.model_copy(update={"phase": "settled"})
        self.emit(value)
        return value
