"""Windows UIA observations and one-shot Invoke in a caller-owned MTA helper."""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from muli.process import _wait_cleanup
from muli.store import new_id
from muli.tools.ax_lease import AXHelperLease, AXLeaseError
from muli.tools.computer import ComputerError

if TYPE_CHECKING:
    from muli.tools.computer import Computer

Owner = tuple[str, str]
_ERRORS = {
    "dependency_missing": "系统 Windows PowerShell 5.1 或 UI Automation 程序集不可用",
    "backend_unavailable": "无法运行 Windows UI Automation 后端",
    "permission_required": "当前系统策略不允许使用 UI Automation",
    "element_unavailable": "所选窗口已不可读取，请重新发现窗口",
    "target_changed": "所选窗口已变化，请重新发现窗口",
    "read_timeout": "UI Automation 读取超时，请重新观察",
    "limit_exceeded": "UI Automation 响应超过预算",
    "lease_expired": "窗口引用已过期，请重新发现窗口",
    "protocol_error": "UI Automation helper 响应格式无效",
    "stale_node": "节点已变化，请重新观察",
    "protected_element": "受保护节点不允许操作",
    "element_disabled": "节点当前未启用",
    "unsupported_operation": "当前节点不支持该操作",
    "action_unknown_outcome": "原生动作结果不确定，请重新观察，勿直接重试",
}


def _powershell() -> Path:
    """Resolve the installed Windows host through the OS, never through PATH."""
    if os.name != "nt":
        raise ComputerError("backend_unavailable", _ERRORS["backend_unavailable"])
    try:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise OSError
        library = loader("kernel32", use_last_error=True)
        get_directory = library.GetSystemDirectoryW
        get_directory.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
        get_directory.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_directory(buffer, len(buffer))
        if not 0 < length < len(buffer):
            raise OSError
        executable = Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not executable.is_file():
            raise OSError
        return executable
    except (AttributeError, OSError) as exc:
        raise ComputerError("dependency_missing", _ERRORS["dependency_missing"]) from exc


class UIAComputer:
    def __init__(
        self,
        host: Computer,
        *,
        owner: Callable[[], Owner | None] | None,
        dispatch_guard: Callable[[], Callable[[], None]] | None = None,
    ) -> None:
        self.host = host
        self._owner = owner or (lambda: None)
        self._guard = dispatch_guard
        self._lease: AXHelperLease | None = None
        self._lease_owner: Owner | None = None
        self._targets: dict[str, str] = {}
        self._nodes: dict[str, dict[str, Any]] = {}
        self._snapshot_id: str | None = None
        self._generation = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._probe: asyncio.Task[dict[str, Any]] | None = None
        self._probe_lease: AXHelperLease | None = None
        self._probe_result: dict[str, Any] | None = None
        self._probe_until = 0.0
        self._cleanup_error: str | None = None

    def _new_lease(self, *, once: bool = False) -> AXHelperLease:
        script = Path(__file__).with_name("uia_helper.ps1")
        return AXHelperLease(
            [
                str(_powershell()),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Mta",
                "-File",
                str(script),
            ],
            timeout=self.host.config.operation_timeout_seconds,
            ttl=self.host.config.snapshot_ttl_seconds,
            max_nodes=self.host.config.max_nodes,
            once=once,
        )

    def _available(self) -> None:
        if self._closed:
            raise ComputerError("backend_closed", "电脑后端已关闭")
        if not self.host.config.enabled:
            raise ComputerError("disabled", "电脑操作尚未启用")
        if sys.platform != "win32":
            raise ComputerError("backend_unavailable", "UI Automation 后端仅支持 Windows")

    def _retire(self) -> int:
        self._generation += 1
        self._targets = {}
        self._nodes = {}
        self._snapshot_id = None
        return self._generation

    def _caller(self) -> Owner:
        self._available()
        owner = self._owner()
        if owner is None:
            raise ComputerError("caller_unavailable", "窗口引用需要真实执行会话")
        if self._lease is not None and self._lease.closed:
            self._lease, self._lease_owner = None, None
            self._retire()
        if self._lease is not None and self._lease_owner != owner:
            raise ComputerError("desktop_busy", "窗口由其他执行持有，请先由它释放引用")
        return owner

    def _current(self, lease: AXHelperLease, owner: Owner, generation: int) -> None:
        self._available()
        if (
            self._owner() != owner
            or self._lease is not lease
            or self._lease_owner != owner
            or generation != self._generation
            or lease.state in {"closing", "closed"}
            or asyncio.get_running_loop().time() >= lease.expires_at
        ):
            raise ComputerError("stale_snapshot", "本次窗口引用已失效，请重新发现窗口")

    @staticmethod
    def _error(exc: AXLeaseError) -> ComputerError:
        return ComputerError(
            exc.code, _ERRORS.get(exc.code, _ERRORS["backend_unavailable"]), exc.outcome
        )

    async def _release_lease(self, lease: AXHelperLease) -> None:
        if self._lease is lease:
            self._retire()
        try:
            await lease.aclose()
        except Exception as exc:
            self._cleanup_error = "UI Automation helper 尚未确认退出"
            raise ComputerError("cleanup_failed", self._cleanup_error) from exc
        finally:
            if self._lease is lease and lease.closed:
                self._lease, self._lease_owner = None, None

    def _records(self, data: dict[str, Any], field: str) -> list[dict[str, Any]]:
        values = data.get(field)
        if not isinstance(values, list) or len(values) > self.host.config.max_nodes:
            raise ComputerError("protocol_error", _ERRORS["protocol_error"])
        seen: dict[str, int] = {}
        for index, node in enumerate(values):
            if not isinstance(node, dict):
                raise ComputerError("protocol_error", _ERRORS["protocol_error"])
            ref, parent, depth = node.get("ref_id"), node.get("parent_ref_id"), node.get("depth")
            invoke = node.get("actions") == ["invoke"]
            if (
                not isinstance(ref, str)
                or not 1 <= len(ref) <= 128
                or ref in seen
                or type(depth) is not int
                or not 0 <= depth <= 12
                or (
                    (parent is not None or depth != 0)
                    and (not isinstance(parent, str) or seen.get(parent) != depth - 1)
                )
                or not isinstance(node.get("kind"), str)
                or node["kind"] not in {"window", "node"}
                or any(not isinstance(node.get(key), str) for key in ("name", "role"))
                or node.get("actions") not in ([], ["invoke"])
                or node.get("editable") is not False
                or node.get("read_only") is not (not invoke)
                or any(type(node.get(key)) is not bool for key in ("protected", "enabled"))
                or (node["protected"] and node["name"] != "")
                or (
                    invoke
                    and (
                        field != "nodes"
                        or node["kind"] != "node"
                        or node["role"] != "ControlType.Button"
                        or node["protected"]
                        or not node["enabled"]
                    )
                )
                or (field == "targets" and (depth != 0 or node["kind"] != "window"))
                or (
                    field == "nodes"
                    and (
                        (index == 0 and (depth != 0 or node["kind"] != "window"))
                        or (index > 0 and (depth == 0 or node["kind"] != "node"))
                    )
                )
            ):
                raise ComputerError("protocol_error", _ERRORS["protocol_error"])
            seen[ref] = depth
        return values

    async def _observe(self, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
        owner = self._caller()
        deadline = asyncio.get_running_loop().time() + self.host.config.operation_timeout_seconds
        identifier = arguments.get("target_id")
        selected = self._targets.get(identifier) if isinstance(identifier, str) else None
        # Capture the target first, then revoke every old public credential
        # before waiting for another operation or validating this attempt.
        generation = self._retire()
        if command == "targets":
            if arguments:
                raise ComputerError(
                    "unsupported_operation", "Windows 直接发现顶层窗口，不按应用展开"
                )
            payload: dict[str, str] = {}
        else:
            if set(arguments) != {"target_id"} or selected is None:
                raise ComputerError(
                    "target_invalid", "请先发现窗口，再使用当前 target_id 读取该窗口"
                )
            payload = {"target_ref_id": selected}
        lease: AXHelperLease | None = None
        complete = False
        try:
            async with asyncio.timeout_at(deadline):
                async with self._lock:
                    if generation != self._generation or self._caller() != owner:
                        raise ComputerError("target_changed", "观察所属执行已变化")
                    if self._lease is None:
                        self._lease, self._lease_owner = self._new_lease(), owner
                    lease = self._lease
                    data = await lease.request(command, payload, deadline=deadline)
                    self._current(lease, owner, generation)
                    field = "targets" if command == "targets" else "nodes"
                    scope = "desktop_windows" if command == "targets" else "window"
                    if data.get("scope") != scope:
                        raise ComputerError("protocol_error", _ERRORS["protocol_error"])
                    records = self._records(data, field)
                    ids = {node["ref_id"]: new_id() for node in records}
                    rows: list[dict[str, Any]] = []
                    for node in records:
                        row = self.host._public_node(node)
                        if command == "targets":
                            row.update(target_id=ids[node["ref_id"]], kind="window")
                        else:
                            row.update(
                                id=ids[node["ref_id"]],
                                parent_id=ids.get(node["parent_ref_id"]),
                                parent_observed=True,
                                depth=node["depth"],
                            )
                        rows.append(row)
                    public_id = new_id()
                    result = self.host._bounded(
                        data,
                        rows,
                        field,
                        scope=scope,
                        **{"discovery_id" if command == "targets" else "snapshot_id": public_id},
                    )
                    if command == "targets":
                        by_id = {ids[node["ref_id"]]: node["ref_id"] for node in records}
                        self._targets = {
                            row["target_id"]: by_id[row["target_id"]] for row in result[field]
                        }
                    else:
                        cached = {ids[node["ref_id"]]: node for node in records}
                        self._snapshot_id = public_id
                        self._nodes = {row["id"]: cached[row["id"]] for row in result[field]}
                    complete = True
                    return result
        except AXLeaseError as exc:
            raise self._error(exc) from exc
        except TimeoutError as exc:
            raise ComputerError("read_timeout", _ERRORS["read_timeout"]) from exc
        finally:
            if not complete and lease is not None and generation == self._generation:
                try:
                    await self._release_lease(lease)
                except Exception:
                    self._cleanup_error = "UI Automation helper 尚未确认退出"

    async def targets(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._observe("targets", arguments)

    async def snapshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._observe("snapshot", arguments)

    async def action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        owner = self._caller()
        lease = self._lease
        if (
            lease is None
            or self._snapshot_id is None
            or arguments.get("snapshot_id") != self._snapshot_id
        ):
            raise ComputerError("stale_snapshot", "快照不属于本次执行或已失效")
        identifier = arguments.get("node_id")
        node = self._nodes.get(identifier) if isinstance(identifier, str) else None
        generation = self._retire()
        deadline = asyncio.get_running_loop().time() + self.host.config.operation_timeout_seconds
        try:
            guard = self._guard() if self._guard else None
            if node is None:
                raise ComputerError("stale_node", _ERRORS["stale_node"])
            if (
                set(arguments) != {"snapshot_id", "node_id", "action"}
                or arguments["action"] != "invoke"
                or node["actions"] != ["invoke"]
            ):
                raise ComputerError("unsupported_operation", "Windows 仅支持普通按钮的 invoke")

            def before_send() -> None:
                self._current(lease, owner, generation)
                if guard is not None:
                    guard()

            async with asyncio.timeout_at(deadline):
                await self._lock.acquire()
            try:
                before_send()
                result = await lease.request(
                    "action",
                    {"ref_id": node["ref_id"], "action": "invoke"},
                    deadline=deadline,
                    before_send=before_send,
                )
                if (
                    result.get("ok") is not True
                    or result.get("outcome") != "accepted"
                    or result.get("effect_verified") is not False
                ):
                    raise ComputerError(
                        "action_unknown_outcome", _ERRORS["action_unknown_outcome"], "unknown"
                    )
                return {"ok": True, "outcome": "accepted", "effect_verified": False}
            finally:
                self._lock.release()
        except AXLeaseError as exc:
            raise self._error(exc) from exc
        except TimeoutError as exc:
            # The lease itself classifies any timeout after stdin.write.
            raise ComputerError("read_timeout", "动作尚未派发便超时") from exc
        finally:
            try:
                await self._release_lease(lease)
            except Exception:
                # Reaping uncertainty must not overwrite accepted/unknown or
                # release the occupied helper for another caller.
                self._cleanup_error = "UI Automation helper 尚未确认退出"

    async def release(self) -> dict[str, Any]:
        owner = self._owner()
        if owner is None:
            raise ComputerError("caller_unavailable", "释放窗口引用需要真实执行会话")
        await self.release_execution(owner)
        return {"released": True}

    async def release_execution(self, owner: Owner) -> None:
        lease = self._lease
        if lease is not None and self._lease_owner == owner:
            await self._release_lease(lease)

    @staticmethod
    def _status_error(exc: ComputerError) -> dict[str, Any]:
        return {
            "available": False,
            "backend": "uia",
            "state": "disabled" if exc.code == "disabled" else "unavailable",
            "reason": exc.message,
            "dependency_available": False if exc.code == "dependency_missing" else None,
            "session_accessible": None,
            "supported_operations": [],
            "discovery_scope": None,
            **exc.as_result(),
        }

    async def _probe_once(self) -> dict[str, Any]:
        lease: AXHelperLease | None = None
        try:
            if self._probe_lease is not None and not self._probe_lease.closed:
                raise ComputerError("cleanup_failed", "上一次 UI Automation 探测尚未确认退出")
            lease = self._new_lease(once=True)
            self._probe_lease = lease
            data = await lease.request("probe", {})
            if (
                data.get("backend") != "uia"
                or data.get("apartment") != "MTA"
                or data.get("dependency_available") is not True
                or data.get("session_accessible") is not None
                or data.get("discovery_scope") != "desktop_windows"
                or type(data.get("helper_pid")) is not int
                or data["helper_pid"] <= 0
            ):
                raise ComputerError("protocol_error", _ERRORS["protocol_error"])
            result = {
                "backend": "uia",
                "apartment": "MTA",
                "dependency_available": True,
                "session_accessible": None,
                "discovery_scope": "desktop_windows",
                "available": True,
                "state": "ready",
                "reason": "",
                "supported_operations": ["targets", "snapshot", "action"],
            }
        except (AXLeaseError, ComputerError) as exc:
            result = self._status_error(self._error(exc) if isinstance(exc, AXLeaseError) else exc)
        finally:
            if lease is not None:
                try:
                    await lease.aclose()
                except Exception:
                    self._cleanup_error = "UI Automation 探测尚未确认退出"
                if lease.closed and self._probe_lease is lease:
                    self._probe_lease = None
        self._probe_result = result
        self._probe_until = asyncio.get_running_loop().time() + (60 if result["available"] else 10)
        return result

    async def status(self) -> dict[str, Any]:
        try:
            self._available()
            if self._probe_result is None or asyncio.get_running_loop().time() >= self._probe_until:
                if self._probe is None or self._probe.done():
                    self._probe = asyncio.create_task(self._probe_once(), name="muli-uia-probe")
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
        if await _wait_cleanup(self._close_task):
            raise asyncio.CancelledError
