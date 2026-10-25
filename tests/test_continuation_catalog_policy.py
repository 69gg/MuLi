"""Catalog policy through real caller runs and the original conversation tool.

Agent/run rows are explicit directory reader fixtures, not completed Team proof.
The separate phone continuity journey owns normal close/reopen certification.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import tool_result

from muli.evidence import json_size
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.reply_origin import ReplyOrigin
from muli.runtime import Runtime
from muli.store import new_id


@dataclass
class Query:
    identifier: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None


class CatalogRig:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.main_id: str = runtime.main_session()["id"]
        self.main_query: Query | None = None
        self.worker_query: Query | None = None
        self.observed_reads = 0

    async def read(self, *, caller: str | None = None, **arguments: Any) -> dict[str, Any]:
        query = Query(new_id(), {"session_id": self.main_id, **arguments})
        assert self.main_query is None
        self.main_query = query
        try:
            receipt = (
                await self.runtime.submit("只核对持久目录，不继续任何旧工作。", mode="yolo")
                if caller is None
                else await self.runtime.start(caller, "只核对持久目录。", mode="yolo")
            )
            assert (
                await asyncio.wait_for(self.runtime.wait(receipt["run_id"]), 3) == "只读核对结束。"
            )
            assert query.result is not None
            return query.result
        finally:
            self.main_query = None

    def agent(
        self,
        *,
        identifier: str | None = None,
        status: str = "interrupted",
        parent_id: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        identifier = identifier or new_id()
        session = {
            "id": identifier,
            "kind": "team",
            "title": "显式目录读夹具",
            "role": "worker",
            "mode": "yolo",
            "status": "idle",
            "principal": "owner",
            "created_at": 1.0,
            "updated_at": 1.0,
        }
        self.runtime.store.put("session", session)
        run_id = new_id()
        self.runtime.store.put(
            "run",
            {
                "id": run_id,
                "session_id": identifier,
                "status": "cancelled",
                "result": "PRIVATE-RUN-BODY",
                "created_at": 1.0,
            },
        )
        return self.runtime.store.put(
            "agent",
            {
                "id": identifier,
                "session_id": identifier,
                "parent_id": parent_id or self.main_id,
                "status": status,
                "generation": 2,
                "completion_revision": 1,
                "run_id": run_id,
                "role": "worker",
                "mode": "yolo",
                "name": "显式目录读夹具",
                "prompt": "PRIVATE-AGENT-PROMPT",
                "result": "PRIVATE-AGENT-RESULT",
                "reply_origin": ReplyOrigin().dump(),
                **fields,
            },
        )


@asynccontextmanager
async def catalog_runtime(path: Path) -> AsyncIterator[CatalogRig]:
    runtime, _ = make_runtime(path, [])
    try:
        runtime.config.update(
            {
                "persona": {"prompt": "只核对已有目录，不继续旧工作。"},
                "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
                "limits": {
                    "max_context_chars": 16000,
                    "max_output_chars": 6000,
                    "max_tool_steps": 3,
                },
                "compaction": {"enabled": False},
            }
        )
        await runtime.reconfigure()
        rig = CatalogRig(runtime)
        original = runtime.tools["conversation_read"].handler

        async def observed(arguments: dict[str, Any]) -> Any:
            changes = runtime.store.db.total_changes
            try:
                return await original(arguments)
            finally:
                # Tool events/owner ingress occur outside this wrapper. The
                # production reader itself must not write any domain/outbox row.
                assert runtime.store.db.total_changes == changes
                rig.observed_reads += 1

        runtime.tools["conversation_read"].handler = observed

        class Client(FakeClient):
            def __init__(self, query: Query | None) -> None:
                super().__init__([], [])
                self.query, self.round = query, 0

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                self.round += 1
                assert self.round <= 2
                if self.query is None:
                    turn = ModelTurn(content="队友结果仅保留原记录。")
                elif self.round == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                self.query.identifier,
                                "conversation_read",
                                json.dumps(self.query.arguments, ensure_ascii=False),
                            )
                        ]
                    )
                else:
                    self.query.result = tool_result(messages, self.query.identifier)
                    turn = ModelTurn(content="只读核对结束。")
                yield StreamEvent("done", turn=turn)

        def factory(config: ModelConfig) -> FakeClient:
            return Client(rig.worker_query if config.model == "worker" else rig.main_query)

        runtime.client_factory = factory  # type: ignore[assignment]
        yield rig
    finally:
        await runtime.close()


def page(result: dict[str, Any], budget: int = 6000) -> list[dict[str, Any]]:
    assert "error" not in result, result
    assert set(result) == {
        "items",
        "omitted",
        "next_cursor",
        "has_more",
        "consistency",
        "side_effect_status",
    }
    assert result["consistency"] == "live"
    assert result["side_effect_status"] == "unknown"
    assert type(result["omitted"]) is int and result["omitted"] >= 0
    assert type(result["has_more"]) is bool
    assert json_size(result) <= budget
    items = result["items"]
    assert isinstance(items, list)
    for item in items:
        assert {"kind", "id", "session_id", "status", "run_id", "availability"} <= set(item)
        assert set(item) <= {
            "kind",
            "id",
            "session_id",
            "status",
            "generation",
            "completion_revision",
            "run_id",
            "availability",
            "commitment_id",
        }
        assert item["kind"] == "agent"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "PRIVATE-" not in serialized
    return items


@pytest.mark.parametrize("association", ["missing", "wrong-session", "queued-old-run"])
async def test_current_agent_identity_does_not_upgrade_missing_or_old_run_evidence(
    tmp_path: Path, association: str
) -> None:
    async with catalog_runtime(tmp_path) as rig:
        agent = rig.agent(status="queued" if association == "queued-old-run" else "interrupted")
        if association == "missing":
            assert rig.runtime.store.delete("run", agent["run_id"])
        elif association == "wrong-session":
            run = rig.runtime.store.get("run", agent["run_id"])
            assert run is not None
            rig.runtime.store.put("run", {**run, "session_id": rig.main_id})
        before = rig.runtime.store.get("agent", agent["id"])
        result = await rig.read(section="continuations", limit=2)
        items = page(result)
        assert len(items) == 1 and result["omitted"] == 0
        assert items[0]["id"] == agent["id"] and items[0]["session_id"] == agent["session_id"]
        assert items[0]["status"] == agent["status"]
        assert items[0]["generation"] == 2 and items[0]["completion_revision"] == 1
        assert items[0]["run_id"] is None
        assert items[0]["availability"] == (
            "historical" if association == "queued-old-run" else "unavailable"
        )
        assert result["has_more"] is False and result["next_cursor"] is None
        assert rig.runtime.store.get("agent", agent["id"]) == before
        assert rig.observed_reads == 1


async def test_completed_and_bad_candidates_consume_the_page_without_refilling(
    tmp_path: Path,
) -> None:
    async with catalog_runtime(tmp_path) as rig:
        rig.runtime.config.update({"limits": {"max_search_results": 2}})
        rig.agent(status="completed")
        damaged = rig.agent()
        rig.runtime.store.put("agent", {**damaged, "session_id": ""})
        rig.agent(status="not-a-real-status")
        valid = rig.agent()
        first = await rig.read(section="continuations", limit=20)
        assert page(first) == []
        assert first["omitted"] == 2 and first["has_more"] is True
        assert isinstance(first["next_cursor"], str)
        second = await rig.read(section="continuations", limit=20, cursor=first["next_cursor"])
        assert [item["id"] for item in page(second)] == [valid["id"]]
        assert second["omitted"] == 1 and second["has_more"] is False
        assert second["next_cursor"] is None


async def test_keyset_cursor_survives_deleting_its_anchor_without_duplicates(
    tmp_path: Path,
) -> None:
    async with catalog_runtime(tmp_path) as rig:
        expected = [rig.agent()["id"] for _ in range(3)]
        first = await rig.read(section="continuations", limit=1)
        assert [item["id"] for item in page(first)] == expected[:1]
        assert first["has_more"] is True
        assert rig.runtime.store.delete("agent", expected[0])
        following = await rig.read(section="continuations", limit=1, cursor=first["next_cursor"])
        assert [item["id"] for item in page(following)] == expected[1:2]
        last = await rig.read(section="continuations", limit=1, cursor=following["next_cursor"])
        assert [item["id"] for item in page(last)] == expected[2:]
        assert last["has_more"] is False and last["next_cursor"] is None


async def test_discovery_excludes_grandchildren_and_other_parents_without_mutating_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[str] = []

    async def forbidden(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        requests.append(request.method)
        raise AssertionError("catalog discovery must not send any HTTP request")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    async with catalog_runtime(tmp_path) as rig:
        child = rig.agent(status="paused")
        grandchild = rig.agent(parent_id=child["id"])
        other_parent = rig.runtime.new_session(mode="yolo")["id"]
        other_child = rig.agent(parent_id=other_parent)
        before = [
            rig.runtime.store.get("agent", item["id"]) for item in (child, grandchild, other_child)
        ]
        result = await rig.read(section="continuations", limit=1)
        assert [item["id"] for item in page(result)] == [child["id"]]
        assert result["omitted"] == 0 and result["has_more"] is False
        assert [
            rig.runtime.store.get("agent", item["id"]) for item in (child, grandchild, other_child)
        ] == before
        assert requests == []


@pytest.mark.parametrize(
    "legacy",
    [
        {"after": 0},
        {"message_id": "old"},
        {"seq": 1},
        {"revision": "old"},
        {"offset_bytes": 0},
        {"length_bytes": 1},
    ],
)
async def test_continuations_cannot_silently_mix_old_message_selectors(
    tmp_path: Path, legacy: dict[str, Any]
) -> None:
    async with catalog_runtime(tmp_path) as rig:
        rig.agent()
        result = await rig.read(section="continuations", **legacy)
        assert result.get("code") == "invalid_arguments"
        assert "items" not in result


async def test_long_necessary_identity_cannot_be_skipped_for_a_short_following_item(
    tmp_path: Path,
) -> None:
    async with catalog_runtime(tmp_path) as rig:
        budget = 1000
        rig.runtime.config.update({"limits": {"max_output_chars": budget}})
        long_id = "必要身份🙂" * budget
        rig.agent(identifier=long_id)
        rig.agent()
        db = rig.runtime.store.db
        original_factory = db.row_factory
        crossed: list[int] = []

        def observe(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> sqlite3.Row:
            crossed.extend(
                len(value) for value in row if isinstance(value, str) and len(value) > budget * 4
            )
            return sqlite3.Row(cursor, row)

        try:
            db.row_factory = observe
            result = await rig.read(section="continuations", limit=2)
        finally:
            db.row_factory = original_factory
        assert crossed == [], (
            "necessary directory identity must be bounded before SQL-to-Python text projection"
        )
        assert result.get("code") == "output_budget"
        assert "items" not in result
        assert rig.runtime.store.get("agent", long_id) is not None


async def test_full_envelope_including_continuation_token_must_fit_the_existing_budget(
    tmp_path: Path,
) -> None:
    async with catalog_runtime(tmp_path) as rig:
        first = rig.agent(identifier="identity-" + "x" * 520)
        rig.agent()
        result = await rig.read(section="continuations", limit=1)
        assert [item["id"] for item in page(result)] == [first["id"]]
        assert result["has_more"] is True and isinstance(result["next_cursor"], str)
        budget = json_size(result) - 1
        assert 1000 <= budget < rig.runtime.config.settings.limits.max_output_chars
        rig.runtime.config.update({"limits": {"max_output_chars": budget}})
        rejected = await rig.read(section="continuations", limit=1)
        assert rejected.get("code") == "output_budget"
        assert "items" not in rejected


async def test_foreign_or_malformed_cursor_cannot_select_another_catalog(tmp_path: Path) -> None:
    async with catalog_runtime(tmp_path / "first") as first:
        first.agent()
        first.agent()
        result = await first.read(section="continuations", limit=1)
        assert len(page(result)) == 1 and result["has_more"] is True
        token = result["next_cursor"]
        assert isinstance(token, str)
        malformed = await first.read(section="continuations", cursor="not-a-valid-cursor")
        assert "error" in malformed and "items" not in malformed
        assert malformed.get("code") == "record_cursor_invalid"
        async with catalog_runtime(tmp_path / "second") as second:
            assert first.runtime.store.instance_id != second.runtime.store.instance_id
            second.agent()
            crossed = await second.read(section="continuations", cursor=token)
            assert "error" in crossed and "items" not in crossed
            assert crossed.get("code") == malformed["code"]
        other = first.runtime.new_session(mode="yolo")["id"]
        decoded = base64.urlsafe_b64decode(token).decode("utf-8")
        assert first.main_id in decoded, "the real emitted cursor must bind its parent scope"
        wrong_token = base64.urlsafe_b64encode(
            decoded.replace(first.main_id, other).encode("utf-8")
        ).decode("ascii")
        changed_scope = await first.read(section="continuations", cursor=wrong_token)
        assert "error" in changed_scope and "items" not in changed_scope
        assert changed_scope.get("code") == malformed["code"]
        wrong_scope = await first.read(section="continuations", session_id=other, cursor=token)
        assert "error" in wrong_scope and "items" not in wrong_scope
        assert wrong_scope.get("code") == "invalid_arguments"
        old_branch = await first.read(cursor=token)
        assert old_branch.get("code") == "invalid_arguments"
        assert "records" not in old_branch


async def test_only_actual_main_can_discover_but_original_body_read_scope_is_unchanged(
    tmp_path: Path,
) -> None:
    async with catalog_runtime(tmp_path) as rig:
        subject = rig.agent()
        message = rig.runtime.store.add_message(
            subject["session_id"], "user", "原正文仍可按准确ID核查。"
        )
        ordinary = rig.runtime.new_session(mode="yolo")["id"]
        denied = await rig.read(caller=ordinary, section="continuations")
        assert "error" in denied and "items" not in denied
        assert denied.get("code") == "invalid_arguments"
        forbidden_parent = await rig.read(section="continuations", session_id=subject["session_id"])
        assert "error" in forbidden_parent and "items" not in forbidden_parent
        assert forbidden_parent.get("code") == denied["code"]
        legacy = await rig.read(
            caller=ordinary, session_id=subject["session_id"], message_id=message["id"]
        )
        assert "error" not in legacy
        assert legacy["content"] == message["content"]
        assert legacy["has_more"] is False


async def test_real_direct_child_cannot_discover_main_catalog_but_can_read_original_evidence(
    tmp_path: Path,
) -> None:
    async with catalog_runtime(tmp_path) as rig:
        message = rig.runtime.store.add_message(
            rig.main_id, "user", "允许原正文读取，目录不授予子代理。"
        )
        for arguments in (
            {"session_id": rig.main_id, "section": "continuations"},
            {"session_id": rig.main_id, "message_id": message["id"]},
        ):
            query = Query(new_id(), arguments)
            rig.worker_query = query
            try:
                child = await rig.runtime.team.spawn(
                    "只读权限边界", "只核查已有记录，不修改或续办。"
                )
                await rig.runtime.team.wait([child["id"]], timeout=3)
                current = rig.runtime.team.get(child["id"])
                assert current["parent_id"] == rig.main_id and current["status"] == "completed"
                assert query.result is not None
                if "section" in arguments:
                    assert "error" in query.result and "items" not in query.result
                    assert query.result.get("code") == "invalid_arguments"
                else:
                    assert "error" not in query.result
                    assert query.result["content"] == message["content"]
            finally:
                rig.worker_query = None
