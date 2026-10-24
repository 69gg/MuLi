"""Explicit, revocable scheduled DAGs on the existing reminder clock."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import asdict, fields, replace
from typing import TYPE_CHECKING, Any

from muli.channels.types import NAMES
from muli.evidence import EvidenceError
from muli.proactive import FrozenRouting
from muli.reminder_followup import task_status
from muli.reply_origin import ReplyOrigin, authorize_route, channel_account
from muli.store import new_id, now
from muli.task_control import TaskSnapshot
from muli.workflows import ScheduledOccurrence, WorkflowEngine

if TYPE_CHECKING:
    from muli.runtime import PreparedRequest, Runtime

JsonObject = dict[str, Any]
_MODES = ("ask", "auto", "yolo")
_ACTIVE = {"pending", "queued", "running"}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False).encode("ascii")
    ).hexdigest()


class ScheduledWorkflows:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.store = runtime.store

    @property
    def _budget(self) -> int:
        limits = self.runtime.config.settings.limits
        return (
            limits.max_context_chars * 2 + limits.max_output_chars * limits.max_workflow_steps
        ) * 4

    def _read(self, kind: str, identifier: str) -> JsonObject:
        item = self.store.bounded_object(kind, identifier, max_bytes=self._budget)
        if item is None:
            raise ValueError("定时委托的持久证明不存在")
        return item

    def _output(self, value: JsonObject) -> JsonObject:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > (
            self.runtime.config.settings.limits.max_output_chars
        ):
            raise EvidenceError("output_budget", "定时委托结果超过输出预算，请缩小列表")
        return value

    def _caller(self) -> tuple[str, str, PreparedRequest, str]:
        runtime = self.runtime
        caller = runtime.tool_session_id
        owner = runtime._tool_execution.get()
        request = runtime.request_context(caller) if caller is not None else None
        if (
            runtime._closing
            or owner is None
            or caller is None
            or caller != self.store.existing_main_session_id()
            or caller != owner[0]
            or runtime.run_ids.get(caller) != owner[1]
            or request is None
            or runtime.tasks.get(caller) is not asyncio.current_task()
            or caller in runtime._stopping
        ):
            raise PermissionError("定时委托需要当前主助手的真实工具执行")
        runtime.maintenance.admission.require()
        session = self._read("session", caller)
        run = self._read("run", owner[1])
        if (
            session.get("kind") != "main"
            or session.get("principal") != "owner"
            or run.get("session_id") != caller
            or run.get("status") != "running"
            or run.get("mode") not in _MODES
        ):
            raise PermissionError("定时委托的主助手执行已失效")
        trigger = self.store.db.execute(
            "SELECT o.id,json_extract(o.data,'$.principal') FROM message_order m "
            "JOIN objects o ON o.kind='message' AND o.id=m.message_id "
            "WHERE m.session_id=? AND json_extract(o.data,'$.role')='user' ORDER BY m.seq DESC LIMIT 1",
            (caller,),
        ).fetchone()
        if trigger is None or trigger[0] != request.reply_origin.input_id or trigger[1] != "owner":
            raise PermissionError("只有当前主人输入可以创建或取消定时委托")
        runtime._guard_request(request)
        if self.store.pending_inputs(caller):
            raise PermissionError("收到新输入，请先重新确认委托")
        return caller, owner[1], request, str(run["mode"])

    @staticmethod
    def _snapshot(item: JsonObject) -> TaskSnapshot | None:
        raw = item.get("snapshot")
        if raw is None:
            return None
        if (
            not isinstance(raw, dict)
            or set(raw) != {field.name for field in fields(TaskSnapshot)}
            or any(
                type(raw[key]) is not int or raw[key] < 0
                for key in ("revision", "epoch", "approved_epoch")
            )
            or any(
                not isinstance(raw[key], str) or not raw[key]
                for key in ("session_id", "commitment_id", "brief_json", "plan_json")
            )
            or (
                raw["replan_request_id"] is not None
                and not isinstance(raw["replan_request_id"], str)
            )
        ):
            raise PermissionError("定时委托的冻结任务范围无效")
        return TaskSnapshot(**raw)

    def _authorize(
        self,
        item: JsonObject,
        version: int,
        mode: str | None = None,
        *,
        check_destination: bool = True,
        definition: JsonObject | None = None,
    ) -> str:
        runtime = self.runtime
        if (
            runtime._closing
            or type(item.get("version")) is not int
            or item["version"] != version
            or item.get("active") is not True
            or "workflow_schedule" in runtime.config.settings.disabled_tools
            or item.get("creator_session_id") != self.store.existing_main_session_id()
        ):
            raise PermissionError("定时委托授权已撤销或失效")
        runtime.maintenance.admission.require()
        session = self._read("session", item["creator_session_id"])
        frozen_mode, current_mode = item.get("mode"), session.get("mode")
        if frozen_mode not in _MODES or current_mode not in _MODES:
            raise PermissionError("定时委托的权限模式无效")
        permitted = _MODES[min(_MODES.index(frozen_mode), _MODES.index(current_mode))]
        if mode is not None and (
            mode not in _MODES or _MODES.index(mode) > _MODES.index(permitted)
        ):
            raise PermissionError("定时委托不能扩大当前权限")
        snapshot = self._snapshot(item)
        if runtime.task_control is not None:
            runtime.task_control.guard_execution(
                snapshot, session_id=item["creator_session_id"], risk="write"
            )
        elif snapshot is not None:
            raise PermissionError("定时委托的任务控制服务不可用")
        if snapshot is not None and task_status(
            self.store, snapshot.commitment_id, runtime.config.settings.limits.max_output_chars
        ) in {"done", "cancelled"}:
            raise PermissionError("定时委托关联的任务已经结束")
        workflow = (
            definition if definition is not None else self._read("workflow", item["workflow_id"])
        )
        if _digest(workflow) != item.get("workflow_digest"):
            raise PermissionError("定时委托的工作流定义已改变")
        origin = ReplyOrigin.load(item.get("reply_origin"))
        proof = self.store.db.execute(
            "SELECT CASE WHEN length(CAST(metadata AS BLOB))<=? THEN CAST(metadata AS BLOB) END "
            "FROM (SELECT json_object('id',id,'source',json_extract(data,'$.source'),"
            "'principal',json_extract(data,'$.principal'),'route',json_extract(data,'$.route'),"
            "'reply_origin',json_extract(data,'$.reply_origin')) AS metadata "
            "FROM objects WHERE kind='message' AND id=? "
            "AND json_extract(data,'$.principal')='owner' AND json_extract(data,'$.session_id')=?)",
            (self._budget, origin.input_id, item["creator_session_id"]),
        ).fetchone()
        if (
            proof is None
            or not isinstance(proof[0], bytes)
            or ReplyOrigin.from_input(json.loads(proof[0].decode("utf-8", errors="strict")))
            != origin
        ):
            raise PermissionError("定时委托的原始主人来源已改变")
        if origin.kind == "channel":
            route = origin.delivery_routes()[0]
            if route.get("kind", "private") != "private":
                raise PermissionError("定时委托仅支持原私聊或 Web")
            if check_destination:
                if route["name"] not in NAMES:
                    raise PermissionError("定时委托的原渠道无效")
                profile = getattr(runtime.config.settings.channels, route["name"])
                if not profile.enabled:
                    raise PermissionError("定时委托的原私聊未启用")
                authorize_route(
                    route["name"],
                    route,
                    account_id=channel_account(route["name"], profile),
                    owner_ids=profile.owner_ids,
                    allowed_groups=profile.allowed_groups,
                    outbound=True,
                )
        elif origin.kind != "web":
            raise PermissionError("定时委托缺少原始回复来源")
        return permitted

    def create(
        self, name: str, prompt: str, due_at: float, interval_seconds: int | None = None
    ) -> JsonObject:
        caller, run_id, request, mode = self._caller()
        try:
            finite = type(due_at) in {int, float} and math.isfinite(due_at)
        except OverflowError:
            finite = False
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 120
            or not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt) > self.runtime.config.settings.limits.max_context_chars
            or not finite
            or due_at <= now()
            or (
                interval_seconds is not None
                and (type(interval_seconds) is not int or not 60 <= interval_seconds <= 31_536_000)
            )
        ):
            raise ValueError("委托名称、任务、未来时间或重复间隔无效")
        origin = request.reply_origin
        if origin.kind not in {"channel", "web"} or not origin.input_id:
            raise PermissionError("定时委托需要原始主人请求")
        proof = self.store.db.execute(
            "SELECT 1 FROM objects WHERE kind='message' AND id=? "
            "AND json_extract(data,'$.session_id')=? "
            "AND json_extract(data,'$.principal')='owner'",
            (origin.input_id, caller),
        ).fetchone()
        if proof is None:
            raise PermissionError("系统通知或队友资料不能授予定时委托")
        count = self.store.db.execute(
            "SELECT count(*) FROM objects WHERE kind='workflow_schedule' "
            "AND json_extract(data,'$.active')=1 AND json_extract(data,'$.next_due_at') IS NOT NULL"
        ).fetchone()[0]
        if count >= self.runtime.config.settings.limits.max_pending_runs:
            raise ValueError("未来定时委托已达到待办上限")
        workflow = self.runtime.workflows._definition(
            name.strip(), WorkflowEngine.review_template(prompt), mode=mode
        )
        item: JsonObject = {
            "id": new_id(),
            "name": name.strip(),
            "prompt": prompt,
            "workflow_id": workflow["id"],
            "workflow_digest": _digest(workflow),
            "version": 1,
            "active": True,
            "mode": mode,
            "creator_session_id": caller,
            "creator_run_id": run_id,
            "snapshot": asdict(request.execution_scope) if request.execution_scope else None,
            "reply_origin": origin.dump(),
            "next_due_at": due_at,
            "interval_seconds": interval_seconds,
            "fired_count": 0,
            "last_occurrence_id": None,
            "created_at": now(),
            "updated_at": now(),
        }
        if len(json.dumps(item, ensure_ascii=False).encode("utf-8")) > self._budget:
            raise EvidenceError("output_budget", "完整委托与授权超过保存预算")
        self._authorize(item, 1, definition=workflow)
        result = self._output({**self._summary(item), "due_at": due_at})
        self.store.event(
            "workflow_schedule.updated",
            result,
            objects=[("workflow", workflow), ("workflow_schedule", item)],
        )
        self.runtime.reminders.changed.set()
        return result

    def _summary(self, item: JsonObject) -> JsonObject:
        occurrence = (
            self._read("workflow_occurrence", item["last_occurrence_id"])
            if item.get("last_occurrence_id")
            else None
        )
        status = occurrence.get("status") if occurrence else None
        if occurrence is not None:
            row = self.store.db.execute(
                "SELECT json_extract(data,'$.status') FROM objects WHERE kind='workflow_run' AND id=?",
                (occurrence["run_id"],),
            ).fetchone()
            if row is not None and row[0] in {
                "queued",
                "running",
                "completed",
                "failed",
                "cancelled",
                "interrupted",
                "timed_out",
            }:
                status = row[0]
        return {
            "id": item["id"],
            "name": item["name"],
            "version": item["version"],
            "enabled": item["active"],
            "blocked": item.get("blocked") is not None,
            "active": item["active"],
            "due_at": item["next_due_at"],
            "next_due_at": item["next_due_at"],
            "interval_seconds": item["interval_seconds"],
            "fired_count": item["fired_count"],
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
            "last_run_id": occurrence.get("run_id") if occurrence else None,
            "last_status": status,
        }

    def list(self, *, after_id: str = "", limit: int = 50) -> JsonObject:
        if type(limit) is not int or limit < 1 or not isinstance(after_id, str):
            raise ValueError("列表游标或数量无效")
        limit = min(limit, self.runtime.config.settings.limits.max_search_results)
        rows = self.store.db.execute(
            "SELECT id FROM objects WHERE kind='workflow_schedule' AND id>? ORDER BY id LIMIT ?",
            (after_id, limit + 1),
        ).fetchall()
        identifiers = [str(row[0]) for row in rows[:limit]]
        return self._output(
            {
                "items": [
                    self._summary(self._read("workflow_schedule", key)) for key in identifiers
                ],
                "next_after": identifiers[-1] if len(rows) > limit else None,
                "has_more": len(rows) > limit,
            }
        )

    async def cancel(self, identifier: str) -> JsonObject:
        # Tool/API callers already own their ordinary authentication and write gate.
        self.runtime.maintenance.admission.require()
        if self.runtime._closing:
            raise PermissionError("运行时正在关闭")
        item = self._read("workflow_schedule", identifier)
        if item["active"]:
            item.update(
                active=False, version=item["version"] + 1, next_due_at=None, updated_at=now()
            )
            self.store.put("workflow_schedule", item)
        self.runtime.reminders.changed.set()
        rows = self.store.db.execute(
            "SELECT id FROM objects WHERE kind='workflow_occurrence' "
            "AND json_extract(data,'$.schedule_id')=? AND json_extract(data,'$.status')='running'",
            (identifier,),
        ).fetchall()
        for row in rows:
            occurrence = self._read("workflow_occurrence", row[0])
            run = self._read("workflow_run", occurrence["run_id"])
            if not self._matches_run(item, occurrence, run):
                raise PermissionError("定时委托的运行关联无法核验，未取消其它运行")
            await self.runtime.workflows.cancel(occurrence["run_id"])
        await self.runtime.bus.emit("workflow_schedule.updated", self._summary(item))
        return self._output(self._summary(item))

    def _binding(self, session_id: str) -> tuple[JsonObject, JsonObject] | None:
        row = self.store.db.execute(
            "SELECT json_type(data,'$.scheduled_workflow'),json_extract(data,'$.workflow_run_id') "
            "FROM objects "
            "WHERE kind='session' AND id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        scheduled_run = self.store.db.execute(
            "SELECT 1 FROM objects WHERE kind='workflow_run' AND id=? "
            "AND json_type(data,'$.scheduled_occurrence_id') IS NOT NULL",
            (row[1],),
        ).fetchone()
        if row[0] is None and scheduled_run is None:
            return None
        if row[0] != "object" or scheduled_run is None:
            raise PermissionError("定时委托的节点关联缺失或改变")
        if session_id in self.runtime._stopping:
            raise PermissionError("定时委托节点正在停止")
        session = self._read("session", session_id)
        raw = session.get("scheduled_workflow")
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schedule_id", "version", "occurrence_id", "run_id"}
            or type(raw["version"]) is not int
            or any(
                not isinstance(raw[key], str) or not raw[key]
                for key in ("schedule_id", "occurrence_id", "run_id")
            )
            or raw["run_id"] != session.get("workflow_run_id")
            or session.get("workflow_node_id") not in {"work", "review"}
        ):
            raise PermissionError("定时委托的节点关联无效")
        item = self._read("workflow_schedule", raw["schedule_id"])
        occurrence = self._read("workflow_occurrence", raw["occurrence_id"])
        proof = self.store.db.execute(
            "SELECT 1 FROM objects WHERE kind='workflow_run' AND id=? "
            "AND json_extract(data,'$.schedule_id')=? "
            "AND json_extract(data,'$.schedule_version')=? "
            "AND json_extract(data,'$.scheduled_occurrence_id')=? "
            "AND json_extract(data,?)=?",
            (
                raw["run_id"],
                item["id"],
                raw["version"],
                occurrence["id"],
                "$.nodes." + session["workflow_node_id"] + ".session_id",
                session_id,
            ),
        ).fetchone()
        if (
            proof is None
            or occurrence.get("run_id") != raw["run_id"]
            or occurrence.get("schedule_id") != item["id"]
            or occurrence.get("version") != raw["version"]
            or occurrence.get("status") != "running"
            or occurrence.get("authorization") != _digest(item)
        ):
            raise PermissionError("定时委托的发生轮次关联已改变")
        self._authorize(item, raw["version"], occurrence.get("mode"))
        return item, occurrence

    def guard_session(self, session_id: str) -> None:
        self._binding(session_id)

    def request_snapshot(self, session_id: str) -> tuple[bool, TaskSnapshot | None]:
        binding = self._binding(session_id)
        if binding is None:
            return False, None
        snapshot = self._snapshot(binding[0])
        return True, replace(snapshot, session_id=session_id) if snapshot else None

    @staticmethod
    def _matches_run(item: JsonObject, occurrence: JsonObject, run: JsonObject) -> bool:
        return bool(
            run.get("id") == occurrence.get("run_id")
            and run.get("schedule_id") == item.get("id") == occurrence.get("schedule_id")
            and run.get("schedule_version") == occurrence.get("version")
            and run.get("scheduled_occurrence_id") == occurrence.get("id")
            and run.get("workflow_id") == item.get("workflow_id")
            and run.get("parent_session_id") == item.get("creator_session_id")
            and run.get("parent_run_id") is None
            and run.get("reply_origin") == item.get("reply_origin")
        )

    def _settle(self) -> bool:
        changed = False
        after = ""
        limit = self.runtime.config.settings.limits.max_pending_runs
        while rows := self.store.db.execute(
            "SELECT id FROM objects WHERE kind='workflow_occurrence' AND id>? "
            "AND json_extract(data,'$.status')='running' ORDER BY id LIMIT ?",
            (after, limit),
        ).fetchall():
            after = str(rows[-1][0])
            for row in rows:
                occurrence = self._read("workflow_occurrence", row[0])
                run = self._read("workflow_run", occurrence["run_id"])
                item = self._read("workflow_schedule", occurrence["schedule_id"])
                if not self._matches_run(item, occurrence, run):
                    raise PermissionError("定时委托的终态关联无法核验")
                if run["status"] in _ACTIVE:
                    continue
                changed = True
                updated = {**occurrence, "status": run["status"], "updated_at": now()}
                if run["status"] == "completed" and not run.get("result_message_id"):
                    try:
                        # Local publication retains the original destination; the
                        # existing automatic outbox gate records revoked routing as blocked.
                        self._authorize(
                            item, occurrence["version"], occurrence["mode"], check_destination=False
                        )
                        if (
                            occurrence["authorization"] != _digest(item)
                            or run.get("scheduled_occurrence_id") != occurrence["id"]
                            or run.get("schedule_id") != item["id"]
                            or run.get("reply_origin") != item["reply_origin"]
                        ):
                            raise PermissionError("定时结果的冻结授权已改变")
                    except (ValueError, PermissionError):
                        updated["publication"] = "revoked"
                    else:
                        with self.store.db:
                            self.runtime.workflows._publish_result(
                                run,
                                item["creator_session_id"],
                                (run["name"] + "\n" + run.get("result", ""))[
                                    : self.runtime.config.settings.limits.max_output_chars
                                ],
                                objects=[
                                    ("workflow_occurrence", {**updated, "publication": "recorded"})
                                ],
                                routing=FrozenRouting.bound(
                                    ReplyOrigin.load(item["reply_origin"])
                                ).dump(),
                            )
                        continue
                self.store.put("workflow_occurrence", updated)
        return changed

    async def tick(self, timestamp: float) -> None:
        runtime = self.runtime
        changed = False
        try:
            async with runtime.configuration_lock:
                if runtime._closing or not runtime.maintenance.admission.allowed:
                    return
                changed = self._settle()
                if runtime.attention_busy or not runtime.workflows.accepting_runs:
                    return
                rows = self.store.db.execute(
                    "SELECT id FROM objects WHERE kind='workflow_schedule' "
                    "AND json_extract(data,'$.active')=1 "
                    "AND json_extract(data,'$.next_due_at')<=? "
                    "ORDER BY json_extract(data,'$.next_due_at'),id LIMIT ?",
                    (timestamp, runtime.config.settings.limits.max_pending_runs),
                ).fetchall()
                for row in rows:
                    item = self._read("workflow_schedule", row[0])
                    try:
                        mode = self._authorize(item, item["version"])
                    except (ValueError, PermissionError) as exc:
                        item["blocked"] = type(exc).__name__
                        self.store.put("workflow_schedule", item)
                        continue
                    due = item["next_due_at"]
                    identifier = _digest([item["id"], item["version"], due])
                    if self.store.get("workflow_occurrence", identifier) is not None:
                        continue  # A lost reply cannot authorize a second start.
                    interval = item["interval_seconds"]
                    next_due = (
                        due + (math.floor(max(0, timestamp - due) / interval) + 1) * interval
                        if interval
                        else None
                    )
                    item.update(
                        next_due_at=next_due,
                        fired_count=item["fired_count"] + 1,
                        last_occurrence_id=identifier,
                        updated_at=now(),
                    )
                    item.pop("blocked", None)
                    occurrence: JsonObject = {
                        "id": identifier,
                        "schedule_id": item["id"],
                        "version": item["version"],
                        "due_at": due,
                        "status": "running",
                        "authorization": _digest(item),
                        "created_at": now(),
                        "updated_at": now(),
                    }
                    runtime.workflows._admit_start(
                        item["workflow_id"],
                        parent_session_id=item["creator_session_id"],
                        request_snapshot=self._snapshot(item),
                        reply_origin=ReplyOrigin.load(item["reply_origin"]),
                        scheduled=ScheduledOccurrence(item, occurrence, mode),
                    )
                    changed = True
                    break
        finally:
            if changed:
                await runtime.bus.notify()

    def next_due(self, timestamp: float) -> float | None:
        row = self.store.db.execute(
            "SELECT json_extract(data,'$.next_due_at') FROM objects WHERE kind='workflow_schedule' "
            "AND json_extract(data,'$.active')=1 AND json_extract(data,'$.next_due_at') IS NOT NULL "
            "AND json_extract(data,'$.blocked') IS NULL "
            "ORDER BY json_extract(data,'$.next_due_at') LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        due = float(row[0])
        return (
            None
            if due <= timestamp
            and (self.runtime.attention_busy or not self.runtime.workflows.accepting_runs)
            else due
        )
