"""Bounded, frozen full-source chunk plans and independently stored hot policy."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Self

from pydantic import ConfigDict, Field, model_validator

from muli.config import Section
from muli.evidence import EvidenceError
from muli.store import Store

_KIND = "memory_chunk_settings"
_ID = "current"
_MAX_RECORD_BYTES = 16_384
_SHORT_SOURCE_CHARS = 12_000
_MAX_CHUNKS = 256
_DIGEST = re.compile(r"[a-f0-9]{64}")
_PLAN_FIELDS = {
    "version",
    "source_digest",
    "content_digest",
    "content_chars",
    "chunk_chars",
    "overlap_chars",
    "count",
    "batch_size",
}


class MemoryChunkSettings(Section):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    chunk_chars: int = Field(default=2000, ge=256, le=12_000, strict=True)
    overlap_chars: int = Field(default=128, ge=0, le=2048, strict=True)
    max_chunks: int = Field(default=64, ge=1, le=_MAX_CHUNKS, strict=True)
    batch_size: int = Field(default=4, ge=1, le=16, strict=True)

    @model_validator(mode="after")
    def valid_overlap(self) -> Self:
        if self.overlap_chars >= self.chunk_chars:
            raise ValueError("overlap_chars 必须小于 chunk_chars")
        return self


class MemoryChunkSettingsStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def read(self) -> MemoryChunkSettings:
        try:
            item = self.store.bounded_object(_KIND, _ID, max_bytes=_MAX_RECORD_BYTES)
            if item is None:
                return MemoryChunkSettings()
            if (
                set(item) != {"id", "schema_version", "settings"}
                or item["id"] != _ID
                or type(item["schema_version"]) is not int
                or item["schema_version"] != 1
                or not isinstance(item["settings"], dict)
            ):
                raise ValueError("unknown memory chunk policy record")
            return MemoryChunkSettings.model_validate(item["settings"])
        except (ValueError, TypeError, RecursionError, sqlite3.Error) as error:
            raise EvidenceError(
                "memory_chunk_settings_unavailable", "记忆分块配置无法完整核对"
            ) from error

    def replace(self, settings: MemoryChunkSettings) -> MemoryChunkSettings:
        db = self.store.db
        if (
            db.in_transaction
            or db.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL
            or db.isolation_level is None
        ):
            raise EvidenceError("memory_chunk_settings_transaction", "记忆分块配置需要独立事务")
        self.read()
        checked = MemoryChunkSettings.model_validate(settings.model_dump())
        item: dict[str, Any] = {
            "id": _ID,
            "schema_version": 1,
            "settings": checked.model_dump(mode="json"),
        }
        try:
            size = len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if size > _MAX_RECORD_BYTES:
                raise ValueError("memory chunk policy record exceeds byte budget")
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise EvidenceError(
                "memory_chunk_settings_budget", "记忆分块配置超过记录预算或不是有效文本"
            ) from error
        try:
            self.store.event("memory.chunk.settings.changed", {"id": _ID}, objects=[(_KIND, item)])
        except sqlite3.Error as error:
            raise EvidenceError(
                "memory_chunk_settings_write_failed", "记忆分块配置保存失败"
            ) from error
        return checked


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("分块计划需要完整的 SHA-256 摘要")
    return value


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("分块计划包含无效整数或超出范围")
    return value


def _count(content_chars: int, chunk_chars: int, overlap_chars: int) -> int:
    if content_chars <= _SHORT_SOURCE_CHARS:
        return 0
    step = chunk_chars - overlap_chars
    return 1 + (content_chars - chunk_chars + step - 1) // step


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    version: int
    source_digest: str
    content_digest: str
    content_chars: int
    chunk_chars: int
    overlap_chars: int
    count: int
    batch_size: int

    def __post_init__(self) -> None:
        _integer(self.version, 1, 1)
        _digest(self.source_digest)
        _digest(self.content_digest)
        _integer(self.chunk_chars, 256, _SHORT_SOURCE_CHARS)
        _integer(self.overlap_chars, 0, min(2048, self.chunk_chars - 1))
        _integer(self.batch_size, 1, 16)
        _integer(self.count, 0, _MAX_CHUNKS)
        # At most max_chunks windows of max chunk_chars can cover the source.
        _integer(self.content_chars, 0, _MAX_CHUNKS * _SHORT_SOURCE_CHARS)
        if self.count != _count(self.content_chars, self.chunk_chars, self.overlap_chars):
            raise ValueError("分块计划数量不能完整覆盖指定正文")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_digest": self.source_digest,
            "content_digest": self.content_digest,
            "content_chars": self.content_chars,
            "chunk_chars": self.chunk_chars,
            "overlap_chars": self.overlap_chars,
            "count": self.count,
            "batch_size": self.batch_size,
        }


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    start_char: int
    end_char: int
    content: str
    dedup_key: str


def _check_content(content: str, plan: ChunkPlan) -> None:
    if (
        not isinstance(content, str)
        or len(content) != plan.content_chars
        or hashlib.sha256(content.encode("utf-8", errors="strict")).hexdigest()
        != plan.content_digest
    ):
        raise ValueError("分块计划正文长度或摘要已变化")


def plan_chunks(source_digest: str, content: str, settings: MemoryChunkSettings) -> ChunkPlan:
    source_digest = _digest(source_digest)
    if not isinstance(content, str):
        raise ValueError("分块正文必须是文本")
    checked = MemoryChunkSettings.model_validate(settings.model_dump())
    count = _count(len(content), checked.chunk_chars, checked.overlap_chars)
    if count > checked.max_chunks:
        raise ValueError("完整正文超过 max_chunks，未创建部分分块计划")
    return ChunkPlan(
        version=1,
        source_digest=source_digest,
        content_digest=hashlib.sha256(content.encode("utf-8", errors="strict")).hexdigest(),
        content_chars=len(content),
        chunk_chars=checked.chunk_chars,
        overlap_chars=checked.overlap_chars,
        count=count,
        batch_size=checked.batch_size,
    )


def restore_plan(value: object, source_digest: str, content: str) -> ChunkPlan:
    source_digest = _digest(source_digest)
    if not isinstance(value, dict) or set(value) != _PLAN_FIELDS:
        raise ValueError("分块 checkpoint 缺失字段或格式不受支持")
    plan = ChunkPlan(**value)
    if plan.source_digest != source_digest:
        raise ValueError("分块计划来源版本已变化")
    _check_content(content, plan)
    return plan


def iter_chunks(content: str, plan: ChunkPlan, start_index: int = 0) -> Iterator[Chunk]:
    """Yield every planned window, including whitespace, without building a list."""
    _check_content(content, plan)
    start_index = _integer(start_index, 0, plan.count)
    step = plan.chunk_chars - plan.overlap_chars
    for index in range(start_index, plan.count):
        start = index * step
        end = min(start + plan.chunk_chars, plan.content_chars)
        yield Chunk(
            index=index,
            start_char=start,
            end_char=end,
            content=content[start:end],
            dedup_key=f"source-chunk:v{plan.version}:{plan.source_digest}:{index}:{start}:{end}",
        )
