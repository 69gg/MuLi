"""Review regressions: the manager must not predecode a core-owned receipt."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from test_channels import Harness, profile, unused
from test_mobile_channels import onebot_payload

from muli.channels import ChannelError, ChannelsConfig
from muli.channels.manager import encoded


class InboundReadSpy:
    """Record projected columns before SQLite materializes potentially bad TEXT."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.columns: list[tuple[str, ...]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        cursor = self.connection.execute(query, parameters)
        if "channel_inbound" in query.lower() and cursor.description is not None:
            self.columns.append(tuple(column[0] for column in cursor.description))
        return cursor


@pytest.mark.parametrize("case", ["bad-json", "non-object", "oversized", "invalid-utf8"])
async def test_current_control_receipt_reaches_core_without_raw_manager_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(account_id="123")), unused)
    connection = harness.store.db
    message = harness.manager.adapters["onebot"].webhook(onebot_payload())[0]
    assert message.control_text == message.text == "/任务"
    assert message.control_account_id == "123"
    key = hashlib.sha256(encoded(["onebot", "123", message.event_id]).encode()).hexdigest()
    fingerprint = hashlib.sha256(encoded(asdict(message)).encode()).hexdigest()
    budget = harness.attachments.config.settings.limits.max_output_chars
    raw = {
        "bad-json": b"{",
        "non-object": b"null",
        "oversized": encoded({"padding": "x" * (budget * 4 + 1)}).encode(),
        "invalid-utf8": b"\x80",
    }[case]
    with connection:
        connection.execute(
            "INSERT INTO channel_inbound(id,name,created_at,fingerprint,receipt) "
            "VALUES(?,?,1,?,CAST(? AS TEXT))",
            (key, "onebot", fingerprint, raw),
        )

    def snapshot() -> dict[str, Any]:
        row = connection.execute(
            "SELECT id,name,created_at,fingerprint,typeof(receipt) AS receipt_type,"
            "CAST(receipt AS BLOB) AS receipt_bytes FROM channel_inbound WHERE id=?",
            (key,),
        ).fetchone()
        assert row is not None
        return dict(row)

    before = snapshot()
    spy = InboundReadSpy(connection)
    calls: list[dict[str, Any]] = []
    rejection = "核心固定拒绝损坏回执"

    async def core(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        assert kwargs["event_id"] == key
        assert kwargs["message"] == message and kwargs["name"] == "onebot"
        raise ChannelError(rejection, status_code=409)

    harness.manager.control_submit = core
    failure: Exception | None = None
    try:
        with monkeypatch.context() as context:
            context.setattr(harness.store, "db", spy)
            try:
                await harness.manager.receive("onebot", message)
            except Exception as error:
                failure = error
        # Inspect the query projection too: exception wrapping alone must not
        # make an unbounded raw TEXT read appear to satisfy the core boundary.
        assert all("receipt" not in columns for columns in spy.columns), (
            "Manager projected the raw core-owned receipt before final admission"
        )
        assert len(calls) == 1
        assert isinstance(failure, ChannelError) and str(failure) == rejection
        assert failure.status_code == 409
        assert (snapshot() == before) is True
        assert not harness.received and not harness.attachments.list()
        assert connection.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0
    finally:
        await harness.close()
