"""Real local memory/source/cache boundaries; no model, fabricated recall or background worker."""

from __future__ import annotations

from typing import Any

import pytest
from test_memory_context import fact, records
from test_memory_context import memory as memory

from muli.memory import Memory, terms
from muli.memory_pipeline import MemoryPipeline, MemoryPipelineConfig


def ingest_current(service: MemoryPipeline, content: str) -> dict[str, Any]:
    receipt = service.ingest(
        {
            "id": "current-owner-input",
            "role": "user",
            "principal": "owner",
            "source": "web",
            "session_id": "main",
            "content": content,
        },
        trusted_owner=True,
    )
    item = service.memory.get(receipt["memory_id"])
    assert item is not None
    assert service.memory.automatic_episode_id("current-owner-input") == item["id"]
    return item


def test_task_goal_cannot_reintroduce_the_exact_current_automatic_episode(memory: Memory) -> None:
    service = MemoryPipeline(memory, config=MemoryPipelineConfig(context_limit=3, max_candidates=4))
    background = fact(
        memory, "background", "TASKSENTINEL 的旧资料要求先核对归档再报告。", "procedural"
    )
    profile = fact(memory, "profile", "我的偏好是简洁中文。", "preference")
    current = ingest_current(service, "TASKSENTINEL 这是本次刚收到的输入，不是旧背景。")
    assert {item["id"] for item in memory.search("TASKSENTINEL", 4)} >= {
        current["id"],
        background["id"],
    }
    assert memory.search("CURRENTMISS", 3) == []
    changes = memory.store.db.total_changes
    rendered = service.context_for_prompt(
        "CURRENTMISS", 4000, latest_user_id="current-owner-input", task_query="TASKSENTINEL"
    )
    selected = records(rendered, budget=4000)
    assert {item["id"] for item in selected} == {background["id"], profile["id"]}
    assert current["id"] not in rendered and current["content"] not in rendered
    assert all("version" not in item for item in selected)
    assert memory.store.db.total_changes == changes
    assert service._worker is None


@pytest.mark.parametrize("exhausted", ["profile", "characters", "terms", "candidates"])
def test_exhausted_shared_budget_does_not_issue_task_keyword_sql(
    memory: Memory, exhausted: str
) -> None:
    service = MemoryPipeline(
        memory,
        config=MemoryPipelineConfig(
            context_limit=1 if exhausted == "profile" else 2,
            max_candidates=1 if exhausted == "candidates" else 4,
        ),
    )
    background = fact(memory, "background", "TASKSENTINEL 独立旧资料。", "procedural")
    profile = fact(memory, "profile", "我的偏好是简洁中文。", "preference")
    query = "CURRENTMISS"
    latest_user_id: str | None = None
    if exhausted == "terms":
        query = " ".join(f"unseen{index:03d}" for index in range(64))
        assert len(terms(query)) == 64
    elif exhausted == "candidates":
        current = ingest_current(service, "CANDIDATEONLY 本次输入。")
        query, latest_user_id = "CANDIDATEONLY", "current-owner-input"
        assert [item["id"] for item in memory.search(query, 1)] == [current["id"]]
    # Prove that the task keyword really has a readable candidate before
    # observing its absence from the prompt path's SQL.
    assert [item["id"] for item in memory.search("TASKSENTINEL", 1)] == [background["id"]]
    baseline = service.context_for_prompt(query, 4000, latest_user_id=latest_user_id)
    assert [item["id"] for item in records(baseline, budget=4000)] == [profile["id"]]
    budget = len(baseline) if exhausted == "characters" else 4000
    statements: list[str] = []
    changes = memory.store.db.total_changes
    memory.store.db.set_trace_callback(statements.append)
    try:
        rendered = service.context_for_prompt(
            query, budget, latest_user_id=latest_user_id, task_query="TASKSENTINEL"
        )
    finally:
        memory.store.db.set_trace_callback(None)
    assert statements, "必须实际经过原本地检索/正文核验，不能用空调用伪证零task SQL"
    assert not any("tasksentinel" in statement.casefold() for statement in statements)
    assert rendered == baseline
    assert [item["id"] for item in records(rendered, budget=budget)] == [profile["id"]]
    assert memory.store.db.total_changes == changes
    assert service._worker is None


async def test_real_query_cache_and_corrections_stay_current_without_another_ingest(
    memory: Memory,
) -> None:
    """Same pipeline lifetime; Runtime's actual tool-round journey is tested separately."""
    service = MemoryPipeline(memory, config=MemoryPipelineConfig(context_limit=1, max_candidates=4))
    old = fact(memory, "cached-source", "CACHEKEY 旧要求采用模式甲。", "procedural")
    background = fact(memory, "background", "TASKSENTINEL 补充旧资料。", "procedural")
    source_id = old["source_id"]
    assert isinstance(source_id, str)
    warmed = await service.query("CACHEKEY", limit=1)
    assert [item["id"] for item in warmed["memories"]] == [old["id"]]
    statements: list[str] = []
    changes = memory.store.db.total_changes
    memory.store.db.set_trace_callback(statements.append)
    try:
        assert await service.query("CACHEKEY", limit=1) == warmed
        rendered = service.context_for_prompt("CACHEKEY", 4000, task_query="TASKSENTINEL")
    finally:
        memory.store.db.set_trace_callback(None)
    assert statements
    assert not any(
        table in statement.casefold()
        for statement in statements
        for table in ("memory_terms", "memory_edges", "memory_aliases")
    ), "完整的真实query缓存已占满当前名额，不应补搜或再展开图"
    assert memory.store.db.total_changes == changes
    assert [item["content"] for item in records(rendered, budget=4000)] == [old["content"]]

    corrected = memory.edit(old["id"], content="CACHEKEY 更正后的要求采用模式乙。")
    assert corrected["version"] > old["version"]
    corrected_context = service.context_for_prompt("CACHEKEY", 4000, task_query="TASKSENTINEL")
    corrected_pages = records(corrected_context, budget=4000)
    assert [item["content"] for item in corrected_pages] == [corrected["content"]]
    assert old["content"] not in corrected_context
    assert all("version" not in item for item in corrected_pages)
    rewarmed = await service.query("CACHEKEY", limit=1)
    assert [item["content"] for item in rewarmed["memories"]] == [corrected["content"]]
    assert memory.forget_source(source_id)
    after_forget = service.context_for_prompt("CACHEKEY", 4000, task_query="TASKSENTINEL")
    assert [item["id"] for item in records(after_forget, budget=4000)] == [background["id"]]
    assert old["id"] not in after_forget and source_id not in after_forget
    assert old["content"] not in after_forget and corrected["content"] not in after_forget
    assert memory.store.db.execute("SELECT count(*) FROM memory_jobs").fetchone()[0] == 0
    assert service._worker is None
