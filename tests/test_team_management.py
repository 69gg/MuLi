from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from muli.config import ConfigFile
from muli.runtime import Runtime
from muli.server import create_app


@pytest.fixture
async def team_client(tmp_path: Path) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient]]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime=runtime)
    async with app.router.lifespan_context(app):
        for index in range(105):
            identifier = f"worker-{index:03}"
            runtime.store.put(
                "agent",
                {
                    "id": identifier,
                    "session_id": identifier,
                    "name": identifier,
                    "role": "worker",
                    "status": "running" if index >= 102 else "completed",
                },
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as client:
            yield runtime, client


async def test_management_pages_reach_all_agents_without_changing_model_listing(
    team_client: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = team_client
    seen: list[str] = []
    for offset in (0, 50, 100):
        response = await client.get("/api/team", params={"offset": offset})
        assert response.status_code == 200
        page = response.json()
        assert page["total"] == 105 and page["offset"] == offset
        assert page["has_more"] == (offset < 100)
        seen.extend(member["id"] for member in page["members"])
    assert seen == [f"worker-{index:03}" for index in reversed(range(105))]
    assert runtime.team.list(limit=1)[0]["id"] == "worker-000"
    assert runtime.team.list(limit=1, offset=104)[0]["id"] == "worker-104"


async def test_active_filter_counts_and_clamps_live_last_page(
    team_client: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = team_client
    params = {"limit": 2, "offset": 2, "active_only": "true"}
    page = (await client.get("/api/team", params=params)).json()
    assert page["total"] == 3 and page["offset"] == 2 and not page["has_more"]
    assert page["members"][0]["id"] == "worker-102"
    for identifier in ("worker-102", "worker-103"):
        member = runtime.team.get(identifier)
        runtime.store.put("agent", {**member, "status": "completed"})
    page = (await client.get("/api/team", params=params)).json()
    assert page["total"] == 1 and page["offset"] == 0
    assert [member["id"] for member in page["members"]] == ["worker-104"]
    runtime.store.put("agent", {**runtime.team.get("worker-104"), "status": "interrupted"})
    page = (await client.get("/api/team", params=params)).json()
    assert page["total"] == 0 and page["offset"] == 0 and page["members"] == []


async def test_management_reads_bounded_previews_and_keeps_original_history(
    team_client: tuple[Runtime, httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, client = team_client
    original = runtime.store.messages

    def read(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("Management previews must not decode full message records")

    for index in range(8):
        runtime.store.add_message(
            "worker-104",
            "assistant",
            f"message {index}" + "长" * 2000,
            provider_data={"native": "kept only in archive"},
            images=[{"data": "not-a-real-image"}],
        )
    monkeypatch.setattr(runtime.store, "messages", read)
    page = (await client.get("/api/team", params={"limit": 1})).json()
    assert len(page["messages"]) == 5
    assert page["messages"][0]["content"].startswith("message 3")
    assert all(len(item["content"]) == 1000 and item["truncated"] for item in page["messages"])
    assert all("provider_data" not in item and "images" not in item for item in page["messages"])
    assert len(original("worker-104")) == 8
    assert original("worker-104")[-1]["provider_data"]["native"] == "kept only in archive"


@pytest.mark.parametrize("text", ["a\x00b尾巴", "😀沐璃" * 10, "", "恰好三"])
async def test_preview_handles_unicode_nul_and_exact_boundary(
    team_client: tuple[Runtime, httpx.AsyncClient], text: str
) -> None:
    runtime, _ = team_client
    runtime.store.add_message("preview", "assistant", text)
    preview = runtime.store.message_previews("preview", max_chars=3)[0]
    assert preview["content"] == text[:3]
    assert preview["truncated"] == (len(text) > 3)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
async def test_invalid_management_page_rejected(
    team_client: tuple[Runtime, httpx.AsyncClient], params: dict[str, int]
) -> None:
    _, client = team_client
    assert (await client.get("/api/team", params=params)).status_code == 422
