"""The exact objects3 predecessor must not interpret multi-anchor snapshots."""

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
from test_outbox_cancellation_format import frozen_e144_bytes, frozen_e144_module
from test_project_inbox_format import upgrade_platform

import muli.store as store_module
from muli.channels import ChannelManager
from muli.compaction import Compactor, digest
from muli.compatibility import CompatibilityManifest, assess_compatibility
from muli.config import CompactionSettings, ConfigFile
from muli.conversation import Conversation
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE,
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
# Root froze original bytes from signed Good0c630d4696913850750b3d08126ba30d1552496c.
# The 2.141.0 source candidate is not a published or installed old release.
_E141_BYTES = {
    "format_admission_v2_141.py": (
        "aa73f4d7ee66bb0ad5ca25f7c7c5ddbb0f8d1e714ca2c2fb802320dd1d4f09ce"
    ),
    "compatibility_v2_141.json": (
        "8bab43aa882602e1d97d2b9700ee9dd977726a2d6a793cbb6cd80afa765e514b"
    ),
}
POSIX = pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX stopped upgrade")


def frozen_e141_bytes(name: str) -> bytes:
    raw = (_FIXTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _E141_BYTES[name]
    return raw


def frozen_e141_reader() -> ModuleType:
    frozen_e141_bytes("format_admission_v2_141.py")
    path = _FIXTURES / "format_admission_v2_141.py"
    spec = importlib.util.spec_from_file_location("muli_frozen_format_admission_v2_141", path)
    assert spec is not None and spec.loader is not None
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    return reader


def objects4_manifest() -> CompatibilityManifest:
    # Preserve the objects3 -> objects4 boundary as outbox advances separately.
    return CompatibilityManifest.from_json(frozen_e144_bytes("compatibility_v2_144.json"))


def test_e141_actual_admission_refuses_objects4_before_business_writes(database: Path) -> None:
    old = frozen_e141_reader()
    assert old.CURRENT_FORMAT_STATE == PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE
    historical = frozen_e144_module("format_admission_v2_144.py")
    assert historical.CURRENT_FORMAT_STATE == PRE_OUTBOX_CANCELLATION_FORMAT_STATE
    assert historical.CURRENT_FORMAT_STATE.formats.model_dump() == {
        **old.CURRENT_FORMAT_STATE.formats.model_dump(),
        "objects": 4,
    }
    before = snapshot(database)
    with closing(sqlite3.connect(database)) as db:
        evidence = read_format_evidence(db)
        previous = replace(evidence, state=old.CURRENT_FORMAT_STATE)
        old.require_current_format(previous)
        historical_evidence = replace(evidence, state=historical.CURRENT_FORMAT_STATE)
        historical.require_current_format(historical_evidence)
        with pytest.raises(old.FormatAdmissionError, match="^store_format_unsupported$"):
            old.require_current_format(historical_evidence)
        with pytest.raises(old.FormatAdmissionError, match="^store_format_unsupported$"):
            old.require_current_format(evidence)
        with pytest.raises(FormatAdmissionError, match="^store_format_upgrade_required$"):
            require_current_format(previous)
        require_current_format(evidence)
        assert db.total_changes == 0
    assert snapshot(database) == before


@pytest.mark.parametrize("after_upgrade", [False, True], ids=["objects3", "objects4"])
@pytest.mark.parametrize("reverse", [False, True], ids=["upgrade", "downgrade"])
def test_managed_same_format_switch_refuses_both_compaction_directions(
    after_upgrade: bool, reverse: bool
) -> None:
    previous = CompatibilityManifest.from_json(frozen_e141_bytes("compatibility_v2_141.json"))
    current = objects4_manifest()
    assert previous.release == "2.141.0"
    assert current.release == "2.144.0"
    assert previous.formats.objects.reads == (3,) and previous.formats.objects.writes == 3
    assert current.formats.objects.reads == (4,) and current.formats.objects.writes == 4
    assert current.formats.model_dump(exclude={"objects"}) == previous.formats.model_dump(
        exclude={"objects"}
    )
    assert not previous.requires_migration and not current.requires_migration
    state = (
        PRE_OUTBOX_CANCELLATION_FORMAT_STATE
        if after_upgrade
        else PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE
    )
    base, candidate = (current, previous) if reverse else (previous, current)
    result = assess_compatibility(base, candidate, state, handoff_protocol=2)
    assert not result.format_compatible
    assert result.code == "current_format_unreadable" and result.domain == "objects"
    assert result.side == ("candidate" if after_upgrade == reverse else "base")
    for declaration, exact in (
        (previous, PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE),
        (current, PRE_OUTBOX_CANCELLATION_FORMAT_STATE),
    ):
        assert assess_compatibility(
            declaration, declaration, exact, handoff_protocol=2
        ).format_compatible


@pytest.mark.parametrize("after_upgrade", [False, True], ids=["objects3", "objects4"])
def test_reading_both_objects_contracts_cannot_authorize_different_writes(
    after_upgrade: bool,
) -> None:
    previous = CompatibilityManifest.from_json(frozen_e141_bytes("compatibility_v2_141.json"))
    declarations = tuple(
        manifest.model_copy(
            update={
                "formats": manifest.formats.model_copy(
                    update={
                        "objects": manifest.formats.objects.model_copy(update={"reads": (3, 4)})
                    }
                )
            }
        )
        for manifest in (previous, objects4_manifest())
    )
    state = (
        PRE_OUTBOX_CANCELLATION_FORMAT_STATE
        if after_upgrade
        else PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE
    )
    result = assess_compatibility(declarations[0], declarations[1], state, handoff_protocol=2)
    assert not result.format_compatible
    assert result.code == "write_format_change" and result.domain == "objects"
    assert result.side == ("base" if after_upgrade else "candidate")


@pytest.mark.parametrize("gate", ["current", "frozen_e141"])
@pytest.mark.parametrize("opener", ["ordinary", "existing", "runtime_store", "runtime", "server"])
def test_both_actual_gates_refuse_other_objects_contract_before_any_write_or_recovery(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str, opener: str
) -> None:
    error: type[Exception] = FormatAdmissionError
    code = "store_format_upgrade_required"
    if gate == "current":
        with closing(sqlite3.connect(database)) as db:
            db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
            )
            db.commit()
    else:
        old = frozen_e141_reader()
        # Execute the old gate with current unchanged construction support;
        # this is a source oracle, not a complete old Runtime installation.
        monkeypatch.setattr(store_module, "require_current_format", old.require_current_format)
        error, code = old.FormatAdmissionError, "store_format_unsupported"
    before = snapshot(database)

    def forbidden_recovery(runtime: Runtime) -> None:
        raise AssertionError("A mismatching objects contract must fail before business recovery")

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


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
async def test_stopped_objects3_upgrade_preserves_old_snapshot_and_every_business_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = tmp_path.resolve() / "data" / "muli.sqlite3"
    store = Store.open_runtime(path, default_mode="auto")
    try:
        identity = read_format_evidence(store.db)
        main = identity.main_session
        original_anchor = store.add_message(main, "user", "主人原输入必须保留。", run_id="old-run")
        original_reply = store.add_message(main, "assistant", "已完成的原答复。", run_id="old-run")
        anchor, reply = store.messages(main)
        old_snapshot = {
            "id": "old-snapshot",
            "session_id": main,
            "generation": 1,
            "from_seq": 0,
            "covered_seq": reply["seq"],
            "prefix_digest": digest([anchor, reply]),
            "summary": "原来的单输入摘要。",
            "level": 1,
            "kind": "semantic",
            "anchor": anchor,
            "parent_snapshot_id": None,
        }
        store.put_many(
            [
                ("compaction_snapshot", old_snapshot),
                ("compaction", {**old_snapshot, "id": main, "snapshot_id": old_snapshot["id"]}),
                # Preservation-only records do not claim a live question/task binding.
                ("owner_question", {"id": "old-question", "version": 1, "status": "waiting"}),
                ("task", {"id": "old-task", "status": "paused"}),
            ]
        )
        payload = Conversation.payload(
            main, "尚未领取的主人输入。", source="web", principal="owner", mode="auto"
        )
        accepted, inserted = store.enqueue_input(payload, max_pending=4)
        assert inserted
        with store.db:
            store.db.execute(
                "INSERT INTO channel_outbox(id,name,route,payload,state,next_part,receipts,"
                "next_attempt,created_at,updated_at) "
                "VALUES('old-unknown','onebot','{}','{}','unknown',1,'[ \"native-id\" ]',0,1,1)"
            )
            store.db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
            )
    finally:
        store.close()
    before = stored_rows(path, omit_marker=True)

    def forbidden_recovery_or_send(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A stopped marker upgrade must not recover, execute or send")

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

    async def forbidden_summary(source: str, max_tokens: int) -> str:
        raise AssertionError("Reading an old snapshot must not summarize or rewrite it")

    reopened = Store.open_existing(path)
    compactor = Compactor(
        reopened,
        forbidden_summary,
        lambda: CompactionSettings(enabled=False),
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "unchanged-config",
    )
    try:
        current = read_format_evidence(reopened.db)
        assert current.state == CURRENT_FORMAT_STATE
        assert (current.store_instance, current.main_session) == (
            identity.store_instance,
            identity.main_session,
        )
        projected = compactor.project(main)
        assert projected[0]["compaction_summary"] is True
        assert [item for item in projected if item["role"] == "user"] == [anchor]
        assert reopened.get("message", anchor["id"]) == original_anchor
        assert reopened.get("message", reply["id"]) == original_reply
        assert reopened.get("compaction_snapshot", "old-snapshot") == old_snapshot
        active = reopened.get("compaction", main)
        assert active is not None and "anchors" not in active
        assert reopened.pending_inputs(main) == 1
        assert Conversation(reopened).pending(main)[0]["id"] == accepted["id"]
        assert tuple(
            reopened.db.execute(
                "SELECT state,next_part,receipts FROM channel_outbox WHERE id='old-unknown'"
            ).fetchone()
        ) == ("unknown", 1, '[ "native-id" ]')
    finally:
        await compactor.close()
        reopened.close()
    assert stored_rows(path, omit_marker=True) == before


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_objects3_upgrade_requires_a_stopped_store_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(
        tmp_path.resolve() / "data", state=PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE
    )
    before = snapshot(path)
    upgrade_platform(platform, monkeypatch)
    with StoreConstructionLock(path):
        with pytest.raises(StoreInitializationError, match="^store_initialization_busy$"):
            upgrade(path.parent)
    assert snapshot(path) == before


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("managed", ["explicit_root", "projection", "switch", "transaction"])
def test_objects3_upgrade_refuses_managed_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, managed: str
) -> None:
    path = legacy_database(
        tmp_path.resolve() / "data", state=PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE
    )
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
@pytest.mark.parametrize(
    "source",
    [
        "unmarked",
        "metadata",
        "mixed",
        "objects",
        "database",
        "configuration",
        "ingress",
        "outbox",
        "memory",
        "install_profile",
    ],
)
def test_objects_upgrade_never_guesses_a_mixed_complete_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, source: str
) -> None:
    path = legacy_database(
        tmp_path.resolve() / "data", state=PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE
    )
    with closing(sqlite3.connect(path)) as db:
        if source == "unmarked":
            db.execute("DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,))
        else:
            value = PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE.model_dump(mode="json")
            if source == "metadata":
                value["metadata_version"] = 2
            elif source == "mixed":
                value["formats"].update({"objects": 4, "ingress": 6})
            else:
                value["formats"][source] = 9
            db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (json.dumps(value), FORMAT_STATE_KEY),
            )
        db.commit()
    before = snapshot(path)
    upgrade_platform(platform, monkeypatch)
    with pytest.raises(ValueError):
        upgrade(path.parent)
    assert snapshot(path) == before


@pytest.mark.parametrize("platform", ["win32", "unsupported"])
def test_objects_upgrade_keeps_existing_unsupported_platform_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(tmp_path / "data", state=PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE)
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
