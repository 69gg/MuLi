"""Explicit owner-source preferences through real HTTP, ingress and model requests.

The absent endpoint is the baseline entry red. Later request and reopen
assertions are reached only after that HTTP contract succeeds.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attachment_memory_policy import offline as offline
from test_memory_runtime_quality import MEMORY_HEADING, memory_records
from test_runtime import FakeClient, make_runtime

from muli.evidence import json_size
from muli.memory_application import MemoryApplication
from muli.providers import Message, ModelConfig, ModelTurn
from muli.runtime import Runtime
from muli.server import create_app

ENDPOINT = "/api/memory/preferences/from-source"
ANSWER = "本轮已结束，记忆状态以实际资料为准。"
PASSWORD = "owner-preference-test-password"
QUOTE = "数学讲解先给直观例子，再推公式。"
QUESTION = "解释为什么等差数列求和可以首尾配对。"
ITEM_FIELDS = (
    "id",
    "version",
    "kind",
    "layer",
    "status",
    "content",
    "quote",
    "source_id",
    "trusted_owner",
)


@dataclass
class OwnerRig:
    runtime: Runtime
    memory: MemoryApplication
    web: httpx.AsyncClient
    seen: list[list[Message]]
    model_calls: list[str]


@asynccontextmanager
async def owner_application(tmp_path: Path) -> AsyncIterator[OwnerRig]:
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "onboarding_pending": False,
            "server": {"password": PASSWORD},
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "memory": {"auto_extract": False, "context_limit": 2},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000},
        }
    )
    model_calls: list[str] = []

    def factory(profile: ModelConfig) -> FakeClient:
        assert profile.model == "main", "此旅程不能额外调用提炼或其它模型"
        model_calls.append(profile.model)
        return FakeClient([ModelTurn(content=ANSWER)], seen)

    runtime.client_factory = factory  # type: ignore[assignment]
    await runtime.reconfigure()
    app = create_app(runtime.config, runtime=runtime)
    memory: MemoryApplication = app.state.memory_application
    async with app.router.lifespan_context(app):
        assert memory._recovery is not None
        await asyncio.wait_for(asyncio.shield(memory._recovery), timeout=4)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as web:
            login = await web.post("/api/auth/login", json={"password": PASSWORD})
            assert login.status_code == 200, login.text
            yield OwnerRig(runtime, memory, web, seen, model_calls)


async def ask(rig: OwnerRig, text: str) -> dict[str, Any]:
    receipt = await rig.runtime.submit(text, mode="yolo", source="web")
    assert isinstance(receipt["run_id"], str)
    assert await asyncio.wait_for(rig.runtime.wait(receipt["run_id"]), timeout=4) == ANSWER
    job = await asyncio.wait_for(rig.memory.pipeline.wait(receipt["message"]["id"]), timeout=4)
    assert job is not None and job["state"] == "done"
    return receipt


async def owner_source(rig: OwnerRig, text: str) -> dict[str, Any]:
    receipt = await ask(rig, text)
    response = await rig.web.get("/api/memory/source", params={"id": receipt["message"]["id"]})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["content"] == text and page["has_more"] is False
    assert page["role"] == "user" and page["principal"] == "owner"
    assert page["trusted_owner"] is True
    assert page["session_id"] == rig.runtime.store.existing_main_session_id()
    return page


def command(source: dict[str, Any], action: str, quote: str = QUOTE) -> dict[str, Any]:
    return {
        "action": action,
        "source_id": source["source_id"],
        "revision": source["revision"],
        "quote": quote,
    }


def current_item(item: dict[str, Any]) -> dict[str, Any]:
    return {field: item[field] for field in ITEM_FIELDS}


def receipt(
    source: dict[str, Any], item: dict[str, Any] | None, quote: str = QUOTE
) -> dict[str, Any]:
    return {
        "state": "absent" if item is None else "present",
        "source_id": source["source_id"],
        "revision": source["revision"],
        "quote": quote,
        "item": current_item(item) if item is not None else None,
    }


async def test_manual_preference_stays_untrusted_in_actual_automatic_context(
    tmp_path: Path,
) -> None:
    async with owner_application(tmp_path) as rig:
        response = await rig.web.post(
            "/api/memories", json={"content": QUOTE, "kind": "preference", "source": "owner"}
        )
        assert response.status_code == 200, response.text
        item = response.json()
        assert item["layer"] == "preference" and item["trusted_owner"] is False
        assert item["source_id"] is None
        assert item["id"] not in {entry["id"] for entry in rig.runtime.memory.owner_profile()}

        await ask(rig, QUESTION)

        assert len(rig.seen) == len(rig.model_calls) == 1
        automatic = [
            message.content for message in rig.seen[-1] if MEMORY_HEADING in message.content
        ]
        assert all(item["id"] not in content and QUOTE not in content for content in automatic)


async def test_confirm_owner_source_reaches_actual_request_and_normal_reopen(
    tmp_path: Path,
) -> None:
    async with owner_application(tmp_path) as rig:
        source = await owner_source(rig, "这是我自己的学习习惯。" + QUOTE)
        main_id = rig.runtime.store.existing_main_session_id()
        original = deepcopy(rig.runtime.memory.source(source["source_id"]))
        original_job = deepcopy(rig.memory.pipeline.job(source["source_id"]))
        before_calls = len(rig.model_calls)
        before_changes = rig.runtime.store.db.total_changes

        response = await rig.web.post(ENDPOINT, json=command(source, "inspect"))
        assert response.status_code == 200, response.text
        assert response.json() == receipt(source, None)
        assert response.headers["cache-control"] == "no-store"
        assert rig.runtime.store.db.total_changes == before_changes
        assert len(rig.model_calls) == before_calls

        response = await rig.web.post(ENDPOINT, json=command(source, "confirm"))
        assert response.status_code == 200, response.text
        item = response.json()["item"]
        assert isinstance(item, dict)
        actual = rig.runtime.memory.get(item["id"])
        assert actual is not None and item == current_item(actual)
        assert response.json() == receipt(source, item)
        assert item["content"] == item["quote"] == QUOTE
        assert item["kind"] == item["layer"] == "preference"
        assert item["trusted_owner"] is True and item["status"] == "active"
        assert item["source_id"] == source["source_id"] and item["version"] == 1
        assert rig.runtime.memory.source(source["source_id"]) == original
        assert rig.memory.pipeline.job(source["source_id"]) == original_job
        assert len(rig.model_calls) == before_calls

        before_changes = rig.runtime.store.db.total_changes
        response = await rig.web.post(ENDPOINT, json=command(source, "inspect"))
        assert response.status_code == 200, response.text
        assert response.json() == receipt(source, item)
        assert rig.runtime.store.db.total_changes == before_changes

        await ask(rig, QUESTION)

        selected = next(
            entry for entry in memory_records(rig.seen[-1]) if entry.get("id") == item["id"]
        )
        for key in ("content", "layer", "source_id", "trusted_owner", "status"):
            assert selected[key] == item[key]
        assert len(rig.model_calls) == before_calls + 1
        frozen_item = deepcopy(rig.runtime.memory.get(item["id"]))
    assert not rig.runtime.tasks

    async with owner_application(tmp_path) as reopened:
        assert reopened.runtime.store.existing_main_session_id() == main_id
        assert reopened.runtime.memory.source(source["source_id"]) == original
        assert reopened.memory.pipeline.job(source["source_id"]) == original_job
        assert reopened.runtime.memory.get(item["id"]) == frozen_item
        before_changes = reopened.runtime.store.db.total_changes
        response = await reopened.web.post(ENDPOINT, json=command(source, "inspect"))
        assert response.status_code == 200, response.text
        assert response.json() == receipt(source, frozen_item)
        assert reopened.runtime.store.db.total_changes == before_changes
        assert reopened.model_calls == []

        await ask(reopened, QUESTION)

        selected = next(
            entry for entry in memory_records(reopened.seen[-1]) if entry.get("id") == item["id"]
        )
        for key in ("content", "layer", "source_id", "trusted_owner", "status"):
            assert selected[key] == frozen_item[key]
        assert len(reopened.seen) == len(reopened.model_calls) == 1


async def test_duplicate_keeps_current_correction_and_normalized_quote_without_writes(
    tmp_path: Path,
) -> None:
    first_quote, equivalent_quote = "我偏好ＡＢＣ　例子。", "我偏好abc 例子。"
    async with owner_application(tmp_path) as rig:
        source = await owner_source(rig, first_quote + "另一种写法：" + equivalent_quote)
        response = await rig.web.post(ENDPOINT, json=command(source, "confirm", first_quote))
        assert response.status_code == 200, response.text
        item = response.json()["item"]
        response = await rig.web.patch(
            "/api/memories/" + item["id"],
            json={"content": "这项已改为普通资料，不再表示当前偏好。", "kind": "fact"},
        )
        assert response.status_code == 200, response.text
        corrected = response.json()
        assert corrected["version"] == 2 and corrected["layer"] == "semantic"
        assert corrected["quote"] == first_quote
        before_changes = rig.runtime.store.db.total_changes
        before_calls = len(rig.model_calls)
        original_job = deepcopy(rig.memory.pipeline.job(source["source_id"]))
        for action in ("inspect", "confirm"):
            response = await rig.web.post(ENDPOINT, json=command(source, action, equivalent_quote))
            assert response.status_code == 200, response.text
            assert response.json() == receipt(source, corrected, equivalent_quote)
        assert rig.runtime.memory.get(item["id"]) == corrected
        assert rig.runtime.store.db.total_changes == before_changes
        assert len(rig.model_calls) == before_calls
        assert rig.memory.pipeline.job(source["source_id"]) == original_job


async def test_policy_identity_revision_and_quote_rejections_do_not_create_a_preference(
    tmp_path: Path,
) -> None:
    async with owner_application(tmp_path) as rig:
        source = await owner_source(rig, QUOTE)
        payload = command(source, "confirm")
        anonymous = await rig.web.post("/api/auth/logout", json={})
        assert anonymous.status_code == 200, anonymous.text
        # Authentication is checked before the proposed preference is looked up.
        response = await rig.web.post(ENDPOINT, json=payload)
        assert response.status_code == 401
        login = await rig.web.post("/api/auth/login", json={"password": PASSWORD})
        assert login.status_code == 200, login.text
        before_changes = rig.runtime.store.db.total_changes
        response = await rig.web.post(
            ENDPOINT, json=payload, headers={"Origin": "http://unrelated.invalid"}
        )
        assert response.status_code == 403
        response = await rig.web.post(ENDPOINT, json={**payload, "trusted_owner": True})
        assert response.status_code == 422
        response = await rig.web.post(ENDPOINT, json={**payload, "revision": "0" * 64})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "evidence_changed"
        response = await rig.web.post(ENDPOINT, json={**payload, "quote": "先推公式再讲例子"})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_arguments"
        rig.runtime.config.update({"memory": {"enabled": False}})
        response = await rig.web.post(ENDPOINT, json=payload)
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_arguments"
        rig.runtime.config.update({"memory": {"enabled": True}})
        rig.runtime.maintenance.admission.pause()
        try:
            for action in ("inspect", "confirm"):
                response = await rig.web.post(ENDPOINT, json=command(source, action))
                assert response.status_code == 409, response.text
        finally:
            rig.runtime.maintenance.admission.release()
        assert rig.runtime.store.db.total_changes == before_changes

        with rig.runtime.store.db:
            rig.runtime.store.db.execute(
                "UPDATE memory_sources SET principal='external' WHERE id=?",
                (source["source_id"],),
            )
        before_changes = rig.runtime.store.db.total_changes
        response = await rig.web.post(ENDPOINT, json=payload)
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_arguments"
        assert rig.runtime.store.db.total_changes == before_changes
        assert len(rig.model_calls) == 1


async def test_complete_receipt_budget_rejects_before_new_or_existing_writes(
    tmp_path: Path,
) -> None:
    quote = QUOTE * 25
    async with owner_application(tmp_path) as rig:
        source = await owner_source(rig, quote)
        assert json_size(receipt(source, None, quote)) < 1000
        rig.runtime.config.update({"limits": {"max_output_chars": 1000}})
        before_changes = rig.runtime.store.db.total_changes
        response = await rig.web.post(ENDPOINT, json=command(source, "inspect", quote))
        assert response.status_code == 200, response.text
        assert response.json() == receipt(source, None, quote)
        response = await rig.web.post(ENDPOINT, json=command(source, "confirm", quote))
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "output_budget"
        assert rig.runtime.store.db.total_changes == before_changes
        assert rig.runtime.memory.owner_profile() == []

        rig.runtime.config.update({"limits": {"max_output_chars": 6000}})
        response = await rig.web.post(ENDPOINT, json=command(source, "confirm", quote))
        assert response.status_code == 200, response.text
        item = response.json()["item"]
        response = await rig.web.patch("/api/memories/" + item["id"], json={"content": QUOTE * 100})
        assert response.status_code == 200, response.text
        corrected = response.json()
        assert json_size(receipt(source, corrected, quote)) > 1000
        rig.runtime.config.update({"limits": {"max_output_chars": 1000}})
        before_changes = rig.runtime.store.db.total_changes
        for action in ("inspect", "confirm"):
            response = await rig.web.post(ENDPOINT, json=command(source, action, quote))
            assert response.status_code == 400, response.text
            assert response.json()["detail"]["code"] == "output_budget"
        assert rig.runtime.store.db.total_changes == before_changes
        assert rig.runtime.memory.get(item["id"]) == corrected
        assert len(rig.model_calls) == 1


async def test_source_sql_byte_gate_precedes_full_read_and_ascii_uses_character_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with owner_application(tmp_path) as rig:
        wide = await owner_source(rig, "🙂" * 1100 + QUOTE)
        ascii_source = await owner_source(rig, "x" * 1500 + QUOTE)
        rig.runtime.config.update({"memory": {"max_source_chars": 1000}})
        before_changes = rig.runtime.store.db.total_changes

        def no_full_read(*args: Any, **kwargs: Any) -> NoReturn:
            raise AssertionError("SQL 字节准入拒绝前不能整份加载原文")

        with monkeypatch.context() as patch:
            patch.setattr(rig.runtime.memory, "source", no_full_read)
            for action in ("inspect", "confirm"):
                response = await rig.web.post(ENDPOINT, json=command(wide, action))
                assert response.status_code == 400, response.text
                assert response.json()["detail"]["code"] == "output_budget"
        for action in ("inspect", "confirm"):
            response = await rig.web.post(ENDPOINT, json=command(ascii_source, action))
            assert response.status_code == 400, response.text
            assert response.json()["detail"]["code"] == "output_budget"
        assert rig.runtime.store.db.total_changes == before_changes
        assert len(rig.model_calls) == 2


async def test_superseded_current_item_stays_superseded_and_forgotten_source_stays_absent(
    tmp_path: Path,
) -> None:
    replacement_quote = "数学讲解先推公式，最后给例子。"
    async with owner_application(tmp_path) as rig:
        source = await owner_source(rig, QUOTE + "我后来改成：" + replacement_quote)
        source_id = source["source_id"]
        memory = rig.runtime.memory
        original = deepcopy(memory.source(source_id))
        original_job = deepcopy(rig.memory.pipeline.job(source_id))
        original_message = deepcopy(rig.runtime.store.get("message", source_id))
        before_calls = len(rig.model_calls)
        # Supersede has no HTTP route. Prepare a real existing extraction and
        # resolve it through the public domain API also used by memory_resolve.
        # This is not an all-HTTP transition or a direct SQL status fixture.
        old, replacement = memory.apply_extraction(
            source_id,
            [
                {
                    "layer": "preference",
                    "content": text,
                    "quote": text,
                    "claim_key": "owner.math.explanation",
                }
                for text in (QUOTE, replacement_quote)
            ],
        )
        conflict = memory.conflicts(old["id"], include_resolved=False)[0]
        resolved = memory.resolve_conflict(conflict["id"], replacement["id"])
        assert resolved["superseded_id"] == old["id"]
        current = memory.get(old["id"])
        assert current is not None and current["status"] == "superseded"
        assert current["version"] == old["version"] + 1
        current_replacement = deepcopy(memory.get(replacement["id"]))
        current_history = deepcopy(memory.revisions(old["id"]))
        assert memory.source(source_id) == original
        assert rig.memory.pipeline.job(source_id) == original_job
        before_changes = rig.runtime.store.db.total_changes
        for action in ("inspect", "confirm"):
            response = await rig.web.post(ENDPOINT, json=command(source, action))
            assert response.status_code == 200, response.text
            assert response.json() == receipt(source, current)
        assert rig.runtime.store.db.total_changes == before_changes
        assert memory.get(old["id"]) == current
        assert memory.get(replacement["id"]) == current_replacement
        assert memory.revisions(old["id"]) == current_history
        assert memory.source(source_id) == original
        assert rig.memory.pipeline.job(source_id) == original_job
        assert len(rig.model_calls) == before_calls

        response = await rig.web.delete("/api/memories/" + old["id"])
        assert response.status_code == 200, response.text
        assert response.json() == {"deleted": True}
        assert memory.source(source_id) is None
        assert rig.memory.pipeline.job(source_id) is None
        assert memory.get(old["id"]) is None and memory.get(replacement["id"]) is None
        before_changes = rig.runtime.store.db.total_changes
        for action in ("inspect", "confirm"):
            response = await rig.web.post(ENDPOINT, json=command(source, action))
            assert response.status_code == 404, response.text
            assert response.json()["detail"]["code"] == "evidence_not_found"
        assert rig.runtime.store.db.total_changes == before_changes
        assert memory.source(source_id) is None
        assert rig.memory.pipeline.job(source_id) is None
        assert memory.get(old["id"]) is None and memory.get(replacement["id"]) is None
        assert rig.runtime.store.get("message", source_id) == original_message
        assert len(rig.model_calls) == before_calls
