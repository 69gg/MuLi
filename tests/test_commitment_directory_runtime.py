"""Real model/tool rounds reach old ledger metadata without opening task bodies."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from test_history_evidence_runtime import completed
from test_history_find_runtime import preserved_reply
from test_runtime import FakeClient, make_runtime
from test_task_briefs import attach_services

from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.store import _SESSION_RECORD_LIMIT


async def test_model_follows_complete_directory_pages_to_a_task_beyond_the_old_500_window(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    responses: list[dict[str, Any]] = []
    target = "task-0000"
    rounds = 0
    try:
        runtime.config.update(
            {
                "persona": {"prompt": "test"},
                "limits": {"max_context_chars": 240000},
                "compaction": {"enabled": False},
            }
        )
        await runtime.reconfigure()
        records = [
            {
                "id": f"task-{index:04d}",
                "title": "旧目标" if index == 0 else f"工作 {index}",
                "status": "open",
                "revision": 1,
                "brief": {
                    "version": 1,
                    "goal": "private-brief-do-not-project",
                    "constraints": [],
                    "evidence_refs": [],
                },
                "control": {
                    "epoch": 0,
                    "approved_epoch": 0,
                    "plan": {"text": "private-plan-do-not-project"},
                },
            }
            for index in range(621)
        ]
        runtime.store.put_many([("commitment", record) for record in records])
        with runtime.store.db:
            runtime.store.db.executemany(
                "UPDATE objects SET updated=? WHERE kind='commitment' AND id=?",
                [(float(index), record["id"]) for index, record in enumerate(records)],
            )
        legacy = services.commitments.list(limit=500)
        assert len(legacy) == 500 and target not in {item["id"] for item in legacy}
        assert runtime.tools["commitment_directory"].risk == "read"
        assert runtime.tools["commitment_directory"].preserve_output is True

        class Reader(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds
                rounds += 1
                seen.append(list(messages))
                functions = {tool["function"]["name"]: tool["function"] for tool in tools}
                schema = functions["commitment_directory"]["parameters"]
                assert set(schema["properties"]) == {"status", "cursor", "limit"}
                arguments: dict[str, Any] = {
                    "status": "active",
                    "limit": _SESSION_RECORD_LIMIT - 1,
                }
                if rounds > 1:
                    page = preserved_reply(messages, f"directory-{rounds - 1}")
                    responses.append(page)
                    assert page["consistency"] == "bounded_live"
                    assert "private-brief" not in json.dumps(page)
                    assert "private-plan" not in json.dumps(page)
                    if any(item["id"] == target for item in page["items"]):
                        assert page["has_more"] is False
                        yield StreamEvent("done", turn=ModelTurn(content="已从旧账本定位目标"))
                        return
                    assert page["has_more"] and page["next_cursor"]
                    arguments["cursor"] = page["next_cursor"]
                assert rounds <= 10
                yield StreamEvent(
                    "done",
                    turn=ModelTurn(
                        tool_calls=[
                            ToolCall(
                                f"directory-{rounds}",
                                "commitment_directory",
                                json.dumps(arguments),
                            )
                        ]
                    ),
                )

        runtime.client_factory = lambda _: Reader([], seen)  # type: ignore[assignment,return-value]
        main = runtime.main_session()
        assert (
            await completed(runtime, main["id"], "翻页找到旧目标，只看账本索引")
            == "已从旧账本定位目标"
        )
        found = [item["id"] for page in responses for item in page["items"]]
        assert found == [record["id"] for record in reversed(records)]
        assert len(found) == len(set(found)) == 621
        limits = runtime.config.settings.limits
        assert all(
            page["scanned"] <= limits.max_search_results and page["omitted_total"] == 0
            for page in responses
        )
        assert all(
            len(json.dumps(page, ensure_ascii=False)) <= limits.max_output_chars
            for page in responses
        )
        assert runtime._selected_replans == {}
        assert runtime.store.get("commitment", target) == records[0]
        archived = [item for item in runtime.store.messages(main["id"]) if item["role"] == "tool"]
        assert len(archived) == len(responses)
        assert all(item["preserve_content"] is True for item in archived)
        assert all(item["content_provenance"] == "accepted" for item in archived)
    finally:
        await runtime.close()
