"""One optional receive policy in the existing Store; no connection authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from muli.management_projection import ProjectionError, _read_object
from muli.management_transaction import StrictRecord
from muli.platforms.installation import _canonical
from muli.store import Store

DEFAULT_MESSAGE_BYTES = 1_048_576
MIN_MESSAGE_BYTES = 65_536
MAX_MESSAGE_BYTES = 4_194_304
POLICY_BYTES = 16_384
_KIND, _ID = "mcp_policy", "receive-v1"


class ReceivePolicyError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ReceivePolicy(StrictRecord):
    max_message_bytes: int = Field(
        default=DEFAULT_MESSAGE_BYTES, ge=MIN_MESSAGE_BYTES, le=MAX_MESSAGE_BYTES
    )


def checked_policy(policy: ReceivePolicy) -> ReceivePolicy:
    try:
        return ReceivePolicy.model_validate(policy.model_dump(mode="python"))
    except (ValueError, TypeError):
        raise ReceivePolicyError("mcp_policy_invalid") from None


def policy_revision(policy: ReceivePolicy) -> str:
    return hashlib.sha256(_canonical(checked_policy(policy).model_dump(mode="json"))).hexdigest()


@dataclass(frozen=True, slots=True)
class ReceivePolicyStore:
    store: Store

    def read(self) -> ReceivePolicy:
        try:
            item = _read_object(self.store.db, _KIND, _ID, POLICY_BYTES)
        except ProjectionError as error:
            raise ReceivePolicyError(
                "mcp_policy_budget"
                if error.code == "projection_object_budget"
                else "mcp_policy_invalid"
            ) from None
        except sqlite3.Error:
            raise ReceivePolicyError("mcp_policy_read_failed") from None
        if item is None:
            return ReceivePolicy()
        if (
            set(item) != {"id", "schema_version", "policy"}
            or item["id"] != _ID
            or type(item["schema_version"]) is not int
            or item["schema_version"] != 1
            or not isinstance(item["policy"], dict)
        ):
            raise ReceivePolicyError("mcp_policy_invalid")
        try:
            return ReceivePolicy.model_validate(item["policy"])
        except ValueError:
            raise ReceivePolicyError("mcp_policy_invalid") from None

    def replace(self, policy: ReceivePolicy) -> ReceivePolicy:
        db = self.store.db
        if db.in_transaction:
            raise ReceivePolicyError("mcp_policy_outer_transaction")
        if db.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL or db.isolation_level is None:
            raise ReceivePolicyError("mcp_policy_transaction_mode_unsupported")
        self.read()  # Complete replacement must not hide future schema or corruption.
        checked = checked_policy(policy)
        item: dict[str, Any] = {
            "id": _ID,
            "schema_version": 1,
            "policy": checked.model_dump(mode="json"),
        }
        if len(json.dumps(item, ensure_ascii=False).encode()) > POLICY_BYTES:
            raise ReceivePolicyError("mcp_policy_budget")
        try:
            self.store.event(
                "mcp.policy.changed",
                {"id": _ID, **checked.model_dump(), "revision": policy_revision(checked)},
                objects=[(_KIND, item)],
            )
        except sqlite3.Error:
            raise ReceivePolicyError("mcp_policy_write_failed") from None
        return checked
