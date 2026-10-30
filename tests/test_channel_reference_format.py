"""The frozen ingress5 consumer cannot preserve native-reference input meaning."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
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

from muli.channels import ChannelManager
from muli.channels.adapters import Telegram
from muli.compatibility import CompatibilityManifest, assess_compatibility
from muli.config import ConfigFile
from muli.conversation import Conversation
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    PRE_CHANNEL_REFERENCE_FORMAT_STATE,
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
# Exact copies from Good88340994f70fd0417868981dcda81667d8fa2f26, not a
# reconstruction of its admission rules. Root verifies the bytes separately.
_E139_BYTES = {
    "format_admission_v2_139.py": (
        "123cc51450e3cc66a71fb06b2b2616e2f35eaf39843eb5ef14eb55813f419ec8"
    ),
    "compatibility_v2_139.json": (
        "3e835cbdc9edc63d3109c23e240afce5e9e0d441e95f8afcfbc3eccf10ee1df8"
    ),
}
POSIX = pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX stopped upgrade")


def frozen_e139_bytes(name: str) -> bytes:
    raw = (_FIXTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _E139_BYTES[name]
    return raw


def frozen_e139_reader() -> ModuleType:
    frozen_e139_bytes("format_admission_v2_139.py")
    path = _FIXTURES / "format_admission_v2_139.py"
    spec = importlib.util.spec_from_file_location("muli_frozen_format_admission_v2_139", path)
    assert spec is not None and spec.loader is not None
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    return reader


def current_manifest() -> CompatibilityManifest:
    path = Path(__file__).parents[1] / "src" / "muli" / "compatibility.json"
    return CompatibilityManifest.from_json(path.read_bytes())


def test_e139_actual_consumer_refuses_current_ingress_before_business_writes(
    database: Path,
) -> None:
    old = frozen_e139_reader()
    assert old.CURRENT_FORMAT_STATE == PRE_CHANNEL_REFERENCE_FORMAT_STATE
    before = snapshot(database)
    with closing(sqlite3.connect(database)) as db:
        evidence = read_format_evidence(db)
        old.require_current_format(replace(evidence, state=PRE_CHANNEL_REFERENCE_FORMAT_STATE))
        with pytest.raises(old.FormatAdmissionError, match="^store_format_unsupported$"):
            old.require_current_format(evidence)
        assert db.total_changes == 0
    assert snapshot(database) == before


def test_current_consumer_requires_explicit_upgrade_from_exact_ingress5(database: Path) -> None:
    old = frozen_e139_reader()
    assert CURRENT_FORMAT_STATE.formats.model_dump() == {
        **old.CURRENT_FORMAT_STATE.formats.model_dump(),
        "ingress": 7,
    }
    with closing(sqlite3.connect(database)) as db:
        evidence = read_format_evidence(db)
        previous = replace(evidence, state=old.CURRENT_FORMAT_STATE)
        old.require_current_format(previous)
        with pytest.raises(FormatAdmissionError, match="^store_format_upgrade_required$"):
            require_current_format(previous)
        require_current_format(evidence)
        assert db.total_changes == 0


@pytest.mark.parametrize("after_upgrade", [False, True], ids=["ingress5", "ingress7"])
@pytest.mark.parametrize("reverse", [False, True], ids=["upgrade", "downgrade"])
def test_managed_same_format_switch_refuses_both_native_reference_directions(
    after_upgrade: bool, reverse: bool
) -> None:
    previous = CompatibilityManifest.from_json(frozen_e139_bytes("compatibility_v2_139.json"))
    current = current_manifest()
    assert previous.release == "2.139.0"
    assert previous.formats.ingress.reads == (5,) and previous.formats.ingress.writes == 5
    assert current.formats.ingress.reads == (7,) and current.formats.ingress.writes == 7
    assert current.formats.model_dump(exclude={"ingress"}) == previous.formats.model_dump(
        exclude={"ingress"}
    )
    state = CURRENT_FORMAT_STATE if after_upgrade else PRE_CHANNEL_REFERENCE_FORMAT_STATE
    base, candidate = (current, previous) if reverse else (previous, current)
    result = assess_compatibility(base, candidate, state, handoff_protocol=2)
    assert not result.format_compatible
    assert result.code == "current_format_unreadable" and result.domain == "ingress"
    assert result.side == ("candidate" if after_upgrade == reverse else "base")
    assert assess_compatibility(
        previous, previous, PRE_CHANNEL_REFERENCE_FORMAT_STATE, handoff_protocol=2
    ).format_compatible
    assert assess_compatibility(
        current, current, CURRENT_FORMAT_STATE, handoff_protocol=2
    ).format_compatible


@pytest.mark.parametrize("after_upgrade", [False, True], ids=["ingress5", "ingress7"])
def test_reading_both_ingress_contracts_does_not_authorize_cross_format_writes(
    after_upgrade: bool,
) -> None:
    previous = CompatibilityManifest.from_json(frozen_e139_bytes("compatibility_v2_139.json"))
    declarations = tuple(
        manifest.model_copy(
            update={
                "formats": manifest.formats.model_copy(
                    update={
                        "ingress": manifest.formats.ingress.model_copy(update={"reads": (5, 7)})
                    }
                )
            }
        )
        for manifest in (previous, current_manifest())
    )
    state = CURRENT_FORMAT_STATE if after_upgrade else PRE_CHANNEL_REFERENCE_FORMAT_STATE
    result = assess_compatibility(declarations[0], declarations[1], state, handoff_protocol=2)
    assert not result.format_compatible
    assert result.code == "write_format_change" and result.domain == "ingress"
    assert result.side == ("base" if after_upgrade else "candidate")


@pytest.mark.parametrize("opener", ["ordinary", "existing", "runtime_store", "runtime", "server"])
def test_ingress5_is_refused_before_recovery_or_construction_writes(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "UPDATE runtime_state SET value=? WHERE name=?",
            (PRE_CHANNEL_REFERENCE_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
        )
        db.commit()
    before = snapshot(database)

    def forbidden_recovery(runtime: Runtime) -> None:
        raise AssertionError("ingress5 must fail before workers or business recovery")

    monkeypatch.setattr(Runtime, "_recover", forbidden_recovery)
    connections, mutations = deny_construction_writes(monkeypatch)
    with pytest.raises(
        (FormatAdmissionError, ExistingStoreError), match="^store_format_upgrade_required$"
    ):
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


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_stopped_ingress5_upgrade_preserves_input_receipts_and_unknown_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = tmp_path.resolve() / "data" / "muli.sqlite3"
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    configuration = config.path.read_bytes()
    route = {
        "name": "telegram",
        "account_id": "139",
        "sender_id": "7",
        "target": "7",
        "kind": "private",
        "reply_to": "140",
    }
    store = Store.open_runtime(path, default_mode="auto")
    try:
        identity = read_format_evidence(store.db)
        main = identity.main_session
        conversation = Conversation(store)
        archived, inserted = conversation.enqueue(
            main,
            "已领取的原主人输入",
            source="channel:telegram:139",
            principal="owner",
            mode="auto",
            max_pending=4,
            event_id="before-ingress6-archived",
            route=route,
        )
        assert inserted
        claimed = store.claim_inputs(main, "old-completed-run", limit=1)
        assert len(claimed) == 1 and claimed[0]["id"] == archived["id"]
        pending, inserted = conversation.enqueue(
            main,
            "原 pending 主人输入不能由升级器消费或推断引用",
            source="channel:telegram:139",
            principal="owner",
            mode="auto",
            max_pending=4,
            event_id="before-ingress6-pending",
            route=route,
        )
        assert inserted
        with store.db:
            for identifier, receipt, fingerprint in (
                ("old-completed-event", '{ "input_id" : "old-input", "accepted" : true }', "old"),
                ("old-pending-event", None, "pending-fingerprint"),
                ("old-unverified-event", '{ "accepted" : true }', None),
            ):
                store.db.execute(
                    "INSERT INTO channel_inbound(id,name,receipt,created_at,fingerprint) "
                    "VALUES(?,'telegram',?,1,?)",
                    (identifier, receipt, fingerprint),
                )
            for identifier, state, confirmed, receipts in (
                ("old-sent", "sent", 2, '[ "101", "102" ]'),
                ("old-unknown", "unknown", 1, '[ "103" ]'),
                ("old-queued", "queued", 0, "[]"),
            ):
                store.db.execute(
                    "INSERT INTO channel_outbox(id,name,route,payload,state,next_part,receipts,"
                    "next_attempt,created_at,updated_at) VALUES(?,'telegram',?,?,?,?,?,0,1,1)",
                    (
                        identifier,
                        json.dumps(route),
                        '{ "parts" : [{"text":"旧正文 A"},{"text":"旧正文 B"}] }',
                        state,
                        confirmed,
                        receipts,
                    ),
                )
            store.db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (PRE_CHANNEL_REFERENCE_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
            )
    finally:
        store.close()
    before = stored_rows(path, omit_marker=True)

    def forbidden_runtime_or_send(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A stopped format upgrade must not recover, execute or send")

    monkeypatch.setattr(Runtime, "_recover", forbidden_runtime_or_send)
    monkeypatch.setattr(Runtime, "startup", forbidden_runtime_or_send)
    monkeypatch.setattr(ChannelManager, "recover", forbidden_runtime_or_send)
    monkeypatch.setattr(ChannelManager, "start", forbidden_runtime_or_send)
    monkeypatch.setattr(Telegram, "send", forbidden_runtime_or_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_runtime_or_send)
    upgrade_platform(platform, monkeypatch)
    assert upgrade(path.parent) is True
    assert upgrade(path.parent) is False
    assert stored_rows(path, omit_marker=True) == before
    assert config.path.read_bytes() == configuration
    reopened = Store.open_existing(path)
    try:
        current = read_format_evidence(reopened.db)
        assert current.state == CURRENT_FORMAT_STATE
        assert (current.store_instance, current.main_session) == (
            identity.store_instance,
            identity.main_session,
        )
        assert reopened.pending_inputs(main) == 1
        retained = Conversation(reopened).pending(main)
        assert len(retained) == 1 and retained[0]["id"] == pending["id"]
        assert "channel_reference" not in retained[0]
        message = reopened.get("message", archived["id"])
        assert message is not None and "channel_reference" not in message
        assert tuple(
            reopened.db.execute(
                "SELECT state,next_part,receipts FROM channel_outbox WHERE id='old-unknown'"
            ).fetchone()
        ) == ("unknown", 1, '[ "103" ]')
    finally:
        reopened.close()
    assert stored_rows(path, omit_marker=True) == before
    assert config.path.read_bytes() == configuration


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_ingress5_upgrade_requires_a_stopped_store_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_CHANNEL_REFERENCE_FORMAT_STATE)
    before = snapshot(path)
    upgrade_platform(platform, monkeypatch)
    with StoreConstructionLock(path):
        with pytest.raises(StoreInitializationError, match="^store_initialization_busy$"):
            upgrade(path.parent)
    assert snapshot(path) == before


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("managed", ["explicit_root", "projection", "switch", "transaction"])
def test_ingress5_upgrade_does_not_bypass_managed_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, managed: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_CHANNEL_REFERENCE_FORMAT_STATE)
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


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("source", ["unmarked", "mixed", "unknown"])
def test_native_reference_upgrade_does_not_guess_an_input_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, source: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_CHANNEL_REFERENCE_FORMAT_STATE)
    with closing(sqlite3.connect(path)) as db:
        if source == "unmarked":
            db.execute("DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,))
        else:
            state = PRE_CHANNEL_REFERENCE_FORMAT_STATE.model_copy(
                update={
                    "formats": PRE_CHANNEL_REFERENCE_FORMAT_STATE.formats.model_copy(
                        update={"ingress": 6, "memory": 4} if source == "mixed" else {"ingress": 8}
                    )
                }
            )
            db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (state.model_dump_json(), FORMAT_STATE_KEY),
            )
        db.commit()
    before = snapshot(path)
    upgrade_platform(platform, monkeypatch)
    with pytest.raises(FormatAdmissionError, match="^store_format_upgrade_source_unsupported$"):
        upgrade(path.parent)
    assert snapshot(path) == before


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_ingress5_commit_failure_keeps_original_marker_and_business_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_CHANNEL_REFERENCE_FORMAT_STATE)
    before = snapshot(path)
    connect = sqlite3.connect
    commits: list[bool] = []

    def authorize(action: int, first: str | None, second: str | None, *rest: Any) -> int:
        if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
            commits.append(True)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def guarded(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = connect(*args, **kwargs)
        db.set_authorizer(authorize)
        return db

    upgrade_platform(platform, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", guarded)
        with pytest.raises(sqlite3.DatabaseError):
            upgrade(path.parent)
    assert commits == [True]
    assert snapshot(path) == before


@pytest.mark.parametrize("platform", ["win32", "unsupported"])
def test_ingress5_upgrade_keeps_existing_unsupported_platform_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(tmp_path / "data", state=PRE_CHANNEL_REFERENCE_FORMAT_STATE)
    before = snapshot(path)

    def forbidden_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        raise AssertionError("Unsupported platforms must refuse before opening SQLite")

    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", platform)
        patch.setattr(sqlite3, "connect", forbidden_connection)
        with pytest.raises(
            FormatAdmissionError, match="^store_format_upgrade_platform_unsupported$"
        ):
            upgrade(path.parent)
    assert snapshot(path) == before
