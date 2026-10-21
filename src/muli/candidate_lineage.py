"""Follow one current management-plan attachment, never an ancestor chain."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from muli.candidate_policy import CandidateError
from muli.candidate_preparation import CandidateEvidence
from muli.managed_preparation import CurrentManagedInstallation
from muli.management_projection import _read_object
from muli.platforms.install_receipt import read_managed_install_receipt
from muli.platforms.installation import FileFingerprint, FileIdentity
from muli.platforms.managed_launcher import read_file
from muli.store import Store

# Independent of a newly selected policy: an older supported attachment remains
# readable when the owner lowers the next preparation's metadata budget.
_EVIDENCE_BYTES = 1_048_576
# Transaction.public(), final result/error/timestamp and progress metadata are
# bounded. Keep room before publishing a plan the fixed old controller reads.
_FINAL_HEADROOM = 4096


@dataclass(frozen=True, slots=True)
class CandidateLineage:
    reference: FileFingerprint = field(repr=False)
    evidence: CandidateEvidence = field(repr=False)


def check_plan_budget(plan: dict[str, Any], limit: int) -> None:
    try:
        size = len(json.dumps(plan, ensure_ascii=False, allow_nan=False).encode())
    except (ValueError, TypeError, UnicodeError):
        raise CandidateError("candidate_plan_invalid") from None
    if size + _FINAL_HEADROOM > limit:
        raise CandidateError("candidate_plan_budget")


def read_lineage(
    store: Store, current: CurrentManagedInstallation, *, plan_limit: int
) -> CandidateLineage | None:
    """The host supplies a real current-source observation and its live guard.

    A known reference that is missing, changed or for another slot is an error.
    Missing references on older or externally switched installations mean no
    carried intent; the actual receipt must independently prove a usable source.
    """
    current.verify()
    record = current.record
    plan = _read_object(store.db, "management", record.request_id, plan_limit)
    if (
        plan is None
        or plan.get("id") != record.request_id
        or plan.get("action") != record.action
        or not isinstance(plan.get("transaction"), dict)
        or plan["transaction"].get("generation") != record.generation
    ):
        raise CandidateError("candidate_source_plan_changed")
    target = record.phase == "committed" and record.action == "switch"
    name = "candidate_evidence" if target else "source_candidate_evidence"
    if name not in plan:
        if target and plan.get("operation") == "update":
            raise CandidateError("candidate_source_evidence_missing")
        current.verify()
        if _read_object(store.db, "management", record.request_id, plan_limit) != plan:
            raise CandidateError("candidate_source_plan_changed")
        return None
    try:
        reference = FileFingerprint.model_validate(plan[name])
        prefix = Path(current.slot.prefix)
        root = prefix.parent.parent
        if (
            prefix.name != "muli-agent"
            or prefix.parent.name != "tools"
            or reference.path != str(root / "evidence.json")
        ):
            raise CandidateError("candidate_source_evidence_changed")
        data, identity = read_file(Path(reference.path), _EVIDENCE_BYTES, private=True)
        observed = FileFingerprint(
            path=reference.path,
            identity=FileIdentity.model_validate(identity),
            digest=hashlib.sha256(data).hexdigest(),
        )
        evidence = CandidateEvidence.from_bytes(data, _EVIDENCE_BYTES)
        receipt = read_managed_install_receipt(current.slot, uv_version=evidence.uv_version)
        if (
            observed != reference
            or root.name != "candidate-" + evidence.request_id
            or evidence.deployed_installation != current.slot.digest
            or evidence.deployed_receipt != receipt.receipt
            or evidence.deployed_intent != receipt.parsed.intent
        ):
            raise CandidateError("candidate_source_evidence_changed")
        current.verify()
        if _read_object(store.db, "management", record.request_id, plan_limit) != plan:
            raise CandidateError("candidate_source_plan_changed")
        return CandidateLineage(reference, evidence)
    except CandidateError:
        raise
    except (OSError, ValueError, TypeError):
        raise CandidateError("candidate_source_evidence_changed") from None
