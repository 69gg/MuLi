"""The exact outbox3 predecessor must refuse durable cancelled deliveries."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from test_channel_format_upgrade import legacy_database, upgrade
from test_channel_format_upgrade import snapshot as stored_rows
from test_fresh_format_admission import database as database
from test_fresh_format_admission import deny_construction_writes, no_model, snapshot
from test_project_inbox_format import upgrade_platform

import muli.store as store_module
from muli.channels import ChannelManager
from muli.compatibility import (
    CompatibilityManifest,
    FormatState,
    FormatVersions,
    assess_compatibility,
)
from muli.config import ConfigFile
from muli.evidence import EvidenceError
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    PRE_OUTBOX_CANCELLATION_FORMAT_STATE,
    FormatAdmissionError,
    require_current_format,
)
from muli.format_evidence import FORMAT_STATE_KEY, read_format_evidence
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import Store
from muli.store_initialization import StoreConstructionLock, StoreInitializationError
from muli.store_schema import ExistingStoreError

_FIXTURES = Path(__file__).parent / "fixtures"
# Exact committed bytes supplied by root from 4f78404f800f69c994d9829c0e37787eb4ac3ec7.
# These source oracles are not an installed old wheel or an A/B service run.
_E144_BYTES = {
    "format_admission_v2_144.py": (
        "9ae2afcaf299bdc050ca7fede70bb381a7771fe1ed1c06aae183aa26f729ea24"
    ),
    "compatibility_v2_144.json": (
        "5196aaa4d165e9519a87fb287dbd067259e9c93b705d6e07457323d5ba1aa433"
    ),
    "previous_outbox_manager_v2_144.py.txt": (
        "9632ebdf4d10e00f99b27ba497f30728cf258b99fcb60c7143df9f9437e8e48d"
    ),
    "previous_channel_history_v2_144.py.txt": (
        "d9eb1c6d11fbb9b404429940101a1e0ec06ab49cd33ae1fbdf4f34e467de1f46"
    ),
}
POSIX = pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX stopped upgrade")

OUTBOX3_FORMAT = FormatState(
    metadata_version=1,
    formats=FormatVersions(
        database=1,
        objects=4,
        configuration=2,
        ingress=7,
        outbox=3,
        memory=5,
        install_profile=1,
    ),
)
OUTBOX4_FORMAT = OUTBOX3_FORMAT.model_copy(
    update={"formats": OUTBOX3_FORMAT.formats.model_copy(update={"outbox": 4})}
)


def frozen_e144_bytes(name: str) -> bytes:
    raw = (_FIXTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _E144_BYTES[name]
    return raw


def frozen_e144_module(name: str) -> ModuleType:
    frozen_e144_bytes(name)
    path = _FIXTURES / name
    package = "muli.channels" if name == "previous_outbox_manager_v2_144.py.txt" else "muli"
    module_name = package + "._frozen_" + name.replace(".", "_")
    loader = SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_file_location(module_name, path, loader=loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def current_manifest() -> CompatibilityManifest:
    path = Path(__file__).parents[1] / "src" / "muli" / "compatibility.json"
    return CompatibilityManifest.from_json(path.read_bytes())


def test_new_store_and_manifest_declare_the_cancelled_outbox_contract(database: Path) -> None:
    with closing(sqlite3.connect(database)) as db:
        evidence = read_format_evidence(db)
        assert evidence.state == OUTBOX4_FORMAT
        assert db.total_changes == 0
    assert CURRENT_FORMAT_STATE == OUTBOX4_FORMAT
    manifest = current_manifest()
    assert manifest.formats.outbox.reads == (4,)
    assert manifest.formats.outbox.writes == 4


def save_cancelled(store: Store) -> None:
    route = {
        "name": "telegram",
        "account_id": "bot:format-fixture",
        "kind": "private",
        "sender_id": "format-owner",
        "target": "format-owner",
    }
    payload = {"parts": [{"text": "主人取消的原文；只做本地格式核查。"}]}
    with store.db:
        store.db.execute(
            "INSERT INTO channel_outbox(id,name,route,payload,state,next_attempt,created_at,updated_at) "
            "VALUES('cancelled-record','telegram',?,?,'cancelled',0,1,1)",
            (json.dumps(route), json.dumps(payload, ensure_ascii=False)),
        )


def test_frozen_e144_admission_refuses_cancelled_store_before_business_writes(
    database: Path,
) -> None:
    old = frozen_e144_module("format_admission_v2_144.py")
    assert old.CURRENT_FORMAT_STATE == OUTBOX3_FORMAT == PRE_OUTBOX_CANCELLATION_FORMAT_STATE
    assert CURRENT_FORMAT_STATE == OUTBOX4_FORMAT
    store = Store.open_existing(database)
    try:
        save_cancelled(store)
    finally:
        store.close()
    before = snapshot(database)
    with closing(sqlite3.connect(database)) as db:
        evidence = read_format_evidence(db)
        old.require_current_format(replace(evidence, state=OUTBOX3_FORMAT))
        with pytest.raises(old.FormatAdmissionError, match="^store_format_unsupported$"):
            old.require_current_format(evidence)
        with pytest.raises(FormatAdmissionError, match="^store_format_upgrade_required$"):
            require_current_format(replace(evidence, state=OUTBOX3_FORMAT))
        require_current_format(evidence)
        assert db.total_changes == 0
    assert snapshot(database) == before


@pytest.mark.parametrize("after_upgrade", [False, True], ids=["outbox3", "outbox4"])
@pytest.mark.parametrize("reverse", [False, True], ids=["upgrade", "downgrade"])
def test_managed_same_format_switch_refuses_both_outbox_directions(
    after_upgrade: bool, reverse: bool
) -> None:
    previous = CompatibilityManifest.from_json(frozen_e144_bytes("compatibility_v2_144.json"))
    current = current_manifest()
    assert previous.release == "2.144.0"
    assert previous.formats.outbox.reads == (3,) and previous.formats.outbox.writes == 3
    assert current.formats.outbox.reads == (4,) and current.formats.outbox.writes == 4
    assert current.formats.model_dump(exclude={"outbox"}) == previous.formats.model_dump(
        exclude={"outbox"}
    )
    assert not previous.requires_migration and not current.requires_migration
    state = OUTBOX4_FORMAT if after_upgrade else OUTBOX3_FORMAT
    base, candidate = (current, previous) if reverse else (previous, current)
    result = assess_compatibility(base, candidate, state, handoff_protocol=2)
    assert not result.format_compatible
    assert result.code == "current_format_unreadable" and result.domain == "outbox"
    assert result.side == ("candidate" if after_upgrade == reverse else "base")
    for declaration, exact in ((previous, OUTBOX3_FORMAT), (current, OUTBOX4_FORMAT)):
        assert assess_compatibility(
            declaration, declaration, exact, handoff_protocol=2
        ).format_compatible


@pytest.mark.parametrize("after_upgrade", [False, True], ids=["outbox3", "outbox4"])
def test_reading_both_outbox_contracts_cannot_authorize_different_writes(
    after_upgrade: bool,
) -> None:
    previous = CompatibilityManifest.from_json(frozen_e144_bytes("compatibility_v2_144.json"))
    declarations = tuple(
        manifest.model_copy(
            update={
                "formats": manifest.formats.model_copy(
                    update={"outbox": manifest.formats.outbox.model_copy(update={"reads": (3, 4)})}
                )
            }
        )
        for manifest in (previous, current_manifest())
    )
    state = OUTBOX4_FORMAT if after_upgrade else OUTBOX3_FORMAT
    result = assess_compatibility(declarations[0], declarations[1], state, handoff_protocol=2)
    assert not result.format_compatible
    assert result.code == "write_format_change" and result.domain == "outbox"
    assert result.side == ("base" if after_upgrade else "candidate")


@pytest.mark.parametrize("gate", ["current", "frozen_e144"])
@pytest.mark.parametrize("opener", ["ordinary", "existing", "runtime_store", "runtime", "server"])
def test_both_actual_gates_refuse_other_outbox_contract_before_writes_or_recovery(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str, opener: str
) -> None:
    error: type[Exception] = FormatAdmissionError
    code = "store_format_upgrade_required"
    if gate == "current":
        with closing(sqlite3.connect(database)) as db:
            db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (OUTBOX3_FORMAT.model_dump_json(), FORMAT_STATE_KEY),
            )
            db.commit()
    else:
        store = Store.open_existing(database)
        try:
            save_cancelled(store)
        finally:
            store.close()
        old = frozen_e144_module("format_admission_v2_144.py")
        # Use the actual old gate with current construction support, not an old installation.
        monkeypatch.setattr(store_module, "require_current_format", old.require_current_format)
        error, code = old.FormatAdmissionError, "store_format_unsupported"
    before = snapshot(database)

    def forbidden_recovery(runtime: Runtime) -> None:
        raise AssertionError("An incompatible outbox must fail before business recovery")

    monkeypatch.setattr(Runtime, "_recover", forbidden_recovery)
    connections, mutations = deny_construction_writes(monkeypatch)
    with pytest.raises((error, ExistingStoreError), match=f"^{code}$"):
        if opener == "ordinary":
            Store(database)
        elif opener == "existing":
            Store.open_existing(database)
        elif opener == "runtime_store":
            Store.open_runtime(database, default_mode="auto")
        elif opener == "runtime":
            Runtime(ConfigFile(tmp_path / "config.json"), database.parent, no_model)
        else:
            create_app(ConfigFile(tmp_path / "config.json"), database.parent)
    assert mutations == [] and snapshot(database) == before
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")


async def test_bypassing_old_admission_exposes_nonterminal_wait_and_unreadable_cancelled_history(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finite method-only diagnosis after proving the old gate refuses the real store."""
    old = frozen_e144_module("format_admission_v2_144.py")
    store = Store.open_existing(database)
    try:
        save_cancelled(store)
        before = tuple(store.db.iterdump())
        with pytest.raises(old.FormatAdmissionError, match="^store_format_unsupported$"):
            old.require_current_format(read_format_evidence(store.db))
        manager_type = frozen_e144_module("previous_outbox_manager_v2_144.py.txt").ChannelManager
        manager = manager_type.__new__(manager_type)
        manager.store, manager._stopped, manager.changed = store, False, asyncio.Condition()
        observed: list[bool] = []

        class TerminalObserved(Exception):
            pass

        async def observe_terminal(predicate: Callable[[], bool]) -> bool:
            observed.append(predicate())
            raise TerminalObserved

        # Observe the actual old predicate once; never use a timeout as proof of a hang.
        monkeypatch.setattr(manager.changed, "wait_for", observe_terminal)
        with pytest.raises(TerminalObserved):
            await manager.wait("cancelled-record")
        assert observed == [False]
        retained = await manager.retry("cancelled-record")
        assert retained["state"] == "cancelled"  # Old retry does not interpret it as success.
        history_type = frozen_e144_module("previous_channel_history_v2_144.py.txt").ChannelHistory
        with pytest.raises(EvidenceError) as rejected:
            history_type(store).read_delivery(
                "telegram",
                "bot:format-fixture",
                "format-owner",
                store.existing_main_session_id(),
                delivery_id="cancelled-record",
                max_output_chars=5000,
            )
        assert rejected.value.code == "content_unavailable"
        assert tuple(store.db.iterdump()) == before
    finally:
        store.close()


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_stopped_outbox3_upgrade_preserves_every_business_row_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=OUTBOX3_FORMAT)
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    configuration = config.path.read_bytes()
    with closing(sqlite3.connect(path)) as db:
        identity = read_format_evidence(db)
        for state, part, receipts in (
            ("pending", 0, "[]"),
            ("retry", 0, "[]"),
            ("retry", 1, '[ {"message_id":"confirmed-original"} ]'),
            ("unknown", 0, "[]"),
            ("sending", 1, '[ {"message_id":"confirmed-original"} ]'),
        ):
            db.execute(
                "INSERT INTO channel_outbox(id,name,route,payload,state,next_part,receipts,"
                "next_attempt,created_at,updated_at) VALUES(?,?,?,?,?,?,?,0,1,1)",
                (
                    f"old-{state}-{part}",
                    "telegram",
                    '{ "kind":"private", "target":"format-owner" }',
                    '{ "parts":[{"text":"保持旧原文"},{"attachment_id":"old-file"}] }',
                    state,
                    part,
                    receipts,
                ),
            )
        db.commit()
    before = stored_rows(path, omit_marker=True)

    def forbidden_recovery_or_send(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A marker-only upgrade must not recover, execute or send")

    for owner, name in (
        (Runtime, "_recover"),
        (Runtime, "startup"),
        (ChannelManager, "recover"),
        (ChannelManager, "start"),
        (httpx.AsyncClient, "send"),
    ):
        monkeypatch.setattr(owner, name, forbidden_recovery_or_send)
    upgrade_platform(platform, monkeypatch)
    assert upgrade(path.parent) is True
    assert upgrade(path.parent) is False
    assert stored_rows(path, omit_marker=True) == before
    assert config.path.read_bytes() == configuration
    with closing(sqlite3.connect(path)) as db:
        current = read_format_evidence(db)
        assert current.state == OUTBOX4_FORMAT
        assert (current.store_instance, current.main_session) == (
            identity.store_instance,
            identity.main_session,
        )
        assert db.total_changes == 0
    store = Store.open_existing(path)
    store.close()
    assert stored_rows(path, omit_marker=True) == before


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_outbox3_upgrade_requires_a_stopped_store_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=OUTBOX3_FORMAT)
    before = snapshot(path)
    upgrade_platform(platform, monkeypatch)
    with StoreConstructionLock(path):
        with pytest.raises(StoreInitializationError, match="^store_initialization_busy$"):
            upgrade(path.parent)
    assert snapshot(path) == before


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("managed", ["explicit_root", "projection", "switch", "transaction"])
def test_outbox3_upgrade_refuses_managed_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, managed: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=OUTBOX3_FORMAT)
    if managed != "explicit_root":
        kind, identifier, payload = {
            "projection": ("management_projection", "transaction-v1", "{}"),
            "switch": ("management", "retained-plan", '{"action":"switch"}'),
            "transaction": ("management", "retained-plan", '{"action":"restart","transaction":{}}'),
        }[managed]
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "INSERT INTO objects(kind,id,data,updated) VALUES(?,?,?,1)",
                (kind, identifier, payload),
            )
            db.commit()
    before = snapshot(path)
    upgrade_platform(platform, monkeypatch)
    with pytest.raises(FormatAdmissionError, match="^store_format_managed_upgrade_unsupported$"):
        upgrade(
            path.parent,
            managed_root=path.parent / "management" if managed == "explicit_root" else None,
        )
    assert snapshot(path) == before
