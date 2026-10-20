from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, NoReturn

import pytest

from muli import __main__ as cli
from muli.management_transaction import TransactionJournal
from muli.platforms import linux_bootstrap
from muli.platforms.linux_bootstrap import open_boot_session
from muli.platforms.linux_transaction import ObservationError, ProbeLimits, ServiceSnapshot
from muli.platforms.managed_deployment import (
    SELECTOR_NAME,
    ManagedDeploymentError,
    require_unselected_without_record,
)
from muli.runtime import Runtime
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed state authority")


class UnavailableProbe:
    def __init__(self, *, limits: ProbeLimits | None = None) -> None:
        pass

    async def read(self, service: UserService) -> ServiceSnapshot:
        raise ObservationError("synthetic_service_unavailable")


def service_at(tmp_path: Path) -> UserService:
    return UserService(
        tmp_path / "config.json", tmp_path / "data", tmp_path / "units", platform="linux"
    )


def select_without_state(root: Path, kind: str) -> None:
    selector = root / SELECTOR_NAME
    if kind == "file":
        selector.write_bytes(b"not valid selector JSON")
    elif kind == "dangling":
        selector.symlink_to(root / "missing-selector")
    elif kind == "fifo":
        os.mkfifo(selector, 0o600)
    else:
        assert kind == "directory"
        selector.mkdir()


@pytest.mark.parametrize("kind", ["file", "dangling", "fifo", "directory"])
async def test_ordinary_serve_rejects_orphaned_selector_before_runtime_or_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    service = service_at(tmp_path)
    database = service.data_dir / "muli.sqlite3"
    root = service.data_dir / "management/transaction-v1"
    with TransactionJournal(root, database):
        pass
    select_without_state(root, kind)
    entered: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        entered.append("configuration/runtime/sqlite")
        raise AssertionError("an orphaned selector must reject before application construction")

    monkeypatch.setattr(cli, "UserService", lambda *args, **kwargs: service)
    monkeypatch.setattr(cli, "ConfigFile", forbidden)
    monkeypatch.setattr(Runtime, "__init__", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(linux_bootstrap, "SystemdProbe", UnavailableProbe)
    with pytest.raises(ManagedDeploymentError, match="managed_state_missing"):
        await cli._serve(argparse.Namespace(config=service.config_path, data_dir=service.data_dir))
    assert not entered
    assert not database.exists() and not service.config_path.exists()
    # Rejection closes the bootstrap owner; no unpublished connection owns this lock.
    with TransactionJournal(root, database) as journal, journal.writer_lease():
        pass


async def test_prepublished_slot_and_anchor_without_selector_keep_ordinary_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_at(tmp_path)
    database = service.data_dir / "muli.sqlite3"
    root = service.data_dir / "management/transaction-v1"
    with TransactionJournal(root, database) as journal:
        journal.directory.replace("slot-" + "1" * 64 + ".json", b"prepublished slot")
        journal.directory.replace("anchor-" + "2" * 64 + ".json", b"prepublished anchor")

    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("ordinary unsupported observation must not enter SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    session = await open_boot_session(service, database, probe=UnavailableProbe())
    try:
        assert session.permit is None
        assert session.unsupported_reason == "synthetic_service_unavailable"
        assert not database.exists() and not (root / "state.json").exists()
    finally:
        session.close_after_writers()


def test_absent_deployment_check_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    require_unselected_without_record(root)
    assert not root.exists()


def test_unreadable_selector_identity_cannot_allow_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        if path == tmp_path / SELECTOR_NAME:
            raise PermissionError("synthetic private path")
        return original(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ManagedDeploymentError, match="^managed_state_unavailable$"):
        require_unselected_without_record(tmp_path)
