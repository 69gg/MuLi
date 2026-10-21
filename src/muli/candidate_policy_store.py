"""One bounded candidate policy object in the host's existing Store.

This repository owns no connection, lock or installation authority. The host
authenticates reads/writes, serializes configuration changes, and rechecks the
stored policy alongside the real current-source binding before each effect.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from muli.candidate_policy import CandidateError, CandidateSettings
from muli.management_projection import ProjectionError, _read_object
from muli.platforms.installation import _canonical
from muli.store import Store

_KIND = "update_policy"
_ID = "candidate-v1"
_BYTES = 16_384
_FIELDS = {"id", "schema_version", "policy"}


def _checked(policy: CandidateSettings) -> tuple[CandidateSettings, dict[str, Any]]:
    # Revalidate before JSON serialization, including the model's per-path
    # length limit: model_copy updates may not have passed validation. The
    # aggregate budget also covers Store._put's exact UTF-8/escaped JSON form.
    try:
        checked = CandidateSettings.model_validate(policy.model_dump(mode="python"))
        item: dict[str, Any] = {
            "id": _ID,
            "schema_version": 1,
            "policy": checked.model_dump(mode="json"),
        }
        size = len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, UnicodeError):
        raise CandidateError("candidate_policy_invalid") from None
    if size > _BYTES:
        raise CandidateError("candidate_policy_budget")
    return checked, item


def policy_revision(policy: CandidateSettings) -> str:
    """Hash the complete validated policy, not an approval or source proof."""
    checked, _ = _checked(policy)
    return hashlib.sha256(_canonical(checked.model_dump(mode="json"))).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidatePolicyStore:
    store: Store

    def read(self) -> CandidateSettings:
        """Missing means disabled; malformed or future data is never reset."""
        try:
            item = _read_object(self.store.db, _KIND, _ID, _BYTES)
        except ProjectionError as error:
            code = (
                "candidate_policy_budget"
                if error.code == "projection_object_budget"
                else "candidate_policy_invalid"
            )
            raise CandidateError(code) from None
        except sqlite3.Error:
            raise CandidateError("candidate_policy_read_failed") from None
        if item is None:
            return CandidateSettings()
        if (
            set(item) != _FIELDS
            or item["id"] != _ID
            or type(item["schema_version"]) is not int
            or item["schema_version"] != 1
            or not isinstance(item["policy"], dict)
        ):
            raise CandidateError("candidate_policy_invalid")
        try:
            policy = CandidateSettings.model_validate(item["policy"])
        except ValueError:
            raise CandidateError("candidate_policy_invalid") from None
        return _checked(policy)[0]

    def replace(self, policy: CandidateSettings) -> CandidateSettings:
        """Replace the complete policy and its summary in one Store event."""
        db = self.store.db
        if db.in_transaction:
            raise CandidateError("candidate_policy_outer_transaction")
        if db.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL or db.isolation_level is None:
            raise CandidateError("candidate_policy_transaction_mode_unsupported")
        # A complete replacement is not permission to overwrite an unknown
        # schema or hide corruption. The host must handle such states explicitly.
        self.read()
        checked, item = _checked(policy)
        summary = {"id": _ID, "enabled": checked.enabled, "revision": policy_revision(checked)}
        try:
            self.store.event("update.policy.changed", summary, objects=[(_KIND, item)])
        except sqlite3.Error:
            raise CandidateError("candidate_policy_write_failed") from None
        return checked
