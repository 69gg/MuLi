from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_runtime import make_runtime

from muli.providers import ModelTurn, ToolCall


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("computer_snapshot", {}, "disabled"),
        ("computer_targets", {}, "disabled"),
    ],
)
async def test_computer_failure_retains_structured_diagnostics_in_model_replay(
    tmp_path: Path, name: str, arguments: dict[str, str], code: str
) -> None:
    runtime, seen = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("observation", name, json.dumps(arguments))]),
            ModelTurn(content="请先配置电脑读取能力"),
        ],
    )
    try:
        session = runtime.new_session(mode="yolo")
        started = await runtime.start(session["id"], "查看桌面应用")
        assert await runtime.wait(started["run_id"]) == "请先配置电脑读取能力"
        response = seen[1][-1]
        assert response.role == "tool" and response.tool_call_id == "observation"
        result = json.loads(response.content)
        assert result["code"] == code
        assert result["outcome"] == "not_started"
        assert result["error"] == result["message"]
        archived = [
            item for item in runtime.store.messages(session["id"]) if item["role"] == "tool"
        ]
        assert len(archived) == 1 and json.loads(archived[0]["content"]) == result
        completed = [event for event in runtime.store.events() if event["type"] == "tool.completed"]
        assert len(completed) == 1
        assert json.loads(completed[0]["data"]["result"]) == result
    finally:
        await runtime.close()
