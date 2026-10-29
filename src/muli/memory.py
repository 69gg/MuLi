"""可追溯的分层记忆、实体图、社区摘要与轻量向量索引。"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import sqlite3
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from muli.evidence import EvidenceError, decode_utf8, validate_integer
from muli.memory_schema import SCHEMA
from muli.store import Store, new_id, now
from muli.store_schema import validate_existing_schema

LAYERS = frozenset({"identity", "preference", "episodic", "semantic", "procedural"})
_MAX_QUERY_TERMS = 64
_MAX_QUERY_CHARS = 12000
_KIND_LAYER = {
    "observation": "episodic",
    "fact": "semantic",
    "preference": "preference",
    "procedure": "procedural",
    "summary": "semantic",
    "identity": "identity",
    "episodic": "episodic",
    "semantic": "semantic",
    "procedural": "procedural",
}
_LAYER_KIND = {
    "identity": "identity",
    "preference": "preference",
    "episodic": "observation",
    "semantic": "fact",
    "procedural": "procedure",
}


def normalize_entity(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def terms(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    words = set(re.findall(r"[a-z0-9_]+", text))
    for chunk in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(chunk[i : i + 2] for i in range(max(1, len(chunk) - 1)))
    return words


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def source_digest(source: Mapping[str, Any], *, trusted_owner: bool) -> str:
    """Use the same complete source identity for registration and host proofs."""
    return hashlib.sha256(
        _json(
            {
                **{
                    key: source[key]
                    for key in ("content", "source", "session_id", "role", "principal")
                },
                "trusted_owner": trusted_owner,
            }
        ).encode("utf-8")
    ).hexdigest()


def _slots(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


@dataclass(slots=True)
class _Walk:
    nodes: dict[str, int]
    edges: list[dict[str, Any]]
    parents: dict[str, dict[str, Any]]
    truncated: bool


@dataclass(frozen=True, slots=True)
class PromptSearch:
    items: list[dict[str, Any]]
    candidates_used: int
    tokens: tuple[str, ...]


class Memory:
    """所有 SQLite 操作在调用事件循环线程同步提交，事务中不等待网络。"""

    def __init__(
        self,
        store: Store,
        *,
        graph_hops: int = 2,
        max_graph_edges: int = 256,
        max_candidates: int = 256,
        max_vector_scan: int = 512,
        max_community_members: int = 32,
    ) -> None:
        self.store = store
        self.configure(
            graph_hops=graph_hops,
            max_graph_edges=max_graph_edges,
            max_candidates=max_candidates,
            max_vector_scan=max_vector_scan,
            max_community_members=max_community_members,
        )
        self.store.revalidate_format()
        if not self.store.migration_allowed:
            validate_existing_schema(self.store.db, "memory")
            return
        self.store.db.executescript(SCHEMA)
        with self.store.db:
            self.store.db.execute(
                "INSERT OR IGNORE INTO memory_meta(memory_id,layer,quote) "
                "SELECT id,CASE kind WHEN 'preference' THEN 'preference' "
                "WHEN 'procedure' THEN 'procedural' WHEN 'observation' THEN 'episodic' "
                "WHEN 'identity' THEN 'identity' ELSE 'semantic' END,content FROM memories"
            )
            self._migrate_alias_evidence()
        cursor = self.store.db.execute(
            "SELECT id,content,entities FROM memories WHERE id NOT IN "
            "(SELECT memory_id FROM memory_terms)"
        )
        while batch := cursor.fetchmany(100):
            with self.store.db:
                for row in batch:
                    self._index(row["id"], row["content"], json.loads(row["entities"]))
        cursor = self.store.db.execute(
            "SELECT DISTINCT memory_id FROM relations WHERE memory_id NOT IN "
            "(SELECT memory_id FROM memory_edges)"
        )
        while batch := cursor.fetchmany(100):
            with self.store.db:
                for row in batch:
                    item = self.get(row["memory_id"])
                    if item is not None:
                        self._sync_graph(
                            item["id"],
                            item["entities"],
                            item["relations"],
                            item["source_id"],
                            item["quote"],
                        )

    def _migrate_alias_evidence(self) -> None:
        """Copy only the one surviving legacy contribution, never infer lost assertions."""
        if self.store.db.execute(
            "SELECT 1 FROM memory_state WHERE name='alias_evidence_version'"
        ).fetchone():
            return
        migrated = self.store.db.execute(
            "INSERT OR IGNORE INTO memory_alias_evidence(alias,entity_id,memory_id,source_id) "
            "SELECT a.alias,a.entity_id,a.memory_id,a.source_id FROM memory_aliases a "
            "JOIN memory_entities e ON e.id=a.entity_id "
            "JOIN memory_meta m ON m.memory_id=a.memory_id "
            "JOIN memories i ON i.id=a.memory_id "
            "WHERE a.alias!=e.canonical AND a.source_id IS m.source_id "
            "AND (a.source_id IS NULL OR EXISTS "
            "(SELECT 1 FROM memory_sources s WHERE s.id=a.source_id))"
        )
        self.store.db.execute("INSERT INTO memory_state VALUES('alias_evidence_version',1)")
        if migrated.rowcount > 0:
            self._bump()

    def configure(
        self,
        *,
        graph_hops: int,
        max_graph_edges: int,
        max_candidates: int,
        max_vector_scan: int,
        max_community_members: int,
    ) -> None:
        values = (max_graph_edges, max_candidates, max_vector_scan, max_community_members)
        if type(graph_hops) is not int or not 0 <= graph_hops <= 8:
            raise ValueError("graph_hops 必须在 0 到 8 之间")
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("记忆检索资源限制必须是正整数")
        self.graph_hops, self.max_graph_edges = graph_hops, max_graph_edges
        self.max_candidates, self.max_vector_scan = max_candidates, max_vector_scan
        self.max_community_members = max_community_members

    @property
    def revision(self) -> int:
        return int(
            self.store.db.execute(
                "SELECT value FROM memory_state WHERE name='revision'"
            ).fetchone()[0]
        )

    def _bump(self) -> None:
        self.store.db.execute("UPDATE memory_state SET value=value+1 WHERE name='revision'")

    def _index(self, identifier: str, content: str, entities: Sequence[str]) -> None:
        self.store.db.execute("DELETE FROM memory_terms WHERE memory_id=?", (identifier,))
        self.store.db.executemany(
            "INSERT OR IGNORE INTO memory_terms VALUES(?,?)",
            ((term, identifier) for term in terms(content + " " + " ".join(entities))),
        )

    def register_source(
        self,
        message: Mapping[str, Any],
        *,
        trusted_owner: bool = False,
        max_chars: int = 64000,
    ) -> tuple[dict[str, Any] | None, bool]:
        identifier, content = message.get("id"), message.get("content")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 200:
            raise ValueError("原始来源需要有效消息 ID")
        if not isinstance(content, str) or not content.strip() or len(content) > max_chars:
            raise ValueError("原始来源内容为空或超过 max_source_chars")
        if self.store.db.execute(
            "SELECT 1 FROM memory_forgotten_sources WHERE id=?", (identifier,)
        ).fetchone():
            return None, False
        role = str(message.get("role", "user"))
        principal = str(message.get("principal", "unknown"))
        source = str(message.get("source", "conversation"))
        session_id = str(message.get("session_id", ""))
        if (
            role not in {"user", "assistant", "tool"}
            or max(len(principal), len(source), len(session_id)) > 1000
        ):
            raise ValueError("原始来源身份或渠道字段无效")
        trusted = trusted_owner is True and role == "user"
        digest = source_digest(
            {
                "content": content,
                "source": source,
                "session_id": session_id,
                "role": role,
                "principal": principal,
            },
            trusted_owner=trusted,
        )
        existing = self.source(identifier)
        if existing is not None:
            if existing["digest"] != digest:
                raise ValueError("相同来源 ID 的内容或授权不一致")
            return existing, False
        timestamp = message.get("created_at", now())
        if type(timestamp) not in {int, float} or not math.isfinite(timestamp):
            raise ValueError("来源时间必须是有限数值")
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO memory_sources VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    content,
                    source,
                    session_id,
                    role,
                    principal,
                    int(trusted),
                    digest,
                    timestamp,
                ),
            )
            self.store.db.executemany(
                "INSERT OR IGNORE INTO memory_source_terms VALUES(?,?)",
                ((term, identifier) for term in terms(content)),
            )
            self._bump()
        return self.source(identifier), True

    def source(self, identifier: str, *, max_chars: int | None = None) -> dict[str, Any] | None:
        if max_chars is None:
            row = self.store.db.execute(
                "SELECT * FROM memory_sources WHERE id=?", (identifier,)
            ).fetchone()
        else:
            if type(max_chars) is not int or max_chars <= 0:
                raise ValueError("来源节选限制必须是正整数")
            # Four bytes cover each requested Unicode code point. Keep SQLite's
            # integer binding bounded even when callers use an arbitrary-size int.
            max_bytes = min(max_chars * 4, sys.maxsize)
            # Older SQLite narrows substr's length to 32 bits. The actual BLOB
            # length is always valid, even when the requested prefix is larger.
            row = self.store.db.execute(
                "SELECT id,source,session_id,role,principal,trusted_owner,digest,created_at,"
                "substr(CAST(content AS BLOB),1,min(?,length(CAST(content AS BLOB)))) "
                "AS content_bytes,"
                "length(CAST(content AS BLOB)) AS total_bytes FROM memory_sources WHERE id=?",
                (max_bytes, identifier),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["trusted_owner"] = bool(result["trusted_owner"])
        if max_chars is not None:
            raw, total = result.pop("content_bytes"), result.pop("total_bytes")
            result["content"] = decode_utf8(raw, allow_incomplete_tail=len(raw) < total)[:max_chars]
            result["truncated"] = len(result["content"].encode("utf-8")) < total
        return result

    def search_sources(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        self._query(query)
        limit = max(1, min(limit, 100))
        if query.strip():
            tokens = sorted(terms(query))[:_MAX_QUERY_TERMS]
            if not tokens:
                return []
            rows = self.store.db.execute(
                f"SELECT source_id AS id,count(*) hits FROM memory_source_terms "
                f"WHERE term IN ({_slots(tokens)}) GROUP BY source_id ORDER BY hits DESC LIMIT ?",
                [*tokens, limit],
            ).fetchall()
        else:
            rows = self.store.db.execute(
                "SELECT id FROM memory_sources ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            item for row in rows if (item := self.source(row["id"], max_chars=1000)) is not None
        ]

    def _ensure_entity(self, name: str) -> str:
        canonical = normalize_entity(name)
        row = self.store.db.execute(
            "SELECT entity_id FROM memory_aliases WHERE alias=?", (canonical,)
        ).fetchone()
        if row is not None:
            return str(row[0])
        row = self.store.db.execute(
            "SELECT id FROM memory_entities WHERE canonical=?", (canonical,)
        ).fetchone()
        if row is None:
            identifier = new_id()
            self.store.db.execute(
                "INSERT INTO memory_entities VALUES(?,?,?)", (identifier, canonical, name.strip())
            )
        else:
            identifier = str(row[0])
        self.store.db.execute(
            "INSERT OR IGNORE INTO memory_aliases(alias,entity_id) VALUES(?,?)",
            (canonical, identifier),
        )
        return identifier

    def _aliases(
        self,
        aliases: Mapping[str, str],
        source_id: str | None,
        memory_id: str | None,
    ) -> bool:
        if len(aliases) > 30:
            raise ValueError("实体别名数量超限")
        if not aliases:
            return False
        owner = self.store.db.execute(
            "SELECT source_id FROM memory_meta WHERE memory_id=?", (memory_id,)
        ).fetchone()
        if owner is None or owner["source_id"] != source_id:
            raise ValueError("别名证明的记忆与原始来源不一致")
        changed_before = self.store.db.total_changes
        for alias, canonical in aliases.items():
            if any(
                not isinstance(value, str) or not value.strip() or len(value) > 200
                for value in (alias, canonical)
            ):
                raise ValueError("实体别名和规范名称必须是 1–200 字符")
            identifier = self._ensure_entity(canonical)
            folded = normalize_entity(alias)
            existing = self.store.db.execute(
                "SELECT entity_id FROM memory_aliases WHERE alias=?", (folded,)
            ).fetchone()
            if existing is not None and existing[0] != identifier:
                raise ValueError("实体别名已有不同规范实体，不能静默合并")
            self.store.db.execute(
                "INSERT OR IGNORE INTO memory_aliases VALUES(?,?,?,?)",
                (folded, identifier, source_id, memory_id),
            )
            canonical_row = self.store.db.execute(
                "SELECT canonical FROM memory_entities WHERE id=?", (identifier,)
            ).fetchone()
            if folded != canonical_row[0]:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO memory_alias_evidence VALUES(?,?,?,?)",
                    (folded, identifier, memory_id, source_id),
                )
                self._repair_aliases([folded])
        return self.store.db.total_changes != changed_before

    def _alias_keys(self, field: str, identifier: str) -> list[str]:
        if field not in {"memory_id", "source_id"}:
            raise ValueError("未知别名证明引用")
        return [
            str(row[0])
            for row in self.store.db.execute(
                f"SELECT alias FROM memory_alias_evidence WHERE {field}=? "
                f"UNION SELECT alias FROM memory_aliases WHERE {field}=?",
                (identifier, identifier),
            )
        ]

    def _repair_aliases(self, aliases: Sequence[str]) -> None:
        """Rebuild affected lookup rows before orphan cleanup, without owning proofs."""
        for alias in sorted(set(aliases)):
            legacy = self.store.db.execute(
                "SELECT 1 FROM memory_aliases a WHERE a.alias=? AND NOT EXISTS "
                "(SELECT 1 FROM memory_alias_evidence p JOIN memory_meta m ON m.memory_id=p.memory_id "
                "WHERE p.alias=a.alias AND p.entity_id=a.entity_id AND p.memory_id=a.memory_id "
                "AND p.source_id IS a.source_id AND p.source_id IS m.source_id)",
                (alias,),
            ).fetchone()
            if legacy is not None:
                # A legacy lookup has its own unknown lifetime. A new contribution
                # must not replace those references and implicitly revoke it later.
                continue
            row = self.store.db.execute(
                "SELECT p.entity_id,p.memory_id,p.source_id FROM memory_alias_evidence p "
                "JOIN memory_meta m ON m.memory_id=p.memory_id "
                "WHERE p.alias=? AND p.source_id IS m.source_id ORDER BY p.memory_id LIMIT 1",
                (alias,),
            ).fetchone()
            if row is not None:
                self.store.db.execute(
                    "INSERT INTO memory_aliases VALUES(?,?,?,?) ON CONFLICT(alias) DO UPDATE SET "
                    "entity_id=excluded.entity_id,source_id=excluded.source_id,memory_id=excluded.memory_id "
                    "WHERE memory_aliases.entity_id!=excluded.entity_id "
                    "OR memory_aliases.source_id IS NOT excluded.source_id "
                    "OR memory_aliases.memory_id IS NOT excluded.memory_id",
                    (alias, row["entity_id"], row["source_id"], row["memory_id"]),
                )
            else:
                # Legacy canonical rows may have owned references. Restore direct lookup
                # after their cascade; the normal orphan purge still removes unused entities.
                self.store.db.execute(
                    "INSERT OR IGNORE INTO memory_aliases(alias,entity_id) "
                    "SELECT canonical,id FROM memory_entities WHERE canonical=?",
                    (alias,),
                )

    def _replace_alias_contributions(self, memory_id: str) -> None:
        affected = self._alias_keys("memory_id", memory_id)
        self.store.db.execute("DELETE FROM memory_alias_evidence WHERE memory_id=?", (memory_id,))
        self.store.db.execute("DELETE FROM memory_aliases WHERE memory_id=?", (memory_id,))
        self._repair_aliases(affected)

    def _sync_graph(
        self,
        identifier: str,
        entities: Sequence[str],
        relations: Sequence[Mapping[str, str]],
        source_id: str | None,
        quote: str,
    ) -> None:
        self.store.db.execute("DELETE FROM memory_mentions WHERE memory_id=?", (identifier,))
        self.store.db.execute("DELETE FROM memory_edges WHERE memory_id=?", (identifier,))
        self.store.db.execute("DELETE FROM relations WHERE memory_id=?", (identifier,))
        names = [*entities, *(edge[key] for edge in relations for key in ("subject", "object"))]
        ids = {name: self._ensure_entity(name) for name in names}
        self.store.db.executemany(
            "INSERT OR IGNORE INTO memory_mentions VALUES(?,?)",
            ((identifier, entity_id) for entity_id in ids.values()),
        )
        for edge in relations:
            subject, target = ids[edge["subject"]], ids[edge["object"]]
            self.store.db.execute(
                "INSERT OR IGNORE INTO memory_edges VALUES(?,?,?,?,?,?)",
                (identifier, subject, edge["predicate"].strip(), target, source_id, quote),
            )
            self.store.db.execute(
                "INSERT OR IGNORE INTO relations VALUES(?,?,?,?)",
                (
                    identifier,
                    edge["subject"].strip(),
                    edge["predicate"].strip(),
                    edge["object"].strip(),
                ),
            )

    def _validate(
        self,
        content: str,
        kind: str,
        source: str,
        entities: Sequence[str],
        relations: Sequence[Mapping[str, str]],
        confidence: float,
        layer: str | None,
        source_id: str | None,
        quote: str,
        trusted_owner: bool,
        claim_key: str,
    ) -> tuple[str, str, bool]:
        if not isinstance(content, str) or not 0 < len(content.strip()) <= 12000:
            raise ValueError("记忆内容需为 1–12000 字符")
        if kind not in _KIND_LAYER or (layer is not None and layer not in LAYERS):
            raise ValueError("未知记忆类型或分层")
        chosen_layer = layer or _KIND_LAYER[kind]
        if not math.isfinite(confidence) or not 0 <= confidence <= 1 or len(source) > 1000:
            raise ValueError("记忆来源或置信度不合法")
        if len(entities) > 30 or len(relations) > 60:
            raise ValueError("记忆关系数量超限")
        if any(
            not isinstance(name, str) or not name.strip() or len(name) > 200 for name in entities
        ):
            raise ValueError("实体名称需为 1–200 字符")
        for edge in relations:
            if not all(
                isinstance(edge.get(key), str) and 0 < len(edge[key].strip()) <= 200
                for key in ("subject", "predicate", "object")
            ):
                raise ValueError("关系需要 subject、predicate、object")
        if len(claim_key) > 500:
            raise ValueError("互斥事实槽位过长")
        trusted = trusted_owner is True
        if source_id is not None:
            original = self.source(source_id)
            if original is None:
                raise ValueError("记忆的原始来源不存在或已忘记")
            if not quote or quote not in original["content"]:
                raise ValueError("记忆证据 quote 必须逐字来自原始来源")
            trusted = trusted and original["trusted_owner"] and original["role"] == "user"
        return content.strip(), chosen_layer, trusted

    def _save(
        self,
        *,
        content: str,
        kind: str,
        source: str,
        entities: Sequence[str],
        relations: Sequence[Mapping[str, str]],
        confidence: float,
        identifier: str | None,
        layer: str | None,
        source_id: str | None,
        trusted_owner: bool,
        quote: str,
        claim_key: str,
        aliases: Mapping[str, str],
        dedup_key: str | None,
        preserve_metadata: bool = False,
        expected_version: int | None = None,
    ) -> str:
        existing = self.get(identifier) if identifier else None
        if identifier and existing is None:
            raise KeyError(identifier)
        if expected_version is not None:
            validate_integer(expected_version, "expected_version", minimum=1)
            if existing is None or existing["version"] != expected_version:
                raise EvidenceError("evidence_changed", "记忆版本已变化，请重新核对后再更正")
        if preserve_metadata and existing is not None:
            source_id, quote = existing["source_id"], existing["quote"]
            trusted_owner, claim_key = existing["trusted_owner"], existing["claim_key"]
            layer = existing["layer"] if kind == existing["kind"] else None
            dedup_key = self.store.db.execute(
                "SELECT dedup_key FROM memory_meta WHERE memory_id=?", (identifier,)
            ).fetchone()[0]
        if existing is not None and existing["source_id"] is not None and source_id is None:
            # Editing a derived fact must not detach it from source-level forgetting.
            source_id, quote = existing["source_id"], quote or existing["quote"]
        content, chosen_layer, trusted = self._validate(
            content,
            kind,
            source,
            entities,
            relations,
            confidence,
            layer,
            source_id,
            quote,
            trusted_owner,
            claim_key,
        )
        if dedup_key is not None:
            duplicate = self.store.db.execute(
                "SELECT memory_id FROM memory_meta WHERE dedup_key=?", (dedup_key,)
            ).fetchone()
            if duplicate is not None and duplicate[0] != identifier:
                if expected_version is not None:
                    raise EvidenceError("evidence_changed", "记忆版本已变化，请重新核对后再更正")
                if self._aliases(aliases, source_id, str(duplicate[0])):
                    self._bump()
                return str(duplicate[0])
        identifier = identifier or new_id()
        if existing is not None:
            if expected_version is not None:
                # This conditional history insert is the first write. It
                # acquires SQLite's write lock and rejects a concurrent edit
                # even if it committed after our last read under DEFERRED.
                cursor = self.store.db.execute(
                    "INSERT INTO memory_revisions(memory_id,snapshot,changed_at) "
                    "SELECT ?,?,? FROM memory_meta "
                    "WHERE memory_id=? AND typeof(version)='integer' AND version=?",
                    (identifier, _json(existing), now(), identifier, expected_version),
                )
                if cursor.rowcount != 1:
                    raise EvidenceError("evidence_changed", "记忆版本已变化，请重新核对后再更正")
            self._invalidate_communities([identifier])
            if expected_version is None:
                self.store.db.execute(
                    "INSERT INTO memory_revisions(memory_id,snapshot,changed_at) VALUES(?,?,?)",
                    (identifier, _json(existing), now()),
                )
            self.store.db.execute("DELETE FROM memory_vectors WHERE memory_id=?", (identifier,))
            if not preserve_metadata:
                self._replace_alias_contributions(identifier)
                self.store.db.execute(
                    "DELETE FROM memory_conflicts WHERE left_id=? OR right_id=?",
                    (identifier, identifier),
                )
            elif content != existing["content"] or chosen_layer != existing["layer"]:
                # A correction recomputes live conflicts without erasing a past choice.
                self.store.db.execute(
                    "DELETE FROM memory_conflicts WHERE resolved_at IS NULL "
                    "AND (left_id=? OR right_id=?)",
                    (identifier, identifier),
                )
        timestamp = now()
        self.store.db.execute(
            "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "content=excluded.content,kind=excluded.kind,source=excluded.source,"
            "entities=excluded.entities,updated_at=excluded.updated_at,confidence=excluded.confidence",
            (
                identifier,
                content,
                kind,
                source,
                _json(list(dict.fromkeys(entities))),
                existing["created_at"] if existing else timestamp,
                timestamp,
                confidence,
            ),
        )
        scoped_claim = normalize_entity(claim_key)
        self.store.db.execute(
            "INSERT INTO memory_meta VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET "
            "layer=excluded.layer,source_id=excluded.source_id,trusted_owner=excluded.trusted_owner,"
            "quote=excluded.quote,claim_key=excluded.claim_key,status=excluded.status,"
            "version=excluded.version,dedup_key=excluded.dedup_key",
            (
                identifier,
                chosen_layer,
                source_id,
                int(trusted),
                quote or content,
                scoped_claim,
                existing["status"] if preserve_metadata and existing else "active",
                existing["version"] + 1 if existing else 1,
                dedup_key,
            ),
        )
        self._aliases(aliases, source_id, identifier)
        self._index(identifier, content, entities)
        self._sync_graph(identifier, entities, relations, source_id, quote or content)
        if scoped_claim and not (
            preserve_metadata and existing and existing["status"] == "superseded"
        ):
            for other in self.store.db.execute(
                "SELECT m.id,m.content FROM memories m JOIN memory_meta x ON x.memory_id=m.id "
                "WHERE x.claim_key=? AND x.layer=? AND x.trusted_owner=? "
                "AND m.id!=? AND x.status!='superseded'",
                (scoped_claim, chosen_layer, int(trusted), identifier),
            ).fetchall():
                if normalize_entity(other["content"]) != normalize_entity(content):
                    left, right = sorted((identifier, other["id"]))
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO memory_conflicts(id,left_id,right_id) VALUES(?,?,?)",
                        (new_id(), left, right),
                    )
        self._refresh_conflicts()
        self._bump()
        return identifier

    def save(
        self,
        content: str,
        kind: str = "observation",
        source: str = "user",
        entities: list[str] | None = None,
        relations: list[dict[str, str]] | None = None,
        confidence: float = 1.0,
        identifier: str | None = None,
        *,
        layer: str | None = None,
        source_id: str | None = None,
        trusted_owner: bool = False,
        quote: str = "",
        claim_key: str = "",
        aliases: dict[str, str] | None = None,
        dedup_key: str | None = None,
    ) -> dict[str, Any]:
        previous_entities = self._memory_entity_ids(identifier) if identifier else set()
        with self.store.db:
            saved = self._save(
                content=content,
                kind=kind,
                source=source,
                entities=entities or [],
                relations=relations or [],
                confidence=confidence,
                identifier=identifier,
                layer=layer,
                source_id=source_id,
                trusted_owner=trusted_owner,
                quote=quote,
                claim_key=claim_key,
                aliases=aliases or {},
                dedup_key=dedup_key,
            )
            self._rebuild_communities(previous_entities | self._memory_entity_ids(saved))
            self._purge_orphans()
        result = self.get(saved)
        assert result is not None
        return result

    def edit(
        self, identifier: str, *, expected_version: int | None = None, **changes: Any
    ) -> dict[str, Any]:
        """Correct visible fields without silently replacing host-owned provenance."""
        allowed = {"content", "kind", "source", "entities", "relations", "confidence"}
        if changes.keys() - allowed:
            raise ValueError("更正仅接受内容、类型、来源标签、实体、关系和置信度")
        if expected_version is not None:
            validate_integer(expected_version, "expected_version", minimum=1)
        with self.store.db:
            existing = self.get(identifier)
            if existing is None:
                raise KeyError(identifier)
            if expected_version is not None:
                validate_integer(existing["version"], "version", minimum=1)
                if existing["version"] != expected_version:
                    raise EvidenceError("evidence_changed", "记忆版本已变化，请重新核对后再更正")
            values = {key: changes.get(key, existing[key]) for key in allowed}
            if all(values[key] == existing[key] for key in allowed):
                return existing
            if expected_version is not None:
                validate_integer(existing["version"] + 1, "version", minimum=1)
            previous_entities = self._memory_entity_ids(identifier)
            saved = self._save(
                **values,
                identifier=identifier,
                layer=None,
                source_id=None,
                trusted_owner=False,
                quote="",
                claim_key="",
                aliases={},
                dedup_key=None,
                preserve_metadata=True,
                expected_version=expected_version,
            )
            self._rebuild_communities(previous_entities | self._memory_entity_ids(saved))
            self._purge_orphans()
        result = self.get(saved)
        assert result is not None
        return result

    def apply_extraction(
        self,
        source_id: str,
        proposals: Sequence[Mapping[str, Any]],
        aliases: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        original = self.source(source_id)
        if original is None:
            return []
        identifiers: list[str] = []
        affected: set[str] = set()
        with self.store.db:
            # No await separates the existence check and the atomic fact/graph commit.
            for proposal in proposals:
                layer = str(proposal["layer"])
                if layer in {"identity", "preference"} and not original["trusted_owner"]:
                    continue
                content = str(proposal["content"])
                key = hashlib.sha256(
                    _json([source_id, layer, normalize_entity(content)]).encode("utf-8")
                ).hexdigest()
                identifier = self._save(
                    content=content,
                    kind=_LAYER_KIND[layer],
                    source=original["source"],
                    entities=proposal.get("entities", []),
                    relations=proposal.get("relations", []),
                    confidence=proposal.get("confidence", 0.8),
                    identifier=None,
                    layer=layer,
                    source_id=source_id,
                    trusted_owner=original["trusted_owner"],
                    quote=str(proposal["quote"]),
                    claim_key=str(proposal.get("claim_key", "")),
                    aliases=aliases or {},
                    dedup_key=key,
                )
                identifiers.append(identifier)
                affected.update(self._memory_entity_ids(identifier))
            self._rebuild_communities(affected)
            self._purge_orphans()
        return [item for identifier in identifiers if (item := self.get(identifier)) is not None]

    def automatic_episode_id(self, source_id: str) -> str | None:
        """只投影仍可证明是指定来源初始自动归档副本的主键。"""
        row = self.store.db.execute(
            "SELECT x.memory_id FROM memory_meta x JOIN memories m ON m.id=x.memory_id "
            "JOIN memory_sources s ON s.id=x.source_id "
            "WHERE x.source_id=? AND x.dedup_key=? AND m.source=s.source "
            "AND m.kind='observation' AND x.layer='episodic' AND x.version=1 "
            "AND x.status='active' AND NOT EXISTS "
            "(SELECT 1 FROM memory_conflicts c WHERE c.resolved_at IS NULL "
            "AND (c.left_id=x.memory_id OR c.right_id=x.memory_id))",
            (source_id, "episodic:" + source_id),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def get(self, identifier: str | None) -> dict[str, Any] | None:
        row = self.store.db.execute(
            "SELECT m.*,x.layer,x.source_id,x.trusted_owner,x.quote,x.claim_key,x.status,x.version "
            "FROM memories m JOIN memory_meta x ON x.memory_id=m.id WHERE m.id=?",
            (identifier,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["entities"] = json.loads(result["entities"])
        result["trusted_owner"] = bool(result["trusted_owner"])
        result["relations"] = [
            dict(edge)
            for edge in self.store.db.execute(
                "SELECT subject,predicate,object FROM relations WHERE memory_id=?", (identifier,)
            ).fetchall()
        ]
        result["conflicts"] = [
            row["right_id"] if row["left_id"] == identifier else row["left_id"]
            for row in self.store.db.execute(
                "SELECT c.left_id,c.right_id FROM memory_conflicts c "
                "JOIN memory_meta l ON l.memory_id=c.left_id JOIN memory_meta r ON r.memory_id=c.right_id "
                "WHERE c.resolved_at IS NULL AND l.status!='superseded' AND r.status!='superseded' "
                "AND (c.left_id=? OR c.right_id=?)",
                (identifier, identifier),
            ).fetchall()
        ]
        return result

    def prompt_evidence(
        self, seed: Mapping[str, Any], *, max_bytes: int
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Recheck one seed and one exact conflict pair without expanding graph or sources."""
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("提示证据字节预算必须是正整数")
        identifier = seed.get("id")
        conflicts = seed.get("conflicts")
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(conflicts, list)
            or type(seed.get("version")) is not int
            or seed["version"] < 1
        ):
            return None, None
        try:
            if len(identifier.encode("utf-8")) > max_bytes:
                return None, None
        except UnicodeError:
            return None, None
        remaining = max_bytes
        for conflict in conflicts:
            if not isinstance(conflict, str) or not conflict:
                return None, None
            try:
                remaining -= len(conflict.encode("utf-8"))
            except UnicodeError:
                return None, None
            if remaining < 0:
                return None, None
        peer_id = conflicts[0] if conflicts else None
        left, right = sorted((identifier, peer_id)) if peer_id else (identifier, identifier)
        metadata = (
            "CASE WHEN typeof(m.id)='text' AND typeof(x.layer)='text' "
            "AND typeof(m.source)='text' AND typeof(x.status)='text' "
            "AND typeof(x.claim_key)='text' AND typeof(x.source_id) IN ('null','text') "
            "AND typeof(x.trusted_owner)='integer' AND typeof(x.version)='integer' "
            "THEN json_object('id',m.id,'layer',x.layer,'source',m.source,'source_id',x.source_id,"
            "'trusted_owner',x.trusted_owner,'status',x.status,"
            "'version',x.version,'claim_key',x.claim_key) END"
        )
        try:
            rows = self.store.db.execute(
                "WITH requested(slot,id) AS (SELECT 0,? UNION ALL SELECT 1,?) "
                f"SELECT r.slot,CASE WHEN length(CAST({metadata} AS BLOB))<=? "
                f"THEN CAST({metadata} AS BLOB) END AS metadata,"
                "CASE WHEN typeof(m.updated_at) IN ('integer','real') THEN m.updated_at END "
                "AS updated_at,"
                "CASE WHEN typeof(m.content)='text' THEN "
                "substr(CAST(m.content AS BLOB),1,?) END AS content,"
                "length(CAST(m.content AS BLOB)) AS content_bytes,"
                "x.source_id IS NULL OR (s.id IS NOT NULL AND "
                "(x.trusted_owner=0 OR (s.trusted_owner=1 AND s.role='user'))) AS source_valid,"
                "EXISTS(SELECT 1 FROM memory_conflicts WHERE left_id=? AND right_id=? "
                "AND resolved_at IS NULL) AS linked "
                "FROM requested r JOIN memories m ON m.id=r.id "
                "JOIN memory_meta x ON x.memory_id=m.id "
                "LEFT JOIN memory_sources s ON s.id=x.source_id",
                (identifier, peer_id, max_bytes, max_bytes, left, right),
            ).fetchall()
        except sqlite3.DatabaseError:
            return None, None
        current: dict[int, dict[str, Any]] = {}
        linked = False
        for row in rows:
            try:
                item = json.loads(row["metadata"])
                if isinstance(item, dict):
                    item["updated_at"] = row["updated_at"]
                if (
                    not isinstance(item, dict)
                    or item.get("id") != (identifier if row["slot"] == 0 else peer_id)
                    or item.get("layer") not in LAYERS
                    or item.get("status") not in {"active", "conflict"}
                    or type(item.get("trusted_owner")) is not int
                    or item["trusted_owner"] not in (0, 1)
                    or type(item.get("version")) is not int
                    or item["version"] < 1
                    or not isinstance(item.get("source"), str)
                    or not isinstance(item.get("claim_key"), str)
                    or not (item.get("source_id") is None or isinstance(item["source_id"], str))
                    or not isinstance(item.get("updated_at"), (int, float))
                    or not math.isfinite(item["updated_at"])
                    or not row["source_valid"]
                ):
                    continue
                _json(item).encode("utf-8")
                item["trusted_owner"] = bool(item["trusted_owner"])
                if item["layer"] in {"identity", "preference"} and not item["trusted_owner"]:
                    continue
                item["conflicts"] = conflicts if row["slot"] == 0 else [identifier]
                raw = row["content"]
                item["content"] = (
                    decode_utf8(raw, allow_incomplete_tail=len(raw) < row["content_bytes"])
                    + ("[节选]" if len(raw) < row["content_bytes"] else "")
                    if isinstance(raw, bytes)
                    else ""
                )
                current[row["slot"]] = item
                linked = bool(row["linked"])
            except (ValueError, TypeError, UnicodeError, EvidenceError):
                continue
        primary = current.get(0)
        if primary is None or primary["version"] != seed.get("version"):
            return None, None
        if linked and primary["status"] != "conflict":
            # A current unresolved edge contradicts this row's active status.
            # Never turn that inconsistent evidence into a normal single-sided fact.
            return None, None
        peer = current.get(1)
        if (
            primary["status"] != "conflict"
            or peer is None
            or not linked
            or peer["id"] == primary["id"]
            or peer["status"] != "conflict"
            or not primary["claim_key"]
            or any(primary[key] != peer[key] for key in ("claim_key", "layer", "trusted_owner"))
        ):
            peer = None
        elif peer is not None:
            peer["conflicts_partial"] = True
        return primary, peer

    def revisions(self, identifier: str) -> list[dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "snapshot": json.loads(row["snapshot"]),
                "changed_at": row["changed_at"],
            }
            for row in self.store.db.execute(
                "SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY id DESC LIMIT 50",
                (identifier,),
            ).fetchall()
        ]

    def _refresh_conflicts(self) -> None:
        self.store.db.execute(
            "UPDATE memory_meta SET status=CASE WHEN EXISTS "
            "(SELECT 1 FROM memory_conflicts c JOIN memory_meta l ON l.memory_id=c.left_id "
            "JOIN memory_meta r ON r.memory_id=c.right_id WHERE c.resolved_at IS NULL "
            "AND l.status!='superseded' AND r.status!='superseded' "
            "AND (c.left_id=memory_meta.memory_id OR c.right_id=memory_meta.memory_id)) "
            "THEN 'conflict' ELSE 'active' END WHERE status!='superseded'"
        )

    def conflicts(
        self,
        identifier: str | None = None,
        limit: int = 50,
        *,
        include_resolved: bool = True,
    ) -> list[dict[str, Any]]:
        rows = self.store.db.execute(
            "SELECT c.* FROM memory_conflicts c "
            "JOIN memory_meta l ON l.memory_id=c.left_id JOIN memory_meta r ON r.memory_id=c.right_id "
            "WHERE (? IS NULL OR c.left_id=? OR c.right_id=?) AND (? OR "
            "(c.resolved_at IS NULL AND l.status!='superseded' AND r.status!='superseded')) "
            "ORDER BY c.resolved_at IS NOT NULL,c.id LIMIT ?",
            (identifier, identifier, identifier, include_resolved, max(1, min(limit, 100))),
        ).fetchall()
        return [
            {**dict(row), "left": self.get(row["left_id"]), "right": self.get(row["right_id"])}
            for row in rows
        ]

    def resolve_conflict(self, conflict_id: str, chosen_id: str) -> dict[str, Any]:
        row = self.store.db.execute(
            "SELECT * FROM memory_conflicts WHERE id=?", (conflict_id,)
        ).fetchone()
        if row is None or chosen_id not in {row["left_id"], row["right_id"]}:
            raise ValueError("冲突或所选证据不存在")
        loser = row["right_id"] if chosen_id == row["left_id"] else row["left_id"]
        with self.store.db:
            affected = self._memory_entity_ids(chosen_id) | self._memory_entity_ids(loser)
            self._invalidate_communities([chosen_id, loser])
            for identifier in (chosen_id, loser):
                self.store.db.execute(
                    "INSERT INTO memory_revisions(memory_id,snapshot,changed_at) VALUES(?,?,?)",
                    (identifier, _json(self.get(identifier)), now()),
                )
            self.store.db.execute(
                "UPDATE memory_conflicts SET resolved_at=?,chosen_id=? WHERE id=?",
                (now(), chosen_id, conflict_id),
            )
            self.store.db.execute(
                "UPDATE memory_meta SET status='superseded',version=version+1 WHERE memory_id=?",
                (loser,),
            )
            self.store.db.execute(
                "UPDATE memory_meta SET status='active',version=version+1 WHERE memory_id=?",
                (chosen_id,),
            )
            self._refresh_conflicts()
            self._rebuild_communities(affected)
            self._bump()
        return {"id": conflict_id, "chosen_id": chosen_id, "superseded_id": loser}

    def _invalidate_communities(self, identifiers: Sequence[str]) -> None:
        if identifiers:
            self.store.db.execute(
                "DELETE FROM memory_communities WHERE id IN "
                f"(SELECT community_id FROM memory_community_members WHERE memory_id IN ({_slots(identifiers)}))",
                list(identifiers),
            )

    def delete(self, identifier: str) -> bool:
        item = self.get(identifier)
        if item is None:
            return False
        if item["source_id"] is not None:
            return self.forget_source(item["source_id"])
        with self.store.db:
            affected = self._memory_entity_ids(identifier)
            aliases = self._alias_keys("memory_id", identifier)
            self._invalidate_communities([identifier])
            self.store.db.execute("DELETE FROM memories WHERE id=?", (identifier,))
            self._repair_aliases(aliases)
            self._refresh_conflicts()
            self._rebuild_communities(affected)
            self._purge_orphans()
            self._bump()
        return True

    def forget_source(self, source_id: str) -> bool:
        if self.source(source_id) is None:
            return False
        identifiers = [
            row[0]
            for row in self.store.db.execute(
                "SELECT memory_id FROM memory_meta WHERE source_id=?", (source_id,)
            ).fetchall()
        ]
        affected = {
            entity for identifier in identifiers for entity in self._memory_entity_ids(identifier)
        }
        with self.store.db:
            aliases = self._alias_keys("source_id", source_id)
            for identifier in identifiers:
                aliases.extend(self._alias_keys("memory_id", identifier))
            self._invalidate_communities(identifiers)
            for identifier in identifiers:
                self.store.db.execute("DELETE FROM memories WHERE id=?", (identifier,))
            self.store.db.execute("DELETE FROM memory_sources WHERE id=?", (source_id,))
            self._repair_aliases(aliases)
            self.store.db.execute(
                "INSERT OR IGNORE INTO memory_forgotten_sources VALUES(?,?)", (source_id, now())
            )
            self._refresh_conflicts()
            self._rebuild_communities(affected)
            self._purge_orphans()
            self._bump()
        return True

    def _purge_orphans(self) -> None:
        self.store.db.execute(
            "DELETE FROM memory_entities WHERE id NOT IN (SELECT entity_id FROM memory_mentions) "
            "AND id NOT IN (SELECT subject_id FROM memory_edges) "
            "AND id NOT IN (SELECT object_id FROM memory_edges) "
            "AND id NOT IN (SELECT entity_id FROM memory_alias_evidence) "
            "AND id NOT IN (SELECT a.entity_id FROM memory_aliases a "
            "JOIN memory_entities e ON e.id=a.entity_id WHERE a.alias!=e.canonical "
            "AND (a.source_id IS NOT NULL OR a.memory_id IS NOT NULL))"
        )

    def _memory_entity_ids(self, identifier: str | None) -> set[str]:
        return {
            row[0]
            for row in self.store.db.execute(
                "SELECT entity_id FROM memory_mentions WHERE memory_id=?", (identifier,)
            ).fetchall()
        }

    @staticmethod
    def _query(query: str) -> None:
        if not isinstance(query, str) or len(query) > _MAX_QUERY_CHARS:
            raise ValueError("记忆查询长度不能超过 12000 字符")

    def source_scope(self, source_ids: Sequence[str] | None) -> tuple[str, ...] | None:
        """核验精确来源集合；无效或空范围从不退回全库。"""
        if source_ids is None:
            return None
        if (
            not isinstance(source_ids, (list, tuple))
            or not 1 <= len(source_ids) <= self.max_candidates
            or any(not isinstance(value, str) or not value for value in source_ids)
        ):
            raise ValueError("查询来源必须是限额内的非空来源 ID 列表")
        if sum(len(value) for value in source_ids) > _MAX_QUERY_CHARS:
            raise ValueError("查询来源 ID 总长度超过记忆查询上限")
        try:
            for value in source_ids:
                value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("查询来源 ID 包含无效 Unicode 字符") from error
        return tuple(sorted(set(source_ids)))

    @staticmethod
    def _scope_clause(source_ids: tuple[str, ...] | None) -> str:
        return "" if source_ids is None else f" AND m.source_id IN ({_slots(source_ids)})"

    def _seeds(self, query: str, *, source_ids: tuple[str, ...] | None = None) -> set[str]:
        folded = normalize_entity(query)
        candidates = self._seed_terms(query)
        if not candidates:
            return set()
        scope_clause = self._scope_clause(source_ids)
        if source_ids is None:
            rows = self.store.db.execute(
                f"SELECT DISTINCT entity_id FROM memory_aliases WHERE alias IN ({_slots(candidates)}) "
                "ORDER BY entity_id LIMIT ?",
                [*candidates, self.max_candidates],
            ).fetchall()
        else:
            # A global alias lookup may be owned by another source. Only actual
            # scoped contributions or canonical mentions can establish a seed.
            rows = self.store.db.execute(
                "SELECT entity_id FROM ("
                "SELECT e.id entity_id FROM memory_entities e "
                "JOIN memory_mentions mm ON mm.entity_id=e.id "
                "JOIN memory_meta m ON m.memory_id=mm.memory_id "
                f"WHERE e.canonical IN ({_slots(candidates)}) AND m.status!='superseded'"
                f"{scope_clause} UNION "
                "SELECT p.entity_id FROM memory_alias_evidence p "
                "JOIN memory_meta m ON m.memory_id=p.memory_id "
                f"WHERE p.alias IN ({_slots(candidates)}) AND p.source_id IS m.source_id "
                f"AND m.status!='superseded'{scope_clause}) ORDER BY entity_id LIMIT ?",
                [*candidates, *source_ids, *candidates, *source_ids, self.max_candidates],
            ).fetchall()
        seeds = {row[0] for row in rows}
        # Longer Chinese names may be embedded in a sentence. Inspect only indexed hits.
        tokens = sorted(terms(query))[:_MAX_QUERY_TERMS]
        if tokens:
            rows = self.store.db.execute(
                "SELECT DISTINCT e.id,e.canonical FROM memory_entities e "
                "JOIN memory_mentions mm ON mm.entity_id=e.id "
                "JOIN memory_terms t ON t.memory_id=mm.memory_id "
                + ("JOIN memory_meta m ON m.memory_id=mm.memory_id " if source_ids else "")
                + f"WHERE t.term IN ({_slots(tokens)})"
                + (f" AND m.status!='superseded'{scope_clause}" if source_ids else "")
                + " ORDER BY e.id LIMIT ?",
                [*tokens, *(source_ids or ()), self.max_candidates],
            ).fetchall()
            seeds.update(row["id"] for row in rows if row["canonical"] in folded)
        return seeds

    @staticmethod
    def _seed_terms(query: str) -> list[str]:
        folded = normalize_entity(query)
        return sorted({folded, *terms(query), *re.findall(r"[\w]+", folded)})[:_MAX_QUERY_TERMS]

    def _seed_evidence(
        self,
        query: str,
        seeds: Sequence[str],
        *,
        source_ids: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """One bounded supporting contribution per actual seed, with no source body reads."""
        if source_ids is not None:
            return self._scoped_seed_evidence(query, seeds, source_ids)
        candidates = self._seed_terms(query)
        result: list[dict[str, Any]] = []
        for identifier in seeds:
            entity = self.store.db.execute(
                "SELECT canonical,name FROM memory_entities WHERE id=?", (identifier,)
            ).fetchone()
            if entity is None:
                continue
            row = self.store.db.execute(
                "SELECT alias,memory_id,source_id FROM memory_aliases "
                f"WHERE entity_id=? AND alias IN ({_slots(candidates)}) "
                "ORDER BY alias=? ,alias LIMIT 1",
                [identifier, *candidates, entity["canonical"]],
            ).fetchone()
            alias = str(row["alias"]) if row is not None else None
            entry: dict[str, Any] = {
                "entity_id": identifier,
                "canonical": entity["canonical"],
                "name": entity["name"],
                "alias": alias,
                "kind": "direct",
                "memory_id": None,
                "source_id": None,
                "has_more": False,
            }
            if alias is not None and alias != entity["canonical"]:
                proofs = self.store.db.execute(
                    "SELECT p.memory_id,p.source_id FROM memory_alias_evidence p "
                    "JOIN memory_meta m ON m.memory_id=p.memory_id "
                    "WHERE p.alias=? AND p.entity_id=? AND p.source_id IS m.source_id "
                    "ORDER BY p.memory_id LIMIT 2",
                    (alias, identifier),
                ).fetchall()
                if proofs:
                    entry.update(
                        kind="source" if proofs[0]["source_id"] is not None else "manual",
                        memory_id=proofs[0]["memory_id"],
                        source_id=proofs[0]["source_id"],
                        has_more=len(proofs) > 1,
                    )
                else:
                    entry.update(
                        kind="legacy_unknown",
                        memory_id=row["memory_id"],
                        source_id=row["source_id"],
                    )
            result.append(entry)
        return result

    def _scoped_seed_evidence(
        self, query: str, seeds: Sequence[str], source_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        candidates = self._seed_terms(query)
        scope_clause = self._scope_clause(source_ids)
        result: list[dict[str, Any]] = []
        for identifier in seeds:
            entity = self.store.db.execute(
                "SELECT canonical,name FROM memory_entities WHERE id=?", (identifier,)
            ).fetchone()
            if entity is None:
                continue
            alias_row = self.store.db.execute(
                "SELECT p.alias FROM memory_alias_evidence p "
                "JOIN memory_meta m ON m.memory_id=p.memory_id "
                f"WHERE p.entity_id=? AND p.alias IN ({_slots(candidates)}) "
                "AND p.source_id IS m.source_id AND m.status!='superseded'"
                f"{scope_clause} ORDER BY p.alias LIMIT 1",
                [identifier, *candidates, *source_ids],
            ).fetchone()
            if alias_row is not None:
                alias = alias_row["alias"]
                proofs = self.store.db.execute(
                    "SELECT p.memory_id,p.source_id FROM memory_alias_evidence p "
                    "JOIN memory_meta m ON m.memory_id=p.memory_id "
                    "WHERE p.entity_id=? AND p.alias=? AND p.source_id IS m.source_id "
                    f"AND m.status!='superseded'{scope_clause} ORDER BY p.memory_id LIMIT 2",
                    [identifier, alias, *source_ids],
                ).fetchall()
            else:
                alias = entity["canonical"]
                proofs = self.store.db.execute(
                    "SELECT mm.memory_id,m.source_id FROM memory_mentions mm "
                    "JOIN memory_meta m ON m.memory_id=mm.memory_id "
                    f"WHERE mm.entity_id=? AND m.status!='superseded'{scope_clause} "
                    "ORDER BY mm.memory_id LIMIT 2",
                    [identifier, *source_ids],
                ).fetchall()
            if proofs:
                result.append(
                    {
                        "entity_id": identifier,
                        "canonical": entity["canonical"],
                        "name": entity["name"],
                        "alias": alias,
                        "kind": "source",
                        "memory_id": proofs[0]["memory_id"],
                        "source_id": proofs[0]["source_id"],
                        "has_more": len(proofs) > 1,
                    }
                )
        return result

    def _walk(
        self,
        seeds: set[str],
        hops: int,
        *,
        node_limit: int | None = None,
        source_ids: tuple[str, ...] | None = None,
    ) -> _Walk:
        node_limit = node_limit or self.max_candidates
        nodes = {identifier: 0 for identifier in sorted(seeds)[:node_limit]}
        frontier = set(nodes)
        seen_edges: set[tuple[str, str, str, str]] = set()
        edges: list[dict[str, Any]] = []
        parents: dict[str, dict[str, Any]] = {}
        truncated = len(seeds) > node_limit
        scope_clause = self._scope_clause(source_ids)
        valid_source = " AND e.source_id IS m.source_id" if source_ids else ""
        for depth in range(1, hops + 1):
            if not frontier or len(edges) >= self.max_graph_edges:
                truncated |= bool(frontier)
                break
            ordered = sorted(frontier)
            remaining = self.max_graph_edges - len(edges)
            rows = self.store.db.execute(
                "SELECT e.*,s.name subject,o.name object FROM memory_edges e "
                "JOIN memory_entities s ON s.id=e.subject_id "
                "JOIN memory_entities o ON o.id=e.object_id "
                "JOIN memory_meta m ON m.memory_id=e.memory_id "
                f"WHERE m.status!='superseded' AND (e.subject_id IN ({_slots(ordered)}) "
                f"OR e.object_id IN ({_slots(ordered)})){scope_clause}{valid_source} "
                "ORDER BY e.memory_id,e.subject_id,e.predicate,e.object_id LIMIT ?",
                [*ordered, *ordered, *(source_ids or ()), remaining + len(seen_edges) + 1],
            ).fetchall()
            next_frontier: set[str] = set()
            for row in rows:
                key = (row["memory_id"], row["subject_id"], row["predicate"], row["object_id"])
                if key in seen_edges:
                    continue
                if len(edges) >= self.max_graph_edges:
                    truncated = True
                    break
                unseen = {row["subject_id"], row["object_id"]} - nodes.keys()
                if len(nodes) + len(unseen) > node_limit:
                    truncated = True
                    continue
                if row["subject_id"] in frontier:
                    origin, destination, direction = row["subject_id"], row["object_id"], "forward"
                else:
                    origin, destination, direction = row["object_id"], row["subject_id"], "reverse"
                step = {
                    "memory_id": row["memory_id"],
                    "source_id": row["source_id"],
                    "subject_id": row["subject_id"],
                    "subject": row["subject"],
                    "predicate": row["predicate"],
                    "object_id": row["object_id"],
                    "object": row["object"],
                    "from_id": origin,
                    "to_id": destination,
                    "direction": direction,
                }
                seen_edges.add(key)
                edges.append({**dict(row), "depth": depth, "traversal": step})
                for identifier in unseen:
                    nodes[identifier] = depth
                    next_frontier.add(identifier)
                    parents[identifier] = step
            frontier = next_frontier
        return _Walk(nodes, edges, parents, truncated)

    @staticmethod
    def _walk_path(step: dict[str, Any], parents: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
        steps = [step]
        seed = str(step["from_id"])
        while seed in parents:
            previous = parents[seed]
            steps.append(previous)
            seed = str(previous["from_id"])
        return {"seed_id": seed, "steps": list(reversed(steps)), "complete": True}

    def traverse(
        self,
        entity: str,
        *,
        hops: int | None = None,
        source_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        self._query(entity)
        scope = self.source_scope(source_ids)
        chosen_hops = self.graph_hops if hops is None else hops
        if type(chosen_hops) is not int or not 0 <= chosen_hops <= 8:
            raise ValueError("图查询 hops 必须在 0 到 8 之间")
        revision = self.revision
        walk = self._walk(self._seeds(entity, source_ids=scope), chosen_hops, source_ids=scope)
        names = []
        for identifier, depth in walk.nodes.items():
            row = self.store.db.execute(
                "SELECT name,canonical FROM memory_entities WHERE id=?", (identifier,)
            ).fetchone()
            if row is not None:
                names.append(
                    {
                        "id": identifier,
                        "name": row["name"],
                        "canonical": row["canonical"],
                        "depth": depth,
                    }
                )
        seeds = [identifier for identifier, depth in walk.nodes.items() if depth == 0]
        return {
            "nodes": names,
            "edges": walk.edges,
            "hops": chosen_hops,
            "truncated": walk.truncated,
            "revision": revision,
            "alias_evidence": self._seed_evidence(entity, seeds, source_ids=scope),
            "paths": [self._walk_path(edge["traversal"], walk.parents) for edge in walk.edges],
        }

    def graph(self) -> dict[str, Any]:
        rows = self.store.db.execute(
            "SELECT e.memory_id,e.source_id,e.quote,s.name subject,e.predicate,o.name object "
            "FROM memory_edges e JOIN memory_entities s ON s.id=e.subject_id "
            "JOIN memory_entities o ON o.id=e.object_id "
            "JOIN memory_meta m ON m.memory_id=e.memory_id WHERE m.status!='superseded' "
            "ORDER BY e.memory_id LIMIT ?",
            (self.max_graph_edges + 1,),
        ).fetchall()
        edges = [dict(row) for row in rows[: self.max_graph_edges]]
        return {
            "nodes": sorted({edge[key] for edge in edges for key in ("subject", "object")}),
            "edges": edges,
            "truncated": len(rows) > self.max_graph_edges,
        }

    def search(
        self,
        query: str = "",
        limit: int = 20,
        *,
        hops: int | None = None,
        layer: str | None = None,
        source_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._search_result(
            query, limit, hops=hops, layer=layer, source_ids=source_ids
        ).items

    def search_for_prompt(self, query: str, limit: int, *, hops: int | None = None) -> PromptSearch:
        return self._search_result(query, limit, hops=hops)

    def _lexical_scores(
        self,
        tokens: Sequence[str],
        limit: int,
        *,
        source_ids: tuple[str, ...] | None = None,
    ) -> dict[str, float]:
        if not tokens or limit <= 0:
            return {}
        rows = self.store.db.execute(
            "SELECT t.memory_id,count(*) hits FROM memory_terms t "
            "JOIN memory_meta m ON m.memory_id=t.memory_id "
            f"WHERE m.status!='superseded' AND t.term IN ({_slots(tokens)})"
            f"{self._scope_clause(source_ids)} "
            "GROUP BY t.memory_id ORDER BY hits DESC,t.memory_id LIMIT ?",
            [*tokens, *(source_ids or ()), limit],
        ).fetchall()
        return {row["memory_id"]: 2 * row["hits"] / len(tokens) for row in rows}

    def _rank_search(
        self,
        scores: Mapping[str, float],
        matched: Mapping[str, str],
        graph_paths: Mapping[str, dict[str, Any]],
        limit: int,
        *,
        layer: str | None = None,
    ) -> list[dict[str, Any]]:
        ranked = []
        timestamp = now()
        for identifier, score in scores.items():
            item = self.get(identifier)
            if item is None or (layer is not None and item["layer"] != layer):
                continue
            age_days = max(0.0, timestamp - item["updated_at"]) / 86400
            score += 0.15 * item["confidence"] + 0.05 / (1 + age_days)
            entry = {**item, "score": score, "matched_by": matched[identifier]}
            if identifier in graph_paths:
                entry["graph_via"] = graph_paths[identifier]
            ranked.append(entry)
        return sorted(ranked, key=lambda item: (-item["score"], -item["updated_at"], item["id"]))[
            :limit
        ]

    def task_prompt_candidates(
        self,
        query: str,
        *,
        candidate_limit: int,
        excluded_ids: frozenset[str],
        current_tokens: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        self._query(query)
        remaining_terms = max(0, _MAX_QUERY_TERMS - len(current_tokens))
        tokens = sorted(terms(query) - set(current_tokens))[:remaining_terms]
        limit = max(0, min(candidate_limit, self.max_candidates))
        # Excluded or invalid rows consume this one SQL slice; never refill it.
        scores = self._lexical_scores(tokens, limit)
        eligible = {
            identifier: score
            for identifier, score in scores.items()
            if identifier not in excluded_ids
        }
        return self._rank_search(eligible, dict.fromkeys(eligible, "lexical"), {}, limit)

    def _search_result(
        self,
        query: str,
        limit: int,
        *,
        hops: int | None = None,
        layer: str | None = None,
        source_ids: Sequence[str] | None = None,
    ) -> PromptSearch:
        self._query(query)
        scope = self.source_scope(source_ids)
        if layer is not None and layer not in LAYERS:
            raise ValueError("未知记忆分层")
        limit = max(1, min(limit, self.max_candidates))
        scores: dict[str, float] = {}
        matched: dict[str, str] = {}
        graph_paths: dict[str, dict[str, Any]] = {}
        tokens = tuple(sorted(terms(query))[:_MAX_QUERY_TERMS])
        if not query.strip():
            rows = self.store.db.execute(
                "SELECT memory_id FROM memory_meta m JOIN memories i ON i.id=m.memory_id "
                f"WHERE m.status!='superseded'{self._scope_clause(scope)} "
                "ORDER BY i.updated_at DESC LIMIT ?",
                [*(scope or ()), self.max_candidates],
            ).fetchall()
            scores = {row[0]: 0.0 for row in rows}
            matched = dict.fromkeys(scores, "recent")
        elif tokens:
            scores = self._lexical_scores(tokens, self.max_candidates, source_ids=scope)
            matched = dict.fromkeys(scores, "lexical")
        if query.strip():
            graph = self.traverse(query, hops=hops, source_ids=scope)
            seed_evidence = {entry["entity_id"]: entry for entry in graph["alias_evidence"]}
            for edge, path in zip(graph["edges"], graph["paths"], strict=True):
                identifier = edge["memory_id"]
                if identifier not in scores and len(scores) < self.max_candidates:
                    scores[identifier] = 0.5 / edge["depth"]
                    matched[identifier] = "graph"
                    graph_paths[identifier] = {
                        **path,
                        "revision": graph["revision"],
                        "seed": seed_evidence[path["seed_id"]],
                    }
        return PromptSearch(
            self._rank_search(scores, matched, graph_paths, limit, layer=layer),
            len(scores),
            tokens,
        )

    def _community_projection(
        self, walk: _Walk, *, source_ids: tuple[str, ...] | None = None
    ) -> dict[str, Any] | None:
        """Build one bounded community from current members without writing it."""
        ordered = sorted(walk.nodes)
        if not ordered:
            return None
        rows = self.store.db.execute(
            "SELECT DISTINCT m.memory_id FROM memory_mentions mm "
            "JOIN memory_meta m ON m.memory_id=mm.memory_id "
            f"WHERE m.status!='superseded' AND mm.entity_id IN ({_slots(ordered)})"
            f"{self._scope_clause(source_ids)} ORDER BY m.memory_id LIMIT ?",
            [*ordered, *(source_ids or ()), 2 * self.max_community_members + 1],
        ).fetchall()
        items = [
            item
            for row in rows[: 2 * self.max_community_members]
            if (item := self.get(row[0])) is not None
        ]
        if not items:
            return None
        entity_rows = self.store.db.execute(
            f"SELECT name FROM memory_entities WHERE id IN ({_slots(ordered)}) ORDER BY canonical",
            ordered,
        ).fetchall()
        names = [str(row[0]) for row in entity_rows]
        identity = _json(
            ordered if source_ids is None else {"nodes": ordered, "sources": source_ids}
        )
        identifier = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if source_ids is not None:
            identifier = "scoped:" + identifier
        return {
            "id": identifier,
            "title": "、".join(names[:4]),
            "summary": "\n".join(f"[{item['id']}] {item['content'][:180]}" for item in items),
            "entities": names,
            "evidence": [item["id"] for item in items],
            "source_ids": sorted(
                {item["source_id"] for item in items if item["source_id"] is not None}
            ),
            "truncated": walk.truncated or len(rows) > 2 * self.max_community_members,
            "updated_at": now()
            if source_ids is None
            else max(item["updated_at"] for item in items),
        }

    def _rebuild_communities(self, affected: set[str]) -> None:
        visited: set[str] = set()
        for seed in sorted(affected):
            if seed in visited:
                continue
            walk = self._walk(
                {seed},
                self.max_community_members,
                node_limit=self.max_community_members,
            )
            visited.update(walk.nodes)
            ordered = sorted(walk.nodes)
            if not ordered:
                continue
            self.store.db.execute(
                "DELETE FROM memory_communities WHERE id IN (SELECT community_id "
                f"FROM memory_community_nodes WHERE entity_id IN ({_slots(ordered)}))",
                ordered,
            )
            community = self._community_projection(walk)
            if community is None:
                continue
            identifier = community["id"]
            self.store.db.execute(
                "INSERT INTO memory_communities VALUES(?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    community["title"],
                    community["summary"],
                    _json(community["entities"]),
                    _json(community["evidence"]),
                    _json(community["source_ids"]),
                    int(community["truncated"]),
                    community["updated_at"],
                ),
            )
            self.store.db.executemany(
                "INSERT INTO memory_community_members VALUES(?,?)",
                ((identifier, item) for item in community["evidence"]),
            )
            self.store.db.executemany(
                "INSERT INTO memory_community_nodes VALUES(?,?)",
                ((identifier, item) for item in ordered),
            )

    def communities(self, query: str = "", limit: int = 10) -> list[dict[str, Any]]:
        self._query(query)
        limit = max(1, min(limit, self.max_candidates))
        if query.strip():
            seeds = sorted(self._seeds(query))
            if not seeds:
                return []
            rows = self.store.db.execute(
                "SELECT DISTINCT c.* FROM memory_communities c JOIN memory_community_nodes n "
                f"ON n.community_id=c.id WHERE n.entity_id IN ({_slots(seeds)}) "
                "ORDER BY c.updated_at DESC LIMIT ?",
                [*seeds, limit],
            ).fetchall()
        else:
            rows = self.store.db.execute(
                "SELECT * FROM memory_communities ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._community(dict(row)) for row in rows]

    @staticmethod
    def _community(item: dict[str, Any]) -> dict[str, Any]:
        for key in ("entities", "evidence", "source_ids"):
            item[key] = json.loads(item[key])
        item["truncated"] = bool(item["truncated"])
        return item

    def communities_for(
        self,
        identifiers: Sequence[str],
        limit: int = 4,
        *,
        source_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        scope = self.source_scope(source_ids)
        chosen = list(dict.fromkeys(identifiers))[: self.max_candidates]
        if not chosen:
            return []
        if scope is not None:
            return self._scoped_communities(chosen, scope, max(1, limit))
        rows = self.store.db.execute(
            "SELECT DISTINCT c.* FROM memory_communities c JOIN memory_community_members m "
            f"ON m.community_id=c.id WHERE m.memory_id IN ({_slots(chosen)}) "
            "ORDER BY c.updated_at DESC LIMIT ?",
            [*chosen, max(1, limit)],
        ).fetchall()
        return [self._community(dict(row)) for row in rows]

    def _scoped_communities(
        self, identifiers: Sequence[str], source_ids: tuple[str, ...], limit: int
    ) -> list[dict[str, Any]]:
        rows = self.store.db.execute(
            "SELECT DISTINCT mm.entity_id FROM memory_mentions mm "
            "JOIN memory_meta m ON m.memory_id=mm.memory_id "
            f"WHERE mm.memory_id IN ({_slots(identifiers)}) AND m.status!='superseded'"
            f"{self._scope_clause(source_ids)} ORDER BY mm.entity_id LIMIT ?",
            [*identifiers, *source_ids, self.max_candidates + 1],
        ).fetchall()
        visited: set[str] = set()
        result: list[dict[str, Any]] = []
        for row in rows[: self.max_candidates]:
            seed = row["entity_id"]
            if seed in visited:
                continue
            walk = self._walk(
                {seed},
                self.max_community_members,
                node_limit=self.max_community_members,
                source_ids=source_ids,
            )
            walk.truncated |= len(rows) > self.max_candidates
            visited.update(walk.nodes)
            community = self._community_projection(walk, source_ids=source_ids)
            if community is not None:
                result.append(community)
                if len(result) >= limit:
                    break
        return result

    @staticmethod
    def _vector(vector: Sequence[float]) -> tuple[list[float], float]:
        if (
            not vector
            or len(vector) > 65536
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in vector
            )
        ):
            raise ValueError("向量必须是有界的有限数值序列")
        values = [float(value) for value in vector]
        norm = math.sqrt(sum(value * value for value in values))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("向量范数必须是有限正数")
        return values, norm

    def store_vector(
        self,
        identifier: str,
        vector: Sequence[float],
        model_key: str,
        *,
        expected_version: int | None = None,
    ) -> bool:
        values, norm = self._vector(vector)
        item = self.get(identifier)
        if item is None or (expected_version is not None and expected_version != item["version"]):
            return False
        if not isinstance(model_key, str) or not model_key or len(model_key) > 1000:
            raise ValueError("向量模型标识无效")
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO memory_vectors VALUES(?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET "
                "model_key=excluded.model_key,dimensions=excluded.dimensions,norm=excluded.norm,vector=excluded.vector",
                (identifier, model_key, len(values), norm, _json(values)),
            )
            self._bump()
        return True

    def vector_count(self, model_key: str, *, source_ids: Sequence[str] | None = None) -> int:
        scope = self.source_scope(source_ids)
        return int(
            self.store.db.execute(
                "SELECT count(*) FROM memory_vectors v JOIN memory_meta m ON m.memory_id=v.memory_id "
                f"WHERE v.model_key=? AND m.status!='superseded'{self._scope_clause(scope)}",
                [model_key, *(scope or ())],
            ).fetchone()[0]
        )

    def vector_search(
        self,
        vector: Sequence[float],
        model_key: str,
        limit: int = 20,
        *,
        source_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        scope = self.source_scope(source_ids)
        values, norm = self._vector(vector)
        limit = max(1, min(limit, self.max_candidates))
        cursor = self.store.db.execute(
            "SELECT v.* FROM memory_vectors v JOIN memory_meta m ON m.memory_id=v.memory_id "
            "JOIN memories i ON i.id=v.memory_id WHERE v.model_key=? AND v.dimensions=? "
            f"AND m.status!='superseded'{self._scope_clause(scope)} "
            "ORDER BY i.updated_at DESC,v.memory_id LIMIT ?",
            [model_key, len(values), *(scope or ()), self.max_vector_scan],
        )
        best: list[tuple[float, str]] = []
        for row in cursor:
            stored = json.loads(row["vector"])
            score = sum(left * right for left, right in zip(values, stored, strict=True)) / (
                norm * row["norm"]
            )
            candidate = (max(-1.0, min(1.0, score)), row["memory_id"])
            if len(best) < limit:
                heapq.heappush(best, candidate)
            elif candidate > best[0]:
                heapq.heapreplace(best, candidate)
        return [
            {**item, "score": score, "matched_by": "semantic"}
            for score, identifier in sorted(best, reverse=True)
            if (item := self.get(identifier)) is not None
        ]

    def owner_profile(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.store.db.execute(
            "SELECT m.memory_id FROM memory_meta m JOIN memories i ON i.id=m.memory_id "
            "WHERE m.trusted_owner=1 AND m.layer IN ('identity','preference') "
            "AND m.status!='superseded' ORDER BY i.updated_at DESC LIMIT ?",
            (max(1, min(limit, self.max_candidates)),),
        ).fetchall()
        return [item for row in rows if (item := self.get(row[0])) is not None]
