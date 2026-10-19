from __future__ import annotations

import asyncio
import json
from pathlib import Path

from test_runtime import make_runtime

from muli.attachments import Attachments
from muli.local_services import LocalServices
from muli.memory_application import MemoryApplication
from muli.providers import ModelTurn, ToolCall


async def test_memory_source_real_tool_round_keeps_complete_pages_and_error_codes(
    tmp_path: Path,
) -> None:
    turns: list[ModelTurn] = []
    runtime, seen = make_runtime(tmp_path, turns)
    application = None
    try:
        runtime.config.update(
            {
                "persona": {"prompt": "测试"},
                "compaction": {"enabled": False},
                "memory": {"enabled": False},
                "limits": {"max_output_chars": 1000, "max_context_chars": 5000},
            }
        )
        await runtime.reconfigure()
        local = LocalServices(
            runtime, Attachments(runtime.store, tmp_path / "attachments", runtime.config)
        )
        application = MemoryApplication(runtime, local.media)
        original, _ = runtime.memory.register_source(
            {
                "id": "long-source",
                "content": "前" * 1000 + "中段证据🙂\0" * 400,
                "role": "user",
                "source": "attention",
                "principal": "system:attention",
            }
        )
        assert original is not None
        turns.extend(
            [
                ModelTurn(
                    tool_calls=[
                        ToolCall("first", "memory_source", json.dumps({"id": original["id"]}))
                    ]
                ),
                ModelTurn(
                    tool_calls=[
                        ToolCall(
                            "middle",
                            "memory_source",
                            json.dumps(
                                {
                                    "id": original["id"],
                                    "offset_bytes": 3000,
                                    "revision": original["digest"],
                                }
                            ),
                        )
                    ]
                ),
                ModelTurn(
                    tool_calls=[
                        ToolCall(
                            "changed",
                            "memory_source",
                            json.dumps(
                                {
                                    "id": original["id"],
                                    "offset_bytes": 3000,
                                    "revision": "outdated",
                                }
                            ),
                        )
                    ]
                ),
                ModelTurn(content="已核对，过时版本被拒绝"),
            ]
        )
        session = runtime.new_session(mode="yolo")
        started = await runtime.start(session["id"], "读取原文", mode="yolo")
        assert (
            await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == "已核对，过时版本被拒绝"
        )
        for index, call_id in ((1, "first"), (2, "middle"), (3, "changed")):
            result = next(message for message in seen[index] if message.tool_call_id == call_id)
            assert result.preserve_content and len(result.content) <= 1000
            value = json.loads(result.content)
            if call_id == "changed":
                assert value["code"] == "evidence_changed"
            else:
                assert value["has_more"] and value["principal"] == "system:attention"
                assert not value["trusted_owner"]
                assert value["revision"] == original["digest"]
                if call_id == "middle":
                    assert value["content"].startswith("中段证据🙂\0")
        assert runtime.memory.source(original["id"])["content"] == original["content"]
    finally:
        if application is not None:
            await application.close()
        await runtime.close()
