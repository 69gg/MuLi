"""One-shot owner appointments for the original parked task and teammate."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from muli.channels.types import NAMES
from muli.commitments import STATES
from muli.conversation import MAX_EVENT_ID_CHARS
from muli.evidence import EvidenceError
from muli.reply_origin import ReplyOrigin, authorize_route, channel_account
from muli.store import now
from muli.task_control import (
    ControlConflict,
    ControlUnavailable,
    OwnerControlRequest,
    TaskControl,
    TaskPaused,
    TaskSnapshot,
    _digest,
    _json,
)

if TYPE_CHECKING:
    from muli.runtime import Runtime

JsonObject = dict[str, Any]
_MODES = ("ask", "auto", "yolo")
_SCHEDULE = "task_replan_schedule"
_OCCURRENCE = "task_replan_occurrence"


class ScheduledTaskReplans:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime, self.store = runtime, runtime.store

    @property
    def _budget(self) -> int:
        limits = self.runtime.config.settings.limits
        return (limits.max_context_chars + limits.max_output_chars) * 4

    def _read(self, kind: str, identifier: str) -> JsonObject:
        item = self.store.bounded_object(kind, identifier, max_bytes=self._budget)
        if item is None:
            raise ControlUnavailable("schedule_invalid", "定时重规划的持久证明不存在")
        return item

    def _control(self) -> TaskControl:
        control = self.runtime.task_control
        if control is None:
            raise ControlUnavailable("schedule_unavailable", "任务控制服务不可用")
        return control

    def _output(self, item: JsonObject) -> JsonObject:
        if len(_json(item)) > self.runtime.config.settings.limits.max_output_chars:
            raise EvidenceError("output_budget", "定时重规划结果超过输出预算")
        return item

    @staticmethod
    def _time(value: object) -> bool:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    @staticmethod
    def _integer(value: object) -> bool:
        return type(value) is int and value >= 0

    def _source(self, item: JsonObject) -> ReplyOrigin:
        origin = ReplyOrigin.load(item.get("reply_origin"))
        if (
            origin.kind not in {"web", "channel"}
            or not origin.input_id
            or origin.dump() != item.get("reply_origin")
        ):
            raise PermissionError("定时重规划缺少准确的原始主人来源")
        row = self.store.db.execute(
            "SELECT CASE WHEN length(CAST(metadata AS BLOB))<=? THEN CAST(metadata AS BLOB) END "
            "FROM (SELECT json_object('id',id,'source',json_extract(data,'$.source'),"
            "'principal',json_extract(data,'$.principal'),'route',json_extract(data,'$.route'),"
            "'reply_origin',json_extract(data,'$.reply_origin')) AS metadata "
            "FROM objects WHERE kind='message' AND id=? "
            "AND json_extract(data,'$.principal')='owner' AND json_extract(data,'$.session_id')=?)",
            (self._budget, origin.input_id, item["creator_session_id"]),
        ).fetchone()
        if row is None or not isinstance(row[0], bytes):
            raise PermissionError("原始主人输入无法完整核验")
        try:
            proof = json.loads(row[0].decode("utf-8", errors="strict"))
        except (ValueError, UnicodeError) as exc:
            raise PermissionError("原始主人输入无法完整核验") from exc
        if ReplyOrigin.from_input(proof) != origin:
            raise PermissionError("原始主人来源已改变")
        if origin.kind == "channel":
            route = origin.delivery_routes()[0]
            name = route["name"]
            if name not in NAMES or route.get("kind", "private") != "private":
                raise PermissionError("定时重规划只支持原始私聊或 Web")
            profile = getattr(self.runtime.config.settings.channels, name)
            if not profile.enabled:
                raise PermissionError("原始私聊渠道已禁用")
            authorize_route(
                name,
                route,
                account_id=channel_account(name, profile),
                owner_ids=profile.owner_ids,
                allowed_groups=profile.allowed_groups,
                outbound=True,
            )
        return origin

    def _task(self, item: JsonObject, stage: str) -> JsonObject:
        control = self._control()
        task = self._read("commitment", item["task_id"])
        state = control._state(task)
        admitted = stage in {"admitted", "approved"}
        approved = stage == "approved"
        epoch = item["epoch"] + int(admitted)
        if (
            task.get("status") not in STATES
            or task["status"] in {"done", "cancelled"}
            or type(task.get("revision")) is not int
            or task["revision"] != item["task_revision"] + int(admitted) + int(approved)
            or _json(control.commitments._validate_brief(task.get("brief"), stored=True))
            != item["brief_json"]
            or state["epoch"] != epoch
            or state["approved_epoch"] != (epoch if approved else item["approved_epoch"])
            or (not admitted and _json(state) != item["control_json"])
        ):
            raise ControlConflict("schedule_stale", "原任务要求或控制版本已经改变")
        return task

    def _identity(self, item: JsonObject) -> JsonObject:
        if (
            item.get("version") != 1
            or type(item.get("version")) is not int
            or item.get("authorization_revoked") is not False
            or item.get("creator_session_id") != self.store.existing_main_session_id()
            or self.runtime._closing
            or "task_schedule_replan" in self.runtime.config.settings.disabled_tools
            or not self._time(item.get("due_at"))
            or not self._time(item.get("expires_at"))
            or item["expires_at"] <= item["due_at"]
            or any(
                not self._integer(item.get(key))
                for key in (
                    "task_revision",
                    "epoch",
                    "approved_epoch",
                    "target_generation",
                    "target_completion_revision",
                )
            )
        ):
            raise PermissionError("定时重规划授权已撤销或损坏")
        self.runtime.maintenance.admission.require()
        main = self._read("session", item["creator_session_id"])
        agent = self._read("agent", item["target_id"])
        session = self._read("session", item["target_id"])
        if (
            main.get("kind") != "main"
            or main.get("principal") != "owner"
            or item.get("main_mode") not in _MODES
            or item.get("target_mode") not in _MODES
            or _MODES.index(item["target_mode"]) > _MODES.index(item["main_mode"])
            or main.get("mode") != item["main_mode"]
            or agent.get("mode") != item["target_mode"]
            or session.get("mode") != item["target_mode"]
            or session.get("kind") != "team"
            or agent.get("session_id") != item["target_id"]
            or agent.get("parent_id") != item["creator_session_id"]
            or agent.get("commitment_id") != item["task_id"]
            or self._control().resolve_scope(item["target_id"]) != item["task_id"]
            or item["creator_session_id"] in self.runtime._stopping
            or item["target_id"] in self.runtime._stopping
        ):
            raise PermissionError("原主助手或原队友的身份、模式已经改变")
        self._source(item)
        return agent

    def _parked(self, item: JsonObject, agent: JsonObject) -> None:
        if (
            agent.get("status") != "paused"
            or type(agent.get("generation")) is not int
            or agent["generation"] != item["target_generation"]
            or type(agent.get("completion_revision")) is not int
            or agent["completion_revision"] != item["target_completion_revision"]
        ):
            raise ControlConflict("target_stale", "原队友已经不处于约定的暂停代次")

    @staticmethod
    def _summary(item: JsonObject) -> JsonObject:
        return {
            key: item.get(key)
            for key in (
                "id",
                "task_id",
                "target_id",
                "due_at",
                "expires_at",
                "version",
                "state",
                "authorization_revoked",
                "occurrence_id",
                "created_at",
                "updated_at",
            )
        }

    def create(
        self, task_id: str, target_id: str, due_at: float, expires_at: float, key: str
    ) -> JsonObject:
        caller, run_id, request, mode = self.runtime.scheduled_workflows._caller()
        if (
            any(not isinstance(value, str) or not value for value in (task_id, target_id, key))
            or len(key) > MAX_EVENT_ID_CHARS
            or not self._time(due_at)
            or not self._time(expires_at)
            or due_at <= now()
            or expires_at <= due_at
        ):
            raise ValueError("任务、原队友、未来时间窗或幂等 key 无效")
        key.encode("utf-8", errors="strict")
        identifier = _digest(["scheduled-task-replan", caller, request.reply_origin.input_id, key])
        fingerprint = _digest([task_id, target_id, due_at, expires_at])
        previous = self.store.bounded_object(_SCHEDULE, identifier, max_bytes=self._budget)
        if previous is not None:
            if previous.get("fingerprint") != fingerprint:
                raise ControlConflict("key_conflict", "相同约定 key 的内容不同")
            return self._output(self._summary(previous))
        control = self._control()
        task = self._read("commitment", task_id)
        state = control._state(task)
        agent = self._read("agent", target_id)
        if state["epoch"] <= state["approved_epoch"]:
            raise ControlConflict("not_paused", "请先暂停原任务")
        item: JsonObject = {
            "id": identifier,
            "version": 1,
            "state": "waiting",
            "authorization_revoked": False,
            "task_id": task_id,
            "task_revision": task["revision"],
            "epoch": state["epoch"],
            "approved_epoch": state["approved_epoch"],
            "brief_json": _json(
                control.commitments._validate_brief(task.get("brief"), stored=True)
            ),
            "control_json": _json(state),
            "target_id": target_id,
            "target_generation": agent.get("generation"),
            "target_completion_revision": agent.get("completion_revision"),
            "target_mode": agent.get("mode"),
            "main_mode": mode,
            "creator_session_id": caller,
            "creator_run_id": run_id,
            "reply_origin": request.reply_origin.dump(),
            "due_at": due_at,
            "expires_at": expires_at,
            "fingerprint": fingerprint,
            "occurrence_id": None,
            "created_at": now(),
            "updated_at": now(),
        }
        self._task(item, "waiting")
        self._parked(item, self._identity(item))
        if control.scope(task_id, active_workflow_ids=self.runtime.workflows.active_run_ids)[
            "active_workflow_count"
        ]:
            raise ControlConflict("workflow_active", "该任务仍有活动工作流")
        if len(_json(item).encode("utf-8")) > self._budget:
            raise EvidenceError("output_budget", "完整约定与授权超过保存预算")
        result = self._output(self._summary(item))
        with control._transaction():
            count = self.store.db.execute(
                "SELECT count(*) FROM objects WHERE kind=? "
                "AND json_extract(data,'$.authorization_revoked')=0 "
                "AND json_extract(data,'$.state') IN ('waiting','admitted')",
                (_SCHEDULE,),
            ).fetchone()[0]
            if count >= self.runtime.config.settings.limits.max_pending_runs:
                raise ControlUnavailable("queue_full", "定时重规划已达到原待办上限")
            self.store._event("task.replan.scheduled", result, caller, objects=[(_SCHEDULE, item)])
        return result

    def list(self, *, limit: int = 50, after: str = "") -> JsonObject:
        if type(limit) is not int or limit < 1 or not isinstance(after, str):
            raise ValueError("列表游标或数量无效")
        limit = min(limit, self.runtime.config.settings.limits.max_search_results)
        rows = self.store.db.execute(
            "SELECT CASE WHEN length(CAST(id AS BLOB))<=? THEN CAST(id AS BLOB) END "
            "FROM objects WHERE kind=? AND id>? ORDER BY id LIMIT ?",
            (self._budget, _SCHEDULE, after, limit + 1),
        ).fetchall()
        identifiers = []
        for row in rows[:limit]:
            if not isinstance(row[0], bytes):
                raise EvidenceError("content_unavailable", "约定目录无法完整核查")
            identifiers.append(row[0].decode("utf-8", errors="strict"))
        return self._output(
            {
                "items": [
                    self._summary(self._read(_SCHEDULE, identifier)) for identifier in identifiers
                ],
                "next_after": identifiers[-1] if len(rows) > limit else None,
                "has_more": len(rows) > limit,
            }
        )

    def _ids(self, condition: str, parameters: Sequence[object]) -> Iterator[str]:
        after = ""
        while True:
            rows = self.store.db.execute(
                "SELECT CAST(id AS BLOB) FROM objects WHERE kind=? AND id>? "
                "AND length(CAST(id AS BLOB))<=? AND " + condition + " ORDER BY id LIMIT ?",
                (
                    _SCHEDULE,
                    after,
                    self._budget,
                    *parameters,
                    self.runtime.config.settings.limits.max_pending_runs,
                ),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                after = row[0].decode("utf-8", errors="strict")
                yield after

    def _revoke(self, item: JsonObject, reason: str, *, state: str = "revoked") -> None:
        if item.get("authorization_revoked") is True:
            return
        updated = {
            **item,
            "authorization_revoked": True,
            "revoked_reason": reason,
            "updated_at": now(),
            "state": item["state"] if item["state"] == "approved" else state,
        }
        self.store._put(_SCHEDULE, updated)
        if item.get("occurrence_id"):
            occurrence = self._read(_OCCURRENCE, item["occurrence_id"])
            self.store._put(
                _OCCURRENCE,
                {
                    **occurrence,
                    "authorization_revoked": True,
                    "revoked_reason": reason,
                    "state": occurrence["state"] if occurrence["state"] == "approved" else state,
                    "updated_at": now(),
                },
            )

    def owner_input_admitted(self, session_id: str, input_id: str) -> None:
        for identifier in self._ids(
            "json_extract(data,'$.creator_session_id')=? "
            "AND json_extract(data,'$.state')='admitted' "
            "AND json_extract(data,'$.authorization_revoked')=0",
            (session_id,),
        ):
            item = self._read(_SCHEDULE, identifier)
            self._revoke(item, "owner_input:" + input_id)

    def session_mode_changed(self, session_id: str, previous: str, selected: str) -> None:
        if previous == selected:
            return
        for identifier in self._ids(
            "json_extract(data,'$.authorization_revoked')=0 "
            "AND json_extract(data,'$.state') IN ('waiting','admitted','approved') "
            "AND (json_extract(data,'$.creator_session_id')=? OR json_extract(data,'$.target_id')=?)",
            (session_id, session_id),
        ):
            self._revoke(self._read(_SCHEDULE, identifier), "mode_changed")

    def cancel(self, id: str) -> JsonObject:
        caller, _, _, _ = self.runtime.scheduled_workflows._caller()
        with self._control()._transaction():
            item = self._read(_SCHEDULE, id)
            if item.get("creator_session_id") != caller:
                raise PermissionError("约定不属于本主会话")
            if item.get("state") == "approved":
                raise ControlConflict("already_approved", "已经批准，请使用原任务暂停或停止入口")
            self._revoke(item, "cancelled", state="cancelled")
            return self._output(self._summary(self._read(_SCHEDULE, id)))

    def _receipt(self, receipt: JsonObject) -> tuple[JsonObject, JsonObject]:
        marker = receipt.get("scheduled_replan")
        if (
            not isinstance(marker, dict)
            or set(marker) != {"version", "schedule_id", "occurrence_id"}
            or type(marker.get("version")) is not int
            or marker["version"] != 1
            or not isinstance(marker.get("schedule_id"), str)
            or not isinstance(marker.get("occurrence_id"), str)
        ):
            raise PermissionError("定时主人回执证明无效")
        item = self._read(_SCHEDULE, marker["schedule_id"])
        occurrence = self._read(_OCCURRENCE, marker["occurrence_id"])
        stage = item.get("state")
        if (
            item.get("authorization_revoked") is True
            or occurrence.get("authorization_revoked") is True
            or stage in {"revoked", "cancelled", "expired"}
        ):
            raise ControlConflict("schedule_stale", "定时重规划资格已经失效，请处理当前主人输入")
        expected_id = _digest([item["id"], item["version"], item["due_at"]])
        request_id = "scheduled-replan:" + expected_id
        owner_id = self._control()._receipt_id(item["task_id"], "owner", request_id)
        if (
            stage not in {"admitted", "approved"}
            or item.get("occurrence_id") != expected_id
            or occurrence.get("id") != expected_id
            or occurrence.get("schedule_id") != item["id"]
            or occurrence.get("schedule_version") != item["version"]
            or type(occurrence.get("schedule_version")) is not int
            or occurrence.get("state") != stage
            or occurrence.get("authorization_revoked") is not False
            or occurrence.get("owner_receipt_id") != owner_id
            or receipt.get("id") != owner_id
            or receipt.get("request_id") != request_id
            or receipt.get("actor") != "owner"
            or receipt.get("action") != "request_replan"
            or receipt.get("commitment_id") != item["task_id"]
            or type(receipt.get("epoch")) is not int
            or receipt["epoch"] != item["epoch"] + 1
            or type(receipt.get("revision")) is not int
            or receipt["revision"] != item["task_revision"] + 1
            or occurrence.get("epoch") != receipt["epoch"]
            or receipt.get("ingress_id") != occurrence.get("ingress_id")
            or not isinstance(occurrence.get("ingress_id"), str)
        ):
            raise PermissionError("定时接纳、主人回执与约定不一致")
        task = self._task(item, stage)
        state = self._control()._state(task)
        if state["replan_request_id"] != owner_id or state.get("scheduled_replan") != marker:
            raise PermissionError("定时主人控制已经被替换")
        row = self.store.db.execute(
            "SELECT session_id,state,CASE WHEN length(CAST(payload AS BLOB))<=? "
            "THEN CAST(payload AS BLOB) END AS payload FROM ingress WHERE id=?",
            (self._budget, occurrence["ingress_id"]),
        ).fetchone()
        if (
            row is None
            or row["session_id"] != item["creator_session_id"]
            or row["state"] not in {"pending", "delivered"}
            or not isinstance(row["payload"], bytes)
        ):
            raise PermissionError("定时主人回执没有其原始入站事实")
        try:
            payload = json.loads(row["payload"].decode("utf-8", errors="strict"))
        except (ValueError, UnicodeError) as exc:
            raise PermissionError("定时入站事实无法完整核查") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("id") != occurrence["ingress_id"]
            or payload.get("session_id") != item["creator_session_id"]
            or payload.get("source") != "task-control"
            or payload.get("principal") != "owner"
            or payload.get("mode") != item["main_mode"]
            or payload.get("route") is not None
            or payload.get("reply_origin") != item["reply_origin"]
            or payload.get("task_control")
            != {"commitment_id": item["task_id"], "receipt_id": owner_id, "epoch": receipt["epoch"]}
        ):
            raise PermissionError("定时入站来源与主人控制不一致")
        self._identity(item)
        return item, occurrence

    def _require_window(self, item: JsonObject) -> None:
        if now() < item["expires_at"]:
            return
        # Runtime's request gate runs before the tool transaction. A deadline
        # observed there stays revoked even if the clock is subsequently edited.
        if not self.store.db.in_transaction:
            with self._control()._transaction():
                self._revoke(self._read(_SCHEDULE, item["id"]), "expired", state="expired")
        raise ControlConflict("schedule_expired", "约定的重新开始时间窗已经结束")

    def guard_receipt(self, receipt: JsonObject, targets: Sequence[str] | None = None) -> None:
        item, _ = self._receipt(receipt)
        if targets is not None and list(targets) != [item["target_id"]]:
            raise ControlConflict("target_mismatch", "约定只允许重新规划给原来的唯一队友")
        if item["state"] == "admitted":
            self._require_window(item)
            self._parked(item, self._read("agent", item["target_id"]))

    def _completed_generation(self, item: JsonObject, occurrence: JsonObject) -> bool:
        """Retire only this exact completed generation, never an arbitrary newer one."""
        revision = item["target_completion_revision"] + 1
        rows = self.store.db.execute(
            "SELECT CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB))<=? "
            "THEN CAST(data AS BLOB) END FROM events WHERE type='team.finished' "
            "AND session_id=? AND json_extract(data,'$.completion_revision')=? LIMIT 2",
            (self._budget, item["target_id"], revision),
        ).fetchall()
        if len(rows) != 1 or not isinstance(rows[0][0], bytes):
            return False
        try:
            completion = json.loads(rows[0][0].decode("utf-8", errors="strict"))
            if not isinstance(completion, dict):
                return False
            run_id = completion.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                return False
            run = self._read("run", run_id)
            report = self._read("team_report", run_id)
            approve_id = self._control()._receipt_id(
                item["task_id"], "main", occurrence["owner_receipt_id"]
            )
            delivery_id = _digest([approve_id, item["target_id"]])
            delivery = self._read("task_plan_delivery", delivery_id)
            return bool(
                item.get("state") == occurrence.get("state") == "approved"
                and occurrence.get("approve_receipt_id") == approve_id
                and occurrence.get("delivery_id") == delivery_id
                and completion.get("agent_id") == item["target_id"]
                and completion.get("status") == "completed"
                and type(completion.get("generation")) is int
                and completion["generation"] == item["target_generation"] + 1
                and type(completion.get("completion_revision")) is int
                and completion["completion_revision"] == revision
                and run.get("session_id") == item["target_id"]
                and run.get("status") == "completed"
                and report.get("agent_id") == item["target_id"]
                and report.get("parent_id") == item["creator_session_id"]
                and report.get("execution_status") == "completed"
                and delivery.get("state") == "delivered"
                and delivery.get("path") == "archive"
                and delivery.get("scheduled_owner_receipt_id") == occurrence["owner_receipt_id"]
                and delivery.get("target_session_id") == item["target_id"]
                and delivery.get("target_generation") == item["target_generation"]
                and delivery.get("target_completion_revision") == item["target_completion_revision"]
            )
        except (ValueError, KeyError, TypeError, UnicodeError):
            return False

    def _delivery(
        self, item: JsonObject, occurrence: JsonObject, agent: JsonObject, *, starting: bool
    ) -> None:
        control = self._control()
        approve_id = control._receipt_id(item["task_id"], "main", occurrence["owner_receipt_id"])
        approve = self._read("task_control_receipt", approve_id)
        delivery_id = _digest([approve_id, item["target_id"]])
        delivery = self._read("task_plan_delivery", delivery_id)
        notice = self._read("message", delivery.get("archive_message_id", ""))
        epoch = item["epoch"] + 1
        reference = {"commitment_id": item["task_id"], "epoch": epoch, "delivery_id": delivery_id}
        if (
            item["state"] != "approved"
            or occurrence.get("approve_receipt_id") != approve_id
            or occurrence.get("delivery_id") != delivery_id
            or approve.get("actor") != "main"
            or approve.get("action") != "approve_plan"
            or approve.get("request_id") != occurrence["owner_receipt_id"]
            or approve.get("commitment_id") != item["task_id"]
            or approve.get("epoch") != epoch
            or delivery.get("state") != "delivered"
            or delivery.get("path") != "archive"
            or delivery.get("commitment_id") != item["task_id"]
            or delivery.get("epoch") != epoch
            or delivery.get("scheduled_owner_receipt_id") != occurrence["owner_receipt_id"]
            or delivery.get("target_session_id") != item["target_id"]
            or delivery.get("target_generation") != item["target_generation"]
            or delivery.get("target_completion_revision") != item["target_completion_revision"]
            or delivery.get("expected_status") != "paused"
            or notice.get("session_id") != item["target_id"]
            or notice.get("role") != "user"
            or notice.get("source") != "task-control"
            or notice.get("principal") != "system:task_control"
            or notice.get("delivery_status") != "delivered"
            or notice.get("run_id") is not None
            or notice.get("task_control") != reference
            or notice.get("route") is not None
            or type(agent.get("generation")) is not int
            or agent["generation"] != item["target_generation"] + (0 if starting else 1)
            or type(agent.get("completion_revision")) is not int
            or agent["completion_revision"] != item["target_completion_revision"]
        ):
            raise PermissionError("原队友的新代缺少准确方案投递证明")

    def _owner_receipt_for_task(self, task_id: str) -> JsonObject | None:
        task = self._read("commitment", task_id)
        identifier = self._control()._state(task)["replan_request_id"]
        if not identifier:
            return None
        receipt = self._read("task_control_receipt", identifier)
        if "scheduled_replan" not in receipt:
            self._control()._guard_scheduled_replan(receipt)
        return receipt if "scheduled_replan" in receipt else None

    def guard_target_start(self, agent: JsonObject, session: JsonObject) -> None:
        task_id = agent.get("commitment_id")
        if not isinstance(task_id, str):
            return
        receipt = self._owner_receipt_for_task(task_id)
        if receipt is None:
            return
        marker = receipt["scheduled_replan"]
        item = self._read(_SCHEDULE, marker["schedule_id"])
        if agent["id"] != item.get("target_id"):
            return
        occurrence = self._read(_OCCURRENCE, marker["occurrence_id"])
        if self._completed_generation(item, occurrence):
            return  # A later explicit Team.send keeps the ordinary authorization contract.
        try:
            item, occurrence = self._receipt(receipt)
            self._require_window(item)
            if session.get("id") != item["target_id"] or agent.get("status") != "queued":
                raise PermissionError("原队友没有准确排队状态")
            self._delivery(item, occurrence, agent, starting=True)
        except (ValueError, PermissionError) as exc:
            raise TaskPaused("schedule_revoked", "定时重规划资格已失效，保持暂停") from exc

    def guard_request(
        self, session_id: str, run_id: str | None, selected_replan: TaskSnapshot | None, risk: str
    ) -> None:
        if session_id == self.store.existing_main_session_id():
            receipt = None
            if selected_replan is not None and selected_replan.replan_request_id:
                receipt = self._read("task_control_receipt", selected_replan.replan_request_id)
            elif run_id is not None:
                rows = self.store.db.execute(
                    "SELECT CASE WHEN length(CAST(o.data AS BLOB))<=? THEN CAST(o.data AS BLOB) END "
                    "FROM objects o JOIN ingress i ON i.id=json_extract(o.data,'$.ingress_id') "
                    "WHERE o.kind=? AND i.session_id=? AND i.run_id=? LIMIT 2",
                    (self._budget, _OCCURRENCE, session_id, run_id),
                ).fetchall()
                if len(rows) > 1:
                    raise PermissionError("本主运行的定时接纳证明不唯一")
                if rows:
                    if not isinstance(rows[0][0], bytes):
                        raise PermissionError("本主运行的定时接纳证明超过预算")
                    occurrence = json.loads(rows[0][0].decode("utf-8", errors="strict"))
                    receipt = self._read("task_control_receipt", occurrence["owner_receipt_id"])
            if receipt is None:
                return
            if "scheduled_replan" not in receipt:
                self._control()._guard_scheduled_replan(receipt)
                return
            marker = receipt["scheduled_replan"]
            item = self._read(_SCHEDULE, marker["schedule_id"])
            # A new owner input must be claimable by this same main run. Its
            # specific old approval remains rejected by guard_receipt.
            reason = item.get("revoked_reason", "")
            if isinstance(reason, str) and reason.startswith("owner_input:"):
                return
            try:
                self.guard_receipt(receipt)
            except (ValueError, PermissionError) as exc:
                raise TaskPaused(
                    "schedule_revoked", "定时重规划资格已失效，原任务保持暂停"
                ) from exc
            return
        agent = self.store.bounded_object("agent", session_id, max_bytes=self._budget)
        if agent is None or not isinstance(agent.get("commitment_id"), str):
            return
        receipt = self._owner_receipt_for_task(agent["commitment_id"])
        if receipt is None:
            return
        item = self._read(_SCHEDULE, receipt["scheduled_replan"]["schedule_id"])
        if agent["id"] != item.get("target_id") or (
            agent.get("generation") != item["target_generation"] + 1
        ):
            return
        try:
            item, occurrence = self._receipt(receipt)
            self._delivery(item, occurrence, agent, starting=False)
            run = self._read("run", run_id or "")
            if (
                not run_id
                or agent.get("run_id") != run_id
                or self.runtime.run_ids.get(session_id) != run_id
                or run.get("session_id") != session_id
                or run.get("status") != "running"
                or run.get("mode") != item["target_mode"]
                or agent.get("status") != "running"
            ):
                raise PermissionError("原队友当前执行已失效")
        except (ValueError, PermissionError) as exc:
            raise TaskPaused("schedule_revoked", "定时重规划资格已失效，不能继续旧工具") from exc

    async def tick(self, timestamp: float) -> None:
        runtime = self.runtime
        if not self._time(timestamp):
            raise ValueError("调度时间无效")
        results = []
        async with runtime.configuration_lock:
            if runtime._closing or not runtime.maintenance.admission.allowed:
                return
            timestamp = max(timestamp, now())
            for identifier in self._ids(
                "json_extract(data,'$.state') IN ('waiting','admitted','approved') "
                "AND json_extract(data,'$.authorization_revoked')=0 "
                "AND json_extract(data,'$.expires_at')<=?",
                (timestamp,),
            ):
                item = self._read(_SCHEDULE, identifier)
                if item["state"] == "approved":
                    agent = self._read("agent", item["target_id"])
                    if agent.get("generation") != item["target_generation"]:
                        continue  # Natural expiry cannot recall an already started generation.
                with self._control()._transaction():
                    self._revoke(item, "expired", state="expired")
            for identifier in self._ids(
                "json_extract(data,'$.state')='waiting' "
                "AND json_extract(data,'$.authorization_revoked')=0 "
                "AND json_extract(data,'$.due_at')<=?",
                (timestamp,),
            ):
                item = self._read(_SCHEDULE, identifier)
                if timestamp >= item["expires_at"]:
                    with self._control()._transaction():
                        self._revoke(item, "expired", state="expired")
                    continue
                if runtime.attention_busy or not runtime.workflows.accepting_runs:
                    continue
                if not runtime.config.settings.models.main.configured:
                    continue
                control = self._control()
                with control._transaction():
                    item = self._read(_SCHEDULE, identifier)
                    try:
                        self._task(item, "waiting")
                        self._parked(item, self._identity(item))
                        if control.scope(
                            item["task_id"], active_workflow_ids=runtime.workflows.active_run_ids
                        )["active_workflow_count"]:
                            raise ControlConflict("workflow_active", "原任务已有活动工作流")
                    except (ValueError, PermissionError):
                        self._revoke(item, "frozen_proof_changed")
                        continue
                    if self.store.pending_inputs(item["creator_session_id"]) >= (
                        runtime.config.settings.limits.max_pending_runs
                    ):
                        continue
                    occurrence_id = _digest([item["id"], item["version"], item["due_at"]])
                    if self.store.bounded_object(
                        _OCCURRENCE, occurrence_id, max_bytes=self._budget
                    ):
                        raise ControlUnavailable(
                            "schedule_invalid", "约定已有接纳事实，不能重造控制"
                        )
                    marker = {
                        "version": 1,
                        "schedule_id": item["id"],
                        "occurrence_id": occurrence_id,
                    }
                    if max(timestamp, now()) >= item["expires_at"]:
                        self._revoke(item, "expired", state="expired")
                        continue
                    result = control._owner_control_in_transaction(
                        item["task_id"],
                        OwnerControlRequest(
                            "request_replan", item["epoch"], "scheduled-replan:" + occurrence_id
                        ),
                        main_session_id=item["creator_session_id"],
                        mode=item["main_mode"],
                        model_configured=True,
                        max_pending=runtime.config.settings.limits.max_pending_runs,
                        active_workflow_ids=runtime.workflows.active_run_ids,
                        expected_approved_epoch=item["approved_epoch"],
                        reply_origin=ReplyOrigin.load(item["reply_origin"]),
                        scheduled_replan=marker,
                    )
                    occurrence: JsonObject = {
                        "id": occurrence_id,
                        "schedule_id": item["id"],
                        "schedule_version": item["version"],
                        "owner_receipt_id": result.receipt["id"],
                        "ingress_id": result.receipt["ingress_id"],
                        "epoch": result.receipt["epoch"],
                        "state": "admitted",
                        "authorization_revoked": False,
                        "created_at": now(),
                        "updated_at": now(),
                    }
                    self.store._put(_OCCURRENCE, occurrence)
                    self.store._put(
                        _SCHEDULE,
                        {
                            **item,
                            "state": "admitted",
                            "occurrence_id": occurrence_id,
                            "updated_at": now(),
                        },
                    )
                    results.append(result)
                break
        for result in results:
            await runtime.task_control_committed(result)

    def next_due(self, timestamp: float) -> float | None:
        runtime = self.runtime
        busy = runtime.attention_busy or not runtime.workflows.accepting_runs
        field = (
            "expires_at" if busy or not runtime.config.settings.models.main.configured else "due_at"
        )
        row = self.store.db.execute(
            "SELECT min(CASE WHEN json_extract(s.data,'$.state')='waiting' "
            "THEN json_extract(s.data,?) ELSE json_extract(s.data,'$.expires_at') END) "
            "FROM objects s LEFT JOIN objects a ON a.kind='agent' AND a.id=json_extract(s.data,'$.target_id') "
            "WHERE s.kind=? AND json_extract(s.data,'$.authorization_revoked')=0 "
            "AND (json_extract(s.data,'$.state') IN ('waiting','admitted') "
            "OR (json_extract(s.data,'$.state')='approved' "
            "AND json_extract(a.data,'$.generation')=json_extract(s.data,'$.target_generation')))",
            ("$." + field, _SCHEDULE),
        ).fetchone()
        return float(row[0]) if row is not None and self._time(row[0]) else None
