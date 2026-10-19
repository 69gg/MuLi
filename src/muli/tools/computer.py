"""Accessibility-first desktop control with fresh snapshot identity checks."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from muli.capture import Capture
from muli.config import Settings
from muli.process import run_process
from muli.store import new_id
from muli.tools import Tool, parameters, string


class Computer:
    def __init__(self, settings: Settings, *, capture: Capture | None = None) -> None:
        self.config = settings.computer
        self.capture = capture
        self.snapshot_id = ""
        self.snapshot_time = 0.0
        self.nodes: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def call(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.config.enabled:
            raise ValueError("电脑操作尚未启用")
        if sys.platform != "linux":
            raise ValueError("当前版本的无障碍操作后端支持 Linux AT-SPI")
        result = await run_process(
            [self.config.python_path, str(Path(__file__).with_name("atspi_helper.py"))],
            timeout=12,
            limit=250000,
            input_text=json.dumps(request),
            merge_stderr=False,
        )
        try:
            data = json.loads(result["output"])
        except ValueError as exc:
            raise ValueError("无法读取 AT-SPI 响应；检查系统 Python GI 与桌面会话") from exc
        if not isinstance(data, dict):
            raise ValueError("AT-SPI 响应格式无效")
        if result["exit_code"] or "error" in data:
            raise ValueError(data.get("error", "AT-SPI 运行失败"))
        return data

    async def status(self) -> dict[str, Any]:
        try:
            data = await self.call({"operation": "probe"})
            result = {"backend": "atspi", **data, "reason": ""}
        except (ValueError, OSError, TimeoutError) as exc:
            result = {"available": False, "backend": "atspi", "reason": str(exc)}
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

    async def snapshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            result = await self.call({"operation": "snapshot", "max_nodes": self.config.max_nodes})
            self.snapshot_id, self.snapshot_time = new_id(), time.monotonic()
            self.nodes = {}
            for index, node in enumerate(result["nodes"]):
                node["id"] = f"n{index}"
                self.nodes[node["id"]] = node
            return {"snapshot_id": self.snapshot_id, **result}

    async def action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if (
                not self.snapshot_id
                or arguments["snapshot_id"] != self.snapshot_id
                or time.monotonic() - self.snapshot_time > 60
            ):
                raise ValueError("快照已过期，请重新读取桌面")
            node = self.nodes.get(str(arguments["node_id"]))
            if node is None:
                raise ValueError("节点不在当前快照中")
            if "text" in arguments and not node.get("editable"):
                raise ValueError("快照中的节点不可编辑")
            if "action" in arguments and arguments["action"] not in node.get("actions", []):
                raise ValueError("快照中不存在该动作")
            operation = "type" if "text" in arguments else "action"
            request = {
                "operation": operation,
                "path": node["path"],
                "expected_name": node["name"],
                "expected_role": node["role"],
                "expected_pid": node["pid"],
                "expected_identity": node["identity"],
                **{key: arguments[key] for key in ("text", "action") if key in arguments},
            }
            # A snapshot authorizes one action only, including concurrent callers.
            self.snapshot_id = ""
            return await self.call(request)

    def tools(self) -> list[Tool]:
        target = {"snapshot_id": string(), "node_id": string()}
        tools = [
            Tool(
                "computer_snapshot",
                "优先读取 Linux 无障碍树；不可用时可另行请求截图，操作前必须获取新快照",
                parameters({}),
                self.snapshot,
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
