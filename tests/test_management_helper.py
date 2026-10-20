from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_installation import Installation, prepare
from test_installation import installation as installation
from test_management_controller import FakeService

import muli.management_helper as helper_module
from muli.management_controller import ServiceObservation
from muli.management_helper import (
    _drive,
    ensure_helper,
    helper_arguments,
    helper_unit_name,
    run_helper,
)
from muli.management_transaction import (
    ClockReading,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
    Transition,
)
from muli.platforms.filesystem_identity import FilesystemError
from muli.platforms.installation import HelperLimits, InstallationManifest
from muli.platforms.linux_transaction import (
    HelperSnapshot,
    ObservationError,
    ProcessIdentity,
    SystemdProbe,
    UnitSnapshot,
)
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux transaction helper")


@pytest.fixture(autouse=True)
def filesystems(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, ...]]:
    observed: list[tuple[Path, ...]] = []

    def verify(directories: Sequence[Path]) -> None:
        observed.append(tuple(directories))

    monkeypatch.setattr(helper_module, "verify_local_filesystems", verify)
    return observed


@dataclass(slots=True)
class Clock:
    elapsed: float = 0.0
    invalid: bool = False

    def __call__(self) -> ClockReading:
        if self.invalid:
            raise TransactionError("clock_invalid")
        return ClockReading(
            boot_id="kernel-one", wall=1.0 + self.elapsed, monotonic=1.0 + self.elapsed
        )


@dataclass(slots=True)
class Environment:
    journal: TransactionJournal
    manifest: InstallationManifest
    record: TransactionRecord
    clock: Clock


@pytest.fixture
def environment(installation: Installation) -> Iterator[Environment]:
    manifest = installation.capture()
    with TransactionJournal(installation.root, installation.database, manifest.journal) as journal:
        with journal.locked() as control:
            manifest.publish(control)
            record = prepare(control, manifest)
        yield Environment(journal, manifest, record, Clock())


class Probe(SystemdProbe):
    def __init__(self) -> None:
        super().__init__()
        self.exists = False
        self.error = ""
        self.pid = os.getpid()
        self.names: list[str] = []
        self.reads: list[tuple[str, tuple[str, ...], HelperLimits]] = []
        self.after_exists: Callable[[], None] = lambda: None

    async def helper_exists(self, name: str) -> bool:
        self.names.append(name)
        if self.error == "listing":
            raise ObservationError("helper_listing_failed")
        self.after_exists()
        return self.exists

    async def read_helper(
        self, name: str, argv: tuple[str, ...], limits: HelperLimits
    ) -> HelperSnapshot:
        self.reads.append((name, argv, limits))
        if self.error:
            raise ObservationError(self.error)
        cgroup = "/user.slice/app.slice/" + name
        unit = UnitSnapshot(
            name=name,
            load_state="loaded",
            active_state="active",
            sub_state="running",
            fragment="/run/user/1000/systemd/transient/" + name,
            source="",
            drop_ins=(),
            needs_reload=False,
            invocation="a" * 32,
            job=(0, "/"),
            service_type="exec",
            pid=self.pid,
            cgroup=cgroup,
            executable=argv[0],
            argv=argv,
            ignore_failure=False,
            kill_mode="control-group",
            send_sigkill=True,
        )
        process = ProcessIdentity(self.pid, 123, os.geteuid(), argv[0], argv, cgroup)
        return HelperSnapshot(unit, process, limits)


class Runner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.exit_code = 0
        self.entered = asyncio.Event()
        self.finish: asyncio.Event | None = None

    async def __call__(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        self.calls.append(argv)
        assert 0 < timeout <= 5 and limit == 65_536 and merge_stderr
        self.entered.set()
        if self.finish is not None:
            await self.finish.wait()
        return {"exit_code": self.exit_code, "output": "secret-must-not-be-exposed"}


class Application(FakeService):
    async def observe(self, record: TransactionRecord) -> ServiceObservation:
        if self.boot == record.identity.origin_boot:
            return ServiceObservation(
                identity_matches=True,
                configuration=record.base.digest,
                service=self.service,
                boot=self.boot,
                writer_free=self.writer_free,
                drained=self.drained,
                resumed=self.resumed,
            )
        return await super().observe(record)


def service(environment: Environment) -> Application:
    backend = Application()
    backend.configuration = b"{}"
    backend.boot = environment.record.identity.origin_boot
    return backend


async def test_ensure_new_helper_is_only_accepted_with_literal_argv_and_fixed_limits(
    environment: Environment,
    filesystems: list[tuple[Path, ...]],
) -> None:
    journal, manifest = environment.journal, environment.manifest
    probe, runner = Probe(), Runner()
    with journal.locked() as control:
        assert await ensure_helper(control, manifest, probe=probe, runner=runner) == "accepted"
        assert control.read() == environment.record
    assert len(runner.calls) == 1 and not probe.reads
    argv = runner.calls[0]
    split = argv.index("--")
    assert tuple(argv[split + 1 :]) == helper_arguments(manifest, journal.directory.path)
    assert argv[0] == "systemd-run" and "--expand-environment=no" in argv
    assert "--unit=" + helper_unit_name(environment.record) in argv
    assert not any(argument in argv for argument in ("--collect", "--scope", "--wait"))
    properties = dict(
        argument[11:].split("=", 1) for argument in argv if argument.startswith("--property=")
    )
    assert properties["CollectMode"] == "inactive"
    assert properties["MemoryMax"] == str(manifest.helper.memory_bytes)
    assert properties["MemorySwapMax"] == "0"
    assert properties["CPUQuota"] == "25%" and properties["TasksMax"] == "32"
    assert properties["OOMPolicy"] == "kill" and properties["UMask"] == "0077"
    assert properties["RuntimeMaxSec"] == properties["StartLimitIntervalSec"] == "180.000000s"
    assert properties["Restart"] == "on-failure" and properties["StartLimitBurst"] == "3"
    assert not {"PartOf", "BindsTo", "Requires", "Requisite", "Conflicts"}.intersection(properties)
    assert filesystems and all(
        directories
        == (
            journal.directory.path,
            Path(manifest.config_path).parent,
            journal.database.parent,
            journal.writer_directory.path,
        )
        for directories in filesystems
    )


async def test_unverified_filesystem_cannot_launch_or_enter_application_backend(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe, runner = Probe(), Runner()

    def failed(directories: Sequence[Path]) -> None:
        raise FilesystemError("filesystem_unsupported")

    monkeypatch.setattr(helper_module, "verify_local_filesystems", failed)
    monkeypatch.setattr(helper_module, "LinuxControllerBackend", Backend)
    before = len(Backend.instances)
    with environment.journal.locked() as control:
        with pytest.raises(FilesystemError, match="filesystem_unsupported"):
            await ensure_helper(control, environment.manifest, probe=probe, runner=runner)
    with pytest.raises(FilesystemError, match="filesystem_unsupported"):
        await run_helper(environment.journal.directory.path, probe=probe, clock=environment.clock)
    assert len(Backend.instances) == before and not probe.names and not runner.calls
    assert environment.journal.read() == environment.record


async def test_backend_effect_verifier_rechecks_filesystem_before_any_effect(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = False

    def verify(directories: Sequence[Path]) -> None:
        if failed:
            raise FilesystemError("filesystem_mount_changed")

    class ChangedMount(Backend):
        async def drain(self, record: TransactionRecord) -> None:
            nonlocal failed
            failed = True
            self.verify_installation()
            await super().drain(record)

    monkeypatch.setattr(helper_module, "verify_local_filesystems", verify)
    monkeypatch.setattr(helper_module, "LinuxControllerBackend", ChangedMount)
    result = await run_helper(
        environment.journal.directory.path, probe=Probe(), clock=environment.clock
    )
    assert result.phase == "recovery_required" and result.code == "backend_failed"
    assert not Backend.instances[-1].calls


async def test_existing_valid_helper_is_reused_without_a_second_launch(
    environment: Environment,
) -> None:
    probe, runner = Probe(), Runner()
    probe.exists = True
    with environment.journal.locked() as control:
        for _ in range(2):
            assert (
                await ensure_helper(control, environment.manifest, probe=probe, runner=runner)
                == "running"
            )
    assert len(probe.reads) == 2 and not runner.calls


@pytest.mark.parametrize("error", ["listing", "inactive", "wrong_argv", "helper_kernel_limits"])
async def test_existing_or_unreadable_helper_never_falls_back_to_creation(
    environment: Environment, error: str
) -> None:
    probe, runner = Probe(), Runner()
    probe.exists, probe.error = True, error
    with environment.journal.locked() as control:
        with pytest.raises(ObservationError):
            await ensure_helper(control, environment.manifest, probe=probe, runner=runner)
        assert control.read() == environment.record
    assert not runner.calls


async def test_create_conflict_is_fixed_failure_without_reset_or_alternate_name(
    environment: Environment,
) -> None:
    runner = Runner()
    runner.exit_code = 1
    with environment.journal.locked() as control:
        with pytest.raises(TransactionError, match="^helper_start_failed$"):
            await ensure_helper(control, environment.manifest, probe=Probe(), runner=runner)
        assert control.read() == environment.record
    assert len(runner.calls) == 1 and "reset-failed" not in runner.calls[0]


async def test_candidate_changed_during_listing_cannot_launch(environment: Environment) -> None:
    probe, runner = Probe(), Runner()
    with environment.journal.locked() as control:
        probe.after_exists = lambda: control.advance(
            environment.record.position, Transition(phase="draining")
        )
        with pytest.raises(TransactionError, match="transaction_changed"):
            await ensure_helper(control, environment.manifest, probe=probe, runner=runner)
    assert not runner.calls


async def test_ensure_cancel_propagates_without_retry_or_false_completion(
    environment: Environment,
) -> None:
    runner = Runner()
    runner.finish = asyncio.Event()
    with environment.journal.locked() as control:
        task = asyncio.create_task(
            ensure_helper(control, environment.manifest, probe=Probe(), runner=runner)
        )
        await runner.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert control.read() == environment.record
    assert len(runner.calls) == 1


def test_helper_identity_uses_full_generation_and_preserves_lexical_venv_entry(
    environment: Environment, tmp_path: Path
) -> None:
    name = helper_unit_name(environment.record)
    changed = environment.record.model_copy(update={"generation": "other-generation"})
    assert len(name.removeprefix("muli-transaction-").removesuffix(".service")) == 64
    assert helper_unit_name(changed) != name
    root = tmp_path / "literal $NAME %i with spaces"
    root.mkdir()
    argv = helper_arguments(environment.manifest, root)
    assert argv[-1] == str(root) and argv[1:4] == ("-I", "-m", "muli.management_helper")
    assert argv[0] == environment.manifest.interpreter.executable
    assert argv[0] != environment.manifest.interpreter.real_executable


@pytest.mark.parametrize("root", ["relative", "../parent"])
def test_relative_or_parent_root_is_rejected(environment: Environment, root: str) -> None:
    with pytest.raises(TransactionError, match="invalid_journal_root"):
        helper_arguments(environment.manifest, Path(root))


@pytest.mark.parametrize("rollback", [False, True])
async def test_helper_loop_drives_existing_commit_or_single_rollback_without_new_deadline(
    environment: Environment, rollback: bool
) -> None:
    backend = service(environment)
    backend.healthy_target = not rollback
    verified: list[str] = []

    async def verify() -> None:
        verified.append("self")

    async def wait(seconds: float) -> None:
        assert seconds == environment.manifest.helper.poll_interval

    with environment.journal.locked() as control:
        result = await _drive(
            control, backend, environment.manifest.helper, environment.clock, verify, wait=wait
        )
        assert result.resolved and result.phase == ("rolled_back" if rollback else "committed")
        assert result.deadline == environment.record.deadline
        assert backend.calls.count("start:target") == 1
        assert backend.calls.count("start:base") == int(rollback)
        assert verified and control.read() == result


async def test_no_progress_wait_uses_original_deadline_and_keeps_old_service(
    environment: Environment,
) -> None:
    backend = service(environment)
    backend.drain_ready = False
    waits: list[float] = []

    async def verify() -> None:
        pass

    async def wait(seconds: float) -> None:
        waits.append(seconds)
        environment.clock.elapsed = 181.0

    with environment.journal.locked() as control:
        result = await _drive(
            control, backend, environment.manifest.helper, environment.clock, verify, wait=wait
        )
    assert result.phase == "recovery_required" and result.code == "deadline_exceeded"
    assert waits == [environment.manifest.helper.poll_interval]
    assert backend.service == "running" and not any(
        call.startswith("stop:") for call in backend.calls
    )


async def test_decision_blocked_exits_without_automatic_activation_retry(
    environment: Environment,
) -> None:
    backend = service(environment)
    backend.activation_error = True

    async def verify() -> None:
        pass

    with environment.journal.locked() as control:
        result = await _drive(
            control, backend, environment.manifest.helper, environment.clock, verify
        )
    assert result.phase == "committed" and result.activation == "blocked" and not result.resolved
    assert backend.calls.count("start:target") == 1 and not backend.activations


async def test_cancel_keeps_last_durable_intent_without_finally_retry(
    environment: Environment,
) -> None:
    backend = service(environment)
    backend.interrupt = "after_start:target"

    async def verify() -> None:
        pass

    with environment.journal.locked() as control:
        with pytest.raises(asyncio.CancelledError):
            await _drive(control, backend, environment.manifest.helper, environment.clock, verify)
        current = control.read()
        assert current is not None and current.intent == "start_target" and not current.resolved
    assert backend.calls.count("start:target") == 1


class Backend(Application):
    closed = False
    instances: list[Backend] = []

    def __init__(
        self,
        service: UserService,
        journal: TransactionJournal,
        verify_installation: Callable[[], None],
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.configuration = b"{}"
        self.boot = "boot-one"
        self.verify_installation = verify_installation
        self.instances.append(self)

    async def __aenter__(self) -> Backend:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True


async def test_runner_verifies_its_actual_pid_and_limits_before_any_controller_effect(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(helper_module, "LinuxControllerBackend", Backend)
    probe = Probe()
    probe.pid = os.getpid() + 1
    before = len(Backend.instances)
    with pytest.raises(TransactionError, match="foreign_helper_process"):
        await run_helper(environment.journal.directory.path, probe=probe, clock=environment.clock)
    backend = Backend.instances[before]
    assert backend.closed and not backend.calls
    assert environment.journal.read() == environment.record


@pytest.mark.parametrize("invalid_clock", [False, True])
async def test_expired_or_invalid_clock_helper_persists_local_outcome_without_application_effects(
    environment: Environment, monkeypatch: pytest.MonkeyPatch, invalid_clock: bool
) -> None:
    monkeypatch.setattr(helper_module, "LinuxControllerBackend", Backend)
    environment.clock.elapsed = 181.0
    environment.clock.invalid = invalid_clock
    with environment.journal.locked() as control:
        assert (
            await ensure_helper(control, environment.manifest, probe=Probe(), runner=Runner())
            == "accepted"
        )
    before = len(Backend.instances)
    result = await run_helper(
        environment.journal.directory.path, probe=Probe(), clock=environment.clock
    )
    backend = Backend.instances[before]
    assert result.phase == "recovery_required"
    assert result.code == ("clock_invalid" if invalid_clock else "deadline_exceeded")
    assert result.deadline == environment.record.deadline
    assert backend.closed and not backend.calls


async def test_resolved_takeover_still_gets_final_hint_and_missing_hint_cannot_undo_release(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    class MissingHint(Backend):
        hints: list[bool] = []

        async def observe(self, record: TransactionRecord) -> Any:
            if record.resolved:
                self.hints.append(True)
                raise TimeoutError
            return await super().observe(record)

    monkeypatch.setattr(helper_module, "LinuxControllerBackend", MissingHint)
    root = environment.journal.directory.path
    result = await run_helper(root, probe=Probe(), clock=environment.clock)
    assert result.resolved and MissingHint.hints == [True]
    again = await run_helper(root, probe=Probe(), clock=environment.clock)
    assert again == result and MissingHint.hints == [True, True]
    runner = Runner()
    with environment.journal.locked() as control:
        with pytest.raises(TransactionError, match="transaction_not_pending"):
            await ensure_helper(control, environment.manifest, probe=Probe(), runner=runner)
    assert not runner.calls


def test_cli_reports_only_fixed_error_without_raw_exception(
    environment: Environment, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failed(root: Path) -> TransactionRecord:
        raise RuntimeError("secret-config-nonce-env")

    monkeypatch.setattr(helper_module, "run_helper", failed)
    monkeypatch.setattr(sys, "argv", ["helper", str(environment.journal.directory.path)])
    assert helper_module.main() == 1
    assert capsys.readouterr().err == "helper_failed\n"
    monkeypatch.setattr(sys, "argv", ["helper", "/root", "extra-command"])
    assert helper_module.main() == 2
    assert capsys.readouterr().err == "helper_invalid_arguments\n"
