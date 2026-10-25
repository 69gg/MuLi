"""Host attribution for one real file_publish copy, not a task acceptance verdict."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from muli.evidence import EvidenceError, json_size
from muli.providers import ToolCall
from muli.task_control import TaskSnapshot

if TYPE_CHECKING:
    from muli.runtime import PreparedRequest, Runtime


@dataclass(frozen=True, slots=True)
class PublicationSource:
    snapshot: TaskSnapshot
    run_id: str
    generation: int
    completion_revision: int
    path: str


def _source_fields(source: PublicationSource, identifier: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "commitment_id": source.snapshot.commitment_id,
        "task_revision": source.snapshot.revision,
        "epoch": source.snapshot.epoch,
        "agent_id": source.snapshot.session_id,
        "run_id": source.run_id,
        "generation": source.generation,
        "completion_revision": source.completion_revision,
        "attachment_id": identifier,
        "source_path": source.path,
    }


def _current(
    runtime: Runtime,
    session_id: str,
    run_id: str,
    request: PreparedRequest,
    path: str,
) -> PublicationSource | None:
    snapshot = request.execution_scope
    main_id = runtime.store.existing_main_session_id()
    if (
        runtime._closing
        or main_id is None
        or request.session_id != session_id
        or runtime.run_ids.get(session_id) != run_id
        or snapshot is None
        or snapshot.session_id != session_id
        or snapshot.epoch != snapshot.approved_epoch
    ):
        return None
    # SQLite checks the original bytes before any host field crosses into Python.
    budget = runtime.config.settings.limits.max_output_chars * 4
    try:
        agent = runtime.store.bounded_object("agent", session_id, max_bytes=budget)
        run = runtime.store.bounded_object("run", run_id, max_bytes=budget)
        task = runtime.store.bounded_object("commitment", snapshot.commitment_id, max_bytes=budget)
    except EvidenceError:
        return None
    if (
        agent is None
        or run is None
        or task is None
        or agent.get("session_id") != session_id
        or agent.get("parent_id") != main_id
        or agent.get("commitment_id") != snapshot.commitment_id
        or agent.get("run_id") != run_id
        or agent.get("status") != "running"
        or type(agent.get("generation")) is not int
        or agent["generation"] < 1
        or type(agent.get("completion_revision")) is not int
        or agent["completion_revision"] < 0
        or run.get("session_id") != session_id
        or run.get("status") != "running"
        or type(task.get("revision")) is not int
        or task["revision"] != snapshot.revision
    ):
        return None
    control = task.get("control")
    if control is None and "control" not in task:
        if snapshot.epoch != 0 or snapshot.approved_epoch != 0:
            return None
    elif (
        not isinstance(control, dict)
        or type(control.get("epoch")) is not int
        or type(control.get("approved_epoch")) is not int
        or control["epoch"] != snapshot.epoch
        or control["approved_epoch"] != snapshot.approved_epoch
    ):
        return None
    return PublicationSource(
        snapshot, run_id, agent["generation"], agent["completion_revision"] + 1, path
    )


def capture_source(runtime: Runtime, path: Path) -> PublicationSource | None:
    try:
        str(path).encode("utf-8", errors="strict")
    except UnicodeError:
        # Legal local paths can be outside SQLite's optional UTF-8 proof contract.
        return None
    owner = runtime._tool_execution.get()
    request = runtime.request_context(owner[0]) if owner is not None else None
    if owner is None or request is None:
        return None
    return _current(runtime, owner[0], owner[1], request, str(path))


def stage_publication(
    runtime: Runtime, source: PublicationSource | None, attachment: dict[str, Any]
) -> None:
    if source is None:
        return
    request = runtime.request_context(source.snapshot.session_id)
    if (
        request is None
        or _current(runtime, source.snapshot.session_id, source.run_id, request, source.path)
        != source
    ):
        return
    if (
        runtime.store.db.execute(
            "SELECT 1 FROM objects WHERE kind='task_file_publication' AND id=?",
            (attachment["id"],),
        ).fetchone()
        is not None
    ):
        # An optional proof is never retrofitted onto an older or duplicate copy.
        return
    proof = {
        **_source_fields(source, attachment["id"]),
        "version": 1,
        "state": "pending",
        "size": attachment["size"],
        "sha256": attachment["sha256"],
    }
    if json_size(proof) <= runtime.config.settings.limits.max_output_chars:
        runtime.store.put("task_file_publication", proof)


def completed_publication(
    runtime: Runtime,
    request: PreparedRequest,
    session_id: str,
    run_id: str,
    assistant: dict[str, Any],
    call: ToolCall,
    result: Any,
    message: dict[str, Any],
) -> dict[str, Any] | None:
    """Return only a host-staged copy matching this exact complete tool result."""
    try:
        return _completed_publication(
            runtime, request, session_id, run_id, assistant, call, result, message
        )
    except EvidenceError:
        # Unreadable optional evidence must not replace a real tool result.
        return None


def _completed_publication(
    runtime: Runtime,
    request: PreparedRequest,
    session_id: str,
    run_id: str,
    assistant: dict[str, Any],
    call: ToolCall,
    result: Any,
    message: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        call.name != "file_publish"
        or not isinstance(result, dict)
        or result.get("error")
        or result.get("status") in ("unknown", "failed", "not_executed", "cancelled")
        or result.get("outcome") in ("unknown", "failed", "not_executed", "cancelled")
        or result.get("truncated")
        or result.get("result_omitted")
        or message.get("content_provenance") != "accepted"
        or message.get("role") != "tool"
        or message.get("session_id") != session_id
        or message.get("tool_call_id") != call.id
        or assistant.get("role") != "assistant"
        or assistant.get("session_id") != session_id
    ):
        return None
    if message.get("content") != json.dumps(result, ensure_ascii=False, default=str):
        return None
    identifier = result.get("id")
    if not isinstance(identifier, str) or re.fullmatch(r"[a-f0-9]{32}", identifier) is None:
        return None
    budget = runtime.config.settings.limits.max_output_chars
    proof = runtime.store.bounded_object("task_file_publication", identifier, max_bytes=budget * 4)
    if (
        proof is None
        or proof.get("state") != "pending"
        or type(proof.get("version")) is not int
        or proof["version"] != 1
    ):
        return None
    if not isinstance(proof.get("source_path"), str):
        return None
    source = _current(runtime, session_id, run_id, request, proof["source_path"])
    if source is None:
        return None
    expected = _source_fields(source, identifier)
    if set(proof) != expected.keys() | {"version", "state", "size", "sha256"} or any(
        type(proof.get(field)) is not type(value) or proof[field] != value
        for field, value in expected.items()
    ):
        return None
    assistant_id = assistant.get("id")
    if not isinstance(assistant_id, str):
        return None
    saved_assistant = runtime.store.bounded_object("message", assistant_id, max_bytes=budget * 4)
    if (
        saved_assistant is None
        or saved_assistant.get("role") != "assistant"
        or saved_assistant.get("session_id") != session_id
        or not isinstance(saved_assistant.get("tool_calls"), list)
    ):
        return None
    matching = [
        item
        for item in saved_assistant["tool_calls"]
        if isinstance(item, dict) and item.get("id") == call.id
    ]
    if matching != [asdict(call)]:
        return None
    try:
        arguments = json.loads(call.arguments)
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"path"}
            or not isinstance(arguments["path"], str)
            or str(runtime.files.path(arguments["path"])) != source.path
        ):
            return None
    except (ValueError, TypeError, PermissionError):
        return None
    attachment = runtime.store.bounded_object("attachment", identifier, max_bytes=budget * 4)
    if attachment is None or any(
        proof.get(field) != result.get(field) or proof.get(field) != attachment.get(field)
        for field in ("size", "sha256")
    ):
        return None
    if (
        type(proof.get("size")) is not int
        or type(result.get("size")) is not int
        or type(attachment.get("size")) is not int
        or proof["size"] < 0
        or not isinstance(proof.get("sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", proof["sha256"]) is None
    ):
        return None
    recorded = {
        **proof,
        "state": "recorded",
        "assistant_message_id": assistant["id"],
        "call_id": call.id,
        "tool_message_id": message["id"],
    }
    return recorded if json_size(recorded) <= budget else None
