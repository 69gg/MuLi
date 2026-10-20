"""Same-version Linux effects behind the existing durable controller.

The caller owns the journal flock. No helper is launched here, and accepted
systemd commands never stand in for a fresh process, lease or nonce observation.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import stat
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Literal, Self

import httpx

from muli.config import Settings
from muli.management_controller import ServiceObservation
from muli.management_transaction import (
    ClockReading,
    FixedLock,
    HealthProof,
    JournalControl,
    Role,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
)
from muli.platforms.linux_transaction import (
    CommandRunner,
    ObservationError,
    ProbeLimits,
    ServiceSnapshot,
    SystemdProbe,
    clock_reading,
)
from muli.platforms.transaction_files import ConfigurationFile, ConfigurationSnapshot
from muli.process import run_process
from muli.service import UserService


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ObservationError("invalid_transaction_reply")
        value[key] = item
    return value


def transaction_url(configuration: bytes, action: str) -> str:
    """Use the frozen configuration and the existing loopback-only endpoint."""
    settings = Settings.model_validate_json(configuration)
    host = settings.server.host
    host = "127.0.0.1" if host in {"localhost", "0.0.0.0"} else "::1" if host == "::" else host
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ObservationError("transaction_http_not_loopback") from None
    if not address.is_loopback:
        raise ObservationError("transaction_http_not_loopback")
    host = f"[{host}]" if address.version == 6 else host
    return f"http://{host}:{settings.server.port}/api/internal/management/{action}"


async def transaction_request(
    client: httpx.AsyncClient,
    *,
    url: str,
    generation: str,
    boot: str,
    challenge: str,
    action: str,
    seconds: float,
    max_bytes: int,
) -> dict[str, Any] | None:
    """Share only the bounded nonce transport; callers validate their own proof."""
    values = {"generation": generation, "boot": boot}
    try:
        async with asyncio.timeout(seconds):
            async with client.stream(
                "GET" if action == "readiness" else "POST",
                url,
                params=values if action == "readiness" else None,
                json=None if action == "readiness" else values,
                headers={"X-MuLi-Nonce": challenge, "Host": "localhost"},
            ) as response:
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ObservationError("transaction_http_encoding")
                data = bytearray()
                async for part in response.aiter_raw():
                    if len(data) + len(part) > max_bytes:
                        raise ObservationError("transaction_http_budget")
                    data.extend(part)
                if response.status_code == 503 and action == "readiness":
                    return None
                if response.status_code != 200:
                    raise ObservationError("transaction_http_rejected")
                value = json.loads(data, object_pairs_hook=_unique)
                if not isinstance(value, dict):
                    raise ObservationError("invalid_transaction_reply")
                return value
    except (httpx.TransportError, TimeoutError):
        return None
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ObservationError):
            raise
        raise ObservationError("invalid_transaction_reply") from None


class CgroupObservation:
    """Read a manager-provided relative cgroup path without following links."""

    def __init__(self, root: Path, limit: int) -> None:
        self.root, self.limit = root, limit

    def empty(self, path: str) -> bool:
        if not path:
            # This means no cgroup is assigned, not the cgroup mount itself.
            return True
        parts = path[1:].split("/")
        if (
            not path.startswith("/")
            or len(os.fsencode(path)) > self.limit
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ObservationError("invalid_cgroup_path")
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            for part in parts:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
        except FileNotFoundError:
            os.close(descriptor)
            return True
        except BaseException:
            os.close(descriptor)
            raise
        try:
            try:
                source = os.open(
                    "cgroup.events",
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                raise ObservationError("cgroup_events_unavailable") from None
            with os.fdopen(source, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > self.limit:
                    raise ObservationError("invalid_cgroup_events")
                data = handle.read(self.limit + 1)
            if len(data) > self.limit:
                raise ObservationError("cgroup_events_budget")
            rows: dict[bytes, bytes] = {}
            for line in data.splitlines():
                pair = line.split()
                if len(pair) != 2 or pair[0] in rows:
                    raise ObservationError("invalid_cgroup_events")
                rows[pair[0]] = pair[1]
            if rows.get(b"populated") not in {b"0", b"1"}:
                raise ObservationError("invalid_cgroup_events")
            return rows[b"populated"] == b"0"
        finally:
            os.close(descriptor)


class LinuxControllerBackend(AbstractAsyncContextManager["LinuxControllerBackend"]):
    def __init__(
        self,
        service: UserService,
        journal: TransactionJournal,
        verify_installation: Callable[[], None],
        *,
        control: JournalControl,
        clock: Callable[[], ClockReading] = clock_reading,
        probe: SystemdProbe | None = None,
        runner: CommandRunner = run_process,
        transport: httpx.AsyncBaseTransport | None = None,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        operation_timeout: float = 20.0,
    ) -> None:
        if service.platform != "linux" or control.journal is not journal:
            raise ObservationError("transaction_backend_unsupported")
        if not math.isfinite(operation_timeout) or not 0 < operation_timeout <= 30:
            raise ObservationError("invalid_operation_timeout")
        self.service, self.journal, self.control = service, journal, control
        self.verify_installation, self.clock = verify_installation, clock
        self.operation_timeout = operation_timeout
        self.probe = probe or SystemdProbe(
            runner=runner, limits=ProbeLimits(seconds=operation_timeout / 4)
        )
        if self.probe.limits.seconds > operation_timeout / 4:
            raise ObservationError("invalid_probe_timeout")
        self.runner = runner
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

    def _check(self, record: TransactionRecord) -> None:
        self.control.check()
        if self.control.read() != record:
            raise TransactionError("stale_revision")
        self.verify_installation()

    @staticmethod
    def _stopped(snapshot: ServiceSnapshot) -> bool:
        unit = snapshot.unit
        return (
            unit.active_state in {"inactive", "failed"}
            and unit.pid == 0
            and snapshot.process is None
            and unit.job == (0, "/")
        )

    def _guard(self, record: TransactionRecord, lease: FixedLock, path: str) -> None:
        self._effect_guard(record)
        lease.check()
        if not self.cgroups.empty(path):
            raise ObservationError("cgroup_not_empty")

    def _effect_guard(self, record: TransactionRecord) -> None:
        self._check(record)
        remaining = record.deadline.remaining(self.clock())
        if record.phase in {"activating", "verifying"}:
            remaining -= record.deadline.rollback_reserve
        if remaining <= 0:
            raise TimeoutError

    @staticmethod
    def _role(record: TransactionRecord, boot: str) -> Role | None:
        if boot == record.identity.origin_boot:
            return None
        if (record.phase, record.intent) == ("rolling_back", "stop_target"):
            return "target"
        return "base" if record.phase in {"rolling_back", "rolled_back"} else "target"

    def _url(self, record: TransactionRecord, role: Role | None, action: str) -> str:
        return transaction_url(
            self.journal.snapshot(record, "target" if role == "target" else "base"), action
        )

    async def _request(
        self, record: TransactionRecord, boot: str, action: str
    ) -> dict[str, Any] | None:
        role = self._role(record, boot)
        challenge = record.rollback_challenge if role == "base" else record.challenge
        if challenge is None:
            raise ObservationError("transaction_challenge_missing")
        url = self._url(record, role, action)
        return await transaction_request(
            self.client,
            url=url,
            generation=record.generation,
            boot=boot,
            challenge=challenge,
            action=action,
            seconds=self.probe.limits.seconds,
            max_bytes=self.journal.limits.max_record_bytes,
        )

    def _proof(self, record: TransactionRecord, boot: str, value: dict[str, Any]) -> HealthProof:
        try:
            proof = HealthProof.model_validate(value)
        except ValueError:
            raise ObservationError("invalid_transaction_proof") from None
        role = self._role(record, boot)
        challenge = record.rollback_challenge if role == "base" else record.challenge
        reference = record.target if role == "target" else record.base
        identity = record.identity
        if (
            proof.generation != record.generation
            or proof.boot != boot
            or proof.challenge != challenge
            or proof.installation != identity.installation
            or proof.service != identity.service
            or proof.store_instance != identity.store_instance
            or proof.main_session != identity.main_session
            or proof.configuration != reference.digest
            or not proof.core_ready
        ):
            raise ObservationError("foreign_transaction_proof")
        return proof

    async def observe(self, record: TransactionRecord) -> ServiceObservation:
        self._check(record)
        before = await self.probe.read(self.service)
        boot = before.unit.invocation if before.process is not None else None
        configuration = self._configuration()
        writer_free = False
        available = self.journal.writer_lease()
        lease: FixedLock | None = None
        try:
            available.__enter__()
        except TransactionError as exc:
            if exc.code != "lock_busy":
                raise
        else:
            writer_free = True
            if self._stopped(before):
                lease = available
            else:
                # Do not hold a newly available writer lease while waiting for
                # the starting CLI to acquire it and produce its core proof.
                available.__exit__()
        proof: HealthProof | None = None
        empty = False
        try:
            if boot is not None and (
                self._role(record, boot) is not None
                or record.phase in {"prepared", "draining", "aborted"}
                or (record.phase, record.intent)
                in {("activating", "stop_base"), ("rolling_back", "stop_target")}
            ):
                value = await self._request(record, boot, "readiness")
                if value is not None:
                    proof = self._proof(record, boot, value)
            after = await self.probe.read(self.service)
            self._check(record)
            if before != after or configuration != self._configuration():
                raise ObservationError("transaction_observation_changed")
            if self._stopped(after) and lease is not None:
                owned = lease
                owned.check()
                empty = self.cgroups.empty(after.unit.cgroup)
                desired = (
                    record.base
                    if record.phase in {"rolling_back", "rolled_back"}
                    else record.target
                )
                if empty and configuration.digest == desired.digest:
                    with ConfigurationFile(
                        self.service.config_path, self.journal.limits.max_snapshot_bytes
                    ) as file:
                        configuration = file.confirm(
                            desired.digest,
                            guard=lambda: self._guard(record, owned, after.unit.cgroup),
                        )
        finally:
            if lease is not None:
                lease.__exit__()
        state: Literal["running", "stopped", "starting", "stopping", "foreign"]
        if self._stopped(after):
            # Neither MainPID=0 nor an empty ControlGroup string is sufficient
            # without both fresh observations and the actual exclusive lease.
            state = "stopped" if empty else "stopping"
        elif after.unit.active_state == "activating":
            state = "starting"
        elif after.unit.active_state == "deactivating":
            state = "stopping"
        elif after.unit.active_state == "active" and after.process is not None:
            early = (record.phase, record.intent) in {
                ("activating", "start_target"),
                ("rolling_back", "start_base"),
            } and (proof is None or (writer_free and proof.admission != "blocked"))
            state = "starting" if early else "running"
        else:
            state = "foreign"
        origin = boot == record.identity.origin_boot
        return ServiceObservation(
            identity_matches=True,
            configuration=configuration.digest,
            service=state,
            boot=boot,
            writer_free=writer_free,
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

    def _configuration(self) -> ConfigurationSnapshot:
        with ConfigurationFile(
            self.service.config_path, self.journal.limits.max_snapshot_bytes
        ) as file:
            return file.read()

    async def _command(self, action: Literal["start", "stop"]) -> None:
        async with asyncio.timeout(self.probe.limits.seconds):
            result = await self.runner(
                ["systemctl", "--user", "--no-block", "--job-mode=fail", action, self.service.unit],
                timeout=self.probe.limits.seconds,
                limit=self.probe.limits.command_bytes,
                merge_stderr=False,
            )
        if result.get("exit_code") != 0:
            raise ObservationError("transaction_unit_command_failed")

    async def _host_command(
        self, record: TransactionRecord, action: str, role: Role | None
    ) -> None:
        self._check(record)
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
        before = await self.probe.read(self.service)
        boot = before.unit.invocation
        if (
            before.unit.active_state != "active"
            or before.process is None
            or self._role(record, boot) != role
            or action == "activate"
            and (record.health is None or record.health.boot != boot)
        ):
            raise ObservationError("foreign_transaction_process")
        reference = record.target if role == "target" else record.base
        configuration = self._configuration()
        if configuration.digest != reference.digest:
            raise ObservationError("foreign_configuration")
        self._effect_guard(record)
        if action == "activate":
            attempt = record.generation, boot, role
            if self._activation_attempt == attempt:
                return
            # A timeout or cancellation can occur after the host accepted the
            # command. This backend only observes afterwards; a new helper still
            # relies on the endpoint's same-generation strongly-owned task.
            self._activation_attempt = attempt
        value = await self._request(record, boot, action)
        after = await self.probe.read(self.service)
        self._check(record)
        if before != after or configuration != self._configuration():
            raise ObservationError("transaction_observation_changed")
        if value is None:
            return
        if action == "activate":
            if self._proof(record, boot, value).admission == "blocked":
                raise ObservationError("transaction_activation_failed")
        elif (
            (action == "drain" and value.get("state") not in {"waiting", "drained"})
            or (action == "resume" and value.get("released") is not True)
            or (action == "drain" and value.get("boot") != boot)
        ):
            raise ObservationError("invalid_transaction_reply")

    async def drain(self, record: TransactionRecord) -> None:
        await self._host_command(record, "drain", None)

    async def resume(self, record: TransactionRecord) -> None:
        await self._host_command(record, "resume", None)

    async def activate(self, record: TransactionRecord, role: Role) -> None:
        await self._host_command(record, "activate", role)

    async def stop(self, record: TransactionRecord, role: Role) -> None:
        self._check(record)
        if (record.phase, record.intent, role) not in {
            ("activating", "stop_base", "base"),
            ("rolling_back", "stop_target", "base"),
            ("rolling_back", "stop_target", "target"),
        }:
            raise ObservationError("transaction_effect_not_permitted")
        snapshot = await self.probe.read(self.service)
        self._check(record)
        if self._stopped(snapshot):
            return
        if snapshot.process is None or self._role(record, snapshot.unit.invocation) != (
            None
            if role == "base" and snapshot.unit.invocation == record.identity.origin_boot
            else role
        ):
            raise ObservationError("foreign_transaction_process")
        if snapshot.unit.job != (0, "/") or snapshot.unit.active_state == "deactivating":
            return
        reference = record.base if role == "base" else record.target
        if self._configuration().digest != reference.digest:
            raise ObservationError("foreign_configuration")
        self._effect_guard(record)
        await self._command("stop")

    async def install(self, record: TransactionRecord, role: Role, data: bytes) -> None:
        self._check(record)
        if (record.phase, record.intent, role) not in {
            ("activating", "install_target", "target"),
            ("rolling_back", "restore_base", "base"),
        }:
            raise ObservationError("transaction_effect_not_permitted")
        if data != self.journal.snapshot(record, role):
            raise ObservationError("transaction_snapshot_changed")
        before = await self.probe.read(self.service)
        if not self._stopped(before):
            raise ObservationError("transaction_service_not_stopped")
        with self.journal.writer_lease() as lease:
            self._guard(record, lease, before.unit.cgroup)
            if before != await self.probe.read(self.service):
                raise ObservationError("transaction_observation_changed")
            with ConfigurationFile(
                self.service.config_path, self.journal.limits.max_snapshot_bytes
            ) as file:
                current = file.read()
                if current.digest not in {record.base.digest, record.target.digest}:
                    raise ObservationError("foreign_configuration")
                file.replace(
                    current.digest,
                    data,
                    guard=lambda: self._guard(record, lease, before.unit.cgroup),
                )

    async def start(self, record: TransactionRecord, role: Role) -> None:
        self._check(record)
        if (record.phase, record.intent, role) not in {
            ("activating", "start_target", "target"),
            ("rolling_back", "start_base", "base"),
        }:
            raise ObservationError("transaction_effect_not_permitted")
        reference = record.target if role == "target" else record.base
        before = await self.probe.read(self.service)
        if not self._stopped(before):
            raise ObservationError("transaction_service_not_stopped")
        with self.journal.writer_lease() as lease:
            self._guard(record, lease, before.unit.cgroup)
            if before != await self.probe.read(self.service):
                raise ObservationError("transaction_observation_changed")
            with ConfigurationFile(
                self.service.config_path, self.journal.limits.max_snapshot_bytes
            ) as file:
                file.confirm(
                    reference.digest,
                    guard=lambda: self._guard(record, lease, before.unit.cgroup),
                )
        self._effect_guard(record)
        await self._command("start")
