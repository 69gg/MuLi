"""Signed candidate readers define the explicit memory4 -> memory5 boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_channel_format_upgrade import legacy_database, upgrade
from test_channel_format_upgrade import snapshot as stored_rows
from test_fresh_format_admission import database as database
from test_fresh_format_admission import deny_construction_writes, no_model, snapshot

from muli.attachment_memory_source import attachment_source, restore_attachment_source
from muli.commitments import Commitments
from muli.compatibility import CompatibilityManifest, assess_compatibility
from muli.config import ConfigFile
from muli.conversation import Conversation
from muli.document_reading import document_revision
from muli.events import EventBus
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    PRE_CHANNEL_REFERENCE_FORMAT_STATE,
    PRE_REPLY_MEMORY_FORMAT_STATE,
    FormatAdmissionError,
)
from muli.format_evidence import FORMAT_STATE_KEY, read_format_evidence
from muli.memory import Memory
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import Store
from muli.store_initialization import StoreConstructionLock, StoreInitializationError
from muli.store_schema import ExistingStoreError
from muli.webpage_snapshot import compose_webpage_snapshot

_FIXTURES = Path(__file__).parent / "fixtures"
# Root verified signed candidate 12efd07f827e09d2de60bb7fff5170cc4e7d595f.
# The source freeze does not assert that 2.137.0 has been published.
_E137_BYTES = {
    "format_admission_v2_137.py": (
        "173e1174cc0ec485fe874db9d538212955a7a5cfc554c5708d0e6453531bb42e"
    ),
    "compatibility_v2_137.json": (
        "0a27ea9c06a6830f12f655dd1463da54c2a73645d509aa9f37a14f30ed014d23"
    ),
}
POSIX = pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX stopped upgrade")


def frozen_e137_bytes(name: str) -> bytes:
    raw = (_FIXTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _E137_BYTES[name]
    return raw


def frozen_e137_reader() -> ModuleType:
    frozen_e137_bytes("format_admission_v2_137.py")
    path = _FIXTURES / "format_admission_v2_137.py"
    spec = importlib.util.spec_from_file_location("muli_frozen_format_admission_v2_137", path)
    assert spec is not None and spec.loader is not None
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    return reader


def test_e137_actual_reader_refuses_reply_memory_before_business_writes(database: Path) -> None:
    old = frozen_e137_reader()
    before = snapshot(database)
    with closing(sqlite3.connect(database)) as db:
        evidence = read_format_evidence(db)
        with pytest.raises(old.FormatAdmissionError, match="^store_format_unsupported$"):
            old.require_current_format(evidence)
        assert db.total_changes == 0
    assert snapshot(database) == before


@pytest.mark.parametrize("after_upgrade", [False, True], ids=["memory4", "memory5"])
def test_e137_manifest_refuses_reply_memory_same_format_switch(after_upgrade: bool) -> None:
    old = frozen_e137_reader()
    previous = CompatibilityManifest.from_json(frozen_e137_bytes("compatibility_v2_137.json"))
    current = CompatibilityManifest.from_json(
        (_FIXTURES / "compatibility_v2_139.json").read_bytes()
    )
    assert previous.release == "2.137.0"
    assert current.release == "2.139.0"
    assert old.CURRENT_FORMAT_STATE == PRE_REPLY_MEMORY_FORMAT_STATE
    assert previous.formats.memory.reads == (4,) and previous.formats.memory.writes == 4
    assert current.formats.memory.reads == (5,) and current.formats.memory.writes == 5
    # Keep the historical memory-only boundary fixed as CURRENT gains later ingress contracts.
    assert PRE_CHANNEL_REFERENCE_FORMAT_STATE.formats.model_dump() == {
        **old.CURRENT_FORMAT_STATE.formats.model_dump(),
        "memory": 5,
    }
    state = PRE_CHANNEL_REFERENCE_FORMAT_STATE if after_upgrade else PRE_REPLY_MEMORY_FORMAT_STATE
    result = assess_compatibility(previous, current, state, handoff_protocol=2)
    assert not result.format_compatible
    assert result.code == "current_format_unreadable" and result.domain == "memory"
    assert result.side == ("base" if after_upgrade else "candidate")
    assert assess_compatibility(
        previous, previous, PRE_REPLY_MEMORY_FORMAT_STATE, handoff_protocol=2
    ).format_compatible
    assert assess_compatibility(
        current, current, PRE_CHANNEL_REFERENCE_FORMAT_STATE, handoff_protocol=2
    ).format_compatible


@pytest.mark.parametrize("after_upgrade", [False, True], ids=["memory4", "memory5"])
def test_reading_both_memory_contracts_cannot_authorize_different_writes(
    after_upgrade: bool,
) -> None:
    previous = CompatibilityManifest.from_json(frozen_e137_bytes("compatibility_v2_137.json"))
    current = CompatibilityManifest.from_json(
        (_FIXTURES / "compatibility_v2_139.json").read_bytes()
    )
    declarations = tuple(
        manifest.model_copy(
            update={
                "formats": manifest.formats.model_copy(
                    update={"memory": manifest.formats.memory.model_copy(update={"reads": (4, 5)})}
                )
            }
        )
        for manifest in (previous, current)
    )
    state = PRE_CHANNEL_REFERENCE_FORMAT_STATE if after_upgrade else PRE_REPLY_MEMORY_FORMAT_STATE
    result = assess_compatibility(declarations[0], declarations[1], state, handoff_protocol=2)
    assert not result.format_compatible
    assert result.code == "write_format_change" and result.domain == "memory"
    assert result.side == ("base" if after_upgrade else "candidate")


def test_exact_memory4_predecessor_requires_explicit_upgrade(database: Path) -> None:
    from muli.format_admission import require_current_format

    old = frozen_e137_reader()
    with closing(sqlite3.connect(database)) as db:
        evidence = read_format_evidence(db)
        previous = replace(evidence, state=PRE_REPLY_MEMORY_FORMAT_STATE)
        old.require_current_format(previous)
        with pytest.raises(FormatAdmissionError, match="^store_format_upgrade_required$"):
            require_current_format(previous)
        require_current_format(evidence)
        assert db.total_changes == 0


@pytest.mark.parametrize("opener", ["ordinary", "existing", "runtime_store", "runtime", "server"])
def test_memory4_is_refused_before_recovery_or_construction_writes(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "UPDATE runtime_state SET value=? WHERE name=?",
            (PRE_REPLY_MEMORY_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
        )
        db.commit()
    before = snapshot(database)

    def forbidden_recovery(runtime: Runtime) -> None:
        raise AssertionError("memory4 must fail before workers or business recovery")

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


def existing_attachment_sources() -> list[dict[str, Any]]:
    """Existing marker shapes remain readable; no reply is fabricated from them."""
    content = "完整既有资料🙂\n保留原作者身份和原创建时间。"
    checksum = hashlib.sha256(content.encode()).hexdigest()
    original_document = hashlib.sha256(b"existing-document-bytes").hexdigest()
    document = {
        "format": "docx",
        "scope": "docx_main_body_text",
        "source_revision": original_document,
        "revision": document_revision(original_document, content.encode()),
    }
    webpage = compose_webpage_snapshot(
        content,
        requested_url="https://example.invalid/requested",
        final_url="https://example.invalid/final",
        captured_at="2026-10-29T08:00:00+00:00",
        scope="http_utf8_text_v1",
    )
    return [
        attachment_source("a" * 32, content, checksum, 12),
        attachment_source("b" * 32, content, original_document, 13, document=document),
        attachment_source("c" * 32, content, checksum, 14, transcript=True),
        attachment_source(
            "d" * 32, webpage, hashlib.sha256(webpage.encode()).hexdigest(), 15, webpage=True
        ),
    ]


def upgrade_platform(platform: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if platform == "linux" and sys.platform != "linux":
        pytest.skip("Linux lock implementation is exercised on Linux")
    if platform == "darwin" and sys.platform != "darwin":
        # Linux fault injection does not constitute native macOS evidence.
        monkeypatch.setattr(sys, "platform", "darwin")


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
async def test_exact_memory4_upgrade_preserves_sources_tombstones_and_all_business_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = tmp_path.resolve() / "data" / "muli.sqlite3"
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    configuration = config.path.read_bytes()
    store = Store.open_runtime(path, default_mode="auto")
    originals = existing_attachment_sources()
    saved: dict[str, dict[str, Any]] = {}
    forgotten = attachment_source(
        "f" * 32,
        originals[0]["content"],
        hashlib.sha256(originals[0]["content"].encode()).hexdigest(),
        16,
    )
    try:
        identity = read_format_evidence(store.db)
        main = identity.main_session
        owner = store.add_message(main, "user", "原主人资料", source="web", principal="owner")
        uncollected = store.add_message(
            main, "assistant", "未明确收藏的旧主答复", source="web", principal="assistant"
        )
        memory = Memory(store)
        owner_source, inserted = memory.register_source(owner, trusted_owner=True)
        assert inserted and owner_source is not None and owner_source["trusted_owner"]
        for original in originals:
            source, inserted = memory.register_source(original, trusted_owner=False)
            assert inserted and source is not None and not source["trusted_owner"]
            saved[source["id"]] = source
        assert memory.register_source(forgotten)[1]
        assert memory.forget_source(forgotten["id"])
        assert memory.source("reply:" + uncollected["id"]) is None
        task = await Commitments(store, EventBus(store)).save(
            "保留原任务", brief={"goal": "升级不重放业务", "constraints": ["原事实完整保留"]}
        )
        receipt, inserted = Conversation(store).enqueue(
            main,
            "原 pending 输入仍需后续正常消费",
            source="web",
            principal="owner",
            mode="auto",
            max_pending=4,
            event_id="before-memory5",
        )
        assert inserted
        with store.db:
            store.db.execute(
                "INSERT INTO memory_jobs(id,source_id,state,attempts,next_attempt,checkpoint,"
                "created_at,updated_at) VALUES(?,?,'done',1,0,?,1,1)",
                ("retained-memory4-job", owner_source["id"], '{"retained":"original-checkpoint"}'),
            )
            store.db.execute(
                "INSERT INTO channel_inbound(id,name,receipt,created_at,fingerprint) "
                "VALUES('retained-memory4-inbound','onebot',NULL,1,'original-fingerprint')"
            )
            store.db.execute(
                "INSERT INTO channel_outbox(id,name,route,payload,state,next_attempt,created_at,"
                "updated_at) VALUES('retained-memory4-unknown','onebot','{}','{}','unknown',0,1,1)"
            )
            store.db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (PRE_REPLY_MEMORY_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
            )
    finally:
        store.close()
    before = stored_rows(path, omit_marker=True)
    upgrade_platform(platform, monkeypatch)
    assert upgrade(path.parent) is True
    assert upgrade(path.parent) is False
    assert stored_rows(path, omit_marker=True) == before
    assert config.path.read_bytes() == configuration
    reopened = Store.open_runtime(path, default_mode="auto")
    try:
        current = read_format_evidence(reopened.db)
        assert current.state == CURRENT_FORMAT_STATE
        assert (current.store_instance, current.main_session) == (
            identity.store_instance,
            identity.main_session,
        )
        memory = Memory(reopened)
        assert memory.source(owner_source["id"]) == owner_source
        for original in originals:
            identifier = original["id"].removeprefix("attachment:")
            assert (
                restore_attachment_source(
                    identifier, original["content"], original["source"], original["created_at"]
                )
                == original
            )
            assert memory.source(original["id"]) == saved[original["id"]]
        assert memory.source(forgotten["id"]) is None
        assert (
            reopened.db.execute(
                "SELECT count(*) FROM memory_forgotten_sources WHERE id=?", (forgotten["id"],)
            ).fetchone()[0]
            == 1
        )
        assert memory.source("reply:" + uncollected["id"]) is None
        assert reopened.get("commitment", task["id"]) == task
        assert reopened.pending_inputs(main) == 1
        pending = Conversation(reopened).pending(main)
        assert len(pending) == 1 and pending[0]["id"] == receipt["id"]
        assert reopened.db.execute("SELECT state FROM channel_outbox").fetchone()[0] == "unknown"
        job = reopened.db.execute("SELECT state,checkpoint FROM memory_jobs").fetchone()
        assert job is not None and tuple(job) == ("done", '{"retained":"original-checkpoint"}')
    finally:
        reopened.close()
    assert stored_rows(path, omit_marker=True) == before
    assert config.path.read_bytes() == configuration


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_memory4_upgrade_refuses_a_live_runtime_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_REPLY_MEMORY_FORMAT_STATE)
    before = snapshot(path)
    upgrade_platform(platform, monkeypatch)
    with StoreConstructionLock(path):
        with pytest.raises(StoreInitializationError, match="^store_initialization_busy$"):
            upgrade(path.parent)
    assert snapshot(path) == before


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("managed", ["explicit_root", "projection", "switch", "transaction"])
def test_memory4_upgrade_refuses_managed_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, managed: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_REPLY_MEMORY_FORMAT_STATE)
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
def test_reply_upgrade_never_guesses_a_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, source: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_REPLY_MEMORY_FORMAT_STATE)
    with closing(sqlite3.connect(path)) as db:
        if source == "unmarked":
            db.execute("DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,))
        else:
            state = PRE_REPLY_MEMORY_FORMAT_STATE.model_copy(
                update={
                    "formats": PRE_REPLY_MEMORY_FORMAT_STATE.formats.model_copy(
                        update={"ingress": 4, "memory": 5} if source == "mixed" else {"memory": 9}
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
def test_memory4_commit_failure_does_not_rewrite_business_or_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_REPLY_MEMORY_FORMAT_STATE)
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
def test_memory4_upgrade_does_not_expand_unsupported_platforms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = legacy_database(tmp_path / "data", state=PRE_REPLY_MEMORY_FORMAT_STATE)
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
