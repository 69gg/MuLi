from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from muli.config import Settings
from muli.mcp_policy import (
    DEFAULT_MESSAGE_BYTES,
    MAX_MESSAGE_BYTES,
    MIN_MESSAGE_BYTES,
    POLICY_BYTES,
    ReceivePolicy,
    ReceivePolicyError,
    ReceivePolicyStore,
    policy_revision,
)
from muli.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    value = Store.open_runtime(tmp_path / "muli.sqlite3", default_mode="auto", durable=True)
    try:
        yield value
    finally:
        value.close()


def raw(store: Store) -> Any:
    row = store.db.execute(
        "SELECT data FROM objects WHERE kind='mcp_policy' AND id='receive-v1'"
    ).fetchone()
    return None if row is None else row[0]


def insert(store: Store, data: str | bytes) -> None:
    with store.db:
        store.db.execute(
            "INSERT INTO objects(kind,id,data,updated) VALUES('mcp_policy','receive-v1',?,0)",
            (data,),
        )


def test_absent_default_is_read_only_and_does_not_enter_settings(store: Store) -> None:
    settings = Settings().model_dump_json()
    before = store.db.total_changes
    assert ReceivePolicyStore(store).read().max_message_bytes == DEFAULT_MESSAGE_BYTES
    assert store.db.total_changes == before and raw(store) is None
    assert "max_message_bytes" not in json.loads(settings)
    assert Settings.model_validate_json(settings).model_dump_json() == settings


@pytest.mark.parametrize("size", [MIN_MESSAGE_BYTES, DEFAULT_MESSAGE_BYTES, MAX_MESSAGE_BYTES])
def test_replace_roundtrip_and_event_are_atomic_without_schema_changes(
    store: Store, size: int
) -> None:
    repository = ReceivePolicyStore(store)
    before = store.db.execute("SELECT name,type,sql FROM sqlite_schema ORDER BY name").fetchall()
    state = store.format_state
    policy = ReceivePolicy(max_message_bytes=size)
    assert repository.replace(policy) == repository.read() == policy
    assert json.loads(raw(store)) == {
        "id": "receive-v1",
        "schema_version": 1,
        "policy": {"max_message_bytes": size},
    }
    assert store.events()[-1]["data"] == {
        "id": "receive-v1",
        "max_message_bytes": size,
        "revision": policy_revision(policy),
    }
    store.revalidate_format()
    assert store.format_state == state
    assert (
        store.db.execute("SELECT name,type,sql FROM sqlite_schema ORDER BY name").fetchall()
        == before
    )


@pytest.mark.parametrize("size", [True, "65536", MIN_MESSAGE_BYTES - 1, MAX_MESSAGE_BYTES + 1])
def test_unvalidated_or_coerced_policy_is_rejected(store: Store, size: Any) -> None:
    invalid = ReceivePolicy().model_copy(update={"max_message_bytes": size})
    with pytest.raises(ReceivePolicyError, match="^mcp_policy_invalid$"):
        ReceivePolicyStore(store).replace(invalid)
    assert raw(store) is None


@pytest.mark.parametrize(
    "data",
    [
        b"not JSON",
        b"[]",
        b"\xff",
        b'{"id":"receive-v1","schema_version":true,"policy":{}}',
        b'{"id":"receive-v1","schema_version":2,"policy":{}}',
        b'{"id":"receive-v1","schema_version":1,"policy":{"future":1}}',
        b'{"id":"receive-v1","schema_version":1,"policy":{},"policy":{}}',
    ],
)
def test_bad_or_future_object_is_not_defaulted_or_overwritten(store: Store, data: bytes) -> None:
    insert(store, data)
    position = store.event_position()
    repository = ReceivePolicyStore(store)
    for action in (repository.read, lambda: repository.replace(ReceivePolicy())):
        with pytest.raises(ReceivePolicyError, match="^mcp_policy_invalid$"):
            action()
    assert raw(store) == data and store.event_position() == position


def test_read_limits_raw_json_in_sql_before_parsing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(store, b" " * (POLICY_BYTES + 1))

    def forbidden(*args: Any) -> Any:
        raise AssertionError("Store.get is unbounded")

    monkeypatch.setattr(store, "get", forbidden)
    sql: list[str] = []
    store.db.set_trace_callback(sql.append)
    try:
        with pytest.raises(ReceivePolicyError, match="^mcp_policy_budget$"):
            ReceivePolicyStore(store).read()
    finally:
        store.db.set_trace_callback(None)
    assert len(sql) == 1 and "substr(CAST(data AS BLOB),1,16385)" in sql[0]
    assert "kind='mcp_policy' AND id='receive-v1'" in sql[0]


def test_failed_event_does_not_save_the_policy(store: Store) -> None:
    repository = ReceivePolicyStore(store)
    repository.replace(ReceivePolicy())
    original, position = raw(store), store.event_position()
    store.db.execute(
        "CREATE TRIGGER reject_mcp_policy BEFORE INSERT ON events "
        "WHEN NEW.type='mcp.policy.changed' BEGIN SELECT RAISE(ABORT,'fixture'); END"
    )
    with pytest.raises(ReceivePolicyError, match="^mcp_policy_write_failed$"):
        repository.replace(ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES))
    assert raw(store) == original and store.event_position() == position
    assert not store.db.in_transaction


@pytest.mark.parametrize("mode", ["outer", "autocommit", "isolation_none"])
def test_replacement_does_not_commit_a_callers_transaction(store: Store, mode: str) -> None:
    if mode == "outer":
        store._put("sentinel", {"id": "caller"})
        code = "mcp_policy_outer_transaction"
    elif mode == "autocommit":
        store.db.autocommit = True
        code = "mcp_policy_transaction_mode_unsupported"
    else:
        store.db.isolation_level = None
        code = "mcp_policy_transaction_mode_unsupported"
    with pytest.raises(ReceivePolicyError, match=f"^{code}$"):
        ReceivePolicyStore(store).replace(ReceivePolicy())
    assert raw(store) is None
    if mode == "outer":
        assert store.db.in_transaction
        store.db.rollback()
        assert store.get("sentinel", "caller") is None
    elif mode == "autocommit":
        assert store.db.autocommit is True
    else:
        assert store.db.isolation_level is None


def test_read_failure_is_not_a_default(store: Store) -> None:
    store.close()
    with pytest.raises(ReceivePolicyError, match="^mcp_policy_read_failed$"):
        ReceivePolicyStore(store).read()


def test_unknown_private_policy_is_retained_by_legacy_store(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    store = Store(path)
    insert(store, '{"id":"receive-v1","schema_version":2,"policy":{"future":1}}')
    original = raw(store)
    store.close()
    old = Store(path)
    try:
        assert old.format_state is None
        old.event("unrelated", {}, objects=[("unrelated", {"id": "new"})])
        assert raw(old) == original
    finally:
        old.close()
