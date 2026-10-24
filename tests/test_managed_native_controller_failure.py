"""Offline helper journal collection; controller, safe capture and result IO are real."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest
from helpers import managed_native_worker as worker
from test_managed_backend import Host
from test_managed_native_ci import ci

from muli.managed_controller import ManagedTransactionController
from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
from muli.management_transaction import TransactionJournal
from muli.platforms.linux_transaction import SystemdProbe

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux helper identity fixture")
PREFIX = "muli.controller_failure.v1 "


@pytest.fixture(autouse=True)
def no_real_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("offline helper diagnostics must not start a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)


@pytest.fixture
async def failure_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> AsyncIterator[tuple[dict[str, Any], bytes]]:
    with TransactionJournal(tmp_path / "journal", tmp_path / "data/muli.sqlite3") as journal:
        with journal.locked() as control:
            host = Host(tmp_path, journal, control, "adopt", change_config=False)
            try:
                host.move("draining")
                original = host.move("activating", "stop_base")
                case = worker.Case(tmp_path)
                helper = managed_helper_unit_name(original)
                argv = managed_helper_arguments(host.anchor, host.slots["base"])
                case.result["generations"].append(
                    {
                        "deployment": original.identity.deployment,
                        "generation": original.generation,
                        "helper": helper,
                    }
                )
                uid = 12345
                snapshot = replace(
                    host.probe.snapshot.unit,
                    name=helper,
                    argv=argv,
                    pid=54321,
                    invocation="4" * 32,
                    cgroup=f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{helper}",
                )

                class HelperProbe:
                    async def helper_exists(self, name: str) -> bool:
                        assert name == helper
                        return True

                    async def _unit_path(self, name: str) -> str:
                        assert name == helper
                        return "/fixed/helper"

                    async def _snapshot(self, path: str) -> Any:
                        assert path == "/fixed/helper"
                        return snapshot

                    async def _properties(self, *args: Any) -> dict[str, Any]:
                        return {"Result": "success", "ExecMainStatus": 0}

                def note(record: Any) -> None:
                    case.note(
                        "transaction",
                        generation=record.generation,
                        phase=record.phase,
                        activation=record.activation,
                        revision=record.revision,
                        intent=record.intent,
                        code=record.code,
                        failure_code=record.failure_code,
                        rollback_attempted=record.rollback_attempted,
                    )

                note(original)
                with monkeypatch.context() as context:
                    context.setattr(os, "geteuid", lambda: uid)
                    assert not await case.helper_empty(
                        cast(SystemdProbe, HelperProbe()), helper, argv
                    )

                async def rejected(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    return {"exit_code": 1, "output": "PRIVATE_EXTERNAL_REPLY"}

                host.backend.runner = rejected
                with caplog.at_level(logging.WARNING, logger="muli.management_controller"):
                    failed = await ManagedTransactionController(
                        control, host.backend, host.clock, operation_timeout=10
                    ).step()
                assert failed.phase == "rolling_back" and failed.failure_code == "backend_failed"
                note(failed)
                case.result["code"] = "native_transaction_blocked"
                messages = [entry.getMessage() for entry in caplog.records]
                message = next(value for value in messages if value.startswith(PREFIX))
                source = case.result["resources"]["helper_source"]
                row = {
                    "_UID": str(uid),
                    "_PID": str(snapshot.pid),
                    "_SYSTEMD_USER_UNIT": helper,
                    "_SYSTEMD_INVOCATION_ID": snapshot.invocation,
                    "MESSAGE": message,
                }
                assert source["pid"] == snapshot.pid
                saved = ci.fixture_result(json.dumps(case.result).encode())
                yield saved, json.dumps(row, separators=(",", ":")).encode() + b"\n"
            finally:
                host.release_writer()
                await host.backend.__aexit__()


def observer(root: Path, result: dict[str, Any], reply: bytes) -> tuple[Any, list[tuple[str, ...]]]:
    root.mkdir(mode=0o700)
    calls: list[tuple[str, ...]] = []

    def run(argv: Sequence[str], *, seconds: float = 5) -> Any:
        values = tuple(argv)
        calls.append(values)
        source = result["resources"]["helper_source"]
        assert values[:4] == ("sudo", "--non-interactive", "--", "journalctl")
        assert "--user" not in values and "--boot=0" in values and "--lines=2" in values
        for field, value in (
            ("_UID", source["uid"]),
            ("_PID", source["pid"]),
        ):
            assert f"{field}={value}" in values
        assert not any(
            value.startswith(("_SYSTEMD_USER_UNIT=", "_SYSTEMD_INVOCATION_ID=")) for value in values
        )
        assert "--grep=^muli[.]controller_failure[.]v1 " in values
        assert (
            "--output-fields=_UID,_PID,_SYSTEMD_USER_UNIT,_SYSTEMD_INVOCATION_ID,MESSAGE" in values
        )
        assert seconds > 0
        return ci.Reply(0, reply)

    native = ci.NativeCI(root, run=run)
    native.receipt = ci.Receipt("muli-ci-" + "1" * 12)
    native.execute_deadline = time.monotonic() + 5
    native.current_account = lambda: (result["resources"]["helper_source"]["uid"], 12345)
    ci.write_json(root / "result.json", result)
    return native, calls


async def test_root_collects_actual_controller_message_once_and_preserves_first_code(
    tmp_path: Path, failure_case: tuple[dict[str, Any], bytes]
) -> None:
    result, reply = failure_case
    native, calls = observer(tmp_path / "parent", result, reply)
    native.setup_bytes = 17
    native.observe_controller_failure(result, deadline=native.execute_deadline)
    native.observe_controller_failure(result, deadline=native.execute_deadline)
    assert len(calls) == 1 and native.setup_bytes == 17 + len(reply)
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert saved["code"] == "native_transaction_blocked" and not saved["completed"]
    assert saved["timeline"] == result["timeline"]
    assert saved["resources"]["controller_failure"]["code"] == "observed"
    assert "PRIVATE" not in json.dumps(saved)
    assert len((native.root / "result.json").read_bytes()) <= ci.OUTPUT_BYTES


async def test_formatter_metadata_does_not_reject_or_escape_the_safe_message(
    tmp_path: Path, failure_case: tuple[dict[str, Any], bytes]
) -> None:
    result, original = failure_case
    source = result["resources"]["helper_source"]
    expected = ci.controller_journal_failure(original, source, result)
    assert expected["code"] == "observed"
    row = json.loads(original)
    # Formatter-owned JSON is not the host's five-field evidence schema.
    row["__EXTRA_FORMATTER_METADATA"] = {"private": ["PRIVATE_FORMATTER_VALUE", 7]}
    reply = json.dumps(row).encode() + b"\n"
    native, calls = observer(tmp_path / "parent", result, reply)
    native.setup_bytes = 17
    native.observe_controller_failure(result, deadline=native.execute_deadline)
    native.observe_controller_failure(result, deadline=native.execute_deadline)
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert len(calls) == 1 and native.setup_bytes == 17 + len(reply)
    assert saved["resources"]["controller_failure"] == expected
    assert saved["timeline"] == result["timeline"]
    assert saved["code"] == "native_transaction_blocked"
    assert "PRIVATE_FORMATTER" not in json.dumps(saved)


async def test_root_requests_all_for_valid_message_larger_than_journal_json_threshold(
    tmp_path: Path, failure_case: tuple[dict[str, Any], bytes]
) -> None:
    result, original = failure_case
    row = json.loads(original)
    payload = json.loads(row["MESSAGE"][len(PREFIX) :])
    diagnostic = payload["diagnostic"]
    # The diagnostic itself is capped at 4096 bytes, but its valid envelope and
    # prefix can cross journalctl's separate per-value JSON formatting threshold.
    for width in range(1, 124):
        diagnostic["frames"] = [
            {"module": "muli." + "a" * width, "function": "b" * width, "line": 1} for _ in range(16)
        ]
        diagnostic["truncated"] = True
        message = PREFIX + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        if len(message.encode()) > 4096:
            break
    assert len(message.encode()) > 4096
    assert ci.failure_diagnostic_module().validate_failure_diagnostic(diagnostic) is not None
    assert len(json.dumps(diagnostic, ensure_ascii=False).encode()) <= 4096
    row["MESSAGE"] = message
    full_reply = json.dumps(row).encode() + b"\n"
    expected = ci.controller_journal_failure(
        full_reply, result["resources"]["helper_source"], result
    )
    assert expected["code"] == "observed"
    native, calls = observer(tmp_path / "parent", result, full_reply)
    original_run = native.run
    wires: list[bytes] = []

    def journal(argv: Sequence[str], *, seconds: float = 5) -> Any:
        original_run(argv, seconds=seconds)  # Retain all original query/identity assertions.
        projected = dict(row)
        if "--all" not in argv:
            projected["MESSAGE"] = None  # Documented journalctl JSON default above 4096.
        wire = json.dumps(projected).encode() + b"\n"
        wires.append(wire)
        return ci.Reply(0, wire)

    native.run = journal
    native.setup_bytes = 17
    deadline = native.execute_deadline
    native.observe_controller_failure(result, deadline=deadline)
    native.observe_controller_failure(result, deadline=deadline)
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert len(calls) == len(wires) == 1
    assert "--all" in calls[0]
    assert native.setup_bytes == 17 + len(wires[0]) <= ci.OUTPUT_BYTES
    assert native.execute_deadline == deadline
    assert saved["resources"]["controller_failure"] == expected
    assert saved["code"] == "native_transaction_blocked"
    assert saved["timeline"] == result["timeline"]
    assert len((native.root / "result.json").read_bytes()) <= ci.OUTPUT_BYTES


@pytest.mark.parametrize(
    "changed", ["_UID", "_PID", "_SYSTEMD_USER_UNIT", "_SYSTEMD_INVOCATION_ID"]
)
async def test_trusted_metadata_mismatch_cannot_claim_the_real_diagnostic(
    failure_case: tuple[dict[str, Any], bytes], changed: str
) -> None:
    result, reply = failure_case
    row = json.loads(reply)
    row[changed] = "other"
    facts = ci.controller_journal_failure(
        json.dumps(row).encode(), result["resources"]["helper_source"], result
    )
    expected: dict[str, str | bool] = {
        "uid": "matches",
        "pid": "matches",
        "unit": "matches",
        "invocation": "matches",
        "shape": True,
    }
    expected[
        {
            "_UID": "uid",
            "_PID": "pid",
            "_SYSTEMD_USER_UNIT": "unit",
            "_SYSTEMD_INVOCATION_ID": "invocation",
        }[changed]
    ] = "mismatch"
    assert facts == {
        "code": "not_observed",
        "reason": "metadata_rejected",
        "metadata": [expected],
    }


@pytest.mark.parametrize("changed", ["generation", "revision", "intent", "diagnostic", "prefix"])
async def test_message_body_cannot_replace_source_binding_or_safe_schema(
    failure_case: tuple[dict[str, Any], bytes], changed: str
) -> None:
    result, reply = failure_case
    row = json.loads(reply)
    payload = json.loads(row["MESSAGE"][len(PREFIX) :])
    if changed == "prefix":
        row["MESSAGE"] = "PRIVATE_UNRELATED_MESSAGE"
    else:
        payload[changed] = {
            "generation": "f" * 32,
            "revision": 2**63,
            "intent": "unknown",
            "diagnostic": {"raw": "PRIVATE_ERROR"},
        }[changed]
        row["MESSAGE"] = PREFIX + json.dumps(payload)
    facts = ci.controller_journal_failure(
        json.dumps(row).encode(), result["resources"]["helper_source"], result
    )
    assert facts == {"code": "not_observed", "reason": "payload_rejected"}


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ("empty", "empty_reply"),
        ("missing_invocation", "metadata_rejected"),
        ("bad_diagnostic", "payload_rejected"),
    ],
)
async def test_root_classifies_unaccepted_actual_message_without_exporting_it(
    tmp_path: Path,
    failure_case: tuple[dict[str, Any], bytes],
    condition: str,
    reason: str,
) -> None:
    result, original = failure_case
    row = json.loads(original)
    if condition == "empty":
        reply = b""
    else:
        if condition == "missing_invocation":
            row.pop("_SYSTEMD_INVOCATION_ID")
        else:
            payload = json.loads(row["MESSAGE"][len(PREFIX) :])
            payload["diagnostic"] = {"raw": "PRIVATE_REJECTED_DIAGNOSTIC"}
            row["MESSAGE"] = PREFIX + json.dumps(payload)
        reply = json.dumps(row).encode() + b"\n"
    native, calls = observer(tmp_path / "parent", result, reply)
    native.setup_bytes = 17
    native.observe_controller_failure(result, deadline=native.execute_deadline)
    native.observe_controller_failure(result, deadline=native.execute_deadline)
    assert len(calls) == 1 and native.setup_bytes == 17 + len(reply)
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    expected_facts: dict[str, Any] = {
        "code": "not_observed",
        "reason": reason,
    }
    if condition == "missing_invocation":
        expected_facts["metadata"] = [
            {
                "uid": "matches",
                "pid": "matches",
                "unit": "matches",
                "invocation": "missing",
                "shape": False,
            }
        ]
    assert saved["resources"]["controller_failure"] == expected_facts
    assert saved["code"] == "native_transaction_blocked"
    assert saved["timeline"] == result["timeline"]
    assert "PRIVATE" not in json.dumps(saved)


async def test_two_rejected_rows_cannot_combine_their_trusted_identity(
    failure_case: tuple[dict[str, Any], bytes],
) -> None:
    result, original = failure_case
    first = json.loads(original)
    second = json.loads(original)
    first["_SYSTEMD_INVOCATION_ID"] = "5" * 32
    second["_UID"] = str(int(second["_UID"]) + 1)
    reply = b"\n".join(json.dumps(row).encode() for row in (first, second)) + b"\n"
    facts = ci.controller_journal_failure(reply, result["resources"]["helper_source"], result)
    assert facts == {
        "code": "not_observed",
        "reason": "metadata_rejected",
        "metadata": [
            {
                "uid": "matches",
                "pid": "matches",
                "unit": "matches",
                "invocation": "mismatch",
                "shape": True,
            },
            {
                "uid": "mismatch",
                "pid": "matches",
                "unit": "matches",
                "invocation": "matches",
                "shape": True,
            },
        ],
    }


@pytest.mark.parametrize(
    ("condition", "changed", "state", "shape"),
    [
        ("unit_missing", "unit", "missing", False),
        ("invocation_missing", "invocation", "missing", False),
        ("invocation_mismatch", "invocation", "mismatch", True),
        ("message_missing", "", "", False),
    ],
)
async def test_rejected_metadata_retains_only_fixed_per_field_states(
    tmp_path: Path,
    failure_case: tuple[dict[str, Any], bytes],
    condition: str,
    changed: str,
    state: str,
    shape: bool,
) -> None:
    result, original = failure_case
    row = json.loads(original)
    if condition == "unit_missing":
        row.pop("_SYSTEMD_USER_UNIT")
    elif condition == "invocation_missing":
        row.pop("_SYSTEMD_INVOCATION_ID")
    elif condition == "invocation_mismatch":
        row["_SYSTEMD_INVOCATION_ID"] = "5" * 32
    else:
        row.pop("MESSAGE")
    expected: dict[str, str | bool] = {
        "uid": "matches",
        "pid": "matches",
        "unit": "matches",
        "invocation": "matches",
        "shape": shape,
    }
    if changed:
        expected[changed] = state
    reply = json.dumps(row).encode() + b"\n"
    native, calls = observer(tmp_path / "parent", result, reply)
    native.setup_bytes = 17
    native.observe_controller_failure(result, deadline=native.execute_deadline)
    assert len(calls) == 1 and native.setup_bytes == 17 + len(reply)
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    facts = saved["resources"]["controller_failure"]
    assert facts == {
        "code": "not_observed",
        "reason": "metadata_rejected",
        "metadata": [expected],
    }
    assert saved["code"] == "native_transaction_blocked"
    assert saved["timeline"] == result["timeline"]
    assert "MESSAGE" not in json.dumps(facts) and "5" * 32 not in json.dumps(facts)


async def test_rejected_metadata_rows_preserve_separate_non_authorizing_states(
    tmp_path: Path, failure_case: tuple[dict[str, Any], bytes]
) -> None:
    result, original = failure_case
    first = json.loads(original)
    second = json.loads(original)
    first["_SYSTEMD_INVOCATION_ID"] = "5" * 32
    second["_UID"] = str(int(second["_UID"]) + 1)
    reply = b"\n".join(json.dumps(row).encode() for row in (first, second)) + b"\n"
    native, calls = observer(tmp_path / "parent", result, reply)
    native.observe_controller_failure(result, deadline=native.execute_deadline)
    assert len(calls) == 1 and native.setup_bytes == len(reply)
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert saved["resources"]["controller_failure"] == {
        "code": "not_observed",
        "reason": "metadata_rejected",
        "metadata": [
            {
                "uid": "matches",
                "pid": "matches",
                "unit": "matches",
                "invocation": "mismatch",
                "shape": True,
            },
            {
                "uid": "mismatch",
                "pid": "matches",
                "unit": "matches",
                "invocation": "matches",
                "shape": True,
            },
        ],
    }
    assert saved["code"] == "native_transaction_blocked"
    assert saved["timeline"] == result["timeline"]


@pytest.mark.parametrize(
    "condition", ["enum", "shape", "empty", "three", "wrong_reason", "row_extra", "extra"]
)
async def test_metadata_summary_reader_rejects_non_contract_values(
    failure_case: tuple[dict[str, Any], bytes], condition: str
) -> None:
    result, _ = failure_case
    summary: dict[str, Any] = {
        "uid": "matches",
        "pid": "matches",
        "unit": "missing",
        "invocation": "matches",
        "shape": False,
    }
    facts: dict[str, Any] = {
        "code": "not_observed",
        "reason": "metadata_rejected",
        "metadata": [summary],
    }
    if condition == "enum":
        summary["unit"] = "PRIVATE_UNIT_VALUE"
    elif condition == "shape":
        summary["shape"] = 0
    elif condition == "empty":
        facts["metadata"] = []
    elif condition == "three":
        facts["metadata"] = [summary, summary, summary]
    elif condition == "wrong_reason":
        facts["reason"] = "payload_rejected"
    elif condition == "row_extra":
        summary["raw"] = "PRIVATE_MESSAGE"
    else:
        facts["raw"] = "PRIVATE_MESSAGE"
    candidate = copy.deepcopy(result)
    candidate["resources"]["controller_failure"] = facts
    with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
        ci.fixture_result(json.dumps(candidate).encode())


async def test_old_rejected_metadata_without_summary_remains_readable(
    failure_case: tuple[dict[str, Any], bytes],
) -> None:
    result, _ = failure_case
    facts = {"code": "not_observed", "reason": "metadata_rejected"}
    candidate = copy.deepcopy(result)
    candidate["resources"]["controller_failure"] = facts
    saved = ci.fixture_result(json.dumps(candidate).encode())
    assert saved["resources"]["controller_failure"] == facts


@pytest.mark.parametrize("bad_first", [False, True])
async def test_valid_message_with_bad_json_keeps_original_observed_contract(
    failure_case: tuple[dict[str, Any], bytes], bad_first: bool
) -> None:
    result, original = failure_case
    valid = original.rstrip(b"\n")
    bad = b"{PRIVATE_INVALID_JSON"
    reply = b"\n".join((bad, valid) if bad_first else (valid, bad)) + b"\n"
    source = result["resources"]["helper_source"]
    expected = ci.controller_journal_failure(original, source, result)
    assert set(expected) == {"code", "failure"} and expected["code"] == "observed"
    assert ci.controller_journal_failure(reply, source, result) == expected


async def test_two_distinct_valid_diagnostics_still_reject_conflicting_evidence(
    failure_case: tuple[dict[str, Any], bytes],
) -> None:
    result, original = failure_case
    row = json.loads(original)
    payload = json.loads(row["MESSAGE"][len(PREFIX) :])
    previous = payload["diagnostic"]["exception_type"]
    payload["diagnostic"]["exception_type"] = (
        "TypeError" if previous != "TypeError" else "ValueError"
    )
    row["MESSAGE"] = PREFIX + json.dumps(payload)
    changed = json.dumps(row).encode()
    source = result["resources"]["helper_source"]
    assert ci.controller_journal_failure(changed, source, result)["code"] == "observed"
    reply = original.rstrip(b"\n") + b"\n" + changed + b"\n"
    assert ci.controller_journal_failure(reply, source, result) == {
        "code": "not_observed",
        "reason": "invalid_reply",
    }


@pytest.mark.parametrize(
    "condition", ["no_grant", "missing_binding", "identity_changed", "deadline"]
)
async def test_no_budget_or_identity_cannot_trigger_the_root_journal_query(
    tmp_path: Path, failure_case: tuple[dict[str, Any], bytes], condition: str
) -> None:
    result, reply = failure_case
    native, calls = observer(tmp_path / "parent", result, reply)
    if condition == "no_grant":
        native.setup_bytes = ci.OUTPUT_BYTES - 1
    elif condition == "missing_binding":
        result["resources"].pop("helper_source")
    elif condition == "identity_changed":
        native.current_account = lambda: (1, 1)
    else:
        native.execute_deadline = time.monotonic() - 1
    native.observe_controller_failure(result, deadline=native.execute_deadline)
    assert not calls and result["code"] == "native_transaction_blocked"
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert saved["resources"].get("controller_failure", {}).get("code") != "observed"


@pytest.mark.parametrize("condition", ["lost", "overflow", "nonzero"])
async def test_failed_root_read_never_refunds_unknown_output_or_exports_raw_reply(
    tmp_path: Path, failure_case: tuple[dict[str, Any], bytes], condition: str
) -> None:
    result, reply = failure_case
    native, _ = observer(tmp_path / "parent", result, reply)
    native.setup_bytes = ci.OUTPUT_BYTES - 64

    def run(argv: Sequence[str], *, seconds: float = 5) -> Any:
        if condition == "lost":
            raise OSError("PRIVATE_LOST_REPLY")
        return ci.Reply(1 if condition == "nonzero" else 0, b"PRIVATE" + b"x" * 57)

    native.run = run
    native.observe_controller_failure(result, deadline=native.execute_deadline)
    assert native.setup_bytes == ci.OUTPUT_BYTES
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert saved["resources"]["controller_failure"]["code"] == "not_observed"
    assert saved["code"] == "native_transaction_blocked" and "PRIVATE" not in json.dumps(saved)


async def test_cleanup_collection_retains_parent_diagnostic_without_another_journal_read(
    tmp_path: Path, failure_case: tuple[dict[str, Any], bytes]
) -> None:
    result, reply = failure_case
    original_worker = copy.deepcopy(result)
    native, calls = observer(tmp_path / "parent", result, reply)
    native.observe_controller_failure(result, deadline=native.execute_deadline)
    observed = result["resources"]["controller_failure"]

    def read_worker(argv: Sequence[str], *, seconds: float = 5) -> Any:
        assert tuple(argv) == (
            "sudo",
            "--non-interactive",
            "cat",
            str(native.volume / "result.json"),
        )
        return ci.Reply(0, json.dumps(original_worker).encode())

    reopened = ci.NativeCI(native.root, run=read_worker)
    reopened.receipt = native.receipt
    reopened.cleanup_deadline = time.monotonic() + 5
    collected = reopened.collect_result()
    assert collected is not None and collected["resources"]["controller_failure"] == observed
    assert len(calls) == 1


async def test_cleanup_cannot_rebind_saved_diagnostic_to_another_helper_invocation(
    tmp_path: Path, failure_case: tuple[dict[str, Any], bytes]
) -> None:
    result, reply = failure_case
    native, _ = observer(tmp_path / "parent", result, reply)
    native.observe_controller_failure(result, deadline=native.execute_deadline)
    changed = copy.deepcopy(result)
    changed["resources"].pop("controller_failure")
    changed["resources"]["helper_source"]["invocation"] = "5" * 32

    def read_worker(argv: Sequence[str], *, seconds: float = 5) -> Any:
        return ci.Reply(0, json.dumps(changed).encode())

    reopened = ci.NativeCI(native.root, run=read_worker)
    reopened.receipt = native.receipt
    reopened.cleanup_deadline = time.monotonic() + 5
    collected = reopened.collect_result()
    assert collected is not None
    assert "controller_failure" not in collected["resources"]


@pytest.mark.parametrize("changed", ["pid", "invocation", "argv", "cgroup"])
async def test_helper_identity_drift_revokes_source_without_adopting_its_return(
    tmp_path: Path,
    failure_case: tuple[dict[str, Any], bytes],
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    result, _ = failure_case
    root = tmp_path / "binding"
    root.mkdir()
    case = worker.Case(root)
    case.result = copy.deepcopy(result)
    source = result["resources"]["helper_source"]
    argv = ("fixed-controller", "-I")
    snapshot = SimpleNamespace(
        name=source["helper"],
        argv=argv,
        pid=source["pid"],
        invocation=source["invocation"],
        active_state="active",
        job=(0, "/"),
        cgroup=f"/user.slice/user-{source['uid']}.slice/user@{source['uid']}.service/helper",
    )

    class Probe:
        async def helper_exists(self, name: str) -> bool:
            return True

        async def _unit_path(self, name: str) -> str:
            return "/fixed/helper"

        async def _snapshot(self, path: str) -> Any:
            return snapshot

        async def _properties(self, *args: Any) -> dict[str, Any]:
            return {"Result": "success", "ExecMainStatus": 0}

    probe = cast(SystemdProbe, Probe())
    with monkeypatch.context() as context:
        context.setattr(os, "geteuid", lambda: source["uid"])
        assert not await case.helper_empty(probe, source["helper"], argv)
        previous = getattr(snapshot, changed)
        setattr(
            snapshot,
            changed,
            {
                "pid": source["pid"] + 1,
                "invocation": "5" * 32,
                "argv": ("foreign",),
                "cgroup": snapshot.cgroup + "/foreign",
            }[changed],
        )
        if changed == "argv":
            with pytest.raises(worker.NativeError, match="^native_helper_identity$"):
                await case.helper_empty(probe, source["helper"], argv)
        else:
            assert not await case.helper_empty(probe, source["helper"], argv)
        assert "helper_source" not in case.result["resources"]
        setattr(snapshot, changed, previous)
        assert not await case.helper_empty(probe, source["helper"], argv)
        assert "helper_source" not in case.result["resources"]


async def test_collected_helper_retains_its_previously_verified_source_without_new_snapshot(
    tmp_path: Path, failure_case: tuple[dict[str, Any], bytes]
) -> None:
    result, _ = failure_case
    root = tmp_path / "collected"
    root.mkdir()
    case = worker.Case(root)
    case.result = copy.deepcopy(result)
    source = copy.deepcopy(result["resources"]["helper_source"])

    class Collected:
        async def helper_exists(self, name: str) -> bool:
            assert name == source["helper"]
            return False

    assert await case.helper_empty(cast(SystemdProbe, Collected()), source["helper"], ())
    assert case.result["resources"]["helper_source"] == source


def test_fixed_diagnostic_validator_load_preserves_module_and_search_path() -> None:
    before = tuple(sys.path)
    module = ci.failure_diagnostic_module()
    assert module.__file__ is not None
    assert (
        Path(module.__file__).resolve()
        == Path(ci.__file__).resolve().parents[1] / "src/muli/failure_diagnostics.py"
    )
    assert ci.failure_diagnostic_module() is module and tuple(sys.path) == before
    assert module.validate_failure_diagnostic({"raw": "PRIVATE"}) is None
