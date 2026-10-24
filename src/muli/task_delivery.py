"""Explicit task delivery records: host file facts and separate model judgments."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from muli.commitments import Commitments
from muli.evidence import EvidenceError, json_size, validate_integer
from muli.store import Store, new_id, now
from muli.task_control import ControlUnavailable, TaskControl
from muli.tools import Tool, parameters, string

if TYPE_CHECKING:
    from muli.runtime import Runtime

_PARAMETERS = {
    "identifier",
    "observed_revision",
    "observed_epoch",
    "report_id",
    "attachment_id",
    "idempotency_key",
    "judgments",
}
_VERDICTS = ("satisfied", "unsatisfied", "unverified")


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def delivery_prefix(task_id: str) -> str:
    """An opaque task range in the existing objects(kind,id) primary key."""
    if not isinstance(task_id, str) or not task_id:
        raise EvidenceError("invalid_arguments", "验收任务 ID 必须是非空字符串")
    try:
        raw = task_id.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise EvidenceError("invalid_arguments", "验收任务 ID 不是有效 UTF-8") from exc
    return hashlib.sha256(raw).hexdigest() + ":"


def delivery_record(store: Store, identifier: str, task_id: str, budget: int) -> dict[str, Any]:
    record = store.bounded_object("task_delivery", identifier, max_bytes=budget * 4)
    if record is None or record.get("commitment_id") != task_id:
        raise EvidenceError("evidence_not_found", "验收单不存在或不属于该任务")
    if (
        type(record.get("version")) is not int
        or record["version"] != 1
        or any(
            type(record.get(field)) is not int or record[field] < minimum
            for field, minimum in (
                ("task_revision", 1),
                ("epoch", 0),
                ("generation", 1),
                ("completion_revision", 1),
            )
        )
        or any(
            not isinstance(record.get(field), str) or not record[field]
            for field in ("agent_id", "run_id")
        )
        or any(
            not isinstance(record.get(field), dict)
            for field in ("brief", "host_facts", "model_judgments", "recorded_by")
        )
        or type(record.get("created_at")) not in (int, float)
    ):
        raise EvidenceError("content_unavailable", "验收单版本不可核查")
    return record


def delivery_current(
    store: Store, control: TaskControl, record: dict[str, Any], task: dict[str, Any], budget: int
) -> bool:
    """Current task/execution identity, not a fresh read of the attachment bytes."""
    try:
        state = control._state(task)
        agent = store.bounded_object("agent", record["agent_id"], max_bytes=budget * 4)
        run = store.bounded_object("run", record["run_id"], max_bytes=budget * 4)
        return bool(
            type(task.get("revision")) is int
            and task["revision"] == record.get("task_revision")
            and state["epoch"] == record.get("epoch")
            and state["epoch"] == state["approved_epoch"]
            and agent is not None
            and run is not None
            and store.existing_main_session_id() is not None
            and agent.get("parent_id") == store.existing_main_session_id()
            and agent.get("session_id") == record["agent_id"]
            and agent.get("commitment_id") == task["id"]
            and agent.get("status") == "completed"
            and agent.get("run_id") == record["run_id"]
            and type(agent.get("generation")) is int
            and agent["generation"] == record.get("generation")
            and type(agent.get("completion_revision")) is int
            and agent["completion_revision"] == record.get("completion_revision")
            and run.get("session_id") == record["agent_id"]
            and run.get("status") == "completed"
        )
    except (EvidenceError, ControlUnavailable, KeyError, TypeError):
        return False


class TaskDelivery:
    def __init__(
        self,
        runtime: Runtime,
        commitments: Commitments,
        control: TaskControl,
        read_bytes: Callable[[str, int], Awaitable[tuple[dict[str, Any], bytes]]],
    ) -> None:
        self.runtime, self.commitments, self.control = runtime, commitments, control
        self.store, self.read_bytes = commitments.store, read_bytes

    def _object(self, kind: str, identifier: str) -> dict[str, Any]:
        item = self.store.bounded_object(
            kind, identifier, max_bytes=self.commitments.max_output_chars() * 4
        )
        if item is None:
            raise EvidenceError("evidence_not_found", "验收所需的准确持久记录不存在")
        return item

    def _host(self) -> tuple[str, str]:
        owner = self.runtime._tool_execution.get()
        if owner is None:
            raise PermissionError("验收登记缺少真实工具执行上下文")
        session_id, run_id = owner
        request = self.runtime.request_context(session_id)
        if (
            self.runtime._closing
            or self.runtime.tool_session_id != session_id
            or session_id != self.store.existing_main_session_id()
            or self.runtime.run_ids.get(session_id) != run_id
            or request is None
            or self.store.pending_inputs(session_id)
            or "task_delivery_record" in self.runtime.config.settings.disabled_tools
        ):
            raise PermissionError("只有当前真实主助手运行可以显式登记验收单")
        session, run = self._object("session", session_id), self._object("run", run_id)
        if (
            session.get("kind") != "main"
            or session.get("principal") != "owner"
            or run.get("session_id") != session_id
            or run.get("status") != "running"
        ):
            raise PermissionError("验收登记的主会话或运行身份不匹配")
        self.runtime._guard_request(request, "write")
        return owner

    def _normalize(self, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != _PARAMETERS:
            raise EvidenceError("invalid_arguments", "验收登记参数缺失或包含受保护字段")
        try:
            if (
                len(json.dumps(args, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                > (self.commitments.max_output_chars() * 4)
                or json_size(args) > self.commitments.max_output_chars()
            ):
                raise EvidenceError("output_budget", "验收参数超过工具输出预算")
        except (UnicodeError, TypeError, ValueError) as exc:
            if isinstance(exc, EvidenceError):
                raise
            raise EvidenceError("invalid_arguments", "验收参数不是有效 UTF-8 JSON") from exc
        validate_integer(args["observed_revision"], "observed_revision", minimum=1)
        validate_integer(args["observed_epoch"], "observed_epoch")
        for field in ("identifier", "report_id", "attachment_id", "idempotency_key"):
            if not isinstance(args[field], str) or not args[field].strip():
                raise EvidenceError("invalid_arguments", "验收关联与去重键必须是非空字符串")
        judgments = args["judgments"]
        if not isinstance(judgments, dict) or set(judgments) != {"goal", "constraints"}:
            raise EvidenceError("invalid_arguments", "判断必须分为目标与限制索引")
        if not isinstance(judgments["constraints"], list):
            raise EvidenceError("invalid_arguments", "限制判断必须是列表")
        entries = [judgments["goal"], *judgments["constraints"]]
        indexes: set[int] = set()
        for index, entry in enumerate(entries):
            if (
                not isinstance(entry, dict)
                or set(entry)
                != ({"status", "reason"} if index == 0 else {"index", "status", "reason"})
                or entry.get("status") not in _VERDICTS
                or not isinstance(entry.get("reason"), str)
                or not entry["reason"].strip()
            ):
                raise EvidenceError("invalid_arguments", "每条模型判断需要有效状态与完整理由")
            if index:
                validate_integer(entry["index"], "constraint index")
                if entry["index"] in indexes:
                    raise EvidenceError("invalid_arguments", "限制索引不能重复")
                indexes.add(entry["index"])
        return {
            **args,
            "judgments": {
                "goal": dict(judgments["goal"]),
                "constraints": sorted(
                    (dict(entry) for entry in judgments["constraints"]),
                    key=lambda entry: entry["index"],
                ),
            },
        }

    def _duplicate(self, key: str, fingerprint: str, task_id: str) -> dict[str, Any] | None:
        receipt = self.store.bounded_object(
            "task_delivery_receipt", key, max_bytes=self.commitments.max_output_chars() * 4
        )
        if receipt is None:
            return None
        if (
            set(receipt) != {"id", "commitment_id", "fingerprint", "delivery_id"}
            or receipt.get("commitment_id") != task_id
            or not isinstance(receipt.get("delivery_id"), str)
            or not receipt["delivery_id"]
        ):
            raise EvidenceError("content_unavailable", "验收去重凭据损坏，不补写或领养旧记录")
        if receipt.get("fingerprint") != fingerprint:
            raise EvidenceError("delivery_key_conflict", "同一验收去重键的内容不同")
        return delivery_record(
            self.store,
            receipt["delivery_id"],
            task_id,
            self.commitments.max_output_chars(),
        )

    def _completion(self, agent_id: str, revision: int) -> dict[str, Any]:
        budget = self.commitments.max_output_chars() * 4
        # Two matching rows already disprove uniqueness; never load an event history.
        rows = self.store.db.execute(
            "SELECT id,length(CAST(data AS BLOB)) AS bytes,"
            "CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB))<=? "
            "THEN CAST(data AS BLOB) END AS data FROM events "
            "WHERE type='team.finished' AND session_id=? "
            "AND json_extract(data,'$.completion_revision')=? ORDER BY id LIMIT 2",
            (budget, agent_id, revision),
        ).fetchall()
        if len(rows) != 1 or rows[0]["data"] is None:
            raise EvidenceError("content_unavailable", "准确完成事件缺失、多义或超过预算")
        try:
            data = json.loads(rows[0]["data"].decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            _digest(data)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise EvidenceError("content_unavailable", "准确完成事件无法完整核查") from exc
        return {"id": rows[0]["id"], "data": data}

    def _evidence(self, args: dict[str, Any], main_id: str) -> dict[str, Any]:
        task_id, report_id, attachment_id = (
            args["identifier"],
            args["report_id"],
            args["attachment_id"],
        )
        task = self._object("commitment", task_id)
        state = self.control._state(task)
        if (
            type(task.get("revision")) is not int
            or task["revision"] != args["observed_revision"]
            or state["epoch"] != args["observed_epoch"]
            or state["epoch"] != state["approved_epoch"]
        ):
            raise EvidenceError("evidence_changed", "任务要求或控制版本已变化")
        # Freeze already persisted reference metadata; do not read every referenced body.
        brief = task.get("brief")
        if (
            not isinstance(brief, dict)
            or brief.keys() - {"version", "goal", "constraints", "evidence_refs"}
            or type(brief.get("version")) is not int
            or brief["version"] != 1
            or not isinstance(brief.get("goal"), str)
            or not brief["goal"].strip()
            or not isinstance(brief.get("constraints"), list)
            or any(not isinstance(item, str) or not item.strip() for item in brief["constraints"])
            or not isinstance(brief.get("evidence_refs", []), list)
        ):
            raise EvidenceError("content_unavailable", "持久任务没有完整可核查的目标与限制")
        if [entry["index"] for entry in args["judgments"]["constraints"]] != list(
            range(len(brief["constraints"]))
        ):
            raise EvidenceError("invalid_arguments", "必须逐项标明全部限制，包括未验项目")
        proof = self._object("task_file_publication", attachment_id)
        proof_fields = {
            "id",
            "version",
            "state",
            "commitment_id",
            "task_revision",
            "epoch",
            "agent_id",
            "run_id",
            "generation",
            "completion_revision",
            "attachment_id",
            "source_path",
            "size",
            "sha256",
            "assistant_message_id",
            "call_id",
            "tool_message_id",
        }
        if (
            set(proof) != proof_fields
            or type(proof.get("version")) is not int
            or proof["version"] != 1
            or proof.get("state") != "recorded"
            or proof.get("commitment_id") != task_id
            or type(proof.get("task_revision")) is not int
            or proof["task_revision"] != task["revision"]
            or type(proof.get("epoch")) is not int
            or proof["epoch"] != state["epoch"]
            or proof.get("run_id") != report_id
            or proof.get("attachment_id") != attachment_id
            or any(
                not isinstance(proof.get(key), str) or not proof[key]
                for key in (
                    "agent_id",
                    "source_path",
                    "assistant_message_id",
                    "call_id",
                    "tool_message_id",
                )
            )
        ):
            raise EvidenceError(
                "evidence_not_found", "附件没有属于准确任务版本与完成报告的宿主发布证明"
            )
        for field in ("generation", "completion_revision"):
            validate_integer(proof[field], field, minimum=1)
        validate_integer(proof["size"], "attachment size")
        if (
            not isinstance(proof["sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", proof["sha256"]) is None
        ):
            raise EvidenceError("content_unavailable", "附件摘要无效")
        agent = self._object("agent", proof["agent_id"])
        run, report = self._object("run", report_id), self._object("team_report", report_id)
        event = self._completion(agent["id"], proof["completion_revision"])
        if (
            not delivery_current(
                self.store,
                self.control,
                {**proof, "version": 1},
                task,
                self.commitments.max_output_chars(),
            )
            or agent.get("parent_id") != main_id
            or report.get("parent_id") != main_id
            or report.get("agent_id") != agent["id"]
            or report.get("commitment_id") != task_id
            or report.get("execution_status") != "completed"
            or any(
                type(event["data"].get(field)) is not type(value)
                or event["data"].get(field) != value
                for field, value in {
                    "agent_id": agent["id"],
                    "run_id": report_id,
                    "status": "completed",
                    "generation": proof["generation"],
                    "completion_revision": proof["completion_revision"],
                }.items()
            )
        ):
            raise EvidenceError("evidence_changed", "报告不是当前准确直接队友的成功完成代次")
        assistant = self._object("message", proof["assistant_message_id"])
        message = self._object("message", proof["tool_message_id"])
        calls = assistant.get("tool_calls")
        matching = (
            [
                item
                for item in calls
                if isinstance(item, dict) and item.get("id") == proof["call_id"]
            ]
            if isinstance(calls, list)
            else []
        )
        if (
            assistant.get("role") != "assistant"
            or assistant.get("session_id") != agent["id"]
            or message.get("role") != "tool"
            or message.get("session_id") != agent["id"]
            or message.get("content_provenance") != "accepted"
            or message.get("tool_call_id") != proof["call_id"]
            or len(matching) != 1
            or set(matching[0]) != {"id", "name", "arguments"}
            or matching[0].get("name") != "file_publish"
            or not isinstance(message.get("content"), str)
            or len(message["content"]) > self.commitments.max_output_chars()
        ):
            raise EvidenceError("content_unavailable", "发布调用或完整工具结果定位不匹配")
        try:
            arguments = json.loads(matching[0]["arguments"])
            result = json.loads(message["content"])
            if (
                not isinstance(arguments, dict)
                or set(arguments) != {"path"}
                or not isinstance(arguments["path"], str)
                or str(self.runtime.files.path(arguments["path"])) != proof["source_path"]
                or not isinstance(result, dict)
                or result.get("id") != attachment_id
                or json.dumps(result, ensure_ascii=False) != message["content"]
                or result.get("error")
                or result.get("truncated")
                or result.get("result_omitted")
                or result.get("status") in ("unknown", "failed", "not_executed", "cancelled")
                or result.get("outcome") in ("unknown", "failed", "not_executed", "cancelled")
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, PermissionError) as exc:
            raise EvidenceError("content_unavailable", "发布参数或实际结果无法完整核查") from exc
        attachment = self._object("attachment", attachment_id)
        if any(
            type(item.get(field)) is not type(proof[field]) or item.get(field) != proof[field]
            for item in (attachment, result)
            for field in ("size", "sha256")
        ):
            raise EvidenceError("evidence_changed", "附件目录、实际结果与宿主复制摘要不一致")
        if (
            type(assistant.get("seq")) is not int
            or type(message.get("seq")) is not int
            or assistant["seq"] >= message["seq"]
        ):
            raise EvidenceError("content_unavailable", "发布消息顺序不匹配")
        preceding = self.store.db.execute(
            "SELECT o.id FROM message_order m JOIN objects o ON o.kind='message' AND o.id=m.message_id "
            "WHERE m.session_id=? AND m.seq<? AND json_extract(o.data,'$.role')='assistant' "
            "ORDER BY m.seq DESC LIMIT 1",
            (agent["id"], message["seq"]),
        ).fetchone()
        if preceding is None or preceding[0] != assistant["id"]:
            raise EvidenceError("content_unavailable", "工具结果没有对应准确 assistant 调用批次")
        return {
            "task": task,
            "brief": brief,
            "proof": proof,
            "agent": agent,
            "run": run,
            "report": report,
            "event": event,
            "assistant": assistant,
            "message": message,
            "attachment": attachment,
        }

    async def record(self, arguments: dict[str, Any]) -> dict[str, Any]:
        owner = self._host()
        args = self._normalize(arguments)
        key, fingerprint = _digest([args["identifier"], args["idempotency_key"]]), _digest(args)
        if duplicate := self._duplicate(key, fingerprint, args["identifier"]):
            return duplicate
        evidence = self._evidence(args, owner[0])
        maximum = min(
            self.runtime.config.settings.limits.max_file_bytes,
            self.commitments.max_output_chars() * 4,
        )
        if evidence["proof"]["size"] > maximum:
            raise EvidenceError("output_budget", "完整文件核验超过当前有限字节窗口")
        attachment, data = await self.read_bytes(args["attachment_id"], maximum)
        if (
            attachment != evidence["attachment"]
            or len(data) != evidence["proof"]["size"]
            or hashlib.sha256(data).hexdigest() != evidence["proof"]["sha256"]
        ):
            raise EvidenceError("evidence_changed", "附件副本字节与发布证明不一致")
        try:
            text = data.decode("utf-8", errors="strict")
            if "\x00" in text:
                raise ValueError
        except (UnicodeError, ValueError) as exc:
            raise EvidenceError("content_unavailable", "附件不是完整 UTF-8 文本") from exc
        proof = evidence["proof"]
        record = {
            "id": delivery_prefix(args["identifier"]) + new_id(),
            "version": 1,
            "commitment_id": args["identifier"],
            "task_revision": args["observed_revision"],
            "epoch": args["observed_epoch"],
            "agent_id": proof["agent_id"],
            "run_id": proof["run_id"],
            "generation": proof["generation"],
            "completion_revision": proof["completion_revision"],
            "brief": evidence["brief"],
            "model_judgments": args["judgments"],
            "host_facts": {
                "attachment_id": args["attachment_id"],
                "size": len(data),
                "sha256": proof["sha256"],
                "text_encoding": "utf-8",
                "verification": "at_record_time",
                "publication_id": proof["id"],
                "assistant_message_id": proof["assistant_message_id"],
                "call_id": proof["call_id"],
                "tool_message_id": proof["tool_message_id"],
                "report_id": args["report_id"],
                "completion_event_id": evidence["event"]["id"],
            },
            "recorded_by": {"session_id": owner[0], "run_id": owner[1]},
            "created_at": now(),
        }
        if json_size(record) > self.commitments.max_output_chars():
            raise EvidenceError("output_budget", "完整验收单超过输出预算，未保存")
        if self.store.db.in_transaction:
            raise RuntimeError("验收登记不能嵌套提交事务")
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            if self._host() != owner:
                raise EvidenceError("evidence_changed", "主助手执行已变化")
            if duplicate := self._duplicate(key, fingerprint, args["identifier"]):
                return duplicate
            if self._evidence(args, owner[0]) != evidence:
                raise EvidenceError("evidence_changed", "核验期间任务、完成代次或发布证据已变化")
            if self.store.db.execute(
                "SELECT 1 FROM objects WHERE kind='task_delivery' AND id=?", (record["id"],)
            ).fetchone():
                raise EvidenceError("delivery_key_conflict", "验收单 ID 已存在")
            self.store._event(
                "task.delivery_recorded",
                {"id": record["id"], "commitment_id": args["identifier"]},
                owner[0],
                objects=[
                    ("task_delivery", record),
                    (
                        "task_delivery_receipt",
                        {
                            "id": key,
                            "commitment_id": args["identifier"],
                            "fingerprint": fingerprint,
                            "delivery_id": record["id"],
                        },
                    ),
                ],
            )
        await self.runtime.bus.notify()
        return record

    def tool(self) -> Tool:
        verdict = parameters(
            {"status": {"type": "string", "enum": list(_VERDICTS)}, "reason": string()},
            ("status", "reason"),
        )
        constraint = parameters(
            {**verdict["properties"], "index": {"type": "integer", "minimum": 0}},
            ("index", "status", "reason"),
        )
        return Tool(
            "task_delivery_record",
            "显式登记直接队友准确完成代次的附件验收依据；宿主仅核本地副本字节，逐条满足与否是模型判断。"
            "不改变任务完成状态、授权或外发；相同去重键仅原样返回原单，参数不同冲突",
            parameters(
                {
                    "identifier": string(),
                    "observed_revision": {"type": "integer", "minimum": 1},
                    "observed_epoch": {"type": "integer", "minimum": 0},
                    "report_id": string(),
                    "attachment_id": string(),
                    "idempotency_key": string(),
                    "judgments": parameters(
                        {
                            "goal": verdict,
                            "constraints": {"type": "array", "items": constraint},
                        },
                        ("goal", "constraints"),
                    ),
                },
                tuple(sorted(_PARAMETERS)),
            ),
            self.record,
            "write",
            preserve_output=True,
        )
