"""Prepare local managed handoffs through the host's existing journal and Store.

No environment is created, no candidate is executed and no service effect is
issued. The supported BootSession owns the writer lease and authenticates the
running host. A helper may consume the returned generation after this method.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import TypeAdapter

from muli.compatibility import assess_compatibility
from muli.config import Settings
from muli.format_evidence import FormatEvidence, read_format_evidence
from muli.maintenance import BootPermit, MaintenanceError
from muli.managed_boot import ManagedBootPermit
from muli.managed_journal import RoleSnapshots, prepare_managed
from muli.managed_runtime_binding import _legacy_installation
from muli.managed_transaction import (
    ManagedAction,
    RoleBinding,
    ServiceIdentityV2,
    TransactionRecordV2,
    raw_snapshot,
)
from muli.management_projection import (
    _check_lease,
    _connection_identity,
    _read_object,
    read_projection,
)
from muli.management_transaction import (
    ClockReading,
    Deadline,
    Identifier,
    ProjectionReceipt,
    Role,
    TransactionError,
    TransactionRecord,
    _snapshot,
)
from muli.platforms.filesystem_identity import verify_local_filesystems
from muli.platforms.installation import InstallationManifest
from muli.platforms.linux_transaction import clock_reading
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    SelectorSnapshot,
    SelectorStore,
    SelectorValue,
    SlotIdentity,
    register_local_slot,
    verify_current_process,
    verify_current_slot,
    verify_slot_files,
)
from muli.platforms.managed_probe import ManagedSystemdProbe, ManagedUserService
from muli.platforms.transaction_files import ConfigurationFile
from muli.service import UserService

if TYPE_CHECKING:
    from muli.runtime import Runtime

_IDENTIFIER = TypeAdapter(Identifier)
CurrentRecord = TransactionRecord | TransactionRecordV2
Permit = BootPermit | ManagedBootPermit
_ROLES: tuple[Role, ...] = ("base", "target")


@dataclass(frozen=True, slots=True)
class LocalSlotPaths:
    prefix: Path = field(repr=False)
    python: Path = field(repr=False)
    package_root: Path = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedManaged:
    record: TransactionRecordV2 = field(repr=False)
    anchor: DeploymentAnchor = field(repr=False)


@dataclass(frozen=True, slots=True)
class CurrentManagedInstallation:
    """Observed running source, with no queued plan or authority to install."""

    record: TransactionRecordV2 = field(repr=False)
    anchor: DeploymentAnchor = field(repr=False)
    slot: SlotIdentity = field(repr=False)
    binding_revision: str
    verify: Callable[[], None] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _Source:
    previous: CurrentRecord | None
    projected: ProjectionReceipt | None
    evidence: FormatEvidence
    slot: SlotIdentity
    controller: SlotIdentity
    data: RoleSnapshots
    selector: SelectorSnapshot | None
    plan: dict[str, Any] | None
    anchor: DeploymentAnchor | None = None
    manifest: InstallationManifest | None = None
    role: Role | None = None


class ManagedPreparation:
    def __init__(
        self,
        runtime: Runtime,
        service: UserService,
        *,
        probe: ManagedSystemdProbe | None = None,
        clock: Callable[[], ClockReading] = clock_reading,
    ) -> None:
        self.runtime, self.service = runtime, service
        self.probe = probe or ManagedSystemdProbe()
        self.clock = clock

    def _permit(self) -> Permit:
        runtime = self.runtime
        runtime.maintenance.require_support()
        session, permit = runtime.boot_session, runtime.maintenance.permit
        if session is None or permit is None or session.permit is not permit:
            raise MaintenanceError("transaction_unsupported", "supported_boot_required")
        if session.journal is not permit.journal:
            raise MaintenanceError("foreign_state")
        session.check_paths(runtime.config.path, runtime.data_dir / "muli.sqlite3")
        permit.check_paths(runtime.config.path, permit.journal.database)
        _check_lease(permit)
        return permit

    @staticmethod
    def _paths(permit: Permit) -> None:
        journal = permit.journal
        verify_local_filesystems(
            (
                permit.config_path.parent,
                journal.database.parent,
                journal.directory.path,
                journal.writer_directory.path,
            )
        )

    def _source(
        self,
        permit: Permit,
        item: dict[str, Any] | None,
        action: ManagedAction,
        candidate: LocalSlotPaths | None,
    ) -> _Source:
        journal, store = permit.journal, self.runtime.store
        store.revalidate_format()
        previous, receipt = journal.read_current(), read_projection(store)
        if previous is not None:
            if not previous.resolved:
                raise TransactionError("transaction_in_progress")
            if item is not None and previous.request_id == item["id"]:
                raise MaintenanceError("managed_preparation_recovery_required")
            if receipt != ProjectionReceipt(
                **previous.position.model_dump(),
                store_instance=previous.identity.store_instance,
            ):
                raise TransactionError("projection_required")
        elif receipt is not None:
            raise MaintenanceError("managed_preparation_state_missing")
        plan = None
        if item is not None:
            plan = _read_object(store.db, "management", item["id"], journal.limits.max_record_bytes)
            if (
                plan is None
                or plan.get("id") != item["id"]
                or plan.get("action") != action
                or plan.get("status") not in ("pending", "applying")
                or plan.get("routing") != item.get("routing")
            ):
                raise MaintenanceError("managed_preparation_plan_changed")
        elif (
            not isinstance(permit, ManagedBootPermit)
            or not isinstance(previous, TransactionRecordV2)
            or action != "switch"
            or candidate is not None
        ):
            raise MaintenanceError("managed_boot_required")
        evidence = read_format_evidence(store.db)
        identity = _connection_identity(store)
        if (
            identity.path != journal.database
            or (identity.instance, identity.main)
            != (evidence.store_instance, evidence.main_session)
            or (
                previous is not None
                and (identity.instance, identity.main)
                != (previous.identity.store_instance, previous.identity.main_session)
            )
            or (
                isinstance(previous, TransactionRecordV2)
                and evidence.state != previous.observed_format
            )
        ):
            raise MaintenanceError("managed_preparation_store_changed")
        anchor: DeploymentAnchor | None = None
        manifest: InstallationManifest | None = None
        role: Role | None = None
        if isinstance(previous, TransactionRecordV2):
            role = "target" if previous.phase == "committed" else "base"
            with DeploymentStore(journal.directory.path, journal.limits) as deployments:
                anchor = deployments.read_anchor(previous.identity.deployment)
                deployments.verify_anchor(anchor)
                slot = deployments.read_slot(previous.binding(role).installation)
                controller = deployments.read_slot(anchor.controller_slot)
            if (
                anchor.config_path != str(permit.config_path)
                or anchor.database_path != str(journal.database)
                or anchor.data_dir != str(self.runtime.data_dir.absolute())
                or anchor.journal_root != str(journal.directory.path)
                or anchor.journal != journal.limits
                or slot.compatibility_digest != previous.binding(role).compatibility
            ):
                raise MaintenanceError("managed_preparation_installation_changed")
            if isinstance(permit, ManagedBootPermit):
                if permit.anchor != anchor or permit.installation != slot.digest:
                    raise MaintenanceError("managed_preparation_installation_changed")
            elif previous.binding(role).entry != "legacy":
                raise MaintenanceError("managed_boot_required")
            else:
                _legacy_installation(permit, previous, anchor, slot)
        else:
            session = self.runtime.boot_session
            assert session is not None
            manifest = session.manifest
            if not isinstance(permit, BootPermit) or manifest is None or candidate is None:
                raise MaintenanceError("managed_adoption_required")
            if (manifest.digest, manifest.service_digest) != (permit.installation, permit.service):
                raise MaintenanceError("managed_preparation_installation_changed")
            manifest.verify()
            slot = register_local_slot(
                candidate.prefix,
                candidate.python,
                candidate.package_root,
                limits=manifest.package_limits,
            )
            controller = slot
        if anchor is not None and slot == controller:
            # verify_anchor authenticated these exact files in this synchronous read.
            verify_current_process(slot)
        else:
            verify_current_slot(slot)
        if manifest is not None and (
            manifest.config_path != str(permit.config_path)
            or manifest.data_dir != str(self.runtime.data_dir.absolute())
            or manifest.database_path != str(journal.database)
            or manifest.unit_path != str(self.service.path.absolute())
            or manifest.journal != journal.limits
            or (manifest.interpreter.executable, manifest.interpreter.prefix, manifest.package.root)
            != (slot.lexical_python, slot.prefix, slot.package.root)
        ):
            raise MaintenanceError("managed_preparation_installation_changed")
        unit_path = Path(
            anchor.unit_path
            if anchor is not None
            else cast(InstallationManifest, manifest).unit_path
        )
        verify_local_filesystems((unit_path.parent,))
        limit = journal.limits.max_snapshot_bytes
        with (
            ConfigurationFile(permit.config_path, limit) as configuration,
            ConfigurationFile(unit_path, limit) as unit,
            SelectorStore(journal.directory.path) as selector,
        ):
            data = RoleSnapshots(
                configuration.read().data,
                (selected.data if (selected := selector.read()) is not None else None),
                unit.read().data,
            )
        if Settings.model_validate_json(data.configuration) != self.runtime.config.settings:
            raise MaintenanceError("configuration_changed")
        if isinstance(previous, TransactionRecordV2):
            assert role is not None
            binding = previous.binding(role)
            if (
                raw_snapshot(data.unit, limit) != binding.unit
                or (None if data.selector is None else _snapshot(data.selector, limit))
                != binding.selector
            ):
                raise MaintenanceError("managed_preparation_files_changed")
            legacy = binding.entry == "legacy"
        else:
            legacy = True
            if data.selector is not None:
                raise MaintenanceError("managed_preparation_files_changed")
        if (action == "adopt") != legacy:
            raise MaintenanceError("managed_adoption_required" if legacy else "already_managed")
        source = _Source(
            previous,
            receipt,
            evidence,
            slot,
            controller,
            data,
            selected,
            plan,
            anchor,
            manifest,
            role,
        )
        if legacy and data.unit != self._legacy_service(permit, source).render().encode():
            raise MaintenanceError("managed_preparation_unit_changed")
        return source

    async def observe_current(self) -> CurrentManagedInstallation:
        """Read the resolved managed source before an update has been approved.

        Reuse preparation's source checks, without fabricating or persisting a
        plan. A later install and switch must still perform their own admission.
        """
        runtime = self.runtime
        runtime.maintenance.require_configuration()
        if not runtime.configuration_lock.locked():
            raise MaintenanceError("configuration_lock_required")
        permit = self._permit()
        self._paths(permit)
        source = self._source(permit, None, "switch", None)
        assert isinstance(source.previous, TransactionRecordV2)
        assert source.anchor is not None
        await self._observe_process(permit, source)

        def verify() -> None:
            runtime.maintenance.require_configuration()
            if self._permit() is not permit:
                raise MaintenanceError("managed_source_changed")
            self._paths(permit)
            if self._source(permit, None, "switch", None) != source:
                raise MaintenanceError("managed_source_changed")

        verify()
        revision = hashlib.sha256(
            json.dumps(
                {
                    "record": source.previous.model_dump(mode="json"),
                    "slot": source.slot.digest,
                    "controller": source.controller.digest,
                    "anchor": source.anchor.digest,
                    "configuration": hashlib.sha256(source.data.configuration).hexdigest(),
                    "unit": hashlib.sha256(source.data.unit).hexdigest(),
                    "selector": hashlib.sha256(source.data.selector or b"").hexdigest(),
                    "schema": [asdict(fact) for fact in source.evidence.schema],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return CurrentManagedInstallation(
            source.previous, source.anchor, source.slot, revision, verify
        )

    def _legacy_service(self, permit: Permit, source: _Source) -> UserService:
        unit_path = (
            source.anchor.unit_path
            if source.anchor
            else cast(InstallationManifest, source.manifest).unit_path
        )
        return UserService(
            permit.config_path,
            self.runtime.data_dir,
            Path(unit_path).parent,
            platform="linux",
            executable=source.slot.lexical_python,
        )

    async def _observe_process(self, permit: Permit, source: _Source) -> None:
        if isinstance(source.previous, TransactionRecordV2):
            assert source.anchor is not None and source.role is not None
            with DeploymentStore(
                permit.journal.directory.path, permit.journal.limits
            ) as deployments:
                slots = {
                    role: deployments.read_slot(source.previous.binding(role).installation)
                    for role in _ROLES
                }
            result = await self.probe.read_managed(
                permit.journal, source.previous, source.anchor, source.controller, slots
            )
            observed = result.service
            if source.role not in result.process_roles or not result.unit_confirmed:
                raise MaintenanceError("foreign_service_boot")
        else:
            observed = await self.probe.read(self._legacy_service(permit, source))
        if (
            observed.process is None
            or observed.process.pid != os.getpid()
            or observed.unit.invocation != permit.boot
        ):
            raise MaintenanceError("foreign_service_boot")

    async def prepare(
        self,
        item: dict[str, Any],
        target: Settings,
        *,
        candidate: LocalSlotPaths | None = None,
        listener_guard: Callable[[Settings], None] | None = None,
        source_guard: Callable[[], None] | None = None,
    ) -> PreparedManaged:
        runtime = self.runtime
        runtime.maintenance.require_configuration()
        if not runtime.configuration_lock.locked():
            raise MaintenanceError("configuration_lock_required")
        if listener_guard is not None:
            listener_guard(target)
        action = item.get("action")
        if action not in ("adopt", "switch", "configure", "restart"):
            raise MaintenanceError("transaction_action_invalid")
        action = cast(ManagedAction, action)
        _IDENTIFIER.validate_python(item.get("id"), strict=True)
        if (candidate is not None) != (action in ("adopt", "switch")):
            raise MaintenanceError("managed_candidate_paths_required")
        permit = self._permit()
        self._paths(permit)
        source = self._source(permit, item, action, candidate)
        if action == "adopt":
            assert candidate is not None
            selected = register_local_slot(
                candidate.prefix,
                candidate.python,
                candidate.package_root,
                limits=(
                    source.anchor.package_limits
                    if source.anchor
                    else cast(InstallationManifest, source.manifest).package_limits
                ),
            )
            verify_current_slot(selected)
            if selected != source.slot or target != runtime.config.settings:
                raise MaintenanceError("managed_adoption_changed")
        elif action == "switch":
            assert source.anchor is not None and candidate is not None
            if target != runtime.config.settings:
                raise MaintenanceError("managed_switch_changes_configuration")
            registering = asyncio.create_task(
                asyncio.to_thread(
                    register_local_slot,
                    candidate.prefix,
                    candidate.python,
                    candidate.package_root,
                    limits=source.anchor.package_limits,
                )
            )
            try:
                selected = await asyncio.shield(registering)
            except asyncio.CancelledError:
                # Retain the preparation owner and its configuration lock until
                # this file-only job settles, including repeated cancellation.
                while not registering.done():
                    try:
                        await asyncio.shield(registering)
                    except asyncio.CancelledError:
                        continue
                    except Exception:  # noqa: BLE001 - collect worker outcome before propagating cancellation
                        break
                if registering.done() and not registering.cancelled():
                    registering.exception()
                raise
        else:
            selected = source.slot
            if action == "restart" and target != runtime.config.settings:
                raise MaintenanceError("restart_changes_configuration")
        from muli.real_format_probe import FormatProbeError, probe_format

        try:
            probe_format(permit.journal.database).verify(
                path=permit.journal.database,
                store_instance=source.evidence.store_instance,
                main_session=source.evidence.main_session,
                state=source.evidence.state,
            )
        except FormatProbeError as exc:
            raise MaintenanceError("managed_preparation_probe_failed", exc.code) from None
        assessment = assess_compatibility(
            source.slot.compatibility,
            selected.compatibility,
            source.evidence.state,
            handoff_protocol=2,
        )
        if not assessment.format_compatible:
            raise MaintenanceError(assessment.code)
        if action == "switch" and selected.digest == source.slot.digest:
            raise MaintenanceError("managed_switch_requires_different_slot")
        await self._observe_process(permit, source)
        try:
            probe_format(permit.journal.database).verify(
                path=permit.journal.database,
                store_instance=source.evidence.store_instance,
                main_session=source.evidence.main_session,
                state=source.evidence.state,
            )
        except FormatProbeError as exc:
            raise MaintenanceError("managed_preparation_probe_failed", exc.code) from None
        # No await from this revalidation through prepared publication and return.
        runtime.validate_proactive_settings(target)
        helper = (
            source.anchor.helper
            if source.anchor
            else cast(InstallationManifest, source.manifest).helper
        )
        deadline = Deadline(
            created=self.clock(), seconds=helper.seconds, rollback_reserve=helper.rollback_reserve
        )
        generation = secrets.token_hex(16)
        assert source.plan is not None
        queued = {**source.plan, "status": "applying", "transaction": {"generation": generation}}
        journal = permit.journal
        from muli.candidate_lineage import check_plan_budget

        check_plan_budget(queued, journal.limits.max_record_bytes)
        expected = source.previous
        expected_plan = source.plan
        with journal.locked() as control:

            def gate() -> None:
                control.check()
                self._permit()
                if control.read_current() != expected:
                    raise TransactionError("stale_revision")
                if deadline.remaining(self.clock()) <= deadline.rollback_reserve:
                    raise TransactionError("deadline_exceeded")

            def validate() -> None:
                gate()
                # Once prepared has been published, the live-source observer
                # correctly rejects the previous resolved record. The journal,
                # selected files and existing validate() now own that handoff.
                if expected == source.previous and source_guard is not None:
                    source_guard()
                if listener_guard is not None:
                    listener_guard(target)
                self._paths(permit)
                verify_current_slot(source.slot)
                verify_slot_files(selected)
                if source.manifest is not None:
                    source.manifest.verify()
                elif isinstance(permit, BootPermit) and isinstance(
                    source.previous, TransactionRecordV2
                ):
                    assert source.anchor is not None
                    _legacy_installation(permit, source.previous, source.anchor, source.slot)
                if source.anchor is not None:
                    with DeploymentStore(journal.directory.path, journal.limits) as deployments:
                        deployments.verify_anchor(source.anchor)
                runtime.store.revalidate_format()
                identity = _connection_identity(runtime.store)
                evidence = read_format_evidence(runtime.store.db)
                if (
                    identity.path != journal.database
                    or (identity.instance, identity.main)
                    != (source.evidence.store_instance, source.evidence.main_session)
                    or evidence != source.evidence
                    or read_projection(runtime.store) != source.projected
                ):
                    raise MaintenanceError("managed_preparation_store_changed")
                if (
                    _read_object(
                        runtime.store.db, "management", item["id"], journal.limits.max_record_bytes
                    )
                    != expected_plan
                ):
                    raise MaintenanceError("managed_preparation_plan_changed")
                if (
                    Settings.model_validate_json(source.data.configuration)
                    != runtime.config.settings
                ):
                    raise MaintenanceError("configuration_changed")
                runtime.validate_proactive_settings(target)
                unit_path = self._legacy_service(permit, source).path
                with (
                    ConfigurationFile(
                        permit.config_path, journal.limits.max_snapshot_bytes
                    ) as configuration,
                    ConfigurationFile(unit_path, journal.limits.max_snapshot_bytes) as unit,
                    SelectorStore(journal.directory.path) as selector,
                ):
                    configuration.confirm(
                        _snapshot(
                            source.data.configuration, journal.limits.max_snapshot_bytes
                        ).digest,
                        guard=gate,
                    )
                    unit.confirm(
                        raw_snapshot(source.data.unit, journal.limits.max_snapshot_bytes).digest,
                        guard=gate,
                    )
                    selector.confirm(source.selector, guard=gate)
                gate()

            validate()
            runtime.store.event("management.preparing", queued, objects=[("management", queued)])
            expected_plan = queued
            with DeploymentStore(journal.directory.path, journal.limits) as deployments:
                if source.anchor is None:
                    deployments.publish_slot(source.slot, guard=gate)
                    launcher = deployments.publish_launcher(guard=gate)
                    managed_service = ManagedUserService(
                        permit.config_path,
                        runtime.data_dir,
                        self.service.path.parent,
                        controller=source.controller,
                        launcher=Path(launcher.path),
                        journal_root=journal.directory.path,
                    )
                    managed_unit = managed_service.render().encode()
                    assert source.manifest is not None
                    anchor = DeploymentAnchor(
                        config_path=str(permit.config_path),
                        data_dir=str(runtime.data_dir.absolute()),
                        database_path=str(journal.database),
                        unit_path=str(self.service.path.absolute()),
                        journal_root=str(journal.directory.path),
                        controller_slot=source.controller.digest,
                        launcher=launcher,
                        managed_unit=raw_snapshot(managed_unit, journal.limits.max_snapshot_bytes),
                        helper=helper,
                        journal=journal.limits,
                        package_limits=source.manifest.package_limits,
                    )
                    deployments.publish_anchor(anchor, guard=gate)
                else:
                    anchor = source.anchor
                    managed_unit = (
                        ManagedUserService(
                            permit.config_path,
                            runtime.data_dir,
                            Path(anchor.unit_path).parent,
                            controller=source.controller,
                            launcher=Path(anchor.launcher.path),
                            journal_root=journal.directory.path,
                        )
                        .render()
                        .encode()
                    )
                    if (
                        raw_snapshot(managed_unit, journal.limits.max_snapshot_bytes)
                        != anchor.managed_unit
                    ):
                        raise MaintenanceError("managed_preparation_unit_changed")
                deployments.publish_slot(selected, guard=gate)
            limit = journal.limits.max_snapshot_bytes
            base = RoleBinding(
                entry="legacy" if action == "adopt" else "managed",
                installation=source.slot.digest,
                compatibility=source.slot.compatibility_digest,
                configuration=_snapshot(source.data.configuration, limit),
                selector=None
                if source.data.selector is None
                else _snapshot(source.data.selector, limit),
                unit=raw_snapshot(source.data.unit, limit),
            )
            target_data = RoleSnapshots(
                source.data.configuration
                if action != "configure"
                else target.model_dump_json(indent=2).encode(),
                SelectorValue(deployment=anchor.digest, installation=selected.digest).canonical(),
                managed_unit,
            )
            binding = RoleBinding(
                entry="managed",
                installation=selected.digest,
                compatibility=selected.compatibility_digest,
                configuration=_snapshot(target_data.configuration, limit),
                selector=_snapshot(cast(bytes, target_data.selector), limit),
                unit=anchor.managed_unit,
            )
            record = prepare_managed(
                control,
                request_id=item["id"],
                action=action,
                identity=ServiceIdentityV2(
                    deployment=anchor.digest,
                    store_instance=source.evidence.store_instance,
                    main_session=source.evidence.main_session,
                    origin_boot=permit.boot,
                ),
                base=base,
                target=binding,
                base_data=source.data,
                target_data=target_data,
                observed_format=source.evidence.state,
                base_manifest=source.slot.compatibility,
                target_manifest=selected.compatibility,
                deadline=deadline,
                writer=permit.lease,
                projected=source.projected,
                generation=generation,
                guard=validate,
            )
            expected = record
            validate()
            return PreparedManaged(record, anchor)
