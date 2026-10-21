"""Host-side preparation and projection; the helper alone drives service effects."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from muli.config import Settings
from muli.maintenance import BootPermit, MaintenanceError
from muli.managed_boot import ManagedBootPermit
from muli.managed_transaction import TransactionRecordV2
from muli.management_projection import project_final, read_projection
from muli.management_transaction import Deadline, ServiceIdentity, TransactionError
from muli.platforms.filesystem_identity import verify_local_filesystems
from muli.platforms.linux_controller import transaction_url
from muli.platforms.linux_transaction import ObservationError, SystemdProbe, clock_reading
from muli.platforms.transaction_files import ConfigurationFile
from muli.service import UserService

if TYPE_CHECKING:
    from muli.legacy_boot_adapter import PendingLegacy
    from muli.platforms.installation import InstallationManifest
    from muli.runtime import Runtime


class RecoveryHost:
    def __init__(
        self,
        runtime: Runtime,
        service: UserService,
        *,
        bound_address: tuple[str, int] | None = None,
    ) -> None:
        self.runtime, self.service = runtime, service
        self._bound_address = bound_address
        self.probe = SystemdProbe()
        self.projection_error: str | None = None
        self._pending_legacy: tuple[ManagedBootPermit, PendingLegacy] | None = None

    def require_listener(self, target: Settings) -> None:
        settings = self.runtime.config.settings
        if self._bound_address is not None and self._bound_address != (
            settings.server.host,
            settings.server.port,
        ):
            raise MaintenanceError("transaction_listener_changed")
        for configuration in (settings, target):
            try:
                transaction_url(configuration.model_dump_json().encode(), "readiness")
            except ObservationError as exc:
                raise MaintenanceError("transaction_unsupported", str(exc)) from None

    def _publish_legacy_view(self, permit: BootPermit, manifest: InstallationManifest) -> None:
        session = self.runtime.boot_session
        assert session is not None
        self.runtime.maintenance.permit = permit
        session.permit, session.manifest = permit, manifest
        self._pending_legacy = None

    def _confirm_pending_legacy(self) -> None:
        if self._pending_legacy is None:
            return
        from muli.legacy_boot_adapter import confirm_legacy_permit

        owner, pending = self._pending_legacy
        if self.runtime.maintenance.permit is not owner:
            raise MaintenanceError("legacy_owner_changed")
        # A new lock supplies the existing directory durability barrier. Never
        # clear or reuse a poisoned controller after an uncertain publication.
        with owner.journal.locked() as control:
            current = control.read_current()
            if current == pending.previous:
                self._pending_legacy = None
                return
            view = confirm_legacy_permit(
                control, owner=owner, store=self.runtime.store, pending=pending
            )
            self._publish_legacy_view(view, pending.manifest)

    @staticmethod
    def _reason(exc: Exception, fallback: str) -> str:
        code = getattr(exc, "code", None)
        return (
            code
            if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
            else fallback
        )

    def _manifest(self) -> InstallationManifest:
        session = self.runtime.boot_session
        if session is None or session.permit is None:
            raise MaintenanceError(
                "transaction_unsupported",
                (session.unsupported_reason or "managed_boot_required")
                if session is not None
                else "managed_boot_required",
            )
        self.runtime.maintenance.require_support()
        session.check_paths(self.runtime.config.path, self.runtime.data_dir / "muli.sqlite3")
        if session.manifest is not None:
            return session.manifest
        current = session.journal.read_current()
        if (
            isinstance(session.permit, ManagedBootPermit)
            and isinstance(current, TransactionRecordV2)
            and current.resolved
            and current.phase in {"aborted", "rolled_back"}
            and current.base.entry == "legacy"
        ):
            from muli.platforms.installation import InstallationManifest

            anchor = session.permit.anchor
            return InstallationManifest.capture(
                self.service,
                session.journal.database,
                helper=anchor.helper,
                journal=anchor.journal,
                package_limits=anchor.package_limits,
            )
        raise MaintenanceError("transaction_unsupported", "managed_boot_required")

    async def verify(self) -> InstallationManifest:
        manifest = self._manifest()
        permit = self.runtime.maintenance.permit
        assert permit is not None
        manifest.verify()
        verify_local_filesystems(
            [
                self.runtime.config.path.absolute().parent,
                permit.journal.database.parent,
                permit.journal.directory.path,
                permit.journal.writer_directory.path,
            ]
        )
        observed = await self.probe.read(self.service)
        if (
            observed.process is None
            or observed.process.pid != os.getpid()
            or observed.unit.invocation != permit.boot
        ):
            raise MaintenanceError("transaction_unsupported", "foreign_service_boot")
        return manifest

    def require_managed_request(self, action: str) -> None:
        """Queue eligibility only; preparation authenticates actual local evidence."""
        runtime = self.runtime
        runtime.maintenance.require_support()
        session, permit = runtime.boot_session, runtime.maintenance.permit
        if session is None or permit is None or session.permit is not permit:
            raise MaintenanceError("transaction_unsupported", "managed_boot_required")
        session.check_paths(runtime.config.path, runtime.data_dir / "muli.sqlite3")
        current = session.journal.read_current()
        if action == "switch" and not isinstance(current, TransactionRecordV2):
            raise MaintenanceError("transaction_unsupported", "managed_adoption_required")
        if not isinstance(permit, ManagedBootPermit) and session.manifest is None:
            raise MaintenanceError("transaction_unsupported", "managed_boot_required")

    def current(self) -> dict[str, Any]:
        session = self.runtime.boot_session
        permit = self.runtime.maintenance.permit
        record = session.journal.read_current() if session is not None else None
        unsupported = self.runtime.maintenance.unsupported_reason
        return {
            "available": bool(
                session
                and permit
                and (session.manifest or isinstance(permit, ManagedBootPermit))
                and not self.projection_error
                and not unsupported
            ),
            "reason": self.projection_error
            or unsupported
            or (session.unsupported_reason if session else "managed_boot_required"),
            "transaction": record.public() if record is not None else None,
        }

    async def project(self, *, strict: bool = False) -> None:
        try:
            self._confirm_pending_legacy()
        except (OSError, ValueError) as exc:
            self.projection_error = self._reason(exc, "legacy_rebind_unconfirmed")
            if strict:
                raise
            return
        permit = self.runtime.maintenance.permit
        if permit is None:
            return
        before = self.runtime.store.event_position()
        try:
            receipt = project_final(
                permit, self.runtime.store, verify_binding=self.runtime.maintenance.verify_managed
            )
        except (OSError, ValueError) as exc:
            self.projection_error = self._reason(exc, "projection_failed")
            if strict:
                raise
            return
        self.projection_error = None
        if self.runtime.store.event_position() != before:
            await self.runtime.bus.notify()
        if receipt is not None:
            record = permit.journal.read_current()
            if record is not None and (record.generation, record.revision) == (
                receipt.generation,
                receipt.revision,
            ):
                # Cleanup is deliberately independent from the committed projection.
                try:
                    (self.runtime.data_dir / "management" / f"{record.request_id}.json").unlink(
                        missing_ok=True
                    )
                except OSError:
                    pass  # Retain the snapshot and retry cleanup on the next projection read.
            self.runtime.maintenance.reconcile()

    async def prepare(self, item: dict[str, Any], target: Settings) -> dict[str, Any]:
        try:
            self.require_listener(target)
            return await self._prepare(item, target)
        except BaseException:
            # The previous with-block has released its possibly poisoned lock.
            # Keep the original error, but recover the logical view if a fresh
            # durable observation confirms this exact bridge was published.
            try:
                self._confirm_pending_legacy()
            except (OSError, ValueError):
                pass  # Retain the one attempt for the next explicit recovery.
            raise

    async def _prepare(self, item: dict[str, Any], target: Settings) -> dict[str, Any]:
        runtime = self.runtime
        runtime.maintenance.require_configuration()
        if not runtime.configuration_lock.locked():
            raise MaintenanceError("configuration_lock_required")
        current = runtime.maintenance.current()
        if item.get("action") in {"adopt", "switch"} or (
            isinstance(current, TransactionRecordV2)
            and current.binding("target" if current.phase == "committed" else "base").entry
            == "managed"
        ):
            return await self._prepare_managed(item, target)
        manifest = await self.verify()
        permit = runtime.maintenance.permit
        assert permit is not None
        journal = permit.journal
        await self.project(strict=True)
        # Evidence can be edited while the plan waits or identity probes await.
        # Revalidate immediately before the first durable preparation write.
        runtime.validate_proactive_settings(target)
        with journal.locked() as control:
            previous = control.read_current()
            receipt = read_projection(runtime.store)
            with ConfigurationFile(
                runtime.config.path, journal.limits.max_snapshot_bytes
            ) as source:
                base = source.read().data
            if Settings.model_validate_json(base) != runtime.config.settings:
                raise MaintenanceError("configuration_changed")
            self.require_listener(target)
            action = item.get("action")
            if action not in {"configure", "restart"}:
                raise MaintenanceError("transaction_action_invalid")
            data = base if action == "restart" else target.model_dump_json(indent=2).encode()
            generation = secrets.token_hex(16)
            queued = {**item, "status": "applying", "transaction": {"generation": generation}}
            # This WAL FULL commit precedes every journal reference to the plan.
            runtime.store.event("management.preparing", queued, objects=[("management", queued)])
            deadline = Deadline(
                created=clock_reading(),
                seconds=manifest.helper.seconds,
                rollback_reserve=manifest.helper.rollback_reserve,
            )
            if isinstance(previous, TransactionRecordV2):
                if isinstance(permit, ManagedBootPermit):
                    from muli.legacy_boot_adapter import PendingLegacy, prepare_legacy_permit

                    pending = PendingLegacy(
                        manifest=manifest,
                        previous=previous,
                        request_id=item["id"],
                        action=action,
                        base=base,
                        target=data,
                        deadline=deadline,
                        generation=generation,
                    )
                    self._pending_legacy = (permit, pending)
                    rebound = prepare_legacy_permit(
                        control, owner=permit, store=runtime.store, pending=pending
                    )
                    record = rebound.record
                    assert record is not None
                    # Publish the V1 view synchronously; its managed owner still
                    # retains all Store/descriptor/lease cleanup responsibility.
                    self._publish_legacy_view(rebound, manifest)
                else:
                    from muli.legacy_transaction_bridge import prepare_legacy_after_managed

                    record = prepare_legacy_after_managed(
                        control,
                        store=runtime.store,
                        writer=permit.lease,
                        manifest=manifest,
                        origin_boot=permit.boot,
                        request_id=item["id"],
                        action=action,
                        target=data,
                        expected_base=base,
                        deadline=deadline,
                        generation=generation,
                    )
                    permit.installation, permit.service = manifest.digest, manifest.service_digest
                    permit.record = record
            else:
                if not isinstance(permit, BootPermit):
                    raise MaintenanceError("managed_preparation_required")
                manifest.publish(control, projected=receipt)
                permit.lease.check()
                if control.read_current() != previous:
                    raise TransactionError("stale_revision")
                permit.installation, permit.service = manifest.digest, manifest.service_digest
                record = control.prepare(
                    request_id=item["id"],
                    action=action,
                    identity=ServiceIdentity(
                        installation=manifest.digest,
                        service=manifest.service_digest,
                        store_instance=runtime.store.instance_id,
                        main_session=runtime.main_session()["id"],
                        origin_boot=permit.boot,
                    ),
                    base=base,
                    target=data,
                    writer=permit.lease,
                    projected=receipt,
                    generation=generation,
                    deadline=deadline,
                )
            queued["transaction"] = record.public()
            runtime.store.event("management.prepared", queued, objects=[("management", queued)])
            runtime.maintenance.reconcile()
            from muli.management_helper import ensure_helper

            await ensure_helper(control, manifest)
        await runtime.bus.notify()
        return queued

    async def _prepare_managed(self, item: dict[str, Any], target: Settings) -> dict[str, Any]:
        from muli.managed_host import ensure_managed_helper
        from muli.managed_preparation import LocalSlotPaths, ManagedPreparation
        from muli.managed_requests import LocalCandidate, validate_candidate

        specification = (
            LocalCandidate.model_validate(item["candidate"]) if "candidate" in item else None
        )
        validate_candidate(item["action"], specification)
        candidate = (
            LocalSlotPaths(
                prefix=Path(specification.prefix),
                python=Path(specification.python),
                package_root=Path(specification.package_root),
            )
            if specification is not None
            else None
        )
        await self.project(strict=True)
        prepared = await ManagedPreparation(self.runtime, self.service).prepare(
            item, target, candidate=candidate, listener_guard=self.require_listener
        )
        permit = self.runtime.maintenance.permit
        assert permit is not None
        queued = self.runtime.store.get("management", item["id"])
        if (
            queued is None
            or queued.get("transaction", {}).get("generation") != prepared.record.generation
        ):
            raise MaintenanceError("managed_preparation_plan_changed")
        queued["transaction"] = prepared.record.public()
        self.runtime.store.event("management.prepared", queued, objects=[("management", queued)])
        self.runtime.maintenance.reconcile()
        with permit.journal.locked() as control:
            if control.read_current() != prepared.record:
                raise TransactionError("stale_revision")
            await ensure_managed_helper(control, prepared.anchor)
        await self.runtime.bus.notify()
        return queued

    async def recover(self) -> dict[str, Any]:
        session = self.runtime.boot_session
        if session is None or session.permit is None:
            return self.current()
        await self.project()
        record = session.journal.read_current()
        if record is None or record.resolved or record.phase == "recovery_required":
            return self.current()
        try:
            state: str
            with session.journal.locked() as control:
                if isinstance(record, TransactionRecordV2):
                    from muli.managed_host import ensure_managed_helper
                    from muli.platforms.managed_deployment import DeploymentStore

                    with DeploymentStore(
                        session.journal.directory.path, session.journal.limits
                    ) as deployments:
                        anchor = deployments.read_anchor(record.identity.deployment)
                    state = await ensure_managed_helper(control, anchor)
                else:
                    from muli.management_helper import ensure_helper

                    state = await ensure_helper(control, self._manifest())
        except (OSError, ValueError, TimeoutError) as exc:
            if isinstance(exc, TransactionError) and exc.code == "lock_busy":
                state = "controller_busy"
            else:
                return {
                    **self.current(),
                    "helper": "unavailable",
                    "reason": self._reason(exc, "helper_unavailable"),
                }
        return {**self.current(), "helper": state}
