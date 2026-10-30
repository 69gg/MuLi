"""Linux protocol 2 effects over the existing leases and frozen role prefixes.

The systemd probe authenticates the stable unit and the exec-replaced process
separately. This backend additionally verifies current slot files, exact disk
bytes and durability before effects; command acceptance is never readiness.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

import httpx

from muli.managed_controller import (
    BindingConfirmation,
    ManagedProcessIdentity,
    ManagedServiceObservation,
    _files,
    _prefixes,
)
from muli.managed_journal import Component, RoleSnapshots, read_snapshots
from muli.managed_transaction import HealthProofV2, TransactionRecordV2, format_digest
from muli.management_transaction import (
    ClockReading,
    FixedLock,
    JournalControl,
    Role,
    TransactionError,
    TransactionJournal,
)
from muli.platforms.installation import FileFingerprint
from muli.platforms.linux_controller import (
    CgroupObservation,
    transaction_request,
    transaction_url,
)
from muli.platforms.linux_transaction import (
    CommandRunner,
    ObservationError,
    ProbeLimits,
    ServiceSnapshot,
    clock_reading,
)
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    SelectorSnapshot,
    SelectorStore,
    SelectorValue,
    SlotIdentity,
    _AuthenticationMismatch,
    verify_slot_files,
)
from muli.platforms.managed_launcher import PackageProofs
from muli.platforms.managed_probe import (
    ManagedProbeResult,
    ManagedSystemdProbe,
    ManagedUserService,
)
from muli.platforms.transaction_files import ConfigurationFile, ConfigurationSnapshot
from muli.process import run_process

_ROLES: tuple[Role, ...] = ("base", "target")


@dataclass(frozen=True, slots=True)
class _DiskBinding:
    configuration: ConfigurationSnapshot
    selector: SelectorSnapshot | None
    unit: ConfigurationSnapshot

    @property
    def digests(self) -> tuple[str, str | None, str]:
        return (
            self.configuration.digest,
            None if self.selector is None else self.selector.digest,
            self.unit.digest,
        )


@dataclass(slots=True)
class _FileProofs:
    python: FileFingerprint | None = None
    packages: PackageProofs | None = None


class ManagedLinuxBackend(AbstractAsyncContextManager["ManagedLinuxBackend"]):
    def __init__(
        self,
        anchor: DeploymentAnchor,
        controller_slot: SlotIdentity,
        slots: dict[Role, SlotIdentity],
        journal: TransactionJournal,
        *,
        control: JournalControl,
        clock: Callable[[], ClockReading] = clock_reading,
        probe: ManagedSystemdProbe | None = None,
        runner: CommandRunner = run_process,
        transport: httpx.AsyncBaseTransport | None = None,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        operation_timeout: float = 20.0,
    ) -> None:
        if (
            control.journal is not journal
            or set(slots) != {"base", "target"}
            or anchor.controller_slot != controller_slot.digest
            or str(journal.directory.path) != anchor.journal_root
            or str(journal.database) != anchor.database_path
            or journal.limits != anchor.journal
        ):
            raise ObservationError("transaction_backend_unsupported")
        if not math.isfinite(operation_timeout) or not 0 < operation_timeout <= 30:
            raise ObservationError("invalid_operation_timeout")
        self.anchor, self.controller_slot, self.slots = anchor, controller_slot, dict(slots)
        self.journal, self.control, self.clock = journal, control, clock
        self.operation_timeout, self.runner = operation_timeout, runner
        self.probe = (
            probe
            if probe is not None
            else ManagedSystemdProbe(
                runner=runner, limits=ProbeLimits(seconds=operation_timeout / 4)
            )
        )
        if self.probe.limits.seconds > operation_timeout / 4:
            raise ObservationError("invalid_probe_timeout")
        self.service = ManagedUserService(
            Path(anchor.config_path),
            Path(anchor.data_dir),
            Path(anchor.unit_path).parent,
            controller=controller_slot,
            launcher=Path(anchor.launcher.path),
            journal_root=Path(anchor.journal_root),
        )
        self.cgroups = CgroupObservation(cgroup_root, self.probe.limits.file_bytes)
        self._activation_attempt: tuple[str, str, Role | None] | None = None
        self.client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=self.probe.limits.seconds,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={"Accept-Encoding": "identity"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.client.aclose()

    def _check(
        self,
        record: TransactionRecordV2,
        role: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        self.control.check()
        if self.control.read_current() != record:
            raise TransactionError("stale_revision")
        if record.identity.deployment != self.anchor.digest:
            raise ObservationError("managed_deployment_changed")
        slots = {self.anchor.controller_slot: self.controller_slot}
        # The opposite role still requires actual metadata, not an executable
        # package: a damaged target cannot prevent restoration of the base.
        for selected in _ROLES:
            binding, slot = record.binding(selected), self.slots[selected]
            if (
                binding.installation != slot.digest
                or binding.compatibility != slot.compatibility_digest
                or (binding.installation in slots and slots[binding.installation] != slot)
            ):
                raise ObservationError("managed_slot_changed")
            slots[binding.installation] = slot

        def verify_selected(slot: SlotIdentity) -> None:
            # Keep the original selected-role verifier and kwargs boundary. It
            # receives the actual record parsed inside this synchronous guard.
            if verified_packages is None and observed_packages is None:
                verify_slot_files(slot, verified_python=self.controller_slot.resolved_python)
            else:
                verify_slot_files(
                    slot,
                    verified_python=self.controller_slot.resolved_python,
                    verified_packages=verified_packages,
                    observed_packages=observed_packages,
                )

        with DeploymentStore(Path(self.anchor.journal_root), self.journal.limits) as store:
            try:
                store._authenticate(
                    self.anchor,
                    slots,
                    selected=None if role is None else record.binding(role).installation,
                    verify_selected=verify_selected,
                    verified_python=verified_python,
                    verified_packages=verified_packages,
                    observed_packages=observed_packages,
                )
            except _AuthenticationMismatch as error:
                raise ObservationError(error.code) from None

    def _effect_guard(
        self,
        record: TransactionRecordV2,
        role: Role | None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        if verified_packages is None and observed_packages is None:
            self._check(record, role, verified_python=verified_python)
        else:
            self._check(
                record,
                role,
                verified_python=verified_python,
                verified_packages=verified_packages,
                observed_packages=observed_packages,
            )
        remaining = record.deadline.remaining(self.clock())
        if record.phase in {"activating", "verifying"}:
            remaining -= record.deadline.rollback_reserve
        if remaining <= 0:
            raise TimeoutError

    def _guard(
        self,
        record: TransactionRecordV2,
        role: Role,
        lease: FixedLock,
        cgroup: str,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        if verified_packages is None and observed_packages is None:
            self._effect_guard(record, role, verified_python=verified_python)
        else:
            self._effect_guard(
                record,
                role,
                verified_python=verified_python,
                verified_packages=verified_packages,
                observed_packages=observed_packages,
            )
        lease.check()
        if not self.cgroups.empty(cgroup):
            raise ObservationError("cgroup_not_empty")

    @staticmethod
    def _stopped(snapshot: ServiceSnapshot) -> bool:
        return (
            snapshot.unit.active_state in {"inactive", "failed"}
            and snapshot.unit.pid == 0
            and snapshot.process is None
            and snapshot.unit.job == (0, "/")
        )

    @staticmethod
    def _role(record: TransactionRecordV2, boot: str) -> Role | None:
        if boot == record.identity.origin_boot:
            return None
        if (record.phase, record.intent) == ("rolling_back", "stop_target"):
            return "target"
        return "base" if record.phase in {"rolling_back", "rolled_back"} else "target"

    @staticmethod
    def _stop_only(record: TransactionRecordV2) -> bool:
        return (record.phase, record.intent) in {
            ("activating", "stop_base"),
            ("rolling_back", "stop_target"),
        }

    def _desired(self, record: TransactionRecordV2, role: Role) -> RoleSnapshots:
        data = read_snapshots(self.journal, record, role)
        if data.selector is not None:
            value = SelectorValue.model_validate_json(data.selector)
            if (
                value.canonical() != data.selector
                or value.deployment != record.identity.deployment
                or value.installation != record.binding(role).installation
            ):
                raise ObservationError("unsupported_managed_selector")
        return data

    async def _probe(
        self,
        record: TransactionRecordV2,
        *,
        preflight: Callable[[], None] | None = None,
        postprocess: Callable[[ManagedProbeResult], Role | None] | None = None,
        postproofs: _FileProofs | None = None,
    ) -> ManagedProbeResult:
        if postproofs is not None:
            postproofs.python = None
            postproofs.packages = None
        if preflight is None:
            self._check(record)
        else:
            # An adjacent stronger synchronous guard includes authentication.
            # Its result never replaces the independent post-await check.
            preflight()
        result = await self.probe.read_managed(
            self.journal, record, self.anchor, self.controller_slot, self.slots
        )
        observed_packages: PackageProofs | None = {} if postproofs is not None else None
        if postprocess is None:
            if observed_packages is None:
                self._check(record)
            else:
                self._check(record, observed_packages=observed_packages)
        else:
            try:
                selected = postprocess(result)
            except BaseException:
                # Preserve controller authentication's precedence over a local
                # observation error, without retaining proofs across the await.
                self._check(record)
                raise
            if observed_packages is None:
                self._check(record, selected)
            else:
                self._check(record, selected, observed_packages=observed_packages)
        if postproofs is not None:
            # Publish only after the independent post-await authentication and
            # its final metadata/root fences have all returned successfully.
            postproofs.python = self.controller_slot.resolved_python
            postproofs.packages = observed_packages
        return result

    def _disk(self) -> _DiskBinding:
        with (
            ConfigurationFile(
                Path(self.anchor.config_path), self.journal.limits.max_snapshot_bytes
            ) as configuration,
            SelectorStore(Path(self.anchor.journal_root)) as selector,
            ConfigurationFile(
                Path(self.anchor.unit_path), self.journal.limits.max_snapshot_bytes
            ) as unit,
        ):
            return _DiskBinding(configuration.read(), selector.read(), unit.read())

    def _owned(
        self, record: TransactionRecordV2, result: ManagedProbeResult, disk: _DiskBinding
    ) -> Role | None:
        snapshot = result.service
        role = self._role(record, snapshot.unit.invocation)
        selected: Role = "base" if role is None else role
        if (
            snapshot.process is None
            or selected not in result.process_roles
            or disk.digests != _files(record.binding(selected))
            or not result.unit_confirmed
        ):
            raise ObservationError("foreign_transaction_process")
        return role

    async def _request(
        self, record: TransactionRecordV2, boot: str, action: str
    ) -> dict[str, Any] | None:
        role = self._role(record, boot)
        challenge = record.rollback_challenge if role == "base" else record.challenge
        if challenge is None:
            raise ObservationError("transaction_challenge_missing")
        data = self._desired(record, "target" if role == "target" else "base")
        return await transaction_request(
            self.client,
            url=transaction_url(data.configuration, action),
            generation=record.generation,
            boot=boot,
            challenge=challenge,
            action=action,
            seconds=self.probe.limits.seconds,
            max_bytes=self.journal.limits.max_record_bytes,
        )

    def _proof(
        self, record: TransactionRecordV2, boot: str, value: dict[str, Any]
    ) -> HealthProofV2:
        try:
            proof = HealthProofV2.model_validate(value)
        except ValueError:
            raise ObservationError("invalid_transaction_proof") from None
        role = self._role(record, boot)
        binding = record.binding("base" if role is None else role)
        challenge = record.rollback_challenge if role == "base" else record.challenge
        if (
            proof.generation != record.generation
            or proof.boot != boot
            or proof.role != ("origin" if role is None else role)
            or proof.challenge != challenge
            or proof.deployment != record.identity.deployment
            or proof.installation != binding.installation
            or proof.compatibility != binding.compatibility
            or (proof.configuration, proof.selector, proof.unit) != _files(binding)
            or proof.store_instance != record.identity.store_instance
            or proof.main_session != record.identity.main_session
            or proof.format_digest != format_digest(record.observed_format)
            or not proof.core_ready
        ):
            raise ObservationError("foreign_transaction_proof")
        return proof

    def _confirm(
        self,
        record: TransactionRecordV2,
        role: Role,
        lease: FixedLock,
        cgroup: str,
        *,
        proofs: _FileProofs | None = None,
    ) -> _DiskBinding:
        expected = _files(record.binding(role))
        batch = _FileProofs() if proofs is None else proofs

        def guard() -> None:
            observed_packages: PackageProofs = {}
            self._guard(
                record,
                role,
                lease,
                cgroup,
                verified_python=batch.python,
                verified_packages=batch.packages,
                observed_packages=observed_packages,
            )
            if self._disk().digests != expected:
                raise ObservationError("foreign_managed_binding")
            # Only an adjacent synchronous file caller may share its batch.
            # Every full guard and exact disk check must succeed before promotion.
            batch.python = self.controller_slot.resolved_python
            batch.packages = observed_packages

        order: tuple[Component, ...] = (
            ("configuration", "selector", "unit")
            if role == "target"
            else ("unit", "selector", "configuration")
        )
        for component in order:
            # Each confirm enters this same guard before its first durable effect.
            if component == "selector":
                with SelectorStore(Path(self.anchor.journal_root)) as selector:
                    selector.confirm(selector.read(), guard=guard)
            else:
                path = (
                    self.anchor.config_path
                    if component == "configuration"
                    else self.anchor.unit_path
                )
                digest = expected[0] if component == "configuration" else expected[2]
                with ConfigurationFile(Path(path), self.journal.limits.max_snapshot_bytes) as file:
                    file.confirm(digest, guard=guard)
        guard()
        return self._disk()

    async def observe(self, record: TransactionRecordV2) -> ManagedServiceObservation:
        disk: _DiskBinding | None = None
        boot: str | None = None
        process: ManagedProcessIdentity | None = None
        proof: HealthProofV2 | None = None

        def bind(result: ManagedProbeResult) -> Role | None:
            nonlocal disk, boot, process
            disk = self._disk()
            if result.service.process is None:
                return None
            boot = result.service.unit.invocation
            role = self._owned(record, result, disk)
            selected: Role = "base" if role is None else role
            process = ManagedProcessIdentity(
                record.binding(selected).entry,
                record.binding(selected).installation,
                record.binding(selected).compatibility,
            )
            return None if self._stop_only(record) else selected

        before = await self._probe(record, postprocess=bind)
        assert disk is not None
        if boot is not None and not self._stop_only(record):
            value = await self._request(record, boot, "readiness")
            if value is not None:
                proof = self._proof(record, boot, value)
        writer_free, empty = False, False
        available = self.journal.writer_lease()
        lease: FixedLock | None = None
        try:
            available.__enter__()
        except TransactionError as exc:
            if exc.code != "lock_busy":
                raise
        else:
            writer_free = True
            if self._stopped(before.service):
                lease = available
            else:
                available.__exit__()
        confirmation: BindingConfirmation | None = None

        def match(result: ManagedProbeResult) -> Role | None:
            if before != result or disk != self._disk():
                raise ObservationError("transaction_observation_changed")
            if boot is not None and not self._stop_only(record):
                current_role = self._role(record, boot)
                return "base" if current_role is None else current_role
            return None

        postproofs = _FileProofs() if lease is not None else None
        try:
            if postproofs is None:
                after = await self._probe(record, postprocess=match)
            else:
                after = await self._probe(record, postprocess=match, postproofs=postproofs)
            if self._stopped(after.service) and lease is not None:
                lease.check()
                empty = self.cgroups.empty(after.service.unit.cgroup)
                desired: Role = (
                    "base" if record.phase in {"rolling_back", "rolled_back"} else "target"
                )
                if empty and disk.digests == _files(record.binding(desired)):
                    self._desired(record, desired)
                    disk = self._confirm(
                        record, desired, lease, after.service.unit.cgroup, proofs=postproofs
                    )

                    def final_preflight() -> None:
                        assert postproofs is not None
                        try:
                            self._check(
                                record,
                                verified_python=postproofs.python,
                                verified_packages=postproofs.packages,
                            )
                        finally:
                            # Only the adjacent confirmation lends content;
                            # the real probe await starts without its proofs.
                            postproofs.python = None
                            postproofs.packages = None

                    confirmed = await self._probe(record, preflight=final_preflight)
                    if confirmed != after or disk != self._disk():
                        raise ObservationError("transaction_observation_changed")
                    if confirmed.unit_confirmed:
                        confirmation = BindingConfirmation(
                            record.position, desired, *disk.digests, loaded_unit=disk.unit.digest
                        )
        finally:
            if postproofs is not None:
                postproofs.python = None
                postproofs.packages = None
            if lease is not None:
                lease.__exit__()
        state: Literal["running", "stopped", "starting", "stopping", "foreign"]
        if self._stopped(after.service):
            state = "stopped" if empty else "stopping"
        elif after.service.unit.active_state == "activating":
            state = "starting"
        elif after.service.unit.active_state == "deactivating":
            state = "stopping"
        elif after.service.unit.active_state == "active" and after.service.process is not None:
            early = (record.phase, record.intent) in {
                ("activating", "start_target"),
                ("rolling_back", "start_base"),
            } and (proof is None or (writer_free and proof.admission != "blocked"))
            state = "starting" if early else "running"
        else:
            state = "foreign"
        origin = boot == record.identity.origin_boot
        return ManagedServiceObservation(
            identity_matches=True,
            configuration=disk.configuration.digest,
            selector=None if disk.selector is None else disk.selector.digest,
            unit=disk.unit.digest,
            service=state,
            boot=boot,
            writer_free=writer_free,
            process=process,
            confirmation=confirmation,
            drained=origin
            and record.phase == "draining"
            and bool(proof and proof.admission == "paused"),
            resumed=origin
            and record.phase in {"prepared", "aborted"}
            and bool(proof and proof.admission in {"released", "released_degraded"}),
            health=proof,
            health_failed=bool(proof and proof.admission == "blocked"),
            activation_failed=bool(
                proof
                and proof.admission == "blocked"
                and record.phase in {"committed", "rolled_back"}
            ),
        )

    async def _command(self, action: Literal["start", "stop", "daemon-reload"]) -> None:
        argv = ["systemctl", "--user"]
        argv += (
            [action]
            if action == "daemon-reload"
            else ["--no-block", "--job-mode=fail", action, self.service.unit]
        )
        async with asyncio.timeout(self.probe.limits.seconds):
            result = await self.runner(
                argv,
                timeout=self.probe.limits.seconds,
                limit=self.probe.limits.command_bytes,
                merge_stderr=False,
            )
        if result.get("exit_code") != 0:
            raise ObservationError("transaction_unit_command_failed")

    async def _host_command(
        self, record: TransactionRecordV2, action: str, role: Role | None
    ) -> ManagedServiceObservation | None:
        if (
            action == "drain"
            and (record.phase != "draining" or role is not None)
            or action == "resume"
            and (record.phase not in {"prepared", "aborted"} or role is not None)
            or action == "activate"
            and (
                (record.phase, role) not in {("committed", "target"), ("rolled_back", "base")}
                or record.activation == "blocked"
            )
        ):
            raise ObservationError("transaction_effect_not_permitted")
        postproofs = _FileProofs()
        before, disk = await self._probe(record, postproofs=postproofs), self._disk()
        boot = before.service.unit.invocation
        if (
            before.service.unit.active_state != "active"
            or self._owned(record, before, disk) != role
            or action == "activate"
            and (record.health is None or record.health.boot != boot)
        ):
            raise ObservationError("foreign_transaction_process")
        self._effect_guard(
            record,
            "base" if role is None else role,
            verified_python=postproofs.python,
            verified_packages=postproofs.packages,
        )
        del postproofs
        if action == "activate":
            attempt = record.generation, boot, role
            if self._activation_attempt == attempt:
                return None
            self._activation_attempt = attempt
        value = await self._request(record, boot, action)
        # Its post-probe role guard includes the full controller authentication;
        # do not repeat that scan in the same synchronous segment.
        after = await self._probe(
            record, postprocess=lambda _result: "base" if role is None else role
        )
        if before != after or disk != self._disk():
            raise ObservationError("transaction_observation_changed")
        if value is None:
            return None
        if action == "activate":
            proof = self._proof(record, boot, value)
            if proof.admission == "blocked":
                raise ObservationError("transaction_activation_failed")
            if proof.admission not in {"released", "released_degraded"}:
                return None
            available = self.journal.writer_lease()
            try:
                available.__enter__()
            except TransactionError as error:
                if error.code != "lock_busy":
                    raise
            else:
                available.__exit__()
                return None
            selected: Role = "base" if role is None else role
            binding = record.binding(selected)
            return ManagedServiceObservation(
                identity_matches=True,
                configuration=disk.configuration.digest,
                selector=None if disk.selector is None else disk.selector.digest,
                unit=disk.unit.digest,
                service="running",
                boot=boot,
                writer_free=False,
                process=ManagedProcessIdentity(
                    binding.entry, binding.installation, binding.compatibility
                ),
                health=proof,
            )
        elif (
            action == "drain"
            and value.get("state") not in {"waiting", "drained"}
            or action == "resume"
            and value.get("released") is not True
            or action == "drain"
            and value.get("boot") != boot
        ):
            raise ObservationError("invalid_transaction_reply")
        return None

    async def drain(self, record: TransactionRecordV2) -> None:
        await self._host_command(record, "drain", None)

    async def resume(self, record: TransactionRecordV2) -> None:
        await self._host_command(record, "resume", None)

    async def activate(
        self, record: TransactionRecordV2, role: Role
    ) -> ManagedServiceObservation | None:
        return await self._host_command(record, "activate", role)

    async def stop(self, record: TransactionRecordV2, role: Role) -> None:
        if (record.phase, record.intent, role) not in {
            ("activating", "stop_base", "base"),
            ("rolling_back", "stop_target", "base"),
            ("rolling_back", "stop_target", "target"),
        }:
            raise ObservationError("transaction_effect_not_permitted")
        postproofs = _FileProofs()
        before, disk = await self._probe(record, postproofs=postproofs), self._disk()
        if self._stopped(before.service):
            return
        expected = (
            None
            if before.service.unit.invocation == record.identity.origin_boot and role == "base"
            else role
        )
        if self._owned(record, before, disk) != expected:
            raise ObservationError("foreign_transaction_process")
        if (
            before.service.unit.job != (0, "/")
            or before.service.unit.active_state == "deactivating"
        ):
            return

        def preflight() -> None:
            self._effect_guard(
                record,
                None,
                verified_python=postproofs.python,
                verified_packages=postproofs.packages,
            )
            # These bytes belong only to the preceding post-await guard.
            # Clear them before _probe enters its next real observation await.
            postproofs.python = None
            postproofs.packages = None

        # Stop-only may terminate a precisely observed broken slot. It neither
        # grants health nor requires that slot's damaged package to run again.
        final_postproofs = _FileProofs()
        if (
            before != await self._probe(record, preflight=preflight, postproofs=final_postproofs)
            or disk != self._disk()
        ):
            raise ObservationError("transaction_observation_changed")
        self._effect_guard(
            record,
            None,
            verified_python=final_postproofs.python,
            verified_packages=final_postproofs.packages,
        )
        del final_postproofs
        await self._command("stop")

    def _install_files(
        self,
        record: TransactionRecordV2,
        role: Role,
        data: RoleSnapshots,
        lease: FixedLock,
        cgroup: str,
        *,
        proofs: _FileProofs | None = None,
    ) -> None:
        # The caller may lend its immediately preceding post-probe bytes only
        # to this synchronous scope, then clears them before the next await.
        batch = _FileProofs() if proofs is None else proofs

        def guard() -> None:
            observed_packages: PackageProofs = {}
            self._guard(
                record,
                role,
                lease,
                cgroup,
                verified_python=batch.python,
                verified_packages=batch.packages,
                observed_packages=observed_packages,
            )
            if self._disk().digests not in _prefixes(record):
                raise ObservationError("foreign_managed_binding")
            batch.python = self.controller_slot.resolved_python
            batch.packages = observed_packages

        order: tuple[Component, ...] = (
            ("configuration", "selector", "unit")
            if role == "target"
            else ("unit", "selector", "configuration")
        )
        for component in order:
            # The selected replace/confirm guards again before writing or syncing.
            current = self._disk()
            if component == "selector":
                value = (
                    None
                    if data.selector is None
                    else SelectorValue.model_validate_json(data.selector)
                )
                with SelectorStore(Path(self.anchor.journal_root)) as selector:
                    selector.replace(current.selector, value, guard=guard)
            else:
                path = (
                    self.anchor.config_path
                    if component == "configuration"
                    else self.anchor.unit_path
                )
                desired = data.configuration if component == "configuration" else data.unit
                snapshot = current.configuration if component == "configuration" else current.unit
                with ConfigurationFile(Path(path), self.journal.limits.max_snapshot_bytes) as file:
                    if snapshot.data == desired:
                        file.confirm(snapshot.digest, guard=guard)
                    else:
                        file.replace(snapshot.digest, desired, guard=guard)
        self._confirm(record, role, lease, cgroup, proofs=batch)

    async def install(
        self, record: TransactionRecordV2, role: Role, data: RoleSnapshots
    ) -> BindingConfirmation:
        if (record.phase, record.intent, role) not in {
            ("activating", "install_target", "target"),
            ("rolling_back", "restore_base", "base"),
        }:
            raise ObservationError("transaction_effect_not_permitted")
        if data != self._desired(record, role):
            raise ObservationError("transaction_snapshot_changed")
        initial_postproofs = _FileProofs()
        try:
            before = await self._probe(
                record, preflight=lambda: self._check(record, role), postproofs=initial_postproofs
            )
            if not self._stopped(before.service):
                raise ObservationError("transaction_service_not_stopped")
            with self.journal.writer_lease() as lease:
                cgroup = before.service.unit.cgroup

                def guard() -> None:
                    try:
                        self._guard(
                            record,
                            role,
                            lease,
                            cgroup,
                            verified_python=initial_postproofs.python,
                            verified_packages=initial_postproofs.packages,
                        )
                        if self._disk().digests not in _prefixes(record):
                            raise ObservationError("foreign_managed_binding")
                    finally:
                        initial_postproofs.python = None
                        initial_postproofs.packages = None

                postproofs = _FileProofs()

                def final_preflight() -> None:
                    try:
                        self._check(
                            record,
                            verified_python=postproofs.python,
                            verified_packages=postproofs.packages,
                        )
                    finally:
                        # The final synchronous file confirmation lends only
                        # content, never authentication across the probe await.
                        postproofs.python = None
                        postproofs.packages = None

                try:
                    if before != await self._probe(record, preflight=guard, postproofs=postproofs):
                        raise ObservationError("transaction_observation_changed")
                    self._install_files(record, role, data, lease, cgroup, proofs=postproofs)
                    final_postproofs = _FileProofs()
                    try:
                        fresh = await self._probe(
                            record, preflight=final_preflight, postproofs=final_postproofs
                        )
                        if not self._stopped(fresh.service) or fresh.service.unit.cgroup != cgroup:
                            raise ObservationError("transaction_observation_changed")
                        if not fresh.unit_confirmed:
                            guard()
                            final_postproofs.python = None
                            final_postproofs.packages = None
                            await self._command("daemon-reload")
                            fresh = await self._probe(
                                record, preflight=guard, postproofs=final_postproofs
                            )
                        if (
                            not self._stopped(fresh.service)
                            or not fresh.unit_confirmed
                            or fresh.disk_unit != record.binding(role).unit.digest
                        ):
                            raise ObservationError("managed_unit_not_loaded")
                        disk = self._confirm(record, role, lease, cgroup, proofs=final_postproofs)
                        return BindingConfirmation(
                            record.position, role, *disk.digests, loaded_unit=fresh.disk_unit
                        )
                    finally:
                        final_postproofs.python = None
                        final_postproofs.packages = None
                finally:
                    postproofs.python = None
                    postproofs.packages = None
        finally:
            initial_postproofs.python = None
            initial_postproofs.packages = None

    async def start(self, record: TransactionRecordV2, role: Role) -> None:
        if (record.phase, record.intent, role) not in {
            ("activating", "start_target", "target"),
            ("rolling_back", "start_base", "base"),
        }:
            raise ObservationError("transaction_effect_not_permitted")
        self._desired(record, role)
        postproofs = _FileProofs()
        before = await self._probe(record, postproofs=postproofs)
        if not self._stopped(before.service):
            raise ObservationError("transaction_service_not_stopped")
        with self.journal.writer_lease() as lease:
            cgroup = before.service.unit.cgroup
            observed_packages: PackageProofs = {}
            self._guard(
                record,
                role,
                lease,
                cgroup,
                verified_python=postproofs.python,
                verified_packages=postproofs.packages,
                observed_packages=observed_packages,
            )
            del postproofs
            # Only the successful writer-held guard lends bytes to this
            # synchronous confirmation; discard them before the next await.
            confirmation_proofs = _FileProofs(
                python=self.controller_slot.resolved_python,
                packages=observed_packages,
            )
            del observed_packages

            def final_preflight() -> None:
                try:
                    self._check(
                        record,
                        verified_python=confirmation_proofs.python,
                        verified_packages=confirmation_proofs.packages,
                    )
                finally:
                    # The last confirmation lends only content to the original
                    # None preflight, never across the real manager await.
                    confirmation_proofs.python = None
                    confirmation_proofs.packages = None

            try:
                disk = self._confirm(record, role, lease, cgroup, proofs=confirmation_proofs)
                final_postproofs = _FileProofs()
                try:
                    fresh = await self._probe(
                        record, preflight=final_preflight, postproofs=final_postproofs
                    )
                    if before != fresh or disk != self._disk() or not fresh.unit_confirmed:
                        raise ObservationError("transaction_observation_changed")
                    effect_packages: PackageProofs = {}
                    self._guard(
                        record,
                        role,
                        lease,
                        cgroup,
                        verified_python=final_postproofs.python,
                        verified_packages=final_postproofs.packages,
                        observed_packages=effect_packages,
                    )
                finally:
                    final_postproofs.python = None
                    final_postproofs.packages = None
            finally:
                confirmation_proofs.python = None
                confirmation_proofs.packages = None
        # Only this successful post-await guard lends bytes to its adjacent
        # effect check. Reopen and verify identities; discard before dispatch.
        self._effect_guard(
            record,
            role,
            verified_python=self.controller_slot.resolved_python,
            verified_packages=effect_packages,
        )
        del effect_packages
        await self._command("start")
