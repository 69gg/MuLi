"""Durable per-call tool attempts used to distinguish replay from recovery.

The journal deliberately reuses the existing ``objects`` table.  It does not
change the storage format: older versions simply ignore the ``tool_attempt``
kind, while new versions can prove whether a call was only planned, actually
started, or had a result committed with its tool message.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from muli.store import Store, now

Phase = Literal["planned", "started", "committed", "not_executed", "unknown"]
ResultStatus = Literal["completed", "failed", "not_executed", "unknown"]
Recovery = Literal["replayable", "reconcile", "never"]

_TERMINAL: frozenset[str] = frozenset({"committed", "not_executed", "unknown"})
_SAFE_PHASES: frozenset[str] = frozenset({"planned", "committed", "not_executed"})


def attempt_id(run_id: str, call_id: str) -> str:
    if not run_id or not call_id:
        raise ValueError("工具尝试缺少运行或调用标识")
    return f"{run_id}:{call_id}"


def arguments_digest(arguments: Any) -> str:
    """Hash the exact parsed arguments without persisting secrets twice."""
    try:
        encoded = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        encoded = repr(arguments)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Attempt:
    attempt_id: str
    call_id: str
    session_id: str
    run_id: str
    generation: int
    tool: str
    risk: str
    recovery: Recovery
    arguments_digest: str
    phase: Phase = "planned"
    started_at: float | None = None
    committed_at: float | None = None
    result_status: str | None = None
    result_message_id: str | None = None
    command_id: str | None = None
    error_code: str | None = None
    approval_id: str | None = None
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)

    def dump(self) -> dict[str, Any]:
        return {
            "id": self.attempt_id,
            "kind": "tool_attempt",
            "attempt_id": self.attempt_id,
            "call_id": self.call_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "generation": self.generation,
            "tool": self.tool,
            "risk": self.risk,
            "recovery": self.recovery,
            "arguments_digest": self.arguments_digest,
            "phase": self.phase,
            "started_at": self.started_at,
            "committed_at": self.committed_at,
            "result_status": self.result_status,
            "result_message_id": self.result_message_id,
            "command_id": self.command_id,
            "error_code": self.error_code,
            "approval_id": self.approval_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def load(cls, value: dict[str, Any]) -> Attempt:
        return cls(
            attempt_id=str(value["attempt_id"]),
            call_id=str(value["call_id"]),
            session_id=str(value["session_id"]),
            run_id=str(value["run_id"]),
            generation=int(value.get("generation", 0)),
            tool=str(value.get("tool", "")),
            risk=str(value.get("risk", "unknown")),
            recovery=value.get("recovery", "never"),
            arguments_digest=str(value.get("arguments_digest", "")),
            phase=value.get("phase", "planned"),
            started_at=value.get("started_at"),
            committed_at=value.get("committed_at"),
            result_status=value.get("result_status"),
            result_message_id=value.get("result_message_id"),
            command_id=value.get("command_id"),
            error_code=value.get("error_code"),
            approval_id=value.get("approval_id"),
            created_at=float(value.get("created_at", 0.0)),
            updated_at=float(value.get("updated_at", 0.0)),
        )


class ToolAttempts:
    """Small journal facade bound to one Store."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def get(self, run_id: str, call_id: str) -> dict[str, Any] | None:
        return self.store.get("tool_attempt", attempt_id(run_id, call_id))

    def begin(
        self,
        *,
        run_id: str,
        call_id: str,
        session_id: str,
        generation: int,
        tool: str,
        risk: str,
        recovery: Recovery,
        arguments: Any,
    ) -> Attempt:
        identifier = attempt_id(run_id, call_id)
        existing = self.store.get("tool_attempt", identifier)
        if existing is not None:
            return Attempt.load(existing)
        attempt = Attempt(
            identifier,
            call_id,
            session_id,
            run_id,
            generation,
            tool,
            risk,
            recovery,
            arguments_digest(arguments),
        )
        self.store.put("tool_attempt", attempt.dump())
        return attempt

    def mark_started(self, attempt: Attempt, *, command_id: str | None = None) -> Attempt:
        if attempt.phase in _TERMINAL:
            return attempt
        if attempt.phase != "started" or attempt.started_at is None:
            attempt.started_at = now()
        attempt.phase = "started"
        attempt.updated_at = now()
        if command_id is not None:
            attempt.command_id = command_id
        self.store.put("tool_attempt", attempt.dump())
        return attempt

    def mark_not_executed(
        self,
        attempt: Attempt,
        *,
        error_code: str | None = None,
        result_message_id: str | None = None,
        persist: bool = True,
    ) -> Attempt:
        if attempt.phase == "committed":
            return attempt
        attempt.phase = "not_executed"
        attempt.result_status = "not_executed"
        attempt.committed_at = now()
        attempt.updated_at = attempt.committed_at
        if error_code is not None:
            attempt.error_code = error_code
        if result_message_id is not None:
            attempt.result_message_id = result_message_id
        if persist:
            self.store.put("tool_attempt", attempt.dump())
        return attempt

    def mark_committed(
        self,
        attempt: Attempt,
        *,
        result_status: ResultStatus,
        result_message_id: str,
        command_id: str | None = None,
        error_code: str | None = None,
    ) -> Attempt:
        attempt.phase = "committed"
        attempt.result_status = result_status
        attempt.result_message_id = result_message_id
        attempt.committed_at = now()
        attempt.updated_at = attempt.committed_at
        if command_id is not None:
            attempt.command_id = command_id
        if error_code is not None:
            attempt.error_code = error_code
        return attempt

    def mark_unknown(
        self, attempt: Attempt, *, error_code: str | None = None, persist: bool = True
    ) -> Attempt:
        if attempt.phase == "committed":
            return attempt
        attempt.phase = "unknown"
        attempt.result_status = "unknown"
        attempt.committed_at = now()
        attempt.updated_at = attempt.committed_at
        if error_code is not None:
            attempt.error_code = error_code
        if persist:
            self.store.put("tool_attempt", attempt.dump())
        return attempt

    def for_run(self, run_id: str, *, limit: int = 512) -> list[dict[str, Any]]:
        rows = self.store.db.execute(
            "SELECT data FROM objects WHERE kind='tool_attempt' "
            "AND json_extract(data,'$.run_id')=? ORDER BY rowid LIMIT ?",
            (run_id, max(1, min(limit, 4096))),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def for_session(self, session_id: str, *, limit: int = 1024) -> list[dict[str, Any]]:
        rows = self.store.db.execute(
            "SELECT data FROM objects WHERE kind='tool_attempt' "
            "AND json_extract(data,'$.session_id')=? ORDER BY rowid LIMIT ?",
            (session_id, max(1, min(limit, 4096))),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def safe_phase(phase: str) -> bool:
        return phase in _SAFE_PHASES


def result_status(result: Any) -> ResultStatus:
    if isinstance(result, dict):
        status = result.get("status")
        if status == "unknown" or result.get("outcome") == "unknown":
            return "unknown"
        if status == "not_executed":
            return "not_executed"
        if status == "failed" or result.get("error"):
            return "failed"
    return "completed"


def command_id(result: Any) -> str | None:
    if isinstance(result, dict):
        value = result.get("command_id")
        if isinstance(value, str) and value:
            return value
    return None
