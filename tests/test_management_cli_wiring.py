from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import pytest
import uvicorn
from fastapi import FastAPI
from test_installation import Installation, prepare
from test_installation import installation as installation
from test_linux_bootstrap import FakeProbe

import muli.__main__ as cli
import muli.server as server
from muli.config import ConfigFile, Settings
from muli.management_transaction import JournalControl, TransactionJournal
from muli.platforms import linux_bootstrap
from muli.platforms.installation import InstallationManifest
from muli.platforms.linux_bootstrap import BootSession, open_boot_session
from muli.service import UserService
from muli.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux CLI writer ownership")


def arguments(
    monkeypatch: pytest.MonkeyPatch,
    installation: Installation,
    *command: str,
    config: Path | None = None,
    data: Path | None = None,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "muli",
            "--config",
            str(config or installation.service.config_path),
            "--data-dir",
            str(data or installation.service.data_dir),
            *command,
        ],
    )

    def service(config_path: Path, data_dir: Path) -> UserService:
        return UserService(
            config_path,
            data_dir,
            installation.service.path.parent,
            platform="linux",
            executable=str(installation.executable),
        )

    monkeypatch.setattr(cli, "UserService", service)


def forbid_application(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("diagnostic CLI must not construct ConfigFile or Store")

    monkeypatch.setattr(cli, "ConfigFile", forbidden)
    monkeypatch.setattr("muli.runtime.Store", forbidden)


def pending(installation: Installation) -> InstallationManifest:
    database = installation.service.data_dir / "muli.sqlite3"
    database.write_bytes(b"not SQLite: diagnostic commands must not open it")
    manifest = InstallationManifest.capture(installation.service, database)
    root = installation.service.data_dir / "management/transaction-v1"
    with TransactionJournal(root, database, manifest.journal) as journal:
        with journal.locked() as control:
            manifest.publish(control)
            prepare(control, manifest)
    return manifest


@pytest.mark.parametrize("action", ["status", "recover"])
def test_diagnostic_without_record_does_not_initialize_application_or_directories(
    installation: Installation,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    arguments(monkeypatch, installation, "management", action)
    forbid_application(monkeypatch)
    before = sorted(installation.service.data_dir.iterdir())
    cli.main()
    assert json.loads(capsys.readouterr().out) == {
        "supported": False,
        "reason": "managed_boot_required",
        "transaction": None,
    }
    assert sorted(installation.service.data_dir.iterdir()) == before
    assert not (installation.service.data_dir / "management").exists()


@pytest.mark.parametrize("action", ["status", "recover"])
def test_pending_diagnostic_uses_only_journal_and_explicit_recover_helper(
    installation: Installation,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    manifest = pending(installation)
    root = installation.service.data_dir / "management/transaction-v1"
    before = (root / "state.json").read_bytes()
    calls: list[str] = []

    async def ensure(control: JournalControl, published: InstallationManifest) -> str:
        assert published == manifest
        record = control.read()
        assert record is not None and record.phase == "prepared"
        calls.append(record.request_id)
        return "accepted"

    monkeypatch.setattr("muli.management_helper.ensure_helper", ensure)
    arguments(monkeypatch, installation, "management", action)
    forbid_application(monkeypatch)
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["transaction"]["phase"] == "prepared"
    assert "configuration-must-not-enter-manifest" not in json.dumps(result)
    assert calls == (["test-request"] if action == "recover" else [])
    assert result.get("helper") == ("accepted" if action == "recover" else None)
    assert (root / "state.json").read_bytes() == before


@pytest.mark.parametrize("action", ["status", "recover"])
def test_diagnostic_rejects_caller_config_mismatch_before_returning_transaction(
    installation: Installation,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    pending(installation)
    other = installation.service.config_path.with_name("other.json")
    arguments(monkeypatch, installation, "management", action, config=other)
    forbid_application(monkeypatch)
    with pytest.raises(SystemExit) as error:
        cli.main()
    output = capsys.readouterr()
    assert error.value.code == 2
    assert "boot_paths_changed" in output.err
    assert not output.out and not other.exists()


@pytest.mark.parametrize("action", ["status", "recover"])
def test_diagnostic_different_data_root_does_not_disclose_other_transaction(
    installation: Installation,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    pending(installation)
    other = installation.service.data_dir.with_name("other-data")
    arguments(monkeypatch, installation, "management", action, data=other)
    forbid_application(monkeypatch)
    cli.main()
    assert json.loads(capsys.readouterr().out)["transaction"] is None
    assert not other.exists()


@pytest.mark.parametrize("action", ["install", "start", "stop", "restart", "enable", "disable"])
def test_raw_service_cannot_cross_pending_transaction(
    installation: Installation,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    pending(installation)
    calls: list[str] = []

    async def raw(self: UserService, name: str) -> dict[str, Any]:
        calls.append(name)
        raise AssertionError("pending transaction must reject before raw service action")

    monkeypatch.setattr(UserService, "action", raw)
    arguments(monkeypatch, installation, "service", action)
    forbid_application(monkeypatch)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "transaction_in_progress" in capsys.readouterr().err
    assert not calls


@dataclass(slots=True)
class Serving:
    installation: Installation
    sessions: list[BootSession] = field(default_factory=list)
    stores: list[Store] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    loops: list[asyncio.AbstractEventLoop] = field(default_factory=list)

    def assert_closed(self) -> None:
        assert self.sessions and self.stores
        for store in self.stores:
            with pytest.raises(sqlite3.ProgrammingError):
                store.db.execute("SELECT 1")
        for session in self.sessions:
            assert session._closed
        root = self.installation.service.data_dir / "management/transaction-v1"
        with TransactionJournal(
            root, self.installation.service.data_dir / "muli.sqlite3"
        ) as journal:
            with journal.writer_lease() as lease:
                lease.check()


@pytest.fixture
def serving(installation: Installation, monkeypatch: pytest.MonkeyPatch) -> Iterator[Serving]:
    installation.service.config_path.write_text(
        Settings(
            updates={"check_on_start": False},
            compaction={"enabled": False},
            memory={"auto_extract": False},
            onboarding_pending=False,
        ).model_dump_json()
    )
    state = Serving(installation)
    original_init, original_close = Store.__init__, Store.close
    original_session_close = BootSession.close_after_writers

    async def bootstrap(service: UserService, database: Path) -> BootSession:
        state.events.append("bootstrap")
        state.loops.append(asyncio.get_running_loop())
        session = await open_boot_session(service, database, probe=FakeProbe(service))
        state.sessions.append(session)
        return session

    def config(path: Path) -> ConfigFile:
        assert state.sessions
        state.sessions[-1].lease.check()
        state.events.append("config")
        state.loops.append(asyncio.get_running_loop())
        return ConfigFile(path)

    def store_init(self: Store, *args: Any, **kwargs: Any) -> None:
        assert state.sessions
        state.sessions[-1].lease.check()
        original_init(self, *args, **kwargs)
        state.stores.append(self)

    def store_close(self: Store) -> None:
        state.sessions[-1].lease.check()
        state.events.append("store-close")
        original_close(self)

    def session_close(self: BootSession) -> None:
        if not self._closed:
            for store in state.stores:
                with pytest.raises(sqlite3.ProgrammingError):
                    store.db.execute("SELECT 1")
            state.events.append("lease-close")
        original_session_close(self)

    monkeypatch.setattr(linux_bootstrap, "open_boot_session", bootstrap)
    monkeypatch.setattr(linux_bootstrap, "verify_local_filesystems", lambda paths: None)
    monkeypatch.setattr(cli, "ConfigFile", config)
    monkeypatch.setattr(Store, "__init__", store_init)
    monkeypatch.setattr(Store, "close", store_close)
    monkeypatch.setattr(BootSession, "close_after_writers", session_close)
    arguments(monkeypatch, installation, "serve")
    try:
        yield state
    finally:
        # Even a failing regression must not retain a SQLite writer or its lease.
        for store in state.stores:
            original_close(store)
        for session in state.sessions:
            original_session_close(session)


def test_serve_bootstrap_store_and_uvicorn_share_loop_and_close_writer_before_lease(
    serving: Serving, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "4")

    async def serve(self: uvicorn.Server, sockets: Any = None) -> None:
        assert self.config.workers == 1 and not self.config.reload
        self.started = True
        app = self.config.app
        assert isinstance(app, FastAPI)
        assert app.state.runtime.boot_session is serving.sessions[-1]
        serving.loops.append(asyncio.get_running_loop())
        serving.events.append("serve")
        async with app.router.lifespan_context(app):
            serving.sessions[-1].lease.check()
            assert app.state.runtime.store.db.execute("SELECT 1").fetchone()[0] == 1

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    cli.main()
    serving.assert_closed()
    assert serving.events == ["bootstrap", "config", "serve", "store-close", "lease-close"]
    assert all(loop is serving.loops[0] for loop in serving.loops)


def test_serve_server_failure_before_lifespan_still_closes_store_then_lease(
    serving: Serving, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(self: uvicorn.Server, sockets: Any = None) -> NoReturn:
        assert not self.config.app.state.runtime.started
        raise RuntimeError("before-lifespan")

    monkeypatch.setattr(uvicorn.Server, "serve", fail)
    with pytest.raises(RuntimeError, match="before-lifespan"):
        cli.main()
    serving.assert_closed()
    assert serving.events[-2:] == ["store-close", "lease-close"]


def test_serve_factory_failure_after_store_creation_closes_writer_before_lease(
    serving: Serving, monkeypatch: pytest.MonkeyPatch
) -> None:
    def attachments(*args: Any, **kwargs: Any) -> NoReturn:
        assert serving.stores
        raise RuntimeError("application-construction")

    monkeypatch.setattr(server, "Attachments", attachments)
    with pytest.raises(RuntimeError, match="application-construction"):
        cli.main()
    serving.assert_closed()
    assert serving.events[-2:] == ["store-close", "lease-close"]


def test_serve_runtime_constructor_failure_closes_writer_before_lease(
    serving: Serving, monkeypatch: pytest.MonkeyPatch
) -> None:
    def initialize(self: server.Runtime) -> NoReturn:
        raise RuntimeError("runtime-construction")

    monkeypatch.setattr(server.Runtime, "_initialize", initialize)
    with pytest.raises(RuntimeError, match="runtime-construction"):
        cli.main()
    serving.assert_closed()
    assert serving.events[-2:] == ["store-close", "lease-close"]
