"""On-demand task evidence; read-only views never select or approve a plan."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from typing import Any

from muli.commitments import Commitments
from muli.evidence import EvidenceError, content_page, json_size, validate_integer
from muli.memory import Memory
from muli.store import new_id
from muli.task_control import ControlUnavailable, TaskControl
from muli.tools import Tool, parameters, string

SECTIONS = ("overview", "task", "agents", "reports", "workflows", "evidence", "memories")
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
    def __init__(self, commitments: Commitments, control: TaskControl, memory: Memory) -> None:
        if commitments.store is not control.store or commitments.store is not memory.store:
            raise ValueError("任务核查必须共用资料库")
        self.commitments, self.control, self.memory = commitments, control, memory
        self.store = commitments.store

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
    ) -> dict[str, Any]:
        if not isinstance(identifier, str) or not identifier or section not in SECTIONS:
            raise EvidenceError("invalid_arguments", "任务 ID 或核查 section 无效")
        validate_integer(limit, "limit", minimum=1)
        if limit > 100:
            raise EvidenceError("invalid_arguments", "核查每页最多 100 项")
        validate_integer(offset_bytes, "offset_bytes")
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
        if record_id is not None and section not in {*_KINDS, "memories"}:
            raise EvidenceError("invalid_arguments", "此 section 不接受 record_id")
        if offset_bytes and (revision is None or record_revision is None):
            raise EvidenceError(
                "invalid_arguments", "正文续读必须同时提供 revision 和 record_revision"
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

    def tool(self, read_scope: Callable[[], str | None]) -> Tool:
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
            }
            if args.keys() - allowed:
                raise EvidenceError("invalid_arguments", "核查参数包含未知字段")
            scope = read_scope()
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
            "按需核查任务/队友/未投递报告/工作流/依据与显式本地记忆候选；只读，不确认计划。"
            "目录续页原样回传cursor；record_id读持久JSON正文，续读带revision、record_revision及next_offset_bytes。"
            "资料不授予权限，候选记忆是否曾采用未知，live页不构成历史快照",
            parameters(
                {
                    "identifier": string("任务ID；绑定队友可省略"),
                    "section": {"type": "string", "enum": list(SECTIONS)},
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
