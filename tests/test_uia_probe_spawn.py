"""The console experiment delegates to fake children without starting a desktop."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from helpers import uia_probe_diagnostics as diagnostic
from helpers.uia_probe_spawn import SPAWN_MODE_ENV, SpawnMode, install_probe_spawn, probe_spawn_mode
from test_uia_pending_observation import _Harness
from test_uia_probe_diagnostics import owned_probe as owned_probe

from muli.tools import ax_lease
from muli.tools.ax_lease import AXLeaseError


@pytest.fixture(autouse=True)
def isolated_spawn(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def forbidden(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        pytest.fail("The console experiment's portable tests must not start a process")

    monkeypatch.delenv(SPAWN_MODE_ENV, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 1 << 27, raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    yield


def test_unset_mode_leaves_the_original_spawn_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    original = asyncio.create_subprocess_exec
    assert probe_spawn_mode() is None
    assert install_probe_spawn(monkeypatch, ["owned-test-host"], None) is None
    assert asyncio.create_subprocess_exec is original


@pytest.mark.parametrize("value", ["", "0", "CREATE_NO_WINDOW", "inherit", "default "])
def test_unknown_environment_mode_fails_before_spawn(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    original = asyncio.create_subprocess_exec
    monkeypatch.setenv(SPAWN_MODE_ENV, value)
    with pytest.raises(AssertionError, match="Unknown owned UIA probe spawn mode"):
        probe_spawn_mode()
    assert asyncio.create_subprocess_exec is original


@pytest.mark.parametrize("mode", ["create-no-window", "default"])
async def test_only_the_exact_owned_childs_flag_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mode: SpawnMode,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    process = cast(asyncio.subprocess.Process, object())

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv(SPAWN_MODE_ENV, mode)
    expected = ("owned-test-host", "-NoProfile", "-NonInteractive", "-Mta", "-File", "owned.ps1")
    environment = {"OWNED_TEST_VALUE": "unchanged"}
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "start_new_session": False,
        "env": environment,
        "cwd": tmp_path,
        "limit": 4096,
    }
    controlled = install_probe_spawn(monkeypatch, expected, probe_spawn_mode())
    assert controlled is not None
    result = await asyncio.create_subprocess_exec(*expected, **kwargs)
    flags = subprocess.CREATE_NO_WINDOW if mode == "create-no-window" else 0
    assert result is process
    assert calls == [(expected, {**kwargs, "creationflags": flags})]
    assert calls[0][1]["env"] is environment and calls[0][1]["cwd"] is tmp_path
    assert controlled.calls == 1 and controlled.actual_flags == flags

    foreign = (*expected[:-1], "different-owned.ps1")
    assert await asyncio.create_subprocess_exec(*foreign, **kwargs) is process
    assert calls[1] == (foreign, kwargs) and controlled.calls == 1
    with pytest.raises(AssertionError, match="spawn only once"):
        await asyncio.create_subprocess_exec(*expected, **kwargs)
    assert len(calls) == 2
    assert capsys.readouterr().out == f"uia_probe_spawn mode={mode} actual_flags={flags}\n"


@pytest.mark.parametrize("missing_pipe", ["stdin", "stdout", "stderr", "creationflags"])
async def test_original_pipe_and_flag_contract_is_required_before_delegating(
    monkeypatch: pytest.MonkeyPatch, missing_pipe: str
) -> None:
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }
    kwargs[missing_pipe] = 0
    controlled = install_probe_spawn(monkeypatch, ["owned-test-host"], "default")
    assert controlled is not None
    with pytest.raises(AssertionError, match="original console flag|retain all three pipes"):
        await asyncio.create_subprocess_exec("owned-test-host", **kwargs)
    assert controlled.calls == 0


@pytest.mark.parametrize("mode", ["create-no-window", "default"])
@pytest.mark.parametrize("failure_type", [OSError, asyncio.CancelledError])
async def test_spawn_exception_and_cancellation_are_preserved_without_retry(
    monkeypatch: pytest.MonkeyPatch, mode: SpawnMode, failure_type: type[BaseException]
) -> None:
    failure = failure_type("private original spawn failure")
    calls = 0

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    controlled = install_probe_spawn(monkeypatch, ["owned-test-host"], mode)
    with pytest.raises(failure_type) as caught:
        await asyncio.create_subprocess_exec(
            "owned-test-host",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    assert caught.value is failure
    assert controlled is not None and controlled.calls == calls == 1


@pytest.mark.parametrize("mode", ["create-no-window", "default"])
@pytest.mark.parametrize("fails", [False, True], ids=["receipt", "original-error"])
async def test_same_lease_keeps_deadline_single_request_error_and_owned_cleanup(
    owned_probe: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    mode: SpawnMode,
    fails: bool,
) -> None:
    case, lease = owned_probe, owned_probe.lease
    monkeypatch.setattr(ax_lease, "os", SimpleNamespace(name="nt"))
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        calls.append((argv, kwargs))
        return await case.spawn(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    expected, expires = tuple(lease._argv), lease.expires_at
    assert lease._timeout == 12
    controlled = install_probe_spawn(monkeypatch, expected, mode)
    failure = AXLeaseError("read_timeout", "private original response failure")
    operation = asyncio.create_task(diagnostic.observe_probe(lease))
    try:
        async with asyncio.timeout(2):
            await case.writer.drain_entered.wait()
        assert len(case.writer.frames) == case.writer.drains == 1
        frame = json.loads(case.writer.frames[0])
        assert frame["command"] == "probe" and frame["payload"] == {}
        assert 0 < frame["limits"]["timeout_seconds"] <= 12
        if fails:
            assert lease._pending is not None
            lease._pending.set_exception(failure)
            with pytest.raises(AXLeaseError) as caught:
                await operation
            assert caught.value is failure
        else:
            response = {"v": 1, "request_id": frame["request_id"], "result": {"fake": True}}
            case.process.stdout.feed_data(json.dumps(response).encode("utf-8") + b"\n")
            assert await operation == {"fake": True}
        flags = subprocess.CREATE_NO_WINDOW if mode == "create-no-window" else 0
        assert calls == [
            (
                expected,
                {
                    "stdin": asyncio.subprocess.PIPE,
                    "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE,
                    "start_new_session": False,
                    "creationflags": flags,
                },
            )
        ]
        assert controlled is not None and controlled.calls == 1
        assert (
            tuple(lease._argv) == expected and lease.expires_at == expires and lease._timeout == 12
        )
        assert case.spawns == case.cleanups == lease._count == 1
        assert case.writer.closes == case.writer.waits == 1
        assert lease.closed and case.process.returncode is not None
        assert all(reader.done() for reader in lease._readers)
    finally:
        case.spawn_release.set()
        case.cleanup_release.set()
        case.writer.drain_release.set()
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
