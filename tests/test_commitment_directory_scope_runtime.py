"""Directory metadata never upgrades real caller authority to task body reads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_runtime import make_runtime
from test_task_briefs import attach_services, bind

from muli.providers import ToolCall


async def test_bound_teammate_and_forwarded_main_handler_keep_existing_brief_scope(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    try:
        own = await services.commitments.save(
            "自己的任务", brief={"goal": "own-private-brief", "constraints": []}
        )
        others = [
            await services.commitments.save(
                f"其他任务 {index}",
                brief={"goal": f"other-private-brief-{index}", "constraints": []},
            )
            for index in range(5)
        ]
        original = {item["id"]: item for item in [own, *others]}
        runtime.config.update({"limits": {"max_output_chars": 1000, "max_search_results": 2}})
        main = runtime.main_session()
        main_tools = runtime.session_tools(main["id"])
        teammate = bind(runtime, own["id"])
        tools = runtime.session_tools(teammate["id"])
        responses: list[dict[str, Any]] = []
        cursor: str | None = None
        for index in range(10):
            arguments: dict[str, Any] = {"limit": 1}
            if cursor is not None:
                arguments["cursor"] = cursor
            page = await runtime._tool(
                ToolCall(f"directory-{index}", "commitment_directory", json.dumps(arguments)),
                tools,
                "yolo",
                teammate["id"],
                "scope-check",
            )
            assert "error" not in page
            assert len(json.dumps(page, ensure_ascii=False)) <= 1000
            responses.append(page)
            if not page["has_more"]:
                break
            assert page["next_cursor"] and page["next_cursor"] != cursor
            cursor = page["next_cursor"]
        assert responses[-1]["has_more"] is False
        found = [item["id"] for page in responses for item in page["items"]]
        assert len(found) == len(set(found)) == len(original)
        assert set(found) == set(original)
        assert "private-brief" not in json.dumps(responses)
        assert all(page["scanned"] <= 2 for page in responses)
        own_page = await runtime._tool(
            ToolCall("read-own", "commitment_read", "{}"),
            tools,
            "yolo",
            teammate["id"],
            "scope-check",
        )
        assert "own-private-brief" in own_page["text"]
        workflow = runtime.new_session(mode="yolo")
        runtime.store.put("session", {**workflow, "kind": "workflow", "role": "main"})
        for session_id, handlers in (
            (teammate["id"], tools),
            (teammate["id"], main_tools),
            (workflow["id"], main_tools),
        ):
            denied = await runtime._tool(
                ToolCall(
                    "read-other", "commitment_read", json.dumps({"identifier": others[0]["id"]})
                ),
                handlers,
                "yolo",
                session_id,
                "scope-check",
            )
            assert "绑定" in denied["error"] and "other-private-brief" not in json.dumps(denied)
        assert runtime._selected_replans == {}
        assert all(
            runtime.store.get("commitment", identifier) == item
            for identifier, item in original.items()
        )
        assert runtime.tasks == {}
    finally:
        await runtime.close()
