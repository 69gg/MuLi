"""Authenticate this managed service before handing its DB lease to admission.

The launcher plan only routes. The actual loaded unit, exec-replaced process,
InvocationID and current role must agree before configuration or SQLite opens.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from muli.managed_boot import _MUTABLE_RECORD_FIELDS, ManagedBootError, ManagedBootPermit
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role, TransactionJournal
from muli.platforms.linux_bootstrap import BootSession, BootstrapError, _current_process
from muli.platforms.linux_transaction import ObservationError, ProbeLimits
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    SlotIdentity,
    resolve_entry,
)
from muli.platforms.managed_probe import ManagedProbeResult, ManagedSystemdProbe
from muli.service import UserService

_ROLES: tuple[Role, ...] = ("base", "target")


class ManagedServiceProbe(Protocol):
    async def read_managed(
        self,
        journal: TransactionJournal,
        record: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult: ...


async def admit_managed_boot(
    session: BootSession,
    service: UserService,
    record: TransactionRecordV2,
    *,
    probe: ManagedServiceProbe | None = None,
) -> BootSession:
    """Use an already-held writer lease; never fall back to an unmanaged boot."""
    journal = session.journal
    paths = (service.config_path, service.data_dir, service.path, service.executable)
    try:
        session.lease.check()
        with DeploymentStore(journal.directory.path, journal.limits) as source:
            anchor = source.read_anchor(record.identity.deployment)
            if (
                anchor.config_path != str(service.config_path.absolute())
                or anchor.data_dir != str(service.data_dir.absolute())
                or anchor.database_path != str(journal.database)
                or anchor.unit_path != str(service.path.absolute())
            ):
                raise BootstrapError("boot_paths_changed")
            journal.limits = anchor.journal
            fresh = journal.read_current()
            if (
                not isinstance(fresh, TransactionRecordV2)
                or fresh.generation != record.generation
                or fresh.identity.deployment != anchor.digest
            ):
                raise BootstrapError("managed_boot_record_changed")
            record = fresh
            controller = source.read_slot(anchor.controller_slot)
            slots: dict[Role, SlotIdentity] = {
                role: source.read_slot(record.binding(role).installation) for role in _ROLES
            }
        plan = resolve_entry(journal.directory.path, purpose="service")
        if plan.generation != record.generation or plan.role not in {"base", "target"}:
            raise BootstrapError("managed_boot_record_changed")
        observer = probe or ManagedSystemdProbe(
            limits=ProbeLimits(seconds=anchor.helper.operation_timeout / 4)
        )
        try:
            async with asyncio.timeout(anchor.helper.operation_timeout / 4):
                observed = await observer.read_managed(journal, record, anchor, controller, slots)
        except TimeoutError:
            raise BootstrapError("managed_boot_observation_timeout") from None
        except ObservationError as error:
            raise BootstrapError("managed_boot_unsupported", str(error)) from None
        if paths != (service.config_path, service.data_dir, service.path, service.executable):
            raise BootstrapError("boot_paths_changed")
        if (
            not _current_process(observed.service)
            or not observed.unit_confirmed
            or plan.role not in observed.process_roles
        ):
            raise BootstrapError("managed_boot_process_mismatch")
        try:
            permit = ManagedBootPermit.acquire(
                journal, boot=observed.service.unit.invocation, plan=plan, lease=session.lease
            )
        except ManagedBootError as error:
            if error._owner is not None:
                # Failed admission can retain a connection. Outer cleanup must
                # ask that owner, rather than closing the raw lease underneath.
                session.permit = error._owner
            raise
        session.permit = permit
        if permit.record.model_dump(exclude=_MUTABLE_RECORD_FIELDS) != record.model_dump(
            exclude=_MUTABLE_RECORD_FIELDS
        ):
            raise BootstrapError("managed_boot_record_changed")
        return session
    except (BootstrapError, ManagedBootError):
        raise
    except (OSError, ValueError):
        raise BootstrapError("managed_boot_verification_failed") from None
