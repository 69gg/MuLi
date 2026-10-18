"""Isolated system-Python AT-SPI helper; stdout is a single JSON response."""

from __future__ import annotations

import json
import sys
from collections import deque
from typing import Any


def identity(accessible: Any) -> dict[str, str]:
    """D-Bus 对象路径与连接名共同标识节点；显示名称不是身份。"""
    path = getattr(accessible, "path", None)
    application = getattr(accessible, "app", None)
    bus_name = getattr(application, "bus_name", None)
    if not isinstance(path, str) or not path or not isinstance(bus_name, str) or not bus_name:
        raise ValueError("节点没有可验证的 D-Bus 身份")
    return {"object_path": path, "bus_name": bus_name}


def verify_identity(accessible: Any, request: dict[str, Any]) -> None:
    if identity(accessible) != request.get("expected_identity"):
        raise ValueError("节点身份已改变，请重新获取快照")
    if (
        accessible.get_name()[:500] != request["expected_name"]
        or accessible.get_role_name() != request["expected_role"]
        or accessible.get_process_id() != request["expected_pid"]
    ):
        raise ValueError("节点已发生变化，请重新获取快照")


def main(request: dict[str, Any]) -> dict[str, Any]:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    operation = request.get("operation", "snapshot")
    Atspi.set_timeout(1000, 2000)
    desktop = Atspi.get_desktop(0)
    if desktop is None:
        raise RuntimeError("当前桌面会话没有 AT-SPI 服务")
    if operation == "probe":
        return {"available": True, "applications": desktop.get_child_count()}
    if operation == "snapshot":
        maximum = min(int(request.get("max_nodes", 300)), 1000)
        queue: deque[tuple[Any, list[int], int]] = deque([(desktop, [], 0)])
        nodes: list[dict[str, Any]] = []
        visited = 0
        truncated = False
        while queue and visited < maximum:
            accessible, path, depth = queue.popleft()
            visited += 1
            try:
                role, name = accessible.get_role_name(), accessible.get_name()
                actions = accessible.get_action_iface()
                action_names = (
                    [actions.get_action_name(i) for i in range(min(actions.get_n_actions(), 20))]
                    if actions
                    else []
                )
                try:
                    node_identity = identity(accessible)
                except ValueError:
                    node_identity = None
                if node_identity is not None:
                    nodes.append(
                        {
                            "path": path,
                            "role": role,
                            "name": name[:500],
                            "pid": accessible.get_process_id(),
                            "identity": node_identity,
                            "actions": action_names,
                            "editable": accessible.get_editable_text_iface() is not None,
                        }
                    )
                if depth < 12:
                    for index in range(min(accessible.get_child_count(), 100)):
                        if len(queue) + visited >= maximum:
                            truncated = True
                            break
                        child = accessible.get_child_at_index(index)
                        if child is not None:
                            queue.append((child, [*path, index], depth + 1))
            except Exception:  # noqa: BLE001, S112 - inaccessible nodes are skipped within the visit budget
                continue
        return {"nodes": nodes, "truncated": truncated or bool(queue)}
    accessible = desktop
    path = request["path"]
    if not isinstance(path, list) or not path or len(path) > 12:
        raise ValueError("无障碍节点路径无效")
    for index in path:
        if not isinstance(index, int) or index < 0:
            raise ValueError("节点索引无效")
        accessible = accessible.get_child_at_index(index)
        if accessible is None:
            raise ValueError("节点已失效，请重新获取快照")
    verify_identity(accessible, request)
    if operation == "action":
        interface = accessible.get_action_iface()
        if interface is None:
            raise ValueError("节点没有可执行动作")
        name = request["action"]
        for index in range(interface.get_n_actions()):
            if interface.get_action_name(index) == name:
                return {"ok": bool(interface.do_action(index))}
        raise ValueError("快照中不存在该动作")
    if operation == "type":
        interface = accessible.get_editable_text_iface()
        if interface is None:
            raise ValueError("节点不可编辑")
        return {"ok": bool(interface.set_text_contents(request["text"]))}
    raise ValueError("未知桌面操作")


if __name__ == "__main__":
    try:
        print(json.dumps(main(json.loads(sys.stdin.read(16000))), ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - helper must emit one structured error response
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
