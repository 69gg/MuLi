"""Fixed stdlib-only worker; stdin JSON, one bounded page, no files or network."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from typing import Any


class _BudgetError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code


def _lines(text: str, maximum: int) -> tuple[list[str], list[int], list[int]]:
    """LF-delimited literal lines; stop before allocating an excessive line list."""
    result: list[str] = []
    byte_offsets, char_offsets = [0], [0]
    start = 0
    while start < len(text):
        if len(result) >= maximum:
            raise _BudgetError("comparison_line_budget")
        end = text.find("\n", start)
        end = len(text) if end < 0 else end + 1
        line = text[start:end]
        result.append(line)
        byte_offsets.append(byte_offsets[-1] + len(line.encode("utf-8")))
        char_offsets.append(end)
        start = end
    return result, byte_offsets, char_offsets


def _side(
    identity: dict[str, Any], lines: list[str], offsets: list[int], start: int, end: int
) -> dict[str, Any]:
    offset, length = offsets[start], offsets[end] - offsets[start]
    arguments: dict[str, Any] = {
        "id": identity["source_id"],
        "revision": identity["revision"],
        "offset_bytes": offset,
    }
    if length:
        arguments["length_bytes"] = length
    return {
        "offset_bytes": offset,
        "length_bytes": length,
        "content": "".join(lines[start:end]),
        "read_arguments": arguments,
    }


def compare_page(request: dict[str, Any]) -> dict[str, Any]:
    left, right = request["left"], request["right"]
    maximum, budget = request["max_lines"], request["max_output_chars"]
    position, cursor_chars = request["position"], request["cursor_chars"]
    template = request["template"]
    if (
        not isinstance(left, str)
        or not isinstance(right, str)
        or type(maximum) is not int
        or maximum <= 0
        or type(budget) is not int
        or budget <= 0
        or type(position) is not int
        or position < 0
        or type(cursor_chars) is not int
        or cursor_chars <= 0
        or not isinstance(template, dict)
    ):
        raise ValueError("invalid worker request")
    left_lines, left_bytes, left_chars = _lines(left, maximum)
    right_lines, right_bytes, right_chars = _lines(right, maximum)
    # The input line count is finite before entering this potentially quadratic
    # operation. The owning process runner also enforces a real kill deadline.
    operations = difflib.SequenceMatcher(
        None, left_lines, right_lines, autojunk=False
    ).get_opcodes()
    changed = any(operation[0] != "equal" for operation in operations)
    if position and (position >= len(operations) or operations[position][0] == "equal"):
        raise ValueError("invalid continuation position")
    status = "changed" if changed else "equal"
    items: list[dict[str, Any]] = []

    def fits(candidate: list[dict[str, Any]], has_more: bool) -> bool:
        page = {
            **template,
            "status": status,
            "items": candidate,
            "has_more": has_more,
            "next_cursor": "x" * cursor_chars if has_more else None,
        }
        return len(json.dumps(page, ensure_ascii=False, allow_nan=False)) <= budget

    for index in range(position, len(operations)):
        kind, a_start, a_end, b_start, b_end = operations[index]
        if kind == "equal":
            continue
        # Never join/serialize an arbitrarily large changed block merely to
        # discover that the public page cannot contain it.
        if (
            left_chars[a_end] - left_chars[a_start] + right_chars[b_end] - right_chars[b_start]
            > budget
        ):
            if items:
                return {"status": status, "items": items, "next_index": index}
            raise _BudgetError("comparison_block_budget")
        item = {
            "kind": kind,
            "left": _side(template["left"], left_lines, left_bytes, a_start, a_end),
            "right": _side(template["right"], right_lines, right_bytes, b_start, b_end),
        }
        # Reserve a signed continuation even when this proves to be the final
        # block; a later block must never make an already accepted prefix grow.
        if not fits([*items, item], True):
            if not items:
                raise _BudgetError("comparison_block_budget")
            return {"status": status, "items": items, "next_index": index}
        items.append(item)
    if not fits(items, False):
        raise _BudgetError("output_budget")
    return {"status": status, "items": items, "next_index": None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-limit", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.input_limit <= 0:
            raise ValueError("invalid input allowance")
        raw = sys.stdin.buffer.read(args.input_limit + 1)
        if len(raw) != args.input_limit:
            raise ValueError("unexpected input length")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("invalid request shape")
        result = compare_page(request)
    except _BudgetError as error:
        result = {"error": error.code, "message": "comparison budget exceeded"}
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        result = {"error": "comparison_worker_invalid", "message": "invalid comparison input"}
    # Keep the pipe protocol UTF-8 even when Windows selects an ANSI encoding.
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))


if __name__ == "__main__":
    main()
