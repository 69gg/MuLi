"""Resumable raw-source chunks within the existing memory and job tables."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from muli.memory import Memory
from muli.memory_chunks import (
    Chunk,
    MemoryChunkSettingsStore,
    iter_chunks,
    plan_chunks,
    restore_plan,
)


class SourceChunks:
    def __init__(self, memory: Memory) -> None:
        self.memory = memory
        self.settings = MemoryChunkSettingsStore(memory.store)

    @staticmethod
    def key(source_id: str, chunk: Chunk) -> str:
        return f"source-chunk:{source_id}:{chunk.dedup_key}"

    def new_record(self, source: dict[str, Any]) -> dict[str, Any]:
        plan = plan_chunks(source["digest"], source["content"], self.settings.read())
        return {"plan": plan.to_dict(), "next_index": 0, "memory_ids": [""] * plan.count}

    def validate(self, source: dict[str, Any], record: Any) -> list[str]:
        if not isinstance(record, dict) or set(record) != {"plan", "next_index", "memory_ids"}:
            raise ValueError("原文块进度无法完整核查")
        plan = restore_plan(record["plan"], source["digest"], source["content"])
        cursor, identifiers = record["next_index"], record["memory_ids"]
        if (
            type(cursor) is not int
            or not 0 <= cursor <= plan.count
            or not isinstance(identifiers, list)
            or len(identifiers) != plan.count
            or any(not isinstance(value, str) for value in identifiers)
            or any(identifiers[cursor:])
        ):
            raise ValueError("原文块前缀或条目集合无效")
        nonempty = [value for value in identifiers if value]
        if len(nonempty) != len(set(nonempty)):
            raise ValueError("原文块条目重复")
        for chunk in iter_chunks(source["content"], plan):
            if chunk.index >= cursor:
                break
            identifier = identifiers[chunk.index]
            if not chunk.content.strip():
                if identifier:
                    raise ValueError("空白原文块不能冒充已保存条目")
                continue
            if re.fullmatch(r"[a-f0-9]{32}", identifier) is None:
                raise ValueError("原文块条目定位无效")
            row = self.memory.store.db.execute(
                "SELECT source_id,dedup_key FROM memory_meta WHERE memory_id=?",
                (identifier,),
            ).fetchone()
            if (
                row is None
                or row["source_id"] != source["id"]
                or row["dedup_key"] != self.key(source["id"], chunk)
            ):
                raise ValueError("原文块不属于本来源版本或已不可用")
        return nonempty

    async def prepare(
        self,
        source: dict[str, Any],
        record: dict[str, Any],
        *,
        live: Callable[[], bool],
        persist: Callable[[], bool],
    ) -> list[str] | None:
        self.validate(source, record)
        plan = restore_plan(record["plan"], source["digest"], source["content"])
        for chunk in iter_chunks(source["content"], plan, start_index=record["next_index"]):
            if not live():
                return None
            if chunk.content.strip():
                item = self.memory.save(
                    chunk.content,
                    kind="observation",
                    source=source["source"],
                    layer="episodic",
                    source_id=source["id"],
                    trusted_owner=source["trusted_owner"],
                    quote=chunk.content,
                    confidence=1.0,
                    dedup_key=self.key(source["id"], chunk),
                )
                record["memory_ids"][chunk.index] = item["id"]
            record["next_index"] = chunk.index + 1
            if record["next_index"] % plan.batch_size == 0 or record["next_index"] == plan.count:
                if not live() or not persist():
                    return None
                await asyncio.sleep(0)
        return self.validate(source, record) if live() else None
