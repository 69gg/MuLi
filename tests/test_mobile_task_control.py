from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, cast

import pytest
from test_task_control import Rig
from test_task_control import rig as rig

from muli.mobile_task_control import (
    MobileControlOutcome,
    MobileControlRejected,
    MobileControlSource,
    MobileTaskControl,
)
from muli.store import now
from muli.task_control import ControlConflict, ControlResult, OwnerControlRequest, _digest


def test_mobile_cas_rejects_approval_change_without_changing_web_fingerprint(rig: Rig) -> None:
    snapshot = rig.replan()
    rig.approve(snapshot)
    request = OwnerControlRequest("pause", snapshot.epoch, "mobile-old-approval")
    with pytest.raises(ControlConflict, match="epoch_conflict"):
        rig.control.owner_control(
            rig.identifier,
            request,
            expected_approved_epoch=snapshot.approved_epoch,
            main_session_id=rig.main,
            mode="auto",
            model_configured=True,
            max_pending=8,
        )
    assert rig.control.current(rig.identifier)["control"]["epoch"] == snapshot.epoch
    web = rig.command(OwnerControlRequest("pause", snapshot.epoch, "web-unchanged"))
    assert web.receipt["fingerprint"] == _digest(
        {
            "commitment_id": rig.identifier,
            **asdict(OwnerControlRequest("pause", snapshot.epoch, "web-unchanged")),
        }
    )


def test_mobile_domain_duplicate_precedes_both_version_checks(rig: Rig) -> None:
    request = OwnerControlRequest("pause", 0, "mobile-first")

    def mobile() -> ControlResult:
        return rig.control.owner_control(
            rig.identifier,
            request,
            expected_approved_epoch=0,
            main_session_id=rig.main,
            mode="auto",
            model_configured=True,
            max_pending=8,
        )

    first = mobile()
    rig.command(OwnerControlRequest("request_replan", 1, "later-intent", "仅交草稿"))
    current = rig.control.replan_snapshot(rig.main, rig.identifier)
    rig.approve(current)
    duplicate = mobile()
    assert not duplicate.created and duplicate.receipt == first.receipt
    assert duplicate.current["control"]["epoch"] == current.epoch
    assert duplicate.current["control"]["approved_epoch"] == current.epoch
    with pytest.raises(ControlConflict, match="request_id_conflict"):
        rig.command(request)


@dataclass
class MobileHarness:
    rig: Rig
    service: MobileTaskControl
    source: MobileControlSource = MobileControlSource("telegram", "100", "7", "7")
    submissions: list[ControlResult] = field(default_factory=list)
    configured: bool = True
    max_pending: int = 8

    def stage(self, inbound_id: str, source: MobileControlSource | None = None) -> None:
        with self.rig.store.db:
            self.rig.store.db.execute(
                "INSERT OR IGNORE INTO channel_inbound(id,name,created_at,fingerprint) "
                "VALUES(?,?,?,?)",
                (inbound_id, (source or self.source).name, now(), "f" * 64),
            )

    def submit(self, identifier: str, request: OwnerControlRequest, approved: int) -> ControlResult:
        result = self.rig.control.owner_control(
            identifier,
            request,
            expected_approved_epoch=approved,
            main_session_id=self.rig.main,
            mode="auto",
            model_configured=self.configured,
            max_pending=self.max_pending,
        )
        self.submissions.append(result)
        return result

    def receive(
        self, inbound_id: str, text: str, *, source: MobileControlSource | None = None
    ) -> MobileControlOutcome:
        self.stage(inbound_id, source)
        return self.service.handle(
            inbound_id, source or self.source, text, submit_control=self.submit
        )


@pytest.fixture
def mobile(rig: Rig) -> MobileHarness:
    rig.store.prepare_channels()
    return MobileHarness(rig, MobileTaskControl(rig.control))


def event(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def short(inbound_id: str, index: int = 1) -> str:
    return f"{inbound_id[:12]}-{index}"


def metadata(outcome: MobileControlOutcome) -> dict[str, Any]:
    return cast(dict[str, Any], outcome.receipt["mobile_task_control"])


async def test_status_same_titles_reference_exact_task_without_model_input(
    mobile: MobileHarness,
) -> None:
    other = await mobile.rig.ledger.save(
        "持续任务", brief={"goal": "另一项任务", "constraints": []}
    )
    status_id = event("two-tasks")
    status = mobile.receive(status_id, "/任务")
    tickets = metadata(status)["tickets"]
    index = next(
        position
        for position, ticket in enumerate(tickets, 1)
        if ticket["commitment_id"] == mobile.rig.identifier
    )
    assert len(tickets) == 2 and status.receipt["reply_text"].count('"持续任务"') == 2
    assert f"/暂停 {short(status_id, index)}" in status.receipt["reply_text"]
    paused = mobile.receive(event("pause-one"), f"/暂停 {short(status_id, index)}")
    assert paused.control_committed and metadata(paused)["kind"] == "pause"
    assert mobile.rig.control.current(mobile.rig.identifier)["control"]["epoch"] == 1
    assert mobile.rig.control.current(other["id"])["control"]["epoch"] == 0
    assert mobile.rig.store.pending_inputs(mobile.rig.main) == 0


def test_saved_mobile_receipt_is_fixed_after_new_intent_and_rejects_changed_text(
    mobile: MobileHarness,
) -> None:
    status_id, pause_id = event("status"), event("pause")
    mobile.receive(status_id, "/任务")
    text = f"/暂停 {short(status_id)}"
    first = mobile.receive(pause_id, text)
    mobile.rig.command(OwnerControlRequest("request_replan", 1, "new-intent", "仅交草稿"))
    snapshot = mobile.rig.control.replan_snapshot(mobile.rig.main, mobile.rig.identifier)
    mobile.rig.approve(snapshot)
    before = mobile.rig.ledger.get(mobile.rig.identifier)
    duplicate = mobile.receive(pause_id, text)
    assert duplicate == first and duplicate.control_committed
    assert len(mobile.submissions) == 1
    assert mobile.rig.ledger.get(mobile.rig.identifier) == before
    with pytest.raises(MobileControlRejected, match="request_id_conflict"):
        mobile.receive(pause_id, text + " changed")
    stored = mobile.rig.store.db.execute(
        "SELECT receipt FROM channel_inbound WHERE id=?", (pause_id,)
    ).fetchone()[0]
    assert json.loads(stored) == first.receipt


def test_receipt_save_failure_recovers_original_confirmation_after_restart_and_approval(
    mobile: MobileHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    status_id, pause_id = event("save-status"), event("save-pause")
    mobile.receive(status_id, "/任务")
    text = f"/暂停 {short(status_id)}"
    captured: list[dict[str, Any]] = []

    def interrupted(inbound_id: str, receipt: dict[str, Any]) -> None:
        assert inbound_id == pause_id
        captured.append(receipt)
        raise RuntimeError("simulated receipt interruption")

    monkeypatch.setattr(mobile.service, "_save", interrupted)
    with pytest.raises(RuntimeError, match="receipt interruption"):
        mobile.receive(pause_id, text)
    assert (
        mobile.rig.store.db.execute(
            "SELECT receipt FROM channel_inbound WHERE id=?", (pause_id,)
        ).fetchone()[0]
        is None
    )
    mobile.rig.command(OwnerControlRequest("request_replan", 1, "newer", "只交草稿"))
    snapshot = mobile.rig.control.replan_snapshot(mobile.rig.main, mobile.rig.identifier)
    mobile.rig.approve(snapshot)
    mobile.rig.reopen()
    mobile.service = MobileTaskControl(mobile.rig.control)
    before = mobile.rig.ledger.get(mobile.rig.identifier)
    recovered = mobile.receive(pause_id, text)
    assert len(captured) == 1 and recovered.receipt == captured[0]
    assert recovered.control_committed and not mobile.submissions[-1].created
    assert metadata(recovered)["tickets"][0]["epoch"] == 1
    assert metadata(recovered)["tickets"][0]["approved_epoch"] == 0
    assert mobile.rig.ledger.get(mobile.rig.identifier) == before
    assert mobile.rig.store.pending_inputs(mobile.rig.main) == 1


def test_adjust_keeps_complete_requirements_one_ingress_and_original_unrouted_control(
    mobile: MobileHarness,
) -> None:
    status_id, pause_id, adjust_id = event("adjust-status"), event("adjust-pause"), event("adjust")
    mobile.receive(status_id, "/任务")
    mobile.receive(pause_id, f"/暂停 {short(status_id)}")
    requirements = '只交草稿，不得发布。\n完整引号"及后文\x01'
    text = f"/调整 {short(pause_id)} {requirements}"
    first = mobile.receive(adjust_id, text)
    assert first.control_committed and metadata(first)["kind"] == "request_replan"
    assert mobile.rig.ledger.get(mobile.rig.identifier)["brief"]["constraints"] == [
        "保留原始要求",
        requirements,
    ]
    inputs = mobile.rig.store.claim_inputs(mobile.rig.main, "mobile-run", limit=8)
    assert len(inputs) == 1 and inputs[0]["route"] is None
    assert inputs[0]["task_control"]["receipt_id"] == metadata(first)["control_receipt_id"]
    assert mobile.receive(adjust_id, text) == first
    assert len(mobile.submissions) == 2
    assert mobile.rig.control.current(mobile.rig.identifier)["control"]["epoch"] == 2


def test_new_event_cannot_reuse_spent_short_reference(mobile: MobileHarness) -> None:
    status_id = event("spent-status")
    mobile.receive(status_id, "/任务")
    text = f"/暂停 {short(status_id)}"
    mobile.receive(event("spent-first"), text)
    rejected = mobile.receive(event("spent-again"), text)
    assert not rejected.control_committed and metadata(rejected)["error"] == "epoch_conflict"
    assert mobile.rig.control.current(mobile.rig.identifier)["control"]["epoch"] == 1
    assert mobile.rig.store.pending_inputs(mobile.rig.main) == 0


def test_mobile_pause_works_without_model_and_never_budgets_new_context(
    mobile: MobileHarness,
) -> None:
    mobile.configured = False
    mobile.rig.limits["context"] = 1
    status_id = event("no-model-status")
    mobile.receive(status_id, "/任务")
    result = mobile.receive(event("no-model-pause"), f"/暂停 {short(status_id)}")
    assert result.control_committed and metadata(result)["kind"] == "pause"
    assert mobile.rig.store.pending_inputs(mobile.rig.main) == 0


def test_adjustment_queue_full_rolls_back_requirements_and_control_version(
    mobile: MobileHarness,
) -> None:
    status_id, pause_id = event("full-status"), event("full-pause")
    mobile.receive(status_id, "/任务")
    mobile.receive(pause_id, f"/暂停 {short(status_id)}")
    mobile.max_pending = 1
    mobile.rig.command(OwnerControlRequest("request_replan", 1, "fills-inbox", "原有要求"))
    fresh_id = event("full-current-status")
    mobile.receive(fresh_id, "/任务")
    before = mobile.rig.ledger.get(mobile.rig.identifier)
    result = mobile.receive(event("full-adjust"), f"/调整 {short(fresh_id)} 不得追加")
    assert metadata(result)["error"] == "queue_full" and not result.control_committed
    assert mobile.rig.ledger.get(mobile.rig.identifier) == before
    assert mobile.rig.store.pending_inputs(mobile.rig.main) == 1


def test_status_reference_expires_when_only_approval_changes(mobile: MobileHarness) -> None:
    snapshot = mobile.rig.replan()
    status_id = event("approval-status")
    mobile.receive(status_id, "/任务")
    mobile.rig.approve(snapshot)
    rejected = mobile.receive(event("approval-old"), f"/调整 {short(status_id)} 新要求")
    assert metadata(rejected)["error"] == "epoch_conflict" and not rejected.control_committed
    assert mobile.rig.control.current(mobile.rig.identifier)["control"]["epoch"] == snapshot.epoch
    assert mobile.rig.store.pending_inputs(mobile.rig.main) == 1


@pytest.mark.parametrize(
    "changes", [{"name": "onebot"}, {"account_id": "200"}, {"owner_id": "8", "target": "8"}]
)
def test_short_reference_is_bound_to_original_private_identity(
    mobile: MobileHarness, changes: dict[str, str]
) -> None:
    status_id = event("bound-status")
    mobile.receive(status_id, "/任务")
    source = replace(mobile.source, **changes)
    result = mobile.receive(event("cross-source"), f"/暂停 {short(status_id)}", source=source)
    assert metadata(result)["error"] == "reference_invalid" and not result.control_committed
    assert not mobile.submissions


@pytest.mark.parametrize("account", ["", "bot"])
def test_control_rejects_missing_or_fallback_account(mobile: MobileHarness, account: str) -> None:
    with pytest.raises(MobileControlRejected, match="source_rejected"):
        mobile.receive(
            event("fallback"), "/任务", source=replace(mobile.source, account_id=account)
        )
    assert not mobile.submissions


@pytest.mark.parametrize("damage", ["missing", "collision", "version", "oversized", "nontext"])
def test_short_reference_rejects_missing_colliding_or_bad_receipts(
    mobile: MobileHarness, damage: str
) -> None:
    status_id = event("damaged-status")
    if damage != "missing":
        status = mobile.receive(status_id, "/任务")
        with mobile.rig.store.db:
            if damage == "collision":
                mobile.stage(status_id[:-1] + ("0" if status_id[-1] != "0" else "1"))
            else:
                value: str | bytes
                if damage == "version":
                    status.receipt["mobile_task_control"]["version"] = 99
                    value = json.dumps(status.receipt)
                elif damage == "oversized":
                    value = "x" * (mobile.rig.limits["output"] * 4 + 1)
                else:
                    value = b"not text"
                mobile.rig.store.db.execute(
                    "UPDATE channel_inbound SET receipt=? WHERE id=?", (value, status_id)
                )
    result = mobile.receive(event("damaged-command"), f"/暂停 {short(status_id)}")
    assert metadata(result)["error"] == "reference_invalid" and not result.control_committed
    assert not mobile.submissions


@pytest.mark.parametrize("column", ["id", "name", "fingerprint"])
def test_reference_sql_byte_gate_never_materializes_oversized_identity_fields(
    mobile: MobileHarness, column: str
) -> None:
    status_id = event("oversized-identity")
    mobile.receive(status_id, "/任务")
    value = {
        "id": status_id + "坏" * 4000,
        "name": "坏" * 8,
        "fingerprint": "坏" * 64,
    }[column]
    with mobile.rig.store.db:
        mobile.rig.store.db.execute(
            f"UPDATE channel_inbound SET {column}=? WHERE id=?", (value, status_id)
        )
    prefix = status_id[:12]
    rows = mobile.service._records(
        "channel_inbound.id>=? AND channel_inbound.id<?", (prefix, prefix + "g")
    )
    assert len(rows) == 1 and rows[0][column] is None
    rejected = mobile.receive(event("oversized-identity-command"), f"/暂停 {short(status_id)}")
    assert metadata(rejected)["error"] == "reference_invalid"
    assert not mobile.submissions


def test_bad_identity_candidate_consumes_prefix_limit_without_filter_or_refill(
    mobile: MobileHarness,
) -> None:
    status_id = "b" * 64
    status = mobile.receive(status_id, "/任务")
    with mobile.rig.store.db:
        for suffix, name in (("0", "坏" * 8000), ("1", "telegram")):
            mobile.rig.store.db.execute(
                "INSERT INTO channel_inbound(id,name,receipt,created_at,fingerprint) VALUES(?,?,?,?,?)",
                ("b" * 12 + suffix * 52, name, json.dumps(status.receipt), now(), "f" * 64),
            )
    prefix = status_id[:12]
    rows = mobile.service._records(
        "channel_inbound.id>=? AND channel_inbound.id<?", (prefix, prefix + "g")
    )
    assert len(rows) == 2 and rows[0]["name"] is None
    assert rows[1]["id"] == "b" * 12 + "1" * 52
    rejected = mobile.receive(event("bad-candidate-command"), f"/暂停 {short(status_id)}")
    assert metadata(rejected)["error"] == "reference_invalid" and not mobile.submissions


def test_oversized_receipt_is_rejected_before_json_decode(
    mobile: MobileHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from muli import mobile_task_control

    status_id = event("receipt-byte-gate")
    mobile.receive(status_id, "/任务")
    with mobile.rig.store.db:
        mobile.rig.store.db.execute(
            "UPDATE channel_inbound SET receipt=? WHERE id=?",
            ("x" * (mobile.rig.limits["output"] * 4 + 1), status_id),
        )

    original_decode = mobile_task_control.json.loads
    decoded_sizes: list[int] = []

    def bounded_decode(value: str | bytes | bytearray, *args: Any, **kwargs: Any) -> Any:
        decoded_sizes.append(len(value))
        assert len(value) <= mobile.rig.limits["output"] * 4
        return original_decode(value, *args, **kwargs)

    monkeypatch.setattr(mobile_task_control.json, "loads", bounded_decode)
    result = mobile.receive(event("receipt-byte-command"), f"/暂停 {short(status_id)}")
    assert metadata(result)["error"] == "reference_invalid" and not mobile.submissions
    assert all(size <= mobile.rig.limits["output"] * 4 for size in decoded_sizes)


@pytest.mark.parametrize(
    "text",
    ["/任务 extra", " /任务", "/暂停 missing", "/调整 missing", "/暂停 123456789abc-1 extra"],
)
def test_reserved_command_malformed_text_is_fixed_error_not_model_input(
    mobile: MobileHarness, text: str
) -> None:
    result = mobile.receive(event("malformed"), text)
    assert metadata(result)["error"] == "invalid_command" and not result.control_committed
    assert not mobile.submissions and mobile.rig.store.pending_inputs(mobile.rig.main) == 0


async def test_status_uses_whole_receipt_budget_and_excludes_incomplete_briefs(
    mobile: MobileHarness,
) -> None:
    mobile.rig.limits["output"] = 1000
    await mobile.rig.ledger.save("没有完整说明")
    for index in range(12):
        await mobile.rig.ledger.save(
            f"有限任务{index}" + "较长标题" * 40,
            brief={"goal": "完整目标", "constraints": []},
        )
    status = mobile.receive(event("bounded-status"), "/任务")
    encoded = json.dumps(status.receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert len(encoded) <= 1000
    assert 0 < len(metadata(status)["tickets"]) <= 10
    assert "没有完整说明" not in status.receipt["reply_text"]
    assert not status.control_committed and not mobile.submissions
