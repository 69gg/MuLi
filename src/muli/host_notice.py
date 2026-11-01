"""宿主固定模板通知的直发路径，不经过模型或审查器。

只有宿主已经保存、能够重新核验的四类事实可以进入这里：更新提醒、已完成的
管理操作、固定错误码的系统日志和到期提醒。正文只使用固定模板，不拼接模型
输出、外部资料节选或记忆内容。投递仍复用渠道管理器的自动投递门、冻结路由和
outbox 幂等键；任何失败都会把原事项留给 Attention 的只读复核。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from muli.proactive import FrozenRouting
from muli.store import now

# 这些是 system_events 已固定的宿主错误码；日志正文和自由字段不会进入模板。
HOST_LOG_CODES = frozenset(
    {
        "runtime_failed",
        "update_failed",
        "management_failed",
        "channel_failed",
        "media_failed",
        "plugin_failed",
        "extension_reload_failed",
        "unknown",
    }
)
_MANAGEMENT_ACTIONS = frozenset({"configure", "restart", "update", "adopt", "switch"})


class HostChannelManager(Protocol):
    async def enqueue(
        self,
        name: str,
        target: str,
        text: str = "",
        attachment_ids: Sequence[str] = (),
        *,
        event_id: str | None = None,
        route: Mapping[str, Any] | None = None,
        automatic: bool = False,
        attention_routing: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class HostNoticeTemplate:
    kind: str
    title: str
    content: str
    notification_id: str | None = None


@dataclass(slots=True)
class HostDeliveryOutcome:
    """按 receipt 记录投递结果；已投递项从 AI 批次剔除。"""

    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    delivered: set[str] = field(default_factory=set)
    notifications: list[dict[str, Any]] = field(default_factory=list)


def _detail(record: Mapping[str, Any]) -> dict[str, Any] | None:
    value = record.get("detail")
    if not isinstance(value, str) or len(value) > 16000:
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def host_template(record: Mapping[str, Any]) -> HostNoticeTemplate | None:
    """只返回宿主固定模板；不读取通知正文或任何模型生成文本。"""
    kind = record.get("kind")
    record_id = record.get("record_id")
    if kind == "update" and isinstance(record_id, str):
        return HostNoticeTemplate(
            "update",
            "沐璃有新版本",
            "发现可用的沐璃新版本。请打开沐璃工作台查看版本与更新条件。",
            record_id,
        )
    if kind == "management":
        detail = _detail(record)
        if (
            detail is None
            or detail.get("status") != "completed"
            or detail.get("action") not in _MANAGEMENT_ACTIONS
        ):
            return None
        return HostNoticeTemplate(
            "management",
            "管理操作已完成",
            f"管理操作 {detail['action']} 已完成。请打开沐璃工作台查看结果与回执。",
        )
    if kind == "system_log":
        detail = _detail(record)
        code = detail.get("code") if detail is not None else None
        if code not in HOST_LOG_CODES:
            return None
        return HostNoticeTemplate(
            "system_log",
            "系统需要关注",
            f"系统记录了一项需要关注的问题（{code}）。请打开沐璃工作台查看详情。",
        )
    if kind == "reminder" and isinstance(record_id, str):
        return HostNoticeTemplate(
            "reminder",
            "提醒时间已到",
            "有一条提醒已到时间。请打开沐璃工作台查看内容。",
            record_id,
        )
    return None


class HostNoticeDelivery:
    """固定模板直发；调用方负责在投递前通过当前策略和证明守卫。"""

    def __init__(
        self,
        runtime: Any,
        manager: Callable[[], HostChannelManager | None],
    ) -> None:
        self.runtime = runtime
        self._manager = manager

    @property
    def mode(self) -> str:
        return self.runtime.config.settings.attention.host_delivery

    @staticmethod
    def _fallback(reason: str, template: HostNoticeTemplate) -> dict[str, Any]:
        return {
            "host_fallback": {
                "mode": "enforce",
                "reason": reason,
                "template": template.kind,
            }
        }

    def _notification(self, record: Mapping[str, Any], template: HostNoticeTemplate) -> dict[str, Any]:
        receipt_id = record["receipt_id"]
        return {
            "id": f"host-notice:{receipt_id}",
            "title": template.title,
            "content": template.content,
            "source": "host_notice",
            "read": False,
            "created_at": now(),
            "routing": record.get("routing", {}),
            "receipt_id": receipt_id,
        }

    async def deliver(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        guard: Callable[[], None],
    ) -> HostDeliveryOutcome:
        outcome = HostDeliveryOutcome()
        mode = self.mode
        if mode == "off":
            return outcome
        for record in records:
            template = host_template(record)
            if template is None:
                continue
            receipt_id = record["receipt_id"]
            if mode == "shadow":
                outcome.receipts[receipt_id] = {
                    "host_delivery": {
                        "mode": "shadow",
                        "template": template.kind,
                        "notification_id": template.notification_id,
                    }
                }
                continue
            guard()
            routes = FrozenRouting.load(record.get("routing")).origin.delivery_routes()
            manager = self._manager()
            if not routes or manager is None:
                outcome.receipts[receipt_id] = self._fallback("no_route", template)
                continue
            route = routes[0]
            try:
                item = await manager.enqueue(
                    route["name"],
                    route["target"],
                    template.content,
                    event_id=f"host-notice:{receipt_id}",
                    route=route,
                    automatic=True,
                    attention_routing=record.get("routing"),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                outcome.receipts[receipt_id] = self._fallback("delivery_error", template)
                continue
            if item.get("state") == "blocked":
                outcome.receipts[receipt_id] = self._fallback("revoked", template)
                continue
            notification_id = template.notification_id
            if notification_id is None:
                notification = self._notification(record, template)
                notification_id = notification["id"]
                outcome.notifications.append(notification)
            outcome.delivered.add(receipt_id)
            outcome.receipts[receipt_id] = {
                "host_delivery": {
                    "mode": "enforce",
                    "template": template.kind,
                    "state": item.get("state", "pending"),
                    "event_id": f"host-notice:{receipt_id}",
                    "notification_id": notification_id,
                }
            }
        return outcome
