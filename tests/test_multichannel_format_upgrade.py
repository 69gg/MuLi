from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from test_channel_format_upgrade import legacy_database, snapshot, upgrade

from muli.compatibility import FormatState, FormatVersions
from muli.config import ConfigFile, MemoryWatchSettings
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    PRE_MEMORY_WATCH_FORMAT_STATE,
    FormatAdmissionError,
)
from muli.format_evidence import (
    FORMAT_STATE_KEY,
    FormatEvidenceError,
    read_optional_format_evidence,
)
from muli.store import Store
from muli.store_initialization import StoreInitializationError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux exclusive fixed upgrade")


def format_state(channel_version: int) -> FormatState:
    # These are exact historical contracts, not the current write format.
    return FormatState(
        metadata_version=1,
        formats=FormatVersions(
            database=1,
            objects=channel_version,
            configuration=1,
            ingress=channel_version,
            outbox=channel_version,
            memory=1,
            install_profile=1,
        ),
    )


TARGET = CURRENT_FORMAT_STATE


def set_marker(path: Path, state: FormatState) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "UPDATE runtime_state SET value=? WHERE name=?",
            (state.model_dump_json(), FORMAT_STATE_KEY),
        )
        db.commit()


def predecessor_database(data_dir: Path, version: int) -> Path:
    path = legacy_database(data_dir)
    set_marker(path, format_state(version))
    return path


@pytest.mark.parametrize("source_version", [1, 2, 3], ids=["source1", "source2", "source3"])
def test_normal_reader_requires_upgrade_for_each_known_predecessor(
    tmp_path: Path, source_version: int
) -> None:
    path = predecessor_database(tmp_path / "data", source_version)
    before = snapshot(path)
    with pytest.raises(FormatAdmissionError, match="store_format_upgrade_required"):
        Store.open_runtime(path, default_mode="auto")
    assert snapshot(path) == before


def test_pre_watch_upgrade_preserves_the_existing_configuration_file(tmp_path: Path) -> None:
    path = predecessor_database(tmp_path / "data", 3)
    config_path = tmp_path / "config.json"
    original = b'{\n  "memory": {"enabled": true}\n}\n'
    config_path.write_bytes(original)
    assert upgrade(path.parent)
    assert config_path.read_bytes() == original
    assert ConfigFile(config_path).settings.memory_watches == MemoryWatchSettings()


@pytest.mark.parametrize("source_version", [1, 2, 3], ids=["source1", "source2", "source3"])
def test_cli_upgrades_each_supported_predecessor_directly_to_current_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source_version: int,
) -> None:
    from muli.__main__ import main

    path = predecessor_database(tmp_path / "旧渠道数据", source_version)
    # This is the existing OneBot scope identifier, independent of the new API.
    scope_id = hashlib.sha256(
        json.dumps(["onebot", "fixture-account", "7"], separators=(",", ":")).encode()
    ).hexdigest()
    raw_object = (
        '{ "id" : "kept", "text" : "原JSON字节与渠道ID保留", "scope_id":"' + scope_id + '" }'
    )
    raw_pending = '{ "id":"old-pending", "text":"待处理输入", "legacy":true }'
    raw_receipt = '{ "accepted":true, "queued":false, "run_id":"old-run" }'
    with closing(sqlite3.connect(path)) as db:
        evidence = read_optional_format_evidence(db)
        assert evidence is not None and evidence.state == format_state(source_version)
        db.execute(
            "INSERT INTO objects(kind,id,data,updated) VALUES('fixture','kept',?,1)",
            (raw_object,),
        )
        if source_version == 2:
            # An opaque historical ownership row is preserved, not re-authorized.
            ownership = json.dumps(
                {
                    "id": scope_id,
                    "channel": "onebot",
                    "account_id": "fixture-account",
                    "owner_id": "7",
                    "main_id": evidence.main_session,
                    "agent_id": "historical-child",
                    "state": "main",
                    "epoch": 2,
                },
                ensure_ascii=False,
                indent=1,
            )
            db.execute(
                "INSERT INTO objects(kind,id,data,updated) VALUES('channel_ownership',?,?,1)",
                (scope_id, ownership),
            )
        db.executemany(
            "INSERT INTO channel_inbound(id,name,receipt,created_at,fingerprint) "
            "VALUES(?,'onebot',?,1,?)",
            [
                ("null-receipt-event", None, "null-fingerprint"),
                ("saved-receipt-event", raw_receipt, "saved-fingerprint"),
            ],
        )
        db.execute(
            "INSERT INTO ingress(id,session_id,state,payload,received) VALUES(?,?,?,?,1)",
            ("old-pending", evidence.main_session, "pending", raw_pending),
        )
        db.execute(
            "INSERT INTO channel_outbox(id,name,route,payload,state,next_attempt,created_at,updated_at) "
            "VALUES('unknown-delivery','onebot',?,?,'unknown',0,1,1)",
            ('{ "kind":"private", "user_id":"7" }', '{ "text":"结果未知，不能重发" }'),
        )
        db.commit()
        before_identity = evidence.store_instance, evidence.main_session
    before = snapshot(path, omit_marker=True)
    monkeypatch.setattr(
        sys, "argv", ["muli", "--data-dir", str(path.parent), "upgrade-channel-ownership"]
    )

    main()

    assert json.loads(capsys.readouterr().out) == {"upgraded": True}
    with closing(sqlite3.connect(path)) as db:
        after = read_optional_format_evidence(db)
        assert after is not None and after.state == TARGET
        assert (after.store_instance, after.main_session) == before_identity
        assert db.execute(
            "SELECT CAST(data AS BLOB) FROM objects WHERE kind='fixture' AND id='kept'"
        ).fetchone() == (raw_object.encode(),)
        assert db.execute("SELECT receipt FROM channel_inbound ORDER BY id").fetchall() == [
            (None,),
            (raw_receipt,),
        ]
        if source_version == 2:
            assert db.execute(
                "SELECT id FROM objects WHERE kind='channel_ownership'"
            ).fetchall() == [(scope_id,)]
    assert snapshot(path, omit_marker=True) == before
    store = Store.open_runtime(path, default_mode="auto")
    try:
        assert store.format_state == TARGET
    finally:
        store.close()
    assert snapshot(path, omit_marker=True) == before

    main()

    assert json.loads(capsys.readouterr().out) == {"upgraded": False}
    assert snapshot(path, omit_marker=True) == before


@pytest.mark.parametrize("key_storage", ["text", "blob"])
def test_format_two_rejects_unknown_reserved_marker_without_changing_rows(
    tmp_path: Path, key_storage: str
) -> None:
    path = predecessor_database(tmp_path / "data", 2)
    key: str | bytes = "format_state_v2" if key_storage == "text" else b"format_state_v2"
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO runtime_state(name,value) VALUES(?,?)", (key, "unknown"))
        db.commit()
        assert db.execute(
            "SELECT typeof(name) FROM runtime_state WHERE name=?", (key,)
        ).fetchone() == (key_storage,)
    before = snapshot(path)
    with pytest.raises(FormatEvidenceError, match="format_marker_unknown"):
        upgrade(path.parent)
    assert snapshot(path) == before


def test_format_two_mixed_with_target_domain_is_not_a_supported_predecessor(tmp_path: Path) -> None:
    path = predecessor_database(tmp_path / "data", 2)
    previous = format_state(2)
    mixed = previous.model_copy(
        update={"formats": previous.formats.model_copy(update={"objects": 3})}
    )
    set_marker(path, mixed)
    before = snapshot(path)
    with pytest.raises(FormatAdmissionError, match="store_format_upgrade_source_unsupported"):
        upgrade(path.parent)
    assert snapshot(path) == before


@pytest.mark.parametrize("source_version", [2, 3])
def test_upgrade_cannot_pass_a_live_runtime_construction_lock(
    tmp_path: Path, source_version: int
) -> None:
    path = tmp_path / "data" / "muli.sqlite3"
    live = Store.open_runtime(path, default_mode="auto")
    try:
        # Retain the actual runtime's lifetime lock while staging the old marker.
        # No old-version process or admission is simulated by a fake lock object.
        set_marker(path, format_state(source_version))
        before = snapshot(path)
        with pytest.raises(StoreInitializationError, match="store_initialization_busy"):
            upgrade(path.parent)
        assert snapshot(path) == before
    finally:
        live.close()


@pytest.mark.parametrize("source_version", [2, 3])
def test_commit_denial_rolls_back_without_rewriting_business_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_version: int
) -> None:
    path = predecessor_database(tmp_path / "data", source_version)
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

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", guarded)
        with pytest.raises(sqlite3.DatabaseError):
            upgrade(path.parent)
    assert commits == [True]
    assert snapshot(path) == before


@pytest.mark.parametrize("changed_domain", ["configuration", "ingress"])
def test_partial_memory_watch_contract_is_not_a_supported_predecessor(
    tmp_path: Path, changed_domain: str
) -> None:
    path = predecessor_database(tmp_path / "data", 3)
    mixed = PRE_MEMORY_WATCH_FORMAT_STATE.model_copy(
        update={
            "formats": PRE_MEMORY_WATCH_FORMAT_STATE.formats.model_copy(
                update={changed_domain: getattr(TARGET.formats, changed_domain)}
            )
        }
    )
    set_marker(path, mixed)
    before = snapshot(path)
    with pytest.raises(FormatAdmissionError, match="store_format_upgrade_source_unsupported"):
        upgrade(path.parent)
    assert snapshot(path) == before
