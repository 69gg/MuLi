"""Small host proofs for complete, genuinely executed attachment text pages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from muli.evidence import EvidenceError, validate_integer

if TYPE_CHECKING:
    from muli.document_settings import DocumentSettingsStore
    from muli.providers import ToolCall
    from muli.runtime import PreparedRequest, Runtime
    from muli.task_control import TaskControl
    from muli.task_inspection import TaskInspection

_PROOF_BYTES = 2048
_HOST_BYTES = 96
_COORDINATES = ("offset_bytes", "length_bytes", "next_offset_bytes", "total_bytes", "has_more")
_SOURCE_FIELDS = (
    "store_instance",
    "main_id",
    "source_session_id",
    "source_agent_id",
    "source_run_id",
    "source_generation",
)
_PAGE_FIELDS = ("attachment_id", "reader", "source_revision", "revision", *_COORDINATES)
_PROOF_FIELDS = {
    *_SOURCE_FIELDS,
    *_PAGE_FIELDS,
    "schema_version",
    "assistant_id",
    "assistant_revision",
    "assistant_seq",
    "call_id",
    "call_digest",
    "result_digest",
    "document_policy",
    "trigger",
}
_READ_ERRORS = (EvidenceError, ValueError, TypeError, UnicodeError, RecursionError, sqlite3.Error)


def _unavailable() -> EvidenceError:
    return EvidenceError("reading_page_unavailable", "来源页没有可完整核对的真实阅读凭据")


def _host(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= _HOST_BYTES:
        raise _unavailable()
    return value


def _revision(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise _unavailable()
    return value


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class _Capture:
    request: PreparedRequest
    identity: dict[str, Any]
    trigger: dict[str, Any]


class _StagedPage(dict[str, Any]):
    """The credential travels only with this actual returned result object."""

    __slots__ = ("owner", "capture", "reader", "policy", "digest")

    def __init__(
        self,
        result: dict[str, Any],
        owner: ReadingPages,
        capture: _Capture,
        reader: str,
        policy: str | None,
    ) -> None:
        super().__init__(result)
        self.owner = owner
        self.capture: _Capture | None = capture
        self.reader, self.policy, self.digest = reader, policy, _digest(result)


class ReadingPages:
    def __init__(
        self,
        runtime: Runtime,
        task_control: TaskControl,
        task_inspection: TaskInspection,
        document_settings: DocumentSettingsStore,
    ) -> None:
        self.runtime, self.control, self.inspection = runtime, task_control, task_inspection
        self.documents = document_settings
        self.store = runtime.store

    def document_policy(self) -> str:
        return _digest(self.documents.read().model_dump(mode="json"))

    def root(self, session_id: str) -> str:
        """Resolve a real main or team session without inventing a binding."""
        try:
            _host(session_id)
            main_id = _host(self.store.existing_main_session_id())
            hops, budget = self.inspection.handoff_limits()
            main = self.store.bounded_object("session", main_id, max_bytes=budget)
            session = self.store.bounded_object("session", session_id, max_bytes=budget)
            if (
                main is None
                or main.get("id") != main_id
                or main.get("kind") != "main"
                or main.get("principal") != "owner"
                or session is None
                or session.get("id") != session_id
            ):
                raise _unavailable()
            if session_id == main_id:
                return main_id
            agent = self.store.bounded_object("agent", session_id, max_bytes=budget)
            if (
                session.get("kind") != "team"
                or agent is None
                or agent.get("id") != session_id
                or agent.get("session_id") != session_id
            ):
                raise _unavailable()
            _, lineage = self.control.scope_lineage(session_id, max_hops=hops, max_bytes=budget)
            if not lineage or lineage[-1]["session_id"] != main_id:
                raise _unavailable()
            for frame in lineage[1:]:
                ancestor_id = _host(frame["session_id"])
                ancestor = self.store.bounded_object("agent", ancestor_id, max_bytes=budget)
                if ancestor is not None and (
                    ancestor.get("id") != ancestor_id or ancestor.get("session_id") != ancestor_id
                ):
                    raise _unavailable()
            return main_id
        except _READ_ERRORS as error:
            raise _unavailable() from error

    def _message(self, identifier: str, fields: tuple[str, ...]) -> dict[str, Any]:
        # Project only fixed host fields: large provider state or message bodies
        # must not prevent a small proof from being checked, or enter Python.
        projection = (
            "json_object('id',o.id,'seq',m.seq,'session_id',m.session_id,"
            + ",".join(f"'{field}',json_extract(o.data,'$.{field}')" for field in fields)
            + ")"
        )
        row = self.store.db.execute(
            "WITH projected AS (SELECT " + projection + " AS data FROM objects o "
            "JOIN message_order m ON m.message_id=o.id WHERE o.kind='message' AND o.id=? "
            "AND json_extract(o.data,'$.id')=o.id "
            "AND json_extract(o.data,'$.session_id')=m.session_id) "
            "SELECT CASE WHEN length(CAST(data AS BLOB))<=? THEN CAST(data AS BLOB) END "
            "FROM projected",
            (identifier, _PROOF_BYTES + _HOST_BYTES * (len(fields) + 4)),
        ).fetchone()
        if row is None or row[0] is None:
            raise _unavailable()
        item: dict[str, Any] = json.loads(row[0].decode("utf-8"))
        _json(item)
        return item

    def _trigger(self, request: PreparedRequest) -> dict[str, Any]:
        boundary = request.tool_batch_seq
        if type(boundary) is not int:
            raise _unavailable()
        validate_integer(boundary, "tool_batch_seq", minimum=1)
        message = next(
            (
                item
                for item in reversed(request.messages)
                if item.role == "user" and item.source_message_id is not None
            ),
            None,
        )
        if message is None:
            raise _unavailable()
        item = self._message(
            _host(message.source_message_id), ("role", "evidence_revision", "source", "principal")
        )
        if (
            item["session_id"] != request.session_id
            or item["role"] != "user"
            or type(item["seq"]) is not int
            or item["seq"] >= boundary
        ):
            raise _unavailable()
        return {
            "message_id": _host(item["id"]),
            "message_revision": _host(item["evidence_revision"]),
            "source": _host(item["source"]),
            "principal": _host(item["principal"]),
        }

    def _identity(self, request: PreparedRequest, run_id: str) -> dict[str, Any]:
        runtime, session_id = self.runtime, request.session_id
        if (
            runtime._closing
            or runtime.run_ids.get(session_id) != run_id
            or runtime.tasks.get(session_id) is not asyncio.current_task()
            or session_id in runtime._stopping
        ):
            raise _unavailable()
        if type(request.tool_batch_seq) is not int:
            raise _unavailable()
        validate_integer(request.tool_batch_seq, "tool_batch_seq", minimum=1)
        main_id = self.root(session_id)
        budget = runtime.config.settings.limits.max_output_chars * 4
        run = self.store.bounded_object("run", _host(run_id), max_bytes=budget)
        if run is None or run.get("session_id") != session_id or run.get("status") != "running":
            raise _unavailable()
        generation: int | None = None
        if session_id != main_id:
            agent = self.store.bounded_object("agent", session_id, max_bytes=budget)
            if agent is None or agent.get("run_id") != run_id or agent.get("status") != "running":
                raise _unavailable()
            value = agent.get("generation")
            if type(value) is not int:
                raise _unavailable()
            generation = value
            validate_integer(generation, "generation")
        return {
            "store_instance": _host(self.store.instance_id),
            "main_id": main_id,
            "source_session_id": _host(session_id),
            "source_agent_id": None if session_id == main_id else session_id,
            "source_run_id": run_id,
            "source_generation": generation,
        }

    def capture(self) -> _Capture | None:
        """Capture before the reader's first await; never retain a global candidate."""
        try:
            owner = self.runtime._tool_execution.get()
            request = self.runtime.request_context(owner[0]) if owner is not None else None
            if owner is None or request is None:
                return None
            return _Capture(request, self._identity(request, owner[1]), self._trigger(request))
        except _READ_ERRORS:
            return None

    @staticmethod
    def _page(result: dict[str, Any], reader: str) -> dict[str, Any]:
        common = {"id", "revision", "field", "content", *_COORDINATES}
        if reader == "attachment_read":
            if set(result) != common | {"truncated"} or type(result["truncated"]) is not bool:
                raise _unavailable()
            source_revision = _revision(result["revision"])
        elif reader == "document_read":
            if set(result) != common | {"format", "scope", "source_revision"} or (
                (result["format"], result["scope"])
                not in {("pdf", "pdf_text_layer"), ("docx", "docx_main_body_text")}
            ):
                raise _unavailable()
            source_revision = _revision(result["source_revision"])
        else:
            raise _unavailable()
        if (
            not isinstance(result["id"], str)
            or re.fullmatch(r"[a-f0-9]{32}", result["id"]) is None
            or result["field"] != "content"
            or not isinstance(result["content"], str)
            or type(result["has_more"]) is not bool
        ):
            raise _unavailable()
        for field in _COORDINATES[:-1]:
            validate_integer(result[field], field)
        offset, length, next_offset, total = (result[field] for field in _COORDINATES[:-1])
        if (
            length != len(result["content"].encode("utf-8"))
            or next_offset != offset + length
            or next_offset > total
            or result["has_more"] != (next_offset < total)
            or (result["has_more"] and not length)
            or (
                reader == "attachment_read"
                and result["truncated"] != bool(offset or result["has_more"])
            )
        ):
            raise _unavailable()
        return {
            "attachment_id": result["id"],
            "reader": reader,
            "source_revision": source_revision,
            "revision": _revision(result["revision"]),
            **{field: result[field] for field in _COORDINATES},
        }

    def stage(
        self,
        result: dict[str, Any],
        reader: str,
        *,
        capture: _Capture | None,
        policy: str | None = None,
    ) -> dict[str, Any]:
        try:
            if (
                capture is None
                or self.runtime.request_context(capture.request.session_id) is not capture.request
            ):
                return result
            if (
                self._identity(capture.request, capture.identity["source_run_id"])
                != capture.identity
            ):
                return result
            self._page(result, reader)
            if (reader == "document_read" and policy != self.document_policy()) or (
                reader == "attachment_read" and policy is not None
            ):
                return result
            if policy is not None:
                _revision(policy)
            return _StagedPage(result, self, capture, reader, policy)
        except _READ_ERRORS:
            return result

    def _call(self, assistant_id: str, call_id: str) -> dict[str, Any]:
        budget = self.runtime.config.settings.limits.max_output_chars * 4
        rows = self.store.db.execute(
            "SELECT CASE WHEN j.type='object' AND length(CAST(j.value AS BLOB))<=? "
            "THEN CAST(j.value AS BLOB) END FROM objects o,json_each(CASE "
            "WHEN json_type(o.data,'$.tool_calls')='array' "
            "AND length(CAST(json_extract(o.data,'$.tool_calls') AS BLOB))<=? "
            "THEN json_extract(o.data,'$.tool_calls') ELSE '[]' END) j "
            "WHERE o.kind='message' AND o.id=? "
            "AND json_extract(j.value,'$.id')=? LIMIT 2",
            (budget, budget, assistant_id, call_id),
        ).fetchall()
        if len(rows) != 1 or rows[0][0] is None:
            raise _unavailable()
        call: dict[str, Any] = json.loads(rows[0][0].decode("utf-8"))
        _json(call)
        if set(call) != {"id", "name", "arguments"} or _host(call["id"]) != call_id:
            raise _unavailable()
        return call

    @staticmethod
    def _arguments(call: dict[str, Any], page: dict[str, Any]) -> None:
        arguments = json.loads(call["arguments"])
        if (
            call["name"] != page["reader"]
            or not isinstance(arguments, dict)
            or not {"id"} <= arguments.keys() <= {"id", "revision", "offset_bytes"}
            or arguments["id"] != page["attachment_id"]
            or type(arguments.get("offset_bytes", 0)) is not int
            or arguments.get("offset_bytes", 0) != page["offset_bytes"]
            or (arguments.get("revision") is not None and arguments["revision"] != page["revision"])
            or (page["offset_bytes"] and arguments.get("revision") is None)
            or (
                page["reader"] == "attachment_read"
                and not {"revision", "offset_bytes"} & arguments.keys()
            )
        ):
            raise _unavailable()

    def completed(
        self,
        request: PreparedRequest,
        session_id: str,
        run_id: str,
        assistant: dict[str, Any],
        call: ToolCall,
        result: Any,
        message: dict[str, Any],
    ) -> dict[str, Any] | None:
        if type(result) is not _StagedPage or result.owner is not self:
            return None
        capture, result.capture = result.capture, None
        try:
            if (
                capture is None
                or capture.request is not request
                or request.session_id != session_id
                or self._identity(request, run_id) != capture.identity
                or self._trigger(request) != capture.trigger
                or result.digest != _digest(result)
                or message.get("role") != "tool"
                or message.get("session_id") != session_id
                or message.get("tool_call_id") != call.id
                or message.get("content_provenance") != "accepted"
                or message.get("content") != _json(result).decode("utf-8")
                or assistant.get("role") != "assistant"
                or assistant.get("session_id") != session_id
            ):
                return None
            page = self._page(result, result.reader)
            self._arguments(asdict(call), page)
            saved = self._message(_host(assistant["id"]), ("role", "evidence_revision"))
            if (
                saved["role"] != "assistant"
                or saved["session_id"] != session_id
                or saved["seq"] != request.tool_batch_seq
                or assistant.get("seq") != saved["seq"]
                or self._call(saved["id"], _host(call.id)) != asdict(call)
                or (result.policy is not None and result.policy != self.document_policy())
            ):
                return None
            proof = {
                "schema_version": 1,
                **capture.identity,
                **page,
                "assistant_id": saved["id"],
                "assistant_revision": _host(saved["evidence_revision"]),
                "assistant_seq": saved["seq"],
                "call_id": call.id,
                "call_digest": _digest(asdict(call)),
                "result_digest": result.digest,
                "document_policy": result.policy,
                "trigger": capture.trigger,
            }
            return proof if len(_json(proof)) <= _PROOF_BYTES else None
        except _READ_ERRORS:
            return None

    def source(
        self,
        message_id: str,
        message_revision: str,
        *,
        before_seq: int,
        main_id: str,
    ) -> tuple[dict[str, Any], str | None]:
        try:
            return self._source(
                message_id, message_revision, before_seq=before_seq, main_id=main_id
            )
        except _READ_ERRORS as error:
            raise _unavailable() from error

    def _source(
        self,
        message_id: str,
        message_revision: str,
        *,
        before_seq: int,
        main_id: str,
    ) -> tuple[dict[str, Any], str | None]:
        validate_integer(before_seq, "before_seq", minimum=1)
        metadata = self._message(
            _host(message_id),
            ("role", "evidence_revision", "tool_call_id", "content_provenance", "reading_page"),
        )
        proof = metadata["reading_page"]
        if (
            metadata["role"] != "tool"
            or metadata["evidence_revision"] != _host(message_revision)
            or metadata["content_provenance"] != "accepted"
            or type(metadata["seq"]) is not int
            or not metadata["seq"] < before_seq
            or not isinstance(proof, dict)
            or set(proof) != _PROOF_FIELDS
            or len(_json(proof)) > _PROOF_BYTES
            or type(proof["schema_version"]) is not int
            or proof["schema_version"] != 1
            or proof["store_instance"] != _host(self.store.instance_id)
            or proof["main_id"] != _host(main_id)
            or proof["source_session_id"] != metadata["session_id"]
            or self.root(metadata["session_id"]) != main_id
        ):
            raise _unavailable()
        _host(proof["source_run_id"])
        budget = self.runtime.config.settings.limits.max_output_chars * 4
        run = self.store.bounded_object("run", proof["source_run_id"], max_bytes=budget)
        if run is None or run.get("session_id") != metadata["session_id"]:
            raise _unavailable()
        if metadata["session_id"] == main_id:
            if proof["source_agent_id"] is not None or proof["source_generation"] is not None:
                raise _unavailable()
        else:
            if proof["source_agent_id"] != metadata["session_id"]:
                raise _unavailable()
            # This is the archived generation. Completion or a later real run
            # must not rewrite that historical identity or revoke the old page.
            validate_integer(proof["source_generation"], "source_generation")
        row = self.store.message_evidence_slice(
            metadata["session_id"],
            message_id=message_id,
            seq=None,
            offset_bytes=0,
            length_bytes=budget,
            metadata_bytes=_HOST_BYTES,
        )
        if (
            row is None
            or row["content_type"] != "text"
            or row["content_bytes"] > budget
            or row["seq"] != metadata["seq"]
            or row["content_provenance"] != "accepted"
            or row["evidence_revision_type"] != "text"
            or row["evidence_revision"] != message_revision.encode("utf-8")
            or row["evidence_revision_bytes"] != len(row["evidence_revision"])
            or len(row["content"]) != row["content_bytes"]
            or hashlib.sha256(row["content"]).hexdigest() != _revision(proof["result_digest"])
        ):
            raise _unavailable()
        result = json.loads(row["content"].decode("utf-8"))
        if not isinstance(result, dict):
            raise _unavailable()
        page = self._page(result, proof["reader"])
        if any(
            type(proof[field]) is not type(value) or proof[field] != value
            for field, value in page.items()
        ):
            raise _unavailable()
        assistant = self._message(_host(proof["assistant_id"]), ("role", "evidence_revision"))
        validate_integer(proof["assistant_seq"], "assistant_seq", minimum=1)
        call = self._call(assistant["id"], _host(proof["call_id"]))
        self._arguments(call, page)
        if (
            assistant["session_id"] != metadata["session_id"]
            or assistant["role"] != "assistant"
            or assistant["evidence_revision"] != _host(proof["assistant_revision"])
            or assistant["seq"] != proof["assistant_seq"]
            or not assistant["seq"] < metadata["seq"]
            or metadata["tool_call_id"] != proof["call_id"]
            or _digest(call) != _revision(proof["call_digest"])
        ):
            raise _unavailable()
        trigger = proof["trigger"]
        if not isinstance(trigger, dict) or set(trigger) != {
            "message_id",
            "message_revision",
            "source",
            "principal",
        }:
            raise _unavailable()
        original = self._message(
            _host(trigger["message_id"]), ("role", "evidence_revision", "source", "principal")
        )
        if (
            original["role"] != "user"
            or original["session_id"] != metadata["session_id"]
            or not original["seq"] < assistant["seq"]
            or any(
                original[field] != _host(trigger[target])
                for field, target in (
                    ("evidence_revision", "message_revision"),
                    ("source", "source"),
                    ("principal", "principal"),
                )
            )
        ):
            raise _unavailable()
        policy = proof["document_policy"]
        if page["reader"] == "document_read":
            _revision(policy)
        elif policy is not None:
            raise _unavailable()
        return {
            **{field: proof[field] for field in _SOURCE_FIELDS},
            **page,
            "source_message_id": message_id,
            "source_message_revision": message_revision,
        }, policy
