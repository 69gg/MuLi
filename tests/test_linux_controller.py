from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from muli.management_controller import TransactionController
from muli.management_transaction import (
    ClockReading,
    Deadline,
    FixedLock,
    HealthProof,
    JournalControl,
    Role,
    ServiceIdentity,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
    Transition,
)
from muli.platforms.linux_controller import CgroupObservation, LinuxControllerBackend
from muli.platforms.linux_transaction import (
    ObservationError,
    ProbeLimits,
    ProcessIdentity,
    ServiceSnapshot,
    SystemdProbe,
    UnitSnapshot,
)
from muli.platforms.transaction_files import ConfigurationFile, ConfigurationSnapshot
from muli.service import UserService

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux journal/lease/cgroup backend"
)
BASE = b'{"server":{"host":"127.0.0.1","port":8787}}'
TARGET = b'{"server":{"host":"127.0.0.1","port":8788}}'
ORIGIN, TARGET_BOOT, BASE_BOOT = "1" * 32, "2" * 32, "3" * 32


class Clock:
    elapsed: float = 0.0

    def __call__(self) -> ClockReading:
        return ClockReading(
            boot_id="kernel", wall=1000.0 + self.elapsed, monotonic=100.0 + self.elapsed
        )


class Parts(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data


def reply(value: object, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, stream=Parts(json.dumps(value).encode()), headers=headers)


class Probe(SystemdProbe):
    def __init__(self, service: UserService) -> None:
        super().__init__(limits=ProbeLimits(seconds=0.05))
        self.count = 0
        self.before_read: Callable[[int], None] | None = None
        unit = UnitSnapshot(
            name=service.unit,
            load_state="loaded",
            active_state="active",
            sub_state="running",
            fragment=str(service.path),
            source="",
            drop_ins=(),
            needs_reload=False,
            invocation=ORIGIN,
            job=(0, "/"),
            service_type="simple",
            pid=123,
            cgroup="/user.slice/muli.service",
            executable=service.executable,
            argv=tuple(service._arguments()),
            ignore_failure=False,
            kill_mode="control-group",
            send_sigkill=True,
        )
        self.snapshot = ServiceSnapshot(
            unit,
            ProcessIdentity(123, 100, os.geteuid(), service.executable, unit.argv, unit.cgroup),
        )

    async def read(self, service: UserService) -> ServiceSnapshot:
        self.count += 1
        if self.before_read is not None:
            self.before_read(self.count)
        return self.snapshot


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, host: Host) -> None:
        self.host = host
        self.calls: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], Awaitable[httpx.Response]] | None = None
        self.unavailable = False
        self.block_target = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.handler is not None:
            return await self.handler(request)
        if self.unavailable:
            return reply({"detail": "maintenance_incomplete"}, 503)
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "drain":
            self.host.admission = "paused"
            return reply({"state": "drained", "boot": ORIGIN, "owners": {}})
        if action == "resume":
            self.host.admission = "released"
            return reply({"released": True})
        if action == "activate":
            self.host.admission = "released"
        admission = (
            "blocked" if self.block_target and self.host.role == "target" else self.host.admission
        )
        return reply(self.host.proof(admission=admission).model_dump())


class Host:
    """Only fake process/HTTP effects; locks and config bytes are real temporary files."""

    def __init__(self, root: Path, journal: TransactionJournal, control: JournalControl) -> None:
        self.journal, self.control, self.clock = journal, control, Clock()
        self.service = UserService(
            root / "config.json",
            root / "data",
            root / "units",
            platform="linux",
            executable=str(root / "python"),
        )
        self.service.config_path.write_bytes(BASE)
        self.probe = Probe(self.service)
        self.cgroup_root = root / "cgroups"
        self.events = self.cgroup_root / "user.slice/muli.service/cgroup.events"
        self.events.parent.mkdir(parents=True)
        self.events.write_bytes(b"populated 0\nfrozen 0\n")
        self.role: Role | None = None
        self.admission: str = "released"
        self.writer: FixedLock | None = None
        self.commands: list[list[str]] = []
        self.verifications = 0
        self.transport = Transport(self)
        self.backend = LinuxControllerBackend(
            self.service,
            journal,
            self.verify,
            control=control,
            clock=self.clock,
            probe=self.probe,
            runner=self.command,
            transport=self.transport,
            cgroup_root=self.cgroup_root,
        )
        self.own_writer()

    def verify(self) -> None:
        self.verifications += 1

    def own_writer(self) -> None:
        assert self.writer is None
        self.writer = self.journal.writer_lease().__enter__()

    def release_writer(self) -> None:
        if self.writer is not None:
            self.writer.__exit__()
            self.writer = None

    def record(self) -> TransactionRecord:
        record = self.control.read()
        assert record is not None
        return record

    def move(self, phase: str, intent: str = "none", **kwargs: Any) -> TransactionRecord:
        return self.control.advance(
            self.record().position,
            Transition.model_validate({"phase": phase, "intent": intent, **kwargs}),
        )

    def target_intent(self, intent: str) -> TransactionRecord:
        self.move("draining")
        record = self.move("activating", "stop_base")
        if intent == "stop_base":
            return record
        record = self.move("activating", "install_target")
        if intent == "install_target":
            return record
        return self.move("activating", "start_target")

    def stopped(self, *, cgroup: str | None = None) -> None:
        self.release_writer()
        self.probe.snapshot = ServiceSnapshot(
            replace(
                self.probe.snapshot.unit,
                active_state="inactive",
                sub_state="dead",
                pid=0,
                cgroup=self.probe.snapshot.unit.cgroup if cgroup is None else cgroup,
                job=(0, "/"),
            ),
            None,
        )

    def running(self, role: Role, *, own_writer: bool = True) -> None:
        self.release_writer()
        self.role, self.admission = role, "paused"
        unit = replace(
            self.probe.snapshot.unit,
            active_state="active",
            sub_state="running",
            pid=456,
            invocation=TARGET_BOOT if role == "target" else BASE_BOOT,
        )
        self.probe.snapshot = ServiceSnapshot(
            unit,
            ProcessIdentity(
                456, 200, os.geteuid(), self.service.executable, unit.argv, unit.cgroup
            ),
        )
        if own_writer:
            self.own_writer()

    def proof(self, *, admission: str = "paused") -> HealthProof:
        record = self.record()
        return HealthProof.model_validate(
            {
                "generation": record.generation,
                "challenge": record.rollback_challenge if self.role == "base" else record.challenge,
                "boot": self.probe.snapshot.unit.invocation,
                "installation": record.identity.installation,
                "service": record.identity.service,
                "store_instance": record.identity.store_instance,
                "main_session": record.identity.main_session,
                "configuration": (record.target if self.role == "target" else record.base).digest,
                "core_ready": True,
                "admission": admission,
            }
        )

    async def command(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert timeout == self.probe.limits.seconds and limit == self.probe.limits.command_bytes
        assert not merge_stderr and "--no-block" in argv and "--job-mode=fail" in argv
        self.commands.append(argv)
        if "stop" in argv:
            self.stopped()
        else:
            self.running("target" if self.service.config_path.read_bytes() == TARGET else "base")
        return {"exit_code": 0, "output": ""}


@pytest.fixture
async def host(tmp_path: Path) -> AsyncIterator[Host]:
    with TransactionJournal(tmp_path / "journal", tmp_path / "data/muli.db") as journal:
        with journal.locked() as control:
            with journal.writer_lease() as writer:
                control.prepare(
                    request_id="configure",
                    action="configure",
                    identity=ServiceIdentity(
                        installation="a" * 64,
                        service="b" * 64,
                        store_instance="store",
                        main_session="main",
                        origin_boot=ORIGIN,
                    ),
                    base=BASE,
                    target=TARGET,
                    deadline=Deadline(created=Clock()(), seconds=60.0, rollback_reserve=10.0),
                    writer=writer,
                )
            value = Host(tmp_path, journal, control)
            try:
                async with value.backend:
                    yield value
            finally:
                value.release_writer()


async def test_real_controller_backend_commit_uses_only_fake_effects(
    host: Host,
) -> None:
    driver = TransactionController(
        host.control, host.backend, host.clock, operation_timeout=host.backend.operation_timeout
    )
    for _ in range(24):
        result = await driver.step()
        if result.resolved:
            break
    assert result.resolved and result.phase == "committed"
    assert host.service.config_path.read_bytes() == TARGET
    assert [call[-2] for call in host.commands] == ["stop", "start"]
    assert sum(call.url.path.endswith("/activate") for call in host.transport.calls) == 1


async def test_real_controller_rollback_uses_target_nonce_until_target_has_stopped(
    host: Host,
) -> None:
    host.transport.block_target = True
    driver = TransactionController(
        host.control, host.backend, host.clock, operation_timeout=host.backend.operation_timeout
    )
    for _ in range(24):
        result = await driver.step()
        if result.resolved:
            break
    assert result.resolved and result.phase == "rolled_back"
    assert host.service.config_path.read_bytes() == BASE
    assert [call[-2] for call in host.commands] == ["stop", "start", "stop", "start"]
    reads = [call for call in host.transport.calls if call.method == "GET"]
    assert any(
        call.url.port == 8788 and call.headers["X-MuLi-Nonce"] == result.challenge for call in reads
    )
    assert any(
        call.url.port == 8787 and call.headers["X-MuLi-Nonce"] == result.rollback_challenge
        for call in reads
    )


async def test_simple_active_start_waits_for_lease_and_core_without_masking_verifying(
    host: Host,
) -> None:
    record = host.target_intent("start_target")
    host.service.config_path.write_bytes(TARGET)
    host.running("target", own_writer=False)
    host.transport.unavailable = True
    early = await host.backend.observe(record)
    assert early.service == "starting" and early.writer_free and early.health is None
    host.own_writer()
    assert (await host.backend.observe(record)).service == "starting"
    host.transport.unavailable = False
    assert (await host.backend.observe(record)).service == "running"
    record = host.move("verifying")
    host.transport.unavailable = True
    verifying = await host.backend.observe(record)
    assert (
        verifying.service == "running" and verifying.health is None and not verifying.health_failed
    )


async def test_explicit_blocked_proof_is_not_soft_starting(host: Host) -> None:
    record = host.target_intent("start_target")
    host.service.config_path.write_bytes(TARGET)
    host.running("target", own_writer=False)
    host.transport.block_target = True
    observed = await host.backend.observe(record)
    assert observed.service == "running" and observed.health_failed


@pytest.mark.parametrize("cgroup", ["", "/removed", "/user.slice/muli.service"])
async def test_stopped_observation_holds_real_lease_across_fresh_probes(
    host: Host, cgroup: str
) -> None:
    record = host.target_intent("install_target")
    host.stopped(cgroup=cgroup)

    def check_second(count: int) -> None:
        if count == 2:
            with pytest.raises(TransactionError, match="lock_busy"), host.journal.writer_lease():
                pytest.fail("observation released its writer before the second probe")

    host.probe.before_read = check_second
    observed = await host.backend.observe(record)
    assert observed.service == "stopped" and observed.writer_free


async def test_mainpid_zero_with_writer_or_descendant_is_not_stopped(host: Host) -> None:
    record = host.target_intent("install_target")
    host.stopped(cgroup="")
    host.own_writer()
    held = await host.backend.observe(record)
    assert held.service == "stopping" and not held.writer_free
    host.release_writer()
    host.stopped(cgroup="/user.slice/muli.service")
    host.events.write_bytes(b"populated 1\nfrozen 0\n")
    populated = await host.backend.observe(record)
    assert populated.service == "stopping" and populated.writer_free
    with pytest.raises(ObservationError, match="cgroup_not_empty"):
        await host.backend.install(record, "target", TARGET)
    assert host.service.config_path.read_bytes() == BASE and not host.commands


async def test_active_or_job_unit_never_becomes_stopped_from_blank_cgroup(host: Host) -> None:
    record = host.target_intent("install_target")
    host.stopped(cgroup="")
    host.probe.snapshot = replace(
        host.probe.snapshot, unit=replace(host.probe.snapshot.unit, job=(9, "/job/9"))
    )
    observed = await host.backend.observe(record)
    assert observed.service != "stopped"
    with pytest.raises(ObservationError, match="not_stopped"):
        await host.backend.install(record, "target", TARGET)


@pytest.mark.parametrize("kind", ["missing_events", "fifo", "symlink", "invalid", "budget"])
async def test_cgroup_events_fail_closed_without_blocking(host: Host, kind: str) -> None:
    record = host.target_intent("install_target")
    host.stopped()
    host.events.unlink()
    if kind == "fifo":
        os.mkfifo(host.events)
    elif kind == "symlink":
        host.events.symlink_to(host.service.config_path)
    elif kind == "invalid":
        host.events.write_bytes(b"populated 0\npopulated 1\n")
    elif kind == "budget":
        host.events.write_bytes(b"x" * (host.probe.limits.file_bytes + 1))
    with pytest.raises((ObservationError, OSError)):
        await host.backend.observe(record)
    assert not host.commands


@pytest.mark.parametrize("path", ["/", "relative", "/a/../b", "/a//b"])
def test_cgroup_path_is_not_inferred_or_normalized(tmp_path: Path, path: str) -> None:
    with pytest.raises(ObservationError, match="invalid_cgroup_path"):
        CgroupObservation(tmp_path, 1024).empty(path)


async def test_os_error_reading_cgroup_is_not_absence(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = host.target_intent("install_target")
    host.stopped()
    original = os.open

    def failing(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path == "cgroup.events":
            raise PermissionError(errno.EACCES, "fixture")
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", failing)
    with pytest.raises(PermissionError):
        await host.backend.observe(record)
    assert not host.commands


async def test_changed_invocation_during_nonce_roundtrip_never_yields_receipt(host: Host) -> None:
    record = host.move("draining")

    async def changed(request: httpx.Request) -> httpx.Response:
        proof = host.proof().model_dump()
        host.probe.snapshot = replace(
            host.probe.snapshot, unit=replace(host.probe.snapshot.unit, invocation="4" * 32)
        )
        return reply(proof)

    host.transport.handler = changed
    with pytest.raises(ObservationError, match="observation_changed"):
        await host.backend.observe(record)


@pytest.mark.parametrize(
    "field,value",
    [("boot", ORIGIN), ("configuration", "f" * 64), ("core_ready", False), ("extra", True)],
)
async def test_invalid_or_wrong_owner_health_is_hard_failure(
    host: Host, field: str, value: object
) -> None:
    record = host.target_intent("start_target")
    host.service.config_path.write_bytes(TARGET)
    host.running("target")

    async def wrong(request: httpx.Request) -> httpx.Response:
        return reply({**host.proof().model_dump(), field: value})

    host.transport.handler = wrong
    with pytest.raises(ObservationError, match="transaction_proof"):
        await host.backend.observe(record)


@pytest.mark.parametrize("kind", ["redirect", "gzip", "oversize", "duplicate", "reject"])
async def test_http_is_bounded_uncompressed_and_does_not_follow_redirects(
    host: Host, kind: str
) -> None:
    record = host.move("draining")

    async def malformed(request: httpx.Request) -> httpx.Response:
        if kind == "redirect":
            return reply({}, 307, location="http://external.invalid/nonce")
        if kind == "gzip":
            return reply({}, **{"content-encoding": "gzip"})
        if kind == "oversize":
            return httpx.Response(
                200, stream=Parts(b"x" * (host.journal.limits.max_record_bytes + 1))
            )
        if kind == "duplicate":
            return httpx.Response(200, stream=Parts(b'{"boot":"a","boot":"b"}'))
        return reply({}, 409)

    host.transport.handler = malformed
    with pytest.raises(ObservationError):
        await host.backend.observe(record)
    assert len(host.transport.calls) == 1


async def test_existing_stop_job_is_observed_without_replacing_job(host: Host) -> None:
    record = host.target_intent("stop_base")
    host.probe.snapshot = replace(
        host.probe.snapshot, unit=replace(host.probe.snapshot.unit, job=(5, "/job/5"))
    )
    await host.backend.stop(record, "base")
    assert not host.commands


@pytest.mark.parametrize("action", ["drain", "resume", "stop", "install", "start", "activate"])
async def test_effects_require_exact_persisted_phase_role_and_snapshot(
    host: Host, action: str
) -> None:
    record = host.target_intent("install_target")
    host.stopped()
    with pytest.raises(ObservationError, match="effect_not_permitted"):
        if action == "drain":
            await host.backend.drain(record)
        elif action == "resume":
            await host.backend.resume(record)
        elif action == "stop":
            await host.backend.stop(record, "base")
        elif action == "install":
            await host.backend.install(record, "base", BASE)
        elif action == "start":
            await host.backend.start(record, "target")
        else:
            await host.backend.activate(record, "target")
    assert (
        not host.transport.calls
        and not host.commands
        and host.service.config_path.read_bytes() == BASE
    )


async def test_cas_refuses_manual_config_and_snapshot_tampering(host: Host) -> None:
    record = host.target_intent("install_target")
    host.stopped()
    with pytest.raises(ObservationError, match="snapshot_changed"):
        await host.backend.install(record, "target", BASE)
    host.service.config_path.write_bytes(b'{"manually":"edited"}')
    with pytest.raises(ObservationError, match="foreign_configuration"):
        await host.backend.install(record, "target", TARGET)
    assert host.service.config_path.read_bytes() == b'{"manually":"edited"}'


async def test_desired_visible_but_directory_sync_eio_prevents_observe_and_start(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = host.target_intent("start_target")
    host.stopped()
    host.service.config_path.write_bytes(TARGET)
    parent = host.service.config_path.parent.stat()
    fsync = os.fsync

    def failed(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (
            parent.st_dev,
            parent.st_ino,
        ):
            raise OSError(errno.EIO, "fixture")
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises(OSError):
        await host.backend.observe(record)
    with pytest.raises(OSError):
        await host.backend.start(record, "target")
    assert not host.commands


@pytest.mark.parametrize("point", ["second_probe", "confirm"])
async def test_expired_deadline_after_await_or_fsync_never_starts(
    host: Host, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    record = host.target_intent("start_target")
    host.stopped()
    host.service.config_path.write_bytes(TARGET)
    if point == "second_probe":

        def expired(count: int) -> None:
            if count == 2:
                host.clock.elapsed = 51.0

        host.probe.before_read = expired
    else:
        confirm = ConfigurationFile.confirm

        def expired_confirm(
            file: ConfigurationFile, expected_digest: str, *, guard: Callable[[], None]
        ) -> ConfigurationSnapshot:
            result = confirm(file, expected_digest, guard=guard)
            host.clock.elapsed = 51.0
            return result

        monkeypatch.setattr(ConfigurationFile, "confirm", expired_confirm)
    with pytest.raises(TimeoutError):
        await host.backend.start(record, "target")
    assert not host.commands


async def test_backend_clock_invalid_during_effect_is_persisted_without_cas(host: Host) -> None:
    host.target_intent("install_target")
    host.stopped()

    def invalid(count: int) -> None:
        if count == 3:
            host.clock.elapsed = -1.0

    host.probe.before_read = invalid
    driver = TransactionController(
        host.control, host.backend, host.clock, operation_timeout=host.backend.operation_timeout
    )
    result = await driver.step()
    assert result.phase == "recovery_required" and result.code == "clock_invalid"
    assert not host.commands and host.service.config_path.read_bytes() == BASE


async def test_stale_record_or_changed_installation_prevents_every_effect(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = host.record()
    record = host.move("draining")
    with pytest.raises(TransactionError, match="stale_revision"):
        await host.backend.drain(stale)

    def changed() -> None:
        raise ObservationError("installation_changed")

    monkeypatch.setattr(host.backend, "verify_installation", changed)
    with pytest.raises(ObservationError, match="installation_changed"):
        await host.backend.drain(record)
    assert not host.commands and not host.transport.calls


async def test_activate_timeout_keeps_owned_effect_pending_then_get_resolves_without_second_post(
    host: Host,
) -> None:
    host.target_intent("start_target")
    host.service.config_path.write_bytes(TARGET)
    host.running("target")
    host.move("verifying")
    record = host.move("committed", proof=host.proof())
    posted = asyncio.Event()

    async def delayed(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.set()
            await asyncio.Event().wait()
        return reply(host.proof(admission=host.admission).model_dump())

    host.transport.handler = delayed
    driver = TransactionController(
        host.control, host.backend, host.clock, operation_timeout=host.backend.operation_timeout
    )
    assert await driver.step() == record and posted.is_set()
    assert await driver.step() == record
    host.admission = "released"
    result = await driver.step()
    assert result.resolved and result.deadline == record.deadline
    assert sum(call.method == "POST" for call in host.transport.calls) == 1


async def test_cancelling_activate_propagates_but_same_backend_does_not_resend(host: Host) -> None:
    host.target_intent("start_target")
    host.service.config_path.write_bytes(TARGET)
    host.running("target")
    host.move("verifying")
    record = host.move("committed", proof=host.proof())
    posted = asyncio.Event()

    async def delayed(request: httpx.Request) -> httpx.Response:
        posted.set()
        await asyncio.Event().wait()
        return reply({})

    host.transport.handler = delayed
    task = asyncio.create_task(host.backend.activate(record, "target"))
    await posted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await host.backend.activate(record, "target")
    assert sum(call.method == "POST" for call in host.transport.calls) == 1


def test_probe_budget_must_fit_shared_action_budget(host: Host) -> None:
    with pytest.raises(ObservationError, match="invalid_probe_timeout"):
        LinuxControllerBackend(
            host.service,
            host.journal,
            host.verify,
            control=host.control,
            clock=host.clock,
            probe=host.probe,
            operation_timeout=0.1,
        )
