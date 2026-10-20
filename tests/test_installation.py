from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import muli
from muli.management_transaction import (
    ClockReading,
    Deadline,
    JournalControl,
    ProjectionReceipt,
    ServiceIdentity,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
    Transition,
)
from muli.platforms.installation import (
    HelperLimits,
    InstallationError,
    InstallationManifest,
    PackageLimits,
)
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux anchored installation files")


@dataclass(slots=True)
class Installation:
    service: UserService
    database: Path
    root: Path
    package: Path
    executable: Path
    real: Path
    venv: Path
    base: Path

    def capture(
        self, *, package_limits: PackageLimits | None = None, require_database: bool = True
    ) -> InstallationManifest:
        return InstallationManifest.capture(
            self.service,
            self.database,
            package_limits=package_limits,
            require_database=require_database,
        )


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Installation:
    base = tmp_path / "base"
    real = base / "bin/python"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"not executed")
    real.chmod(0o700)
    venv = tmp_path / "venv"
    executable = venv / "bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(real)
    (venv / "pyvenv.cfg").write_bytes(b"home = base\n")
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    package = tmp_path / "installed/muli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"init\n")
    (package / "main.py").write_bytes(b"body\n")
    monkeypatch.setattr(muli, "__file__", str(package / "__init__.py"))
    config = tmp_path / "configuration/settings.json"
    config.parent.mkdir()
    config.write_bytes(b'{"secret":"configuration-must-not-enter-manifest"}')
    data = tmp_path / "data"
    data.mkdir()
    database = data / "muli.db"
    database.write_bytes(b"database-body-must-not-enter-manifest")
    (data / "muli.db-wal").write_bytes(b"wal-must-not-be-read")
    service = UserService(
        config, data, tmp_path / "units", platform="linux", executable=str(executable)
    )
    service.path.parent.mkdir()
    service.path.write_text(service.render())
    return Installation(
        service, database, tmp_path / "journal", package, executable, real, venv, base
    )


def prepare(control: JournalControl, manifest: InstallationManifest) -> TransactionRecord:
    with control.journal.writer_lease() as writer:
        return control.prepare(
            request_id="test-request",
            action="restart",
            identity=ServiceIdentity(
                installation=manifest.digest,
                service=manifest.service_digest,
                store_instance="store-one",
                main_session="main-one",
                origin_boot="boot-one",
            ),
            base=b"{}",
            target=b"{}",
            deadline=Deadline(
                created=ClockReading(boot_id="kernel-one", wall=1.0, monotonic=1.0),
                seconds=180.0,
                rollback_reserve=60.0,
            ),
            writer=writer,
        )


def test_capture_keeps_venv_entry_and_separate_binary_identity(installation: Installation) -> None:
    manifest = installation.capture()
    manifest.verify()
    assert manifest.interpreter.executable == str(installation.executable)
    assert manifest.interpreter.real_executable == str(installation.real)
    assert manifest.interpreter.prefix == str(installation.venv)
    assert manifest.interpreter.pyvenv is not None
    assert manifest.user_service()._arguments() == installation.service._arguments()
    assert manifest.user_service().render() == installation.service.render()
    assert manifest.user_service()._arguments()[0] != manifest.interpreter.real_executable
    assert b"configuration-must-not-enter-manifest" not in manifest.canonical()
    assert b"database-body-must-not-enter-manifest" not in manifest.canonical()
    assert b"wal-must-not-be-read" not in manifest.canonical()
    assert manifest.helper.operation_timeout == 20.0


def test_foreign_venv_sharing_binary_is_not_current_environment(
    installation: Installation, tmp_path: Path
) -> None:
    other = tmp_path / "other-venv"
    (other / "bin").mkdir(parents=True)
    (other / "bin/python").symlink_to(installation.real)
    (other / "pyvenv.cfg").write_bytes(b"different environment")
    installation.service.executable = str(other / "bin/python")
    installation.service.path.write_text(installation.service.render())
    with pytest.raises(InstallationError, match="foreign_interpreter_entry"):
        installation.capture()


@pytest.mark.parametrize("change", ["binary", "pyvenv", "package", "unit", "prefix", "import"])
def test_verify_recomputes_installation(
    installation: Installation, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    manifest = installation.capture()
    if change == "binary":
        installation.real.write_bytes(b"replaced binary")
    elif change == "pyvenv":
        (installation.venv / "pyvenv.cfg").write_bytes(b"different venv")
    elif change == "package":
        (installation.package / "main.py").write_bytes(b"new source")
    elif change == "unit":
        installation.service.path.write_text("[Service]\nExecStart=/other/python\n")
    elif change == "prefix":
        monkeypatch.setattr(sys, "prefix", str(installation.base))
    else:
        other = installation.package.with_name("other")
        other.mkdir()
        (other / "__init__.py").write_bytes(b"other")
        monkeypatch.setattr(muli, "__file__", str(other / "__init__.py"))
    with pytest.raises((InstallationError, OSError)):
        manifest.verify()


def test_mutable_configuration_database_and_python_cache_are_not_code_identity(
    installation: Installation,
) -> None:
    manifest = installation.capture()
    installation.service.config_path.write_bytes(b"new configuration")
    installation.database.write_bytes(b"SQLite changes during normal operation")
    cache = installation.package / "__pycache__"
    cache.mkdir()
    (cache / "unread.pyc").write_bytes(b"byte cache")
    (installation.package / "unused.pyc").write_bytes(b"also ignored")
    manifest.verify()
    assert installation.capture().digest == manifest.digest


@pytest.mark.parametrize("name", ["config", "database", "unit", "package"])
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_special_files_are_refused_without_following_or_blocking(
    installation: Installation, name: str, kind: str
) -> None:
    manifest = installation.capture()
    path = {
        "config": installation.service.config_path,
        "database": installation.database,
        "unit": installation.service.path,
        "package": installation.package / "main.py",
    }[name]
    path.unlink()
    if kind == "symlink":
        path.symlink_to(installation.real)
    else:
        os.mkfifo(path)
    with pytest.raises((InstallationError, OSError)):
        manifest.verify(require_database=False)


@pytest.mark.parametrize("name", ["configuration", "data", "units", "installed"])
def test_parent_swap_to_link_is_not_accepted(installation: Installation, name: str) -> None:
    manifest = installation.capture()
    parent = installation.root.parent / name
    moved = parent.with_name(name + "-moved")
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises((InstallationError, OSError)):
        manifest.verify()


def test_parent_swap_during_package_read_stays_anchored_and_is_rejected(
    installation: Installation, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = installation.capture()
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "main.py"
    external.write_bytes(b"outside must not be read")
    inode = external.stat().st_ino
    original = os.open
    changed = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal changed
        gate = path == "main.py" and dir_fd is not None and not changed
        if gate:
            changed = True
            installation.package.rename(installation.package.with_name("muli-moved"))
            installation.package.symlink_to(outside, target_is_directory=True)
        descriptor = original(path, flags, mode, dir_fd=dir_fd)
        if gate:
            assert os.fstat(descriptor).st_ino != inode
        return descriptor

    monkeypatch.setattr(os, "open", swapping_open)
    with pytest.raises((OSError, InstallationError)):
        manifest.verify()
    assert changed


def test_initial_boot_can_explicitly_allow_only_missing_database(
    installation: Installation,
) -> None:
    manifest = installation.capture()
    installation.database.unlink()
    with pytest.raises(FileNotFoundError):
        installation.capture()
    assert installation.capture(require_database=False).digest == manifest.digest
    manifest.verify(require_database=False)
    with pytest.raises(FileNotFoundError):
        manifest.verify()
    installation.database.parent.rename(installation.database.parent.with_name("data-moved"))
    with pytest.raises(FileNotFoundError):
        manifest.verify(require_database=False)


@pytest.mark.parametrize("budget", ["entries", "total", "file", "depth"])
def test_package_budgets_bound_traversal_and_bytes(installation: Installation, budget: str) -> None:
    values: dict[str, int] = {}
    if budget == "entries":
        values["entries"] = 1
    elif budget == "total":
        values["total_bytes"] = 4
    elif budget == "file":
        values["file_bytes"] = 32
        (installation.package / "main.py").write_bytes(b"x" * 33)
    else:
        values["depth"] = 1
        (installation.package / "nested/deeper").mkdir(parents=True)
    with pytest.raises(InstallationError, match="budget"):
        installation.capture(package_limits=PackageLimits(**values))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seconds", float("nan")),
        ("seconds", float("inf")),
        ("seconds", "180"),
        ("seconds", True),
        ("operation_timeout", 31.0),
        ("poll_interval", 0.0),
        ("memory_bytes", 1024),
        ("cpu_percent", 101),
        ("tasks", 65),
        ("stop_seconds", 6.0),
        ("restart_burst", 4),
        ("restart_delay", 0.0),
        ("rollback_reserve", 180.0),
    ],
)
def test_helper_limits_are_strict_and_finite(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        HelperLimits(**{field: value})


def test_load_does_not_create_root_or_parents(installation: Installation) -> None:
    path = installation.root / "absent"
    with pytest.raises(FileNotFoundError):
        InstallationManifest.load(path)
    assert not installation.root.exists()


def test_manifest_round_trip_and_service_digest_are_frozen(installation: Installation) -> None:
    manifest = installation.capture()
    with TransactionJournal(installation.root, installation.database) as journal:
        with journal.locked() as control:
            manifest.publish(control)
        loaded = InstallationManifest.load(installation.root)
        assert loaded == manifest
        assert stat.S_IMODE((installation.root / "installation.json").stat().st_mode) == 0o600
    installation.service.config_path.unlink()
    installation.service.config_path.symlink_to(installation.real)
    assert loaded.digest == manifest.digest
    assert loaded.service_digest == manifest.service_digest
    with pytest.raises(OSError):
        loaded.verify()


@pytest.mark.parametrize("bad", ["duplicate", "constant", "unknown", "relative", "too_large"])
def test_load_rejects_invalid_private_json(installation: Installation, bad: str) -> None:
    manifest = installation.capture()
    installation.root.mkdir(mode=0o700)
    value = manifest.model_dump(mode="json")
    if bad == "duplicate":
        data = b'{"protocol":1,' + manifest.canonical()[1:]
    elif bad == "constant":
        data = manifest.canonical().replace(b'"seconds":180.0', b'"seconds":NaN')
    elif bad == "unknown":
        value["nonce"] = "must not exist"
        data = json.dumps(value).encode()
    elif bad == "relative":
        value["config_path"] = "relative.json"
        data = json.dumps(value).encode()
    else:
        data = b" " * (131_072 + 1)
    path = installation.root / "installation.json"
    path.write_bytes(data)
    path.chmod(0o600)
    with pytest.raises((InstallationError, TransactionError)):
        InstallationManifest.load(installation.root)


def test_pending_or_unprojected_generation_blocks_manifest_replacement(
    installation: Installation,
) -> None:
    manifest = installation.capture()
    other = manifest.model_copy(update={"helper": HelperLimits(cpu_percent=30)})
    with TransactionJournal(installation.root, installation.database) as journal:
        with journal.locked() as control:
            manifest.publish(control)
            record = prepare(control, manifest)
            manifest.publish(control)
            with pytest.raises(TransactionError, match="transaction_in_progress"):
                other.publish(control)
            record = control.advance(record.position, Transition(phase="aborted", code="cancelled"))
            with pytest.raises(TransactionError, match="projection_required"):
                other.publish(control)
            assert InstallationManifest.load(installation.root) == manifest
            wrong = ProjectionReceipt(**record.position.model_dump(), store_instance="other-store")
            with pytest.raises(TransactionError, match="projection_required"):
                other.publish(control, projected=wrong)
            receipt = ProjectionReceipt(**record.position.model_dump(), store_instance="store-one")
            other.publish(control, projected=receipt)
            assert InstallationManifest.load(installation.root) == other


def test_identical_manifest_does_not_ignore_foreign_record(installation: Installation) -> None:
    manifest = installation.capture()
    with TransactionJournal(installation.root, installation.database) as journal:
        with journal.locked() as control:
            manifest.publish(control)
            record = prepare(control, manifest)
            foreign = record.model_copy(
                update={"identity": record.identity.model_copy(update={"installation": "e" * 64})}
            )
            control._write(foreign)
            with pytest.raises(InstallationError, match="foreign_installation_record"):
                manifest.publish(control)


def test_published_successor_can_reenter_before_its_prepare(installation: Installation) -> None:
    manifest = installation.capture()
    successor = manifest.model_copy(update={"helper": HelperLimits(cpu_percent=30)})
    with TransactionJournal(installation.root, installation.database) as journal:
        with journal.locked() as control:
            manifest.publish(control)
            record = prepare(control, manifest)
            record = control.advance(record.position, Transition(phase="aborted", code="cancelled"))
            receipt = ProjectionReceipt(**record.position.model_dump(), store_instance="store-one")
            successor.publish(control, projected=receipt)
        # Simulate exit after the manifest rename/fsync, before new prepared.
        with journal.locked() as control:
            with pytest.raises(TransactionError, match="projection_required"):
                successor.publish(control)
            successor.publish(control, projected=receipt)
            assert InstallationManifest.load(installation.root) == successor


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["file", "directory"])
def test_manifest_fsync_failure_prevents_prepare_even_on_identical_adoption(
    installation: Installation, monkeypatch: pytest.MonkeyPatch, existing: bool, failure: str
) -> None:
    manifest = installation.capture()
    with TransactionJournal(installation.root, installation.database) as journal:
        with journal.locked() as control:
            if existing:
                manifest.publish(control)
            original = os.fsync
            calls = 0

            def failed_sync(descriptor: int) -> None:
                nonlocal calls
                calls += 1
                # control.read() confirms its directory before the manifest write.
                if (failure == "file" and stat.S_ISREG(os.fstat(descriptor).st_mode)) or (
                    failure == "directory" and calls >= 3
                ):
                    raise OSError("injected sync failure")
                original(descriptor)

            monkeypatch.setattr(os, "fsync", failed_sync)
            with pytest.raises(OSError, match="injected sync failure"):
                manifest.publish(control)
            with pytest.raises(TransactionError, match="journal_unconfirmed"):
                control.check()
            assert journal.read() is None
