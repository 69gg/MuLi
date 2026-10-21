from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_management_projection import Host
from test_management_projection import host as host

from muli.candidate_policy import CandidateError, CandidateLimits, CandidateSettings
from muli.candidate_policy_store import CandidatePolicyStore, policy_revision
from muli.config import Settings
from muli.management_projection import project_final
from muli.store import Store

KIND = "update_policy"
ID = "candidate-v1"
LIMIT = 16_384


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    value = Store(tmp_path / "muli.sqlite3")
    try:
        yield value
    finally:
        value.close()


def configured(tmp_path: Path, *, enabled: bool = True) -> CandidateSettings:
    return CandidateSettings(
        enabled=enabled,
        wheelhouse=str(tmp_path / "private wheels"),
        retained_root=str(tmp_path / "retained"),
        uv_executable=str(tmp_path / "uv"),
        disk_mode="strict",
        limits=CandidateLimits(seconds=120.0, preflight_seconds=10.0, cpu_percent=25),
    )


def envelope(policy: CandidateSettings) -> dict[str, Any]:
    return {"id": ID, "schema_version": 1, "policy": policy.model_dump(mode="json")}


def raw_policy(store: Store) -> str | None:
    row = store.db.execute("SELECT data FROM objects WHERE kind=? AND id=?", (KIND, ID)).fetchone()
    return None if row is None else str(row[0])


def put_raw(store: Store, raw: str | bytes) -> None:
    with store.db:
        store.db.execute(
            "INSERT INTO objects(kind,id,data,updated) VALUES(?,?,?,0)", (KIND, ID, raw)
        )


def test_missing_policy_is_disabled_without_writes_or_a_new_connection(store: Store) -> None:
    repository = CandidatePolicyStore(store)
    before = store.db.total_changes
    assert repository.read() == CandidateSettings()
    assert not repository.read().enabled
    assert store.db.total_changes == before
    assert raw_policy(store) is None
    assert store.event_position() == 0


def test_read_failure_does_not_become_a_disabled_default(store: Store) -> None:
    store.db.execute("DROP TABLE objects")
    repository = CandidatePolicyStore(store)
    for action in (repository.read, lambda: repository.replace(CandidateSettings())):
        with pytest.raises(CandidateError, match="^candidate_policy_read_failed$"):
            action()
    assert store.event_position() == 0


def test_complete_policy_roundtrip_and_summary_do_not_change_config(
    store: Store, tmp_path: Path
) -> None:
    # This is the unchanged 2.22 Settings format. Policy is not an extra key in
    # its frozen JSON, so the fixed old controller can still parse it.
    config = tmp_path / "config.json"
    original = Settings().model_dump_json(indent=2).encode()
    config.write_bytes(original)
    before = Settings.model_validate_json(original)
    assert "candidate" not in before.updates.model_dump()
    policy = configured(tmp_path)
    repository = CandidatePolicyStore(store)
    assert repository.replace(policy) == policy
    assert repository.read() == policy
    raw = raw_policy(store)
    assert raw is not None
    assert json.loads(raw) == envelope(policy)
    assert len(raw.encode("utf-8")) <= LIMIT
    assert config.read_bytes() == original
    assert Settings.model_validate_json(config.read_bytes()) == before
    event = store.events()[0]
    assert event["type"] == "update.policy.changed"
    assert event["session_id"] is None
    assert event["data"] == {
        "id": ID,
        "enabled": True,
        "revision": policy_revision(policy),
    }
    assert str(tmp_path) not in json.dumps(event["data"])


def test_marked_database_keeps_its_format_and_reopens_without_migration(tmp_path: Path) -> None:
    database = tmp_path / "marked.sqlite3"
    policy = configured(tmp_path)
    store = Store.open_runtime(database, default_mode="auto", durable=True)
    try:
        state = store.format_state
        assert state is not None
        CandidatePolicyStore(store).replace(policy)
        store.revalidate_format()
        assert store.format_state == state
    finally:
        store.close()
    reopened = Store.open_existing(database, durable=True)
    try:
        assert not reopened.migration_allowed
        assert reopened.format_state == state
        assert CandidatePolicyStore(reopened).read() == policy
    finally:
        reopened.close()


def test_replace_is_complete_and_revision_depends_only_on_validated_policy(
    store: Store, tmp_path: Path
) -> None:
    repository = CandidatePolicyStore(store)
    enabled = configured(tmp_path)
    repository.replace(enabled)
    disabled = CandidateSettings()
    assert repository.replace(disabled) == disabled
    assert repository.read() == disabled
    assert repository.read().wheelhouse == ""
    assert repository.read().limits == CandidateLimits()
    canonical = json.dumps(
        disabled.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    assert policy_revision(disabled) == hashlib.sha256(canonical).hexdigest()
    assert policy_revision(disabled) != policy_revision(enabled)
    repository.replace(disabled)
    assert store.events()[-1]["data"]["revision"] == policy_revision(disabled)


@pytest.mark.parametrize("version", [True, 0, 2, "1", None])
def test_unknown_schema_cannot_be_read_or_overwritten(store: Store, version: Any) -> None:
    item = {**envelope(CandidateSettings()), "schema_version": version}
    put_raw(store, json.dumps(item))
    original = raw_policy(store)
    repository = CandidatePolicyStore(store)
    for action in (repository.read, lambda: repository.replace(CandidateSettings())):
        with pytest.raises(CandidateError, match="^candidate_policy_invalid$"):
            action()
    assert raw_policy(store) == original
    assert store.event_position() == 0


@pytest.mark.parametrize(
    "raw",
    [
        b"not JSON",
        b"[]",
        b"\xff",
        b'{"id":"candidate-v1","schema_version":1,"policy":{},"policy":{}}',
        b'{"id":"candidate-v1","schema_version":1,"policy":{},"future":true}',
        b'{"id":"wrong","schema_version":1,"policy":{}}',
        b'{"id":"candidate-v1","schema_version":1}',
        b'{"id":"candidate-v1","schema_version":1,"policy":[]}',
        b'{"id":"candidate-v1","schema_version":1,"policy":{"enabled":"false"}}',
        b'{"id":"candidate-v1","schema_version":1,"policy":{"unknown":1}}',
        b'{"id":"candidate-v1","schema_version":1,"policy":{"enabled":true}}',
        b'{"id":"candidate-v1","schema_version":1,"policy":{"wheelhouse":"relative"}}',
    ],
)
def test_invalid_fixed_object_is_not_reset(store: Store, raw: bytes) -> None:
    put_raw(store, raw)
    repository = CandidatePolicyStore(store)
    before = store.db.total_changes
    for action in (repository.read, lambda: repository.replace(CandidateSettings())):
        with pytest.raises(CandidateError, match="^candidate_policy_invalid$"):
            action()
    assert store.db.total_changes == before
    assert store.event_position() == 0
    assert (
        store.db.execute("SELECT data FROM objects WHERE kind=? AND id=?", (KIND, ID)).fetchone()[0]
        == raw
    )


def test_oversized_read_uses_only_the_fixed_bounded_object(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_raw(store, b" " * (LIMIT + 1))

    def unbounded_get(kind: str, identifier: str) -> dict[str, Any] | None:
        raise AssertionError("policy must not call Store.get")

    monkeypatch.setattr(store, "get", unbounded_get)
    statements: list[str] = []
    store.db.set_trace_callback(statements.append)
    try:
        with pytest.raises(CandidateError, match="^candidate_policy_budget$"):
            CandidatePolicyStore(store).read()
    finally:
        store.db.set_trace_callback(None)
    assert len(statements) == 1
    assert "substr(CAST(data AS BLOB),1,16385)" in statements[0]
    assert "kind='update_policy' AND id='candidate-v1'" in statements[0]
    assert store.event_position() == 0


@pytest.mark.parametrize("tail", ["😀" * 1500, "中" * 2000, '"' * 2800])
def test_write_budget_counts_utf8_and_json_escaping(
    store: Store, tmp_path: Path, tail: str
) -> None:
    policy = CandidateSettings(
        wheelhouse=str(tmp_path / tail),
        retained_root=str(tmp_path / tail),
        uv_executable=str(tmp_path / tail),
    )
    repository = CandidatePolicyStore(store)
    with pytest.raises(CandidateError, match="^candidate_policy_budget$"):
        repository.replace(policy)
    assert repository.read() == CandidateSettings()
    assert raw_policy(store) is None
    assert store.event_position() == 0


def test_all_three_maximum_length_ascii_paths_fit_the_record_budget(
    store: Store, tmp_path: Path
) -> None:
    prefix = str(tmp_path / "x")[:-1]
    path = prefix + "x" * (4096 - len(prefix))
    policy = CandidateSettings(wheelhouse=path, retained_root=path, uv_executable=path)
    repository = CandidatePolicyStore(store)
    assert repository.replace(policy) == policy
    assert repository.read() == policy
    raw = raw_policy(store)
    assert raw is not None and len(raw.encode("utf-8")) <= LIMIT


def test_unvalidated_overlong_path_is_rejected_again_before_serialization(store: Store) -> None:
    invalid = CandidateSettings().model_copy(update={"wheelhouse": "/" + "x" * 4096})
    with pytest.raises(CandidateError, match="^candidate_policy_invalid$"):
        CandidatePolicyStore(store).replace(invalid)
    assert raw_policy(store) is None
    assert store.event_position() == 0


def test_unvalidated_model_copy_does_not_bypass_policy_validation(store: Store) -> None:
    invalid = CandidateSettings().model_copy(
        update={"limits": CandidateLimits().model_copy(update={"seconds": 181.0})}
    )
    with pytest.raises(CandidateError, match="^candidate_policy_invalid$"):
        CandidatePolicyStore(store).replace(invalid)
    assert raw_policy(store) is None
    assert store.event_position() == 0


def test_failed_event_rolls_back_policy_and_summary(store: Store, tmp_path: Path) -> None:
    repository = CandidatePolicyStore(store)
    repository.replace(CandidateSettings())
    before = raw_policy(store)
    position = store.event_position()
    store.db.execute(
        "CREATE TRIGGER reject_policy_event BEFORE INSERT ON events "
        "WHEN NEW.type='update.policy.changed' "
        "BEGIN SELECT RAISE(ABORT,'fixture injected event failure'); END"
    )
    with pytest.raises(CandidateError, match="^candidate_policy_write_failed$"):
        repository.replace(configured(tmp_path))
    assert not store.db.in_transaction
    assert raw_policy(store) == before
    assert store.event_position() == position
    assert repository.read() == CandidateSettings()


def test_outer_transaction_is_neither_committed_nor_rolled_back(store: Store) -> None:
    store._put("unrelated", {"id": "pending", "value": "caller write"})
    assert store.db.in_transaction
    repository = CandidatePolicyStore(store)
    assert repository.read() == CandidateSettings()
    with pytest.raises(CandidateError, match="^candidate_policy_outer_transaction$"):
        repository.replace(CandidateSettings())
    assert store.db.in_transaction
    assert store.get("unrelated", "pending") is not None
    store.db.rollback()
    assert store.get("unrelated", "pending") is None
    assert raw_policy(store) is None


@pytest.mark.parametrize("mode", ["autocommit", "isolation_none"])
def test_transaction_mode_cannot_make_event_writes_nonatomic(store: Store, mode: str) -> None:
    if mode == "autocommit":
        store.db.autocommit = True
    else:
        store.db.isolation_level = None
    with pytest.raises(CandidateError, match="^candidate_policy_transaction_mode_unsupported$"):
        CandidatePolicyStore(store).replace(CandidateSettings())
    assert raw_policy(store) is None
    assert store.event_position() == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Existing Linux journal projection fixture")
def test_legacy_final_projection_preserves_unknown_policy_object(host: Host) -> None:
    # The 2.22 projection only updates its plan and fixed receipt. A future
    # policy schema is opaque to it, including during repeated projection.
    future = json.dumps(
        {
            "id": ID,
            "schema_version": 2,
            "policy": {"future": "preserve this unknown shared-DB management object"},
        }
    )
    put_raw(host.store, future)
    host.resolve("committed")
    first = project_final(host.permit, host.store)
    assert first is not None
    assert project_final(host.permit, host.store) == first
    assert raw_policy(host.store) == future
    assert (
        len([event for event in host.store.events() if event["type"] == "management.completed"])
        == 1
    )
    with pytest.raises(CandidateError, match="^candidate_policy_invalid$"):
        CandidatePolicyStore(host.store).read()
