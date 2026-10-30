"""Explicit owner questions bound to one durable task and its original worker.

This coordinator owns its synchronous transactions. It records coordination
facts; an answer is neither task completion nor permission for another effect.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from muli.attachments import validate_attachment_ids
from muli.evidence import EvidenceError, content_page, json_size, validate_integer
from muli.management_transaction import _object
from muli.owner_text import owner_text_prefix
from muli.reply_origin import ReplyOrigin
from muli.store import new_id, now
from muli.team_wait import TERMINAL

if TYPE_CHECKING:
    from muli.runtime import PreparedRequest, Runtime

_KIND = "owner_question"
_DIRECTORY_KIND = "owner_question_directory"
_MARKER = "owner_question_answer"
_STATUSES = {"open", "answered", "cancelled", "stale", "all"}
_MARKER_FIELDS = {
    "version",
    "question_id",
    "answer_revision",
    "task_id",
    "worker_id",
    "delivery_key",
}
_QUESTION_ID = re.compile(r"oq:[a-f0-9]{64}:[a-f0-9]{64}")
_BINDING_FIELDS = {
    "session_id",
    "commitment_id",
    "epoch",
    "approved_epoch",
    "replan_request_id",
    "brief_json",
    "plan_json",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8", errors="strict")).hexdigest()


def question_prefix(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id:
        raise EvidenceError("invalid_arguments", "问题任务 ID 必须是非空字符串")
    return "oq:" + _digest(task_id) + ":"


def owner_question_intent(text: str) -> tuple[str, str, str | None, str | None]:
    """Parse the entire original text, without trimming or interpreting prose."""
    if not isinstance(text, str):
        raise PermissionError("问题操作缺少准确主人原文")
    answer = re.fullmatch(r"/(回答问题|回答并继续) (\S+)\n([\s\S]+)", text)
    if answer is not None and answer[3].strip():
        if _QUESTION_ID.fullmatch(answer[2]) is None:
            raise PermissionError("请使用完整问题 ID")
        delivery = "record_only" if answer[1] == "回答问题" else "dispatch"
        return "answer", answer[2], delivery, answer[3]
    control = re.fullmatch(r"/(继续问题|取消问题) (\S+)", text)
    if control is not None and _QUESTION_ID.fullmatch(control[2]) is not None:
        return "dispatch" if control[1] == "继续问题" else "cancel", control[2], None, None
    raise PermissionError("本轮原文须完整使用 /回答问题、/回答并继续、/继续问题 或 /取消问题 模板")


class OwnerQuestions:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime, self.store = runtime, runtime.store

    @property
    def _bytes(self) -> int:
        return self.runtime.config.settings.limits.max_context_chars * 4

    @property
    def _output(self) -> int:
        return self.runtime.config.settings.limits.max_output_chars

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self.store.db.in_transaction:
            raise RuntimeError("主人问题协调不能接管已有事务")
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            yield

    def _object(self, kind: str, identifier: str) -> dict[str, Any]:
        item = self.store.bounded_object(kind, identifier, max_bytes=self._bytes, strict_json=True)
        if item is None:
            raise EvidenceError("evidence_not_found", "问题所需的准确持久记录不存在")
        return item

    def _record(self, question_id: str) -> dict[str, Any]:
        if not isinstance(question_id, str) or _QUESTION_ID.fullmatch(question_id) is None:
            raise EvidenceError("invalid_arguments", "问题 ID 必须是完整宿主 ID")
        item = self._object(_KIND, question_id)
        if (
            set(item)
            != {
                "id",
                "version",
                "revision",
                "task_id",
                "main_id",
                "worker_id",
                "key",
                "message",
                "attachment_ids",
                "status",
                "fingerprint",
                "binding",
                "author",
                "reply_origin",
                "answer",
                "delivery",
                "cancellation",
                "created_at",
                "updated_at",
                "notice",
            }
            or type(item.get("version")) is not int
            or item["version"] != 1
            or not isinstance(item.get("task_id"), str)
            or not question_id.startswith(question_prefix(item["task_id"]))
            or item.get("status") not in {"open", "answered", "cancelled"}
            or any(
                not isinstance(item.get(field), str) or not item[field]
                for field in ("main_id", "worker_id", "revision", "fingerprint", "key", "message")
            )
            or not isinstance(item.get("binding"), dict)
            or not isinstance(item.get("author"), dict)
            or not isinstance(item.get("reply_origin"), dict)
            or not isinstance(item.get("attachment_ids"), list)
            or (item.get("answer") is not None and not isinstance(item["answer"], dict))
            or (item.get("delivery") is not None and not isinstance(item["delivery"], dict))
        ):
            raise EvidenceError("content_unavailable", "问题记录版本或完整性不可核查")
        try:
            binding, author = item["binding"], item["author"]
            if (
                set(binding) != _BINDING_FIELDS
                or binding["session_id"] != item["worker_id"]
                or binding["commitment_id"] != item["task_id"]
                or any(
                    type(binding[field]) is not int or binding[field] < 0
                    for field in ("epoch", "approved_epoch")
                )
                or binding["epoch"] != binding["approved_epoch"]
                or any(not isinstance(binding[field], str) for field in ("brief_json", "plan_json"))
                or set(author) != {"run_id", "generation", "tool", "input", "archive"}
                or not isinstance(author["run_id"], str)
                or not author["run_id"]
                or type(author["generation"]) is not int
                or author["generation"] < 1
                or any(
                    not isinstance(author[field], dict) for field in ("tool", "input", "archive")
                )
                or any(
                    type(item[field]) not in {int, float} or not math.isfinite(item[field])
                    for field in ("created_at", "updated_at")
                )
                or ReplyOrigin.load(item["reply_origin"]).dump() != item["reply_origin"]
                or validate_attachment_ids(item["attachment_ids"]) != item["attachment_ids"]
                or item["id"]
                != question_prefix(item["task_id"]) + _digest([item["worker_id"], item["key"]])
                or item["fingerprint"]
                != _digest(
                    [
                        item["main_id"],
                        item["task_id"],
                        item["worker_id"],
                        author["run_id"],
                        author["generation"],
                        binding,
                        item["message"],
                        item["attachment_ids"],
                    ]
                )
            ):
                raise ValueError
            answer, delivery = item["answer"], item["delivery"]
            if answer is not None:
                if (
                    set(answer)
                    != {"revision", "owner", "interpretation", "delivery", "recorded_at"}
                    or not isinstance(answer["revision"], str)
                    or not answer["revision"]
                    or not isinstance(answer["interpretation"], str)
                    or answer["delivery"] not in {"record_only", "dispatch"}
                    or not isinstance(answer["owner"], dict)
                    or not isinstance(answer["owner"].get("body"), str)
                    or not answer["owner"]["body"].strip()
                    or not isinstance(answer["owner"].get("original_text"), str)
                ):
                    raise ValueError
                self._verify_owner_evidence(
                    answer["owner"], item["id"], action="answer", delivery=answer["delivery"]
                )
            if item["status"] == "answered" and answer is None:
                raise ValueError
            if delivery is not None and (
                answer is None
                or delivery.get("state") not in {"queued", "claimed", "cancelled"}
                or any(
                    not isinstance(delivery.get(field), str) or not delivery[field]
                    for field in ("key", "input_id")
                )
                or type(delivery.get("input_seq")) is not int
                or delivery["input_seq"] < 1
                or not isinstance(delivery.get("payload"), dict)
                or type(delivery.get("queued_generation")) is not int
                or delivery["queued_generation"] < author["generation"]
                or delivery.get("previous_agent_status") not in TERMINAL | {None}
            ):
                raise ValueError
            if delivery is not None:
                origin = delivery.get("owner")
                if not isinstance(origin, dict):
                    raise ValueError
                original_text = origin.get("original_text")
                if not isinstance(original_text, str):
                    raise ValueError
                intent = owner_question_intent(original_text)
                if (intent[0], intent[2]) not in {("answer", "dispatch"), ("dispatch", None)}:
                    raise ValueError
                self._verify_owner_evidence(
                    origin, item["id"], action=intent[0], delivery=intent[2]
                )
            cancellation = item.get("cancellation")
            if cancellation is not None:
                if not isinstance(cancellation, dict) or set(cancellation) != {
                    "owner",
                    "interpretation",
                }:
                    raise ValueError
                self._verify_owner_evidence(cancellation["owner"], item["id"], action="cancel")
            source, archive = self._input(author["input"]["id"])
            if (
                source != author["input"]
                or archive != author["archive"]
                or source.get("session_id") != item["worker_id"]
                or archive.get("session_id") != item["worker_id"]
                or archive.get("run_id") != author["run_id"]
                or archive.get("id") != source.get("id")
            ):
                raise ValueError
            tool = author["tool"]
            message = self._object("message", tool["message_id"])
            if (
                message.get("session_id") != item["worker_id"]
                or message.get("seq") != tool.get("seq")
                or message.get("role") != "assistant"
                or message.get("tool_calls") != tool.get("calls")
            ):
                raise ValueError
        except PermissionError as error:
            raise EvidenceError("content_unavailable", "问题原答的原始主人意图不可核查") from error
        except (
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            UnicodeError,
            OverflowError,
        ) as error:
            raise EvidenceError("content_unavailable", "问题记录字段或关联证据损坏") from error
        return item

    def _verify_owner_evidence(
        self,
        owner: dict[str, Any],
        question_id: str,
        *,
        action: str,
        delivery: str | None = None,
    ) -> None:
        """Re-prove stored derived text against the complete original ingress."""
        if not isinstance(owner, dict) or set(owner) != {
            "session_id",
            "run_id",
            "input_id",
            "input_seq",
            "archive_seq",
            "payload",
            "archive",
            "payload_digest",
            "archive_digest",
            "original_text",
            "body",
            "attachment_ids",
            "reply_origin",
        }:
            raise EvidenceError("content_unavailable", "主人原答来源字段不可完整核查")
        if (
            any(
                not isinstance(owner[field], str) or not owner[field]
                for field in (
                    "session_id",
                    "run_id",
                    "input_id",
                    "payload_digest",
                    "archive_digest",
                    "original_text",
                )
            )
            or any(
                type(owner[field]) is not int or owner[field] < 1
                for field in ("input_seq", "archive_seq")
            )
            or not isinstance(owner["payload"], dict)
            or not isinstance(owner["archive"], dict)
        ):
            raise EvidenceError("content_unavailable", "主人原答来源类型损坏")
        payload, archive = self._input(owner["input_id"])
        original = owner_text_prefix(payload)
        if original is None:
            raise EvidenceError("content_unavailable", "主人原答没有准确原文前缀证明")
        intent = owner_question_intent(original)
        if (
            payload.get("principal") != "owner"
            or payload.get("session_id") != owner["session_id"]
            or owner["session_id"] != self.store.existing_main_session_id()
            or archive.get("run_id") != owner["run_id"]
            or archive.get("input_seq") != owner["input_seq"]
            or archive.get("seq") != owner["archive_seq"]
            or payload != owner["payload"]
            or archive != owner["archive"]
            or _digest(payload) != owner["payload_digest"]
            or _digest(archive) != owner["archive_digest"]
            or original != owner["original_text"]
            or intent != (action, question_id, delivery, owner["body"])
            or validate_attachment_ids(payload.get("attachment_ids")) != owner["attachment_ids"]
            or ReplyOrigin.from_input(payload).dump() != owner["reply_origin"]
        ):
            raise EvidenceError("content_unavailable", "主人原答与准确原输入或完整存档不一致")

    @staticmethod
    def _owner_view(owner: dict[str, Any]) -> dict[str, Any]:
        fields: tuple[str, ...] = (
            "input_id",
            "input_seq",
            "archive_seq",
            "run_id",
            "attachment_ids",
            "reply_origin",
            "original_text",
            "body",
        )
        return {
            **{key: owner[key] for key in fields},
            "source": owner["payload"]["source"],
            "principal": owner["payload"]["principal"],
        }

    def _record_view(self, item: dict[str, Any]) -> dict[str, Any]:
        """Complete question/answer text with source locators, not repeated proofs."""
        view = {
            key: item[key]
            for key in (
                "id",
                "version",
                "task_id",
                "main_id",
                "worker_id",
                "message",
                "attachment_ids",
                "status",
                "notice",
                "created_at",
                "updated_at",
            )
        }
        view["author"] = {
            "run_id": item["author"]["run_id"],
            "generation": item["author"]["generation"],
            "tool": {key: item["author"]["tool"][key] for key in ("message_id", "seq")},
        }
        answer = item["answer"]
        view["answer"] = (
            None
            if answer is None
            else {
                **{
                    key: answer[key]
                    for key in ("revision", "interpretation", "delivery", "recorded_at")
                },
                "owner": self._owner_view(answer["owner"]),
            }
        )
        delivery = item["delivery"]
        view["delivery"] = (
            None
            if delivery is None
            else {
                **{
                    key: value
                    for key, value in delivery.items()
                    if key not in {"payload", "owner", "previous_agent_status", "queued_generation"}
                },
                "owner": {
                    "input_id": delivery["owner"]["input_id"],
                    "same_as": "answer.owner",
                }
                if delivery["owner"]["input_id"] == answer["owner"]["input_id"]
                else self._owner_view(delivery["owner"]),
            }
        )
        cancellation = item["cancellation"]
        view["cancellation"] = (
            None
            if cancellation is None
            else {
                "interpretation": cancellation["interpretation"],
                "owner": self._owner_view(cancellation["owner"]),
            }
        )
        return view

    def _fit_record(self, item: dict[str, Any]) -> None:
        if len(_json(item).encode("utf-8")) > self._bytes:
            raise EvidenceError("output_budget", "完整问题及主人原答超过持久核查字节预算，未保存")

    def _bump(self, task_id: str) -> None:
        self.store._put(_DIRECTORY_KIND, {"id": task_id, "version": 1, "revision": new_id()})

    def _write(self, item: dict[str, Any], event: str, actor: str) -> None:
        item["revision"] = new_id()
        item["updated_at"] = now()
        self._fit_record(item)
        self.store._event(
            event,
            {"question_id": item["id"], "task_id": item["task_id"]},
            actor,
            objects=[(_KIND, item)],
        )
        self._bump(item["task_id"])

    def revision(self, task_id: str) -> str:
        task = self._object("commitment", task_id)
        directory = self.store.bounded_object(
            _DIRECTORY_KIND, task_id, max_bytes=self._bytes, strict_json=True
        )
        if directory is not None and (
            type(directory.get("version")) is not int
            or directory["version"] != 1
            or not isinstance(directory.get("revision"), str)
            or not directory["revision"]
        ):
            raise EvidenceError("content_unavailable", "问题目录修订不可核查")
        return _digest(
            [self.store.instance_id, task_id, task, directory, self._bytes, self._output]
        )

    def _host(self) -> tuple[str, str, PreparedRequest]:
        runtime = self.runtime
        owner = runtime._tool_execution.get()
        active = runtime._active_tool.get()
        task = asyncio.current_task()
        if owner is None:
            raise PermissionError("主人问题操作缺少真实工具执行上下文")
        caller, run_id = owner
        request = runtime.request_context(caller)
        if (
            runtime._closing
            or runtime.team._closed
            or runtime.tool_session_id != caller
            or runtime.run_ids.get(caller) != run_id
            or runtime.tasks.get(caller) is not task
            or task is None
            or task.done()
            or task.cancelling()
            or runtime.team._parent_stopping(caller)
            or task in runtime.team._stopping_tasks
            or request is None
            or active is None
            or active.name != "send_input"
            or "send_input" in runtime.config.settings.disabled_tools
            or self.store.pending_inputs(caller)
        ):
            raise PermissionError("主人问题操作所属执行已改变、停止或收到新输入")
        runtime.maintenance.admission.require()
        return caller, run_id, request

    def _tool_evidence(self, caller: str, request: PreparedRequest) -> dict[str, Any]:
        sequence = request.tool_batch_seq
        if type(sequence) is not int:
            raise PermissionError("问题登记缺少实际工具批次证据")
        row = self.store.db.execute(
            "SELECT message_id FROM message_order WHERE session_id=? AND seq=?", (caller, sequence)
        ).fetchone()
        if row is None:
            raise PermissionError("问题登记工具批次未持久化")
        message = self._object("message", row[0])
        if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
            raise PermissionError("问题登记工具批次不可核查")
        return {"message_id": message["id"], "seq": sequence, "calls": message["tool_calls"]}

    def _input(self, input_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(input_id, str) or not input_id:
            raise EvidenceError("content_unavailable", "原输入定位必须是准确非空 ID")
        row = self.store.db.execute(
            "SELECT seq,session_id,state,run_id,delivered_seq,received FROM ingress WHERE id=?",
            (input_id,),
        ).fetchone()
        if row is None or row["state"] != "delivered":
            raise EvidenceError("content_unavailable", "原输入没有准确领取事实")
        raw = self.store.ingress_audit_bytes(row["seq"], max_bytes=self._bytes)
        if raw is None:
            raise EvidenceError("content_unavailable", "原输入正文不可核查")
        payload = _object(raw)
        archive = self._object("message", input_id)
        projected = self.store.input_archive(
            payload, received=row["received"], run_id=row["run_id"], input_seq=row["seq"]
        )
        if (
            payload.get("id") != input_id
            or payload.get("session_id") != row["session_id"]
            or archive.get("seq") != row["delivered_seq"]
            or any(archive.get(key) != value for key, value in projected.items())
        ):
            raise EvidenceError("content_unavailable", "原输入与完整领取存档不一致")
        return payload, archive

    def _register_host(
        self, caller: str, run_id: str | None, generation: int | None, target: str
    ) -> tuple[dict[str, Any], dict[str, Any], PreparedRequest]:
        real_caller, real_run, request = self._host()
        main_id = self.store.existing_main_session_id()
        if (
            real_caller != caller
            or real_run != run_id
            or type(generation) is not int
            or generation < 1
            or main_id is None
            or target != main_id
        ):
            raise PermissionError("登记只能来自当前直属队友并发给固定主助手")
        agent, session = self._object("agent", caller), self._object("session", caller)
        run, parent = self._object("run", real_run), self._object("session", main_id)
        if (
            agent.get("session_id") != caller
            or agent.get("parent_id") != main_id
            or agent.get("role") != "worker"
            or agent.get("status") != "running"
            or agent.get("run_id") != real_run
            or type(agent.get("generation")) is not int
            or agent["generation"] != generation
            or session.get("kind") != "team"
            or session.get("role") != "worker"
            or session.get("principal") != "owner"
            or session.get("workflow_run_id")
            or "scheduled_workflow" in session
            or run.get("session_id") != caller
            or run.get("role") != "worker"
            or run.get("status") != "running"
            or parent.get("kind") != "main"
            or parent.get("principal") != "owner"
            or request.consult is not None
            or request.selected_replan is not None
            or request.channel_ownership is not None
            or request.attention_routing is not None
            or request.shell_continuation is not None
            or self.runtime.channel_ownership.for_agent(caller) is not None
        ):
            raise PermissionError("仅当前普通直属 worker 可登记主人问题")
        control = self.runtime.task_control
        scope = request.execution_scope
        if control is None or scope is None or agent.get("commitment_id") != scope.commitment_id:
            raise PermissionError("主人问题必须绑定完整受控任务说明")
        self.runtime._guard_request(request, "write")
        current = control.execution_snapshot(caller)
        task = self._object("commitment", scope.commitment_id)
        if (
            current is None
            or replace(current, revision=scope.revision) != scope
            or task.get("status") in {None, "done", "cancelled"}
        ):
            raise PermissionError("登记所依完整任务说明或当前方案已改变")
        return agent, task, request

    def _owner(self, action: str, question_id: str, delivery: str | None = None) -> dict[str, Any]:
        caller, run_id, request = self._host()
        archive = self.runtime._owner_round_input(request, run_id)
        if caller != self.store.existing_main_session_id() or archive is None:
            raise PermissionError("仅当前真实主助手已领取的主人输入可操作问题")
        self.runtime._guard_request(request, "write")
        payload, checked = self._input(archive["id"])
        if checked != archive:
            raise EvidenceError("content_unavailable", "主人原输入证据已改变")
        text = owner_text_prefix(payload)
        if text is None:
            raise PermissionError("本轮输入没有可核查的原始主人文本前缀")
        parsed_action, parsed_id, parsed_delivery, body = owner_question_intent(text)
        if (parsed_action, parsed_id, parsed_delivery) != (action, question_id, delivery):
            raise PermissionError("模型选择的问题动作、ID或交回方式与本轮主人原文不符")
        references = self.runtime.team._attachment_references(payload.get("attachment_ids"))
        return {
            "session_id": caller,
            "run_id": run_id,
            "input_id": payload["id"],
            "input_seq": checked["input_seq"],
            "archive_seq": checked["seq"],
            "payload": payload,
            "archive": checked,
            "payload_digest": _digest(payload),
            "archive_digest": _digest(checked),
            "original_text": text,
            "body": body,
            "attachment_ids": references,
            "reply_origin": request.reply_origin.dump(),
        }

    def _binding(self, item: dict[str, Any]) -> str:
        """Generation proves authorship; later legal generations keep the binding."""
        try:
            agent = self._object("agent", item["worker_id"])
            session = self._object("session", item["worker_id"])
            task = self._object("commitment", item["task_id"])
            control = self.runtime.task_control
            if control is None:
                return "stale"
            state = control._state(task)
            binding = item["binding"]
            current = control.execution_snapshot(item["worker_id"])
            if (
                self.store.existing_main_session_id() != item["main_id"]
                or task.get("status") in {None, "done", "cancelled"}
                or agent.get("parent_id") != item["main_id"]
                or agent.get("session_id") != item["worker_id"]
                or agent.get("role") != "worker"
                or agent.get("commitment_id") != item["task_id"]
                or agent.get("status") not in TERMINAL | {"queued", "running"}
                or type(agent.get("generation")) is not int
                or agent["generation"] < item["author"]["generation"]
                or session.get("kind") != "team"
                or session.get("role") != "worker"
                or session.get("workflow_run_id")
                or "scheduled_workflow" in session
                or current is None
                or current.brief_json != binding.get("brief_json")
                or self.runtime.channel_ownership.for_agent(item["worker_id"]) is not None
                or ReplyOrigin.load(agent.get("reply_origin")).dump() != item["reply_origin"]
            ):
                return "stale"
            if state["epoch"] > state["approved_epoch"]:
                # A pure pause advances its control epoch once, without
                # replacing the approved plan. Replanning or another epoch
                # cannot reclassify an obsolete question as merely paused.
                return (
                    "paused"
                    if state["epoch"] == binding.get("epoch", -1) + 1
                    and state["approved_epoch"] == binding.get("approved_epoch")
                    and current.plan_json == binding.get("plan_json")
                    and state["replan_request_id"] is None
                    else "stale"
                )
            if any(
                asdict(current).get(field) != binding.get(field)
                for field in (
                    "session_id",
                    "commitment_id",
                    "epoch",
                    "approved_epoch",
                    "brief_json",
                    "plan_json",
                    "replan_request_id",
                )
            ):
                return "stale"
            return "current"
        except (EvidenceError, ValueError, KeyError, TypeError):
            return "unavailable"

    def _verify_delivery(self, item: dict[str, Any]) -> None:
        delivery = item.get("delivery")
        if delivery is None:
            return
        row = self.store.db.execute(
            "SELECT seq,id,session_id,state,delivered_seq,run_id FROM ingress WHERE id=?",
            (delivery["input_id"],),
        ).fetchone()
        if row is None:
            raise EvidenceError("content_unavailable", "问题交回的原邮箱项已缺失")
        raw = self.store.ingress_audit_bytes(row["seq"], max_bytes=self._bytes)
        if (
            raw is None
            or _object(raw) != delivery["payload"]
            or row["seq"] != delivery["input_seq"]
            or row["session_id"] != item["worker_id"]
            or row["state"]
            != {"queued": "pending", "claimed": "delivered", "cancelled": "cancelled"}[
                delivery["state"]
            ]
        ):
            raise EvidenceError("content_unavailable", "问题交回事实与准确原邮箱项不一致")
        if delivery["state"] == "claimed":
            claim = delivery.get("claim")
            if (
                not isinstance(claim, dict)
                or claim.get("run_id") != row["run_id"]
                or claim.get("message_id") != row["id"]
                or claim.get("seq") != row["delivered_seq"]
                or claim.get("input_seq") != row["seq"]
            ):
                raise EvidenceError("content_unavailable", "问题领取回执与实际存档位置不一致")
            self._input(delivery["input_id"])

    def _metadata(self, item: dict[str, Any]) -> dict[str, Any]:
        binding = self._binding(item)
        self._verify_delivery(item)
        delivery = item.get("delivery")
        answer = item.get("answer")
        status = item["status"]
        return {
            "question_id": item["id"],
            "record_id": item["id"],
            "record_revision": _digest(item),
            "task_id": item["task_id"],
            "worker_id": item["worker_id"],
            "status": (
                "stale"
                if status != "cancelled" and binding not in {"current", "paused"}
                else status
            ),
            "recorded_status": status,
            "binding": binding,
            "dispatch_allowed": (
                binding == "current" and status == "answered" and delivery is None
            ),
            "answered": answer is not None,
            "answer_recorded": answer is not None,
            "delivery_state": delivery["state"] if delivery else "not_dispatched",
            "queued": bool(delivery and delivery["state"] == "queued"),
            "claimed": bool(delivery and delivery["state"] == "claimed"),
            "cancellation_effect": (
                "claimed_answer_cannot_be_retracted_task_not_stopped"
                if status == "cancelled" and delivery and delivery["state"] == "claimed"
                else "question_closed"
                if status == "cancelled"
                else "not_cancelled"
            ),
            "task_completion": "separate_evidence_required",
        }

    def _result(self, item: dict[str, Any], *, duplicate: bool) -> dict[str, Any]:
        metadata = self._metadata(item)
        result: dict[str, Any] = {
            "accepted": True,
            "duplicate": duplicate,
            **{
                key: metadata[key]
                for key in (
                    "question_id",
                    "status",
                    "binding",
                    "answer_recorded",
                    "delivery_state",
                    "queued",
                    "claimed",
                    "task_completion",
                    "dispatch_allowed",
                )
            },
            "authority": "coordination_data",
            "read_arguments": {
                "identifier": item["task_id"],
                "section": "owner_questions",
                "record_id": item["id"],
            },
        }
        if item["status"] == "cancelled":
            result["cancellation_effect"] = metadata["cancellation_effect"]
        if json_size(result) > self._output:
            raise EvidenceError("output_budget", "问题核心回执超过输出预算，未提交此次操作")
        # Give the next useful exact command when it fits. Repeating the full
        # question ID in four templates must not hide an already committed ID.
        template: tuple[str, str] | None = None
        if item["status"] == "open":
            template = "record_only", f"/回答问题 {item['id']}\n在此填写原答"
        elif item["status"] == "answered" and item["delivery"] is None:
            template = "dispatch", f"/继续问题 {item['id']}"
        if template is not None:
            candidate = {**result, "owner_templates": {template[0]: template[1]}}
            if json_size(candidate) <= self._output:
                result = candidate
        return result

    async def register(
        self,
        caller: str,
        run_id: str | None,
        generation: int | None,
        *,
        key: str,
        message: str,
        target: str,
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(key, str)
            or not key.strip()
            or len(key) > 256
            or not isinstance(message, str)
            or not message.strip()
            or len(message) > self.runtime.config.settings.limits.max_context_chars
        ):
            raise EvidenceError("invalid_arguments", "问题正文和稳定 key 必须是有界非空文本")
        with self._transaction():
            agent, task, request = self._register_host(caller, run_id, generation, target)
            references = self.runtime.team._attachment_references(attachment_ids)
            scope = request.execution_scope
            assert scope is not None
            binding = asdict(scope)
            binding.pop("revision")
            fingerprint = _digest(
                [target, task["id"], caller, run_id, generation, binding, message, references]
            )
            identifier = question_prefix(task["id"]) + _digest([caller, key])
            previous = self.store.bounded_object(
                _KIND, identifier, max_bytes=self._bytes, strict_json=True
            )
            if previous is not None:
                previous = self._record(identifier)
                if previous["fingerprint"] != fingerprint:
                    raise EvidenceError(
                        "question_key_conflict", "相同 key 的提问内容或原执行绑定不同"
                    )
                return self._result(previous, duplicate=True)
            tool = self._tool_evidence(caller, request)
            rows = self.store.db.execute(
                "SELECT id FROM ingress WHERE session_id=? AND run_id=? AND state='delivered' "
                "ORDER BY seq DESC LIMIT 1",
                (caller, run_id),
            ).fetchall()
            if not rows:
                raise PermissionError("问题登记缺少当前队友实际领取的任务输入")
            source, archive = self._input(rows[0][0])
            origin = ReplyOrigin.load(agent.get("reply_origin"))
            item = {
                "id": identifier,
                "version": 1,
                "revision": new_id(),
                "task_id": task["id"],
                "main_id": target,
                "worker_id": caller,
                "key": key,
                "message": message,
                "attachment_ids": references,
                "status": "open",
                "fingerprint": fingerprint,
                "binding": binding,
                "author": {
                    "run_id": run_id,
                    "generation": generation,
                    "tool": tool,
                    "input": source,
                    "archive": archive,
                },
                "reply_origin": origin.dump(),
                "answer": None,
                "delivery": None,
                "cancellation": None,
                "created_at": now(),
                "updated_at": now(),
            }
            self._fit_record(item)
            payload = self.runtime.conversation.payload(
                target,
                self.runtime.team._message(
                    caller,
                    f"待主人回答的问题 {identifier}\n{message}\n\n"
                    "主人可用 /回答问题 或 /回答并继续 加上本问题完整 ID，换行后填写原答；"
                    "已登记后用 /继续问题 加完整 ID；关闭用 /取消问题 加完整 ID。",
                ),
                source=f"team-progress:{caller}",
                principal=f"agent:{caller}",
                mode=self._object("session", target)["mode"],
                attachment_ids=references,
                event_id="owner-question-register:" + identifier,
                reply_origin=origin,
            )
            receipt, inserted = self.store._enqueue_input(
                payload,
                max_pending=self.runtime.config.settings.limits.max_pending_runs,
                apply_objects_on_duplicate=False,
            )
            if not inserted:
                raise EvidenceError("content_unavailable", "问题登记原对象缺失，不能领养孤立通知")
            item["notice"] = {"input_id": receipt["id"], "input_seq": receipt["seq"]}
            self._write(item, "owner_question.registered", caller)
            result = self._result(item, duplicate=False)
        self.runtime._wake_main()
        await self.runtime.bus.notify()
        return result

    def _target(self, item: dict[str, Any], target: str) -> None:
        if target != item["worker_id"]:
            raise PermissionError("问题只能交回原持久队友，不允许另选目标")

    def _enqueue_answer(
        self, item: dict[str, Any], owner: dict[str, Any], interpretation: str
    ) -> None:
        if self._binding(item) != "current":
            raise PermissionError("任务已暂停、结束或完整要求已改变，旧答不能交回执行")
        agent = self._object("agent", item["worker_id"])
        if agent.get("status") == "paused":
            raise PermissionError("暂停队友必须经过原任务重规划与批准")
        if agent.get("status") in TERMINAL:
            self.runtime.team._admit(replacing=agent["id"])
        answer = item["answer"]
        if not isinstance(answer, dict):
            raise EvidenceError("question_unanswered", "问题尚无主人原答，不能继续")
        previous_status = agent["status"] if agent["status"] in TERMINAL else None
        if agent["status"] == "queued":
            # Pending questions share the same dormant activation. Propagate
            # its original terminal fact through this finite mailbox window,
            # so cancelling the last one can undo exactly that activation.
            rows = self.store.db.execute(
                "SELECT * FROM ingress WHERE session_id=? AND state='pending' ORDER BY seq LIMIT ?",
                (item["worker_id"], self.runtime.config.settings.limits.max_pending_runs),
            ).fetchall()
            for row in rows:
                raw = self.store.ingress_audit_bytes(row["seq"], max_bytes=self._bytes)
                if raw is None:
                    raise EvidenceError("content_unavailable", "待处理输入的完整来源不可核查")
                pending_payload = _object(raw)
                if _MARKER not in pending_payload:
                    continue
                previous = self._claim_item(row, pending_payload)["delivery"]
                origin_status = previous.get("previous_agent_status")
                if (
                    previous.get("queued_generation") == agent["generation"]
                    and origin_status in TERMINAL
                ):
                    if previous_status is not None and previous_status != origin_status:
                        raise EvidenceError("content_unavailable", "同一排队代次的原终态凭据不一致")
                    previous_status = origin_status
        key = "owner-question-answer:" + item["id"]
        marker = {
            "version": 1,
            "question_id": item["id"],
            "answer_revision": answer["revision"],
            "task_id": item["task_id"],
            "worker_id": item["worker_id"],
            "delivery_key": key,
        }
        content = (
            f"主人已明确交回问题 {item['id']} 的原答；这是原任务补充，不增加其他授权。\n"
            f"队友原问：\n{item['message']}\n\n"
            f"准确主人原文：\n{answer['owner']['original_text']}\n\n"
            f"主人原答：\n{answer['owner']['body']}\n\n"
            f"主助手协调解释（不是主人原文）：\n{answer['interpretation']}\n\n"
            f"本次交回协调解释（不是主人原文）：\n{interpretation}"
        )
        payload = self.runtime.conversation.payload(
            item["worker_id"],
            content,
            source=f"team:{item['main_id']}",
            principal=f"agent:{item['main_id']}",
            mode=agent["mode"],
            attachment_ids=answer["owner"]["attachment_ids"],
            event_id=key,
            reply_origin=ReplyOrigin.load(item["reply_origin"]),
        )
        payload[_MARKER] = marker
        receipt, inserted = self.store._enqueue_input(
            payload,
            max_pending=self.runtime.config.settings.limits.max_pending_runs,
            apply_objects_on_duplicate=False,
        )
        if not inserted:
            raise EvidenceError("content_unavailable", "问题交回收据缺失，不能重建或重放旧邮箱项")
        if agent.get("status") in TERMINAL:
            self.store._put("agent", {**agent, "status": "queued", "updated_at": now()})
        item["delivery"] = {
            "key": key,
            "state": "queued",
            "input_id": receipt["id"],
            "input_seq": receipt["seq"],
            "payload": payload,
            "owner": owner,
            "queued_at": now(),
            "claim": None,
            "interpretation": interpretation,
            "queued_generation": agent["generation"],
            "previous_agent_status": previous_status,
        }

    def _restore_unstarted_queue(self, item: dict[str, Any]) -> None:
        """Undo only this delivery's dormant activation, never a live execution."""
        delivery = item["delivery"]
        previous = delivery.get("previous_agent_status")
        agent = self._object("agent", item["worker_id"])
        if (
            previous in TERMINAL
            and agent.get("status") == "queued"
            and agent.get("generation") == delivery.get("queued_generation")
            and not self.store.pending_inputs(item["worker_id"])
        ):
            self.store._put("agent", {**agent, "status": previous, "updated_at": now()})

    async def answer(
        self, *, question_id: str, target: str, interpretation: str, delivery: str
    ) -> dict[str, Any]:
        if not isinstance(delivery, str) or delivery not in {"record_only", "dispatch"}:
            raise EvidenceError("invalid_arguments", "回答必须明确 record_only 或 dispatch")
        return await self._owner_action(
            "answer", question_id, target, interpretation, delivery=delivery
        )

    async def dispatch(
        self, *, question_id: str, target: str, interpretation: str
    ) -> dict[str, Any]:
        return await self._owner_action("dispatch", question_id, target, interpretation)

    async def cancel(self, *, question_id: str, target: str, interpretation: str) -> dict[str, Any]:
        return await self._owner_action("cancel", question_id, target, interpretation)

    async def _owner_action(
        self,
        action: str,
        question_id: str,
        target: str,
        interpretation: str,
        *,
        delivery: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(interpretation, str) or not interpretation.strip():
            raise EvidenceError("invalid_arguments", "message 必须保留主助手协调解释")
        start = False
        with self._transaction():
            owner = self._owner(action, question_id, delivery)
            item = self._record(question_id)
            self._target(item, target)
            self._verify_delivery(item)
            if action == "cancel":
                if item["status"] == "cancelled":
                    return self._result(item, duplicate=True)
                pending = item.get("delivery")
                if pending is not None and pending["state"] == "queued":
                    row = self.store.db.execute(
                        "SELECT * FROM ingress WHERE id=?", (pending["input_id"],)
                    ).fetchone()
                    if row is None or row["state"] != "pending":
                        raise EvidenceError(
                            "content_unavailable", "待领取问题答案的准确邮箱项不可核查"
                        )
                    self.store.db.execute(
                        "UPDATE ingress SET state='cancelled' WHERE seq=? AND id=? AND state='pending'",
                        (row["seq"], pending["input_id"]),
                    )
                    pending["state"] = "cancelled"
                    pending["cancelled_at"] = now()
                    self._restore_unstarted_queue(item)
                item["status"] = "cancelled"
                item["cancellation"] = {"owner": owner, "interpretation": interpretation}
                self._write(item, "owner_question.cancelled", owner["session_id"])
            else:
                if item["status"] == "cancelled":
                    raise EvidenceError("question_cancelled", "问题已取消，不能重新回答或交回")
                if action == "answer":
                    previous = item.get("answer")
                    if previous is not None:
                        same = previous["owner"]["input_id"] == owner["input_id"]
                        if not same:
                            raise EvidenceError(
                                "question_answered", "问题已有原答，不允许后续输入覆写"
                            )
                        if previous["delivery"] != delivery:
                            raise EvidenceError(
                                "question_answer_conflict", "相同原答不能改变交回方式"
                            )
                        if previous["interpretation"] != interpretation:
                            raise EvidenceError(
                                "question_answer_conflict", "相同原答重试的协调解释不同"
                            )
                        return self._result(item, duplicate=True)
                    binding = self._binding(item)
                    if binding not in {"current", "paused"}:
                        raise EvidenceError(
                            "question_stale", "原任务绑定或完整要求已改变，问题已失效"
                        )
                    item["answer"] = {
                        "revision": new_id(),
                        "owner": owner,
                        "interpretation": interpretation,
                        "delivery": delivery,
                        "recorded_at": now(),
                    }
                    item["status"] = "answered"
                if action == "dispatch" or delivery == "dispatch":
                    if item.get("delivery") is not None:
                        return self._result(item, duplicate=True)
                    self._enqueue_answer(item, owner, interpretation)
                    start = True
                self._write(
                    item,
                    "owner_question.dispatched" if start else "owner_question.answered",
                    owner["session_id"],
                )
            result = self._result(item, duplicate=False)
        if start:
            self.runtime.team.cancel_shell_continuation(target, "主人已明确交回问题原答")
            self.runtime.team.start()
        await self.runtime.bus.notify()
        return result

    def _claim_item(self, row: sqlite3.Row, payload: dict[str, Any]) -> dict[str, Any]:
        marker = payload.get(_MARKER)
        if (
            not isinstance(marker, dict)
            or set(marker) != _MARKER_FIELDS
            or type(marker.get("version")) is not int
            or marker["version"] != 1
            or any(
                not isinstance(marker[field], str) or not marker[field]
                for field in _MARKER_FIELDS - {"version"}
            )
        ):
            raise EvidenceError("content_unavailable", "问题答案入站标记损坏")
        item = self._record(marker["question_id"])
        answer, delivery = item.get("answer"), item.get("delivery")
        if (
            answer is None
            or delivery is None
            or marker["answer_revision"] != answer.get("revision")
            or marker["task_id"] != item["task_id"]
            or marker["worker_id"] != item["worker_id"]
            or marker["delivery_key"] != delivery.get("key")
            or row["id"] != delivery.get("input_id")
            or row["seq"] != delivery.get("input_seq")
            or row["session_id"] != item["worker_id"]
            or payload != delivery.get("payload")
        ):
            raise EvidenceError("content_unavailable", "问题答案与原对象、原答或唯一邮箱不一致")
        return item

    def admit_claim_in_transaction(
        self, row: sqlite3.Row, payload: dict[str, Any], run_id: str
    ) -> bool:
        if not self.store.db.in_transaction:
            raise RuntimeError("问题答案领取核验必须位于 Store 原事务")
        item = self._claim_item(row, payload)
        run = self._object("run", run_id)
        agent = self._object("agent", item["worker_id"])
        binding = self._binding(item)
        if binding == "unavailable":
            raise EvidenceError("content_unavailable", "问题答案领取绑定不可完整核查")
        valid = (
            item["status"] == "answered"
            and item["delivery"]["state"] == "queued"
            and binding == "current"
            and run.get("session_id") == item["worker_id"]
            and run.get("role") == "worker"
            and run.get("status") == "running"
            and agent.get("run_id") == run_id
            and agent.get("status") == "running"
            and type(agent.get("generation")) is int
            and agent["generation"] >= item["author"]["generation"]
        )
        if not valid and item["delivery"]["state"] == "queued":
            item["delivery"].update(state="cancelled", reason="binding_stale", cancelled_at=now())
            self._write(item, "owner_question.delivery_stale", item["worker_id"])
        return valid

    def claimed_in_transaction(
        self, row: sqlite3.Row, payload: dict[str, Any], message: dict[str, Any], run_id: str
    ) -> None:
        if not self.store.db.in_transaction:
            raise RuntimeError("问题答案领取回执必须与 Store 存档同事务")
        item = self._claim_item(row, payload)
        if item["delivery"]["state"] != "queued":
            raise EvidenceError("content_unavailable", "问题答案不再等待领取")
        canonical, archive = self._input(payload["id"])
        if (
            canonical != payload
            or archive != message
            or message.get("run_id") != run_id
            or message.get("input_seq") != row["seq"]
        ):
            raise EvidenceError("content_unavailable", "问题答案领取存档与原入站不一致")
        item["delivery"].update(
            state="claimed",
            claim={
                "run_id": run_id,
                "message_id": message["id"],
                "seq": message["seq"],
                "input_seq": row["seq"],
                "claimed_at": now(),
            },
        )
        self._write(item, "owner_question.claimed", item["worker_id"])

    def cancelled_in_transaction(self, row: sqlite3.Row, payload: dict[str, Any] | None) -> None:
        if not self.store.db.in_transaction:
            raise RuntimeError("问题答案取消必须与精确邮箱项同事务")
        if payload is None:
            raw = self.store.ingress_audit_bytes(row["seq"], max_bytes=self._bytes)
            if raw is None:
                raise EvidenceError("content_unavailable", "问题答案原输入不可核查，未取消")
            payload = _object(raw)
        item = self._claim_item(row, payload)
        if item["delivery"]["state"] != "queued":
            return
        item["delivery"].update(state="cancelled", reason="input_cancelled", cancelled_at=now())
        self._restore_unstarted_queue(item)
        self._write(item, "owner_question.delivery_cancelled", item["worker_id"])

    def read(
        self,
        task_id: str,
        *,
        question_status: str | None = None,
        cursor: str | None = None,
        revision: str | None = None,
        record_id: str | None = None,
        record_revision: str | None = None,
        offset_bytes: int = 0,
        length_bytes: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        status = question_status or "open"
        if status not in _STATUSES:
            raise EvidenceError("invalid_arguments", "问题状态筛选无效")
        validate_integer(offset_bytes, "offset_bytes")
        if length_bytes is not None:
            validate_integer(length_bytes, "length_bytes", minimum=1)
        validate_integer(limit, "limit", minimum=1)
        token = self.revision(task_id)
        if revision is not None and revision != token:
            raise EvidenceError("evidence_changed", "问题目录已改变，请从首页重读")
        if record_id is not None:
            if cursor is not None or question_status is not None:
                raise EvidenceError("invalid_arguments", "原记录正文读取不能混合目录筛选或游标")
            item = self._record(record_id)
            self._verify_delivery(item)
            if item["task_id"] != task_id:
                raise EvidenceError("evidence_not_found", "问题不属于该任务")
            record_token = _digest(item)
            if record_revision is not None and record_revision != record_token:
                raise EvidenceError("evidence_changed", "问题原记录已改变，请从首字节重读")
            if offset_bytes and record_revision is None:
                raise EvidenceError("invalid_arguments", "续读原文须保持 record_revision")
            raw = json.dumps(
                self._record_view(item),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            return content_page(
                {
                    "id": task_id,
                    "section": "owner_questions",
                    "record_id": record_id,
                    "record_revision": record_token,
                    "authority": "coordination_data",
                },
                raw[offset_bytes : offset_bytes + min(length_bytes or self._bytes, self._bytes)],
                offset_bytes=offset_bytes,
                total_bytes=len(raw),
                max_output_chars=self._output,
            )
        if record_revision is not None or offset_bytes:
            raise EvidenceError("invalid_arguments", "原文坐标需要准确 record_id")
        prefix, after = question_prefix(task_id), ""
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > self._output:
                    raise ValueError
                decoded = _object(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if set(decoded) != {"v", "revision", "status", "after"} or decoded != {
                    "v": 1,
                    "revision": token,
                    "status": status,
                    "after": decoded["after"],
                }:
                    raise ValueError
                after = decoded["after"]
                if not isinstance(after, str) or not after.startswith(prefix):
                    raise ValueError
            except (ValueError, KeyError, binascii.Error, TypeError) as error:
                raise EvidenceError("evidence_changed", "问题游标失效或筛选已改变") from error
        maximum = min(limit, self.runtime.config.settings.limits.max_search_results)
        rows = self.store.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB))<=? "
            "THEN CAST(id AS BLOB) END AS id,length(CAST(data AS BLOB)) AS size "
            "FROM objects WHERE kind=? AND id>? AND id>=? AND id<? ORDER BY id LIMIT ?",
            (
                len(prefix) + hashlib.sha256().digest_size * 2,
                _KIND,
                after or prefix,
                prefix,
                prefix + "\uffff",
                maximum + 1,
            ),
        ).fetchall()
        result: dict[str, Any] = {
            "id": task_id,
            "section": "owner_questions",
            "revision": token,
            "question_status": status,
            "authority": "coordination_data",
            "source_completeness": "candidate_window_only",
            "items": [],
            "scanned": 0,
            "omitted": 0,
            "unavailable": 0,
            "has_more": False,
            "next_cursor": None,
        }
        remaining = self._bytes
        deadline = time.monotonic() + self.runtime.config.settings.limits.max_search_seconds
        for index, row in enumerate(rows[:maximum]):
            if time.monotonic() > deadline:
                if not result["scanned"]:
                    raise EvidenceError("output_budget", "问题目录候选核查已达到时间预算")
                break
            if not isinstance(row["id"], bytes):
                raise EvidenceError("content_unavailable", "问题目录主键超过核查字节窗口")
            identifier = row["id"].decode("utf-8", errors="strict")
            candidate = {**result, "items": list(result["items"]), "scanned": result["scanned"] + 1}
            try:
                if type(row["size"]) is not int or row["size"] > remaining:
                    raise EvidenceError("output_budget", "问题候选超过本页剩余原文核查预算")
                remaining -= row["size"]
                item = self._record(identifier)
                if item["task_id"] != task_id:
                    raise EvidenceError("content_unavailable", "问题主键与任务绑定不一致")
                metadata = self._metadata(item)
                if metadata["binding"] == "unavailable":
                    candidate["unavailable"] += 1
                if status == "all" or metadata["status"] == status:
                    candidate["items"].append(metadata)
                else:
                    candidate["omitted"] += 1
            except (EvidenceError, ValueError, KeyError, TypeError):
                candidate["unavailable"] += 1
                candidate["omitted"] += 1
            more = len(rows) > index + 1
            candidate.update(
                has_more=more,
                next_cursor=base64.urlsafe_b64encode(
                    _json(
                        {"v": 1, "revision": token, "status": status, "after": identifier}
                    ).encode()
                ).decode()
                if more
                else None,
            )
            if json_size(candidate) > self._output:
                if not result["scanned"]:
                    raise EvidenceError("output_budget", "预算不足以返回问题目录首项与续页坐标")
                break
            result = candidate
        if json_size(result) > self._output:
            raise EvidenceError("output_budget", "问题目录超过输出预算")
        return result
