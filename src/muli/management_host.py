"""Host-side preparation and projection; the helper alone drives service effects."""

from __future__ import annotations

import os
import re
import secrets
from typing import TYPE_CHECKING, Any

from muli.config import Settings
from muli.maintenance import BootPermit, MaintenanceError
from muli.managed_boot import ManagedBootPermit
from muli.managed_transaction import TransactionRecordV2
from muli.management_projection import project_final, read_projection
from muli.management_transaction import Deadline, ServiceIdentity, TransactionError
from muli.platforms.filesystem_identity import verify_local_filesystems
from muli.platforms.linux_transaction import SystemdProbe, clock_reading
from muli.platforms.transaction_files import ConfigurationFile
from muli.service import UserService

if TYPE_CHECKING:
    from muli.platforms.installation import InstallationManifest
    from muli.runtime import Runtime


class RecoveryHost:
    def __init__(self, runtime: Runtime, service: UserService) -> None:
        self.runtime, self.service = runtime, service
        self.probe = SystemdProbe()
        self.projection_error: str | None = None

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
        if session is None or session.permit is None or session.manifest is None:
            raise MaintenanceError(
                "transaction_unsupported",
                (session.unsupported_reason or "managed_boot_required")
                if session is not None
                else "managed_boot_required",
            )
        self.runtime.maintenance.require_support()
        session.check_paths(self.runtime.config.path, self.runtime.data_dir / "muli.sqlite3")
        return session.manifest

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
        runtime = self.runtime
        runtime.maintenance.require_configuration()
        if not runtime.configuration_lock.locked():
            raise MaintenanceError("configuration_lock_required")
        manifest = await self.verify()
        permit = runtime.maintenance.permit
        if not isinstance(permit, BootPermit):
            raise MaintenanceError("transaction_unsupported", "managed_preparation_required")
        journal = permit.journal
        await self.project(strict=True)
        # Evidence can be edited while the plan waits or identity probes await.
        # Revalidate immediately before the first durable preparation write.
        runtime.validate_proactive_settings(target)
        with journal.locked() as control:
            previous = control.read()
            receipt = read_projection(runtime.store)
            with ConfigurationFile(
                runtime.config.path, journal.limits.max_snapshot_bytes
            ) as source:
                base = source.read().data
            if Settings.model_validate_json(base) != runtime.config.settings:
                raise MaintenanceError("configuration_changed")
            action = item.get("action")
            if action not in {"configure", "restart"}:
                raise MaintenanceError("transaction_action_invalid")
            data = base if action == "restart" else target.model_dump_json(indent=2).encode()
            generation = secrets.token_hex(16)
            queued = {**item, "status": "applying", "transaction": {"generation": generation}}
            # This WAL FULL commit precedes every journal reference to the plan.
            runtime.store.event("management.preparing", queued, objects=[("management", queued)])
            manifest.publish(control, projected=receipt)
            permit.lease.check()
            if control.read() != previous:
                raise TransactionError("stale_revision")
            # publish checked that any previous generation was resolved and projected.
            # Rebind only after its exact installation manifest is durable.
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
                deadline=Deadline(
                    created=clock_reading(),
                    seconds=manifest.helper.seconds,
                    rollback_reserve=manifest.helper.rollback_reserve,
                ),
            )
            queued["transaction"] = record.public()
            runtime.store.event("management.prepared", queued, objects=[("management", queued)])
            runtime.maintenance.reconcile()
            from muli.management_helper import ensure_helper

            await ensure_helper(control, manifest)
        await runtime.bus.notify()
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
