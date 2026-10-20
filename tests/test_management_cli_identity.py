from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Never

import pytest
from test_installation import Installation, prepare
from test_installation import installation as installation

from muli.initialization import start_background
from muli.management_cli import guarded_service_action, management_action
from muli.management_transaction import (
    TransactionError,
    TransactionJournal,
    Transition,
)
from muli.platforms.installation import InstallationManifest
from muli.platforms.transaction_files import ConfigurationFile, ConfigurationSnapshot
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux fixed service identity")


@dataclass(slots=True)
class Installed:
    service: UserService
    manifest: InstallationManifest
    journal: TransactionJournal


@pytest.fixture
def installed(installation: Installation) -> Iterator[Installed]:
    service = installation.service
    database = service.data_dir / "muli.sqlite3"
    database.write_bytes(b"not opened as SQLite")
    manifest = InstallationManifest.capture(service, database)
    with TransactionJournal(service.data_dir / "management/transaction-v1", database) as journal:
        with journal.locked() as control:
            manifest.publish(control)
        yield Installed(service, manifest, journal)


@pytest.mark.parametrize("action", ["install", "start", "stop", "restart", "enable", "disable"])
async def test_other_data_root_cannot_control_the_same_installed_unit(
    installed: Installed, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    with installed.journal.locked() as control:
        prepare(control, installed.manifest)
    service = installed.service
    other = UserService(
        service.config_path,
        service.data_dir.with_name("unrelated-data"),
        service.path.parent,
        platform="linux",
        executable=service.executable,
    )

    async def unexpected(name: str) -> Never:
        raise AssertionError("an unrelated data root must not control the shared unit")

    monkeypatch.setattr(other, "action", unexpected)
    original = service.path.read_bytes()
    with pytest.raises(TransactionError, match="service_identity_"):
        await guarded_service_action(other, action)
    assert not other.data_dir.exists()
    assert service.path.read_bytes() == original
    assert installed.journal.read().phase == "prepared"


@pytest.mark.parametrize("action", ["install", "start", "stop", "restart", "enable", "disable"])
async def test_matching_unit_keeps_raw_action_inside_the_journal_lock(
    installed: Installed, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    calls: list[str] = []

    async def control(name: str) -> dict[str, Any]:
        with pytest.raises(TransactionError, match="lock_busy"):
            with installed.journal.locked():
                pass
        calls.append(name)
        return {"accepted": True}

    monkeypatch.setattr(installed.service, "action", control)
    result = await guarded_service_action(installed.service, action)
    assert result["accepted"] and calls == [action]


@pytest.mark.parametrize("phase", ["prepared", "recovery_required"])
async def test_unresolved_matching_unit_refuses_raw_actions(
    installed: Installed, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    with installed.journal.locked() as control:
        record = prepare(control, installed.manifest)
        if phase == "recovery_required":
            control.advance(
                record.position, Transition(phase="recovery_required", code="foreign_state")
            )

    async def unexpected(action: str) -> Never:
        raise AssertionError("an unresolved transaction must own service control")

    monkeypatch.setattr(installed.service, "action", unexpected)
    with pytest.raises(TransactionError, match="transaction_in_progress"):
        await guarded_service_action(installed.service, "restart")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversized", "custom"])
async def test_unverified_unit_never_reaches_service_control(
    installed: Installed, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = installed.service.path
    standard = path.read_bytes()
    path.unlink()
    if kind == "symlink":
        target = path.with_name("other.service")
        target.write_bytes(standard)
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversized":
        path.write_bytes(standard + b"extra")
    else:
        path.write_bytes(standard.replace(b"Restart=on-failure", b"Restart=no"))

    async def unexpected(action: str) -> Never:
        raise AssertionError("unit verification must finish before control")

    monkeypatch.setattr(installed.service, "action", unexpected)
    with pytest.raises(TransactionError, match="service_identity_"):
        await guarded_service_action(installed.service, "install")


async def test_unit_is_rechecked_after_the_authority_is_locked(
    installed: Installed, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = ConfigurationFile.read
    calls = 0

    def changed(source: ConfigurationFile) -> ConfigurationSnapshot:
        nonlocal calls
        snapshot = read(source)
        if source.path == installed.service.path:
            calls += 1
            if calls == 1:
                source.path.write_bytes(snapshot.data.replace(b"Restart=on-failure", b"Restart=no"))
        return snapshot

    async def unexpected(action: str) -> Never:
        raise AssertionError("a replaced unit must not receive service control")

    monkeypatch.setattr(ConfigurationFile, "read", changed)
    monkeypatch.setattr(installed.service, "action", unexpected)
    with pytest.raises(TransactionError, match="service_identity_changed"):
        await guarded_service_action(installed.service, "stop")
    assert calls == 2


@pytest.mark.parametrize("action", ["status", "recover"])
@pytest.mark.parametrize("field", ["config", "unit"])
async def test_management_reads_check_manifest_paths_before_empty_record_return(
    installed: Installed, action: Literal["status", "recover"], field: str
) -> None:
    original = installed.service
    other = UserService(
        original.config_path.with_name("other.json") if field == "config" else original.config_path,
        original.data_dir,
        original.path.parent / "other" if field == "unit" else original.path.parent,
        platform="linux",
        executable=original.executable,
    )
    with pytest.raises(TransactionError, match="boot_paths_changed"):
        await management_action(other, action)


@pytest.mark.parametrize("action", ["status", "recover"])
async def test_management_diagnostics_do_not_construct_config_or_store(
    installed: Installed,
    monkeypatch: pytest.MonkeyPatch,
    action: Literal["status", "recover"],
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> Never:
        raise AssertionError("diagnostics must not construct a configuration or database writer")

    monkeypatch.setattr("muli.config.ConfigFile.__init__", unexpected)
    monkeypatch.setattr("muli.store.Store.__init__", unexpected)
    assert await management_action(installed.service, action) == {"transaction": None}


async def test_initial_setup_uses_guard_and_keeps_another_installation_unchanged(
    installed: Installed, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = installed.service
    other = UserService(
        current.config_path.with_name("other.json"),
        current.data_dir.with_name("new-data"),
        current.path.parent,
        platform="linux",
        executable=current.executable,
    )

    async def unexpected(action: str) -> Never:
        raise AssertionError("init must not replace the shared unit of another installation")

    monkeypatch.setattr(other, "capability", lambda: {"supported": True})
    monkeypatch.setattr(other, "action", unexpected)
    original = current.path.read_bytes()
    result = await start_background(other)
    assert not result["accepted"] and result["completed"] == []
    assert str(result["reason"]).startswith("service_identity_")
    assert current.path.read_bytes() == original and not other.data_dir.exists()


async def test_first_install_has_an_explicit_missing_unit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = UserService(tmp_path / "config.json", tmp_path / "data", tmp_path / "units")
    calls: list[str] = []

    async def action(name: str) -> dict[str, Any]:
        calls.append(name)
        if name == "install":
            service.path.parent.mkdir(parents=True)
            service.path.write_text(service.render())
        return {"accepted": True}

    async def status() -> dict[str, Any]:
        return {"active": False}

    monkeypatch.setattr(service, "capability", lambda: {"supported": True})
    monkeypatch.setattr(service, "action", action)
    monkeypatch.setattr(service, "status", status)
    result = await start_background(service)
    assert result["accepted"] and calls == ["install", "enable", "start"]
    assert not result["status"]["active"]


async def test_raw_service_await_keeps_the_lock_until_the_manager_call_finishes(
    installed: Installed, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def action(name: str) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {"accepted": True}

    monkeypatch.setattr(installed.service, "action", action)
    task = asyncio.create_task(guarded_service_action(installed.service, "restart"))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        with pytest.raises(TransactionError, match="lock_busy"):
            with installed.journal.locked():
                pass
    finally:
        release.set()
        await task
    with installed.journal.locked() as control:
        assert control.read() is None
