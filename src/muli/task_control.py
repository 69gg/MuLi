"""Synchronous task-control facts; the host owns authentication, locks and wakeups.

No model, event-loop worker or handler is started here. Callers publish/wake only
after these methods return, and keep execution snapshots separate from main's
selected replan snapshot.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Literal

from muli.commitments import Commitments
from muli.context import ContextLimitError, context_size
from muli.conversation import MAX_EVENT_ID_CHARS, Conversation
from muli.providers import Message
from muli.reply_origin import ReplyOrigin
from muli.store import Store, new_id, now

JsonObject = dict[str, Any]
_WORKFLOW_ACTIVE = ("pending", "queued", "running")
_DELIVERY_TARGETS = frozenset({"paused", "queued", "running"})
_CONTEXT_LABEL = (
    "完整受控任务说明（目标与原始要求不可被计划、摘要或队友转述覆盖；"
    "计划不是新授权，暂停后的旧工具调用不能恢复执行）：\n"
)


class ControlConflict(ValueError):
    """A stable domain code for the API layer, normally an HTTP 409."""

    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(f"{code}: {message}")


class ControlUnavailable(ValueError):
    """A configuration, integrity or capacity problem, without an implied mutation."""

    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(f"{code}: {message}")


class TaskPaused(ControlConflict):
    """The caller must journal unexecuted tool results and park this generation."""

    def __init__(self, code: str, message: str, *, observed_epoch: int | None = None) -> None:
        self.observed_epoch = observed_epoch
        super().__init__(code, message)


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _scheduled_marker(value: object) -> JsonObject:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "schedule_id", "occurrence_id"}
        or type(value.get("version")) is not int
        or value["version"] != 1
        or any(
            not isinstance(value.get(key), str)
            or not value[key]
            or len(value[key]) > MAX_EVENT_ID_CHARS
            for key in ("schedule_id", "occurrence_id")
        )
    ):
        raise ControlUnavailable("control_invalid", "定时重规划授权标记无效")
    try:
        _json(value).encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ControlUnavailable("control_invalid", "定时重规划授权标记无效") from exc
    return dict(value)


def _positive(value: int, name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} 必须为正整数")


@dataclass(frozen=True, slots=True)
class OwnerControlRequest:
    action: Literal["pause", "request_replan"]
    expected_epoch: int
    request_id: str
    additional_requirements: str = ""

    def __post_init__(self) -> None:
        if self.action not in {"pause", "request_replan"}:
            raise ValueError("未知任务控制操作")
        if type(self.expected_epoch) is not int or self.expected_epoch < 0:
            raise ValueError("expected_epoch 必须为非负整数")
        if (
            not isinstance(self.request_id, str)
            or not self.request_id.strip()
            or len(self.request_id) > MAX_EVENT_ID_CHARS
        ):
            raise ValueError("任务控制 request_id 无效")
        if not isinstance(self.additional_requirements, str) or (
            self.action == "pause" and self.additional_requirements
        ):
            raise ValueError("pause 不能同时修改要求")


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """Serialized values cannot be mutated after a request has used them."""

    session_id: str
    commitment_id: str
    revision: int
    epoch: int
    approved_epoch: int
    replan_request_id: str | None
    brief_json: str
    plan_json: str

    def context(self) -> str:
        return _CONTEXT_LABEL + _json(
            {
                "commitment_id": self.commitment_id,
                "revision": self.revision,
                "brief": json.loads(self.brief_json),
                "control": {
                    "epoch": self.epoch,
                    "approved_epoch": self.approved_epoch,
                    "plan": json.loads(self.plan_json),
                },
            }
        )


@dataclass(frozen=True, slots=True)
class ControlResult:
    receipt: JsonObject
    current: JsonObject
    created: bool
    ingress: JsonObject | None = None

    def public(self) -> JsonObject:
        return {"receipt": dict(self.receipt), "current": self.current}


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    state: Literal["pending", "delivered", "superseded"]
    session_id: str
    changed: bool = False
    wake: bool = False
    ingress: JsonObject | None = None
    message: JsonObject | None = None
    path: Literal["ingress", "archive"] | None = None


class TaskControl:
    def __init__(self, store: Store, commitments: Commitments) -> None:
        if commitments.store is not store:
            raise ValueError("任务控制与工作账本必须使用同一个 Store")
        self.store, self.commitments = store, commitments
        self.scheduled_replan_guard: Callable[[JsonObject, Sequence[str] | None], None] | None = (
            None
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self.store.db.in_transaction:
            raise RuntimeError("任务控制不能嵌套在另一个提交事务中")
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            yield

    def _main(self, session_id: str) -> None:
        row = self.store.db.execute(
            "SELECT value FROM runtime_state WHERE name='main_session'"
        ).fetchone()
        session = self.store.get("session", session_id)
        if (
            row is None
            or row[0] != session_id
            or session is None
            or session.get("kind") != "main"
            or session.get("principal") != "owner"
        ):
            raise PermissionError("只有真实持续主会话可以确认重新规划")

    @staticmethod
    def _state(item: JsonObject) -> JsonObject:
        state = item.get("control")
        if state is None and "control" not in item:
            return {"epoch": 0, "approved_epoch": 0, "replan_request_id": None, "plan": None}
        if (
            not isinstance(state, dict)
            or set(state)
            not in (
                {"epoch", "approved_epoch", "replan_request_id", "plan"},
                {"epoch", "approved_epoch", "replan_request_id", "plan", "scheduled_replan"},
            )
            or type(state.get("epoch")) is not int
            or type(state.get("approved_epoch")) is not int
            or not 0 <= state["approved_epoch"] <= state["epoch"]
            or (
                state["replan_request_id"] is not None
                and not isinstance(state["replan_request_id"], str)
            )
        ):
            raise ControlUnavailable("control_invalid", "持久任务控制字段无效")
        if "scheduled_replan" in state:
            _scheduled_marker(state["scheduled_replan"])
        plan = state["plan"]
        if plan is not None and (
            not isinstance(plan, dict)
            or set(plan) != {"epoch", "text"}
            or type(plan.get("epoch")) is not int
            or not 0 <= plan["epoch"] <= state["approved_epoch"]
            or not isinstance(plan.get("text"), str)
            or not plan["text"].strip()
        ):
            raise ControlUnavailable("control_invalid", "持久重规划方案无效")
        return dict(state)

    def resolve_scope(self, session_id: str) -> str | None:
        """Follow host lineage, including old workflow runs without an explicit binding."""
        return self._resolve_scope(session_id)[0]

    def scope_lineage(
        self, session_id: str, *, max_hops: int, max_bytes: int
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Version only the specified bounded parent chain, without enumerating peers."""
        if type(max_hops) is not int or max_hops < 1:
            raise ControlUnavailable("scope_budget", "执行范围父链预算不足")
        return self._resolve_scope(session_id, max_hops=max_hops, max_bytes=max_bytes)

    def _resolve_scope(
        self, session_id: str, *, max_hops: int | None = None, max_bytes: int | None = None
    ) -> tuple[str | None, list[dict[str, Any]]]:
        current: str | None = session_id
        visited: set[str] = set()
        binding: str | None = None
        frames: list[dict[str, Any]] = []
        while current is not None:
            if current in visited:
                raise ControlUnavailable("scope_invalid", "执行范围父链包含环")
            if max_hops is not None and len(visited) >= max_hops:
                raise ControlUnavailable("scope_budget", "执行范围父链超过核查跳数预算")
            visited.add(current)
            if max_bytes is not None:
                session = self.store.bounded_object("session", current, max_bytes=max_bytes)
                agent = self.store.bounded_object("agent", current, max_bytes=max_bytes)
            else:
                session_row = self.store.db.execute(
                    "SELECT json_extract(data,'$.kind') AS kind,"
                    "json_extract(data,'$.workflow_id') AS workflow_id,"
                    "json_extract(data,'$.workflow_run_id') AS workflow_run_id "
                    "FROM objects WHERE kind='session' AND id=?",
                    (current,),
                ).fetchone()
                session = dict(session_row) if session_row is not None else None
                agent_row = self.store.db.execute(
                    "SELECT json_extract(data,'$.commitment_id') AS commitment_id,"
                    "json_extract(data,'$.parent_id') AS parent_id "
                    "FROM objects WHERE kind='agent' AND id=?",
                    (current,),
                ).fetchone()
                agent = dict(agent_row) if agent_row is not None else None
            if session is None:
                raise ControlUnavailable("scope_invalid", "执行范围父会话缺失")
            frame: dict[str, Any] = {}
            if max_bytes is not None:
                frame = {
                    "session_id": current,
                    "session": _digest(session),
                    "agent": _digest(agent),
                }
            if agent is None and session.get("kind") == "team":
                raise ControlUnavailable("scope_invalid", "团队执行记录缺失")
            if session.get("workflow_id") is not None and not session.get("workflow_run_id"):
                raise ControlUnavailable("scope_invalid", "工作流执行关联缺失")
            parent: str | None = None
            value: object = None
            if agent is not None:
                value, parent = agent.get("commitment_id"), agent.get("parent_id")
            elif session.get("workflow_run_id") is not None:
                workflow_id = session["workflow_run_id"]
                if not isinstance(workflow_id, str) or not workflow_id:
                    raise ControlUnavailable("scope_invalid", "执行范围工作流关联无效")
                if max_bytes is not None:
                    workflow = self.store.bounded_object(
                        "workflow_run", workflow_id, max_bytes=max_bytes
                    )
                else:
                    row = self.store.db.execute(
                        "SELECT json_extract(data,'$.commitment_id') AS commitment_id,"
                        "json_extract(data,'$.parent_session_id') AS parent_session_id "
                        "FROM objects WHERE kind='workflow_run' AND id=?",
                        (workflow_id,),
                    ).fetchone()
                    workflow = dict(row) if row is not None else None
                if workflow is None:
                    raise ControlUnavailable("scope_invalid", "执行范围工作流缺失")
                value, parent = workflow.get("commitment_id"), workflow.get("parent_session_id")
                if max_bytes is not None:
                    frame["workflow"] = _digest(workflow)
            if value is not None:
                if (
                    not isinstance(value, str)
                    or not value
                    or (binding is not None and value != binding)
                ):
                    raise ControlUnavailable("scope_invalid", "执行范围绑定不一致")
                binding = value
            if parent is not None and (not isinstance(parent, str) or not parent):
                raise ControlUnavailable("scope_invalid", "执行范围父会话无效")
            if max_bytes is not None:
                frames.append(frame)
            current = parent
        return binding, frames

    def _workflow_scope(self, identifier: str) -> str | None:
        row = self.store.db.execute(
            "SELECT json_extract(data,'$.commitment_id'),json_extract(data,'$.parent_session_id') "
            "FROM objects WHERE kind='workflow_run' AND id=?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise ControlUnavailable("scope_invalid", "活动工作流记录缺失")
        value, parent = row[0], row[1]
        inherited = self.resolve_scope(parent) if isinstance(parent, str) and parent else None
        if (parent is not None and (not isinstance(parent, str) or not parent)) or (
            value is not None and (not isinstance(value, str) or not value or value != inherited)
        ):
            raise ControlUnavailable("scope_invalid", "活动工作流的范围绑定不一致")
        return inherited

    def scope(self, identifier: str, *, active_workflow_ids: Sequence[str] = ()) -> JsonObject:
        count = self.store.db.execute(
            "WITH RECURSIVE members(id) AS ("
            "SELECT id FROM objects WHERE kind='agent' AND json_extract(data,'$.commitment_id')=? "
            "UNION SELECT child.id FROM objects child JOIN members parent "
            "ON json_extract(child.data,'$.parent_id')=parent.id WHERE child.kind='agent') "
            "SELECT count(*) FROM members",
            (identifier,),
        ).fetchone()[0]
        active = {
            str(row[0])
            for row in self.store.db.execute(
                "SELECT id FROM objects WHERE kind='workflow_run' "
                "AND json_extract(data,'$.status') IN (?,?,?)",
                _WORKFLOW_ACTIVE,
            )
        }
        active.update(active_workflow_ids)
        workflow_count = sum(self._workflow_scope(key) == identifier for key in active)
        return {
            "member_count": int(count),
            "active_workflow_count": workflow_count,
            "can_pause": workflow_count == 0,
            "blocked_reason": "workflow_active" if workflow_count else None,
        }

    def current(self, identifier: str, *, active_workflow_ids: Sequence[str] = ()) -> JsonObject:
        item = self.commitments.get(identifier)
        state = self._state(item)
        return {
            "id": identifier,
            "revision": item["revision"],
            "control": {
                "epoch": state["epoch"],
                "approved_epoch": state["approved_epoch"],
                "replan_request_id": state["replan_request_id"],
                "paused": state["epoch"] > state["approved_epoch"],
                "has_plan": state["plan"] is not None,
                "plan_epoch": state["plan"]["epoch"] if state["plan"] else None,
            },
            "scope": self.scope(identifier, active_workflow_ids=active_workflow_ids),
        }

    def _snapshot(self, session_id: str, item: JsonObject) -> TaskSnapshot:
        state = self._state(item)
        if "brief" not in item:
            raise ControlUnavailable("brief_missing", "任务没有完整说明")
        brief = self.commitments._validate_brief(item["brief"], stored=True)
        return TaskSnapshot(
            session_id,
            str(item["id"]),
            int(item["revision"]),
            state["epoch"],
            state["approved_epoch"],
            state["replan_request_id"],
            _json(brief),
            _json(state["plan"]),
        )

    def _budget(self, snapshot: TaskSnapshot) -> None:
        size = context_size([Message(role="system", content=snapshot.context())])
        if size > self.commitments.max_context_chars():
            raise ContextLimitError("完整受控任务说明和方案超过上下文字符预算")

    def execution_snapshot(self, session_id: str) -> TaskSnapshot | None:
        identifier = self.resolve_scope(session_id)
        if identifier is None:
            return None
        snapshot = self._snapshot(session_id, self.commitments.get(identifier))
        self._budget(snapshot)
        return snapshot

    def replan_snapshot(self, main_session_id: str, identifier: str) -> TaskSnapshot:
        self._main(main_session_id)
        snapshot = self._snapshot(main_session_id, self.commitments.get(identifier))
        self._validate_replan(snapshot)
        self._budget(snapshot)
        return snapshot

    def _validate_replan(self, snapshot: TaskSnapshot) -> JsonObject:
        self._main(snapshot.session_id)
        item = self.commitments.get(snapshot.commitment_id)
        state = self._state(item)
        if (
            state["epoch"] != snapshot.epoch
            or state["approved_epoch"] >= state["epoch"]
            or not snapshot.replan_request_id
            or state["replan_request_id"] != snapshot.replan_request_id
            or _json(self.commitments._validate_brief(item.get("brief"), stored=True))
            != snapshot.brief_json
        ):
            raise ControlConflict("replan_stale", "重规划快照已失效或没有主人重规划请求")
        receipt = self.store.get("task_control_receipt", snapshot.replan_request_id)
        if (
            receipt is None
            or receipt.get("action") != "request_replan"
            or receipt.get("actor") != "owner"
            or receipt.get("commitment_id") != snapshot.commitment_id
            or receipt.get("epoch") != snapshot.epoch
        ):
            raise ControlUnavailable("receipt_invalid", "缺少可核验的主人重规划回执")
        if state.get("scheduled_replan") != receipt.get("scheduled_replan"):
            raise ControlConflict("replan_stale", "定时任务控制与主人回执授权不一致")
        self._guard_scheduled_replan(receipt)
        return item

    def _guard_scheduled_replan(
        self, receipt: JsonObject, targets: Sequence[str] | None = None
    ) -> None:
        if "scheduled_replan" not in receipt:
            request_id = receipt.get("request_id")
            prefix = "scheduled-replan:"
            if isinstance(request_id, str) and request_id.startswith(prefix):
                occurrence = self.store.bounded_object(
                    "task_replan_occurrence",
                    request_id[len(prefix) :],
                    max_bytes=(
                        self.commitments.max_context_chars() + self.commitments.max_output_chars()
                    )
                    * 4,
                )
                if occurrence is not None and occurrence.get("owner_receipt_id") == receipt.get(
                    "id"
                ):
                    raise ControlUnavailable("schedule_invalid", "定时主人回执缺少其原始授权证明")
            return
        _scheduled_marker(receipt["scheduled_replan"])
        if self.scheduled_replan_guard is None:
            raise ControlUnavailable("schedule_unavailable", "定时重规划授权核验未接入")
        self.scheduled_replan_guard(receipt, targets)

    def is_current_replan(self, snapshot: TaskSnapshot) -> bool:
        """False means the host must clear its temporary selected-replan state."""
        try:
            self._validate_replan(snapshot)
        except (KeyError, ValueError, PermissionError):
            return False
        return True

    def guard_execution(self, snapshot: TaskSnapshot | None, *, session_id: str, risk: str) -> None:
        identifier = self.resolve_scope(session_id)
        if identifier is None:
            if snapshot is not None:
                raise ControlConflict("scope_mismatch", "主协调选中任务不能当作执行绑定")
            return
        if (
            snapshot is None
            or snapshot.session_id != session_id
            or snapshot.commitment_id != identifier
        ):
            raise ControlConflict("scope_mismatch", "工具请求缺少真实执行范围快照")
        state = self._state(self.commitments.get(identifier))
        if risk != "read" and (
            snapshot.epoch != state["epoch"]
            or state["epoch"] > state["approved_epoch"]
            or snapshot.approved_epoch != snapshot.epoch
        ):
            raise TaskPaused(
                "task_paused",
                "任务已暂停或工具属于旧计划，请在新上下文重新规划",
                observed_epoch=snapshot.epoch,
            )

    @staticmethod
    def _receipt_id(identifier: str, actor: str, request_id: str) -> str:
        return _digest([identifier, actor, request_id])

    def _duplicate(
        self, key: str, fingerprint: str, *, active_workflow_ids: Sequence[str] = ()
    ) -> ControlResult | None:
        previous = self.store.get("task_control_receipt", key)
        if previous is None:
            return None
        if previous.get("fingerprint") != fingerprint:
            raise ControlConflict("request_id_conflict", "相同控制 ID 的内容不同")
        return ControlResult(
            previous,
            self.current(previous["commitment_id"], active_workflow_ids=active_workflow_ids),
            False,
        )

    def _write(self, item: JsonObject, receipt: JsonObject) -> None:
        self.store._put("commitment", item)
        self.store._put(
            "commitment_revision", {**item, "id": new_id(), "commitment_id": item["id"]}
        )
        self.store._put("task_control_receipt", receipt)
        state = self._state(item)
        self.store._event(
            "commitment.updated",
            {
                "id": item["id"],
                "revision": item["revision"],
                "epoch": state["epoch"],
                "approved_epoch": state["approved_epoch"],
                "action": receipt["action"],
                "control_changed": True,
            },
        )

    def owner_control(
        self,
        identifier: str,
        request: OwnerControlRequest,
        *,
        main_session_id: str,
        mode: str,
        model_configured: bool,
        max_pending: int,
        active_workflow_ids: Sequence[str] = (),
        expected_approved_epoch: int | None = None,
        reply_origin: ReplyOrigin | None = None,
    ) -> ControlResult:
        """Only the authenticated owner endpoint may call this method."""
        with self._transaction():
            return self._owner_control_in_transaction(
                identifier,
                request,
                main_session_id=main_session_id,
                mode=mode,
                model_configured=model_configured,
                max_pending=max_pending,
                active_workflow_ids=active_workflow_ids,
                expected_approved_epoch=expected_approved_epoch,
                reply_origin=reply_origin,
            )

    def _owner_control_in_transaction(
        self,
        identifier: str,
        request: OwnerControlRequest,
        *,
        main_session_id: str,
        mode: str,
        model_configured: bool,
        max_pending: int,
        active_workflow_ids: Sequence[str] = (),
        expected_approved_epoch: int | None = None,
        reply_origin: ReplyOrigin | None = None,
        scheduled_replan: JsonObject | None = None,
    ) -> ControlResult:
        """Stage the ordinary owner control while the host owns its one transaction."""
        if not self.store.db.in_transaction:
            raise RuntimeError("任务控制同步体需要调用方事务")
        if scheduled_replan is not None:
            scheduled_replan = _scheduled_marker(scheduled_replan)
        if expected_approved_epoch is not None and (
            type(expected_approved_epoch) is not int
            or not 0 <= expected_approved_epoch <= request.expected_epoch
        ):
            raise ValueError("expected_approved_epoch 必须是有效控制快照")
        self._main(main_session_id)
        key = self._receipt_id(identifier, "owner", request.request_id)
        fields = {"commitment_id": identifier, **asdict(request)}
        # Keep historical fingerprints unchanged. The authenticated mobile event
        # already binds its source; duplicate receipts never retrofit that source.
        if expected_approved_epoch is not None:
            fields["expected_approved_epoch"] = expected_approved_epoch
        if scheduled_replan is not None:
            fields["scheduled_replan"] = scheduled_replan
        fingerprint = _digest(fields)
        duplicate = self._duplicate(key, fingerprint, active_workflow_ids=active_workflow_ids)
        if duplicate is not None:
            return duplicate
        item = self.commitments.get(identifier)
        state = self._state(item)
        if state["epoch"] != request.expected_epoch or (
            expected_approved_epoch is not None
            and state["approved_epoch"] != expected_approved_epoch
        ):
            raise ControlConflict("epoch_conflict", "任务控制版本已改变，请读取当前状态")
        if (
            request.action == "pause"
            and self.scope(identifier, active_workflow_ids=active_workflow_ids)[
                "active_workflow_count"
            ]
        ):
            raise ControlConflict("workflow_active", "该任务含活动工作流，请先停止或等待结束")
        if request.action == "request_replan":
            if state["epoch"] <= state["approved_epoch"]:
                raise ControlConflict("not_paused", "请先暂停任务，再交主助手重新规划")
            if not model_configured:
                raise ControlUnavailable("model_unconfigured", "尚未配置主模型，任务保持暂停")
            _positive(max_pending, "入站容量")
            if mode not in {"ask", "auto", "yolo"}:
                raise ValueError("执行模式无效")
            brief = self.commitments._validate_brief(item.get("brief"), stored=True)
            if request.additional_requirements.strip():
                brief["constraints"].append(request.additional_requirements)
            item["brief"] = brief
        state = {
            **state,
            "epoch": state["epoch"] + 1,
            "replan_request_id": key if request.action == "request_replan" else None,
        }
        if scheduled_replan is None:
            state.pop("scheduled_replan", None)
        else:
            state["scheduled_replan"] = dict(scheduled_replan)
        item.update(control=state, revision=item["revision"] + 1, updated_at=now())
        # A pure pause must work even after a context budget reduction.
        if request.action == "request_replan":
            self._budget(self._snapshot(main_session_id, item))
        elif not isinstance(item.get("brief"), dict):
            raise ControlUnavailable("brief_missing", "任务没有完整说明")
        receipt: JsonObject = {
            "id": key,
            "commitment_id": identifier,
            "request_id": request.request_id,
            "fingerprint": fingerprint,
            "action": request.action,
            "actor": "owner",
            "epoch": state["epoch"],
            "revision": item["revision"],
            "created_at": now(),
        }
        if scheduled_replan is not None:
            receipt["scheduled_replan"] = dict(scheduled_replan)
        ingress: JsonObject | None = None
        if request.action == "request_replan":
            payload = Conversation.payload(
                main_session_id,
                f"主人通过任务控制入口要求重新规划任务 {identifier}（epoch {state['epoch']}）。"
                "先用 commitment_read 读取并选择当前完整说明，再基于下一轮完整快照确认方案；任务仍暂停。",
                source="task-control",
                principal="owner",
                mode=mode,
                event_id=key,
                reply_origin=reply_origin,
            )
            payload["task_control"] = {
                "commitment_id": identifier,
                "receipt_id": key,
                "epoch": state["epoch"],
            }
            if self.store.pending_inputs(main_session_id) >= max_pending:
                raise ControlUnavailable("queue_full", "主会话入站队列已满，任务保持暂停")
            ingress, _ = self.store._enqueue_input(
                payload, max_pending=max_pending, apply_objects_on_duplicate=False
            )
            receipt["ingress_id"] = ingress["id"]
        self._write(item, receipt)
        return ControlResult(
            receipt,
            self.current(identifier, active_workflow_ids=active_workflow_ids),
            True,
            ingress,
        )

    def approve_plan(
        self,
        snapshot: TaskSnapshot,
        observed_epoch: int,
        plan: str,
        targets: Sequence[str],
        *,
        caller_session_id: str,
        max_targets: int,
    ) -> ControlResult:
        self._main(caller_session_id)
        if snapshot.session_id != caller_session_id or not snapshot.replan_request_id:
            raise PermissionError("缺少本次主模型请求的重规划快照")
        if type(observed_epoch) is not int or observed_epoch != snapshot.epoch:
            raise ControlConflict("snapshot_mismatch", "模型参数不能替换宿主观察到的 epoch")
        if not isinstance(plan, str) or not plan.strip():
            raise ValueError("方案不能为空")
        if isinstance(targets, (str, bytes)) or any(
            not isinstance(target, str) or not target for target in targets
        ):
            raise ValueError("方案通知目标无效")
        normalized = sorted(set(targets))
        key = self._receipt_id(snapshot.commitment_id, "main", snapshot.replan_request_id)
        fingerprint = _digest({"epoch": observed_epoch, "plan": plan, "targets": normalized})
        with self._transaction():
            duplicate = self._duplicate(key, fingerprint)
            if duplicate is not None:
                return duplicate
            _positive(max_targets, "通知目标预算")
            if len(plan) > self.commitments.max_output_chars():
                raise ValueError("方案超过配置输出预算")
            if len(normalized) > max_targets:
                raise ControlUnavailable("targets_full", "方案通知目标超过配置预算")
            item = self._validate_replan(snapshot)
            owner_receipt = self.store.get("task_control_receipt", snapshot.replan_request_id)
            assert owner_receipt is not None
            self._guard_scheduled_replan(owner_receipt, normalized)
            report_origin = self._owner_report_origin(
                snapshot.replan_request_id,
                snapshot.commitment_id,
                snapshot.epoch,
                caller_session_id,
            )
            deliveries: list[JsonObject] = []
            for target in normalized:
                agent = self.store.get("agent", target)
                if (
                    agent is None
                    or self.resolve_scope(target) != snapshot.commitment_id
                    or agent.get("status") not in _DELIVERY_TARGETS
                ):
                    raise ControlConflict("target_invalid", "通知目标不是当前任务的未完成队友")
                deliveries.append(
                    {
                        "id": _digest([key, target]),
                        "commitment_id": snapshot.commitment_id,
                        "epoch": snapshot.epoch,
                        "target_session_id": target,
                        "target_generation": agent.get("generation", 0),
                        "target_completion_revision": agent.get("completion_revision", 0),
                        "expected_status": agent["status"],
                        "state": "pending",
                        "created_at": now(),
                        **(
                            {"scheduled_owner_receipt_id": snapshot.replan_request_id}
                            if "scheduled_replan" in owner_receipt
                            else {}
                        ),
                        **(
                            {
                                "report_source": {
                                    "version": 1,
                                    "owner_receipt_id": snapshot.replan_request_id,
                                    "reply_origin": report_origin.dump(),
                                }
                            }
                            if report_origin is not None
                            and agent.get("parent_id") == caller_session_id
                            else {}
                        ),
                    }
                )
            state = self._state(item)
            item.update(
                control={
                    **state,
                    "approved_epoch": snapshot.epoch,
                    "plan": {"epoch": snapshot.epoch, "text": plan},
                },
                revision=item["revision"] + 1,
                updated_at=now(),
            )
            self._budget(self._snapshot(caller_session_id, item))
            receipt = {
                "id": key,
                "commitment_id": snapshot.commitment_id,
                "request_id": snapshot.replan_request_id,
                "fingerprint": fingerprint,
                "action": "approve_plan",
                "actor": "main",
                "epoch": snapshot.epoch,
                "revision": item["revision"],
                "created_at": now(),
                "target_count": len(deliveries),
            }
            if "scheduled_replan" in owner_receipt:
                marker = owner_receipt["scheduled_replan"]
                occurrence = self.store.get("task_replan_occurrence", marker["occurrence_id"])
                if occurrence is None:
                    raise ControlUnavailable("schedule_invalid", "定时接纳事实不存在")
                self.store._put(
                    "task_replan_occurrence",
                    {
                        **occurrence,
                        "state": "approved",
                        "approve_receipt_id": key,
                        "delivery_id": deliveries[0]["id"],
                        "updated_at": now(),
                    },
                )
                scheduled = self.store.get("task_replan_schedule", marker["schedule_id"])
                if scheduled is None:
                    raise ControlUnavailable("schedule_invalid", "定时约定不存在")
                self.store._put(
                    "task_replan_schedule", {**scheduled, "state": "approved", "updated_at": now()}
                )
            self._write(item, receipt)
            for delivery in deliveries:
                self.store._put("task_plan_delivery", delivery)
            return ControlResult(receipt, self.current(snapshot.commitment_id), True)

    def _owner_report_origin(
        self, receipt_id: str, commitment_id: str, epoch: int, main_id: str
    ) -> ReplyOrigin | None:
        """Read the original authenticated control input, never today's chat destination."""
        try:
            self._main(main_id)
            receipt = self.store.get("task_control_receipt", receipt_id)
            if (
                receipt is None
                or receipt.get("action") != "request_replan"
                or receipt.get("actor") != "owner"
                or not isinstance(receipt.get("request_id"), str)
                or self._receipt_id(commitment_id, "owner", receipt["request_id"]) != receipt_id
                or receipt.get("commitment_id") != commitment_id
                or type(receipt.get("epoch")) is not int
                or receipt.get("epoch") != epoch
            ):
                return None
            row = self.store.db.execute(
                "SELECT * FROM ingress WHERE id=? AND session_id=?",
                (receipt.get("ingress_id"), main_id),
            ).fetchone()
            if row is None or row["state"] != "delivered":
                return None
            payload = json.loads(row["payload"])
            message = self.store.get("message", row["id"])
            reference = {"commitment_id": commitment_id, "receipt_id": receipt_id, "epoch": epoch}
            if (
                not isinstance(payload, dict)
                or message is None
                or payload.get("id") != row["id"]
                or any(
                    record.get("session_id") != main_id
                    or record.get("source") != "task-control"
                    or record.get("principal") != "owner"
                    or record.get("route") is not None
                    or record.get("task_control") != reference
                    for record in (payload, message)
                )
                or message.get("role") != "user"
                or message.get("run_id") != row["run_id"]
                or message.get("input_seq") != row["seq"]
                or message.get("seq") != row["delivered_seq"]
                or message.get("reply_origin") != payload.get("reply_origin")
            ):
                return None
            origin = ReplyOrigin.load(payload.get("reply_origin"))
            routes = origin.delivery_routes()
            if (
                origin.kind != "channel"
                or origin.dump() != payload.get("reply_origin")
                or len(routes) != 1
                or routes[0].get("kind") != "private"
                or routes[0].get("sender_id") != routes[0].get("target")
            ):
                return None
            return origin
        except (KeyError, TypeError, ValueError, PermissionError):
            return None  # Legacy or damaged source facts never acquire a destination retroactively.

    def _report_candidate(
        self, agent: JsonObject, run_id: str, notice: JsonObject
    ) -> JsonObject | None:
        """Prove one actual notice belongs to this execution, including the parked archive path."""
        try:
            self._main(agent["parent_id"])
            reference = notice.get("task_control")
            run = self.store.get("run", run_id)
            if (
                agent.get("status") != "running"
                or agent.get("run_id") != run_id
                or run is None
                or run.get("session_id") != agent["id"]
                or run.get("status") not in {"running", "completed"}
                or not isinstance(reference, dict)
                or set(reference) != {"commitment_id", "epoch", "delivery_id"}
                or not isinstance(reference.get("delivery_id"), str)
                or type(reference.get("epoch")) is not int
                or notice.get("session_id") != agent["id"]
                or notice.get("source") != "task-control"
                or notice.get("principal") != "system:task_control"
                or notice.get("route") is not None
                or notice.get("role") != "user"
                or notice.get("delivery_status") != "delivered"
                or self.resolve_scope(agent["id"]) != reference["commitment_id"]
            ):
                return None
            delivery = self.store.get("task_plan_delivery", reference["delivery_id"])
            if (
                delivery is None
                or delivery.get("state") != "delivered"
                or delivery.get("target_session_id") != agent["id"]
                or delivery.get("commitment_id") != reference["commitment_id"]
                or type(delivery.get("epoch")) is not int
                or delivery.get("epoch") != reference["epoch"]
            ):
                return None
            source = delivery.get("report_source")
            if (
                not isinstance(source, dict)
                or set(source) != {"version", "owner_receipt_id", "reply_origin"}
                or type(source.get("version")) is not int
                or source["version"] != 1
                or not isinstance(source.get("owner_receipt_id"), str)
            ):
                return None
            receipt_id = self._receipt_id(
                delivery["commitment_id"], "main", source["owner_receipt_id"]
            )
            receipt = self.store.get("task_control_receipt", receipt_id)
            if (
                delivery["id"] != _digest([receipt_id, agent["id"]])
                or receipt is None
                or receipt.get("action") != "approve_plan"
                or receipt.get("actor") != "main"
                or receipt.get("request_id") != source["owner_receipt_id"]
                or receipt.get("commitment_id") != delivery["commitment_id"]
                or receipt.get("epoch") != delivery["epoch"]
            ):
                return None
            origin = self._owner_report_origin(
                source["owner_receipt_id"],
                delivery["commitment_id"],
                delivery["epoch"],
                agent["parent_id"],
            )
            if origin is None or origin.dump() != source["reply_origin"]:
                return None
            generation, revision = agent.get("generation"), agent.get("completion_revision", 0)
            target_generation = delivery.get("target_generation")
            target_revision = delivery.get("target_completion_revision")
            if (
                type(generation) is not int
                or type(revision) is not int
                or type(target_generation) is not int
                or type(target_revision) is not int
                or min(generation, revision, target_generation, target_revision) < 0
            ):
                return None
            parked = False
            if generation == target_generation + 1 and revision == target_revision + 1:
                rows = self.store.db.execute(
                    "SELECT data FROM events WHERE type='team.finished' AND session_id=? "
                    "AND json_extract(data,'$.completion_revision')=? LIMIT 2",
                    (agent["id"], revision),
                ).fetchall()
                if len(rows) == 1:
                    terminal = json.loads(rows[0][0])
                    parked = (
                        terminal.get("status") == "paused"
                        and terminal.get("generation") == target_generation
                        and delivery.get("expected_status") in {"running", "queued"}
                    )
            if delivery.get("path") == "archive":
                if (
                    notice["id"] != delivery.get("archive_message_id")
                    or notice.get("run_id") is not None
                    or generation != target_generation + 1
                    or not (
                        (
                            delivery.get("expected_status") == "paused"
                            and revision == target_revision
                        )
                        or parked
                    )
                ):
                    return None
            elif delivery.get("path") == "ingress":
                row = self.store.db.execute(
                    "SELECT * FROM ingress WHERE id=? AND session_id=?",
                    (delivery.get("ingress_id"), agent["id"]),
                ).fetchone()
                if (
                    row is None
                    or row["state"] != "delivered"
                    or row["id"] != notice["id"]
                    or row["run_id"] != run_id
                    or notice.get("run_id") != run_id
                    or notice.get("input_seq") != row["seq"]
                    or notice.get("seq") != row["delivered_seq"]
                ):
                    return None
                payload = json.loads(row["payload"])
                if not isinstance(payload, dict) or any(
                    payload.get(key) != notice.get(key)
                    for key in ("id", "session_id", "source", "principal", "route", "task_control")
                ):
                    return None
                same_running = (
                    delivery.get("expected_status") == "running"
                    and generation == target_generation
                    and revision == target_revision
                )
                next_queued = (
                    delivery.get("expected_status") == "queued"
                    and generation == target_generation + 1
                    and revision == target_revision
                )
                if not (same_running or next_queued or parked):
                    return None
            else:
                return None
            return {
                "version": 1,
                "delivery_id": delivery["id"],
                "notice_id": notice["id"],
                "run_id": run_id,
                "generation": generation,
                "completion_revision": revision + 1,
                "reply_origin": origin.dump(),
            }
        except (KeyError, TypeError, ValueError, PermissionError):
            return None

    def bind_report_notice(
        self,
        session_id: str,
        run_id: str,
        snapshot: TaskSnapshot | None,
        projected: Sequence[JsonObject],
        messages: Sequence[Message],
    ) -> None:
        """Only notices actually retained in this request can bind its own completion report."""
        agent = self.store.get("agent", session_id)
        if agent is None or snapshot is None or snapshot.epoch != snapshot.approved_epoch:
            return
        retained = {
            (message.source_message_id, message.content)
            for message in messages
            if message.role == "user" and message.source_message_id is not None
        }
        candidates: dict[str, JsonObject] = {}
        for notice in projected:
            reference = notice.get("task_control")
            if (
                notice.get("principal") != "system:task_control"
                or (notice.get("id"), notice.get("content")) not in retained
                or not isinstance(reference, dict)
                or reference.get("commitment_id") != snapshot.commitment_id
                or reference.get("epoch") != snapshot.epoch
            ):
                continue
            candidate = self._report_candidate(agent, run_id, notice)
            if candidate is not None:
                candidates[candidate["delivery_id"]] = candidate
        if not candidates:
            return
        existing = agent.get("report_binding")
        candidate = next(iter(candidates.values()))
        if len(candidates) != 1 or (existing is not None and existing != candidate):
            candidate = {"version": 1, "run_id": run_id, "ambiguous": True}
        if existing != candidate:
            self.store.put("agent", {**agent, "report_binding": candidate})

    def completed_report_binding(self, agent: JsonObject, run_id: str) -> JsonObject | None:
        """Recheck the frozen exact execution before Team commits its terminal fact and report."""
        binding = agent.get("report_binding")
        if (
            not isinstance(binding, dict)
            or set(binding)
            != {
                "version",
                "delivery_id",
                "notice_id",
                "run_id",
                "generation",
                "completion_revision",
                "reply_origin",
            }
            or type(binding.get("version")) is not int
            or binding["version"] != 1
            or not isinstance(binding.get("notice_id"), str)
            or not isinstance(binding.get("delivery_id"), str)
            or type(binding.get("generation")) is not int
            or type(binding.get("completion_revision")) is not int
            or binding.get("run_id") != run_id
        ):
            return None
        notice = self.store.get("message", binding["notice_id"])
        if notice is None:
            return None
        return binding if self._report_candidate(agent, run_id, notice) == binding else None

    def pending_deliveries(self, limit: int, *, after_id: str | None = None) -> list[JsonObject]:
        _positive(limit, "投递批次")
        return [
            json.loads(row[0])
            for row in self.store.db.execute(
                "SELECT data FROM objects WHERE kind='task_plan_delivery' "
                "AND json_extract(data,'$.state')='pending' AND id>? ORDER BY id LIMIT ?",
                (after_id or "", limit),
            )
        ]

    def _same_delivery_target(self, delivery: JsonObject, agent: JsonObject) -> bool:
        if agent.get("generation", 0) != delivery["target_generation"]:
            return False
        revision = agent.get("completion_revision", 0)
        expected = delivery["target_completion_revision"]
        if revision == expected and agent.get("status") == delivery["expected_status"]:
            return True
        # Only the unique next terminal event proves a normal parked transition.
        # Cancellation consumes that revision, even if queued again before start.
        if (
            delivery["expected_status"] not in {"running", "queued"}
            or agent.get("status") != "paused"
            or revision != expected + 1
        ):
            return False
        row = self.store.db.execute(
            "SELECT json_extract(data,'$.status'),json_extract(data,'$.generation') "
            "FROM events WHERE type='team.finished' AND session_id=? "
            "AND json_extract(data,'$.completion_revision')=?",
            (delivery["target_session_id"], revision),
        ).fetchone()
        return row is not None and row[0] == "paused" and row[1] == delivery["target_generation"]

    def resume_notified_target(self, target_session_id: str) -> bool:
        """Prove a delivered plan can resume this exact generation, without writing.

        The host normally calls this with a paused outcome while the stored agent
        is still running/queued, then immediately commits one paused completion
        event plus a queued agent. No await may separate proof and that write.
        A proven already-parked target is also readable; this method never adds a
        completion revision or creates another message.
        """
        agent = self.store.get("agent", target_session_id)
        if agent is None or agent.get("status") not in _DELIVERY_TARGETS:
            return False
        try:
            scope = self.resolve_scope(target_session_id)
            if scope is None:
                return False
            state = self._state(self.commitments.get(scope))
            if (
                state["epoch"] != state["approved_epoch"]
                or not state["replan_request_id"]
                or state["plan"] is None
                or state["plan"]["epoch"] != state["epoch"]
            ):
                return False
            receipt_id = self._receipt_id(scope, "main", state["replan_request_id"])
            receipt = self.store.get("task_control_receipt", receipt_id)
            delivery_id = _digest([receipt_id, target_session_id])
            delivery = self.store.get("task_plan_delivery", delivery_id)
            if (
                receipt is None
                or receipt.get("action") != "approve_plan"
                or receipt.get("actor") != "main"
                or receipt.get("commitment_id") != scope
                or receipt.get("epoch") != state["epoch"]
                or delivery is None
                or delivery.get("state") != "delivered"
                or delivery.get("path") != "ingress"
                or delivery.get("target_session_id") != target_session_id
                or delivery.get("commitment_id") != scope
                or delivery.get("epoch") != state["epoch"]
                or not self._same_delivery_target(delivery, agent)
            ):
                return False
            row = self.store.db.execute(
                "SELECT * FROM ingress WHERE id=? AND session_id=?",
                (delivery.get("ingress_id"), target_session_id),
            ).fetchone()
            if row is None or row["state"] not in {"pending", "delivered"}:
                return False
            reference = {
                "commitment_id": scope,
                "epoch": state["epoch"],
                "delivery_id": delivery_id,
            }

            def trusted(record: object) -> bool:
                return (
                    isinstance(record, dict)
                    and record.get("id") == row["id"]
                    and record.get("session_id") == target_session_id
                    and record.get("source") == "task-control"
                    and record.get("principal") == "system:task_control"
                    and record.get("route") is None
                    and record.get("task_control") == reference
                )

            if not trusted(json.loads(row["payload"])):
                return False
            if row["state"] == "pending":
                return True
            message = self.store.get("message", row["id"])
            return (
                message is not None
                and trusted(message)
                and message.get("role") == "user"
                and message.get("delivery_status") == "delivered"
                and message.get("input_seq") == row["seq"]
                and message.get("seq") == row["delivered_seq"]
                and message.get("run_id") == row["run_id"]
            )
        except (KeyError, ValueError):
            # Corrupt/missing lineage or proof must never revive a parked agent.
            return False

    def deliver(self, identifier: str, *, max_pending: int) -> DeliveryResult:
        _positive(max_pending, "入站容量")
        with self._transaction():
            delivery = self.store.get("task_plan_delivery", identifier)
            if delivery is None:
                raise KeyError(identifier)
            target = str(delivery["target_session_id"])
            if delivery["state"] != "pending":
                if delivery["state"] not in {"delivered", "superseded"}:
                    raise ControlUnavailable("delivery_invalid", "方案投递状态无效")
                return DeliveryResult(delivery["state"], target)
            item = self.commitments.get(delivery["commitment_id"])
            state = self._state(item)
            agent = self.store.get("agent", target)
            scheduled_receipt = delivery.get("scheduled_owner_receipt_id")
            if scheduled_receipt is not None:
                owner_receipt = self.store.get("task_control_receipt", scheduled_receipt)
                if owner_receipt is None or "scheduled_replan" not in owner_receipt:
                    raise ControlUnavailable("schedule_invalid", "方案缺少定时主人回执")
                try:
                    self._guard_scheduled_replan(owner_receipt, [target])
                except (ValueError, PermissionError):
                    self.store._put(
                        "task_plan_delivery",
                        {**delivery, "state": "superseded", "updated_at": now()},
                    )
                    return DeliveryResult("superseded", target, changed=True)
            matches = (
                agent is not None
                and state["epoch"] == state["approved_epoch"] == delivery["epoch"]
                and self.resolve_scope(target) == delivery["commitment_id"]
                and self._same_delivery_target(delivery, agent)
                and agent.get("status") in _DELIVERY_TARGETS
            )
            if not matches:
                self.store._put(
                    "task_plan_delivery", {**delivery, "state": "superseded", "updated_at": now()}
                )
                return DeliveryResult("superseded", target, changed=True)
            assert agent is not None
            if agent["status"] != "paused" and self.store.pending_inputs(target) >= max_pending:
                return DeliveryResult("pending", target)
            plan = state["plan"]
            if plan is None or plan["epoch"] != delivery["epoch"]:
                raise ControlUnavailable("delivery_invalid", "方案投递缺少对应的持久方案")
            payload = Conversation.payload(
                target,
                f"任务 {item['id']} 的 epoch {delivery['epoch']} 方案已获主助手确认。"
                "这是既有主人要求内的内部通知，不是新授权；使用下一轮完整上下文，先核查未知副作用。",
                source="task-control",
                principal="system:task_control",
                mode=agent["mode"],
                event_id=identifier,
            )
            payload["task_control"] = {
                "commitment_id": item["id"],
                "epoch": delivery["epoch"],
                "delivery_id": identifier,
            }
            ingress: JsonObject | None = None
            message: JsonObject | None = None
            path: Literal["ingress", "archive"]
            wake = agent["status"] == "paused"
            if wake:
                # A parked generation has already paired every native call. Its
                # full pending inbox cannot drain until it is resumed, so archive
                # this one durable control fact rather than expanding its queue.
                message = self.store._archive_input(payload, received=now(), run_id=None)
                path = "archive"
                delivery["archive_message_id"] = message["id"]
                self.store._put("agent", {**agent, "status": "queued", "updated_at": now()})
            else:
                ingress, _ = self.store._enqueue_input(
                    payload, max_pending=max_pending, apply_objects_on_duplicate=False
                )
                path = "ingress"
                delivery["ingress_id"] = ingress["id"]
            self.store._put(
                "task_plan_delivery",
                {**delivery, "state": "delivered", "path": path, "updated_at": now()},
            )
            self.store._event(
                "task.plan.delivered",
                {
                    "id": identifier,
                    "commitment_id": item["id"],
                    "epoch": delivery["epoch"],
                    "target_session_id": target,
                    "path": path,
                },
            )
            return DeliveryResult(
                "delivered",
                target,
                changed=True,
                wake=wake,
                ingress=ingress,
                message=message,
                path=path,
            )
