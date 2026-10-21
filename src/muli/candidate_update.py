"""Bind offline candidate preparation to the host's real managed installation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field, model_validator

from muli.candidate_lineage import CandidateLineage, read_lineage
from muli.candidate_online import ArtifactApproval
from muli.candidate_policy import CandidateError, ExpectedPolicy
from muli.candidate_policy_store import CandidatePolicyStore
from muli.candidate_preparation import CandidateSource, FrozenPolicy, inspect_policy
from muli.compatibility import ReleaseVersion
from muli.managed_preparation import ManagedPreparation
from muli.management_transaction import StrictRecord
from muli.platforms.install_receipt import read_managed_install_receipt
from muli.platforms.managed_deployment import DeploymentStore

if TYPE_CHECKING:
    from muli.runtime import Runtime
    from muli.service import UserService

# This selects the explicitly supported receipt parser, not an assertion about
# the binary that produced the source receipt. The actual configured uv must
# separately report a supported version through the guarded process backend.
_SOURCE_RECEIPT_ADAPTER = "0.13.1"


class UpdateRequest(StrictRecord):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=40)
    expected_policy: ExpectedPolicy | None = None
    expected_artifact: ArtifactApproval | None = None

    @model_validator(mode="after")
    def same_approval(self) -> UpdateRequest:
        if self.expected_artifact is not None and (
            self.expected_policy is None or self.expected_artifact.version != self.version
        ):
            raise ValueError("candidate_online_approval_required")
        return self


class PreviewRequest(StrictRecord):
    version: ReleaseVersion | None = None


@dataclass(frozen=True, slots=True)
class CapturedCandidate:
    policy: FrozenPolicy = field(repr=False)
    lineage: CandidateLineage | None = field(repr=False)
    artifact: ArtifactApproval | None = None


class CandidateUpdates:
    def __init__(
        self,
        runtime: Runtime,
        service: UserService,
        policies: CandidatePolicyStore,
        *,
        closing: Callable[[], bool],
    ) -> None:
        self.runtime, self.service, self.policies, self.closing = (
            runtime,
            service,
            policies,
            closing,
        )

    async def capture(self) -> CapturedCandidate:
        """Observe under the configuration lock without a fake or queued plan."""
        runtime = self.runtime
        if self.closing():
            raise CandidateError("candidate_host_closing")
        settings = self.policies.read()
        if not settings.enabled:
            raise CandidateError("candidate_source_not_configured")
        current = await ManagedPreparation(runtime, self.service).observe_current()
        limit = current.anchor.journal.max_record_bytes
        lineage = read_lineage(runtime.store, current, plan_limit=limit)
        receipt = read_managed_install_receipt(
            current.slot,
            uv_version=lineage.evidence.uv_version if lineage else _SOURCE_RECEIPT_ADAPTER,
        )
        with DeploymentStore(
            Path(current.anchor.journal_root), current.anchor.journal
        ) as deployments:
            protected = tuple(
                Path(deployments.read_slot(identifier).prefix)
                for identifier in {
                    current.anchor.controller_slot,
                    current.record.base.installation,
                    current.record.target.installation,
                }
            )

        def verify() -> None:
            if self.closing():
                raise CandidateError("candidate_host_closing")
            current.verify()
            if self.policies.read() != settings:
                raise CandidateError("candidate_policy_changed")
            if read_lineage(runtime.store, current, plan_limit=limit) != lineage:
                raise CandidateError("candidate_source_evidence_changed")
            receipt.verify(current.slot)

        source = CandidateSource(
            slot=current.slot,
            receipt=receipt,
            binding_revision=current.binding_revision,
            verify=verify,
            previous=lineage.evidence if lineage else None,
            protected_roots=protected,
            observed_format=current.record.observed_format,
        )
        return CapturedCandidate(inspect_policy(settings, source), lineage)
