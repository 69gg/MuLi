"""On-demand task evidence; read-only views never select or approve a plan."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from collections.abc import Callable
from typing import Any

from muli.commitments import Commitments
from muli.evidence import EvidenceError, content_page, json_size, validate_integer
from muli.history_evidence import HistoryEvidence
from muli.memory import Memory
from muli.store import SQLITE_MAX_INTEGER, new_id
from muli.task_control import ControlUnavailable, TaskControl
from muli.task_delivery import delivery_current, delivery_prefix, delivery_record
from muli.tools import Tool, parameters, string

SECTIONS = (
    "overview",
    "task",
    "agents",
    "reports",
    "workflows",
    "evidence",
    "memories",
    "handoff",
    "deliveries",
    "delivery_review",
    "current_wait",
    "owner_questions",
)
_MAX_HANDOFF_RECORDS = 100
WaitProjection = Callable[
    [dict[str, Any], dict[str, Any], list[dict[str, Any]], int, int], dict[str, Any]
]
_KINDS = {"agents": "agent", "reports": "team_report", "workflows": "workflow_run"}
_FIELDS = {
    "agents": ("id", "name", "status", "session_id", "run_id", "completion_revision"),
    "reports": ("id", "agent_id", "status", "execution_status", "created_at"),
    "workflows": ("id", "name", "workflow_id", "status", "created_at", "updated_at"),
}
# Follow the same mixed agent/workflow parent chain as TaskControl.resolve_scope.
# UNION (not UNION ALL) terminates cycles, which the per-record check then rejects.
_SCOPED = (
    "WITH RECURSIVE links(session_id,parent_id,binding) AS ("
    "SELECT id,json_extract(data,'$.parent_id'),json_extract(data,'$.commitment_id') "
    "FROM objects WHERE kind='agent' UNION ALL "
    "SELECT s.id,json_extract(r.data,'$.parent_session_id'),"
    "json_extract(r.data,'$.commitment_id') FROM objects s JOIN objects r "
    "ON r.kind='workflow_run' AND r.id=json_extract(s.data,'$.workflow_run_id') "
    "WHERE s.kind='session'), scoped(session_id) AS ("
    "SELECT session_id FROM links WHERE binding=? UNION "
    "SELECT l.session_id FROM links l JOIN scoped p ON l.parent_id=p.session_id) "
)


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class TaskInspection:
    def __init__(
        self,
        commitments: Commitments,
        control: TaskControl,
        memory: Memory,
        *,
        max_handoff_records: Callable[[], int] = lambda: _MAX_HANDOFF_RECORDS,
        current_wait: WaitProjection | None = None,
        owner_questions: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        if commitments.store is not control.store or commitments.store is not memory.store:
            raise ValueError("任务核查必须共用资料库")
        self.commitments, self.control, self.memory = commitments, control, memory
        self.store = commitments.store
        self.max_handoff_records = max_handoff_records
        self.current_wait = current_wait
        self.owner_questions = owner_questions
        # store.instance_id survives a normal reopen; live-wait cursors must not.
        self._wait_nonce = new_id()

    def handoff_limits(self) -> tuple[int, int]:
        maximum = self.max_handoff_records()
        if type(maximum) is not int or maximum < 1:
            raise EvidenceError("output_budget", "接办记录与父链预算不足")
        return min(maximum, _MAX_HANDOFF_RECORDS), self.commitments.max_output_chars() * 4

    def _current_wait(
        self,
        identifier: str,
        *,
        limit: int,
        cursor: str | None,
        revision: str | None,
    ) -> dict[str, Any]:
        maximum, byte_budget = self.handoff_limits()
        budget = self.commitments.max_output_chars()
        task = self.store.bounded_object("commitment", identifier, max_bytes=byte_budget)
        if task is None:
            raise EvidenceError("evidence_not_found", "任务不存在")
        if type(task.get("revision")) is not int or task["revision"] < 1:
            raise EvidenceError("content_unavailable", "任务版本无法核查")
        state = self.control._state(task)
        token = _digest(
            [
                self._wait_nonce,
                identifier,
                "current_wait",
                task,
                limit,
                maximum,
                byte_budget,
                self.commitments.max_context_chars(),
            ]
        )
        if revision is not None and revision != token:
            raise EvidenceError("evidence_changed", "当前等待的实例、任务或预算已变化，请重新核查")
        after = self._after(cursor, token, budget)
        after_key = ""
        if after:
            try:
                position = int(after)
                if str(position) != after or not 1 <= position <= SQLITE_MAX_INTEGER:
                    raise ValueError
            except ValueError as exc:
                raise EvidenceError("invalid_arguments", "当前等待续页位置无效") from exc
            anchor = self.store.db.execute(
                "SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB)) BETWEEN 1 AND ? "
                "THEN CAST(id AS BLOB) END FROM objects WHERE kind='agent' AND rowid=?",
                (byte_budget, position),
            ).fetchone()
            if anchor is None or not isinstance(anchor[0], bytes):
                raise EvidenceError(
                    "evidence_changed", "当前等待续页锚点已缺失或不可用，请重新核查"
                )
            try:
                after_key = anchor[0].decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise EvidenceError("content_unavailable", "续页锚点无法核查") from exc
        result: dict[str, Any] = {
            "id": identifier,
            "section": "current_wait",
            "revision": token,
            "task_revision": task["revision"],
            "epoch": state["epoch"],
            "consistency": "live",
            "authority": "coordination_data",
            "source_completeness": "candidate_window_only",
            "owner_questions": {
                "coverage": "explicit_only",
                "read_arguments": {
                    "identifier": identifier,
                    "section": "owner_questions",
                    "question_status": "open",
                },
            },
            "side_effect_status": "unknown",
            "items": [],
            "scanned": 0,
            "omitted": 0,
            "unavailable": 0,
            "has_more": False,
            "next_cursor": None,
        }
        self._fit(result, budget)
        # Seek the (kind,id) primary key before opening any JSON or parent chain.
        # Out-of-scope and broken candidates consume this finite window too.
        rows = self.store.db.execute(
            "SELECT rowid,CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB)) BETWEEN 1 AND ? "
            "THEN CAST(id AS BLOB) END FROM objects "
            "WHERE kind='agent' AND id>? ORDER BY id LIMIT ?",
            (byte_budget, after_key, min(limit, maximum) + 1),
        ).fetchall()
        for index, row in enumerate(rows[: min(limit, maximum)]):
            if not isinstance(row[1], bytes):
                raise EvidenceError("content_unavailable", "候选主键超过预算或无法核查")
            try:
                agent_id = row[1].decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise EvidenceError("content_unavailable", "候选主键不是有效 UTF-8") from exc
            page = {
                **result,
                "scanned": result["scanned"] + 1,
                "has_more": len(rows) > index + 1,
                # Only a raw row position goes into the cursor, never an out-of-scope ID.
                "next_cursor": self._cursor(token, str(row[0])) if len(rows) > index + 1 else None,
            }
            try:
                agent = self.store.bounded_object("agent", agent_id, max_bytes=byte_budget)
                if agent is None or agent.get("session_id") != agent_id:
                    raise EvidenceError("content_unavailable", "候选队友身份不可用")
                binding, lineage = self.control.scope_lineage(
                    agent_id, max_hops=maximum, max_bytes=byte_budget
                )
                if binding != identifier:
                    page["omitted"] += 1
                elif self.current_wait is None:
                    page["unavailable"] += 1
                    page["omitted"] += 1
                else:
                    item = self.current_wait(agent, task, lineage, maximum, byte_budget)
                    page["items"] = [*result["items"], item]
                    if any(
                        item[key].get("availability") == "unavailable"
                        for key in ("execution", "shell_continuation")
                    ):
                        page["unavailable"] += 1
            except (EvidenceError, ControlUnavailable, sqlite3.DatabaseError):
                # Without a verified lineage, never disclose even the candidate's ID.
                page["unavailable"] += 1
                page["omitted"] += 1
            if json_size(page) > budget:
                if not result["scanned"]:
                    raise EvidenceError("output_budget", "预算不足以返回当前等待首项及续页位置")
                break
            result = page
        return result

    def _handoff_execution(
        self, agent: dict[str, Any], identifier: str, byte_budget: int
    ) -> tuple[dict[str, Any], list[Any]]:
        execution: dict[str, Any] = {
            "availability": "unknown",
            "side_effect_status": "unknown",
            "run": None,
            "report": None,
        }
        run_id = agent.get("run_id")
        if run_id is None:
            return execution, []
        if not isinstance(run_id, str) or not run_id:
            raise EvidenceError("content_unavailable", "队友执行关联无效")
        # send() queues a new turn before _run() replaces its previous run ID.
        if agent.get("status") == "queued":
            execution["availability"] = "historical"
            return execution, []
        run = self.store.bounded_object("run", run_id, max_bytes=byte_budget)
        report = self.store.bounded_object("team_report", run_id, max_bytes=byte_budget)
        if run is not None and run.get("session_id") != agent["session_id"]:
            raise EvidenceError("evidence_not_found", "执行不属于指定队友")
        if report is not None and (
            report.get("agent_id") != agent["id"]
            or report.get("parent_id") != agent.get("parent_id")
            or report.get("commitment_id") not in (None, identifier)
        ):
            raise EvidenceError("evidence_not_found", "报告不属于指定队友及任务")
        execution["availability"] = "available" if run is not None else "missing"
        if run is not None:
            execution["run"] = {
                key: run[key] for key in ("id", "status", "session_id") if key in run
            }
        execution["report_availability"] = "available" if report is not None else "missing"
        if report is not None:
            execution["report"] = {
                key: report[key] for key in ("id", "status", "execution_status") if key in report
            }
            execution["report_read"] = {
                "identifier": identifier,
                "section": "reports",
                "record_id": run_id,
                "record_revision": _digest(report),
            }
        return execution, [run, report]

    def _handoff_messages(
        self, session_id: str, maximum: int, byte_budget: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[Any]]:
        mappings = self.store.message_evidence_window(
            session_id, limit=maximum + 1, metadata_bytes=byte_budget
        )
        selected = mappings[:maximum]
        window = {
            "limit": maximum,
            "through_seq": selected[0]["seq"] if selected else 0,
            "oldest_seq": selected[-1]["seq"] if selected else 0,
            "has_older": len(mappings) > maximum,
        }
        items: list[dict[str, Any]] = []
        versions: list[Any] = []
        history = HistoryEvidence(self.store)
        for mapping in selected:
            item = {"id": mapping["message_id"], "seq": mapping["seq"], "availability": "missing"}
            if mapping["message_id"] is None:
                items.append({**item, "availability": "unavailable"})
                versions.append(mapping)
                continue
            try:
                row = self.store.message_evidence_slice(
                    session_id,
                    message_id=None,
                    seq=mapping["seq"],
                    offset_bytes=0,
                    length_bytes=0,
                    metadata_bytes=byte_budget,
                )
                versions.append(
                    [
                        mapping,
                        {
                            key: hashlib.sha256(value).hexdigest()
                            if isinstance(value, bytes)
                            else value
                            for key, value in row.items()
                            if key not in {"content", "content_bytes", "content_type"}
                        },
                    ]
                    if row is not None
                    else [mapping, None]
                )
                if row is not None:
                    metadata = history._metadata(row, byte_budget // 4)
                    item = {
                        **metadata,
                        "availability": "available",
                        "read": {
                            "session_id": session_id,
                            "seq": row["seq"],
                            "revision": metadata["revision"],
                        },
                    }
            except (EvidenceError, sqlite3.DatabaseError, UnicodeError) as exc:
                # Do not backfill from older history when a raw mapping is broken.
                item = {
                    **item,
                    "availability": "unavailable",
                    "reason": (
                        exc.code if isinstance(exc, EvidenceError) else "content_unavailable"
                    ),
                }
                if len(versions) < len(items) + 1:
                    versions.append([mapping, "unavailable"])
            items.append(item)
        return items, window, versions

    def _handoff(
        self,
        identifier: str,
        agent_id: str,
        *,
        limit: int,
        cursor: str | None,
        revision: str | None,
    ) -> dict[str, Any]:
        maximum, byte_budget = self.handoff_limits()
        budget = self.commitments.max_output_chars()
        task = self.store.bounded_object("commitment", identifier, max_bytes=byte_budget)
        agent = self.store.bounded_object("agent", agent_id, max_bytes=byte_budget)
        if task is None or agent is None:
            raise EvidenceError("evidence_not_found", "任务或指定队友不存在")
        if type(task.get("revision")) is not int or task["revision"] < 1:
            raise EvidenceError("content_unavailable", "任务版本无效")
        if agent.get("session_id") != agent_id:
            raise EvidenceError("content_unavailable", "队友会话关联无效")
        binding, lineage = self.control.scope_lineage(
            agent_id, max_hops=maximum, max_bytes=byte_budget
        )
        if binding != identifier:
            raise EvidenceError("evidence_not_found", "队友不属于指定任务")
        state = self.control._state(task)
        execution, execution_records = self._handoff_execution(agent, identifier, byte_budget)
        items, window, versions = self._handoff_messages(agent_id, min(limit, maximum), byte_budget)
        memory_revision = self.memory.revision
        token = _digest(
            [
                self.store.instance_id,
                identifier,
                agent_id,
                limit,
                maximum,
                byte_budget,
                task,
                agent,
                lineage,
                execution_records,
                window,
                versions,
                memory_revision,
            ]
        )
        if revision is not None and revision != token:
            raise EvidenceError("evidence_changed", "接办资料已变化，请重新取得首页")
        after = self._after(cursor, token, budget)
        try:
            start = int(after) if after else 0
            if after and (str(start) != after or not 0 < start < len(items)):
                raise ValueError
        except ValueError as exc:
            raise EvidenceError("invalid_arguments", "接办游标无效") from exc
        result: dict[str, Any] = {
            "id": identifier,
            "section": "handoff",
            "revision": token,
            "task_revision": task["revision"],
            "epoch": state["epoch"],
            "approved_epoch": state["approved_epoch"],
            "memory_revision": memory_revision,
            "consistency": "live",
            "source_completeness": "recent_window_only",
            "authority": "coordination_data",
            "candidate_usage": "unknown",
            "task_read": {
                "identifier": identifier,
                "section": "task",
                "record_revision": _digest(task),
            },
            "agent": {
                key: agent[key]
                for key in (
                    "id",
                    "session_id",
                    "parent_id",
                    "status",
                    "generation",
                    "completion_revision",
                    "run_id",
                )
                if key in agent
            },
            "execution": execution,
            "window": window,
            "items": [],
            "has_more": False,
            "next_cursor": None,
        }
        self._fit(result, budget)
        for index in range(start, len(items)):
            more = index + 1 < len(items)
            page = {
                **result,
                "items": [*result["items"], items[index]],
                "has_more": more,
                "next_cursor": self._cursor(token, str(index + 1)) if more else None,
            }
            if json_size(page) > budget:
                if not result["items"]:
                    raise EvidenceError("output_budget", "预算不足以返回接办首项及续读位置")
                break
            result = page
        return result

    def _scope_check(self, section: str, record: dict[str, Any], identifier: str) -> None:
        if section == "agents":
            actual = self.control.resolve_scope(record["id"])
        elif section == "workflows":
            actual = self.control._workflow_scope(record["id"])
        else:
            actual = record.get("commitment_id")
            agent = self.store.db.execute(
                "SELECT id FROM objects WHERE kind='agent' AND id=?",
                (record.get("agent_id", ""),),
            ).fetchone()
            if agent is not None:
                inherited = self.control.resolve_scope(agent["id"])
                if actual is not None and inherited != actual:
                    raise EvidenceError("evidence_changed", "报告任务关联已变化，请重新核查")
                actual = inherited
        if actual != identifier:
            raise EvidenceError("evidence_not_found", "记录不属于当前任务或关联缺失")

    def _delivery_review(
        self, identifier: str, task: dict[str, Any], record: dict[str, Any]
    ) -> dict[str, Any]:
        """Compare stored declarations; old judgments never assess new requirements."""
        try:
            if (
                task.get("id") != identifier
                or type(task.get("revision")) is not int
                or task["revision"] < 1
                or record["host_facts"].get("verification") != "at_record_time"
            ):
                raise ValueError
            before = self.commitments._validate_brief(
                record["brief"], stored=True, resolve_references=False
            )
            after = self.commitments._validate_brief(
                task.get("brief"), stored=True, resolve_references=False
            )
        except (KeyError, ValueError, TypeError, UnicodeError) as exc:
            raise EvidenceError("content_unavailable", "两版完整说明或登记时事实无法核查") from exc
        return {
            "version": 1,
            "commitment_id": identifier,
            "delivery_id": record["id"],
            "recorded": {
                "task_revision": record["task_revision"],
                "epoch": record["epoch"],
                "brief": before,
            },
            "current": {
                "task_revision": task["revision"],
                "epoch": self.control._state(task)["epoch"],
                "brief": after,
            },
            "changes": {
                field: before[field] != after[field]
                for field in ("goal", "constraints", "evidence_refs")
            },
            "host_facts": record["host_facts"],
            "historical_model_judgments": record["model_judgments"],
            "judgment_scope": "recorded_requirements_only",
        }

    def _deliveries(
        self,
        identifier: str,
        *,
        limit: int,
        cursor: str | None,
        revision: str | None,
        record_id: str | None,
        record_revision: str | None,
        offset_bytes: int,
        length_bytes: int | None,
        review: bool = False,
    ) -> dict[str, Any]:
        if review and record_id is None:
            raise EvidenceError("invalid_arguments", "变更复核需要准确验收单record_id")
        budget = self.commitments.max_output_chars()
        task = self.store.bounded_object("commitment", identifier, max_bytes=budget * 4)
        if task is None:
            raise EvidenceError("evidence_not_found", "任务不存在")
        state = self.control._state(task)
        section = "delivery_review" if review else "deliveries"
        token = _digest([self.store.instance_id, task, section])
        if revision is not None and revision != token:
            raise EvidenceError("evidence_changed", "任务已变化，请从验收目录首页重新读取")
        meta = {
            "id": identifier,
            "section": section,
            "revision": token,
            "task_revision": task["revision"],
            "epoch": state["epoch"],
            "consistency": "live",
            "authority": "coordination_data",
            "file_verification": "at_record_time",
        }
        if record_id is not None:
            if cursor is not None:
                raise EvidenceError("invalid_arguments", "验收目录游标与正文参数不能混用")
            record = delivery_record(self.store, record_id, identifier, budget)
            if review and record.get("id") != record_id:
                raise EvidenceError("content_unavailable", "验收单持久身份不一致")
            body = self._delivery_review(identifier, task, record) if review else record
            digest = _digest(body)
            if record_revision is not None and record_revision != digest:
                raise EvidenceError("evidence_changed", "验收单记录已变化")
            raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
            return content_page(
                {
                    **meta,
                    "record_id": record_id,
                    "record_revision": digest,
                    "current": delivery_current(self.store, self.control, record, task, budget),
                    "source_completeness": "stored_record_only",
                },
                raw[offset_bytes : offset_bytes + min(length_bytes or budget * 4, budget * 4)],
                offset_bytes=offset_bytes,
                total_bytes=len(raw),
                max_output_chars=budget,
            )
        if offset_bytes or length_bytes is not None or record_revision is not None:
            raise EvidenceError("invalid_arguments", "验收正文分页需要指定 record_id")
        after = self._after(cursor, token, budget)
        prefix = delivery_prefix(identifier)
        lower = max(prefix, after)
        comparison = ">" if after >= prefix else ">="
        rows = self.store.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB))<=? "
            "THEN CAST(id AS BLOB) END FROM objects WHERE kind='task_delivery' "
            f"AND id{comparison}? AND id<? ORDER BY id LIMIT ?",
            (budget * 4, lower, prefix[:-1] + ";", limit + 1),
        ).fetchall()
        result: dict[str, Any] = {**meta, "items": [], "has_more": False, "next_cursor": None}
        self._fit(result, budget)
        for index, row in enumerate(rows[:limit]):
            raw_id = row[0]
            if not isinstance(raw_id, bytes):
                raise EvidenceError("content_unavailable", "验收目录主键无法完整核查")
            try:
                delivery_id = raw_id.decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise EvidenceError("content_unavailable", "验收目录主键不是有效 UTF-8") from exc
            record = delivery_record(self.store, delivery_id, identifier, budget)
            item = {
                key: record[key]
                for key in ("id", "task_revision", "epoch", "agent_id", "run_id", "created_at")
            }
            item["current"] = delivery_current(self.store, self.control, record, task, budget)
            page = {
                **result,
                "items": [*result["items"], item],
                "has_more": len(rows) > index + 1,
                "next_cursor": self._cursor(token, record["id"]) if len(rows) > index + 1 else None,
            }
            if json_size(page) > budget:
                if not result["items"]:
                    raise EvidenceError("output_budget", "预算不足以返回验收目录首项与续读位置")
                break
            result = page
        return result

    def _rows(self, identifier: str, section: str, after: str, limit: int) -> list[dict[str, Any]]:
        kind = _KINDS[section]
        if section == "agents":
            condition = "o.id IN (SELECT session_id FROM scoped)"
        elif section == "workflows":
            condition = (
                "(json_extract(o.data,'$.commitment_id')=? OR "
                "json_extract(o.data,'$.parent_session_id') IN (SELECT session_id FROM scoped))"
            )
        else:
            condition = (
                "(json_extract(o.data,'$.commitment_id')=? OR "
                "json_extract(o.data,'$.agent_id') IN (SELECT session_id FROM scoped))"
            )
        fields = (*_FIELDS[section], "commitment_id", "parent_session_id", "parent_id")
        projection = (
            "json_object("
            + ",".join(f"'{key}',json_extract(o.data,'$.{key}')" for key in fields)
            + ")"
        )
        args: list[Any] = [identifier, kind]
        if section != "agents":
            args.append(identifier)
        args.extend([after, limit])
        rows = self.store.db.execute(
            _SCOPED + f"SELECT {projection} FROM objects o WHERE o.kind=? AND {condition} "
            "AND o.id>? ORDER BY o.id LIMIT ?",
            args,
        )
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def _cursor(token: str, after: str) -> str:
        return base64.urlsafe_b64encode(json.dumps([1, token, after]).encode()).decode("ascii")

    @staticmethod
    def _after(cursor: str | None, token: str, budget: int) -> str:
        if cursor is None:
            return ""
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= budget:
                raise ValueError
            payload = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (
                not isinstance(payload, list)
                or len(payload) != 3
                or type(payload[0]) is not int
                or payload[0] != 1
                or not isinstance(payload[2], str)
                or not payload[2]
            ):
                raise ValueError
            if payload[1] != token:
                raise EvidenceError("evidence_changed", "任务、查询或记忆已变化，请从首页重新核查")
            return payload[2]
        except (ValueError, TypeError, RecursionError, binascii.Error) as exc:
            if isinstance(exc, EvidenceError):
                raise
            raise EvidenceError("invalid_arguments", "核查游标无效") from exc

    def read(self, identifier: str, **args: Any) -> dict[str, Any]:
        """Nest a read snapshot without committing or rolling back the caller's writes."""
        savepoint = "task_inspection_" + new_id()
        self.store.db.execute(f"SAVEPOINT {savepoint}")
        try:
            return self._read(identifier, **args)
        except ControlUnavailable as exc:
            if exc.code == "scope_budget":
                raise EvidenceError("output_budget", "接办父链超过配置预算，无法完整核查") from exc
            raise EvidenceError("evidence_changed", "任务关联或控制状态不可用，请重新核查") from exc
        finally:
            self.store.db.execute(f"ROLLBACK TO {savepoint}")
            self.store.db.execute(f"RELEASE {savepoint}")

    def _read(
        self,
        identifier: str,
        *,
        section: str = "overview",
        cursor: str | None = None,
        limit: int = 20,
        query: str | None = None,
        revision: str | None = None,
        record_id: str | None = None,
        record_revision: str | None = None,
        offset_bytes: int = 0,
        length_bytes: int | None = None,
        agent_id: str | None = None,
        question_status: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(identifier, str) or not identifier or section not in SECTIONS:
            raise EvidenceError("invalid_arguments", "任务 ID 或核查 section 无效")
        validate_integer(limit, "limit", minimum=1)
        if limit > 100:
            raise EvidenceError("invalid_arguments", "核查每页最多 100 项")
        validate_integer(offset_bytes, "offset_bytes")
        if section == "owner_questions":
            if agent_id is not None or query is not None:
                raise EvidenceError("invalid_arguments", "持久问题不接受队友或记忆查询参数")
            if self.owner_questions is None:
                raise EvidenceError("owner_question_unavailable", "持久问题读取尚未接入")
            return self.owner_questions(
                identifier,
                question_status=question_status,
                cursor=cursor,
                revision=revision,
                record_id=record_id,
                record_revision=record_revision,
                offset_bytes=offset_bytes,
                length_bytes=length_bytes,
                limit=limit,
            )
        if question_status is not None:
            raise EvidenceError("invalid_arguments", "仅持久问题段接受 question_status")
        if section == "current_wait":
            if (
                agent_id is not None
                or query is not None
                or record_id is not None
                or record_revision is not None
                or offset_bytes
                or length_bytes is not None
            ):
                raise EvidenceError("invalid_arguments", "当前等待仅接受任务及目录参数")
            return self._current_wait(identifier, limit=limit, cursor=cursor, revision=revision)
        if section == "handoff":
            if (
                not isinstance(agent_id, str)
                or not agent_id
                or query is not None
                or record_id is not None
                or record_revision is not None
                or offset_bytes != 0
                or length_bytes is not None
            ):
                raise EvidenceError("invalid_arguments", "接办需要指定 agent_id，仅接受目录参数")
            return self._handoff(
                identifier, agent_id, limit=limit, cursor=cursor, revision=revision
            )
        if agent_id is not None:
            raise EvidenceError("invalid_arguments", "仅 handoff 接受 agent_id")
        if length_bytes is not None:
            validate_integer(length_bytes, "length_bytes", minimum=1)
        if record_id is not None and (not isinstance(record_id, str) or not record_id):
            raise EvidenceError("invalid_arguments", "record_id 必须是非空字符串")
        if query is not None and (
            section != "memories" or not isinstance(query, str) or not query.strip()
        ):
            raise EvidenceError("invalid_arguments", "记忆段需要显式非空 query，其它段不接受 query")
        if section == "memories" and query is None:
            raise EvidenceError("invalid_arguments", "记忆查询不能使用最近记忆代替显式 query")
        body = section == "task" or record_id is not None
        if (body and cursor is not None) or (
            not body and (offset_bytes or length_bytes is not None or record_revision is not None)
        ):
            raise EvidenceError("invalid_arguments", "目录游标与正文分页不能混用")
        if record_id is not None and section not in {
            *_KINDS,
            "memories",
            "deliveries",
            "delivery_review",
        }:
            raise EvidenceError("invalid_arguments", "此 section 不接受 record_id")
        if offset_bytes and (revision is None or record_revision is None):
            raise EvidenceError(
                "invalid_arguments", "正文续读必须同时提供 revision 和 record_revision"
            )
        if section in {"deliveries", "delivery_review"}:
            return self._deliveries(
                identifier,
                limit=limit,
                cursor=cursor,
                revision=revision,
                record_id=record_id,
                record_revision=record_revision,
                offset_bytes=offset_bytes,
                length_bytes=length_bytes,
                review=section == "delivery_review",
            )
        task = self.commitments.get(identifier)
        state = self.control._state(task)
        memory_revision = self.memory.revision if section == "memories" else None
        token = _digest([self.store.instance_id, task, section, query, memory_revision])
        if revision is not None and revision != token:
            raise EvidenceError("evidence_changed", "任务或记忆已变化，请从首页重新核查")
        budget = self.commitments.max_output_chars()
        meta: dict[str, Any] = {
            "id": identifier,
            "section": section,
            "revision": token,
            "task_revision": task["revision"],
            "epoch": state["epoch"],
            "consistency": "live",
            "authority": "coordination_data",
        }
        if memory_revision is not None:
            meta.update(memory_revision=memory_revision, candidate_usage="unknown", query=query)
        after = self._after(cursor, token, budget)
        if section == "overview":
            result = {**meta, "task": self.commitments.metadata(task), "sections": list(SECTIONS)}
            return self._fit(result, budget)
        candidates = (
            self.memory.search(query or "", self.memory.max_candidates)
            if section == "memories"
            else []
        )
        if body:
            record: dict[str, Any] | None
            if section == "task":
                record = task
            elif section == "memories":
                record = next((item for item in candidates if item["id"] == record_id), None)
                if record is None:
                    raise EvidenceError(
                        "evidence_not_found", "记忆不存在、已忘记或不在当前候选集中"
                    )
                # Search scores depend on the observation time; source content does not.
                record = self.memory.get(record["id"])
                assert record is not None
            else:
                record = self.store.get(_KINDS[section], str(record_id))
                if record is None:
                    raise EvidenceError("evidence_not_found", "记录不存在或已删除")
                self._scope_check(section, record, identifier)
            digest = _digest(record)
            if record_revision is not None and record_revision != digest:
                raise EvidenceError("evidence_changed", "记录已变化，请重新读取正文")
            raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
            return content_page(
                {
                    **meta,
                    "record_id": record["id"],
                    "record_revision": digest,
                    "source_completeness": "stored_record_only",
                },
                raw[offset_bytes : offset_bytes + min(length_bytes or budget * 4, budget * 4)],
                offset_bytes=offset_bytes,
                total_bytes=len(raw),
                max_output_chars=budget,
            )
        if section == "evidence":
            refs = task.get("brief", {}).get("evidence_refs", [])
            records = []
            for index, ref in enumerate(refs):
                key = f"{index:020d}"
                if key <= after:
                    continue
                found = self.store.db.execute(
                    "SELECT 1 FROM objects WHERE kind=? AND id=?", (ref["kind"], ref["id"])
                ).fetchone()
                records.append(
                    {"key": key, **ref, "availability": "available" if found else "missing"}
                )
                if len(records) > limit:
                    break
        elif section == "memories":
            records = [
                {key: item[key] for key in ("id", "source_id", "layer", "status", "matched_by")}
                for item in sorted(candidates, key=lambda item: item["id"])
                if item["id"] > after
            ][: limit + 1]
            meta["candidate_limit"] = self.memory.max_candidates
        else:
            records = self._rows(identifier, section, after, limit + 1)
        result = {**meta, "items": [], "has_more": False, "next_cursor": None}
        self._fit(result, budget)
        for index, record in enumerate(records[:limit]):
            if section in _KINDS:
                self._scope_check(section, record, identifier)
                record = {key: record[key] for key in _FIELDS[section] if record[key] is not None}
            after_key = record["key"] if "key" in record else record["id"]
            page = {
                **result,
                "items": [*result["items"], record],
                "has_more": len(records) > index + 1,
                "next_cursor": self._cursor(token, after_key) if len(records) > index + 1 else None,
            }
            if json_size(page) > budget:
                if not result["items"]:
                    raise EvidenceError("output_budget", "预算不足以完整返回首项及续页位置")
                break
            result = page
        return result

    @staticmethod
    def _fit(result: dict[str, Any], budget: int) -> dict[str, Any]:
        if json_size(result) > budget:
            raise EvidenceError("output_budget", "核查元数据超过输出预算")
        return result

    def tool(
        self,
        read_scope: Callable[[], str | None],
        *,
        handoff_read_scope: Callable[[], str | None] | None = None,
    ) -> Tool:
        async def inspect(args: dict[str, Any]) -> Any:
            allowed = {
                "identifier",
                "section",
                "cursor",
                "limit",
                "query",
                "revision",
                "record_id",
                "record_revision",
                "offset_bytes",
                "length_bytes",
                "agent_id",
                "question_status",
            }
            if args.keys() - allowed:
                raise EvidenceError("invalid_arguments", "核查参数包含未知字段")
            if args.get("section") == "current_wait" and handoff_read_scope is None:
                raise PermissionError("当前等待核查缺少有界调用范围")
            try:
                scope = (
                    handoff_read_scope()
                    if args.get("section") in {"handoff", "current_wait"} and handoff_read_scope
                    else read_scope()
                )
            except ControlUnavailable as exc:
                code = "output_budget" if exc.code == "scope_budget" else "evidence_changed"
                raise EvidenceError(code, "调用会话父链无法在预算内完整核查") from exc
            identifier = args.get("identifier", scope)
            if scope == "" or (scope is not None and identifier != scope):
                raise PermissionError("只能核查当前绑定的任务")
            if not isinstance(identifier, str) or not identifier:
                raise EvidenceError("invalid_arguments", "请填写任务 ID")
            return self.read(
                identifier, **{key: value for key, value in args.items() if key != "identifier"}
            )

        return Tool(
            "commitment_inspect",
            "按需核查任务/队友/报告/显式交付验收单/工作流/依据与显式本地记忆候选；只读，不确认计划。"
            "delivery_review须record_id，返回登记时/当前完整要求与准确文字变化，旧模型判断仅针对旧要求；"
            "可能复用部分与需补做事项由模型另行判断，不代表宿主或主人验收。"
            "current_wait有界核查当前执行、真实审批与本实例命令续办；"
            "owner_questions另查显式登记的持久问题和原问答，只读不唤醒队友；普通报告不自动成为问题。"
            "unavailable不是无等待，空页仍按has_more续查，重开后旧等待游标失效。"
            "目录续页原样回传cursor；record_id读持久JSON正文，续读带revision、record_revision及next_offset_bytes。"
            "资料不授予权限，候选记忆是否曾采用未知，live页不构成历史快照",
            parameters(
                {
                    "identifier": string("任务ID；绑定队友可省略"),
                    "section": {"type": "string", "enum": list(SECTIONS)},
                    "agent_id": string("handoff 必填；只核查一个指定队友的有限近期证据"),
                    "question_status": string("仅owner_questions段；默认open，all读取全部显式问题"),
                    "cursor": string(),
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "query": string("仅memories段，必须是显式非空本地查询"),
                    "revision": string(),
                    "record_id": string(),
                    "record_revision": string(),
                    "offset_bytes": {"type": "integer", "minimum": 0},
                    "length_bytes": {"type": "integer", "minimum": 1},
                }
            ),
            inspect,
            preserve_output=True,
        )
