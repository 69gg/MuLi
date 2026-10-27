from __future__ import annotations

import sys

import pytest

from muli.process import run_process


async def test_binary_stdin_is_delivered_without_text_encoding() -> None:
    data = b"\x00\xffPDF\r\n\x80"
    result = await run_process(
        [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().hex())"],
        input_bytes=data,
        limit=100,
        timeout=2,
    )
    assert result["exit_code"] == 0 and result["output"].strip() == data.hex()


async def test_binary_and_text_stdin_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="stdin|输入"):
        await run_process(
            [sys.executable, "-c", "raise AssertionError()"], input_text="", input_bytes=b""
        )


async def test_strict_output_rejects_invalid_utf8_while_default_remains_compatible() -> None:
    arguments = [sys.executable, "-c", "import os; os.write(1, bytes([255]))"]
    default = await run_process(arguments, timeout=2, limit=10)
    assert default["output"] == "\ufffd"
    with pytest.raises(UnicodeDecodeError):
        await run_process(arguments, timeout=2, limit=10, strict_output=True)
