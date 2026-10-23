"""Review regressions for byte-bounded, malformed reference identities."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_mobile_task_control import MobileHarness, event, metadata, short
from test_mobile_task_control import mobile as mobile
from test_task_control import rig as rig


@pytest.mark.parametrize("column", ["name", "fingerprint", "id"])
def test_bad_utf8_reference_identity_is_a_fixed_rejection_not_driver_failure(
    mobile: MobileHarness,
    column: str,
) -> None:
    status_id = event("review-bad-utf8-reference")
    mobile.receive(status_id, "/任务")
    values = {
        "name": b"\x80",
        "fingerprint": b"\x80" * len(status_id),
        "id": status_id[:-1].encode() + b"\x80",
    }
    damaged = values[column]
    candidate_id = damaged if column == "id" else status_id.encode()
    connection = mobile.rig.store.db
    with connection:
        # Only this parameterized allowlist chooses the column. SQLite TEXT
        # can contain invalid UTF-8; reading these values as BLOB is deliberate.
        connection.execute(
            f"UPDATE channel_inbound SET {column}=CAST(? AS TEXT) WHERE id=?",
            (damaged, status_id),
        )

    def candidate() -> dict[str, Any]:
        row = connection.execute(
            "SELECT CAST(id AS BLOB) AS id_bytes,CAST(name AS BLOB) AS name_bytes,"
            "CAST(fingerprint AS BLOB) AS fingerprint_bytes,"
            "CAST(receipt AS BLOB) AS receipt_bytes,typeof(receipt) AS receipt_type "
            "FROM channel_inbound WHERE CAST(id AS BLOB)=?",
            (candidate_id,),
        ).fetchone()
        assert row is not None
        return dict(row)

    before = candidate()
    task_before = mobile.rig.ledger.get(mobile.rig.identifier)
    inbound_id = event("review-bad-utf8-command-" + column)
    outcome = mobile.receive(inbound_id, f"/暂停 {short(status_id)}")
    assert metadata(outcome)["error"] == "reference_invalid"
    assert not outcome.control_committed and not mobile.submissions
    assert (candidate() == before) is True
    assert (mobile.rig.ledger.get(mobile.rig.identifier) == task_before) is True
    assert mobile.rig.store.pending_inputs(mobile.rig.main) == 0
    saved = connection.execute(
        "SELECT receipt FROM channel_inbound WHERE id=?",
        (inbound_id,),
    ).fetchone()
    assert saved is not None and json.loads(saved[0]) == outcome.receipt
