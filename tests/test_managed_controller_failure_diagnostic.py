"""Real controller/backend failure location; manager evidence and command reply are offline."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from abc import ABCMeta
from pathlib import Path
from typing import Any

import pytest
from test_managed_backend import Host
from test_managed_backend import host as host

import muli.failure_diagnostics as diagnostics
from muli.managed_controller import ManagedTransactionController
from muli.management_transaction import TransactionError, TransactionJournal
from muli.platforms.linux_transaction import ObservationError, ProbeLimits, SystemdProbe
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux journal/flock fixture")


def test_real_backend_has_exact_standard_abc_metaclass() -> None:
    assert type(ManagedLinuxBackend) is ABCMeta


async def test_real_get_unit_failures_have_distinct_safe_package_locations(
    tmp_path: Path,
) -> None:
    """The generic read keeps GetUnit; managed reads now support collected units."""
    locations: dict[str, int] = {}
    cases = (
        ("no_such_unit", 1, "Call failed: Unit muli.service not loaded.\n"),
        ("signal", -signal.SIGKILL, "PRIVATE_SIGNAL_STDERR"),
        ("other", 1, "PRIVATE_OTHER_STDERR"),
    )
    for label, exit_code, stderr in cases:
        root = tmp_path / label
        root.mkdir()
        calls: list[list[str]] = []

        async def failed_get_unit(
            argv: list[str],
            *,
            timeout: float,
            limit: int,
            merge_stderr: bool,
            expected_exit: int = exit_code,
            expected_stderr: str = stderr,
            expected_calls: list[list[str]] = calls,
        ) -> dict[str, Any]:
            assert timeout == 1.0 and limit > 0 and not merge_stderr
            assert argv == [
                "busctl",
                "--user",
                "--json=short",
                "--auto-start=no",
                "--allow-interactive-authorization=no",
                "--timeout=1.0s",
                "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "GetUnit",
                "s",
                "muli.service",
            ]
            expected_calls.append(list(argv))
            return {
                "exit_code": expected_exit,
                "output": "",
                "stderr": expected_stderr,
                "truncated": False,
            }

        service = UserService(root / "config.json", root / "data", root / "units", platform="linux")
        probe = SystemdProbe(runner=failed_get_unit, limits=ProbeLimits(seconds=1.0))
        with pytest.raises(ObservationError, match="^systemd_observation_unavailable$") as caught:
            await probe.read(service)
        assert len(calls) == 1
        diagnostic = diagnostics.capture_failure(caught.value)
        assert diagnostic["exception_type"] == "unknown"
        frames = [
            frame
            for frame in diagnostic["frames"]
            if frame["module"] == "muli.platforms.linux_transaction"
            and frame["function"] == "SystemdProbe._command"
        ]
        assert len(frames) == 1 and type(frames[0]["line"]) is int
        assert frames[0]["line"] > 0
        locations[label] = frames[0]["line"]
        assert stderr not in json.dumps(diagnostic)
        assert len(json.dumps(diagnostic, ensure_ascii=False).encode("utf-8")) <= 4096

    assert len(set(locations.values())) == 3, (
        "the existing safe frame must distinguish fixed NoSuchUnit, signal, and other failures"
    )


@pytest.mark.parametrize("group", ["near_stderr", "other_call", "incomplete_reply"])
async def test_get_unit_failure_classification_rejects_near_matches(group: str) -> None:
    arguments = (
        "call",
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
        "GetUnit",
        "s",
        "muli.service",
    )
    exact = "Call failed: Unit muli.service not loaded.\n"
    original: dict[str, Any] = {
        "exit_code": 1,
        "output": "",
        "stderr": exact,
        "truncated": False,
    }

    async def location(call: tuple[str, ...], reply: dict[str, Any]) -> int:
        async def command(
            argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
        ) -> dict[str, Any]:
            assert tuple(argv[6:]) == call
            assert timeout > 0 and limit > 0 and not merge_stderr
            return reply

        probe = SystemdProbe(runner=command)
        try:
            await probe._command(*call)
        except ObservationError as exc:
            assert str(exc) == "systemd_observation_unavailable"
            diagnostic = diagnostics.capture_failure(exc)
        else:
            pytest.fail("a nonzero observation must still fail")
        frames = [
            frame
            for frame in diagnostic["frames"]
            if frame["module"] == "muli.platforms.linux_transaction"
            and frame["function"] == "SystemdProbe._command"
        ]
        assert len(frames) == 1
        assert type(frames[0]["line"]) is int and frames[0]["line"] > 0
        assert exact not in json.dumps(diagnostic)
        return frames[0]["line"]

    accepted = await location(arguments, original)
    other = await location(arguments, {**original, "stderr": "PRIVATE_OTHER"})
    assert accepted != other
    cases: list[tuple[tuple[str, ...], dict[str, Any]]]
    if group == "near_stderr":
        cases = [
            (arguments, {**original, "stderr": value})
            for value in (
                exact.rstrip("\n"),
                " " + exact,
                "\x1b[31m" + exact + "\x1b[0m",
                exact + "PRIVATE_EXTRA_LINE\n",
                "Call failed: Unit other.service not loaded.\n",
            )
        ]
    elif group == "other_call":
        cases = [
            (arguments[:-1] + ("other.service",), original),
            (arguments[:4] + ("LoadUnit",) + arguments[5:], original),
            (("get-property",) + arguments[1:], original),
        ]
    else:
        cases = [
            (arguments, {**original, "truncated": True}),
            (arguments, {key: value for key, value in original.items() if key != "truncated"}),
            (arguments, {**original, "truncated": 0}),
            (arguments, {**original, "stderr": exact.encode("utf-8")}),
            (arguments, {**original, "exit_code": True}),
            (arguments, {**original, "exit_code": "1"}),
        ]
    for call, reply in cases:
        assert await location(call, reply) == other


async def test_real_stop_failure_keeps_decision_and_safe_original_package_location(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A real package raise is caught by step; no capture or logger is stubbed."""
    calls: list[list[str]] = []
    secret = "PRIVATE_COMMAND_BODY_MUST_NOT_ESCAPE"

    async def failed_command(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert timeout > 0 and limit > 0 and not merge_stderr
        assert argv == [
            "systemctl",
            "--user",
            "--no-block",
            "--job-mode=fail",
            "stop",
            "muli.service",
        ]
        calls.append(list(argv))
        return {"exit_code": 1, "output": secret}

    with TransactionJournal(tmp_path / "journal", tmp_path / "data/muli.sqlite3") as journal:
        with journal.locked() as control:
            host = Host(tmp_path, journal, control, "adopt", change_config=False)
            try:
                host.move("draining")
                original = host.move("activating", "stop_base")
                host.backend.runner = failed_command
                driver = ManagedTransactionController(
                    control, host.backend, host.clock, operation_timeout=10.0
                )
                with caplog.at_level(logging.WARNING, logger="muli.management_controller"):
                    failed = await driver.step()

                assert len(calls) == 1  # Observe succeeded; the real stop command reached its seam.
                assert failed.phase == "rolling_back" and failed.intent == "stop_target"
                assert failed.code == failed.failure_code == "backend_failed"
                assert failed.rollback_attempted and failed.deadline == original.deadline
                assert host.record() == failed

                prefix = "muli.controller_failure.v1 "
                messages = [record.getMessage() for record in caplog.records]
                payloads = [
                    json.loads(text[len(prefix) :]) for text in messages if text.startswith(prefix)
                ]
                assert len(payloads) == 1, (
                    "the swallowed original failure must retain its safe location"
                )
                payload = payloads[0]
                assert set(payload) == {"version", "generation", "revision", "intent", "diagnostic"}
                assert payload["version"] == 1
                assert payload["generation"] == original.generation
                assert payload["revision"] == original.revision
                assert payload["intent"] == original.intent
                diagnostic = payload["diagnostic"]
                assert diagnostic["schema_version"] == 1
                assert diagnostic["scope"] == "muli_package_only"
                assert diagnostic["exception_type"] == "unknown"  # Exact ObservationError type.
                assert len(diagnostic["frames"]) <= 16
                for function in ("ManagedLinuxBackend.stop", "ManagedLinuxBackend._command"):
                    assert any(
                        frame["module"] == "muli.platforms.managed_backend"
                        and frame["function"] == function
                        and type(frame["line"]) is int
                        and frame["line"] > 0
                        for frame in diagnostic["frames"]
                    )
                assert len(json.dumps(diagnostic, ensure_ascii=False).encode("utf-8")) <= 4096
                assert secret not in "\n".join(messages)
            finally:
                host.release_writer()
                await host.backend.__aexit__()


@pytest.mark.parametrize("fault", ["capture", "logger"])
async def test_diagnostic_failure_is_not_retried_or_allowed_to_change_rollback(
    host: Host, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    host.move("draining")
    original = host.move("activating", "stop_base")
    captured: list[BaseException] = []
    logged: list[object] = []
    original_capture = diagnostics.capture_failure

    def capture(exc: BaseException) -> Any:
        captured.append(exc)
        if fault == "capture":
            raise RuntimeError("PRIVATE_CAPTURE_FAILURE")
        return original_capture(exc)

    def warning(*args: object, **kwargs: object) -> None:
        logged.append(args)
        raise RuntimeError("PRIVATE_LOGGER_FAILURE")

    async def rejected(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"exit_code": 1, "output": "PRIVATE_EXTERNAL_REPLY"}

    monkeypatch.setattr(diagnostics, "capture_failure", capture)
    monkeypatch.setattr(logging.getLogger("muli.management_controller"), "warning", warning)
    host.backend.runner = rejected
    driver = ManagedTransactionController(
        host.control, host.backend, host.clock, operation_timeout=10
    )
    first = await driver.step()
    final = await driver.step()
    assert first.phase == "rolling_back" and first.failure_code == "backend_failed"
    assert final.phase == "recovery_required" and final.failure_code == "backend_failed"
    assert final.code == "backend_failed" and final.deadline == original.deadline
    assert len(captured) == 1 and len(logged) == (0 if fault == "capture" else 1)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_cancel_or_invalid_clock_has_no_backend_failure_diagnostic(
    host: Host, caplog: pytest.LogCaptureFixture, cancelled: bool
) -> None:
    host.move("draining")
    original = host.move("activating", "stop_base")

    async def rejected(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if cancelled:
            raise asyncio.CancelledError
        raise TransactionError("clock_invalid")

    host.backend.runner = rejected
    driver = ManagedTransactionController(
        host.control, host.backend, host.clock, operation_timeout=10
    )
    with caplog.at_level(logging.WARNING, logger="muli.management_controller"):
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await driver.step()
            assert host.record() == original
        else:
            failed = await driver.step()
            assert failed.phase == "recovery_required" and failed.code == "clock_invalid"
    assert not any(
        entry.getMessage().startswith("muli.controller_failure.v1 ") for entry in caplog.records
    )
