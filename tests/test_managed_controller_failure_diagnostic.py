"""Real controller/backend failure location; manager evidence and command reply are offline."""

from __future__ import annotations

import asyncio
import json
import logging
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
from muli.platforms.managed_backend import ManagedLinuxBackend

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux journal/flock fixture")


def test_real_backend_has_exact_standard_abc_metaclass() -> None:
    assert type(ManagedLinuxBackend) is ABCMeta


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
