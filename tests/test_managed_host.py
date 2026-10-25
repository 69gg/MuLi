from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_linux_controller import Clock
from test_managed_backend import Host
from test_managed_controller import FakeManagedService
from test_management_helper import Probe as HelperProbe
from test_management_helper import Runner

import muli
import muli.managed_host as host_module
import muli.platforms.managed_deployment as deployment_module
from muli.managed_controller import ManagedServiceObservation
from muli.managed_host import (
    ensure_managed_helper,
    managed_helper_arguments,
    managed_helper_unit_name,
    recover,
    run_managed_helper,
)
from muli.managed_journal import advance_managed, read_snapshots
from muli.managed_transaction import TransactionRecordV2, TransitionV2
from muli.management_transaction import JournalControl, Role, TransactionError, TransactionJournal
from muli.platforms.filesystem_identity import FilesystemError
from muli.platforms.installation import FileFingerprint, HelperLimits
from muli.platforms.linux_transaction import HelperSnapshot
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    ManagedDeploymentError,
    SlotIdentity,
)
from muli.platforms.managed_launcher import PackageProofs
from muli.platforms.managed_probe import ManagedSystemdProbe

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed helper/real locks")


@dataclass(slots=True)
class Environment:
    journal: TransactionJournal
    anchor: DeploymentAnchor
    slots: dict[Role, SlotIdentity]
    record: TransactionRecordV2
    clock: Clock


@pytest.fixture(autouse=True)
def filesystems(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, ...]]:
    observed: list[tuple[Path, ...]] = []

    def verify(directories: Sequence[Path]) -> None:
        observed.append(tuple(directories))

    monkeypatch.setattr(host_module, "verify_local_filesystems", verify)
    return observed


@pytest.fixture
async def environment(tmp_path: Path) -> AsyncIterator[Environment]:
    with TransactionJournal(tmp_path / "journal", tmp_path / "data/muli.sqlite3") as journal:
        with journal.locked() as control:
            host = Host(tmp_path, journal, control, "switch", change_config=False)
            try:
                record = host.record()
                # The helper checks ordinary path identity, never SQLite data.
                journal.database.write_bytes(b"")
                journal.database.chmod(0o600)
                value = Environment(journal, host.anchor, host.slots, record, host.clock)
            finally:
                host.release_writer()
                await host.backend.__aexit__()
        yield value


@pytest.fixture
def current_controller(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Only process identity is fake; retained package/anchor hashes stay real."""
    verified: list[str] = []

    def verify(slot: SlotIdentity) -> None:
        verified.append(slot.digest)

    monkeypatch.setattr(host_module, "verify_current_slot", verify)
    monkeypatch.setattr(host_module, "verify_current_process", verify)
    return verified


class Probe(ManagedSystemdProbe):
    def __init__(self) -> None:
        super().__init__()
        self.helper = HelperProbe()
        self.after_read: Callable[[], None] = lambda: None

    async def helper_exists(self, name: str) -> bool:
        return await self.helper.helper_exists(name)

    async def read_helper(
        self, name: str, argv: tuple[str, ...], limits: HelperLimits
    ) -> HelperSnapshot:
        result = await self.helper.read_helper(name, argv, limits)
        self.after_read()
        return result


class Backend(FakeManagedService):
    instances: list[Backend] = []
    rollback = False
    interrupt_at = ""
    fail_at = ""
    missing_hint = False

    def __init__(
        self,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
        journal: TransactionJournal,
        **kwargs: Any,
    ) -> None:
        record = journal.read_current()
        assert isinstance(record, TransactionRecordV2)
        assert controller.digest == anchor.controller_slot
        assert slots["target"].digest == record.target.installation
        super().__init__(record, read_snapshots(journal, record, "base"))
        self.healthy_target = not self.rollback
        self.interrupt, self.fail = self.interrupt_at, self.fail_at
        self.closed = False
        self.hints = 0
        self.instances.append(self)

    async def __aenter__(self) -> Backend:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def observe(self, record: TransactionRecordV2) -> ManagedServiceObservation:
        if record.resolved:
            self.hints += 1
            if self.missing_hint:
                raise TimeoutError
        return await super().observe(record)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, current_controller: list[str]) -> type[Backend]:
    class Effects(Backend):
        instances: list[Backend] = []

    monkeypatch.setattr(host_module, "ManagedLinuxBackend", Effects)
    return Effects


async def test_ensure_launches_only_fixed_controller_with_resource_envelope(
    environment: Environment, filesystems: list[tuple[Path, ...]]
) -> None:
    probe, runner = Probe(), Runner()
    with environment.journal.locked() as control:
        assert (
            await ensure_managed_helper(control, environment.anchor, probe=probe, runner=runner)
            == "accepted"
        )
        assert control.read_current() == environment.record
    assert len(runner.calls) == 1 and not probe.helper.reads
    argv = runner.calls[0]
    assert tuple(argv[argv.index("--") + 1 :]) == managed_helper_arguments(
        environment.anchor, environment.slots["base"]
    )
    assert argv[0] == "systemd-run" and "--expand-environment=no" in argv
    assert "--unit=" + managed_helper_unit_name(environment.record) in argv
    assert not {"--collect", "--scope", "--wait"}.intersection(argv)
    properties = dict(item[11:].split("=", 1) for item in argv if item.startswith("--property="))
    limits = environment.anchor.helper
    assert properties["MemoryMax"] == str(limits.memory_bytes)
    assert properties["MemorySwapMax"] == "0" and properties["CPUQuota"] == f"{limits.cpu_percent}%"
    assert properties["TasksMax"] == str(limits.tasks) and properties["Restart"] == "on-failure"
    assert properties["StartLimitBurst"] == str(limits.restart_burst)
    assert (
        properties["RuntimeMaxSec"]
        == properties["StartLimitIntervalSec"]
        == f"{limits.seconds:.6f}s"
    )
    assert properties["TimeoutStopSec"] == f"{limits.stop_seconds:.6f}s"
    assert properties["UMask"] == "0077" and properties["OOMPolicy"] == "kill"
    assert filesystems and all(
        paths
        == (
            environment.journal.directory.path,
            Path(environment.anchor.config_path).parent,
            environment.journal.database.parent,
            environment.journal.writer_directory.path,
        )
        for paths in filesystems
    )


async def test_existing_valid_helper_is_reused_without_reset_or_second_name(
    environment: Environment,
) -> None:
    probe, runner = Probe(), Runner()
    probe.helper.exists = True
    with environment.journal.locked() as control:
        for _ in range(2):
            assert (
                await ensure_managed_helper(control, environment.anchor, probe=probe, runner=runner)
                == "running"
            )
    assert len(probe.helper.reads) == 2 and not runner.calls
    assert set(probe.helper.names) == {managed_helper_unit_name(environment.record)}


@pytest.mark.parametrize("error", ["listing", "wrong_argv", "helper_kernel_limits"])
async def test_existing_or_unreadable_helper_never_falls_back(
    environment: Environment, error: str
) -> None:
    probe, runner = Probe(), Runner()
    probe.helper.exists, probe.helper.error = True, error
    with environment.journal.locked() as control, pytest.raises(ValueError):
        await ensure_managed_helper(control, environment.anchor, probe=probe, runner=runner)
    assert not runner.calls and environment.journal.read_current() == environment.record


async def test_create_conflict_is_failure_without_an_alternate_unit(
    environment: Environment,
) -> None:
    runner = Runner()
    runner.exit_code = 1
    with (
        environment.journal.locked() as control,
        pytest.raises(TransactionError, match="^helper_start_failed$"),
    ):
        await ensure_managed_helper(control, environment.anchor, probe=Probe(), runner=runner)
    assert len(runner.calls) == 1 and "reset-failed" not in runner.calls[0]


async def test_changed_record_after_helper_listing_cannot_spawn(environment: Environment) -> None:
    probe, runner = Probe(), Runner()
    with environment.journal.locked() as control:
        probe.helper.after_exists = lambda: advance_managed(
            control, environment.record.position, TransitionV2(phase="draining")
        )
        with pytest.raises(TransactionError, match="transaction_changed"):
            await ensure_managed_helper(control, environment.anchor, probe=probe, runner=runner)
    assert not runner.calls


@pytest.mark.parametrize("changed", ["protocol", "budget"])
async def test_wrong_protocol_or_persisted_budget_cannot_launch(
    environment: Environment, current_controller: list[str], changed: str
) -> None:
    path = environment.journal.directory.path / "state.json"
    data = environment.record.model_dump(mode="json")
    if changed == "protocol":
        data["protocol"] = 1
    else:
        data["deadline"]["seconds"] += 1.0
    path.write_bytes(json.dumps(data).encode())
    runner = Runner()
    with pytest.raises(ValueError):
        await recover(environment.journal.directory.path, probe=Probe(), runner=runner)
    assert not runner.calls


def test_foreign_anchor_paths_are_rejected_before_any_helper_query(
    environment: Environment,
) -> None:
    anchor = environment.anchor.model_copy(
        update={"database_path": str(environment.journal.database.parent / "other.sqlite3")}
    )
    with (
        environment.journal.locked() as control,
        pytest.raises(TransactionError, match="managed_paths_changed"),
    ):
        host_module._record(control, anchor)


async def test_recover_never_constructs_effect_backend_and_ignores_broken_target_selector(
    environment: Environment, backend: type[Backend]
) -> None:
    root = environment.journal.directory.path
    (root / "selector.json").write_bytes(b"invalid selector")
    Path(environment.slots["target"].package.root, "__main__.py").write_text(
        "damaged target", encoding="utf-8"
    )
    runner = Runner()
    assert await recover(root, probe=Probe(), runner=runner) == "accepted"
    assert len(runner.calls) == 1 and not backend.instances
    assert environment.journal.read_current() == environment.record


async def test_foreign_current_interpreter_cannot_run_recovery(environment: Environment) -> None:
    runner = Runner()
    with pytest.raises(ManagedDeploymentError, match="managed_process_mismatch"):
        await recover(environment.journal.directory.path, probe=Probe(), runner=runner)
    assert not runner.calls


async def test_unverified_filesystem_cannot_launch_or_construct_backend(
    environment: Environment, backend: type[Backend], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(directories: Sequence[Path]) -> None:
        raise FilesystemError("filesystem_unsupported")

    monkeypatch.setattr(host_module, "verify_local_filesystems", failed)
    runner = Runner()
    with pytest.raises(FilesystemError, match="filesystem_unsupported"):
        await recover(environment.journal.directory.path, probe=Probe(), runner=runner)
    with pytest.raises(FilesystemError, match="filesystem_unsupported"):
        await run_managed_helper(
            environment.journal.directory.path, probe=Probe(), clock=environment.clock
        )
    assert not runner.calls and not backend.instances


@pytest.mark.parametrize("error", ["pid", "helper_kernel_limits"])
async def test_run_authenticates_helper_pid_and_limits_before_backend(
    environment: Environment, backend: type[Backend], error: str
) -> None:
    probe = Probe()
    if error == "pid":
        probe.helper.pid = os.getpid() + 1
    else:
        probe.helper.error = error
    with pytest.raises(ValueError):
        await run_managed_helper(
            environment.journal.directory.path, probe=probe, clock=environment.clock
        )
    assert not backend.instances and environment.journal.read_current() == environment.record


@pytest.mark.parametrize("rollback", [False, True])
async def test_authenticated_helper_drives_one_commit_or_rollback_without_renewing_deadline(
    environment: Environment, backend: type[Backend], current_controller: list[str], rollback: bool
) -> None:
    backend.rollback = rollback
    probe = Probe()
    final = await run_managed_helper(
        environment.journal.directory.path, probe=probe, clock=environment.clock
    )
    assert final.resolved and final.phase == ("rolled_back" if rollback else "committed")
    assert final.deadline == environment.record.deadline
    effects = backend.instances[-1]
    assert effects.closed and effects.calls.count("start:target") == 1
    assert effects.calls.count("start:base") == int(rollback)
    assert len(probe.helper.reads) > 2 and len(current_controller) == 1 + 2 * len(
        probe.helper.reads
    )
    assert effects.hints == 1


async def test_controller_changed_during_helper_await_cannot_enter_backend(
    environment: Environment, backend: type[Backend]
) -> None:
    probe = Probe()
    probe.after_read = lambda: Path(
        environment.slots["base"].package.root, "__main__.py"
    ).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        await run_managed_helper(
            environment.journal.directory.path, probe=probe, clock=environment.clock
        )
    assert not backend.instances and environment.journal.read_current() == environment.record


@pytest.fixture
def authentic_current_process(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> SlotIdentity:
    """Match only this process's names; all retained file checks remain real."""
    controller = environment.slots["base"]
    monkeypatch.setattr(sys, "executable", controller.lexical_python)
    monkeypatch.setattr(sys, "prefix", controller.prefix)
    monkeypatch.setattr(muli, "__file__", str(Path(controller.package.root) / "__init__.py"))
    return controller


async def test_real_host_authentication_hashes_controller_once_on_each_side_of_helper_await(
    environment: Environment,
    authentic_current_process: SlotIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    original_files = deployment_module.verify_slot_files
    original_process = deployment_module.verify_current_process

    def files(
        slot: SlotIdentity,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        assert slot == authentic_current_process
        events.append("files")
        original_files(
            slot,
            verified_python=verified_python,
            verified_packages=verified_packages,
            observed_packages=observed_packages,
        )

    def process(slot: SlotIdentity) -> None:
        events.append("process")
        original_process(slot)

    class ReachedBackend(Exception):
        pass

    def reached(*args: object, **kwargs: object) -> None:
        events.append("backend")
        raise ReachedBackend

    monkeypatch.setattr(deployment_module, "verify_slot_files", files)
    monkeypatch.setattr(deployment_module, "verify_current_process", process)
    monkeypatch.setattr(host_module, "verify_current_process", process)
    monkeypatch.setattr(host_module, "ManagedLinuxBackend", reached)
    probe = Probe()
    probe.after_read = lambda: events.append("helper_returned")

    with pytest.raises(ReachedBackend):
        await run_managed_helper(
            environment.journal.directory.path, probe=probe, clock=environment.clock
        )

    # Entry and initial _record retain their original three full file checks.
    # Authentication then requires exactly one complete check before the RPC
    # and another after it, with exact current-process validation on both sides.
    assert events == [
        "files",
        "files",
        "process",
        "files",
        "files",
        "process",
        "helper_returned",
        "files",
        "process",
        "backend",
    ]
    assert len(probe.helper.reads) == 1
    assert environment.journal.read_current() == environment.record


@pytest.mark.parametrize(
    "changed", ["package", "python", "interpreter", "prefix", "module", "revision", "cancel"]
)
async def test_real_host_rechecks_files_process_and_record_after_helper_await(
    environment: Environment,
    authentic_current_process: SlotIdentity,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    controls: list[JournalControl] = []
    entered: list[bool] = []
    original_record = host_module._record

    def record(
        control: JournalControl, anchor: DeploymentAnchor
    ) -> tuple[TransactionRecordV2, SlotIdentity, dict[Role, SlotIdentity]]:
        controls.append(control)
        return original_record(control, anchor)

    def reached(*args: object, **kwargs: object) -> None:
        entered.append(True)
        raise AssertionError("unverified helper reached backend")

    def drift() -> None:
        controller = authentic_current_process
        if changed == "package":
            Path(controller.package.root, "__main__.py").write_text("changed", encoding="utf-8")
        elif changed == "python":
            replacement = environment.journal.directory.path.parent / "changed-python"
            replacement.write_bytes(b"changed interpreter")
            replacement.chmod(0o700)
            lexical = Path(controller.lexical_python)
            lexical.unlink()
            lexical.symlink_to(replacement)
        elif changed == "interpreter":
            monkeypatch.setattr(sys, "executable", environment.slots["target"].lexical_python)
        elif changed == "prefix":
            monkeypatch.setattr(sys, "prefix", environment.slots["target"].prefix)
        elif changed == "module":
            monkeypatch.setattr(
                muli,
                "__file__",
                str(Path(environment.slots["target"].package.root) / "__init__.py"),
            )
        elif changed == "revision":
            advance_managed(
                controls[-1], environment.record.position, TransitionV2(phase="draining")
            )
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr(host_module, "_record", record)
    monkeypatch.setattr(host_module, "ManagedLinuxBackend", reached)
    probe = Probe()
    probe.after_read = drift
    expected = asyncio.CancelledError if changed == "cancel" else ValueError
    with pytest.raises(expected):
        await run_managed_helper(
            environment.journal.directory.path, probe=probe, clock=environment.clock
        )
    assert len(probe.helper.reads) == 1 and not entered
    current = environment.journal.read_current()
    assert isinstance(current, TransactionRecordV2)
    assert current.phase == ("draining" if changed == "revision" else environment.record.phase)


async def test_cancel_preserves_start_intent_without_finally_retry(
    environment: Environment, backend: type[Backend]
) -> None:
    backend.interrupt_at = "after_start:target"
    with pytest.raises(asyncio.CancelledError):
        await run_managed_helper(
            environment.journal.directory.path, probe=Probe(), clock=environment.clock
        )
    current = environment.journal.read_current()
    assert (
        isinstance(current, TransactionRecordV2)
        and current.intent == "start_target"
        and not current.resolved
    )
    effects = backend.instances[-1]
    assert effects.closed and effects.calls.count("start:target") == 1


async def test_expired_transaction_cannot_execute_application_effects(
    environment: Environment, backend: type[Backend]
) -> None:
    environment.clock.elapsed = 181.0
    final = await run_managed_helper(
        environment.journal.directory.path, probe=Probe(), clock=environment.clock
    )
    assert final.phase == "recovery_required" and final.code == "deadline_exceeded"
    assert final.deadline == environment.record.deadline
    assert backend.instances[-1].closed and not backend.instances[-1].calls


async def test_blocked_activation_is_not_automatically_retried(
    environment: Environment, backend: type[Backend]
) -> None:
    backend.fail_at = "after_activate"
    final = await run_managed_helper(
        environment.journal.directory.path, probe=Probe(), clock=environment.clock
    )
    assert final.phase == "committed" and final.activation == "blocked" and not final.resolved
    assert backend.instances[-1].calls.count("activate:target") == 1


async def test_missing_final_hint_cannot_undo_release_or_repeat_activation(
    environment: Environment, backend: type[Backend]
) -> None:
    backend.missing_hint = True
    root, probe = environment.journal.directory.path, Probe()
    final = await run_managed_helper(root, probe=probe, clock=environment.clock)
    again = await run_managed_helper(root, probe=probe, clock=environment.clock)
    assert final.resolved and again == final
    assert sum(item.calls.count("activate:target") for item in backend.instances) == 1
    assert all(item.hints == 1 and item.closed for item in backend.instances)
    with (
        environment.journal.locked() as control,
        pytest.raises(TransactionError, match="transaction_not_pending"),
    ):
        await ensure_managed_helper(control, environment.anchor, probe=probe, runner=Runner())


async def test_cancelled_launch_is_not_retried_or_reported_running(
    environment: Environment,
) -> None:
    runner = Runner()
    runner.finish = asyncio.Event()
    with environment.journal.locked() as control:
        task = asyncio.create_task(
            ensure_managed_helper(control, environment.anchor, probe=Probe(), runner=runner)
        )
        await asyncio.wait_for(runner.entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(runner.calls) == 1 and environment.journal.read_current() == environment.record


def test_identity_includes_protocol_deployment_and_generation(environment: Environment) -> None:
    name = managed_helper_unit_name(environment.record)
    assert len(name.removeprefix("muli-transaction-").removesuffix(".service")) == 64
    assert name != managed_helper_unit_name(
        environment.record.model_copy(update={"generation": "a" * 32})
    )
    foreign = environment.record.identity.model_copy(update={"deployment": "d" * 64})
    assert name != managed_helper_unit_name(
        environment.record.model_copy(update={"identity": foreign})
    )
    controller = environment.slots["base"]
    argv = managed_helper_arguments(environment.anchor, controller)
    assert argv[0] == controller.lexical_python != controller.resolved_python.path
    assert argv[1:4] == ("-I", "-m", "muli.managed_host")
    assert argv[-3:] == ("--root", str(environment.journal.directory.path), "run")


async def test_missing_root_is_not_created(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    with pytest.raises((ValueError, OSError)):
        await recover(root, probe=Probe(), runner=Runner())
    assert not root.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--roo", "/private", "recover"],
        ["--root", "/private", "run", "secret"],
        ["--root=/private", "recover"],
    ],
)
def test_cli_exact_arguments_only(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert host_module.main(arguments) == 2
    assert capsys.readouterr().err == "managed_host_invalid_arguments\n"


def test_cli_errors_hide_raw_configuration_and_nonce(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failed(root: Path) -> str:
        raise RuntimeError("secret-configuration-nonce")

    monkeypatch.setattr(host_module, "recover", failed)
    assert host_module.main(["--root", "/private", "recover"]) == 1
    assert capsys.readouterr().err == "managed_host_failed\n"
