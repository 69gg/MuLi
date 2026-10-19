from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from muli.auth import hash_password
from muli.config import ConfigFile
from muli.memory import Memory
from muli.runtime import Runtime
from muli.server import create_app


@pytest.fixture
async def case(tmp_path: Path) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient]]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"workspace_roots": [str(tmp_path)], "updates": {"check_on_start": False}})
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime=runtime)
    # No lifespan, services, extractor, models, or external communication is started.
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            yield runtime, client
    finally:
        await runtime.close()


def extracted(memory: Memory) -> dict[str, Any]:
    source, _ = memory.register_source(
        {
            "id": "source",
            "content": "我喜欢蓝色，小蓝是我的别名。",
            "role": "user",
            "principal": "owner",
            "session_id": "original",
            "source": "web",
        },
        trusted_owner=True,
    )
    assert source is not None
    return memory.apply_extraction(
        source["id"],
        [
            {
                "content": "用户喜欢蓝色",
                "layer": "preference",
                "quote": "我喜欢蓝色",
                "entities": ["主人"],
                "relations": [],
                "confidence": 0.8,
                "claim_key": "owner.color",
            }
        ],
        aliases={"小蓝": "主人"},
    )[0]


def hidden(memory: Memory, identifier: str) -> dict[str, Any]:
    item = memory.get(identifier)
    assert item is not None
    result: dict[str, Any] = {
        key: item[key]
        for key in (
            "content",
            "kind",
            "source",
            "entities",
            "relations",
            "confidence",
            "source_id",
            "quote",
            "trusted_owner",
            "claim_key",
            "layer",
            "status",
        )
    }
    result["dedup_key"] = memory.store.db.execute(
        "SELECT dedup_key FROM memory_meta WHERE memory_id=?", (identifier,)
    ).fetchone()[0]
    result["aliases"] = [
        tuple(row)
        for row in memory.store.db.execute(
            "SELECT alias,entity_id,source_id,memory_id FROM memory_aliases WHERE memory_id=? ORDER BY alias",
            (identifier,),
        )
    ]
    return result


async def test_noop_content_patch_preserves_hidden_and_omitted_visible_fields(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    item = extracted(runtime.memory)
    before = hidden(runtime.memory, item["id"])
    revision = runtime.memory.revision
    response = await client.patch(f"/api/memories/{item['id']}", json={"content": item["content"]})
    assert response.status_code == 200
    after = hidden(runtime.memory, item["id"])
    differences = {key: (value, after[key]) for key, value in before.items() if value != after[key]}
    assert not differences, differences
    assert runtime.memory.revision == revision
    assert not runtime.memory.revisions(item["id"])


async def test_edit_superseded_fact_does_not_reactivate_or_erase_resolved_conflict(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    first = runtime.memory.save(
        "偏好红色", kind="preference", trusted_owner=True, claim_key="color"
    )
    second = runtime.memory.save(
        "偏好蓝色", kind="preference", trusted_owner=True, claim_key="color"
    )
    conflict = runtime.memory.conflicts()[0]
    runtime.memory.resolve_conflict(conflict["id"], second["id"])
    response = await client.patch(
        f"/api/memories/{first['id']}", json={"content": first["content"]}
    )
    assert response.status_code == 200
    current = runtime.memory.get(first["id"])
    assert current is not None
    records = runtime.memory.conflicts(include_resolved=True)
    assert current["status"] == "superseded" and any(
        row["id"] == conflict["id"] and row["chosen_id"] == second["id"] for row in records
    ), {"status": current["status"], "conflicts": records}


async def test_preserving_own_dedup_key_must_not_turn_edit_into_noop(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, _ = case
    item = extracted(runtime.memory)
    key = hidden(runtime.memory, item["id"])["dedup_key"]
    # This models the tempting minimal patch: pass old hidden values back to save.
    changed = runtime.memory.save(
        "用户现在喜欢绿色",
        identifier=item["id"],
        kind=item["kind"],
        source=item["source"],
        entities=item["entities"],
        relations=item["relations"],
        confidence=item["confidence"],
        layer=item["layer"],
        source_id=item["source_id"],
        quote=item["quote"],
        trusted_owner=item["trusted_owner"],
        claim_key=item["claim_key"],
        dedup_key=key,
    )
    assert changed["content"] == "用户现在喜欢绿色"


async def test_current_unresolved_conflict_is_not_resolved_by_noop_patch(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    first = runtime.memory.save(
        "偏好红色", kind="preference", trusted_owner=True, claim_key="color"
    )
    second = runtime.memory.save(
        "偏好蓝色", kind="preference", trusted_owner=True, claim_key="color"
    )
    before = runtime.memory.conflicts(include_resolved=False)[0]
    payload = {
        key: first[key]
        for key in ("content", "kind", "source", "entities", "relations", "confidence")
    }
    response = await client.patch(f"/api/memories/{first['id']}", json=payload)
    assert response.status_code == 200
    after = runtime.memory.conflicts(include_resolved=False)
    assert [row["id"] for row in after] == [before["id"]]
    assert runtime.memory.get(second["id"])["status"] == "conflict"


async def test_retained_quote_is_original_evidence_not_proof_of_edited_claim(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    item = extracted(runtime.memory)
    response = await client.patch(f"/api/memories/{item['id']}", json={"content": "我讨厌蓝色"})
    assert response.status_code == 200
    changed = response.json()
    original = runtime.memory.source(changed["source_id"])
    assert original is not None
    assert changed["quote"] in original["content"]
    assert changed["content"] not in original["content"]
    assert original["content"] == "我喜欢蓝色，小蓝是我的别名。"
    assert runtime.memory.revisions(item["id"])[0]["snapshot"]["content"] == item["content"]


async def test_edit_preserves_extraction_dedup_aliases_and_forgetting(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    item = extracted(runtime.memory)
    before = hidden(runtime.memory, item["id"])
    response = await client.patch(
        f"/api/memories/{item['id']}", json={"content": "用户现在喜欢绿色", "source": "更正标签"}
    )
    assert response.status_code == 200
    after = hidden(runtime.memory, item["id"])
    for key in (
        "aliases",
        "dedup_key",
        "layer",
        "trusted_owner",
        "claim_key",
        "source_id",
        "quote",
    ):
        assert after[key] == before[key]
    assert after["content"] == "用户现在喜欢绿色"
    assert extracted(runtime.memory)["content"] == "用户现在喜欢绿色"
    assert len(runtime.memory.search("")) == 1
    events = runtime.store.events(0)
    event = next(event for event in events if event["type"] == "memory.updated")
    assert event["data"] == {"memory_ids": [item["id"]], "source_id": "source"}
    assert (await client.delete(f"/api/memories/{item['id']}")).json()["deleted"]
    assert runtime.memory.source("source") is None
    assert runtime.memory.revisions(item["id"]) == []
    assert runtime.store.events(0)[-1]["type"] == "memory.forgotten"
    assert (await client.get(f"/api/memories/{item['id']}/source")).status_code == 404


async def test_kind_changes_are_explicit_and_cannot_forge_host_provenance(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    item = runtime.memory.save("资料", kind="fact", layer="identity", trusted_owner=False)
    url = f"/api/memories/{item['id']}"
    same = await client.patch(url, json={"content": "更正资料", "kind": "fact"})
    assert same.json()["layer"] == "identity"
    changed = await client.patch(url, json={"content": "更正资料", "kind": "procedure"})
    assert changed.json()["layer"] == "procedural"
    before = runtime.memory.get(item["id"])
    for field, value in (
        ("trusted_owner", True),
        ("source_id", "forged"),
        ("quote", "假引文"),
        ("claim_key", "owner"),
    ):
        response = await client.patch(url, json={"content": "资料", field: value})
        assert response.status_code == 422
    invalid = await client.patch(url, json={"content": "资料", "relations": [{"subject": "bad"}]})
    assert invalid.status_code == 400
    assert runtime.memory.get(item["id"]) == before


async def test_source_detail_and_paging_require_auth_and_preserve_actual_principal(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    source, _ = runtime.memory.register_source(
        {
            "id": "attention-source",
            "content": "先前提醒原文🙂\0" * 500,
            "role": "user",
            "source": "attention",
            "principal": "system:attention",
            "session_id": "archived-session",
        }
    )
    assert source is not None
    item = runtime.memory.save(
        "摘要", source="用户可更改的标签", source_id=source["id"], quote="先前提醒原文"
    )
    url = f"/api/memories/{item['id']}"
    runtime.config.update(
        {
            "server": {"password_hash": hash_password("test-password")},
            "limits": {"max_output_chars": 1000},
        }
    )
    for endpoint in (url, url + "/source"):
        assert (await client.get(endpoint)).status_code == 401
    assert (
        await client.post("/api/auth/login", json={"password": "test-password"})
    ).status_code == 200
    detail = (await client.get(url)).json()
    assert "content" not in detail["origin"]
    assert detail["origin"]["principal"] == "system:attention"
    assert detail["origin"]["source"] == "attention" and not detail["origin"]["trusted_owner"]
    assert detail["source"] == "用户可更改的标签"
    revision = detail["origin"]["digest"]
    page_response = await client.get(url + "/source", params={"revision": revision})
    page = page_response.json()
    assert page_response.headers["cache-control"] == "no-store"
    assert page["has_more"] and page["memory_id"] == item["id"]
    assert page["principal"] == "system:attention" and not page["trusted_owner"]
    assert len(page_response.text) <= 1000
    invalid = await client.get(url + "/source", params={"revision": "old-digest"})
    assert invalid.status_code == 409 and invalid.json()["detail"]["code"] == "evidence_changed"
    assert (await client.get(url + "/source", params={"offset_bytes": -1})).status_code == 422
    assert (
        await client.get(url + "/source", params={"offset_bytes": str(2**63)})
    ).status_code == 400
    no_revision = await client.get(
        url + "/source", params={"offset_bytes": page["next_offset_bytes"]}
    )
    assert (
        no_revision.status_code == 400
        and no_revision.json()["detail"]["code"] == "invalid_arguments"
    )
    assert runtime.memory.forget_source(source["id"])
    gone = await client.get(
        url + "/source", params={"revision": revision, "offset_bytes": page["next_offset_bytes"]}
    )
    assert gone.status_code == 404
    assert "先前提醒" not in gone.text


async def test_manual_fact_has_no_invented_origin(case: tuple[Runtime, httpx.AsyncClient]) -> None:
    runtime, client = case
    item = runtime.memory.save("手动事实")
    url = f"/api/memories/{item['id']}"
    assert (await client.get(url)).json()["origin"] is None
    assert (await client.get(url + "/source")).json()["detail"]["code"] == "evidence_not_found"


async def test_removing_mentions_keeps_owned_alias_evidence_until_source_forgetting(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    item = extracted(runtime.memory)
    before = hidden(runtime.memory, item["id"])["aliases"]
    assert before
    response = await client.patch(
        f"/api/memories/{item['id']}",
        json={"content": "更正后的偏好", "entities": [], "relations": []},
    )
    assert response.status_code == 200
    assert hidden(runtime.memory, item["id"])["aliases"] == before
    runtime.memory.forget_source(item["source_id"])
    assert not runtime.store.db.execute("SELECT * FROM memory_aliases").fetchall()
    assert not runtime.store.db.execute("SELECT * FROM memory_entities").fetchall()
