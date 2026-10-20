"""Cooperative execution admission and passive transaction boots.

No service commands are issued here. Installation/service digests are supplied
by the host; a real Linux capability verifier remains a separate responsibility.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Self

from muli.management_transaction import (
    ClockReading,
    FixedLock,
    HealthProof,
    Role,
    TransactionJournal,
    TransactionRecord,
)
from muli.platforms.transaction_files import ConfigurationError, ConfigurationFile


class MaintenanceError(ValueError):
    def __init__(self, code: str, reason: str = "") -> None:
        self.code, self.reason = code, reason
        super().__init__(code)


class Admission:
    """One event-loop gate; owners are actual Tasks, never resettable counters."""

    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self._released = asyncio.Event()
        if allowed:
            self._released.set()
        self._owners: dict[asyncio.Task[Any], int] = {}

    def pause(self) -> None:
        self.allowed = False
        self._released.clear()

    def release(self) -> bool:
        if self.allowed:
            return False
        self.allowed = True
        self._released.set()
        return True

    async def wait(self) -> None:
        while not self.allowed and asyncio.current_task() not in self._owners:
            await self._released.wait()

    def require(self) -> None:
        if not self.allowed and asyncio.current_task() not in self._owners:
            raise MaintenanceError("execution_paused")

    @property
    def idle(self) -> bool:
        return not self._owners

    @contextmanager
    def operation(self) -> Iterator[None]:
        # Enter immediately after a successful wait, with no intervening await.
        self.require()
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("effect_requires_task")
        self._owners[task] = self._owners.get(task, 0) + 1
        try:
            yield
        finally:
            count = self._owners[task] - 1
            if count:
                self._owners[task] = count
            else:
                del self._owners[task]


def configuration_digest(path: Path, limit: int) -> str:
    """Read exact bounded bytes without blocking on a replaced FIFO or link."""
    try:
        with ConfigurationFile(path, limit) as source:
            return source.read().digest
    except ConfigurationError as exc:
        raise MaintenanceError(str(exc)) from None


class BootPermit:
    """Own the DB lease before ConfigFile/Store, until all writers have closed.

    The caller owns the journal's directory descriptors for this whole lifetime.
    Only a supported Linux host can construct this optional transaction path.
    """

    def __init__(
        self,
        journal: TransactionJournal,
        config_path: Path,
        boot: str,
        installation: str,
        service: str,
        lease: FixedLock,
        record: TransactionRecord | None,
        role: Role | None,
    ) -> None:
        self.journal, self.config_path = journal, config_path.absolute()
        self.boot, self.installation, self.service = boot, installation, service
        self.lease, self.record, self.role = lease, record, role
        self.probation = role is not None

    @classmethod
    def acquire(
        cls,
        journal: TransactionJournal,
        config_path: Path,
        *,
        boot: str,
        installation: str,
        service: str,
    ) -> Self:
        lease = journal.writer_lease().__enter__()
        try:
            record = journal.read()
            role: Role | None = None
            if record is not None and not record.resolved:
                if (
                    installation != record.identity.installation
                    or service != record.identity.service
                ):
                    raise MaintenanceError("foreign_installation")
                if boot == record.identity.origin_boot:
                    raise MaintenanceError("boot_reused")
                if record.phase in {"verifying", "committed"} or (
                    record.phase == "activating" and record.intent == "start_target"
                ):
                    role = "target"
                elif record.phase == "rolled_back" or (
                    record.phase == "rolling_back"
                    and record.intent in {"start_base", "verify_base"}
                ):
                    role = "base"
                else:
                    raise MaintenanceError("boot_not_permitted")
                reference = record.target if role == "target" else record.base
                if (
                    configuration_digest(config_path, journal.limits.max_snapshot_bytes)
                    != reference.digest
                ):
                    raise MaintenanceError("configuration_changed")
                # This is a read-only identity check, not Store construction or migration.
                with closing(
                    sqlite3.connect(journal.database.as_uri() + "?mode=ro", uri=True)
                ) as db:
                    values = dict(
                        db.execute(
                            "SELECT name,value FROM runtime_state WHERE name IN ('store_instance_v1','main_session')"
                        )
                    )
                    if (
                        values
                        != {
                            "store_instance_v1": record.identity.store_instance,
                            "main_session": record.identity.main_session,
                        }
                        or db.execute(
                            "SELECT 1 FROM objects WHERE kind='session' AND id=?",
                            (record.identity.main_session,),
                        ).fetchone()
                        is None
                    ):
                        raise MaintenanceError("store_identity_changed")
            return cls(journal, config_path, boot, installation, service, lease, record, role)
        except BaseException:
            lease.__exit__()
            raise

    def check_paths(self, config_path: Path, database: Path) -> None:
        self.lease.check()
        if (
            config_path.absolute() != self.config_path
            or database.resolve() != self.journal.database
        ):
            raise MaintenanceError("boot_paths_changed")

    def close_after_writers(self) -> None:
        self.lease.__exit__()


class Maintenance:
    """A host binds local owners and activation; unsealed coverage cannot prove drain."""

    def __init__(self, permit: BootPermit | None = None) -> None:
        self.permit = permit
        self.admission = Admission(allowed=not (permit and permit.probation))
        self._owners: dict[str, Callable[[], bool]] = {}
        self._sealed = False
        self._core: tuple[str, str] | None = None
        self._activate: Callable[[], Awaitable[None]] | None = None
        self._wake: list[Callable[[], None]] = []
        self._activation: asyncio.Task[None] | None = None
        self._activation_failed = False
        self._unsupported: dict[str, Callable[[], bool]] = {}
        self._configuration_free = asyncio.Event()
        self._configuration_wake: list[Callable[[], None]] = []
        self.reconcile()

    def unsupported(self, reason: str, present: Callable[[], bool]) -> None:
        self._unsupported[reason] = present

    @property
    def unsupported_reason(self) -> str | None:
        return next((reason for reason, present in self._unsupported.items() if present()), None)

    def require_support(self) -> None:
        if reason := self.unsupported_reason:
            raise MaintenanceError("maintenance_unsupported", reason)

    def owner(self, name: str, idle: Callable[[], bool]) -> None:
        if self._sealed or name in self._owners:
            raise RuntimeError("maintenance_owner_already_bound")
        self._owners[name] = idle

    def seal(self, required: set[str]) -> None:
        if set(self._owners) != required:
            raise RuntimeError("maintenance_coverage_incomplete")
        self._sealed = True

    def bind_activation(self, activate: Callable[[], Awaitable[None]]) -> None:
        if self._activate is not None:
            raise RuntimeError("maintenance_activation_already_bound")
        self._activate = activate

    def on_release(self, wake: Callable[[], None]) -> None:
        self._wake.append(wake)

    def on_configuration_release(self, wake: Callable[[], None]) -> None:
        self._configuration_wake.append(wake)

    def reconcile(self) -> None:
        """The host calls this after prepare or the controller's final notification."""
        if self.configuration_blocked:
            self._configuration_free.clear()
        elif not self._configuration_free.is_set():
            self._configuration_free.set()
            for wake in self._configuration_wake:
                wake()

    async def wait_configuration(self) -> None:
        self.reconcile()
        while self.configuration_blocked:
            self._configuration_free.clear()
            await self._configuration_free.wait()

    def prepared(self, store_instance: str, main_session: str) -> None:
        if self.permit and self.permit.probation:
            record = self.permit.record
            assert record is not None
            if (store_instance, main_session) != (
                record.identity.store_instance,
                record.identity.main_session,
            ):
                raise MaintenanceError("store_identity_changed")
        self._core = store_instance, main_session

    def current(self) -> TransactionRecord | None:
        return self.permit.journal.read() if self.permit else None

    @property
    def pending(self) -> bool:
        record = self.current()
        return record is not None and not record.resolved

    def require_configuration(self) -> None:
        self.reconcile()
        if self.configuration_blocked:
            self._configuration_free.clear()
            raise MaintenanceError("transaction_in_progress")

    @property
    def configuration_blocked(self) -> bool:
        return not self.admission.allowed or self.pending

    @property
    def accepts_external_input(self) -> bool:
        # Existing instances may keep receiving durable owner input while paused.
        # A passive replacement cannot start external polling or accept webhooks.
        if not self.permit or not self.permit.probation:
            return True
        record = self.current()
        return bool(record and record.identity.origin_boot == self.permit.boot)

    def _record(self, generation: str, boot: str, nonce: str) -> TransactionRecord:
        permit = self.permit
        record = self.current()
        if permit is None or record is None:
            raise MaintenanceError("transaction_unavailable")
        role = self._role(record)
        challenge = record.rollback_challenge if role == "base" else record.challenge
        if (
            record.generation != generation
            or permit.boot != boot
            or not challenge
            or not hmac.compare_digest(nonce, challenge)
        ):
            raise MaintenanceError("transaction_unauthorized")
        permit.lease.check()
        if (permit.installation, permit.service) != (
            record.identity.installation,
            record.identity.service,
        ) or self._core != (record.identity.store_instance, record.identity.main_session):
            raise MaintenanceError("foreign_state")
        reference = record.target if role == "target" else record.base
        if (
            configuration_digest(permit.config_path, permit.journal.limits.max_snapshot_bytes)
            != reference.digest
        ):
            raise MaintenanceError("configuration_changed")
        return record

    def _role(self, record: TransactionRecord) -> Role | None:
        permit = self.permit
        assert permit is not None
        if record.identity.origin_boot == permit.boot:
            return None
        if permit.record is None or record.generation != permit.record.generation:
            raise MaintenanceError("foreign_state")
        return permit.role

    def drain(self, generation: str, boot: str, nonce: str) -> dict[str, Any]:
        record = self._record(generation, boot, nonce)
        self.require_support()
        assert self.permit is not None
        if (
            self._role(record) is not None
            or boot != record.identity.origin_boot
            or record.phase != "draining"
        ):
            raise MaintenanceError("foreign_state")
        owners = {name: idle() for name, idle in self._owners.items()}
        owners["effects"] = self.admission.idle
        if self._sealed and self.admission.idle and all(owners.values()):
            # Caller holds configuration_lock. No await between final receipts and gate.
            self.admission.pause()
            return {"state": "drained", "boot": boot, "owners": owners}
        return {"state": "waiting", "boot": boot, "owners": owners, "coverage": self._sealed}

    def _release(self) -> None:
        if self.admission.release():
            try:
                for wake in self._wake:
                    wake()
            except BaseException:
                self.admission.pause()
                raise

    def resume(self, generation: str, boot: str, nonce: str) -> None:
        record = self._record(generation, boot, nonce)
        assert self.permit is not None
        if (
            self._role(record) is not None
            or boot != record.identity.origin_boot
            or record.phase not in {"prepared", "aborted"}
        ):
            raise MaintenanceError("foreign_state")
        self._release()
        self.reconcile()

    def readiness(self, generation: str, boot: str, nonce: str) -> HealthProof:
        record = self._record(generation, boot, nonce)
        self.require_support()
        permit = self.permit
        assert permit is not None
        role = self._role(record)
        if not self._sealed or self._core is None:
            raise MaintenanceError("maintenance_incomplete")
        if role is None:
            if record.phase not in {"prepared", "draining", "aborted"} and (
                record.phase,
                record.intent,
            ) not in {("activating", "stop_base"), ("rolling_back", "stop_target")}:
                raise MaintenanceError("foreign_state")
            if not self.admission.allowed and (
                not self.admission.idle or not all(idle() for idle in self._owners.values())
            ):
                raise MaintenanceError("maintenance_incomplete")
        if record.activation == "blocked":
            self.admission.pause()
        if record.resolved:
            self.reconcile()
        challenge = record.rollback_challenge if role == "base" else record.challenge
        assert challenge is not None
        return HealthProof(
            generation=generation,
            challenge=challenge,
            boot=boot,
            installation=permit.installation,
            service=permit.service,
            store_instance=self._core[0],
            main_session=self._core[1],
            configuration=(record.target if role == "target" else record.base).digest,
            core_ready=True,
            admission="blocked"
            if self._activation_failed or record.activation == "blocked"
            else ("released" if self.admission.allowed else "paused"),
        )

    async def activate(
        self, generation: str, boot: str, nonce: str, clock: Callable[[], ClockReading]
    ) -> HealthProof:
        record = self._record(generation, boot, nonce)
        permit = self.permit
        assert permit is not None
        self.require_support()
        self._decision(record, boot)
        if record.deadline.remaining(clock()) <= 0 or not self._sealed or self._activate is None:
            raise MaintenanceError("activation_not_permitted")
        # A lock-free host never contends with the controller's flock. Confirm
        # the durable decision before any external activation, then re-read CAS.
        os.fsync(permit.journal.directory.fd)
        if permit.journal.read() != record:
            raise MaintenanceError("transaction_changed")
        if self._activation is None:
            self._activation = asyncio.create_task(
                self._run_activation(generation, boot, nonce, clock),
                name="muli-maintenance-activation",
            )
        await asyncio.shield(self._activation)
        return self.readiness(generation, boot, nonce)

    def _decision(self, record: TransactionRecord, boot: str) -> None:
        permit = self.permit
        assert permit is not None
        role = self._role(record)
        phase = "committed" if role == "target" else "rolled_back"
        if (
            role is None
            or record.phase != phase
            or record.health is None
            or record.health.boot != boot
            or record.activation == "blocked"
        ):
            raise MaintenanceError("activation_not_permitted")

    async def _run_activation(
        self, generation: str, boot: str, nonce: str, clock: Callable[[], ClockReading]
    ) -> None:
        assert self._activate is not None
        try:
            record = self._record(generation, boot, nonce)
            self.require_support()
            self._decision(record, boot)
            if record.deadline.remaining(clock()) <= 0:
                raise MaintenanceError("deadline_exceeded")
            await self._activate()
            record = self._record(generation, boot, nonce)
            self._decision(record, boot)
            if record.deadline.remaining(clock()) <= 0:
                raise MaintenanceError("deadline_exceeded")
            self._release()
        except BaseException:
            self._activation_failed = True
            self.admission.pause()
            raise

    async def settle_activation(self) -> None:
        if self._activation is not None:
            while not self._activation.done():
                try:
                    await asyncio.shield(self._activation)
                except asyncio.CancelledError:
                    continue
            self._activation.result()
