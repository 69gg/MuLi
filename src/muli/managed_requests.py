"""Shared local candidate input, without installing or executing a candidate."""

from __future__ import annotations

from pydantic import Field, field_validator

from muli.management_transaction import StrictRecord
from muli.platforms.installation import AbsolutePath, _absolute


class LocalCandidate(StrictRecord):
    prefix: AbsolutePath = Field(repr=False)
    python: AbsolutePath = Field(repr=False)
    package_root: AbsolutePath = Field(repr=False)

    @field_validator("prefix", "python", "package_root")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _absolute(value)


def validate_candidate(action: str, candidate: LocalCandidate | None) -> None:
    if (candidate is not None) != (action in {"adopt", "switch"}):
        raise ValueError("managed_candidate_paths_required")
