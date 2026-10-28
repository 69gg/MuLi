"""A text-free change directory can guide bounded reads of the original sources."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from test_source_comparison import POLICY, changed_texts, compare, save
from test_source_comparison import memory as memory
from test_source_comparison_worker_encoding import worker_request

import muli.source_comparison as comparison
from muli.evidence import EvidenceError, json_size
from muli.memory import Memory
from muli.memory_evidence import MemoryEvidence
from muli.source_comparison_worker import compare_page


async def test_overview_counts_all_changes_without_returning_source_bodies(memory: Memory) -> None:
    texts = {
        "left": "共同🙂\n旧期限\n锚点甲\n删除此行\n锚点乙\n结尾",
        "right": "共同🙂\n新期限\n锚点甲\n锚点乙\n新增条件\n结尾",
    }
    left = save(memory, "a" * 32, texts["left"])
    right = save(memory, "b" * 32, texts["right"])
    before = memory.store.db.total_changes
    page = await compare(memory, left, right, view="overview")
    assert page["view"] == "overview" and page["status"] == "changed"
    assert page["totals"] == {
        "change_groups": 3,
        "insert_groups": 1,
        "delete_groups": 1,
        "replace_groups": 1,
        "removed_lines": 2,
        "added_lines": 2,
        "removed_bytes": len("旧期限\n删除此行\n".encode()),
        "added_bytes": len("新期限\n新增条件\n".encode()),
    }
    for item in page["items"]:
        for name in ("left", "right"):
            side = item[name]
            assert "content" not in side
            lines = texts[name].splitlines(keepends=True)
            expected = "".join(lines[side["start_line"] : side["end_line"]])
            assert side["offset_bytes"] == len("".join(lines[: side["start_line"]]).encode())
            assert side["length_bytes"] == len(expected.encode())
            if side["length_bytes"]:
                read = MemoryEvidence(memory).read(
                    side["read_arguments"]["id"],
                    by_memory=False,
                    max_output_chars=POLICY.max_output_chars,
                    **{key: value for key, value in side["read_arguments"].items() if key != "id"},
                )
                assert read["content"] == expected
            else:
                assert "length_bytes" not in side["read_arguments"]
    assert not page["has_more"] and page["next_cursor"] is None
    assert "旧期限" not in json.dumps(page, ensure_ascii=False)
    assert memory.store.db.total_changes == before
    default = await compare(memory, left, right)
    explicit = await compare(memory, left, right, view="changes")
    assert default == explicit
    assert "view" not in default and "totals" not in default
    assert all("content" in item["left"] for item in default["items"])


@pytest.mark.parametrize(
    ("old", "new", "removed", "added", "removed_lines", "added_lines"),
    [
        ("", "", 0, 0, 0, 0),
        ("相同\n", "相同\n", 0, 0, 0, 0),
        ("", "🙂", 0, 4, 0, 1),
        ("一\n", "", 4, 0, 1, 0),
        ("甲\r\n", "甲\n", 5, 4, 1, 1),
        ("甲\n", "甲", 4, 3, 1, 1),
        ("甲\r乙", "甲\r丙", 7, 7, 1, 1),
    ],
)
async def test_overview_literal_lf_and_empty_source_accounting(
    memory: Memory,
    old: str,
    new: str,
    removed: int,
    added: int,
    removed_lines: int,
    added_lines: int,
) -> None:
    # The worker defines empty-input arithmetic, but retained attachment sources
    # deliberately reject empty text. Do not loosen that existing source rule.
    if old and new:
        left, right = save(memory, "a" * 32, old), save(memory, "b" * 32, new)
        page = await compare(memory, left, right, view="overview")
    else:
        request = worker_request(old, new)
        request["view"] = "overview"
        page = compare_page(request)
    totals = page["totals"]
    assert (totals["removed_bytes"], totals["added_bytes"]) == (removed, added)
    assert (totals["removed_lines"], totals["added_lines"]) == (removed_lines, added_lines)
    assert page["status"] == ("equal" if old == new else "changed")
    assert totals["change_groups"] == int(old != new)


async def test_large_block_directory_still_uses_original_input_and_output_allowances(
    memory: Memory,
) -> None:
    left = save(memory, "a" * 32, "旧🙂" * 5000)
    right = save(memory, "b" * 32, "新🙂" * 5000)
    policy = replace(POLICY, max_output_chars=4000)
    with pytest.raises(EvidenceError) as body:
        await compare(memory, left, right, policy=policy)
    assert body.value.code == "comparison_block_budget"
    page = await compare(memory, left, right, policy=policy, view="overview")
    assert page["totals"]["change_groups"] == 1
    assert page["items"][0]["left"]["length_bytes"] == 35000
    assert json_size(page) <= policy.max_output_chars
    with pytest.raises(EvidenceError) as input_limit:
        await compare(
            memory, left, right, policy=replace(policy, max_input_bytes=100), view="overview"
        )
    assert input_limit.value.code == "comparison_input_budget"
    with pytest.raises(EvidenceError) as output_limit:
        await compare(
            memory, left, right, policy=replace(policy, max_output_chars=100), view="overview"
        )
    assert output_limit.value.code in {"comparison_block_budget", "output_budget"}


async def test_overview_pages_are_complete_and_cursors_are_isolated_from_body_mode(
    memory: Memory,
) -> None:
    one, two = changed_texts()
    left, right = save(memory, "a" * 32, one), save(memory, "b" * 32, two)
    policy = replace(POLICY, max_output_chars=4000)
    cursor: str | None = None
    starts: list[int] = []
    overview_cursor: str | None = None
    for index in range(32):
        page = await compare(memory, left, right, policy=policy, view="overview", cursor=cursor)
        assert json_size(page) <= policy.max_output_chars
        assert ("totals" in page) == (index == 0)
        if index == 0:
            assert page["totals"]["change_groups"] == 16
            overview_cursor = page["next_cursor"]
        starts.extend(item["left"]["start_line"] for item in page["items"])
        if not page["has_more"]:
            break
        assert page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    else:
        pytest.fail("finite overview did not complete")
    assert starts == list(range(2, 34, 2))
    body = await compare(memory, left, right, policy=policy)
    assert overview_cursor and body["next_cursor"]
    for view, token in (("changes", overview_cursor), ("overview", body["next_cursor"])):
        with pytest.raises(EvidenceError) as mismatch:
            await compare(memory, left, right, policy=policy, view=view, cursor=token)
        assert mismatch.value.code == "comparison_cursor_invalid"


@pytest.mark.parametrize("view", ["summary", "", None, True, 1])
async def test_unknown_view_is_rejected_before_starting_a_worker(
    memory: Memory, monkeypatch: pytest.MonkeyPatch, view: Any
) -> None:
    left, right = save(memory, "a" * 32, "甲"), save(memory, "b" * 32, "乙")

    async def forbidden(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("invalid view must fail before starting a worker")

    monkeypatch.setattr(comparison, "run_process", forbidden)
    with pytest.raises(EvidenceError) as invalid:
        await compare(memory, left, right, view=view)
    assert invalid.value.code == "invalid_arguments"


@pytest.mark.parametrize(
    "damage",
    [
        "mid_utf8",
        "wrong_line",
        "bool_line",
        "overlap",
        "leaked_body",
        "bool_total",
        "bad_total",
        "missing_total",
    ],
)
async def test_overview_rejects_malformed_worker_coordinates_or_totals(
    memory: Memory, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    left = save(memory, "a" * 32, "共同\n旧🙂\n")
    right = save(memory, "b" * 32, "共同\n新🙂\n")
    run_process = comparison.run_process

    async def corrupt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await run_process(*args, **kwargs)
        payload = json.loads(result["output"])
        side = payload["items"][0]["left"]
        if damage == "mid_utf8":
            side["offset_bytes"] += 1
            side["length_bytes"] -= 1
            side["read_arguments"].update(
                {"offset_bytes": side["offset_bytes"], "length_bytes": side["length_bytes"]}
            )
        elif damage == "wrong_line":
            side["start_line"] = 0
        elif damage == "bool_line":
            side["start_line"] = True
        elif damage == "overlap":
            payload["items"].append(payload["items"][0])
        elif damage == "leaked_body":
            side["content"] = "不得泄漏正文"
        elif damage == "bool_total":
            payload["totals"]["replace_groups"] = True
        elif damage == "bad_total":
            payload["totals"]["removed_bytes"] += 1
        else:
            del payload["totals"]
        return {**result, "output": json.dumps(payload, ensure_ascii=False)}

    monkeypatch.setattr(comparison, "run_process", corrupt)
    with pytest.raises(EvidenceError) as invalid:
        await compare(memory, left, right, view="overview")
    assert invalid.value.code == "comparison_worker_invalid"
