"""Keep the isolated worker's UTF-8 protocol independent of pipe encodings."""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

import muli.source_comparison as comparison
from muli.process import run_process


def worker_request(left: str, right: str) -> dict[str, Any]:
    return {
        "left": left,
        "right": right,
        "template": {
            "algorithm": comparison.ALGORITHM,
            "left": {"source_id": "attachment:" + "a" * 32, "revision": "a" * 64},
            "right": {"source_id": "attachment:" + "b" * 32, "revision": "b" * 64},
        },
        "max_lines": 16,
        "max_output_chars": 4000,
        "position": 0,
        "cursor_chars": 512,
    }


async def run_worker_with_legacy_stdout(request: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
    # Windows redirected pipes can use an ANSI encoding even with -I. Force
    # that condition on every platform, then execute the real worker entrypoint.
    wrapper = (
        "import runpy, sys\n"
        "sys.stdout.reconfigure(encoding='cp1252', errors='strict')\n"
        "sys.argv = sys.argv[1:]\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    return await run_process(
        [
            sys.executable,
            "-I",
            "-c",
            wrapper,
            str(comparison._WORKER),
            "--input-limit",
            str(len(raw)),
        ],
        input_bytes=raw,
        timeout=5,
        limit=request["max_output_chars"] * 4,
        merge_stderr=False,
        strict_output=True,
    )


@pytest.mark.parametrize(
    ("left_change", "right_change"),
    [
        ("旧条款🙂é\r\n旧尾\x00\n最后\r", "新条款🙂é\r\n新尾\x00\n终行\n"),
        ("café\r\nold\x00\nlast\r", "résumé\r\nnew\x00\nend\n"),
    ],
    ids=["unencodable-chinese-emoji", "encodable-non-utf8-accent"],
)
async def test_worker_writes_utf8_and_preserves_literal_changed_spans(
    left_change: str, right_change: str
) -> None:
    prefix = "shared\r\n"
    request = worker_request(prefix + left_change, prefix + right_change)
    result = await run_worker_with_legacy_stdout(request)
    assert result["exit_code"] == 0, result["stderr"]
    assert result["stderr"] == ""
    assert "é" in result["output"]
    payload = json.loads(result["output"])
    assert payload["status"] == "changed" and payload["next_index"] is None
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["kind"] == "replace"
    for name, content in (("left", left_change), ("right", right_change)):
        side = item[name]
        assert side["content"] == content
        assert json.dumps(content, ensure_ascii=False) in result["output"]
        assert side["offset_bytes"] == len(prefix.encode("utf-8"))
        assert side["length_bytes"] == len(content.encode("utf-8"))
        original = request[name].encode("utf-8")
        start, length = side["offset_bytes"], side["length_bytes"]
        assert original[start : start + length].decode("utf-8") == content


@pytest.mark.parametrize(
    ("field", "value", "error", "message"),
    [
        ("max_lines", 1, "comparison_line_budget", "comparison budget exceeded"),
        ("position", -1, "comparison_worker_invalid", "invalid comparison input"),
    ],
)
async def test_worker_error_receipts_remain_bounded_json_with_legacy_stdout(
    field: str, value: int, error: str, message: str
) -> None:
    request = worker_request("旧🙂é\r\n原文\x00\n", "新🙂é\r\n修改\x00\n")
    request[field] = value
    result = await run_worker_with_legacy_stdout(request)
    assert result["exit_code"] == 0, result["stderr"]
    assert result["stderr"] == ""
    assert json.loads(result["output"]) == {"error": error, "message": message}
    assert len(result["output"].encode("utf-8")) <= request["max_output_chars"]
