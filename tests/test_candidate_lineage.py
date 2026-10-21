from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from test_candidate_preparation import (
    attempt,
    install_fixture,
)
from test_candidate_preparation import (
    no_processes as no_processes,
)
from test_candidate_preparation import (
    policy as policy,
)

import muli
from muli.candidate_lineage import check_plan_budget, read_lineage
from muli.candidate_policy import CandidateError
from muli.candidate_preparation import CandidateEvidence, FrozenPolicy, ReadyCandidate
from muli.config import Settings
from muli.format_admission import CURRENT_FORMAT_STATE
from muli.managed_preparation import CurrentManagedInstallation
from muli.managed_transaction import (
    RoleBinding,
    TransactionRecordV2,
    format_digest,
    raw_snapshot,
)
from muli.management_projection import ProjectionError
from muli.management_transaction import ClockReading, Deadline, _snapshot
from muli.platforms.installation import FileFingerprint
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    SelectorValue,
    SlotIdentity,
    verify_slot_files,
)
from muli.platforms.managed_launcher import fingerprint
from muli.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux candidate receipt identity")
_PLAN_BYTES = 32_768


@dataclass(slots=True)
class LineageHost:
    store: Store
    ready: ReadyCandidate
    prior: SlotIdentity
    anchor: DeploymentAnchor

    def current(
        self, *, action: str = "switch", phase: str = "committed", update: bool = True
    ) -> tuple[CurrentManagedInstallation, dict[str, Any]]:
        """Assemble a validated current record; no service/lease proof is invented."""
        unit = self.anchor.managed_unit

        def binding(slot: SlotIdentity, configuration: bytes = b"{}") -> RoleBinding:
            selector = SelectorValue(deployment=self.anchor.digest, installation=slot.digest)
            return RoleBinding(
                entry="managed",
                installation=slot.digest,
                compatibility=slot.compatibility_digest,
                configuration=_snapshot(configuration, 16_384),
                selector=_snapshot(selector.canonical(), 16_384),
                unit=unit,
            )

        base = binding(
            self.prior if action == "switch" and phase == "committed" else self.ready.slot
        )
        target = (
            binding(self.ready.slot if phase == "committed" else self.prior)
            if action == "switch"
            else binding(
                self.ready.slot,
                b'{"updates":{"check_on_start":false}}' if action == "configure" else b"{}",
            )
        )
        main = self.store.main_session(Settings().default_mode)["id"]
        generation = "b" * 32
        role = "base" if phase == "rolled_back" else "target"
        selected = base if role == "base" else target
        health = (
            None
            if phase == "aborted"
            else {
                "generation": generation,
                "challenge": "rollback-nonce" if phase == "rolled_back" else "target-nonce",
                "boot": "current-boot",
                "role": role,
                "deployment": self.anchor.digest,
                "installation": selected.installation,
                "compatibility": selected.compatibility,
                "configuration": selected.configuration.digest,
                "selector": selected.selector.digest if selected.selector else None,
                "unit": selected.unit.digest,
                "store_instance": self.store.instance_id,
                "main_session": main,
                "format_digest": format_digest(CURRENT_FORMAT_STATE),
                "core_ready": True,
                "admission": "released",
            }
        )
        record = TransactionRecordV2.model_validate(
            {
                "request_id": "later-plan-not-original-candidate",
                "generation": generation,
                "revision": 7,
                "action": action,
                "identity": {
                    "deployment": self.anchor.digest,
                    "store_instance": self.store.instance_id,
                    "main_session": main,
                    "origin_boot": "origin-boot",
                },
                "base": base,
                "target": target,
                "observed_format": CURRENT_FORMAT_STATE,
                "deadline": Deadline(
                    created=ClockReading(boot_id="kernel", wall=1.0, monotonic=1.0),
                    seconds=180.0,
                    rollback_reserve=60.0,
                ),
                "phase": phase,
                "challenge": "target-nonce",
                "rollback_challenge": "rollback-nonce" if phase == "rolled_back" else None,
                "rollback_attempted": phase == "rolled_back",
                "health": health,
                "activation": "pending" if phase == "aborted" else "released",
            }
        )
        assert record.resolved
        reference = self.ready.evidence_file.model_dump(mode="json")
        plan: dict[str, Any] = {
            "id": record.request_id,
            "action": action,
            "operation": "update" if update else action,
            "status": "completed" if phase == "committed" else "failed",
            "transaction": record.public(),
            # Both fields are present to prove role selection, not a fallback search.
            "candidate_evidence": reference,
            "source_candidate_evidence": reference,
        }
        self.store.put("management", plan)

        def verify() -> None:
            # The host's service/lease observation is injected. Files, receipt,
            # validated protocol record and the Store attachment remain real.
            verify_slot_files(self.ready.slot)

        return CurrentManagedInstallation(
            record, self.anchor, self.ready.slot, "c" * 64, verify
        ), plan

    def publish_evidence(self, plan: dict[str, Any], evidence: CandidateEvidence) -> None:
        path = Path(self.ready.evidence_file.path)
        path.write_bytes(evidence.to_bytes(262_144))
        value = FileFingerprint.model_validate(fingerprint(path, 262_144)).model_dump(mode="json")
        plan["candidate_evidence"] = value
        plan["source_candidate_evidence"] = value
        self.store.put("management", plan)


@pytest.fixture
def host(
    policy: FrozenPolicy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[LineageHost]:
    ready = install_fixture(attempt(policy, "original-candidate-request"))
    slot = ready.slot
    monkeypatch.setattr(sys, "executable", slot.lexical_python)
    monkeypatch.setattr(sys, "prefix", slot.prefix)
    monkeypatch.setattr(muli, "__file__", str(Path(slot.package.root) / "__init__.py"))
    store = Store.open_runtime(tmp_path / "data/muli.sqlite3", default_mode=Settings().default_mode)
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    try:
        with DeploymentStore(root) as deployment:
            deployment.publish_slot(policy.source.slot, guard=lambda: None)
            deployment.publish_slot(slot, guard=lambda: None)
            launcher = deployment.publish_launcher(guard=lambda: None)
            anchor = DeploymentAnchor(
                config_path=str(tmp_path / "config.json"),
                data_dir=str(tmp_path / "data"),
                database_path=str(tmp_path / "data/muli.sqlite3"),
                unit_path=str(tmp_path / "units/muli.service"),
                journal_root=str(root),
                controller_slot=policy.source.slot.digest,
                launcher=launcher,
                managed_unit=raw_snapshot(b"[Service]\nExecStart=/fixture/launcher\n", 16_384),
            )
            deployment.publish_anchor(anchor, guard=lambda: None)
        yield LineageHost(store, ready, policy.source.slot, anchor)
    finally:
        store.close()


@pytest.mark.parametrize(
    "action,phase,chosen",
    [
        ("switch", "committed", "candidate_evidence"),
        ("switch", "rolled_back", "source_candidate_evidence"),
        ("switch", "aborted", "source_candidate_evidence"),
        ("restart", "committed", "source_candidate_evidence"),
        ("configure", "committed", "source_candidate_evidence"),
    ],
)
def test_current_role_selects_exactly_one_attachment(
    host: LineageHost, action: str, phase: str, chosen: str
) -> None:
    current, plan = host.current(action=action, phase=phase)
    ignored = (
        "source_candidate_evidence" if chosen == "candidate_evidence" else "candidate_evidence"
    )
    plan[ignored] = {"unrelated-target-must-not-be-read": True}
    host.store.put("management", plan)
    observed = read_lineage(host.store, current, plan_limit=_PLAN_BYTES)
    assert observed is not None
    assert observed.evidence == host.ready.evidence
    assert observed.reference == host.ready.evidence_file
    assert observed.evidence.request_id == "original-candidate-request"
    assert observed.evidence.request_id != current.record.request_id
    assert observed.evidence.original_intent.main.specifier == ">=2,<3"
    assert observed.evidence.deployed_intent.constraints[-1].specifier == "==2.23.0"


def test_update_missing_target_evidence_cannot_fall_back_to_source(host: LineageHost) -> None:
    current, plan = host.current()
    del plan["candidate_evidence"]
    host.store.put("management", plan)
    with pytest.raises(CandidateError, match="^candidate_source_evidence_missing$"):
        read_lineage(host.store, current, plan_limit=_PLAN_BYTES)


@pytest.mark.parametrize(
    "action,phase", [("switch", "committed"), ("restart", "committed"), ("switch", "rolled_back")]
)
def test_external_switch_or_first_source_has_no_invented_ancestor(
    host: LineageHost, action: str, phase: str
) -> None:
    current, plan = host.current(action=action, phase=phase, update=False)
    del plan["candidate_evidence"]
    del plan["source_candidate_evidence"]
    host.store.put("management", plan)
    # A tempting unrelated older plan is not a lineage source.
    host.store.put(
        "management",
        {"id": "ancestor", "candidate_evidence": host.ready.evidence_file.model_dump(mode="json")},
    )
    assert read_lineage(host.store, current, plan_limit=_PLAN_BYTES) is None


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "bad-json",
        "hash",
        "identity",
        "symlink",
        "fifo",
        "path",
        "receipt",
        "installation",
        "candidate-request",
    ],
)
def test_changed_evidence_is_rejected_without_repair(host: LineageHost, change: str) -> None:
    current, plan = host.current()
    path = Path(host.ready.evidence_file.path)
    if change == "missing":
        path.unlink()
    elif change == "bad-json":
        path.write_bytes(b"not json")
    elif change == "hash":
        plan["candidate_evidence"]["digest"] = "f" * 64
    elif change == "identity":
        replacement = path.with_name("replacement")
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(path)
    elif change == "symlink":
        saved = path.with_name("unfollowed")
        path.rename(saved)
        path.symlink_to(saved)
    elif change == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif change == "path":
        plan["candidate_evidence"]["path"] = str(path.with_name("other-evidence.json"))
    elif change == "receipt":
        receipt_path = Path(host.ready.evidence.deployed_receipt.path)
        receipt_path.write_bytes(receipt_path.read_bytes() + b"\n# external edit\n")
    else:
        field = "deployed_installation" if change == "installation" else "request_id"
        value = "f" * 64 if change == "installation" else "unrelated-candidate-request"
        host.publish_evidence(plan, host.ready.evidence.model_copy(update={field: value}))
    host.store.put("management", plan)
    error = (
        "candidate_evidence_invalid"
        if change == "bad-json"
        else "candidate_source_evidence_changed"
    )
    with pytest.raises(CandidateError, match=f"^{error}$"):
        read_lineage(host.store, current, plan_limit=_PLAN_BYTES)
    if change == "symlink":
        assert path.is_symlink()
    assert path.exists() == (change != "missing")


@pytest.mark.parametrize("field", ["id", "action", "generation"])
def test_current_plan_identity_is_not_selected_from_an_old_generation(
    host: LineageHost, field: str
) -> None:
    current, plan = host.current()
    if field == "id":
        with host.store.db:
            host.store.db.execute(
                "UPDATE objects SET data=json_set(data,'$.id','unrelated') WHERE kind='management' AND id=?",
                (current.record.request_id,),
            )
    else:
        if field == "generation":
            plan["transaction"]["generation"] = "d" * 32
        else:
            plan[field] = "restart"
        host.store.put("management", plan)
    with pytest.raises(CandidateError, match="^candidate_source_plan_changed$"):
        read_lineage(host.store, current, plan_limit=_PLAN_BYTES)


@pytest.mark.parametrize("has_attachment", [False, True])
def test_plan_change_during_source_revalidation_is_rejected(
    host: LineageHost, has_attachment: bool
) -> None:
    current, plan = host.current(update=has_attachment)
    if not has_attachment:
        del plan["candidate_evidence"]
        host.store.put("management", plan)
    calls = 0

    def verify() -> None:
        nonlocal calls
        current.verify()
        calls += 1
        if calls == 2:
            host.store.put("management", {**plan, "changed_after_read": True})

    with pytest.raises(CandidateError, match="^candidate_source_plan_changed$"):
        read_lineage(host.store, replace(current, verify=verify), plan_limit=_PLAN_BYTES)
    assert calls == 2


def test_whole_plan_budget_includes_metadata_and_terminal_headroom(host: LineageHost) -> None:
    _, plan = host.current()
    check_plan_budget(plan, _PLAN_BYTES)
    plan["description"] = "界" * 10_000
    assert len(plan["description"]) < _PLAN_BYTES
    with pytest.raises(CandidateError, match="^candidate_plan_budget$"):
        check_plan_budget(plan, _PLAN_BYTES)


def test_oversized_persisted_plan_is_bounded_before_lineage_file_reads(
    host: LineageHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    import muli.candidate_lineage as lineage

    current, plan = host.current()
    plan["description"] = "界" * 12_000
    host.store.put("management", plan)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("oversized plan must fail before reading evidence")

    monkeypatch.setattr(lineage, "read_file", forbidden)
    with pytest.raises(ProjectionError, match="^projection_object_budget$"):
        read_lineage(host.store, current, plan_limit=_PLAN_BYTES)


def test_missing_current_plan_cannot_find_an_unrelated_ancestor(host: LineageHost) -> None:
    current, plan = host.current()
    with host.store.db:
        host.store.db.execute("DELETE FROM objects WHERE kind='management' AND id=?", (plan["id"],))
    host.store.put("management", {**plan, "id": "tempting-ancestor"})
    with pytest.raises(CandidateError, match="^candidate_source_plan_changed$"):
        read_lineage(host.store, current, plan_limit=_PLAN_BYTES)


@pytest.mark.parametrize("value", ["界😀", '\\\n\t"', "ascii"])
def test_plan_budget_counts_the_actual_json_utf8_encoding(value: str) -> None:
    plan = {"id": "plan", "description": value * 100}
    data = json.dumps(plan, ensure_ascii=False, allow_nan=False).encode("utf-8")
    check_plan_budget(plan, len(data) + 4096)
    with pytest.raises(CandidateError, match="^candidate_plan_budget$"):
        check_plan_budget(plan, len(data) + 4095)


@pytest.mark.parametrize("value", [float("nan"), b"not-json", "\ud800"])
def test_invalid_plan_encoding_has_a_fixed_private_error(value: object) -> None:
    with pytest.raises(CandidateError, match="^candidate_plan_invalid$"):
        check_plan_budget({"id": "plan", "value": value}, _PLAN_BYTES)
