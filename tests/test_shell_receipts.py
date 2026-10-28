"""Process-free format, byte paging and transactional retention regressions."""

from __future__ import annotations

import base64
import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

import pytest

import muli.shell_receipts as receipts_module
import muli.store as store_module
from muli.shell_protocol import ShellSpec
from muli.shell_receipts import (
    MAX_ENVELOPE_BYTES,
    MAX_METADATA_BYTES,
    MAX_RAW_BYTES,
    RECEIPT_EVENT,
    RECEIPT_KIND,
    ShellReceipts,
    receipt_key,
)
from muli.shell_sessions import ShellSessionError, ShellSessions, _Command, _Shell
from muli.store import SQLITE_MAX_INTEGER, Store, new_id


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    journal = Store(tmp_path / "receipts.sqlite3")
    journal.put("session", {"id": "owner"})
    journal.put("session", {"id": "other"})
    try:
        yield journal
    finally:
        journal.close()


@contextmanager
def closed_commands(
    tmp_path: Path,
    parts: list[tuple[bytes, bytes]],
    *,
    session_id: str = "owner",
    state: str = "completed",
    flavor: str = "sh",
) -> Iterator[ShellSessions]:
    # Synthetic producer states exercise the storage contract only. The separate
    # Runtime journey establishes real command/proof/fence/close behavior.
    directory = tempfile.TemporaryDirectory(dir=tmp_path)
    manager = ShellSessions(
        default_cwd=tmp_path,
        max_sessions=lambda: 8,
        max_output_bytes=lambda: 100000,
        max_command_bytes=lambda: 4096,
        max_run_seconds=lambda: 3,
    )
    try:
        for output, stderr in parts:
            shell = _Shell(
                session_id,
                ShellSpec("fixture-shell", flavor),
                directory,
                str(tmp_path),
                state="closed",
            )
            item = _Command(new_id(), shell, 0, state=state, exit_code=7)
            item.output.append("output", output, 100000)
            item.output.append("stderr", stderr, 100000)
            item.proof.set()
            item.stdout_fence.set()
            item.stderr_fence.set()
            item.done.set()
            manager._retain_command(item)
        manager._closed = True
        yield manager
    finally:
        directory.cleanup()


def read(receipts: ShellReceipts, command_id: str, **options: Any) -> dict[str, Any]:
    return receipts.read("owner", command_id, output_limit=6000, receipt_limit=6000, **options)


@pytest.fixture
def saved(store: Store, tmp_path: Path) -> dict[str, Any]:
    with closed_commands(tmp_path, [(b"a", b"b")]) as manager:
        ShellReceipts(store).archive(manager, max_records=3, output_limit=6000)
        command_id = next(iter(manager._commands))
    record = store.get(RECEIPT_KIND, receipt_key("owner", command_id))
    assert record is not None
    return record


@pytest.mark.parametrize(
    "corruption",
    [
        "future",
        "boolean_version",
        "extra_field",
        "running",
        "false_exit",
        "boolean_count",
        "count_overflow",
        "count_mismatch",
        "gap",
        "stream",
        "base64",
        "empty",
        "noncanonical",
        "epoch",
        "segments",
        "metadata",
    ],
)
def test_unreadable_format_is_rejected_without_a_process_or_partial_content(
    store: Store, saved: dict[str, Any], corruption: str
) -> None:
    body = saved["output"]
    if corruption == "future":
        saved["version"] = 2
    elif corruption == "boolean_version":
        saved["version"] = True
    elif corruption == "extra_field":
        saved["secret"] = "must not be accepted"
    elif corruption == "running":
        saved["metadata"]["state"] = "running"
    elif corruption == "false_exit":
        saved["metadata"]["state"] = "unknown"
    elif corruption == "boolean_count":
        body["retained"] = True
    elif corruption == "count_overflow":
        body["observed"] = SQLITE_MAX_INTEGER + 1
    elif corruption == "count_mismatch":
        body["discarded"] = 1
    elif corruption == "gap":
        body["chunks"][0][0] = 1
    elif corruption == "stream":
        body["chunks"][0][1] = "stdout"
    elif corruption == "base64":
        body["chunks"][0][2] = "!!!="
    elif corruption == "empty":
        body["chunks"][0][2] = ""
    elif corruption == "noncanonical":
        body["chunks"][0][2] = "YR=="
    elif corruption == "epoch":
        saved["metadata"]["os_epoch"] = "not-an-epoch"
    elif corruption == "segments":
        body["chunks"] *= 2
    else:
        saved["metadata"]["unexpected"] = "field"
    store.put(RECEIPT_KIND, saved)
    with pytest.raises(ShellSessionError) as caught:
        read(ShellReceipts(store), saved["command_id"])
    assert caught.value.code == "content_unavailable"
    assert store.get(RECEIPT_KIND, saved["id"]) == saved


def test_sql_envelope_gate_precedes_json_materialization(
    store: Store, saved: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=CAST(zeroblob(?) AS TEXT) WHERE kind=? AND id=?",
            (MAX_ENVELOPE_BYTES + 1, RECEIPT_KIND, saved["id"]),
        )

    def forbidden(data: bytes) -> dict[str, Any]:
        raise AssertionError("SQL 超限对象不得进入 JSON 解析器")

    monkeypatch.setattr(store_module, "_object", forbidden)
    with pytest.raises(ShellSessionError) as caught:
        read(ShellReceipts(store), saved["command_id"])
    assert caught.value.code == "content_unavailable"


def test_duplicate_json_fields_are_not_silently_reinterpreted(
    store: Store, saved: dict[str, Any]
) -> None:
    raw = json.dumps(saved, ensure_ascii=False).replace(
        '"version": 1', '"version": 2, "version": 1'
    )
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=? WHERE kind=? AND id=?", (raw, RECEIPT_KIND, saved["id"])
        )
    with pytest.raises(ShellSessionError) as caught:
        read(ShellReceipts(store), saved["command_id"])
    assert caught.value.code == "content_unavailable"


def test_sql_metadata_gate_precedes_python_json_materialization(
    store: Store, saved: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved["metadata"]["cwd"] = "x" * (MAX_METADATA_BYTES + 1)
    store.put(RECEIPT_KIND, saved)

    def forbidden(data: bytes) -> dict[str, Any]:
        raise AssertionError("SQL 超限元数据不得进入 Python JSON 解析器")

    monkeypatch.setattr(store_module, "_object", forbidden)
    with pytest.raises(ShellSessionError) as caught:
        read(ShellReceipts(store), saved["command_id"])
    assert caught.value.code == "content_unavailable"


def test_archive_metadata_bound_precedes_base64_allocation(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(data: bytes) -> NoReturn:
        raise AssertionError("超限元数据不得先构造 Base64 输出")

    with closed_commands(tmp_path, [(b"observed", b"")]) as manager:
        item = next(iter(manager._commands.values()))
        item.shell.cwd = "x" * (MAX_METADATA_BYTES + 1)
        monkeypatch.setattr(receipts_module.base64, "b64encode", forbidden)
        with pytest.raises(ValueError, match="format envelope"):
            ShellReceipts(store).archive(manager, max_records=3, output_limit=6000)
    assert (
        store.db.execute("SELECT count(*) FROM objects WHERE kind=?", (RECEIPT_KIND,)).fetchone()[0]
        == 0
    )


def test_raw_format_cap_is_checked_before_base64_decoding(
    store: Store, saved: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    count = MAX_RAW_BYTES + 1
    saved["output"] = {
        "observed": count,
        "retained": count,
        "discarded": 0,
        "observed_by_stream": {"output": count, "stderr": 0},
        "discarded_by_stream": {"output": 0, "stderr": 0},
        "chunks": [[0, "output", base64.b64encode(b"a" * count).decode("ascii")]],
    }
    store.put(RECEIPT_KIND, saved)

    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("超限 raw count 不得进入 Base64 解码器")

    monkeypatch.setattr(receipts_module.base64, "b64decode", forbidden)
    with pytest.raises(ShellSessionError) as caught:
        read(ShellReceipts(store), saved["command_id"])
    assert caught.value.code == "content_unavailable"


@pytest.mark.parametrize(
    "cursor", [{"output": True, "stderr": 0}, {"output": 2, "stderr": 0}, {"output": 0}]
)
def test_stored_cursor_validation_keeps_the_live_contract(
    store: Store, saved: dict[str, Any], cursor: dict[str, Any]
) -> None:
    with pytest.raises(ShellSessionError) as caught:
        read(ShellReceipts(store), saved["command_id"], cursor=cursor)
    assert caught.value.code == "invalid_cursor"


def test_other_session_cannot_read_the_same_command_id(store: Store, saved: dict[str, Any]) -> None:
    with pytest.raises(ShellSessionError) as caught:
        ShellReceipts(store).read(
            "other", saved["command_id"], output_limit=6000, receipt_limit=6000
        )
    assert caught.value.code == "not_found"


def test_smaller_current_cap_trims_old_large_record_in_original_cross_stream_order(
    store: Store, tmp_path: Path
) -> None:
    receipts = ShellReceipts(store)
    with closed_commands(tmp_path, [(b"", b"")]) as manager:
        item = next(iter(manager._commands.values()))
        item.output.append("output", b"before" * 80, 6000)
        item.output.append("stderr", b"middle" * 50, 6000)
        item.output.append("output", "中文尾".encode() * 100, 6000)
        receipts.archive(manager, max_records=3, output_limit=6000)
    before = store.get(RECEIPT_KIND, receipt_key("owner", item.id))
    assert len(json.dumps(before)) > 1000
    page = receipts.read("owner", item.id, output_limit=1000, receipt_limit=1200)
    assert page["output"] == "中文尾" * 100
    assert page["stderr"].encode() == (b"middle" * 50)[-100:]
    assert page["observed_bytes"] == 1680 and page["discarded_bytes"] == 680
    assert page["truncated"] is True
    assert page["next_cursor"] == {"output": 1380, "stderr": 300}
    assert store.get(RECEIPT_KIND, receipt_key("owner", item.id)) == before


@pytest.mark.parametrize("flavor", ["sh", "pwsh", "cmd"])
def test_stored_reader_flushes_both_partial_utf8_tails_with_original_byte_cursors(
    store: Store, tmp_path: Path, flavor: str
) -> None:
    with closed_commands(tmp_path, [(b"A\xe4\xb8", b"B\xf0\x9f")], flavor=flavor) as manager:
        ShellReceipts(store).archive(manager, max_records=3, output_limit=6000)
        command_id = next(iter(manager._commands))
    page = read(ShellReceipts(store), command_id)
    assert page["output"] == "A�" and page["stderr"] == "B�"
    assert page["next_cursor"] == {"output": 3, "stderr": 3}
    assert page["state"] == "completed" and page["exit_code"] == 7
    assert page["status_basis"] == ("powershell_success" if flavor == "pwsh" else "shell_status")
    empty = read(ShellReceipts(store), command_id, cursor=page["next_cursor"])
    assert empty["output"] == empty["stderr"] == ""
    assert empty["next_cursor"] == page["next_cursor"]


def test_global_retention_uses_event_order_across_equal_and_reversed_clocks(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipts = ShellReceipts(store)
    monkeypatch.setattr(store_module, "now", lambda: 100)
    with closed_commands(tmp_path, [(b"first", b"")]) as manager:
        first = next(iter(manager._commands))
        receipts.archive(manager, max_records=2, output_limit=6000)
    monkeypatch.setattr(store_module, "now", lambda: 99)
    with closed_commands(tmp_path, [(b"second", b""), (b"third", b"")]) as manager:
        second, third = tuple(manager._commands)
        receipts.archive(manager, max_records=2, output_limit=6000)
    with pytest.raises(ShellSessionError, match="没有可读"):
        read(receipts, first)
    second_event = store.get(RECEIPT_KIND, receipt_key("owner", second))["event_id"]
    assert read(receipts, second)["output"] == "second"
    assert store.get(RECEIPT_KIND, receipt_key("owner", second))["event_id"] == second_event
    monkeypatch.setattr(store_module, "now", lambda: 1)
    with closed_commands(tmp_path, [(b"fourth", b"")], session_id="other") as manager:
        fourth = next(iter(manager._commands))
        receipts.archive(manager, max_records=2, output_limit=6000)
    assert store.get(RECEIPT_KIND, receipt_key("owner", second)) is None
    assert read(receipts, third)["output"] == "third"
    assert (
        receipts.read("other", fourth, output_limit=6000, receipt_limit=6000)["output"] == "fourth"
    )
    with closed_commands(tmp_path, []) as manager:
        receipts.archive(manager, max_records=1, output_limit=6000)
    assert (
        store.db.execute("SELECT count(*) FROM objects WHERE kind=?", (RECEIPT_KIND,)).fetchone()[0]
        == 2
    )
    with closed_commands(tmp_path, [(b"fifth", b"")]) as manager:
        fifth = next(iter(manager._commands))
        receipts.archive(manager, max_records=1, output_limit=6000)
    assert read(receipts, fifth)["output"] == "fifth"
    assert (
        store.db.execute("SELECT count(*) FROM objects WHERE kind=?", (RECEIPT_KIND,)).fetchone()[0]
        == 1
    )
    assert (
        store.db.execute("SELECT count(*) FROM events WHERE type=?", (RECEIPT_EVENT,)).fetchone()[0]
        == 1
    )


def test_archive_failure_rolls_back_records_events_and_eviction(
    store: Store, saved: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = store._put
    count = 0

    def fail_second(kind: str, item: dict[str, Any]) -> None:
        nonlocal count
        if kind == RECEIPT_KIND:
            count += 1
            if count == 2:
                raise RuntimeError("injected archive write failure")
        original(kind, item)

    monkeypatch.setattr(store, "_put", fail_second)
    with closed_commands(tmp_path, [(b"new-one", b""), (b"new-two", b"")]) as manager:
        with pytest.raises(RuntimeError, match="injected archive"):
            ShellReceipts(store).archive(manager, max_records=1, output_limit=6000)
        for identifier in manager._commands:
            assert store.get(RECEIPT_KIND, receipt_key("owner", identifier)) is None
    assert store.get(RECEIPT_KIND, saved["id"]) == saved
    assert (
        store.db.execute("SELECT count(*) FROM events WHERE type=?", (RECEIPT_EVENT,)).fetchone()[0]
        == 1
    )


def test_pruning_small_invalid_json_clears_only_its_own_producer_event(
    store: Store, saved: dict[str, Any], tmp_path: Path
) -> None:
    unrelated = store.event("other.producer", {"id": saved["id"]}, "owner")
    with store.db:
        store.db.execute(
            "UPDATE objects SET data='{' WHERE kind=? AND id=?", (RECEIPT_KIND, saved["id"])
        )
    with closed_commands(tmp_path, [(b"new", b"")]) as manager:
        ShellReceipts(store).archive(manager, max_records=1, output_limit=6000)
        command_id = next(iter(manager._commands))
    assert read(ShellReceipts(store), command_id)["output"] == "new"
    assert store.get(RECEIPT_KIND, saved["id"]) is None
    assert (
        store.db.execute("SELECT 1 FROM events WHERE id=?", (saved["event_id"],)).fetchone() is None
    )
    assert (
        store.db.execute("SELECT 1 FROM events WHERE id=?", (unrelated["id"],)).fetchone()
        is not None
    )


@pytest.mark.parametrize("state", ["stopped", "unknown", "timed_out"])
def test_noncompleted_terminal_evidence_never_certifies_an_exit(
    store: Store, tmp_path: Path, state: str
) -> None:
    with closed_commands(tmp_path, [(b"observed", b"")], state=state) as manager:
        ShellReceipts(store).archive(manager, max_records=3, output_limit=6000)
        command_id = next(iter(manager._commands))
    page = read(ShellReceipts(store), command_id)
    assert page["state"] == state and page["exit_code"] is None


@pytest.mark.parametrize("missing", ["proof", "stdout_fence", "stderr_fence", "running"])
def test_archive_refuses_unsettled_or_uncertified_completed_snapshot(
    store: Store, tmp_path: Path, missing: str
) -> None:
    with closed_commands(tmp_path, [(b"observed", b"")]) as manager:
        item = next(iter(manager._commands.values()))
        if missing == "running":
            item.state = "running"
        else:
            getattr(item, missing).clear()
        with pytest.raises(ShellSessionError) as caught:
            ShellReceipts(store).archive(manager, max_records=3, output_limit=6000)
        assert caught.value.code == "cleanup_unknown"
    assert (
        store.db.execute("SELECT count(*) FROM objects WHERE kind=?", (RECEIPT_KIND,)).fetchone()[0]
        == 0
    )


def test_delete_transaction_rolls_back_and_deleted_session_cannot_be_archived_again(
    store: Store, tmp_path: Path
) -> None:
    receipts = ShellReceipts(store)
    with closed_commands(tmp_path, [(b"retained", b"")]) as manager:
        command_id = next(iter(manager._commands))
        receipts.archive(manager, max_records=3, output_limit=6000)
        with pytest.raises(RuntimeError, match="delete fault"), store.db:
            receipts.delete_session("owner")
            store.db.execute("DELETE FROM objects WHERE kind='session' AND id='owner'")
            raise RuntimeError("delete fault")
        assert read(receipts, command_id)["output"] == "retained"
        with store.db:
            receipts.delete_session("owner")
            store.db.execute("DELETE FROM objects WHERE kind='session' AND id='owner'")
        receipts.archive(manager, max_records=3, output_limit=6000)
    with pytest.raises(ShellSessionError) as caught:
        read(receipts, command_id)
    assert caught.value.code == "not_found"
    assert store.get(RECEIPT_KIND, receipt_key("owner", command_id)) is None
    assert (
        store.db.execute("SELECT count(*) FROM events WHERE type=?", (RECEIPT_EVENT,)).fetchone()[0]
        == 0
    )


def test_generic_store_reopen_preserves_unknown_feature_version(
    store: Store, saved: dict[str, Any], tmp_path: Path
) -> None:
    saved["version"] = 2
    store.put(RECEIPT_KIND, saved)
    database = tmp_path / "receipts.sqlite3"
    store.close()
    generic_reader = Store(database)
    try:
        assert generic_reader.get("session", "owner") == {"id": "owner"}
        assert generic_reader.get(RECEIPT_KIND, saved["id"]) == saved
        with pytest.raises(ShellSessionError) as caught:
            read(ShellReceipts(generic_reader), saved["command_id"])
        assert caught.value.code == "content_unavailable"
    finally:
        generic_reader.close()
