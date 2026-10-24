"""Observe the stable unit and its exec-replaced process as separate identities.

Only frozen legacy/managed layouts from this transaction are accepted. A stopped
unit may need reload during file reconciliation; that observation is never a
loaded-unit confirmation or a healthy running role.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from muli.managed_journal import RoleSnapshots, _read_snapshots, read_snapshots
from muli.managed_transaction import ProjectControllerTransferRecord, TransactionRecordV2
from muli.management_transaction import Role, TransactionJournal
from muli.platforms.linux_transaction import (
    ObservationError,
    ServiceSnapshot,
    SystemdProbe,
    _file_identity,
    _read_regular,
)
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    SelectorValue,
    SlotIdentity,
)
from muli.service import UserService

_ROLES: tuple[Role, ...] = ("base", "target")


@dataclass(frozen=True, slots=True)
class ManagedProbeResult:
    service: ServiceSnapshot
    process_roles: tuple[Role, ...]
    loaded_units: frozenset[str]
    disk_unit: str
    origin_process: bool = False

    @property
    def unit_confirmed(self) -> bool:
        return not self.service.unit.needs_reload and self.disk_unit in self.loaded_units


class ManagedUserService(UserService):
    """Reuse the standard resource limits and unit quoting with a fixed launcher."""

    def __init__(
        self,
        config_path: Path,
        data_dir: Path,
        unit_dir: Path,
        *,
        controller: SlotIdentity,
        launcher: Path,
        journal_root: Path,
    ) -> None:
        super().__init__(
            config_path, data_dir, unit_dir, platform="linux", executable=controller.lexical_python
        )
        self.launcher, self.journal_root = launcher, journal_root

    def _arguments(self) -> list[str]:
        return [
            self.executable,
            "-I",
            str(self.launcher),
            "--root",
            str(self.journal_root),
            "service",
        ]


def managed_process_arguments(anchor: DeploymentAnchor, slot: SlotIdentity) -> tuple[str, ...]:
    return (
        slot.lexical_python,
        "-I",
        "-m",
        "muli",
        "--config",
        anchor.config_path,
        "--data-dir",
        anchor.data_dir,
        "--managed-root",
        anchor.journal_root,
        "serve",
    )


def managed_unit_arguments(anchor: DeploymentAnchor, controller: SlotIdentity) -> tuple[str, ...]:
    return (
        controller.lexical_python,
        "-I",
        anchor.launcher.path,
        "--root",
        anchor.journal_root,
        "service",
    )


def transfer_origin_material(
    journal: TransactionJournal,
    record: ProjectControllerTransferRecord,
    anchor: DeploymentAnchor,
) -> tuple[DeploymentAnchor, SlotIdentity, RoleSnapshots]:
    """Authenticate both frozen authorities; origin is not a replacement role."""
    with DeploymentStore(journal.directory.path, journal.limits) as store:
        origin = store.read_anchor(record.origin.deployment)
        controller = store.read_slot(origin.controller_slot)
        bridge = store.read_slot(anchor.controller_slot)
        if store.read_anchor(record.identity.deployment) != anchor:
            raise ObservationError("managed_probe_identity_changed")
        store.verify_anchor(anchor)
        store.verify_anchor(origin)
    if (
        anchor.model_dump(exclude={"controller_slot", "managed_unit"})
        != origin.model_dump(exclude={"controller_slot", "managed_unit"})
        or anchor.controller_slot != record.base.installation
        or origin.controller_slot == anchor.controller_slot
        or record.origin.binding.unit != origin.managed_unit
        or record.base.unit != anchor.managed_unit
        or bridge.compatibility_digest != record.base.compatibility
    ):
        raise ObservationError("managed_transfer_origin_changed")
    data = _read_snapshots(journal, record.generation, record.origin.binding, "origin")
    origin_service = ManagedUserService(
        Path(origin.config_path),
        Path(origin.data_dir),
        Path(origin.unit_path).parent,
        controller=controller,
        launcher=Path(origin.launcher.path),
        journal_root=journal.directory.path,
    )
    target_service = ManagedUserService(
        Path(anchor.config_path),
        Path(anchor.data_dir),
        Path(anchor.unit_path).parent,
        controller=bridge,
        launcher=Path(anchor.launcher.path),
        journal_root=journal.directory.path,
    )
    if (
        data.unit != origin_service.render().encode()
        or data.selector
        != SelectorValue(deployment=origin.digest, installation=bridge.digest).canonical()
    ):
        raise ObservationError("managed_transfer_origin_changed")
    for role in _ROLES:
        selected = read_snapshots(journal, record, role)
        if (
            selected.configuration != data.configuration
            or selected.unit != target_service.render().encode()
            or selected.selector
            != SelectorValue(deployment=anchor.digest, installation=bridge.digest).canonical()
        ):
            raise ObservationError("managed_transfer_origin_changed")
    return origin, controller, data


class ManagedSystemdProbe(SystemdProbe):
    async def read_managed(
        self,
        journal: TransactionJournal,
        record: TransactionRecordV2 | ProjectControllerTransferRecord,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        if sys.platform != "linux":
            raise ObservationError("platform_unsupported")
        if (
            record.identity.deployment != anchor.digest
            or anchor.controller_slot != controller.digest
            or str(journal.directory.path) != anchor.journal_root
            or str(journal.database) != anchor.database_path
            or set(slots) != {"base", "target"}
        ):
            raise ObservationError("managed_probe_identity_changed")
        service = ManagedUserService(
            Path(anchor.config_path),
            Path(anchor.data_dir),
            Path(anchor.unit_path).parent,
            controller=controller,
            launcher=Path(anchor.launcher.path),
            journal_root=Path(anchor.journal_root),
        )
        unit_argv: dict[Role, tuple[str, ...]] = {}
        process_argv: dict[Role, tuple[str, ...]] = {}
        unit_data: dict[Role, bytes] = {}
        origin_argv: tuple[str, ...] | None = None
        origin_data: RoleSnapshots | None = None
        if isinstance(record, ProjectControllerTransferRecord):
            origin, origin_controller, origin_data = transfer_origin_material(
                journal, record, anchor
            )
            origin_argv = managed_unit_arguments(origin, origin_controller)
        for role in _ROLES:
            binding, slot = record.binding(role), slots[role]
            if (
                binding.installation != slot.digest
                or binding.compatibility != slot.compatibility_digest
            ):
                raise ObservationError("managed_probe_identity_changed")
            unit_data[role] = read_snapshots(journal, record, role).unit
            if binding.entry == "managed":
                if (
                    binding.unit != anchor.managed_unit
                    or unit_data[role] != service.render().encode()
                ):
                    raise ObservationError("managed_probe_identity_changed")
                unit_argv[role] = managed_unit_arguments(anchor, controller)
                process_argv[role] = managed_process_arguments(anchor, slot)
            else:
                legacy = UserService(
                    Path(anchor.config_path),
                    Path(anchor.data_dir),
                    Path(anchor.unit_path).parent,
                    platform="linux",
                    executable=slot.lexical_python,
                )
                if unit_data[role] != legacy.render().encode():
                    raise ObservationError("unsupported_unit_file")
                unit_argv[role] = process_argv[role] = tuple(legacy._arguments())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.limits.seconds
        try:
            async with asyncio.timeout(self.limits.seconds):
                path = await self._unit_path(service.unit)
                before = await self._snapshot(path)
                loaded = frozenset(
                    record.binding(role).unit.digest
                    for role in _ROLES
                    if before.argv == unit_argv[role] and before.executable == unit_argv[role][0]
                )
                if (
                    origin_argv is not None
                    and before.argv == origin_argv
                    and before.executable == origin_argv[0]
                ):
                    assert isinstance(record, ProjectControllerTransferRecord)
                    loaded = loaded | {record.origin.binding.unit.digest}
                if (
                    before.name != service.unit
                    or before.load_state != "loaded"
                    or before.fragment != str(service.path.absolute())
                    or before.source
                    or before.drop_ins
                    or before.service_type != "simple"
                    or not loaded
                    or before.ignore_failure
                    or before.kill_mode != "control-group"
                    or not before.send_sigkill
                ):
                    raise ObservationError("unsupported_service_layout")
                identity = _file_identity(service.path.stat(follow_symlinks=False))
                data = _read_regular(service.path, self.limits.file_bytes)
                disk = next(
                    (
                        record.binding(role).unit.digest
                        for role in _ROLES
                        if data == unit_data[role]
                    ),
                    None,
                )
                if origin_data is not None and data == origin_data.unit:
                    assert isinstance(record, ProjectControllerTransferRecord)
                    disk = record.origin.binding.unit.digest
                if disk is None:
                    raise ObservationError("unsupported_unit_file")
                reconciling = (
                    before.active_state in {"inactive", "failed"}
                    and before.pid == 0
                    and before.job == (0, "/")
                    and (record.phase, record.intent)
                    in {
                        ("activating", "install_target"),
                        ("rolling_back", "stop_target"),
                        ("rolling_back", "restore_base"),
                    }
                )
                if (before.needs_reload or disk not in loaded) and not reconciling:
                    raise ObservationError("managed_unit_not_loaded")
                process = self._process(before.pid) if before.pid else None
                matches: tuple[Role, ...] = ()
                origin_process = False
                if process is not None:
                    matches = tuple(
                        role
                        for role in _ROLES
                        if (
                            process.argv == process_argv[role]
                            and process.executable == slots[role].resolved_python.path
                            and record.binding(role).unit.digest == disk
                        )
                    )
                    if isinstance(record, ProjectControllerTransferRecord):
                        origin_process = (
                            process.argv == managed_process_arguments(anchor, slots["base"])
                            and process.executable == slots["base"].resolved_python.path
                            and record.origin.binding.unit.digest == disk
                            and before.invocation == record.identity.origin_boot
                        )
                    if (
                        (not matches and not origin_process)
                        or process.uid != os.geteuid()
                        or process.cgroup != before.cgroup
                        or before.invocation == "0" * 32
                    ):
                        raise ObservationError("foreign_process")
                after = await self._snapshot(path)
                if (
                    before != after
                    or data != _read_regular(service.path, self.limits.file_bytes)
                    or identity != _file_identity(service.path.stat(follow_symlinks=False))
                ):
                    raise ObservationError("service_observation_changed")
                if process is not None and process != self._process(process.pid):
                    raise ObservationError("process_identity_changed")
                if loop.time() >= deadline:
                    raise TimeoutError
                return ManagedProbeResult(
                    ServiceSnapshot(after, process), matches, loaded, disk, origin_process
                )
        except TimeoutError:
            raise
        except (OSError, UnicodeError):
            raise ObservationError("systemd_observation_unavailable") from None
