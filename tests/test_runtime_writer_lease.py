"""Cooperative Runtime ownership, not exclusion of arbitrary SQLite writers."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Literal, Self

import pytest

import muli.store as store_module
from muli.config import ConfigFile
from muli.format_admission import CURRENT_FORMAT_STATE, FormatAdmissionError
from muli.runtime import Runtime
from muli.store import Store
from muli.store_initialization import StoreConstructionLock, StoreInitializationError
from muli.store_schema import ExistingStoreError


@pytest.fixture(autouse=True)
def isolated_muli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "muli-home"
    home.mkdir()
    monkeypatch.setenv("MULI_HOME", str(home))
    monkeypatch.delenv("MULI_CONFIG", raising=False)
    monkeypatch.delenv("MULI_DATA_DIR", raising=False)


def no_model(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("This test must never construct a model client")


def run_store_script(path: Path, script: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(store_module.__file__).resolve().parents[1]),
    }
    return subprocess.run(
        [sys.executable, "-c", script, str(path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )


@pytest.mark.asyncio
async def test_runtime_lease_rejects_before_recovery_and_reopens_the_same_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    data_dir = tmp_path / "runtime data"
    first = Runtime(config, data_dir, no_model)
    second: Runtime | None = None
    third: Runtime | None = None
    try:
        main = first.main_session()
        instance = first.store.instance_id
        assert first.store.format_state == CURRENT_FORMAT_STATE
        first.store.add_message(main["id"], "user", "before competing runtime")
        before = tuple(first.store.db.iterdump())
        recoveries: list[str] = []
        recover = Runtime._recover

        def observe_recovery(runtime: Runtime) -> None:
            recoveries.append(runtime.store.instance_id)
            recover(runtime)

        with monkeypatch.context() as patch:
            patch.setattr(Runtime, "_recover", observe_recovery)
            with pytest.raises(StoreInitializationError) as refused:
                second = Runtime(config, data_dir, no_model)
            assert refused.value.code == "store_initialization_busy"

        assert recoveries == []
        assert tuple(first.store.db.iterdump()) == before
        assert first.store.instance_id == instance
        assert first.main_session() == main
        first.store.add_message(main["id"], "assistant", "original writer is still usable")
        messages = first.store.messages(main["id"])
        assert [message["content"] for message in messages] == [
            "before competing runtime",
            "original writer is still usable",
        ]

        await first.close()
        third = Runtime(config, data_dir, no_model)
        assert third.store.instance_id == instance
        assert third.main_session() == main
        assert third.store.messages(main["id"]) == messages
        third.store.put("fixture", {"id": "reopened-writer", "value": "usable"})
        assert third.store.get("fixture", "reopened-writer") == {
            "id": "reopened-writer",
            "value": "usable",
        }
    finally:
        if second is not None:
            await second.close()
        if third is not None:
            await third.close()
        await first.close()


@pytest.mark.asyncio
async def test_failed_runtime_initialization_closes_its_connection_and_releases_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    data_dir = tmp_path / "failed runtime"
    path = data_dir / "muli.sqlite3"
    captured: list[Store] = []

    class InitializationFailure(RuntimeError):
        pass

    def fail_initialization(runtime: Runtime) -> None:
        captured.append(runtime.store)
        runtime.store.put("fixture", {"id": "before-initialize-failure", "value": "kept"})
        raise InitializationFailure("injected Runtime._initialize failure")

    with monkeypatch.context() as patch:
        patch.setattr(Runtime, "_initialize", fail_initialization)
        with pytest.raises(InitializationFailure, match="Runtime._initialize failure"):
            Runtime(config, data_dir, no_model)

    assert len(captured) == 1
    failed_store = captured[0]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed_store.db.execute("SELECT 1")
    with StoreConstructionLock(path) as released:
        released.check()

    retry = Runtime(config, data_dir, no_model)
    try:
        assert retry.store.format_state == CURRENT_FORMAT_STATE
        assert retry.store.get("fixture", "before-initialize-failure") == {
            "id": "before-initialize-failure",
            "value": "kept",
        }
        retry.store.add_message(retry.main_session()["id"], "user", "retry succeeded")
        assert retry.store.messages(retry.main_session()["id"])[-1]["content"] == "retry succeeded"
    finally:
        await retry.close()


def test_runtime_store_lease_blocks_a_process_and_process_exit_releases_it(tmp_path: Path) -> None:
    path = tmp_path / "process data" / "muli.sqlite3"
    contender_script = (
        "from __future__ import annotations\n"
        "import json\nimport sys\nfrom pathlib import Path\n"
        "from muli.store import Store\n"
        "from muli.store_initialization import StoreInitializationError\n"
        "try:\n"
        " store = Store.open_runtime(Path(sys.argv[1]), default_mode='auto')\n"
        "except StoreInitializationError as error:\n"
        " print(json.dumps({'code': error.code}))\n"
        "else:\n"
        " try:\n"
        "  print(json.dumps({'code': 'opened'}))\n"
        " finally:\n"
        "  store.close()\n"
    )
    # Keep the real Store alive until interpreter exit, deliberately without close().
    holder_script = (
        "from __future__ import annotations\n"
        "import json\nimport sys\nfrom pathlib import Path\n"
        "from muli.store import Store\n"
        "store = Store.open_runtime(Path(sys.argv[1]), default_mode='auto')\n"
        "store.put('fixture', {'id': 'process-exit', 'value': 'committed'})\n"
        "print(json.dumps({'instance': store.instance_id, 'main': store.main_session()['id']}))\n"
    )

    first = Store.open_runtime(path, default_mode="auto")
    try:
        instance = first.instance_id
        main = first.main_session()
        first.put("fixture", {"id": "original-process", "value": "kept"})
        before = tuple(first.db.iterdump())
        refused = run_store_script(path, contender_script)
        assert refused.stderr == ""
        assert json.loads(refused.stdout) == {"code": "store_initialization_busy"}
        assert tuple(first.db.iterdump()) == before
        first.put("fixture", {"id": "still-live", "value": "usable"})
    finally:
        first.close()

    exited = run_store_script(path, holder_script)
    assert exited.stderr == ""
    assert json.loads(exited.stdout) == {"instance": instance, "main": main["id"]}
    reopened = Store.open_runtime(path, default_mode="auto")
    try:
        assert reopened.instance_id == instance
        assert reopened.main_session() == main
        assert reopened.get("fixture", "original-process") == {
            "id": "original-process",
            "value": "kept",
        }
        assert reopened.get("fixture", "still-live") == {"id": "still-live", "value": "usable"}
        assert reopened.get("fixture", "process-exit") == {
            "id": "process-exit",
            "value": "committed",
        }
    finally:
        reopened.close()


def test_legacy_store_connections_remain_independently_usable(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    first = Store(path)
    second: Store | None = None
    try:
        second = Store(path)
        assert first.format_state is None and second.format_state is None
        assert first.migration_allowed and second.migration_allowed
        first.put("fixture", {"id": "shared", "value": "first"})
        assert second.get("fixture", "shared") == {"id": "shared", "value": "first"}
        second.put("fixture", {"id": "shared", "value": "second"})
        assert first.get("fixture", "shared") == {"id": "shared", "value": "second"}
        second.close()
        second = None
        first.put("fixture", {"id": "after-peer-close", "value": "still usable"})
        assert first.get("fixture", "after-peer-close") == {
            "id": "after-peer-close",
            "value": "still usable",
        }
    finally:
        if second is not None:
            second.close()
        first.close()


@pytest.mark.parametrize("opener", ["runtime", "existing"])
def test_connection_close_failure_keeps_the_runtime_lock_until_a_successful_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: Literal["runtime", "existing"]
) -> None:
    path = tmp_path / "close-failure.sqlite3"
    with closing(Store.open_runtime(path, default_mode="ask")) as initial:
        instance = initial.instance_id
    refuse_close = True

    class RefusingClose(sqlite3.Connection):
        def close(self) -> None:
            if refuse_close:
                raise sqlite3.OperationalError("injected connection close failure")
            super().close()

    connect = sqlite3.connect

    def refusing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return connect(*args, **kwargs, factory=RefusingClose)

    monkeypatch.setattr(store_module.sqlite3, "connect", refusing_connect)
    store = (
        Store.open_runtime(path, default_mode="ask")
        if opener == "runtime"
        else Store.open_existing(path, runtime_owner=True)
    )
    try:
        with pytest.raises(sqlite3.OperationalError, match="connection close failure"):
            store.close()
        assert store.instance_id == instance
        store.put("fixture", {"id": "after-failed-close", "value": "still open"})
        with pytest.raises(StoreInitializationError, match="store_initialization_busy"):
            with StoreConstructionLock(path):
                pass
    finally:
        refuse_close = False
        store.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.db.execute("SELECT 1")
    with StoreConstructionLock(path) as released:
        released.check()


def test_runtime_owner_factory_close_failure_is_not_mapped_to_a_closed_store_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "factory-close-failure.sqlite3"
    Store.open_runtime(path, default_mode="ask").close()
    refuse_close = True
    stores: list[Store] = []

    class RefusingClose(sqlite3.Connection):
        def close(self) -> None:
            if refuse_close:
                raise sqlite3.OperationalError("injected factory close failure")
            super().close()

    connect = sqlite3.connect
    configure = Store._configure_existing

    def refusing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return connect(*args, **kwargs, factory=RefusingClose)

    def failed_admission(store: Store, *, durable: bool) -> None:
        stores.append(store)
        configure(store, durable=durable)
        raise FormatAdmissionError("store_format_marker_missing")

    monkeypatch.setattr(store_module.sqlite3, "connect", refusing_connect)
    monkeypatch.setattr(Store, "_configure_existing", failed_admission)
    try:
        with pytest.raises(sqlite3.OperationalError, match="factory close failure"):
            Store.open_existing(path, runtime_owner=True)
        assert len(stores) == 1
        assert stores[0].db.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(StoreInitializationError, match="store_initialization_busy"):
            with StoreConstructionLock(path):
                pass
    finally:
        refuse_close = False
        for store in stores:
            store.close()
    with StoreConstructionLock(path) as released:
        released.check()


def test_runtime_owner_busy_refuses_before_opening_any_sqlite_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "busy.sqlite3"
    store = Store.open_runtime(path, default_mode="ask")
    connections: list[str] = []

    def forbidden_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connections.append("sqlite")
        raise AssertionError("a busy Runtime lock must fail before SQLite connects")

    monkeypatch.setattr(store_module.sqlite3, "connect", forbidden_connect)
    try:
        with pytest.raises(ExistingStoreError, match="store_initialization_busy"):
            Store.open_existing(path, runtime_owner=True)
        assert connections == []
        assert store.db.execute("SELECT 1").fetchone()[0] == 1
    finally:
        store.close()
    with StoreConstructionLock(path) as released:
        released.check()


def test_default_existing_store_does_not_claim_or_release_runtime_ownership(tmp_path: Path) -> None:
    path = tmp_path / "existing-reader.sqlite3"
    with closing(Store.open_runtime(path, default_mode="ask")) as owner:
        with closing(Store.open_existing(path)) as extra:
            assert extra.instance_id == owner.instance_id
            extra.put("fixture", {"id": "independent", "value": "legacy behavior"})
            assert owner.get("fixture", "independent")["value"] == "legacy behavior"
        with pytest.raises(StoreInitializationError, match="store_initialization_busy"):
            with StoreConstructionLock(path):
                pass
    with StoreConstructionLock(path) as released:
        released.check()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symbolic-link path-change boundary")
def test_runtime_owner_uses_the_canonical_path_captured_before_a_symlink_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_path, second_path = tmp_path / "first.sqlite3", tmp_path / "second.sqlite3"
    with closing(Store.open_runtime(first_path, default_mode="ask")) as first:
        first_instance = first.instance_id
    with closing(Store.open_runtime(second_path, default_mode="ask")) as second:
        second_instance = second.instance_id
        second_before = tuple(second.db.iterdump())
    alias = tmp_path / "current.sqlite3"
    alias.symlink_to(first_path)
    locks: list[StoreConstructionLock] = []

    class RetargetingLock(StoreConstructionLock):
        def __enter__(self) -> Self:
            super().__enter__()
            locks.append(self)
            alias.unlink()
            alias.symlink_to(second_path)
            return self

    monkeypatch.setattr(store_module, "StoreConstructionLock", RetargetingLock)
    with closing(Store.open_existing(alias, runtime_owner=True)) as opened:
        assert opened.instance_id == first_instance != second_instance
        assert locks[0].database == first_path.resolve()
        assert Path(opened.db.execute("PRAGMA database_list").fetchone()[2]) == first_path
        locks[0].check()
        with pytest.raises(StoreInitializationError, match="store_initialization_busy"):
            with StoreConstructionLock(first_path):
                pass
        with StoreConstructionLock(second_path) as unclaimed:
            unclaimed.check()
    with StoreConstructionLock(first_path) as released:
        released.check()
    with closing(Store.open_existing(second_path)) as unchanged:
        assert unchanged.instance_id == second_instance
        assert tuple(unchanged.db.iterdump()) == second_before
