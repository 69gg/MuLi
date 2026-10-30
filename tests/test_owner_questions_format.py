"""The real ingress6 predecessor cannot safely consume owner-question answers."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
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

from muli.channels import ChannelManager
from muli.channels.adapters import Telegram
from muli.compatibility import CompatibilityManifest, assess_compatibility
from muli.config import ConfigFile
from muli.conversation import Conversation
from muli.evidence import EvidenceError
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    PRE_OWNER_QUESTION_FORMAT_STATE,
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
# Root froze and verified Good28cf2153b467bcf0e7f5a44a55eb6aa2f834918e.
# This signed 2.140.0 candidate is not evidence of a published old installation.
_E140_BYTES = {
    "format_admission_v2_140.py": (
        "5517cc6c614e3e5c884d9c2b1263e6d1d4191cea197aa7900052a17b455dd017"
    ),
    "compatibility_v2_140.json": (
        "7607a0611d9f7a0e0bf6f79418766b3d14d2cdd4da13021990b3b0ce6ddbf19b"
    ),
    "previous_input_consumer_v2_140.py.txt": (
        "2ab445fe2ce93e628717544308c84433cc298159e217ceb96b4bc1b0a9fdbf69"
    ),
}
POSIX = pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX stopped upgrade")


def frozen_e140_bytes(name: str) -> bytes:
    raw = (_FIXTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _E140_BYTES[name]
    return raw


def frozen_e140_module(name: str) -> ModuleType:
    frozen_e140_bytes(name)
    path = _FIXTURES / name
    module_name = "muli_frozen_" + name.replace(".", "_")
    loader = SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_file_location(module_name, path, loader=loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def current_manifest() -> CompatibilityManifest:
    path = Path(__file__).parents[1] / "src" / "muli" / "compatibility.json"
    return CompatibilityManifest.from_json(path.read_bytes())


def test_e140_actual_admission_refuses_ingress7_before_business_writes(database: Path) -> None:
    old = frozen_e140_module("format_admission_v2_140.py")
    assert old.CURRENT_FORMAT_STATE == PRE_OWNER_QUESTION_FORMAT_STATE
    before = snapshot(database)
    with closing(sqlite3.connect(database)) as db:
        evidence = read_format_evidence(db)
        old.require_current_format(replace(evidence, state=PRE_OWNER_QUESTION_FORMAT_STATE))
        with pytest.raises(old.FormatAdmissionError, match="^store_format_unsupported$"):
            old.require_current_format(evidence)
        assert db.total_changes == 0
    assert snapshot(database) == before


def test_current_admission_requires_explicit_upgrade_from_actual_ingress6(database: Path) -> None:
    old = frozen_e140_module("format_admission_v2_140.py")
    assert CURRENT_FORMAT_STATE.formats.model_dump() == {
        **old.CURRENT_FORMAT_STATE.formats.model_dump(),
        "ingress": 7,
    }
    assert PRE_OWNER_QUESTION_FORMAT_STATE.formats.model_dump() == {
        "database": 1,
        "objects": 3,
        "configuration": 2,
        "ingress": 6,
        "outbox": 3,
        "memory": 5,
        "install_profile": 1,
    }
    with closing(sqlite3.connect(database)) as db:
        evidence = read_format_evidence(db)
        previous = replace(evidence, state=old.CURRENT_FORMAT_STATE)
        old.require_current_format(previous)
        with pytest.raises(FormatAdmissionError, match="^store_format_upgrade_required$"):
            require_current_format(previous)
        require_current_format(evidence)
        assert db.total_changes == 0


@pytest.mark.parametrize("after_upgrade", [False, True], ids=["ingress6", "ingress7"])
@pytest.mark.parametrize("reverse", [False, True], ids=["upgrade", "downgrade"])
def test_managed_same_format_switch_refuses_both_owner_answer_directions(
    after_upgrade: bool, reverse: bool
) -> None:
    previous = CompatibilityManifest.from_json(frozen_e140_bytes("compatibility_v2_140.json"))
    current = current_manifest()
    assert previous.release == "2.140.0"
    assert previous.formats.ingress.reads == (6,) and previous.formats.ingress.writes == 6
    assert current.formats.ingress.reads == (7,) and current.formats.ingress.writes == 7
    assert current.formats.model_dump(exclude={"ingress"}) == previous.formats.model_dump(
        exclude={"ingress"}
    )
    state = CURRENT_FORMAT_STATE if after_upgrade else PRE_OWNER_QUESTION_FORMAT_STATE
    base, candidate = (current, previous) if reverse else (previous, current)
    result = assess_compatibility(base, candidate, state, handoff_protocol=2)
    assert not result.format_compatible
    assert result.code == "current_format_unreadable" and result.domain == "ingress"
    assert result.side == ("candidate" if after_upgrade == reverse else "base")
    assert assess_compatibility(
        previous, previous, PRE_OWNER_QUESTION_FORMAT_STATE, handoff_protocol=2
    ).format_compatible
    assert assess_compatibility(
        current, current, CURRENT_FORMAT_STATE, handoff_protocol=2
    ).format_compatible


@pytest.mark.parametrize("after_upgrade", [False, True], ids=["ingress6", "ingress7"])
def test_reading_both_ingress_contracts_cannot_authorize_owner_answer_write_changes(
    after_upgrade: bool,
) -> None:
    previous = CompatibilityManifest.from_json(frozen_e140_bytes("compatibility_v2_140.json"))
    declarations = tuple(
        manifest.model_copy(
            update={
                "formats": manifest.formats.model_copy(
                    update={
                        "ingress": manifest.formats.ingress.model_copy(update={"reads": (6, 7)})
                    }
                )
            }
        )
        for manifest in (previous, current_manifest())
    )
    state = CURRENT_FORMAT_STATE if after_upgrade else PRE_OWNER_QUESTION_FORMAT_STATE
    result = assess_compatibility(declarations[0], declarations[1], state, handoff_protocol=2)
    assert not result.format_compatible
    assert result.code == "write_format_change" and result.domain == "ingress"
    assert result.side == ("base" if after_upgrade else "candidate")


def test_e140_methods_ignore_answer_binding_batch_boundaries_and_archive_marker(
    database: Path,
) -> None:
    """Bypass admission deliberately: these three old methods are not an old install."""
    consumer_type: type[Store] = frozen_e140_module(
        "previous_input_consumer_v2_140.py.txt"
    ).PreviousInputConsumer
    store = Store.open_existing(database)
    try:
        main = read_format_evidence(store.db).main_session
        payloads: list[dict[str, Any]] = []
        for number in (1, 2):
            payload = Conversation.payload(
                main,
                f"只回答原问题 {number}",
                source="web",
                principal="owner",
                mode="auto",
                event_id=f"synthetic-owner-answer-{number}",
            )
            payload["owner_question_answer"] = {
                "version": 1,
                "question_id": f"missing-question-{number}",
                "answer_revision": f"missing-answer-{number}",
                "task_id": "missing-task",
                "worker_id": "missing-worker",
                "delivery_key": f"missing-delivery-{number}",
            }
            pending, inserted = store.enqueue_input(payload, max_pending=4)
            assert inserted and pending["state"] == "pending"
            payloads.append(payload)
        assert store.list("owner_question") == []
        before = tuple(store.db.iterdump())
        # The current bare Store lacks its host coordinator and refuses this
        # claim before writing. Full canonical binding is tested at service level.
        with pytest.raises(EvidenceError) as blocked:
            store.claim_inputs(main, "no-coordinator-run", limit=4)
        assert blocked.value.code == "owner_question_unavailable"
        assert tuple(store.db.iterdump()) == before

        old_admission = frozen_e140_module("format_admission_v2_140.py")
        with pytest.raises(old_admission.FormatAdmissionError, match="^store_format_unsupported$"):
            old_admission.require_current_format(read_format_evidence(store.db))
        assert tuple(store.db.iterdump()) == before

        # __new__ is intentional: constructing the old program normally must
        # stop at its admission error above, before this unsafe method path.
        diagnostic = consumer_type.__new__(consumer_type)
        diagnostic.db = store.db
        claimed = diagnostic.claim_inputs(main, "method-only-old-run", limit=4)
        assert [item["id"] for item in claimed] == [item["id"] for item in payloads]
        assert store.pending_inputs(main) == 0
        for payload, archived in zip(payloads, claimed, strict=True):
            assert "owner_question_answer" not in archived
            assert diagnostic.get("message", payload["id"]) == archived
            current = Store.input_archive(payload, received=1.0, run_id="projection-only")
            assert current["owner_question_answer"] == payload["owner_question_answer"]
        assert store.list("owner_question") == []
    finally:
        store.close()


@pytest.mark.parametrize("opener", ["ordinary", "existing", "runtime_store", "runtime", "server"])
def test_ingress6_is_refused_before_recovery_or_construction_writes(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "UPDATE runtime_state SET value=? WHERE name=?",
            (PRE_OWNER_QUESTION_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
        )
        db.commit()
    before = snapshot(database)

    def forbidden_recovery(runtime: Runtime) -> None:
        raise AssertionError("ingress6 must fail before workers or business recovery")

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
def test_stopped_ingress6_upgrade_preserves_every_business_row_without_synthesizing_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = tmp_path.resolve() / "data" / "muli.sqlite3"
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    configuration = config.path.read_bytes()
    store = Store.open_runtime(path, default_mode="auto")
    try:
        identity = read_format_evidence(store.db)
        main = identity.main_session
        originals: list[dict[str, Any]] = []
        for event, text in (
            ("archive", "保留已领取的原始引用"),
            ("pending", "保留未领取的原输入"),
        ):
            payload = Conversation.payload(
                main,
                text,
                source="web",
                principal="owner",
                mode="auto",
                event_id=f"before-ingress7-{event}",
            )
            payload["channel_reference"] = {"version": 1, "status": "unavailable"}
            accepted, inserted = store.enqueue_input(payload, max_pending=4)
            assert inserted
            originals.append(accepted)
            if event == "archive":
                archived = store.claim_inputs(main, "completed-before-ingress7", limit=1)
                assert [message["id"] for message in archived] == [accepted["id"]]
        store.put("fixture", {"id": "unknown-object", "nested": {"preserve": [1, "原字节"]}})
        with store.db:
            store.db.execute(
                "INSERT INTO channel_inbound(id,name,receipt,created_at,fingerprint) "
                "VALUES('old-inbound','onebot',NULL,1,'original-fingerprint')"
            )
            store.db.execute(
                "INSERT INTO channel_outbox(id,name,route,payload,state,next_part,receipts,"
                "next_attempt,created_at,updated_at) "
                "VALUES('old-unknown','onebot','{}','{}','unknown',1,'[ \"native-id\" ]',0,1,1)"
            )
            store.db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (PRE_OWNER_QUESTION_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
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
        assert len(retained) == 1 and retained[0]["id"] == originals[1]["id"]
        archived_message = reopened.get("message", originals[0]["id"])
        assert archived_message is not None
        assert archived_message["channel_reference"] == retained[0]["channel_reference"]
        assert "owner_question_answer" not in archived_message
        assert "owner_question_answer" not in retained[0]
        assert reopened.list("owner_question") == []
        assert reopened.list("owner_question_scope") == []
        assert tuple(
            reopened.db.execute(
                "SELECT state,next_part,receipts FROM channel_outbox WHERE id='old-unknown'"
            ).fetchone()
        ) == ("unknown", 1, '[ "native-id" ]')
    finally:
        reopened.close()
    assert stored_rows(path, omit_marker=True) == before
    assert config.path.read_bytes() == configuration


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_ingress6_upgrade_requires_a_stopped_store_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_OWNER_QUESTION_FORMAT_STATE)
    before = snapshot(path)
    upgrade_platform(platform, monkeypatch)
    with StoreConstructionLock(path):
        with pytest.raises(StoreInitializationError, match="^store_initialization_busy$"):
            upgrade(path.parent)
    assert snapshot(path) == before


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("managed", ["explicit_root", "projection", "switch", "transaction"])
def test_ingress6_upgrade_refuses_managed_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, managed: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_OWNER_QUESTION_FORMAT_STATE)
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
def test_owner_answer_upgrade_does_not_guess_a_complete_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, source: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_OWNER_QUESTION_FORMAT_STATE)
    with closing(sqlite3.connect(path)) as db:
        if source == "unmarked":
            db.execute("DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,))
        else:
            state = PRE_OWNER_QUESTION_FORMAT_STATE.model_copy(
                update={
                    "formats": PRE_OWNER_QUESTION_FORMAT_STATE.formats.model_copy(
                        update={"memory": 4} if source == "mixed" else {"ingress": 8}
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
def test_ingress6_commit_failure_preserves_original_marker_and_business_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_OWNER_QUESTION_FORMAT_STATE)
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
def test_ingress6_upgrade_keeps_existing_unsupported_platform_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(tmp_path / "data", state=PRE_OWNER_QUESTION_FORMAT_STATE)
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
