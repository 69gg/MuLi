"""Temporary files, real CAS/journal/Case paths; all system effects stay fake."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_managed_backend as backend_fixture
from helpers import managed_native_unit as births
from helpers import managed_native_worker as worker
from test_linux_transaction import FakeBus
from test_managed_native_ci import (
    fixture as fixture,
)
from test_managed_native_ci import (
    forbid_real_effects as forbid_real_effects,
)
from test_managed_native_ci import (
    private_environment,
)
from test_managed_native_ci import (
    test_actual_case_reuses_bootstrap_directory_before_first_manager_command as drive_to_manager,
)

from muli.managed_host import managed_helper_arguments
from muli.management_transaction import TransactionJournal
from muli.platforms import linux_controller, linux_transaction
from muli.platforms.managed_deployment import SlotIdentity
from muli.platforms.managed_probe import _UNIT_PATH
from muli.platforms.transaction_files import (
    ConfigurationError,
    ConfigurationFile,
    ConfigurationSnapshot,
)
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux isolated fixture ownership")


def unit_path(root: Path) -> Path:
    return root / "home/.config/systemd/user/muli.service"


async def observed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: tuple[Any, Any]
) -> Path:
    """Load the actual prepare-generated hook with a genuine V2 helper argv."""
    native, _ = fixture
    native.prepare()
    hook = (native.volume / "_muli_native_ci_hook.py").read_text(encoding="utf-8")
    root = tmp_path / "owned-helper"
    root.mkdir(mode=0o700)
    unit_path(root).parent.mkdir(mode=0o700, parents=True)
    for path in (root / "home", root / "home/.config", root / "home/.config/systemd"):
        path.chmod(0o700)
    worker.write_json(
        root / "fixture.json",
        {
            "uid": os.geteuid(),
            "memory": worker.MEMORY,
            "tasks": worker.TASKS,
            "harness_memory": worker.HARNESS_MEMORY,
            "harness_tasks": worker.HARNESS_TASKS,
            "seconds": worker.CASE_SECONDS,
            "cleanup_seconds": worker.CLEANUP_SECONDS,
            "harness_seconds": worker.HARNESS_SECONDS,
            "http_bytes": worker.MAX_BYTES,
            "harness_unit": "muli-native-" + "a" * 32 + ".service",
        },
    )
    private_environment(monkeypatch, root, os.geteuid())
    original_slot = backend_fixture._slot

    def slot(directory: Path, name: str) -> SlotIdentity:
        return original_slot(directory, {"base": "a", "target": "b"}[name])

    monkeypatch.setattr(backend_fixture, "_slot", slot)
    monkeypatch.setitem(sys.modules, "managed_native_worker", worker)
    # Preserve the class binding at teardown. Profiling is independently tested;
    # this test exercises the generated birth observer rather than that observer.
    monkeypatch.setattr(ConfigurationFile, "replace", ConfigurationFile.replace)
    monkeypatch.setattr(worker, "install_helper_cost_hook", lambda: None)
    monkeypatch.setattr(worker, "install_helper_guard_cost_hook", lambda: None)
    with TransactionJournal(
        root / "data/management/transaction-v1", root / "data/muli.sqlite3"
    ) as journal:
        with journal.locked() as control:
            host = backend_fixture.Host(root, journal, control, "adopt", change_config=False)
            try:
                argv = managed_helper_arguments(host.anchor, host.slots["base"])
                service = UserService(root / "config.json", root / "data", executable=argv[0])
                assert service.path == unit_path(root)
                births.create(root, service.render().encode("utf-8"), limit=worker.MAX_BYTES)
                monkeypatch.setattr(sys, "prefix", str(root / "a"))
                monkeypatch.setattr(sys, "executable", argv[0])
                monkeypatch.setattr(sys, "orig_argv", list(argv))
                monkeypatch.setattr(sys, "argv", list(argv[3:]))
                before = ConfigurationFile.replace
                exec(compile(hook, "<fixture-unit-hook>", "exec"), {})
                assert ConfigurationFile.replace is not before
            finally:
                host.release_writer()
                await host.backend.__aexit__()
    return root


def cleanup_boundaries(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, list[tuple[str, ...]]]:
    """Keep real Case cleanup, journal, locks and filesystem deletion."""
    stopped = False
    commands: list[tuple[str, ...]] = []

    class Probe:
        def __init__(self, **kwargs: Any) -> None:
            assert "limits" in kwargs

        async def helper_exists(self, name: str) -> bool:
            assert name.startswith("muli-transaction-")
            return False

        async def _unit_path(self, name: str) -> str:
            assert name == "muli.service"
            return _UNIT_PATH

        async def _snapshot(self, path: str) -> SimpleNamespace:
            assert path == _UNIT_PATH
            return SimpleNamespace(
                fragment=str(unit_path(root)),
                pid=0 if stopped else 100,
                job=(0, "/"),
                cgroup="/fixture-app",
            )

    async def exists(probe: Any) -> bool:
        assert isinstance(probe, Probe)
        return True

    def membership(name: str, uid: int) -> Path:
        assert name == "muli-native-cleanup.service" and uid == os.geteuid()
        return root

    def empty(self: Any, group: str) -> bool:
        assert group == "/fixture-app" and stopped
        return True

    case = worker.Case(root, seconds=worker.CLEANUP_SECONDS)

    async def command(*argv: str) -> str:
        nonlocal stopped
        assert argv == ("systemctl", "--user", "stop", "muli.service")
        commands.append(argv)
        stopped = True
        return ""

    monkeypatch.setattr(linux_transaction, "SystemdProbe", Probe)
    monkeypatch.setattr(linux_controller.CgroupObservation, "empty", empty)
    monkeypatch.setattr(worker, "service_exists", exists)
    monkeypatch.setattr(worker, "membership", membership)
    monkeypatch.setattr(case, "command", command)
    return case, commands


async def test_actual_case_records_exclusive_unit_birth_before_manager_start(
    fixture: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await drive_to_manager(fixture, monkeypatch)
    native, _ = fixture
    with ConfigurationFile(unit_path(native.volume), worker.MAX_BYTES) as unit:
        snapshot = unit.read()
    receipt = json.loads((native.volume / ".muli-native-unit.json").read_text(encoding="utf-8"))
    assert receipt["file"] == list(snapshot.identity)
    assert receipt["digest"] == snapshot.digest
    assert receipt["parent"] == [
        unit_path(native.volume).parent.stat().st_dev,
        unit_path(native.volume).parent.stat().st_ino,
    ]


async def test_generated_helper_tracks_real_cas_then_case_cleanup_unlinks_only_current_birth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: tuple[Any, Any]
) -> None:
    root = await observed_root(tmp_path, monkeypatch, fixture)
    identities: list[tuple[int, int, int, int, int]] = []
    with TransactionJournal(
        root / "data/management/transaction-v1", root / "data/muli.sqlite3"
    ) as journal:
        with journal.locked() as control, journal.writer_lease() as lease:

            def guard() -> None:
                control.check()
                lease.check()

            with ConfigurationFile(unit_path(root), worker.MAX_BYTES) as unit:
                for suffix in (b"\n# first real CAS\n", b"\n# second real CAS\n"):
                    before = unit.read()
                    after = unit.replace(before.digest, before.data + suffix, guard=guard)
                    assert before.identity != after.identity
                    identities.append(after.identity)
                    receipt = json.loads(
                        (root / ".muli-native-unit.json").read_text(encoding="utf-8")
                    )
                    assert (
                        receipt["file"] == list(after.identity)
                        and receipt["digest"] == after.digest
                    )
    assert identities[0] != identities[1]
    foreign = unit_path(root).with_name("unrelated.service")
    foreign.write_bytes(b"foreign, not owned")
    case, commands = cleanup_boundaries(root, monkeypatch)
    await case.cleanup()
    assert commands == [("systemctl", "--user", "stop", "muli.service")]
    assert not unit_path(root).exists()
    assert foreign.read_bytes() == b"foreign, not owned"
    # Re-entering after a lost cleanup reply does not recreate/adopt the leaf.
    await case.cleanup()
    assert not unit_path(root).exists()


@pytest.mark.parametrize("fault", [None, "properties", "fragment", "cgroup", "pid", "job"])
async def test_actual_case_cleanup_preserves_birth_safety_after_stopped_unit_gc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: tuple[Any, Any],
    fault: str | None,
) -> None:
    """Real cleanup/birth/locks and typed probe; only OS effects use temporary replies."""
    root = await observed_root(tmp_path, monkeypatch, fixture)
    foreign = unit_path(root).with_name("unrelated.service")
    foreign.write_bytes(b"foreign, not owned")
    foreign_identity = foreign.stat().st_dev, foreign.stat().st_ino
    owned_data = unit_path(root).read_bytes()
    owned_identity = unit_path(root).stat().st_dev, unit_path(root).stat().st_ino
    birth_data = (root / ".muli-native-unit.json").read_bytes()
    service = UserService(
        root / "config.json", root / "data", unit_path(root).parent, platform="linux"
    )
    bus = FakeBus(service)
    bus.rows["ControlGroup"] = ("s", "/fixture-app")
    cgroup_root = tmp_path / "cgroups"
    group = cgroup_root / "fixture-app"
    group.mkdir(parents=True)
    events = group / "cgroup.events"
    events.write_bytes(b"populated 1\n")
    stopped = False
    commands: list[tuple[str, ...]] = []
    stopped_properties: list[str] = []
    original_probe = linux_transaction.SystemdProbe
    original_cgroup = linux_controller.CgroupObservation

    async def manager(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert 0 < timeout <= worker.HTTP_SECONDS
        assert limit == worker.MAX_BYTES and merge_stderr is False
        if "ListUnitsByPatterns" in argv:
            # Keep both actual listing parsers: no helper remains, but the app
            # is present before its one real Case.command stop boundary.
            name = argv[-1]
            assert name == "muli.service" or name.startswith("muli-transaction-")
            entries = (
                [[name, "fixture app", "loaded", "active", "running", "", _UNIT_PATH, 0, "", "/"]]
                if name == "muli.service"
                else []
            )
            return {
                "exit_code": 0,
                "output": json.dumps({"type": "a(ssssssouso)", "data": [entries]}),
            }
        if stopped and "GetUnit" in argv:
            assert argv[-1] == "muli.service"
            return {
                "exit_code": 1,
                "output": "",
                "stderr": "Call failed: Unit muli.service not loaded.\n",
                "truncated": False,
            }
        if "get-property" in argv:
            assert argv[8] == _UNIT_PATH
            if stopped:
                stopped_properties.append(argv[9].rsplit(".", 1)[-1])
                if fault == "properties":
                    return {"exit_code": 1, "output": "", "stderr": "", "truncated": False}
        return await bus(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)

    def probe(**kwargs: Any) -> linux_transaction.SystemdProbe:
        return original_probe(runner=manager, **kwargs)

    def cgroups(path: Path, limit: int) -> linux_controller.CgroupObservation:
        assert path == Path("/sys/fs/cgroup") and limit == worker.MAX_BYTES
        return original_cgroup(cgroup_root, limit)

    def membership(name: str, uid: int) -> Path:
        assert name == "muli-native-cleanup.service" and uid == os.geteuid()
        return root

    async def command(*argv: str) -> str:
        nonlocal stopped
        assert argv == ("systemctl", "--user", "stop", "muli.service")
        assert not stopped and unit_path(root).is_file()
        commands.append(argv)
        stopped = True
        events.write_bytes(b"populated 1\n" if fault == "cgroup" else b"populated 0\n")
        # The regular description can still be loaded by Properties; the
        # collected instance is no longer returned by Manager.GetUnit.
        bus.rows.update(
            ActiveState=("s", "inactive"),
            SubState=("s", "dead"),
            MainPID=("u", 0),
            Job=("(uo)", [0, "/"]),
            InvocationID=("ay", []),
        )
        if fault == "fragment":
            bus.rows["FragmentPath"] = ("s", str(foreign))
        elif fault == "pid":
            bus.rows.update(MainPID=("u", 100), InvocationID=("ay", list(range(16))))
        elif fault == "job":
            bus.rows.update(
                Job=("(uo)", [1, "/org/freedesktop/systemd1/job/1"]),
                InvocationID=("ay", list(range(16))),
            )
        return ""

    case = worker.Case(root, seconds=worker.CLEANUP_SECONDS)
    monkeypatch.setattr(linux_transaction, "SystemdProbe", probe)
    monkeypatch.setattr(linux_controller, "CgroupObservation", cgroups)
    monkeypatch.setattr(worker, "membership", membership)
    monkeypatch.setattr(case, "command", command)
    if fault is None:
        await case.cleanup()
        assert not os.path.lexists(unit_path(root))
    else:
        error: type[Exception] = (
            linux_transaction.ObservationError if fault == "properties" else worker.NativeError
        )
        code = (
            "systemd_observation_unavailable"
            if fault == "properties"
            else "native_cleanup_fragment"
            if fault == "fragment"
            else "native_service_not_empty"
        )
        with pytest.raises(error, match=code):
            await case.cleanup()
        assert unit_path(root).read_bytes() == owned_data
        assert (unit_path(root).stat().st_dev, unit_path(root).stat().st_ino) == owned_identity
    assert (root / ".muli-native-unit.json").read_bytes() == birth_data
    assert commands == [("systemctl", "--user", "stop", "muli.service")]
    assert stopped_properties == (["Unit"] if fault == "properties" else ["Unit", "Service"])
    assert not os.path.lexists(root / ".muli-native-unit.pending")
    assert (foreign.stat().st_dev, foreign.stat().st_ino) == foreign_identity
    assert foreign.read_bytes() == b"foreign, not owned"


async def test_failed_original_guard_never_starts_birth_or_file_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: tuple[Any, Any]
) -> None:
    root = await observed_root(tmp_path, monkeypatch, fixture)
    receipt = root / ".muli-native-unit.json"
    before_receipt = receipt.read_bytes()
    rejected = ValueError("original guard rejected")
    guard_calls = 0
    replacements: list[tuple[object, ...]] = []

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        raise rejected

    def forbidden_replace(*args: Any, **kwargs: Any) -> None:
        replacements.append(args)
        raise AssertionError("publication after rejected guard")

    with ConfigurationFile(unit_path(root), worker.MAX_BYTES) as unit:
        before = unit.read()
        with monkeypatch.context() as context:
            context.setattr(os, "replace", forbidden_replace)
            with pytest.raises(ValueError) as caught:
                unit.replace(before.digest, before.data + b"\n# rejected\n", guard=guard)
        assert caught.value is rejected
        assert unit.read() == before
    assert guard_calls == 1 and replacements == []
    assert receipt.read_bytes() == before_receipt
    assert not os.path.lexists(root / ".muli-native-unit.pending")


async def test_real_replace_prepares_birth_once_after_first_successful_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: tuple[Any, Any]
) -> None:
    root = await observed_root(tmp_path, monkeypatch, fixture)
    original_begin = births._begin
    begins: list[int] = []
    guard_calls = 0

    def begin(directory: Path, limit: int) -> ConfigurationSnapshot:
        assert directory == root and limit == worker.MAX_BYTES
        begins.append(guard_calls)
        return original_begin(directory, limit)

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        assert os.path.lexists(root / ".muli-native-unit.pending") is (guard_calls > 1)

    monkeypatch.setattr(births, "_begin", begin)
    with ConfigurationFile(unit_path(root), worker.MAX_BYTES) as unit:
        before = unit.read()
        installed = unit.replace(before.digest, before.data + b"\n# observed\n", guard=guard)
        assert installed == unit.read() and installed.identity != before.identity
    assert guard_calls == 2 and begins == [1]
    assert not os.path.lexists(root / ".muli-native-unit.pending")
    receipt = json.loads((root / ".muli-native-unit.json").read_text(encoding="utf-8"))
    assert receipt["file"] == list(installed.identity) and receipt["digest"] == installed.digest


@pytest.mark.parametrize("replacement", ["same_bytes", "symlink", "parent"])
async def test_case_cleanup_preserves_external_unit_or_directory_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: tuple[Any, Any], replacement: str
) -> None:
    root = await observed_root(tmp_path, monkeypatch, fixture)
    unit = unit_path(root)
    data = unit.read_bytes()
    if replacement == "parent":
        original = unit.parent.with_name("old-user")
        unit.parent.rename(original)
        unit.parent.mkdir(mode=0o700)
        unit.write_bytes(data)
        unit.chmod(0o600)
    else:
        original = unit.with_name("old-muli.service")
        unit.rename(original)
        if replacement == "symlink":
            unit.symlink_to(original)
        else:
            unit.write_bytes(data)
            unit.chmod(0o600)
            before_birth = (root / ".muli-native-unit.json").read_bytes()
            with TransactionJournal(
                root / "data/management/transaction-v1", root / "data/muli.sqlite3"
            ) as journal:
                with journal.locked() as control, journal.writer_lease() as lease:

                    def guard() -> None:
                        control.check()
                        lease.check()

                    with ConfigurationFile(unit, worker.MAX_BYTES) as file:
                        before = file.read()
                        installed = file.replace(
                            before.digest, data + b"\n# authorized CAS\n", guard=guard
                        )
                        data = installed.data
            assert (root / ".muli-native-unit.json").read_bytes() == before_birth
    case, commands = cleanup_boundaries(root, monkeypatch)
    with pytest.raises((ValueError, OSError)):
        await case.cleanup()
    assert commands == [("systemctl", "--user", "stop", "muli.service")]
    assert os.path.lexists(unit) and unit.read_bytes() == data
    assert original.exists()


@pytest.mark.parametrize("failure", ["publication", "receipt", "cancelled", "confirm"])
async def test_uncertain_replace_never_repairs_birth_or_changes_original_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: tuple[Any, Any], failure: str
) -> None:
    root = await observed_root(tmp_path, monkeypatch, fixture)
    receipt = root / ".muli-native-unit.json"
    before_receipt = receipt.read_bytes()
    with ConfigurationFile(unit_path(root), worker.MAX_BYTES) as unit:
        before = unit.read()
        if failure == "confirm":
            replacement = unit.path.with_name("outside.service")
            replacement.write_bytes(before.data)
            replacement.chmod(0o600)
            replacement.replace(unit.path)
            confirmed = unit.confirm(before.digest, guard=lambda: None)
            assert confirmed.identity != before.identity
        elif failure == "cancelled":

            def cancelled() -> None:
                raise asyncio.CancelledError

            with pytest.raises(asyncio.CancelledError):
                unit.replace(before.digest, before.data + b"\n# cancelled\n", guard=cancelled)
            assert unit.read() == before
        else:
            original_fsync = os.fsync
            root_syncs = 0

            def fsync(fd: int) -> None:
                nonlocal root_syncs
                directory = os.fstat(fd)
                identity = (directory.st_dev, directory.st_ino)
                root_identity = (root.stat().st_dev, root.stat().st_ino)
                if identity == root_identity:
                    root_syncs += 1
                expected = unit.path.parent.stat()
                if (
                    failure == "publication" and identity == (expected.st_dev, expected.st_ino)
                ) or (failure == "receipt" and identity == root_identity and root_syncs == 2):
                    raise OSError("injected directory sync failure")
                original_fsync(fd)

            with monkeypatch.context() as context:
                context.setattr(os, "fsync", fsync)
                if failure == "publication":
                    with pytest.raises(OSError, match="injected directory sync failure"):
                        unit.replace(
                            before.digest, before.data + b"\n# published\n", guard=lambda: None
                        )
                    with pytest.raises(ConfigurationError, match="configuration_unconfirmed"):
                        unit.read()
                else:
                    # Observe-only receipt failure must not replace the real CAS success.
                    installed = unit.replace(
                        before.digest, before.data + b"\n# published\n", guard=lambda: None
                    )
                    assert installed == unit.read() and installed.identity != before.identity
                    assert (
                        root_syncs == 2
                    )  # Failure is after the receipt rename, not before the CAS.
                    assert receipt.read_bytes() != before_receipt
    if failure != "receipt":
        assert receipt.read_bytes() == before_receipt
    assert os.path.lexists(root / ".muli-native-unit.pending") is (
        failure not in {"confirm", "cancelled"}
    )
    case, _ = cleanup_boundaries(root, monkeypatch)
    if failure == "cancelled":
        await case.cleanup()
        assert not unit_path(root).exists()
        return
    with pytest.raises((ValueError, OSError)):
        await case.cleanup()
    assert unit_path(root).exists()
