"""Pure fixture contracts: never import GI, spawn a window or contact a bus."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from helpers import atspi_native_window as fixture
from helpers.atspi_native_session import owned_window, parse_state


def environment() -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "MULI_ATSPI_NATIVE": "1",
        "DISPLAY": ":fixture",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/fixture/private-bus",
    }


@pytest.mark.parametrize("missing", list(environment()))
def test_owned_window_requires_every_explicit_session_gate(missing: str) -> None:
    values = environment()
    del values[missing]
    with pytest.raises(ValueError, match="^owned_atspi_fixture_not_enabled$"):
        fixture.require_fixture_environment(values, "linux")


@pytest.mark.parametrize("platform", ["win32", "darwin", ""])
def test_owned_window_refuses_other_platforms(platform: str) -> None:
    with pytest.raises(ValueError, match="^owned_atspi_fixture_not_enabled$"):
        fixture.require_fixture_environment(environment(), platform)


def test_owned_window_arguments_keep_unicode_out_of_command_text(tmp_path: Path) -> None:
    python = str(tmp_path / "system python")
    title = "muli-atspi-" + "a" * 32
    fixture.require_fixture_environment(environment(), "linux")
    assert fixture.window_arguments(python, title) == (
        python,
        "-I",
        "-B",
        str(Path(fixture.__file__).absolute()),
        title,
    )


@pytest.mark.parametrize("title", ["", "other", "muli-atspi-" + "f" * 33, "$(command)"])
def test_owned_window_refuses_nonfixture_titles(tmp_path: Path, title: str) -> None:
    with pytest.raises(ValueError, match="^invalid_owned_atspi_window_arguments$"):
        fixture.window_arguments(str(tmp_path / "python"), title)


def test_owned_window_refuses_path_lookup_for_its_interpreter() -> None:
    with pytest.raises(ValueError, match="^invalid_owned_atspi_window_arguments$"):
        fixture.window_arguments("python3", "muli-atspi-" + "a" * 32)


def test_independent_control_evidence_preserves_unicode_without_password_contents() -> None:
    state = {
        "kind": "text",
        "pid": 42,
        "title": "muli-atspi-" + "a" * 32,
        "clicks": 1,
        "changes": 1,
        "text": fixture.UNICODE_TEXT,
        "password_unchanged": True,
    }
    data = fixture.event_bytes(state)
    assert data.endswith(b"\n") and len(data) <= fixture.EVENT_BYTES
    assert data.isascii() and json.loads(data) == state
    assert fixture.PASSWORD_TEXT.encode() not in data


def test_independent_control_evidence_has_a_hard_output_limit() -> None:
    with pytest.raises(ValueError, match="^owned_atspi_event_too_large$"):
        fixture.event_bytes({"text": "x" * fixture.EVENT_BYTES})


@pytest.mark.parametrize(
    "changed",
    [
        {"kind": []},
        {"kind": "other"},
        {"pid": True},
        {"pid": 43},
        {"title": "another window"},
        {"clicks": True},
        {"clicks": 5},
        {"changes": -1},
        {"text": None},
        {"password_unchanged": 1},
        {"unexpected": "field"},
    ],
)
def test_native_state_refuses_foreign_or_malformed_evidence(changed: dict[str, object]) -> None:
    state = {
        "kind": "ready",
        "pid": 42,
        "title": "owned",
        "clicks": 0,
        "changes": 0,
        "text": "",
        "password_unchanged": True,
    }
    with pytest.raises(AssertionError, match="exact process"):
        parse_state(fixture.event_bytes({**state, **changed}), 42, "owned")


@pytest.mark.parametrize("data", [b"", b"{}", b"invalid\n", b"x" * 4097 + b"\n"])
def test_native_state_refuses_missing_or_oversized_frames(data: bytes) -> None:
    with pytest.raises(AssertionError):
        parse_state(data, 42, "owned")


def test_native_state_accepts_independent_unicode_evidence() -> None:
    state = {
        "kind": "text",
        "pid": 42,
        "title": "owned",
        "clicks": 1,
        "changes": 1,
        "text": fixture.UNICODE_TEXT,
        "password_unchanged": True,
    }
    assert parse_state(fixture.event_bytes(state), 42, "owned") == state


@pytest.mark.skipif(os.name != "posix", reason="Owned POSIX process cleanup; no GUI is loaded")
async def test_native_observer_reaps_a_late_created_owned_child_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = asyncio.create_subprocess_exec
    created: list[asyncio.subprocess.Process] = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def start(*arguments: str, **kwargs: Any) -> asyncio.subprocess.Process:
        # This is a plain temporary Python child, never the GTK producer.
        process = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        created.append(process)
        entered.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)

    async def observe() -> None:
        async with owned_window(sys.executable, "muli-atspi-" + "a" * 32):
            pytest.fail("Cancelled startup cannot publish a native window")

    observer = asyncio.create_task(observe())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        observer.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(observer, 2)
        assert len(created) == 1 and created[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(created[0].pid, 0)
    finally:
        release.set()
        if not observer.done():
            observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)


@pytest.mark.skipif(os.name != "posix", reason="Owned POSIX process cleanup; no GUI is loaded")
async def test_native_observer_rejects_wrong_process_evidence_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = asyncio.create_subprocess_exec
    created: list[asyncio.subprocess.Process] = []
    state = {
        "kind": "ready",
        "pid": -1,
        "title": "muli-atspi-" + "a" * 32,
        "clicks": 0,
        "changes": 0,
        "text": "",
        "password_unchanged": True,
    }
    source = f"import time; print({json.dumps(state)!r}, flush=True); time.sleep(30)"

    async def start(*arguments: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(sys.executable, "-c", source, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    with pytest.raises(AssertionError, match="exact process"):
        async with owned_window(sys.executable, "muli-atspi-" + "a" * 32):
            pytest.fail("Foreign readiness cannot publish a native window")
    assert len(created) == 1 and created[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(created[0].pid, 0)
