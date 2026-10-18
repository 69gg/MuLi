"""Evidence-backed lexical and graph retrieval without a resident embedding model."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from muli.store import Store, new_id, now


def terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for chunk in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(chunk[i : i + 2] for i in range(max(1, len(chunk) - 1)))
    return words


class Memory:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_terms (
                term TEXT NOT NULL,
                memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                PRIMARY KEY(term,memory_id)
            );
            CREATE INDEX IF NOT EXISTS memory_terms_id ON memory_terms(memory_id);
            CREATE INDEX IF NOT EXISTS relations_subject_folded ON relations(lower(subject));
            CREATE INDEX IF NOT EXISTS relations_object_folded ON relations(lower(object));
            CREATE TABLE IF NOT EXISTS memory_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                snapshot TEXT NOT NULL, changed_at REAL NOT NULL
            );
        """)
        # One-time bounded batches migrate pre-index memories without a model call.
        cursor = self.store.db.execute(
            "SELECT id,content,entities FROM memories WHERE id NOT IN (SELECT memory_id FROM memory_terms)"
        )
        while batch := cursor.fetchmany(100):
            with self.store.db:
                for row in batch:
                    self._index(row["id"], row["content"], json.loads(row["entities"]))

    def _index(self, identifier: str, content: str, entities: list[str]) -> None:
        self.store.db.execute("DELETE FROM memory_terms WHERE memory_id=?", (identifier,))
        self.store.db.executemany(
            "INSERT OR IGNORE INTO memory_terms VALUES(?,?)",
            ((term, identifier) for term in terms(content + " " + " ".join(entities))),
        )

    def revisions(self, identifier: str) -> list[dict[str, Any]]:
        rows = self.store.db.execute(
            "SELECT id,snapshot,changed_at FROM memory_revisions WHERE memory_id=? ORDER BY id DESC LIMIT 50",
            (identifier,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "snapshot": json.loads(row["snapshot"]),
                "changed_at": row["changed_at"],
            }
            for row in rows
        ]

    def save(
        self,
        content: str,
        kind: str = "observation",
        source: str = "user",
        entities: list[str] | None = None,
        relations: list[dict[str, str]] | None = None,
        confidence: float = 1.0,
        identifier: str | None = None,
    ) -> dict[str, Any]:
        content = content.strip()
        if not content or len(content) > 12000:
            raise ValueError("记忆内容需为 1–12000 字符")
        if kind not in {"observation", "fact", "preference", "procedure", "summary"}:
            raise ValueError("未知记忆类型")
        if not 0 <= confidence <= 1 or len(source) > 1000:
            raise ValueError("记忆来源或置信度不合法")
        if len(entities or []) > 30 or len(relations or []) > 60:
            raise ValueError("记忆关系数量超限")
        entities = list(dict.fromkeys(e.strip() for e in (entities or []) if e.strip()))
        if any(len(e) > 200 for e in entities):
            raise ValueError("实体名称过长")
        for edge in relations or []:
            if not all(
                isinstance(edge.get(key), str) and 0 < len(edge[key]) <= 200
                for key in ("subject", "predicate", "object")
            ):
                raise ValueError("关系需要 subject、predicate、object")
        timestamp = now()
        existing = self.get(identifier) if identifier else None
        if identifier and not existing:
            raise KeyError(identifier)
        identifier = identifier or new_id()
        created = existing["created_at"] if existing else timestamp
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET content=excluded.content,kind=excluded.kind,source=excluded.source,entities=excluded.entities,updated_at=excluded.updated_at,confidence=excluded.confidence",
                (
                    identifier,
                    content,
                    kind,
                    source,
                    json.dumps(entities, ensure_ascii=False),
                    created,
                    timestamp,
                    confidence,
                ),
            )
            if existing:
                self.store.db.execute(
                    "INSERT INTO memory_revisions(memory_id,snapshot,changed_at) VALUES(?,?,?)",
                    (identifier, json.dumps(existing, ensure_ascii=False), timestamp),
                )
            self._index(identifier, content, entities)
            self.store.db.execute("DELETE FROM relations WHERE memory_id=?", (identifier,))
            for edge in relations or []:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO relations VALUES(?,?,?,?)",
                    (identifier, edge["subject"], edge["predicate"], edge["object"]),
                )
        result = self.get(identifier)
        assert result is not None
        return result

    def get(self, identifier: str) -> dict[str, Any] | None:
        row = self.store.db.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["entities"] = json.loads(result["entities"])
        result["relations"] = [
            dict(edge)
            for edge in self.store.db.execute(
                "SELECT subject,predicate,object FROM relations WHERE memory_id=?", (identifier,)
            ).fetchall()
        ]
        return result

    def delete(self, identifier: str) -> bool:
        with self.store.db:
            return bool(
                self.store.db.execute("DELETE FROM memories WHERE id=?", (identifier,)).rowcount
            )

    def search(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        if not query.strip():
            rows = self.store.db.execute(
                "SELECT id FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [item for row in rows if (item := self.get(row["id"])) is not None]
        tokens = terms(query)
        if not tokens:
            return []
        # The index spans the entire archive; only ranked candidates enter RAM.
        selected_tokens = sorted(tokens)[:64]
        slots = ",".join("?" for _ in selected_tokens)
        rows = self.store.db.execute(
            f"SELECT memory_id,count(*) AS hits FROM memory_terms WHERE term IN ({slots}) GROUP BY memory_id ORDER BY hits DESC LIMIT 256",
            selected_tokens,
        ).fetchall()
        candidates = {row["memory_id"] for row in rows}
        initial = [item for identifier in candidates if (item := self.get(identifier)) is not None]
        named = {
            name.casefold()
            for item in initial
            for name in item["entities"]
            if name.casefold() in query.casefold()
        }
        graph_evidence: set[str] = set()
        for _ in range(2):
            if not named:
                break
            names = sorted(named)[:100]
            slots = ",".join("?" for _ in names)
            edges = self.store.db.execute(
                f"SELECT * FROM relations WHERE lower(subject) IN ({slots}) OR lower(object) IN ({slots}) LIMIT 256",
                [*names, *names],
            ).fetchall()
            for edge in edges:
                named.update((edge["subject"].casefold(), edge["object"].casefold()))
                graph_evidence.add(edge["memory_id"])
        candidates.update(graph_evidence)
        items = [
            item for identifier in sorted(candidates) if (item := self.get(identifier)) is not None
        ]
        ranked: list[tuple[float, dict[str, Any], set[str]]] = []
        for item in items:
            item_terms = terms(item["content"] + " " + " ".join(item["entities"]))
            overlap = len(tokens & item_terms) / max(1, len(tokens))
            graph = item["id"] in graph_evidence or bool(
                named & {e.casefold() for e in item["entities"]}
            )
            if not overlap and not graph:
                continue
            age_days = max(0, now() - item["updated_at"]) / 86400
            decay = 1 if item["kind"] in {"preference", "procedure"} else math.exp(-age_days / 180)
            score = (overlap + 0.35 * graph) * (0.75 + 0.25 * decay) * item["confidence"]
            ranked.append(
                (
                    score,
                    {
                        **item,
                        "score": round(score, 4),
                        "matched_by": "graph+text"
                        if graph and overlap
                        else "graph"
                        if graph
                        else "text",
                    },
                    item_terms,
                )
            )
        selected: list[dict[str, Any]] = []
        selected_terms: list[set[str]] = []
        while ranked and len(selected) < limit:

            def relevance(entry: tuple[float, dict[str, Any], set[str]]) -> float:
                redundancy = max(
                    (
                        len(entry[2] & previous) / max(1, len(entry[2] | previous))
                        for previous in selected_terms
                    ),
                    default=0,
                )
                return entry[0] - 0.25 * redundancy

            best = max(ranked, key=relevance)
            ranked.remove(best)
            selected.append(best[1])
            selected_terms.append(best[2])
        return selected

    def graph(self) -> dict[str, Any]:
        edges = [
            dict(row)
            for row in self.store.db.execute("SELECT * FROM relations LIMIT 1000").fetchall()
        ]
        nodes = sorted({str(edge[key]) for edge in edges for key in ("subject", "object")})
        return {"nodes": nodes, "edges": edges}
