"""Accessibility-first desktop control with fresh snapshot identity checks."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from muli.capture import Capture
from muli.config import Settings
from muli.process import run_process
from muli.store import new_id
from muli.tools import Tool, parameters, string


class ComputerError(ValueError):
    def __init__(self, code: str, message: str, outcome: str = "not_started") -> None:
        self.code, self.message, self.outcome = code, message, outcome
        super().__init__(message)

    def as_result(self) -> dict[str, Any]:
        result = {
            "error": self.message,
            "message": self.message,
            "code": self.code,
            "outcome": self.outcome,
        }
        # Keep structured failures intact even at the minimum tool-output budget.
        if len(json.dumps(result, ensure_ascii=False)) > 1000:
            low, high = 0, len(self.message)
            while low < high:
                middle = (low + high + 1) // 2
                text = self.message[:middle] + "…"
                candidate = {**result, "error": text, "message": text}
                if len(json.dumps(candidate, ensure_ascii=False)) <= 1000:
                    low = middle
                else:
                    high = middle - 1
            result.update(error=self.message[:low] + "…", message=self.message[:low] + "…")
        return result


@dataclass(frozen=True)
class _Target:
    kind: str
    node: dict[str, Any]


_FLAGS = ("enabled", "sensitive", "defunct", "protected", "focused", "active", "read_only")
_OMISSIONS = frozenset(
    {"node_budget", "depth_limit", "children_limit", "provider_error", "output_budget"}
)
_HELPER_RESPONSE_BYTES = 250000
_HELPER_RESPONSE_CHARS = 60000  # At most 240001 UTF-8 bytes including the final newline.
_HELPER_ERRORS = frozenset(
    {
        "dependency_missing",
        "no_desktop",
        "target_invalid",
        "target_changed",
        "node_budget",
        "protocol_error",
        "element_disabled",
        "element_unavailable",
        "protected_element",
        "unsupported_operation",
        "action_unknown_outcome",
    }
)


class Computer:
    def __init__(
        self,
        settings: Settings,
        *,
        capture: Capture | None = None,
        owner: Callable[[], tuple[str, str] | None] | None = None,
        dispatch_guard: Callable[[], Callable[[], None]] | None = None,
    ) -> None:
        self.settings = settings
        self.config = settings.computer
        self.capture = capture
        self.snapshot_id = ""
        self.snapshot_time = 0.0
        self.nodes: dict[str, dict[str, Any]] = {}
        self.discovery_id = ""
        self.discovery_time = 0.0
        self._targets: dict[str, _Target] = {}
        self._snapshot_generation = 0
        self._discovery_generation = 0
        self._lock = asyncio.Lock()
        self._owner = owner or (lambda: None)
        self._dispatch_guard = dispatch_guard
        self._atspi_owner: tuple[str, str] | None = None
        self._atspi_generation = 0
        self._atspi_deadline = 0.0
        self._atspi_closed = False
        self.backend = (
            {"linux": "atspi", "darwin": "ax", "win32": "uia"}.get(sys.platform, "unavailable")
            if self.config.backend == "auto"
            else self.config.backend
        )
        # AX refs live inside one short-lived native process. Importing its host
        # adapter never loads macOS libraries or starts that process.
        from muli.tools.ax_computer import AXComputer
        from muli.tools.uia_computer import UIAComputer

        self._ax = AXComputer(self, owner=owner, dispatch_guard=dispatch_guard)
        self._uia = UIAComputer(self, owner=owner, dispatch_guard=dispatch_guard)

    def _atspi_available(self) -> None:
        if not self.config.enabled:
            raise ComputerError("disabled", "电脑操作尚未启用")
        if self.backend != "atspi" or sys.platform != "linux":
            raise ComputerError("backend_unavailable", "当前平台无法使用所选无障碍后端")
        if self._atspi_closed:
            raise ComputerError("backend_closed", "电脑后端已关闭")

    def _retire_atspi(self) -> None:
        self._atspi_generation += 1
        self._atspi_owner, self._atspi_deadline = None, 0.0
        self._clear_snapshot()
        self._discovery_generation += 1
        self.discovery_id, self.discovery_time, self._targets = "", 0.0, {}

    def _atspi_caller(self, *, claim: bool = False, targeted: bool = False) -> tuple[str, str]:
        self._atspi_available()
        owner = self._owner()
        if owner is None:
            raise ComputerError("caller_unavailable", "无障碍引用需要真实执行会话")
        now = time.monotonic()
        if self._atspi_owner is not None and now >= self._atspi_deadline:
            previous_owner = self._atspi_owner
            self._retire_atspi()
            if targeted and previous_owner == owner:
                raise ComputerError("target_expired", "发现结果已过期，请重新发现应用")
        if self._atspi_owner is not None and self._atspi_owner != owner:
            raise ComputerError("desktop_busy", "桌面由其他执行持有，请先由它释放引用")
        if claim and self._atspi_owner is None:
            self._atspi_owner = owner
            self._atspi_deadline = now + self.config.snapshot_ttl_seconds
        return owner

    def _atspi_current(
        self, owner: tuple[str, str], generation: int, *, targeted: bool = False
    ) -> None:
        self._atspi_available()
        if self._owner() != owner:
            raise ComputerError("execution_changed", "执行已结束或被新的执行替换")
        if self._atspi_owner != owner or generation != self._atspi_generation:
            raise ComputerError("stale_snapshot", "本次无障碍引用已失效，请重新观察")
        if time.monotonic() >= self._atspi_deadline:
            self._retire_atspi()
            if targeted:
                raise ComputerError("target_expired", "发现结果已过期，请重新发现应用")
            raise ComputerError("stale_snapshot", "本次无障碍引用已过期，请重新观察")

    async def call(
        self, request: dict[str, Any], *, before_send: Callable[[], None] | None = None
    ) -> dict[str, Any]:
        self._atspi_available()
        action = request.get("operation") in {"action", "type"}
        if request.get("operation") != "probe":
            owner = self._atspi_caller(claim=before_send is None)
            generation = self._atspi_generation
            reference_guard = before_send

            def current() -> None:
                self._atspi_current(owner, generation)
                if reference_guard is not None:
                    reference_guard()

            before_send = current
        runtime_guard = self._dispatch_guard() if action and self._dispatch_guard else None
        dispatched = False
        guard_error: BaseException | None = None

        def input_guard() -> None:
            nonlocal dispatched, guard_error
            try:
                self._atspi_available()
                if before_send is not None:
                    before_send()
                if runtime_guard is not None:
                    runtime_guard()
            except BaseException as exc:
                guard_error = exc
                raise
            # This callback and run_process's first write have no await between them.
            dispatched = True

        def failure(code: str, message: str) -> ComputerError:
            return ComputerError(
                "action_unknown_outcome" if action and dispatched else code,
                message,
                "unknown" if action and dispatched else "not_started",
            )

        try:
            result = await run_process(
                [self.config.python_path, str(Path(__file__).with_name("atspi_helper.py"))],
                timeout=self.config.operation_timeout_seconds,
                limit=_HELPER_RESPONSE_BYTES,
                input_text=json.dumps({**request, "max_response_chars": _HELPER_RESPONSE_CHARS}),
                input_guard=input_guard,
                merge_stderr=False,
            )
        except (OSError, ValueError) as exc:
            if exc is guard_error or isinstance(exc, ComputerError):
                raise
            if isinstance(exc, FileNotFoundError):
                raise ComputerError("dependency_missing", "无法启动系统 Python 无障碍后端") from exc
            if isinstance(exc, TimeoutError):
                raise failure("read_timeout", "无障碍操作超时；请重新观察，勿自动重试动作") from exc
            if isinstance(exc, ValueError):
                raise failure("limit_exceeded", "无障碍后端响应超过限制") from exc
            raise failure("backend_unavailable", "无法运行无障碍后端") from exc
        if not action:
            self._atspi_available()
            if before_send is not None:
                before_send()
        try:
            data = json.loads(result["output"])
        except (ValueError, TypeError, KeyError) as exc:
            raise failure(
                "protocol_error", "无法读取 AT-SPI 响应；检查系统 Python GI 与桌面会话"
            ) from exc
        if not isinstance(data, dict):
            raise failure("protocol_error", "AT-SPI 响应格式无效")
        if result.get("truncated"):
            raise failure("limit_exceeded", "无障碍后端响应超过限制")
        if result.get("exit_code") != 0 or "error" in data:
            code = data.get("code")
            message = data.get("message", data.get("error"))
            if (
                not isinstance(code, str)
                or code not in _HELPER_ERRORS
                or not isinstance(message, str)
            ):
                raise failure("protocol_error", "AT-SPI 运行失败或响应格式无效")
            outcome = data.get("outcome")
            if not isinstance(outcome, str) or outcome not in {"not_started", "unknown"}:
                raise failure("protocol_error", "AT-SPI 错误结果缺少执行状态")
            if code == "action_unknown_outcome" and outcome != "unknown":
                raise failure("protocol_error", "AT-SPI 动作结果不确定")
            raise ComputerError(code, message[:1000], outcome)
        if action:
            if (
                data.get("ok") is not True
                or data.get("outcome") != "accepted"
                or data.get("effect_verified") is not False
            ):
                raise failure("protocol_error", "原生动作未返回明确的接受回执，结果不确定")
            return {"ok": True, "outcome": "accepted", "effect_verified": False}
        return data

    async def status(self) -> dict[str, Any]:
        if self.backend == "ax":
            return await self._ax.status()
        if self.backend == "uia":
            return await self._uia.status()
        try:
            data = await self.call({"operation": "probe"})
            if (
                data.get("available") is not True
                or type(data.get("applications")) is not int
                or data["applications"] < 0
                or not isinstance(data.get("supported_operations"), list)
                or data.get("discovery_scope") != "desktop_applications"
            ):
                raise ComputerError("protocol_error", "AT-SPI 探测响应格式无效")
            result = {
                "available": True,
                "backend": self.backend,
                "state": "ready",
                "reason": "",
                "dependency_available": True,
                "session_accessible": True,
                "applications": data["applications"],
                "supported_operations": [
                    operation
                    for operation in ("targets", "snapshot", "action", "type")
                    if operation in data.get("supported_operations", [])
                ],
                "discovery_scope": data.get("discovery_scope"),
            }
        except ComputerError as exc:
            result = {
                "available": False,
                "backend": self.backend,
                "reason": exc.message,
                "state": "disabled" if exc.code == "disabled" else "unavailable",
                "dependency_available": (
                    False
                    if exc.code == "dependency_missing"
                    else True
                    if exc.code == "no_desktop"
                    else None
                ),
                "session_accessible": False if exc.code == "no_desktop" else None,
                "supported_operations": [],
                "discovery_scope": None,
                **exc.as_result(),
            }
        result["enabled"] = self.config.enabled
        result["capture"] = (
            self.capture.status()
            if self.capture is not None
            else {"enabled": False, "available": False, "reason": "采集服务未连接"}
        )
        return result

    async def screenshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.config.enabled or self.capture is None:
            raise ValueError("电脑采集尚未启用")
        return await self.capture.screenshot()

    async def record(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.config.enabled or self.capture is None:
            raise ValueError("电脑采集尚未启用")
        return await self.capture.record(arguments["seconds"])

    def _clear_snapshot(self) -> int:
        self._snapshot_generation += 1
        self.snapshot_id, self.snapshot_time, self.nodes = "", 0.0, {}
        return self._snapshot_generation

    def _target(self, identifier: Any, *, application: bool = False) -> _Target:
        if not isinstance(identifier, str) or identifier not in self._targets:
            raise ComputerError("target_invalid", "目标不在当前发现结果中，请重新发现应用")
        if time.monotonic() - self.discovery_time >= self.config.snapshot_ttl_seconds:
            self.discovery_id, self.discovery_time, self._targets = "", 0.0, {}
            raise ComputerError("target_expired", "发现结果已过期，请重新发现应用")
        target = self._targets[identifier]
        if application and target.kind != "application":
            raise ComputerError("target_invalid", "仅可使用当前发现的应用 ID 查找窗口")
        return target

    @staticmethod
    def _locator(node: dict[str, Any]) -> dict[str, Any]:
        return {
            "path": node["path"],
            "expected_name": node["name"],
            "expected_role": node["role"],
            "expected_pid": node["pid"],
            "expected_identity": node["identity"],
        }

    @staticmethod
    def _node(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ComputerError("protocol_error", "无障碍节点格式无效")
        path, identity, actions = value.get("path"), value.get("identity"), value.get("actions")
        if (
            not isinstance(path, list)
            or len(path) > 12
            or any(type(index) is not int or index < 0 for index in path)
            or not isinstance(identity, dict)
            or any(
                not isinstance(identity.get(key), str) or not identity[key]
                for key in ("object_path", "bus_name")
            )
            or any(not isinstance(value.get(key), str) for key in ("name", "role"))
            or type(value.get("pid")) is not int
            or value["pid"] < 0
            or not isinstance(actions, list)
            or len(actions) > 20
            or any(not isinstance(action, str) for action in actions)
            or type(value.get("editable")) is not bool
            or any(value.get(key) is not None and type(value[key]) is not bool for key in _FLAGS)
        ):
            raise ComputerError("protocol_error", "无障碍节点缺少有效的原生身份或状态")
        return {
            "path": list(path),
            "identity": {key: identity[key] for key in ("object_path", "bus_name")},
            "name": value["name"],
            "role": value["role"],
            "pid": value["pid"],
            "actions": list(actions) if path else [],
            "editable": value["editable"] if path else False,
            **{key: value[key] for key in _FLAGS if key in value},
        }

    @staticmethod
    def _public_node(node: dict[str, Any]) -> dict[str, Any]:
        return {
            key: list(node[key]) if key == "actions" else node[key]
            for key in ("name", "role", "actions", "editable", *_FLAGS)
            if key in node
        }

    def _bounded(
        self, data: dict[str, Any], rows: list[dict[str, Any]], field: str, **metadata: Any
    ) -> dict[str, Any]:
        omissions = data.get("omissions")
        reasons = data.get("omission_reasons", [])
        omissions_truncated = data.get("omissions_truncated", False)
        if (
            type(data.get("complete")) is not bool
            or type(data.get("truncated")) is not bool
            or data["complete"] == data["truncated"]
            or type(data.get("visited")) is not int
            or not 0 <= data["visited"] <= self.config.max_nodes
            or not isinstance(omissions, list)
            or type(omissions_truncated) is not bool
            or not isinstance(reasons, list)
            or any(not isinstance(reason, str) or reason not in _OMISSIONS for reason in reasons)
            or (data["complete"] and (omissions or reasons or omissions_truncated))
            or (not data["complete"] and not omissions and not reasons)
        ):
            raise ComputerError("protocol_error", "无障碍响应缺少完整的预算及省略信息")
        grouped: dict[str, int | None] = {}
        for omission in omissions:
            if (
                not isinstance(omission, dict)
                or not isinstance(omission.get("reason"), str)
                or omission["reason"] not in _OMISSIONS
            ):
                raise ComputerError("protocol_error", "无障碍省略原因格式无效")
            reason, count = omission["reason"], omission.get("omitted_direct_children")
            if count is not None and (type(count) is not int or count < 0):
                raise ComputerError("protocol_error", "无障碍省略数量格式无效")
            previous = grouped.get(reason, 0)
            grouped[reason] = None if count is None or previous is None else previous + count
        for reason in reasons:
            grouped.setdefault(reason, None)
        if omissions_truncated:
            grouped = {reason: None for reason in grouped}
        result = {
            **metadata,
            field: rows,
            "complete": data["complete"],
            "truncated": data["truncated"],
            "visited": data["visited"],
            "omissions": [
                {"reason": reason, "omitted_direct_children": count}
                for reason, count in grouped.items()
            ],
            "omissions_truncated": omissions_truncated or len(omissions) > len(grouped),
            "omission_reasons": sorted(grouped),
        }
        limit = self.settings.limits.max_output_chars
        if len(json.dumps(result, ensure_ascii=False)) <= limit:
            return result
        dropped = {
            "reason": "output_budget",
            "omitted_direct_children": None,
            "omitted_records": len(rows),
        }
        result.update(complete=False, truncated=True)
        result["omissions"].append(dropped)
        result["omission_reasons"] = sorted({*grouped, "output_budget"})
        result[field] = []
        size = len(json.dumps(result, ensure_ascii=False))
        if size > limit:
            raise ComputerError("limit_exceeded", "输出预算不足以保留完整的目标及省略信息")
        kept: list[dict[str, Any]] = []
        for row in rows:
            extra = len(json.dumps(row, ensure_ascii=False)) + (2 if kept else 0)
            if size + extra > limit:
                break
            kept.append(row)
            size += extra
        result[field] = kept
        dropped["omitted_records"] = len(rows) - len(kept)
        return result

    async def targets(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.backend == "ax":
            return await self._ax.targets(arguments)
        if self.backend == "uia":
            return await self._uia.targets(arguments)
        owner = self._atspi_caller(claim=True, targeted="application_id" in arguments)
        owner_generation = self._atspi_generation
        selected: _Target | None = None
        selection_error: ComputerError | None = None
        try:
            if "application_id" in arguments:
                selected = self._target(arguments["application_id"], application=True)
        except ComputerError as exc:
            selection_error = exc
        selected_deadline = self.discovery_time + self.config.snapshot_ttl_seconds
        self._discovery_generation += 1
        generation = self._discovery_generation
        self.discovery_id, self.discovery_time, self._targets = "", 0.0, {}
        if selection_error:
            raise selection_error

        def current() -> None:
            self._atspi_current(owner, owner_generation, targeted=selected is not None)
            if generation != self._discovery_generation:
                raise ComputerError("target_changed", "发现已被新的请求替换，请重新发现应用")
            if selected and time.monotonic() >= selected_deadline:
                raise ComputerError("target_expired", "发现结果已过期，请重新发现应用")

        async with self._lock:
            current()
            request: dict[str, Any] = {"operation": "targets", "max_nodes": self.config.max_nodes}
            if selected:
                request["target"] = self._locator(selected.node)
            data = await self.call(request, before_send=current)
            current()
            native = data.get("targets")
            if not isinstance(native, list) or len(native) > self.config.max_nodes:
                raise ComputerError("protocol_error", "无障碍目标列表格式无效")
            parent = selected.node["path"] if selected else []
            records = [self._node(node) for node in native]
            if any(
                node["path"][:-1] != parent or len(node["path"]) != len(parent) + 1
                for node in records
            ):
                raise ComputerError("protocol_error", "无障碍发现返回了所选应用以外的节点")
            cached = ([("application", selected.node)] if selected else []) + [
                ("window" if selected else "application", node) for node in records
            ]
            targets = {new_id(): _Target(kind, node) for kind, node in cached}
            rows = [
                {
                    "target_id": identifier,
                    "kind": target.kind,
                    **({"application_id": identifier} if target.kind == "application" else {}),
                    **self._public_node(target.node),
                }
                for identifier, target in targets.items()
            ]
            discovery_id = new_id()
            result = self._bounded(
                data,
                rows,
                "targets",
                discovery_id=discovery_id,
                scope="application_windows" if selected else "desktop_applications",
            )
            self.discovery_id, self.discovery_time = discovery_id, time.monotonic()
            self._targets = {
                row["target_id"]: targets[row["target_id"]] for row in result["targets"]
            }
            return result

    async def snapshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.backend == "ax":
            return await self._ax.snapshot(arguments)
        if self.backend == "uia":
            return await self._uia.snapshot(arguments)
        owner = self._atspi_caller(claim=True, targeted="target_id" in arguments)
        owner_generation = self._atspi_generation
        generation = self._clear_snapshot()
        selected = self._target(arguments["target_id"]) if "target_id" in arguments else None
        discovery_id = self.discovery_id

        def current() -> None:
            self._atspi_current(owner, owner_generation, targeted=selected is not None)
            if generation != self._snapshot_generation:
                raise ComputerError("stale_snapshot", "快照已被新的读取替换，请重新读取桌面")
            if selected:
                self._target(arguments["target_id"])
                if discovery_id != self.discovery_id:
                    raise ComputerError("target_changed", "所选目标已被新的发现替换")

        async with self._lock:
            current()
            request: dict[str, Any] = {"operation": "snapshot", "max_nodes": self.config.max_nodes}
            if selected:
                request["target"] = self._locator(selected.node)
            data = await self.call(request, before_send=current)
            current()
            if selected and (
                discovery_id != self.discovery_id
                or self._target(arguments["target_id"]) is not selected
            ):
                raise ComputerError("target_changed", "所选目标已被新的发现替换")
            native = data.get("nodes")
            if not isinstance(native, list) or len(native) > self.config.max_nodes:
                raise ComputerError("protocol_error", "无障碍节点列表格式无效")
            # Parent-first order also makes whole-record prefix trimming retain every
            # exposed parent reference, regardless of the helper's traversal order.
            ordered = sorted((self._node(node) for node in native), key=lambda node: node["path"])
            nodes = {f"n{index}": node for index, node in enumerate(ordered)}
            if selected and any(
                node["path"][: len(selected.node["path"])] != selected.node["path"]
                for node in nodes.values()
            ):
                raise ComputerError("protocol_error", "无障碍快照返回了所选目标以外的节点")
            snapshot_id = new_id()
            scope: str | dict[str, str] = (
                {"target_id": arguments["target_id"], "kind": selected.kind}
                if selected
                else "desktop"
            )
            root_depth = len(selected.node["path"]) if selected else 0
            identifiers = {tuple(node["path"]): identifier for identifier, node in nodes.items()}
            rows: list[dict[str, Any]] = []
            for identifier, node in nodes.items():
                depth = len(node["path"]) - root_depth
                parent_id = identifiers.get(tuple(node["path"][:-1])) if depth else None
                rows.append(
                    {
                        "id": identifier,
                        "depth": depth,
                        "parent_id": parent_id,
                        "parent_observed": not depth or parent_id is not None,
                        **self._public_node(node),
                    }
                )
            result = self._bounded(
                data,
                rows,
                "nodes",
                snapshot_id=snapshot_id,
                scope=scope,
            )
            self.snapshot_id, self.snapshot_time = snapshot_id, time.monotonic()
            self.nodes = {row["id"]: nodes[row["id"]] for row in result["nodes"]}
            return result

    async def action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.backend == "ax":
            return await self._ax.action(arguments)
        if self.backend == "uia":
            return await self._uia.action(arguments)
        owner = self._atspi_caller()
        owner_generation = self._atspi_generation
        async with self._lock:
            self._atspi_current(owner, owner_generation)
            if (
                not self.snapshot_id
                or arguments["snapshot_id"] != self.snapshot_id
                or time.monotonic() - self.snapshot_time >= self.config.snapshot_ttl_seconds
            ):
                raise ComputerError("stale_snapshot", "快照已过期，请重新读取桌面")
            node = self.nodes.get(str(arguments["node_id"]))
            generation = self._clear_snapshot()
            if node is None:
                raise ComputerError("stale_node", "节点不在当前快照中")
            if "text" in arguments and not node.get("editable"):
                raise ComputerError("unsupported_operation", "快照中的节点不可编辑")
            if "action" in arguments and arguments["action"] not in node.get("actions", []):
                raise ComputerError("unsupported_operation", "快照中不存在该动作")
            operation = "type" if "text" in arguments else "action"
            request = {
                "operation": operation,
                **self._locator(node),
                **{key: arguments[key] for key in ("text", "action") if key in arguments},
            }

            def current() -> None:
                self._atspi_current(owner, owner_generation)
                if generation != self._snapshot_generation:
                    raise ComputerError("stale_snapshot", "快照已被新的读取替换，请重新观察")

            # A snapshot authorizes one action only, including concurrent callers.
            return await self.call(request, before_send=current)

    async def release(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.backend == "ax":
            return await self._ax.release()
        if self.backend == "uia":
            return await self._uia.release()
        self._atspi_caller()
        self._retire_atspi()
        return {"released": True}

    async def release_execution(self, session_id: str, run_id: str) -> None:
        if self._atspi_owner == (session_id, run_id):
            self._retire_atspi()
        try:
            await self._ax.release_execution((session_id, run_id))
        finally:
            await self._uia.release_execution((session_id, run_id))

    async def aclose(self) -> None:
        self._atspi_closed = True
        self._retire_atspi()
        try:
            await self._ax.aclose()
        finally:
            await self._uia.aclose()

    def tools(self) -> list[Tool]:
        target = {"snapshot_id": string(), "node_id": string()}
        tools = [
            Tool(
                "computer_targets",
                "发现无障碍应用或 Windows 顶层窗口；macOS 限前台应用。应用可按 application_id 展开",
                parameters({"application_id": string("当前发现结果中的应用 ID，可选")}),
                self.targets,
            ),
            Tool(
                "computer_snapshot",
                "读取无障碍树；Linux 默认桌面，macOS 默认前台应用，Windows 须选当前窗口 target_id",
                parameters({"target_id": string("当前发现中的应用或窗口 ID，可选")}),
                self.snapshot,
            ),
            Tool(
                "computer_release",
                "释放当前执行持有的无障碍引用；委派其他 agent 前先释放，让它重新观察",
                parameters({}),
                self.release,
            ),
            Tool(
                "computer_action",
                "执行当前无障碍节点提供的具名动作",
                parameters({**target, "action": string()}, ("snapshot_id", "node_id", "action")),
                self.action,
                "computer",
            ),
            Tool(
                "computer_type",
                "设置可编辑节点的完整文本",
                parameters({**target, "text": string()}, ("snapshot_id", "node_id", "text")),
                self.action,
                "computer",
            ),
        ]
        if self.capture is not None:
            tools.extend(
                [
                    Tool(
                        "computer_screenshot",
                        "保存当前桌面截图到本地附件；需要采集授权",
                        parameters({}),
                        self.screenshot,
                        "computer",
                    ),
                    Tool(
                        "computer_record",
                        "保存有界桌面录屏到本地附件；不录制声音",
                        parameters(
                            {
                                "seconds": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": self.capture.config.max_seconds,
                                }
                            },
                            ("seconds",),
                        ),
                        self.record,
                        "computer",
                    ),
                ]
            )
        return tools
