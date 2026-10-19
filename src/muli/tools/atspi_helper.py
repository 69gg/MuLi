"""One-shot AT-SPI helper; stdout is one bounded JSON response.

Observation work counts visited native slots, including failed reads. Output
keeps whole records and reports every reason for an incomplete observation.
"""

from __future__ import annotations

import json
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

MAX_DEPTH = 12
MAX_CHILDREN = 100
MAX_ACTIONS = 20
MAX_NODES = 1000
DEFAULT_MAX_NODES = 300
MAX_LABEL_CHARS = 500
MAX_IDENTITY_CHARS = 4096
MAX_REQUEST_CHARS = 16000
DEFAULT_RESPONSE_CHARS = 50000
MAX_RESPONSE_CHARS = 60000  # At most 240001 UTF-8 bytes including the output newline.
MIN_RESPONSE_CHARS = 512


class HelperError(ValueError):
    def __init__(
        self, code: str, error: str, *, outcome: str = "not_started", visited: int | None = None
    ) -> None:
        super().__init__(error)
        self.code, self.error, self.outcome, self.visited = code, error, outcome, visited


def identity(accessible: Any) -> dict[str, str]:
    """D-Bus 对象路径与连接名共同标识节点；显示名称不是身份。"""
    path = getattr(accessible, "path", None)
    application = getattr(accessible, "app", None)
    bus_name = getattr(application, "bus_name", None)
    if (
        not isinstance(path, str)
        or not path
        or len(path) > MAX_IDENTITY_CHARS
        or not isinstance(bus_name, str)
        or not bus_name
        or len(bus_name) > MAX_IDENTITY_CHARS
    ):
        raise ValueError("节点没有可验证的 D-Bus 身份")
    return {"object_path": path, "bus_name": bus_name}


def verify_identity(accessible: Any, request: dict[str, Any]) -> None:
    try:
        if identity(accessible) != request.get("expected_identity"):
            raise HelperError("target_changed", "节点身份已改变，请重新获取快照")
        if (
            accessible.get_name()[:MAX_LABEL_CHARS] != request.get("expected_name")
            or accessible.get_role_name() != request.get("expected_role")
            or accessible.get_process_id() != request.get("expected_pid")
        ):
            raise HelperError("target_changed", "节点已发生变化，请重新获取快照")
    except HelperError:
        raise
    except Exception as exc:
        raise HelperError("target_changed", "无法验证节点身份，请重新获取快照") from exc


def _path(request: dict[str, Any]) -> list[int]:
    path = request.get("path")
    expected = request.get("expected_identity")
    if (
        not isinstance(path, list)
        or not path
        or len(path) > MAX_DEPTH
        or any(type(index) is not int or index < 0 for index in path)
        or not isinstance(expected, dict)
        or set(expected) != {"object_path", "bus_name"}
        or any(not isinstance(value, str) or not value for value in expected.values())
        or not isinstance(request.get("expected_name"), str)
        or not isinstance(request.get("expected_role"), str)
        or type(request.get("expected_pid")) is not int
        or request["expected_pid"] < 0
    ):
        raise HelperError("target_invalid", "无障碍目标格式或路径无效")
    return list(path)


@dataclass(slots=True)
class _Observation:
    maximum: int
    response_chars: int = DEFAULT_RESPONSE_CHARS
    key: str = "nodes"
    visited: int = 0
    records: list[dict[str, Any]] = field(default_factory=list)
    omissions: list[dict[str, Any]] = field(default_factory=list)
    omission_reasons: set[str] = field(default_factory=set)
    omissions_truncated: bool = False
    seen: set[tuple[str, str] | tuple[str, int]] = field(default_factory=set)

    def _note(self, reason: str, path: list[int], count: int | None) -> None:
        self.omission_reasons.add(reason)
        for omission in self.omissions:
            if omission["reason"] == reason and omission["path"] == path:
                previous = omission["omitted_direct_children"]
                omission["omitted_direct_children"] = (
                    previous + count if previous is not None and count is not None else None
                )
                return
        self.omissions.append(
            {"reason": reason, "path": list(path), "omitted_direct_children": count}
        )

    def omit(self, reason: str, path: list[int], count: int | None) -> None:
        self._note(reason, path, count)
        self._fit()

    def add(self, record: dict[str, Any]) -> None:
        self.records.append(record)
        self._fit()

    def _payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            self.key: self.records,
            "complete": not self.omission_reasons,
            "truncated": bool(self.omission_reasons),
            "omissions": self.omissions,
            "visited": self.visited,
        }
        if self.omissions_truncated:
            result.update(omissions_truncated=True, omission_reasons=sorted(self.omission_reasons))
        return result

    def _fit(self) -> None:
        def size() -> int:
            return len(json.dumps(self._payload(), ensure_ascii=False))

        while size() > self.response_chars and self.records:
            removed = self.records.pop()
            self._note("output_budget", removed["path"][:-1], 1 if removed["path"] else None)
        while size() > self.response_chars and self.omissions:
            self.omissions.pop()
            self.omissions_truncated = True
        if size() > self.response_chars:
            raise HelperError("protocol_error", "响应预算不足以保存必要元数据")

    def remember(self, accessible: Any, path: list[int]) -> bool:
        try:
            native = identity(accessible)
            key: tuple[str, str] | tuple[str, int] = (native["bus_name"], native["object_path"])
        except Exception:
            # Identity-less desktop is a container, not a synthetic action target.
            key = ("local_object", id(accessible))
        if key in self.seen:
            self.omit("provider_error", path, None)
            return False
        self.seen.add(key)
        return True

    def locate(self, desktop: Any, target: dict[str, Any]) -> tuple[Any, list[int]]:
        path = _path(target)
        accessible = desktop
        self.visited += 1
        self.remember(desktop, [])
        for offset, index in enumerate(path):
            if self.visited >= self.maximum:
                raise HelperError("node_budget", "访问预算不足以验证指定目标", visited=self.visited)
            self.visited += 1
            try:
                accessible.clear_cache()
                accessible = accessible.get_child_at_index(index)
            except Exception as exc:
                raise HelperError("target_changed", "无法定位原目标，请重新发现应用或窗口") from exc
            if accessible is None:
                raise HelperError("target_changed", "原目标已失效，请重新发现应用或窗口")
            if offset < len(path) - 1 and not self.remember(accessible, path[: offset + 1]):
                raise HelperError("target_changed", "目标路径包含循环，请重新发现应用或窗口")
        try:
            accessible.clear_cache()
        except Exception as exc:
            raise HelperError("target_changed", "无法刷新目标，请重新发现应用或窗口") from exc
        verify_identity(accessible, target)
        return accessible, path

    def result(self) -> dict[str, Any]:
        self._fit()
        return self._payload()


def _state_flags(accessible: Any, atspi: Any) -> dict[str, bool | None]:
    states = accessible.get_state_set()
    if states is None:
        raise ValueError("无障碍状态不可用")
    flags: dict[str, bool | None] = {}
    for key, name in (
        ("enabled", "ENABLED"),
        ("sensitive", "SENSITIVE"),
        ("defunct", "DEFUNCT"),
        ("focused", "FOCUSED"),
        ("active", "ACTIVE"),
        ("read_only", "READ_ONLY"),
    ):
        value = getattr(atspi.StateType, name, None)
        flags[key] = bool(states.contains(value)) if value is not None else None
    password = getattr(atspi.Role, "PASSWORD_TEXT", None)
    flags["protected"] = accessible.get_role() == password if password is not None else None
    return flags


def _editable_state(accessible: Any, atspi: Any) -> bool:
    editable = getattr(atspi.StateType, "EDITABLE", None)
    if editable is None:
        return False
    states = accessible.get_state_set()
    return states is not None and bool(states.contains(editable))


def _record(accessible: Any, path: list[int], atspi: Any) -> dict[str, Any]:
    native = identity(accessible)
    role, name, pid = (
        accessible.get_role_name(),
        accessible.get_name(),
        accessible.get_process_id(),
    )
    if not isinstance(role, str) or not isinstance(name, str) or type(pid) is not int or pid < 0:
        raise ValueError("无障碍节点元数据无效")
    if len(role) > MAX_LABEL_CHARS:
        raise HelperError("output_budget", "角色名称超过响应字段预算")
    flags = _state_flags(accessible, atspi)
    actionable = (
        flags["enabled"] is True and flags["sensitive"] is True and flags["defunct"] is False
    )
    interface = accessible.get_action_iface()
    names: list[str] = []
    if interface is not None and actionable:
        count = interface.get_n_actions()
        if type(count) is not int or count < 0:
            raise ValueError("无障碍动作数量无效")
        if count > MAX_ACTIONS:
            raise HelperError("output_budget", "动作列表超过响应字段预算")
        for index in range(count):
            value = interface.get_action_name(index)
            if not isinstance(value, str):
                raise ValueError("无障碍动作名称无效")
            if len(value) > MAX_LABEL_CHARS:
                raise HelperError("output_budget", "动作名称超过响应字段预算")
            names.append(value)
    return {
        "path": list(path),
        "name": name[:MAX_LABEL_CHARS],
        "role": role,
        "pid": pid,
        "identity": native,
        "actions": names if path else [],
        "editable": accessible.get_editable_text_iface() is not None
        and bool(path)
        and actionable
        and flags["protected"] is False
        and flags["read_only"] is False
        and _editable_state(accessible, atspi),
        **flags,
    }


def _observe(desktop: Any, request: dict[str, Any], atspi: Any) -> dict[str, Any]:
    maximum = request.get("max_nodes", DEFAULT_MAX_NODES)
    if type(maximum) is not int or maximum < 1:
        raise HelperError("protocol_error", "max_nodes 必须为正整数")
    response_chars = request.get("max_response_chars", DEFAULT_RESPONSE_CHARS)
    if type(response_chars) is not int or response_chars < MIN_RESPONSE_CHARS:
        raise HelperError("protocol_error", "max_response_chars 不能小于必要元数据预算")
    direct = request.get("operation", "snapshot") == "targets"
    observation = _Observation(
        min(maximum, MAX_NODES),
        min(response_chars, MAX_RESPONSE_CHARS),
        "targets" if direct else "nodes",
    )
    target = request.get("target")
    if target is not None:
        if not isinstance(target, dict):
            raise HelperError("target_invalid", "无障碍目标格式无效")
        root, root_path = observation.locate(desktop, target)
    else:
        root, root_path = desktop, []
        observation.visited = 1
    # Fetch a queued child only when visited; failed/duplicate slots consume work.
    queue: deque[tuple[Any, int | None, list[int]]] = deque([(root, None, root_path)])
    while queue:
        parent, index, path = queue.popleft()
        accessible = parent
        if index is not None:
            observation.visited += 1
            try:
                accessible = parent.get_child_at_index(index)
                if accessible is None:
                    raise ValueError("原生子节点缺失")
            except Exception:
                observation.omit("provider_error", path[:-1], 1)
                continue
        if not observation.remember(accessible, path):
            continue
        include_record = not direct or path != root_path
        if include_record and not path:
            try:
                identity(accessible)
            except ValueError:
                # A desktop without native identity remains a traversal-only
                # container. Other provider failures must still be visible.
                include_record = False
            except Exception:
                observation.omit("provider_error", path, None)
                include_record = False
        if include_record:
            try:
                observation.add(_record(accessible, path, atspi))
            except HelperError as exc:
                observation.omit(exc.code, path, None)
            except Exception:
                observation.omit("provider_error", path, None)
        if direct and path != root_path:
            continue
        try:
            count = accessible.get_child_count()
            if type(count) is not int or count < 0:
                raise ValueError("无法确认子节点数量")
        except Exception:
            observation.omit("provider_error", path, None)
            continue
        if not count:
            continue
        if len(path) >= MAX_DEPTH:
            observation.omit("depth_limit", path, count)
            continue
        allowed = min(count, MAX_CHILDREN)
        if count > allowed:
            observation.omit("children_limit", path, count - allowed)
        # Reserve queued visits before widening the frontier. This bounds both
        # native work and queue memory, including provider failures and cycles.
        capacity = max(0, observation.maximum - observation.visited - len(queue))
        queued = min(allowed, capacity)
        if allowed > queued:
            observation.omit("node_budget", path, allowed - queued)
        queue.extend((accessible, child, [*path, child]) for child in range(queued))
    return observation.result()


def _perform(desktop: Any, request: dict[str, Any], atspi: Any) -> dict[str, Any]:
    accessible, _ = _Observation(MAX_DEPTH + 1).locate(desktop, request)
    invoke: Callable[[], bool]
    try:
        flags = _state_flags(accessible, atspi)
        if flags["defunct"] is not False:
            raise HelperError("element_unavailable", "节点已失效或状态未知，请重新获取快照")
        if flags["enabled"] is not True or flags["sensitive"] is not True:
            raise HelperError("element_disabled", "节点当前不可操作")
        if request["operation"] == "type":
            if flags["protected"] is not False:
                raise HelperError("protected_element", "不能向密码或保护状态未知的控件设置文本")
            if flags["read_only"] is not False or not _editable_state(accessible, atspi):
                raise HelperError("element_disabled", "节点当前不可编辑")
            editable = accessible.get_editable_text_iface()
            text = request.get("text")
            if editable is None or not isinstance(text, str):
                raise HelperError("unsupported_operation", "节点不可编辑或文本格式无效")

            def set_text() -> bool:
                return bool(editable.set_text_contents(text))

            invoke = set_text
        else:
            interface = accessible.get_action_iface()
            name = request.get("action")
            if interface is None or not isinstance(name, str):
                raise HelperError("unsupported_operation", "节点没有该具名动作")
            count = interface.get_n_actions()
            if type(count) is not int or count < 0:
                raise ValueError("原生动作数量无效")
            selected = next(
                (
                    index
                    for index in range(min(count, MAX_ACTIONS))
                    if interface.get_action_name(index) == name
                ),
                None,
            )
            if selected is None:
                raise HelperError("unsupported_operation", "节点当前没有该具名动作")

            def do_action() -> bool:
                return bool(interface.do_action(selected))

            invoke = do_action
    except HelperError:
        raise
    except Exception as exc:
        raise HelperError("protocol_error", "无法验证节点当前状态或操作接口") from exc
    try:
        accepted = invoke()
    except Exception as exc:
        raise HelperError(
            "action_unknown_outcome",
            "原生操作已调用，结果未知；请先核查界面，不要自动重试",
            outcome="unknown",
        ) from exc
    if not accepted:
        raise HelperError(
            "action_unknown_outcome",
            "原生操作未确认成功；请先核查界面，不要自动重试",
            outcome="unknown",
        )
    return {"ok": True, "outcome": "accepted", "effect_verified": False}


def _dispatch(request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation", "snapshot")
    if operation not in {"probe", "targets", "snapshot", "action", "type"}:
        raise HelperError("unsupported_operation", "未知桌面操作")
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as exc:
        raise HelperError("dependency_missing", "系统 Python 缺少 GI 或 Atspi 2.0") from exc
    try:
        Atspi.set_timeout(1000, 2000)
        desktop = Atspi.get_desktop(0)
    except Exception as exc:
        raise HelperError("no_desktop", "无法连接当前 AT-SPI 桌面会话") from exc
    if desktop is None:
        raise HelperError("no_desktop", "当前桌面会话没有 AT-SPI 服务")
    if operation == "probe":
        count = desktop.get_child_count()
        if type(count) is not int or count < 0:
            raise HelperError("protocol_error", "无法确认当前桌面的应用数量")
        return {
            "available": True,
            "applications": count,
            "supported_operations": ["targets", "snapshot", "action", "type"],
            "discovery_scope": "desktop_applications",
        }
    if operation in {"targets", "snapshot"}:
        return _observe(desktop, request, Atspi)
    return _perform(desktop, request, Atspi)


def main(request: dict[str, Any]) -> dict[str, Any]:
    try:
        if not isinstance(request, dict):
            raise HelperError("protocol_error", "AT-SPI 请求必须为 JSON 对象")
        return _dispatch(request)
    except HelperError as exc:
        result: dict[str, Any] = {"code": exc.code, "error": exc.error, "outcome": exc.outcome}
        if exc.visited is not None:
            result["visited"] = exc.visited
        return result
    except Exception:
        return {"code": "protocol_error", "error": "无法读取 AT-SPI 响应", "outcome": "not_started"}


if __name__ == "__main__":
    try:
        result = main(json.loads(sys.stdin.read(MAX_REQUEST_CHARS)))
    except Exception:
        result = {
            "code": "protocol_error",
            "error": "AT-SPI 请求格式无效",
            "outcome": "not_started",
        }
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(1 if "error" in result else 0)
