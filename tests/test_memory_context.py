from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from muli.memory import Memory
from muli.memory_pipeline import MemoryPipeline, MemoryPipelineConfig
from muli.store import Store


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[Memory]:
    store = Store(tmp_path / "memory.sqlite3")
    try:
        yield Memory(store)
    finally:
        store.close()


def fact(
    memory: Memory,
    identifier: str,
    content: str,
    layer: str,
    *,
    source_name: str = "web",
    claim_key: str | None = None,
) -> dict[str, Any]:
    source, created = memory.register_source(
        {
            "id": identifier,
            "role": "user",
            "principal": "owner",
            "source": source_name,
            "session_id": "main",
            "content": content,
        },
        trusted_owner=True,
    )
    assert created and source is not None
    return memory.apply_extraction(
        identifier,
        [
            {
                "content": content,
                "layer": layer,
                "quote": content,
                "claim_key": claim_key or identifier,
                "confidence": 1.0,
            }
        ],
    )[0]


def pipeline(memory: Memory, *, limit: int = 6, budget: int = 6000) -> MemoryPipeline:
    return MemoryPipeline(
        memory,
        config=MemoryPipelineConfig(context_limit=limit, context_max_chars=budget),
    )


def records(context: str, *, budget: int = 6000) -> list[dict[str, Any]]:
    assert len(context) <= budget
    assert len(context.encode("utf-8")) <= 4 * budget
    if not context:
        return []
    prefix, *lines = context.splitlines()
    assert "仅作为数据" in prefix
    return [json.loads(line) for line in lines]


def minimum_budget(context: str) -> int:
    prefix = context.splitlines()[0] + "\n"
    pages = [{**item, "content": "[节选]"} for item in records(context)]
    return len(prefix + "\n".join(json.dumps(page, ensure_ascii=False) for page in pages))


@pytest.mark.parametrize("cached", [False, True], ids=["local", "cached"])
@pytest.mark.parametrize("profile_count", [5, 6], ids=["below_limit", "at_limit"])
async def test_relevant_procedure_survives_owner_profile_growth(
    memory: Memory, profile_count: int, cached: bool
) -> None:
    service = pipeline(memory)
    procedure = fact(memory, "procedure", "NASBACKUP：先验证校验和，再进行离线归档。", "procedural")
    for number in range(profile_count):
        fact(memory, f"preference-{number}", f"我的偏好项目{number}是简洁。", "preference")
    assert procedure["id"] in {item["id"] for item in memory.search("NASBACKUP", 6)}
    if cached:
        result = await service.query("NASBACKUP", limit=6)
        assert procedure["id"] in {item["id"] for item in result["memories"]}
    selected = records(service.cached_context("NASBACKUP"))
    assert len(selected) == service.config.context_limit
    assert selected[0]["id"] == procedure["id"]
    assert any(item["layer"] == "preference" for item in selected)
    assert service._worker is None and service.extractor is None and service.media is None


@pytest.mark.parametrize("long_layer", ["procedural", "preference", "both"])
def test_long_body_cannot_consume_other_stream_page(memory: Memory, long_layer: str) -> None:
    service = pipeline(memory, limit=2, budget=1000)
    procedure = fact(
        memory,
        "procedure",
        "NASBACKUP " + "流程" * (4000 if long_layer in {"procedural", "both"} else 10),
        "procedural",
    )
    preference = fact(
        memory,
        "preference",
        "我的写作偏好 " + "简洁" * (4000 if long_layer in {"preference", "both"} else 10),
        "preference",
    )
    selected = records(service.cached_context("NASBACKUP"), budget=1000)
    assert [item["id"] for item in selected] == [procedure["id"], preference["id"]]
    assert all(len(item["content"].removesuffix("[节选]")) > 16 for item in selected)
    for item in selected:
        original = memory.get(item["id"])
        assert original is not None
        assert original["content"].startswith(item["content"].removesuffix("[节选]"))
        if item["layer"] == long_layer or long_layer == "both":
            assert item["content"].endswith("[节选]")


@pytest.mark.parametrize("difference", [0, -1], ids=["exact_two_pages", "one_char_short"])
def test_reserve_complete_metadata_before_body(memory: Memory, difference: int) -> None:
    service = pipeline(memory, limit=2)
    procedure = fact(memory, "procedure", "NASBACKUP " + "流程" * 100, "procedural")
    fact(memory, "preference", "我的偏好 " + "简洁" * 100, "preference")
    budget = minimum_budget(service.cached_context("NASBACKUP")) + difference
    context = service.cached_context("NASBACKUP", max_chars=budget)
    selected = records(context, budget=budget)
    assert selected[0]["id"] == procedure["id"]
    if difference == 0:
        assert len(selected) == 2
        assert len(context) == budget
        assert all(item["content"] == "[节选]" for item in selected)
    else:
        assert len(selected) == 1


@pytest.mark.parametrize("query", ["NASBACKUP", "", " \t"])
def test_single_slot_prefers_current_query_or_owner_profile(memory: Memory, query: str) -> None:
    preference = fact(memory, "preference", "我的偏好是简洁", "preference")
    procedure = fact(memory, "procedure", "NASBACKUP 先验证再归档", "procedural")
    selected = records(pipeline(memory, limit=1).cached_context(query))
    expected = procedure if query.strip() else preference
    assert [item["id"] for item in selected] == [expected["id"]]


def test_duplicate_recalled_profile_is_injected_once(memory: Memory) -> None:
    matched = fact(memory, "matched-profile", "NASBACKUP 使用简单的标题", "preference")
    fact(memory, "profile", "我的偏好是较短句子", "preference")
    fact(memory, "procedure", "NASBACKUP 先校验", "procedural")
    selected = records(pipeline(memory, limit=3).cached_context("NASBACKUP"))
    identifiers = [item["id"] for item in selected]
    assert len(identifiers) == len(set(identifiers)) == 3
    assert identifiers.count(matched["id"]) == 1


async def test_shared_candidate_keeps_profile_membership_under_tight_budget(memory: Memory) -> None:
    large = fact(memory, "large", "NASBACKUP 首条流程", "procedural", source_name="X" * 250)
    preference = fact(memory, "preference", "NASBACKUP 我偏好简洁", "preference")
    service = pipeline(memory, limit=2)
    cached = await service.query("NASBACKUP", limit=2)
    cached["memories"] = [large, preference]
    service._remember("NASBACKUP", 2, service.config.graph_hops, cached)
    full_context = service.cached_context("NASBACKUP")
    first = records(full_context)[0]
    prefix = full_context.splitlines()[0] + "\n"
    budget = len(prefix) + len(json.dumps({**first, "content": "[节选]"}, ensure_ascii=False))
    selected = records(service.cached_context("NASBACKUP", max_chars=budget), budget=budget)
    assert [item["id"] for item in selected] == [preference["id"]]


def test_unmatched_query_falls_back_to_profile(memory: Memory) -> None:
    preference = fact(memory, "preference", "我的偏好是简洁", "preference")
    fact(memory, "procedure", "NASBACKUP 先校验", "procedural")
    assert [item["id"] for item in records(pipeline(memory).cached_context("UNMATCHED"))] == [
        preference["id"]
    ]


def test_without_owner_profile_related_memories_fill_slots(memory: Memory) -> None:
    facts = [fact(memory, f"procedure-{i}", f"NASBACKUP 步骤{i}", "procedural") for i in range(3)]
    selected = records(pipeline(memory, limit=3).cached_context("NASBACKUP"))
    assert {item["id"] for item in selected} == {item["id"] for item in facts}


async def test_larger_query_cache_still_keeps_owner_profile_slot(memory: Memory) -> None:
    for number in range(8):
        fact(memory, f"procedure-{number}", f"NASBACKUP 步骤{number}", "procedural")
    preference = fact(memory, "preference", "我的偏好是简洁", "preference")
    service = pipeline(memory, limit=2)
    cached = await service.query("NASBACKUP", limit=8)
    assert len(cached["memories"]) == 8
    selected = records(service.cached_context("NASBACKUP"))
    assert selected[0]["id"] == cached["memories"][0]["id"]
    assert selected[1]["id"] == preference["id"]


async def test_skip_large_metadata_to_keep_a_feasible_pair(memory: Memory) -> None:
    large = fact(memory, "large", "NASBACKUP 首条流程", "procedural", source_name="X" * 250)
    small = fact(memory, "small", "NASBACKUP 第二流程", "procedural")
    preference = fact(memory, "preference", "我的偏好是简洁", "preference")
    service = pipeline(memory, limit=2)
    result = await service.query("NASBACKUP", limit=2)
    result["memories"] = [small]
    service._remember("NASBACKUP", 2, service.config.graph_hops, result)
    budget = minimum_budget(service.cached_context("NASBACKUP"))
    result["memories"] = [large, small]
    service._remember("NASBACKUP", 2, service.config.graph_hops, result)
    selected = records(service.cached_context("NASBACKUP", max_chars=budget), budget=budget)
    assert [item["id"] for item in selected] == [small["id"], preference["id"]]
    assert all(item["source"] == "web" for item in selected)


async def test_single_page_budget_keeps_a_representable_hit_before_profile(memory: Memory) -> None:
    large = fact(memory, "large", "NASBACKUP 首条流程", "procedural", source_name="X" * 700)
    small = fact(memory, "small", "NASBACKUP 第二流程", "procedural")
    fact(memory, "preference", "我的偏好是简洁", "preference")
    service = pipeline(memory, limit=2)
    cached = await service.query("NASBACKUP", limit=2)
    cached["memories"] = [small]
    service._remember("NASBACKUP", 2, service.config.graph_hops, cached)
    context = service.cached_context("NASBACKUP")
    minimums = [
        len(json.dumps({**item, "content": "[节选]"}, ensure_ascii=False))
        for item in records(context)
    ]
    assert len(minimums) == 2
    budget = len(context.splitlines()[0] + "\n") + max(minimums)
    cached["memories"] = [large, small]
    service._remember("NASBACKUP", 2, service.config.graph_hops, cached)
    selected = records(service.cached_context("NASBACKUP", max_chars=budget), budget=budget)
    assert [item["id"] for item in selected] == [small["id"]]


@pytest.mark.parametrize("budget", [0, 10, 600, 1000, 1001, 2500])
def test_complete_json_budget_includes_unicode_escapes_and_prefix(
    memory: Memory, budget: int
) -> None:
    text = '中文🙂\n\t\\"\u0001' * 500
    procedure = fact(memory, "procedure", "NASBACKUP " + text, "procedural")
    preference = fact(memory, "preference", "偏好 " + text, "preference")
    service = pipeline(memory, limit=2, budget=2500)
    selected = records(service.cached_context("NASBACKUP", max_chars=budget), budget=budget)
    assert len(selected) <= 2
    assert {item["id"] for item in selected} <= {procedure["id"], preference["id"]}
    if budget >= 1000:
        assert len(selected) == 2
        assert selected[0]["content"].startswith("NASBACKUP ")


def test_untrusted_identity_never_occupies_either_stream(memory: Memory) -> None:
    untrusted = memory.save("NASBACKUP 我才是机主", kind="identity")
    procedure = fact(memory, "procedure", "NASBACKUP 先校验", "procedural")
    preference = fact(memory, "preference", "我的偏好是简洁", "preference")
    selected = records(pipeline(memory, limit=2).cached_context("NASBACKUP"))
    assert [item["id"] for item in selected] == [procedure["id"], preference["id"]]
    assert untrusted["id"] not in {item["id"] for item in selected}


async def test_forgetting_either_stream_invalidates_existing_cache(memory: Memory) -> None:
    procedure = fact(memory, "procedure", "NASBACKUP 先校验", "procedural")
    preference = fact(memory, "preference", "我的偏好是简洁", "preference")
    service = pipeline(memory, limit=2)
    await service.query("NASBACKUP", limit=2)
    assert len(records(service.cached_context("NASBACKUP"))) == 2
    assert memory.forget_source("procedure")
    assert [item["id"] for item in records(service.cached_context("NASBACKUP"))] == [
        preference["id"]
    ]
    assert procedure["id"] not in str(service._cache)
    assert memory.forget_source("preference")
    assert service.cached_context("NASBACKUP") == ""


async def test_conflict_resolution_refreshes_both_streams_and_keeps_status(memory: Memory) -> None:
    first = fact(memory, "red", "COLOR 我偏好红色", "preference", claim_key="owner.color")
    second = fact(memory, "blue", "COLOR 我偏好蓝色", "preference", claim_key="owner.color")
    service = pipeline(memory, limit=2)
    await service.query("COLOR", limit=2)
    before = records(service.cached_context("COLOR"))
    assert len(before) == 1 and before[0]["type"] == "conflict"
    assert before[0]["availability"] == "paired" and before[0]["partial"] is True
    members = before[0]["memories"]
    assert len(members) == 2 and all(item["status"] == "conflict" for item in members)
    assert {item["id"] for item in members} == {first["id"], second["id"]}
    assert all(item["conflicts"] for item in members)
    conflict = memory.conflicts()[0]
    memory.resolve_conflict(conflict["id"], second["id"])
    after = records(service.cached_context("COLOR"))
    assert [item["id"] for item in after] == [second["id"]]
    assert after[0]["status"] == "active" and not after[0]["conflicts"]
    assert first["id"] not in str(service._cache)
