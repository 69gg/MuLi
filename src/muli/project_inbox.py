"""Finite project-inbox provenance, separate from current task admission state."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from muli.evidence import EvidenceError
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin

if TYPE_CHECKING:
    from muli.store import Store

# The existing AttentionSettings ceilings: batch_size <= 128 and max_chars <=
# 32000, with at most four UTF-8 bytes per character. Runtime further narrows
# these limits to its current configured admission budget.
MAX_PROJECT_PROOFS = 128
MAX_PROJECT_BATCH_BYTES = 32_000 * 4
WEB_PROJECT_ROUTING = FrozenRouting.bound(ReplyOrigin(kind="web"))


def _integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value < 1 << 63


def _identifier(value: object, max_bytes: int) -> bool:
    if type(max_bytes) is not int or max_bytes < 1 or not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= max_bytes
    except UnicodeError:
        return False


def _fits(value: dict[str, Any], max_bytes: int) -> bool:
    if type(max_bytes) is not int or max_bytes < 1:
        return False
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= min(
            max_bytes, MAX_PROJECT_BATCH_BYTES
        )
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        return False


@dataclass(frozen=True, slots=True)
class ProjectInboxProof:
    record_id: str
    event_id: int
    scope: str
    revision: str
    binding_revision: str
    project_id: str

    def dump(self) -> dict[str, Any]:
        return {
            "version": 1,
            "record_id": self.record_id,
            "event_id": self.event_id,
            "scope": self.scope,
            "revision": self.revision,
            "binding_revision": self.binding_revision,
            "project_id": self.project_id,
        }

    @classmethod
    def load(
        cls, value: object, *, max_bytes: int = MAX_PROJECT_BATCH_BYTES
    ) -> ProjectInboxProof | None:
        if not isinstance(value, dict) or set(value) != {
            "version",
            "record_id",
            "event_id",
            "scope",
            "revision",
            "binding_revision",
            "project_id",
        }:
            return None
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or not _integer(value["event_id"], minimum=1)
            or any(
                not isinstance(value[key], str) or len(value[key]) != 32
                for key in ("record_id", "scope", "revision", "binding_revision")
            )
            or not _identifier(value["project_id"], max_bytes)
            or not _fits(value, max_bytes)
        ):
            return None
        return cls(
            value["record_id"],
            value["event_id"],
            value["scope"],
            value["revision"],
            value["binding_revision"],
            value["project_id"],
        )


@dataclass(frozen=True, slots=True)
class ProjectTaskSnapshot:
    project_id: str
    revision: int
    status: str
    epoch: int
    approved_epoch: int

    def dump(self) -> dict[str, Any]:
        return {
            "version": 1,
            "project_id": self.project_id,
            "revision": self.revision,
            "status": self.status,
            "epoch": self.epoch,
            "approved_epoch": self.approved_epoch,
        }

    @classmethod
    def load(
        cls, value: object, *, max_bytes: int = MAX_PROJECT_BATCH_BYTES
    ) -> ProjectTaskSnapshot | None:
        if not isinstance(value, dict) or set(value) != {
            "version",
            "project_id",
            "revision",
            "status",
            "epoch",
            "approved_epoch",
        }:
            return None
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or not _identifier(value["project_id"], max_bytes)
            or not _integer(value["revision"], minimum=1)
            or not isinstance(value["status"], str)
            or value["status"] not in {"open", "in_progress", "blocked"}
            or not _integer(value["epoch"])
            or not _integer(value["approved_epoch"])
            or value["approved_epoch"] > value["epoch"]
            or not _fits(value, max_bytes)
        ):
            return None
        return cls(
            value["project_id"],
            value["revision"],
            value["status"],
            value["epoch"],
            value["approved_epoch"],
        )


@dataclass(frozen=True, slots=True)
class ProjectInboxBatch:
    proofs: tuple[ProjectInboxProof, ...]
    snapshot: ProjectTaskSnapshot

    def dump(self) -> dict[str, Any]:
        return {
            "version": 1,
            "proofs": [proof.dump() for proof in self.proofs],
            "snapshot": self.snapshot.dump(),
        }

    @classmethod
    def load(
        cls,
        value: object,
        *,
        max_bytes: int = MAX_PROJECT_BATCH_BYTES,
        max_records: int = MAX_PROJECT_PROOFS,
    ) -> ProjectInboxBatch | None:
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "proofs", "snapshot"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or not isinstance(value["proofs"], list)
            or type(max_records) is not int
            or not 1 <= len(value["proofs"]) <= min(max_records, MAX_PROJECT_PROOFS)
        ):
            return None
        snapshot = ProjectTaskSnapshot.load(value["snapshot"], max_bytes=max_bytes)
        proofs = tuple(
            ProjectInboxProof.load(item, max_bytes=max_bytes) for item in value["proofs"]
        )
        if snapshot is None or any(proof is None for proof in proofs):
            return None
        checked = tuple(proof for proof in proofs if proof is not None)
        first = checked[0]
        source = (first.scope, first.revision, first.binding_revision, first.project_id)
        if (
            snapshot.project_id != first.project_id
            or any(
                (proof.scope, proof.revision, proof.binding_revision, proof.project_id) != source
                for proof in checked
            )
            or len({proof.record_id for proof in checked}) != len(checked)
            or any(a.event_id >= b.event_id for a, b in zip(checked, checked[1:], strict=False))
            or not _fits(value, max_bytes)
        ):
            return None
        return cls(checked, snapshot)


def read_task_snapshot(
    store: Store, project_id: str, *, max_bytes: int
) -> ProjectTaskSnapshot | None:
    """Point-read the exact bounded task; never resolve or read its source bodies."""
    from muli.commitments import Commitments
    from muli.events import EventBus
    from muli.task_control import TaskControl

    if not _identifier(project_id, max_bytes):
        return None
    try:
        item = store.bounded_object("commitment", project_id, max_bytes=max_bytes, strict_json=True)
    except EvidenceError as error:
        if error.code == "output_budget":
            raise
        return None
    if item is None or item.get("status") in ("done", "cancelled"):
        return None
    try:
        # The existing canonical brief validator has a metadata-only mode that
        # deliberately does not resolve attachment/message references.
        Commitments(store, EventBus(store))._validate_brief(
            item.get("brief"), stored=True, resolve_references=False
        )
        control = TaskControl._state(item)
        return ProjectTaskSnapshot.load(
            {
                "version": 1,
                "project_id": project_id,
                "revision": item.get("revision"),
                "status": item.get("status"),
                "epoch": control["epoch"],
                "approved_epoch": control["approved_epoch"],
            },
            max_bytes=max_bytes,
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def project_batch_current(
    store: Store,
    batch: ProjectInboxBatch,
    *,
    workspace_roots: list[str],
    max_task_bytes: int,
) -> bool:
    """Authenticate one admission against current bounded canonical host facts.

    This is an enqueue boundary, never a running segment's task-version lease.
    Source revocation and task-snapshot conflicts are distinguished by Attention
    before this final synchronous check, under the same configuration lock.
    """
    from muli.file_inbox import (
        _OBJECT_BYTES,
        EVENT_TYPE,
        FileInboxSettingsStore,
        FileInboxState,
        _read_policy,
        _signature,
        read_record,
    )

    if not isinstance(batch, ProjectInboxBatch) or ProjectInboxBatch.load(batch.dump()) != batch:
        return False
    try:
        settings_store = FileInboxSettingsStore(store)
        desired = settings_store.read()
        binding = settings_store.binding(desired)
        policy = _read_policy(store)
        if (
            not desired.enabled
            or binding is None
            or policy is None
            or not policy["enabled"]
            or desired.project_id != batch.snapshot.project_id
            or policy["signature"] != _signature(desired, workspace_roots)
        ):
            return False
        state = FileInboxState(
            policy["scope"],
            policy["revision"],
            True,
            policy["code"],
            desired.project_id,
            binding["revision"],
        )
        for proof in batch.proofs:
            row = store.db.execute(
                "SELECT id,type,session_id,CASE WHEN typeof(data)='text' "
                "AND length(CAST(data AS BLOB))<=? THEN CAST(data AS BLOB) END AS data "
                "FROM events WHERE id=?",
                (_OBJECT_BYTES, proof.event_id),
            ).fetchone()
            if (
                row is None
                or row["type"] != EVENT_TYPE
                or row["session_id"] is not None
                or not isinstance(row["data"], bytes)
            ):
                return False
            event = {"id": row["id"], "type": row["type"], "data": json.loads(row["data"])}
            record = read_record(store, event, state)
            if record is None or record.get("project_proof") != proof.dump():
                return False
        return (
            read_task_snapshot(store, batch.snapshot.project_id, max_bytes=max_task_bytes)
            == batch.snapshot
        )
    except (ValueError, TypeError, UnicodeError, RecursionError, sqlite3.Error):
        return False
