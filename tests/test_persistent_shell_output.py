"""Real child pipes exercise UTF-8 cursors and complete shell receipt budgets."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from muli.process import _settle_process, _wait_cleanup
from muli.shell_protocol import ShellSpec, quote, select_shell
from muli.shell_sessions import (
    OutputCursor,
    ShellSessionError,
    ShellSessions,
    StreamName,
    _Command,
    _Output,
    _Shell,
)


def cursor_at(output: int = 0, stderr: int = 0) -> OutputCursor:
    return {"output": output, "stderr": stderr}


@asynccontextmanager
async def child(source: str) -> AsyncIterator[asyncio.subprocess.Process]:
    starting = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-u",
            "-c",
            source,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
            creationflags=(
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
            ),
        )
    )

    async def settle() -> None:
        try:
            process = await asyncio.shield(starting)
        except Exception:
            return
        await _settle_process(process, ())

    try:
        async with asyncio.timeout(3):
            process = await asyncio.shield(starting)
            yield process
    finally:
        if await _wait_cleanup(asyncio.create_task(settle())):
            raise asyncio.CancelledError


async def receive(
    process: asyncio.subprocess.Process,
    output: _Output,
    stream: StreamName,
    count: int,
    *,
    limit: int = 64,
) -> None:
    pipe = process.stdout if stream == "output" else process.stderr
    assert pipe is not None
    output.append(stream, await pipe.readexactly(count), limit)


async def release(process: asyncio.subprocess.Process) -> None:
    assert process.stdin is not None
    process.stdin.write(b"g")
    await process.stdin.drain()


async def finish(process: asyncio.subprocess.Process) -> None:
    assert process.stdout is not None and process.stderr is not None
    assert await process.stdout.read() == b""
    assert await process.stderr.read() == b""
    assert await process.wait() == 7


def split_source(descriptor: int, first: bytes, second: bytes) -> str:
    return (
        "import os, sys\n"
        f"os.write({descriptor}, {first!r})\n"
        "if sys.stdin.buffer.read(1) != b'g':\n"
        "    raise SystemExit(2)\n"
        f"os.write({descriptor}, {second!r})\n"
        "raise SystemExit(7)\n"
    )


@pytest.mark.parametrize("stream", ["output", "stderr"])
async def test_live_utf8_tail_is_consumed_only_after_the_second_child_write(
    stream: StreamName,
) -> None:
    output = _Output()
    descriptor = 1 if stream == "output" else 2
    async with child(split_source(descriptor, b"\xe4", b"\xb8\xad")) as process:
        await receive(process, output, stream, 1)
        assert process.returncode is None
        assert output.observed == output.retained == 1 and output.discarded == 0
        first = output.read(cursor_at())
        assert first == ("", "", cursor_at())
        await release(process)
        await receive(process, output, stream, 2)
        end = cursor_at(3) if stream == "output" else cursor_at(stderr=3)
        expected = ("中", "", end) if stream == "output" else ("", "中", end)
        second = output.read(first[2])
        assert second == expected
        assert output.read(second[2]) == ("", "", end)
        await finish(process)
        assert output.read(cursor_at(), final=True) == expected
        assert output.read(second[2], final=True) == ("", "", end)


async def test_partial_stdout_does_not_block_or_repeat_complete_stderr() -> None:
    output = _Output()
    diagnostic = b"diagnostic"
    source = (
        "import os, sys\n"
        "os.write(1, b'\\xe4')\n"
        f"os.write(2, {diagnostic!r})\n"
        "if sys.stdin.buffer.read(1) != b'g':\n"
        "    raise SystemExit(2)\n"
        "os.write(1, b'\\xb8\\xad')\n"
        "raise SystemExit(7)\n"
    )
    async with child(source) as process:
        await receive(process, output, "output", 1)
        await receive(process, output, "stderr", len(diagnostic))
        first = output.read(cursor_at())
        assert first == ("", diagnostic.decode("ascii"), cursor_at(stderr=len(diagnostic)))
        assert output.observed == 1 + len(diagnostic)
        await release(process)
        await receive(process, output, "output", 2)
        second = output.read(first[2])
        assert second == ("中", "", cursor_at(3, len(diagnostic)))
        assert output.read(second[2]) == ("", "", second[2])
        await finish(process)
        assert output.read(second[2], final=True) == ("", "", second[2])


@pytest.mark.parametrize(
    ("payload", "running", "final_tail"),
    [
        (b"prefix:\xe4\xb8\xad", "prefix:中", ""),
        (b"prefix:\xff", "prefix:�", ""),
        (b"prefix:\xe4\xb8", "prefix:", "�"),
    ],
    ids=["complete", "invalid", "incomplete-at-eof"],
)
async def test_real_child_eof_flushes_only_the_unconsumed_utf8_tail(
    payload: bytes, running: str, final_tail: str
) -> None:
    output = _Output()
    async with child(split_source(1, payload, b"")) as process:
        await receive(process, output, "output", len(payload))
        text, error, cursor = output.read(cursor_at())
        assert text == running and error == ""
        assert cursor == cursor_at(len(payload) - 2 if final_tail else len(payload))
        assert process.returncode is None
        await release(process)
        await finish(process)
        assert output.read(cursor, final=True) == (final_tail, "", cursor_at(len(payload)))
        assert output.read(cursor_at(), final=True) == (
            running + final_tail,
            "",
            cursor_at(len(payload)),
        )
        assert output.read(cursor_at(len(payload)), final=True) == ("", "", cursor_at(len(payload)))


@pytest.mark.parametrize("limit", [2, 3])
async def test_truncated_real_pipe_keeps_absolute_cursors_and_reports_lost_utf8_prefix(
    limit: int,
) -> None:
    output = _Output()
    async with child(split_source(1, b"0123\xe4", b"\xb8\xad")) as process:
        await receive(process, output, "output", 5, limit=limit)
        first = output.read(cursor_at())
        assert first == ("0123"[-(limit - 1) :], "", cursor_at(4))
        assert output.observed == 5 and output.retained == limit
        assert output.discarded == 5 - limit
        await release(process)
        await receive(process, output, "output", 2, limit=limit)
        expected = "中" if limit == 3 else "��"
        assert output.read(first[2]) == (expected, "", cursor_at(7))
        assert output.observed == 7 and output.retained == limit
        assert output.discarded == 7 - limit
        assert output.read(cursor_at(7)) == ("", "", cursor_at(7))
        await finish(process)
        assert output.read(first[2], final=True) == (expected, "", cursor_at(7))
        assert output.read(cursor_at(5), final=True) == ("��", "", cursor_at(7))


@asynccontextmanager
async def published_child(
    source: str, directory: Path, budget: list[int]
) -> AsyncIterator[tuple[ShellSessions, _Command, asyncio.subprocess.Process]]:
    # Exercise publication with real pipe bytes/PID/exit status. Native shell
    # completion framing has its own Runtime journey, not a fake adapter here.
    temporary = tempfile.TemporaryDirectory(prefix="muli-output-test-")
    try:
        async with child(source) as process:
            manager = ShellSessions(
                default_cwd=directory,
                max_sessions=lambda: 2,
                max_output_bytes=lambda: 4096,
                max_command_bytes=lambda: 4096,
                max_run_seconds=lambda: 3,
                max_receipt_chars=lambda: budget[0],
            )
            shell = _Shell(
                "pipe-owner",
                ShellSpec(sys.executable, "pipe"),
                temporary,
                str(directory),
                state="busy",
                process=process,
            )
            item = _Command("pipe-command", shell, asyncio.get_running_loop().time() + 3)
            shell.current = item
            manager._retain_command(item)
            yield manager, item, process
    finally:
        temporary.cleanup()


async def complete_child(item: _Command, process: asyncio.subprocess.Process) -> None:
    await finish(process)
    item.state = "completed"
    item.exit_code = process.returncode
    item.shell.state = "idle"
    item.done.set()


def receipt_size(receipt: dict[str, Any]) -> int:
    encoded = json.dumps(receipt, ensure_ascii=False)
    assert json.loads(encoded) == receipt
    return len(encoded)


async def test_receipt_budget_pages_actual_dual_pipe_bytes_without_losing_terminal_status(
    tmp_path: Path,
) -> None:
    expected_output = '中"\\\n' * 12 + "output-end"
    expected_error = '界"\\\t' * 10 + "error-end"
    output_bytes, error_bytes = expected_output.encode(), expected_error.encode()
    source = (
        "import os, sys\n"
        f"os.write(1, {output_bytes!r})\n"
        f"os.write(2, {error_bytes!r})\n"
        "if sys.stdin.buffer.read(1) != b'g':\n"
        "    raise SystemExit(2)\n"
        "raise SystemExit(7)\n"
    )
    budget = [4096]
    async with published_child(source, tmp_path, budget) as (manager, item, process):
        await receive(process, item.output, "output", len(output_bytes), limit=4096)
        await receive(process, item.output, "stderr", len(error_bytes), limit=4096)
        await release(process)
        await complete_child(item, process)
        original = await manager.read("pipe-owner", item.id)
        assert original["output"] == expected_output and original["stderr"] == expected_error
        budget[0] = (
            receipt_size(
                await manager.read("pipe-owner", item.id, cursor=item.output.observed_by_stream)
            )
            + 24
        )
        assert receipt_size(original) > budget[0]
        output_parts: list[str] = []
        error_parts: list[str] = []
        cursor, pages = cursor_at(), 0
        while sum(cursor.values()) < item.output.observed:
            page = await manager.read("pipe-owner", item.id, cursor=cursor)
            assert set(page) == set(original)
            assert receipt_size(page) <= budget[0]
            assert page["state"] == "completed" and page["exit_code"] == 7
            assert page["shell_pid"] == process.pid
            assert page["os_epoch"] == original["os_epoch"] and page["command_id"] == item.id
            assert page["cursor"] == cursor
            assert sum(cursor.values()) < sum(page["next_cursor"].values()) <= item.output.observed
            for stream in ("output", "stderr"):
                assert page["next_cursor"][stream] - cursor[stream] == len(page[stream].encode())
            assert page["discarded_bytes"] == 0 and page["truncated"] is False
            output_parts.append(page["output"])
            error_parts.append(page["stderr"])
            cursor = page["next_cursor"]
            pages += 1
        assert pages > 1
        assert "".join(output_parts) == expected_output
        assert "".join(error_parts) == expected_error
        tail = await manager.read("pipe-owner", item.id, cursor=cursor)
        assert tail["output"] == tail["stderr"] == ""
        assert tail["next_cursor"] == cursor
        assert tail["observed_bytes"] == sum(cursor.values())
        assert tail["exit_code"] == 7 and receipt_size(tail) <= budget[0]


async def test_receipt_budget_retains_both_live_partial_characters_until_real_completion(
    tmp_path: Path,
) -> None:
    prefix = b'"\\\n' * 8
    first = prefix + b"\xe4"
    source = (
        "import os, sys\n"
        f"os.write(1, {first!r})\n"
        "os.write(2, b'\\xe7')\n"
        "if sys.stdin.buffer.read(1) != b'g':\n"
        "    raise SystemExit(2)\n"
        "os.write(1, b'\\xb8\\xad')\n"
        "os.write(2, b'\\x95\\x8c')\n"
        "raise SystemExit(7)\n"
    )
    budget = [4096]
    async with published_child(source, tmp_path, budget) as (manager, item, process):
        await receive(process, item.output, "output", len(prefix) + 1)
        await receive(process, item.output, "stderr", 1)
        budget[0] = (
            receipt_size(
                await manager.read("pipe-owner", item.id, cursor=item.output.observed_by_stream)
            )
            + 12
        )
        pieces: list[str] = []
        cursor = cursor_at()
        while cursor["output"] < len(prefix):
            page = await manager.read("pipe-owner", item.id, cursor=cursor)
            assert receipt_size(page) <= budget[0]
            assert page["state"] == "running" and page["exit_code"] is None
            assert page["stderr"] == "" and "�" not in page["output"]
            assert cursor["output"] < page["next_cursor"]["output"] <= len(prefix)
            assert page["next_cursor"]["stderr"] == 0
            pieces.append(page["output"])
            cursor = page["next_cursor"]
        assert "".join(pieces).encode() == prefix
        waiting = await manager.read("pipe-owner", item.id, cursor=cursor)
        assert waiting["output"] == waiting["stderr"] == ""
        assert waiting["next_cursor"] == cursor
        assert sum(cursor.values()) < waiting["observed_bytes"]
        await release(process)
        await receive(process, item.output, "output", 2)
        await receive(process, item.output, "stderr", 2)
        await complete_child(item, process)
        final = await manager.read("pipe-owner", item.id, cursor=cursor)
        assert receipt_size(final) <= budget[0]
        assert final["output"] == "中" and final["stderr"] == "界"
        assert final["next_cursor"] == cursor_at(len(prefix) + 3, 3)
        assert final["observed_bytes"] == len(prefix) + 6
        assert final["state"] == "completed" and final["exit_code"] == 7


async def test_read_metadata_budget_failure_preserves_the_real_command_and_output(
    tmp_path: Path,
) -> None:
    payload = "可续读".encode()
    budget = [4096]
    async with published_child(split_source(1, payload, b""), tmp_path, budget) as (
        manager,
        item,
        process,
    ):
        await receive(process, item.output, "output", len(payload))
        before = (
            item.state,
            tuple(item.output.chunks),
            item.output.observed,
            item.output.discarded,
        )
        budget[0] = 1
        with pytest.raises(ShellSessionError) as rejected:
            await manager.read("pipe-owner", item.id)
        assert rejected.value.code == "output_budget"
        assert (
            item.state,
            tuple(item.output.chunks),
            item.output.observed,
            item.output.discarded,
        ) == before
        assert process.returncode is None and not item.done.is_set()
        budget[0] = 4096
        recovered = await manager.read("pipe-owner", item.id)
        assert recovered["state"] == "running" and recovered["output"].encode() == payload
        assert recovered["cursor"] == cursor_at()
        assert recovered["next_cursor"] == cursor_at(len(payload))
        await release(process)
        await complete_child(item, process)
        final = await manager.read("pipe-owner", item.id)
        assert final["state"] == "completed" and final["exit_code"] == 7
        assert final["output"].encode() == payload


async def test_impossible_metadata_budget_rejects_before_the_real_shell_command_dispatch(
    tmp_path: Path,
) -> None:
    spec = select_shell()
    script, effect = tmp_path / "receipt-effect.py", tmp_path / "must-not-exist.txt"
    script.write_text(
        "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('effect', encoding='utf-8')\n",
        encoding="utf-8",
    )
    manager = ShellSessions(
        default_cwd=tmp_path,
        executable=spec.executable,
        max_sessions=lambda: 2,
        max_output_bytes=lambda: 4096,
        max_command_bytes=lambda: 4096,
        max_run_seconds=lambda: 3,
        max_receipt_chars=lambda: 1,
    )
    command = (
        f"{quote(sys.executable, spec.flavor)} -I {quote(str(script), spec.flavor)} "
        f"{quote(str(effect), spec.flavor)}"
    )
    try:
        async with asyncio.timeout(4):
            with pytest.raises(ShellSessionError) as rejected:
                await manager.execute("owner", command, admission_guard=lambda: None)
            assert rejected.value.code == "output_budget"
            assert "尚未派发" in str(rejected.value)
            assert not effect.exists()
            assert manager.live_sessions() == ()
            assert not manager.is_running("owner") and manager.current_cwd("owner") is None
    finally:
        await manager.aclose()


@pytest.mark.parametrize("completed", [False, True], ids=["running", "completed"])
async def test_interleaved_partial_stdout_and_long_stderr_progress_with_a_thousand_char_receipt(
    tmp_path: Path, completed: bool
) -> None:
    diagnostic = b"x" * 900
    source = (
        "import os, sys\n"
        "os.write(1, b'\\xe4')\n"
        f"os.write(2, {diagnostic!r})\n"
        "if sys.stdin.buffer.read(1) != b'g':\n"
        "    raise SystemExit(2)\n"
        "os.write(1, b'\\xb8\\xad')\n"
        "if sys.stdin.buffer.read(1) != b'g':\n"
        "    raise SystemExit(2)\n"
        "raise SystemExit(7)\n"
    )
    async with published_child(source, tmp_path, [1000]) as (manager, item, process):
        await receive(process, item.output, "output", 1, limit=4096)
        await receive(process, item.output, "stderr", len(diagnostic), limit=4096)
        first = await manager.read("pipe-owner", item.id)
        assert receipt_size(first) <= 1000
        assert first["output"] == "" and first["stderr"]
        assert first["next_cursor"]["output"] == 0
        assert 0 < first["next_cursor"]["stderr"] < len(diagnostic)
        assert first["state"] == "running" and first["exit_code"] is None
        cursor = first["next_cursor"]
        outputs, errors = [first["output"]], [first["stderr"]]
        await release(process)
        await receive(process, item.output, "output", 2, limit=4096)
        if completed:
            await release(process)
            await complete_child(item, process)
        pages = 1
        while sum(cursor.values()) < item.output.observed:
            page = await manager.read("pipe-owner", item.id, cursor=cursor)
            assert receipt_size(page) <= 1000
            assert page["discarded_bytes"] == 0 and page["truncated"] is False
            assert page["state"] == ("completed" if completed else "running")
            assert page["exit_code"] == (7 if completed else None)
            for stream in ("output", "stderr"):
                assert page["next_cursor"][stream] - cursor[stream] == len(page[stream].encode())
            assert sum(page["next_cursor"].values()) > sum(cursor.values())
            if cursor["output"] == 0:
                assert page["output"] == "中" and page["stderr"]
            outputs.append(page["output"])
            errors.append(page["stderr"])
            cursor = page["next_cursor"]
            pages += 1
        assert pages > 1
        assert "".join(outputs) == "中"
        assert "".join(errors).encode() == diagnostic
        assert cursor == cursor_at(3, len(diagnostic))
        assert item.output.observed == item.output.retained == 903
        assert item.output.discarded == 0
        if not completed:
            await release(process)
            await complete_child(item, process)
        final = await manager.read("pipe-owner", item.id, cursor=cursor)
        assert final["output"] == final["stderr"] == ""
        assert final["next_cursor"] == cursor and final["exit_code"] == 7


async def native_completed(manager: ShellSessions, receipt: dict[str, Any]) -> dict[str, Any]:
    for _ in range(3):
        if receipt["state"] != "running":
            break
        receipt = await manager.read(
            "owner", receipt["command_id"], cursor=receipt["next_cursor"], wait_seconds=1
        )
    assert receipt["state"] == "completed" and receipt["exit_code"] == 0
    return await manager.read("owner", receipt["command_id"])


async def test_completed_command_flushes_its_partial_utf8_once_while_native_shell_stays_idle(
    tmp_path: Path,
) -> None:
    spec = select_shell()
    manager = ShellSessions(
        default_cwd=tmp_path,
        executable=spec.executable,
        max_sessions=lambda: 2,
        max_output_bytes=lambda: 4096,
        max_command_bytes=lambda: 4096,
        max_run_seconds=lambda: 3,
        max_receipt_chars=lambda: 1000,
    )
    script = tmp_path / "partial-output.py"
    script.write_text("import os\nos.write(1, b'\\xe4\\xb8')\n", encoding="utf-8")
    command = (
        "[Console]::OpenStandardOutput().Write([byte[]](0xe4, 0xb8), 0, 2)"
        if spec.flavor == "pwsh"
        else f"{quote(sys.executable, spec.flavor)} -I {quote(str(script), spec.flavor)}"
    )
    try:
        async with asyncio.timeout(4):
            boot = await native_completed(
                manager,
                await manager.execute("owner", "echo SHELL_READY", admission_guard=lambda: None),
            )
            receipt = await native_completed(
                manager,
                await manager.execute("owner", command, admission_guard=lambda: None),
            )
            assert receipt_size(receipt) <= 1000
            assert receipt["output"] == "�" and receipt["stderr"] == ""
            assert receipt["next_cursor"] == cursor_at(2)
            assert receipt["output_scope"] == "shell" and receipt["shell_state"] == "idle"
            assert receipt["shell_pid"] == boot["shell_pid"]
            assert receipt["os_epoch"] == boot["os_epoch"]
            process = manager._sessions["owner"].process
            assert process is not None and process.returncode is None
            assert manager.live_sessions() == ("owner",) and not manager.is_running("owner")
            after = await manager.read(
                "owner", receipt["command_id"], cursor=receipt["next_cursor"]
            )
            assert after["output"] == after["stderr"] == ""
            assert after["next_cursor"] == cursor_at(2)
            assert after["state"] == "completed" and after["exit_code"] == 0
    finally:
        await manager.aclose()
