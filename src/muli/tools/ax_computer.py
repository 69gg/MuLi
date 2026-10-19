"""Host-owned macOS accessibility references; no native libraries in the server."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from muli.store import new_id
from muli.tools.ax_lease import AXHelperLease, AXLeaseError
from muli.tools.computer import ComputerError

if TYPE_CHECKING:
    from muli.tools.computer import Computer

Owner = tuple[str, str]
_FLAGS = ("enabled", "sensitive", "defunct", "protected", "focused", "active", "read_only")


class AXComputer:
    def __init__(
        self,
        host: Computer,
        *,
        owner: Callable[[], Owner | None] | None,
        dispatch_guard: Callable[[], Callable[[], None]] | None,
    ) -> None:
        self.host = host
        self._owner = owner or (lambda: None)
        self._guard = dispatch_guard
        self._lease: AXHelperLease | None = None
        self._lease_owner: Owner | None = None
        self._lock = asyncio.Lock()
        self._generation = 0
        self._targets: dict[str, dict[str, Any]] = {}
        self._nodes: dict[str, dict[str, Any]] = {}
        self._snapshot_id = ""
        self._probe: asyncio.Task[dict[str, Any]] | None = None
        self._probe_lease: AXHelperLease | None = None
        self._probe_result: dict[str, Any] | None = None
        self._probe_until = 0.0
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._cleanup_error: str | None = None

    def _new_lease(self, *, once: bool = False) -> AXHelperLease:
        config = self.host.config
        return AXHelperLease(
            [
                sys.executable,
                str(Path(__file__).with_name("ax_helper.py")),
                "--once" if once else "--session",
            ],
            timeout=config.operation_timeout_seconds,
            ttl=config.snapshot_ttl_seconds,
            max_nodes=config.max_nodes,
            once=once,
        )

    def _available(self) -> None:
        if self._closed:
            raise ComputerError("backend_closed", "电脑后端已关闭")
        if not self.host.config.enabled:
            raise ComputerError("disabled", "电脑操作尚未启用")
        if sys.platform != "darwin":
            raise ComputerError("backend_unavailable", "AX 后端仅支持 macOS")

    def _retire(self) -> int:
        self._generation += 1
        self._targets, self._nodes, self._snapshot_id = {}, {}, ""
        return self._generation

    def _caller(self) -> Owner:
        self._available()
        owner = self._owner()
        if owner is None:
            raise ComputerError("caller_unavailable", "无障碍引用需要真实执行会话")
        if self._lease is not None and self._lease.closed:
            self._retire()
            self._lease, self._lease_owner = None, None
        if self._lease is not None and self._lease_owner != owner:
            raise ComputerError("desktop_busy", "桌面由其他执行持有，请先由它释放引用")
        return owner

    def _current(self, lease: AXHelperLease, owner: Owner, generation: int) -> None:
        if (
            self._closed
            or self._lease is not lease
            or self._lease_owner != owner
            or self._generation != generation
            or lease.closed
            or asyncio.get_running_loop().time() >= lease.expires_at
        ):
            raise ComputerError("stale_snapshot", "本次原生引用已失效，请重新观察")

    async def _release_lease(self, lease: AXHelperLease) -> None:
        # Capture the exact object before awaiting. A late old finalizer must
        # never look up and close a new run's helper.
        if self._lease is lease:
            self._retire()
        try:
            await lease.aclose()
        finally:
            if self._lease is lease and lease.closed:
                self._lease, self._lease_owner = None, None

    @staticmethod
    def _error(exc: AXLeaseError) -> ComputerError:
        return ComputerError(exc.code, exc.message, exc.outcome)

    def _records(self, data: dict[str, Any], field: str) -> list[dict[str, Any]]:
        values = data.get(field)
        if not isinstance(values, list) or len(values) > self.host.config.max_nodes:
            raise ComputerError("protocol_error", "AX 节点列表格式无效")
        seen: dict[str, int] = {}
        result: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, dict):
                raise ComputerError("protocol_error", "AX 节点格式无效")
            ref, parent, depth = value.get("ref_id"), value.get("parent_ref_id"), value.get("depth")
            actions = value.get("actions")
            if (
                not isinstance(ref, str)
                or not 0 < len(ref) <= 128
                or ref in seen
                or (parent is not None and (not isinstance(parent, str) or parent not in seen))
                or type(depth) is not int
                or not 0 <= depth <= 12
                or type(value.get("parent_observed")) is not bool
                or (
                    parent is not None
                    and (depth != seen[parent] + 1 or not value["parent_observed"])
                )
                or (parent is None and value["parent_observed"] != (depth == 0))
                or value.get("kind") not in ("application", "window", "node")
                or any(not isinstance(value.get(key), str) for key in ("name", "role"))
                or (value.get("subrole") is not None and not isinstance(value["subrole"], str))
                or type(value.get("pid")) is not int
                or value["pid"] < 0
                or not isinstance(actions, list)
                or len(actions) > 20
                or any(not isinstance(action, str) or not action for action in actions)
                or type(value.get("editable")) is not bool
                or any(
                    value.get(key) is not None and type(value[key]) is not bool for key in _FLAGS
                )
            ):
                raise ComputerError("protocol_error", "AX 节点引用、结构或状态无效")
            seen[ref] = depth
            result.append(value)
        return result

    async def _observe(self, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
        owner = self._caller()
        deadline = asyncio.get_running_loop().time() + self.host.config.operation_timeout_seconds
        selected: dict[str, Any] | None = None
        key = "application_id" if command == "targets" else "target_id"
        if key in arguments:
            selected = self._targets.get(arguments[key])
        generation = self._retire()
        if key in arguments and (
            selected is None or (command == "targets" and selected["kind"] != "application")
        ):
            raise ComputerError("target_invalid", "目标不属于本次执行的当前发现结果")
        lease: AXHelperLease | None = None
        acquired = False
        try:
            async with asyncio.timeout_at(deadline):
                async with self._lock:
                    if generation != self._generation or self._caller() != owner:
                        raise ComputerError("target_changed", "观察已被其他请求替换")
                    if self._lease is None:
                        self._lease, self._lease_owner = self._new_lease(), owner
                    lease = self._lease
                    acquired = True
                    payload = (
                        {
                            "application_ref_id"
                            if command == "targets"
                            else "target_ref_id": selected["ref_id"]
                        }
                        if selected
                        else {}
                    )
                    data = await lease.request(command, payload, deadline=deadline)
                    self._current(lease, owner, generation)
                    field = "targets" if command == "targets" else "nodes"
                    records = self._records(data, field)
                    scope = data.get("scope")
                    expected_scope = (
                        ("application_windows" if command == "targets" else selected["kind"])
                        if selected
                        else "foreground_application"
                    )
                    if scope != expected_scope:
                        raise ComputerError("protocol_error", "AX 返回范围与所选目标不一致")
                    identifiers = {node["ref_id"]: new_id() for node in records}
                    rows: list[dict[str, Any]] = []
                    cached: dict[str, dict[str, Any]] = {}
                    for node in records:
                        identifier = identifiers[node["ref_id"]]
                        row = self.host._public_node(node)
                        row["subrole"] = node.get("subrole")
                        if command == "targets":
                            if node["kind"] not in {"application", "window"}:
                                raise ComputerError("protocol_error", "AX 发现包含无效目标类型")
                            row.update(target_id=identifier, kind=node["kind"])
                            if node["kind"] == "application":
                                row["application_id"] = identifier
                        else:
                            row.update(
                                id=identifier,
                                depth=node["depth"],
                                parent_id=identifiers.get(node["parent_ref_id"]),
                                parent_observed=node["parent_observed"],
                            )
                        rows.append(row)
                        cached[identifier] = node
                    identifier = new_id()
                    result = self.host._bounded(
                        data,
                        rows,
                        field,
                        scope=scope,
                        **{"discovery_id" if command == "targets" else "snapshot_id": identifier},
                    )
                    if command == "targets":
                        self._targets = {
                            row["target_id"]: cached[row["target_id"]] for row in result[field]
                        }
                    else:
                        self._snapshot_id = identifier
                        self._nodes = {row["id"]: cached[row["id"]] for row in result[field]}
                    return result
        except AXLeaseError as exc:
            raise self._error(exc) from exc
        except TimeoutError as exc:
            raise ComputerError("read_timeout", "无障碍观察超时，请重新观察") from exc
        finally:
            # No old public IDs survive a failed or cancelled observation.
            if acquired and lease is not None and not (self._targets or self._snapshot_id):
                try:
                    await self._release_lease(lease)
                except Exception as exc:
                    self._cleanup_error = f"原生进程回收失败：{type(exc).__name__}"

    async def targets(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._observe("targets", arguments)

    async def snapshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._observe("snapshot", arguments)

    async def action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        owner = self._caller()
        lease = self._lease
        if (
            lease is None
            or not self._snapshot_id
            or arguments.get("snapshot_id") != self._snapshot_id
        ):
            raise ComputerError("stale_snapshot", "快照不属于本次执行或已失效")
        node = self._nodes.get(str(arguments.get("node_id")))
        # Consume before the first await, including rejected legal attempts.
        generation = self._retire()
        deadline = asyncio.get_running_loop().time() + self.host.config.operation_timeout_seconds
        try:
            guard = self._guard() if self._guard else None
            if node is None:
                raise ComputerError("stale_node", "节点不在当前快照中")
            command = "type" if "text" in arguments else "action"
            if command == "type" and not node["editable"]:
                raise ComputerError("unsupported_operation", "当前节点不支持文本设置")
            if command == "action" and arguments.get("action") not in node["actions"]:
                raise ComputerError("unsupported_operation", "当前节点不提供该动作")

            def before_send() -> None:
                self._current(lease, owner, generation)
                if guard is not None:
                    guard()

            async with asyncio.timeout_at(deadline):
                await self._lock.acquire()
            try:
                before_send()
                result = await lease.request(
                    command,
                    {
                        "ref_id": node["ref_id"],
                        "text" if command == "type" else "action": arguments[
                            "text" if command == "type" else "action"
                        ],
                    },
                    deadline=deadline,
                    before_send=before_send,
                )
                if (
                    result.get("ok") is not True
                    or result.get("outcome") != "accepted"
                    or result.get("effect_verified") is not False
                ):
                    raise ComputerError(
                        "action_unknown_outcome", "原生动作未返回明确回执，请重新观察", "unknown"
                    )
                return {"ok": True, "outcome": "accepted", "effect_verified": False}
            finally:
                self._lock.release()
        except AXLeaseError as exc:
            raise self._error(exc) from exc
        except TimeoutError as exc:
            # A timeout outside lease.request is before its dispatch. The lease
            # itself classifies any write/response timeout as unknown.
            raise ComputerError("read_timeout", "动作尚未派发便超时") from exc
        finally:
            try:
                await self._release_lease(lease)
            except Exception as exc:
                # Cleanup cannot turn an accepted or unknown action into a
                # generic retryable error. Keep the occupied slot and diagnostic.
                self._cleanup_error = f"原生进程回收失败：{type(exc).__name__}"

    async def release(self) -> dict[str, Any]:
        owner = self._owner()
        if owner is None:
            raise ComputerError("caller_unavailable", "释放引用需要真实执行会话")
        await self.release_execution(owner)
        return {"released": True}

    async def release_execution(self, owner: Owner) -> None:
        lease = self._lease
        if lease is not None and self._lease_owner == owner:
            await self._release_lease(lease)

    async def _probe_once(self) -> dict[str, Any]:
        if self._probe_lease is not None and not self._probe_lease.closed:
            return self._status_error(ComputerError("cleanup_failed", "上一次探测尚未完成回收"))
        lease = self._new_lease(once=True)
        self._probe_lease = lease
        try:
            data = await lease.request("probe", {})
            permission = data.get("permission")
            if (
                data.get("backend") != "ax"
                or data.get("dependency_available") is not True
                or not isinstance(permission, dict)
                or type(permission.get("trusted")) is not bool
                or permission.get("subject") != "helper_process"
                or permission.get("prompted") is not False
                or data.get("session_accessible") is not None
                or data.get("discovery_scope") != "foreground_application"
                or type(data.get("helper_pid")) is not int
                or data["helper_pid"] <= 0
                or not isinstance(data.get("executable"), str)
                or not 0 < len(data["executable"]) <= 4096
            ):
                raise ComputerError("protocol_error", "AX 探测响应格式无效")
            trusted = permission["trusted"]
            result = {
                "available": trusted,
                "backend": "ax",
                "state": "ready" if trusted else "unavailable",
                "reason": "" if trusted else "当前 Python helper 尚无系统辅助功能权限",
                "dependency_available": True,
                "session_accessible": None,
                "permission": permission,
                "helper_pid": data["helper_pid"],
                "executable": data["executable"],
                "discovery_scope": "foreground_application",
                "supported_operations": ["targets", "snapshot", "action", "type"]
                if trusted
                else [],
            }
        except (AXLeaseError, ComputerError) as exc:
            error = self._error(exc) if isinstance(exc, AXLeaseError) else exc
            result = self._status_error(error)
        finally:
            try:
                await lease.aclose()
            except Exception as exc:
                self._cleanup_error = f"能力探测回收失败：{type(exc).__name__}"
            finally:
                if self._probe_lease is lease and lease.closed:
                    self._probe_lease = None
        self._probe_result = result
        self._probe_until = asyncio.get_running_loop().time() + (60 if result["available"] else 10)
        return result

    @staticmethod
    def _status_error(exc: ComputerError) -> dict[str, Any]:
        return {
            "available": False,
            "backend": "ax",
            "state": "disabled" if exc.code == "disabled" else "unavailable",
            "reason": exc.message,
            "dependency_available": False if exc.code == "dependency_missing" else None,
            "session_accessible": None,
            "supported_operations": [],
            "discovery_scope": None,
            **exc.as_result(),
        }

    async def status(self) -> dict[str, Any]:
        try:
            self._available()
            if self._probe_result is None or asyncio.get_running_loop().time() >= self._probe_until:
                if self._probe is None or self._probe.done():
                    self._probe = asyncio.create_task(self._probe_once(), name="muli-ax-probe")
                result = dict(await asyncio.shield(self._probe))
            else:
                result = dict(self._probe_result)
        except ComputerError as exc:
            result = self._status_error(exc)
        result.update(
            enabled=self.host.config.enabled,
            busy=self._lease is not None and not self._lease.closed,
            capture=self.host.capture.status()
            if self.host.capture
            else {"enabled": False, "available": False, "reason": "采集服务未连接"},
        )
        if self._cleanup_error is not None:
            result["cleanup_error"] = self._cleanup_error
            if (self._lease is not None and not self._lease.closed) or (
                self._probe_lease is not None and not self._probe_lease.closed
            ):
                result.update(available=False, state="unavailable", reason=self._cleanup_error)
        return result

    async def aclose(self) -> None:
        self._closed = True
        self._retire()

        async def settle() -> None:
            if self._probe is not None:
                self._probe.cancel()
            try:
                if self._lease is not None:
                    await self._release_lease(self._lease)
            finally:
                if self._probe is not None:
                    await asyncio.gather(self._probe, return_exceptions=True)
                if self._probe_lease is not None:
                    await self._probe_lease.aclose()

        if self._close_task is None:
            self._close_task = asyncio.create_task(settle())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError
