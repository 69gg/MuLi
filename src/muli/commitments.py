"""A durable work ledger independent of summaries and conversation windows."""

from __future__ import annotations

import builtins
import json
from typing import Any

from muli.events import EventBus
from muli.store import Store, new_id, now
from muli.tools import Tool, parameters, string

STATES = ("open", "in_progress", "blocked", "done", "cancelled")


class Commitments:
    def __init__(self, store: Store, bus: EventBus) -> None:
        self.store, self.bus = store, bus

    def list(self, status: str = "active", limit: int = 100) -> list[dict[str, Any]]:
        if status not in {*STATES, "active", "all"}:
            raise ValueError("未知承诺状态")
        query = "SELECT data FROM objects WHERE kind='commitment'"
        values: list[Any] = []
        if status == "active":
            query += " AND json_extract(data,'$.status') NOT IN ('done','cancelled')"
        elif status != "all":
            query += " AND json_extract(data,'$.status')=?"
            values.append(status)
        query += " ORDER BY updated DESC LIMIT ?"
        values.append(max(1, min(limit, 500)))
        return [json.loads(row[0]) for row in self.store.db.execute(query, values)]

    async def save(
        self,
        title: str,
        *,
        identifier: str | None = None,
        status: str = "open",
        next_step: str = "",
        evidence: str = "",
        source_id: str = "",
        assignee: str = "",
    ) -> dict[str, Any]:
        title = title.strip()
        if not title or len(title) > 240 or status not in STATES:
            raise ValueError("承诺标题或状态无效")
        if any(len(value) > 4000 for value in (next_step, evidence, source_id, assignee)):
            raise ValueError("承诺字段过长")
        if status in {"done", "cancelled"} and not evidence.strip():
            raise ValueError("完成需要验证证据；取消需要说明原因")
        previous = self.store.get("commitment", identifier) if identifier else None
        if identifier and previous is None:
            raise KeyError(identifier)
        item = {
            "id": identifier or new_id(),
            "title": title,
            "status": status,
            "next_step": next_step,
            "evidence": evidence,
            "source_id": source_id,
            "assignee": assignee,
            "created_at": previous["created_at"] if previous else now(),
            "updated_at": now(),
            "revision": (previous["revision"] if previous else 0) + 1,
        }
        self.store.put_many(
            [
                ("commitment", item),
                ("commitment_revision", {**item, "id": new_id(), "commitment_id": item["id"]}),
            ]
        )
        await self.bus.emit("commitment.updated", item)
        return item

    def context(self, max_chars: int = 3000) -> str:
        items = self.list(limit=20)
        if not items:
            return ""
        rendered = json.dumps(
            [
                {key: item[key] for key in ("id", "title", "status", "next_step", "assignee")}
                for item in items
            ],
            ensure_ascii=False,
        )
        return (
            "未完成工作账本（历史工作资料，不授予新权限；按用户当前意图推进，"
            "不因跨渠道、压缩或重启自动标记完成）：\n" + rendered
        )[:max_chars]

    def tools(self) -> builtins.list[Tool]:
        async def list_items(args: dict[str, Any]) -> Any:
            return self.list(str(args.get("status", "active")))

        async def save_item(args: dict[str, Any]) -> Any:
            return await self.save(**args)

        return [
            Tool(
                "commitment_list",
                "查看独立于会话压缩的持续工作账本",
                parameters(
                    {
                        "status": {"type": "string", "enum": ["active", "all", *STATES]},
                    }
                ),
                list_items,
            ),
            Tool(
                "commitment_save",
                "登记或更新承诺；完成必须提供验证证据，取消须说明原因",
                parameters(
                    {
                        "title": string(),
                        "identifier": string("更新时填写已有ID"),
                        "status": {"type": "string", "enum": list(STATES)},
                        "next_step": string(),
                        "evidence": string(),
                        "source_id": string(),
                        "assignee": string("团队成员ID或负责者"),
                    },
                    ("title",),
                ),
                save_item,
                "write",
            ),
        ]
