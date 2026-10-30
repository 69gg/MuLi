"""Small native failure receipts; all commands, proc data and accounts are injected."""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from helpers import managed_native_peer as peer
from test_managed_native_checkpoint import observed_round
from test_managed_native_ci import Commands, ci, worker
from test_managed_native_ci import context_reader as context_reader
from test_managed_native_ci import fixture as fixture
from test_managed_native_ci import forbid_real_effects as forbid_real_effects
from test_managed_native_ci import manager_observation as manager_observation

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only CI coordinator")


def instance_packet(uid: int = 23145) -> dict[str, Any]:
    return {
        "version": 1,
        "code": "observed",
        "reason": "none",
        "uid": uid,
        "pid": 42,
        "start_ticks": 100,
        "invocation": "0a" * 16,
        "exe": "manager",
        "uids": "all_expected",
        "internal": "not_observed",
        "pending_job": {"code": "empty", "identifier": "", "type": ""},
        "used": 128,
    }


@pytest.mark.parametrize(
    ("prefix", "change"),
    [
        ("", "none"),
        ("(2 of 5) ", "none"),
        ("", "invocation"),
        ("", "pid"),
        ("", "group"),
        ("", "deadline"),
    ],
)
async def test_pending_user_job_survives_compact_instance_without_another_query(
    manager_observation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    change: str,
) -> None:
    """v255's PID1 STATUS identifies a running job, not a READY notification."""
    from muli import process

    control = manager_observation
    control["states"] = ["activating"]
    status = prefix + "User job dbus.socket/start running (1min 21s / no limit)..."
    # Keep the legacy large diagnostic's fixed-label contract. The compact path
    # must recover only bounded identifier/type hints, never a verified unit ID.
    legacy = worker.notification_projection(
        {
            "StatusText": status,
            "NotifyAccess": "main",
            "Type": "notify-reload",
            "Result": "success",
        },
        {"ActiveState": "activating", "SubState": "start"},
    )
    assert legacy["status_text"] == "other" and ci.notification_facts(legacy)
    original = process.run_process
    returned: list[int] = []
    calls: list[list[str]] = []
    inner_used = 128

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert 0 < timeout <= 5 and not merge_stderr
        assert "ListJobs" not in argv and "LoadUnit" not in argv
        calls.append(argv)
        result: dict[str, Any]
        if argv[:3] == ["sudo", "--non-interactive", "--"]:
            assert argv[6] == "project-manager-instance"
            result = {
                "exit_code": 0,
                "output": json.dumps(
                    {
                        "code": "observed",
                        "reason": "none",
                        "exe": "manager",
                        "uids": "all_expected",
                        "used": inner_used,
                    },
                    separators=(",", ":"),
                )
                + "\n",
                "stderr": "",
            }
            if change == "invocation":
                control["overrides"]["InvocationID"] = [11] * 16
            elif change == "pid":
                control["overrides"]["MainPID"] = 43
            elif change == "group":
                control["overrides"]["ControlGroup"] = "/other/user@23145.service"
            elif change == "deadline":
                control["clock"] = 105.01
        elif "get-property" in argv:
            index = argv.index("get-property")
            fields = argv[index + 4 :]
            if "StatusText" in fields:
                # Use the original typed probe and original accounting for the
                # identity fields; inject only the actual StatusText wire value.
                identity_argv = argv[: index + 4] + [
                    field for field in fields if field != "StatusText"
                ]
                result = await original(
                    identity_argv, timeout=timeout, limit=limit, merge_stderr=False
                )
                rows = iter(result["output"].splitlines())
                result["output"] = "\n".join(
                    json.dumps({"type": "s", "data": status})
                    if field == "StatusText"
                    else next(rows)
                    for field in fields
                )
            else:
                result = await original(argv, timeout=timeout, limit=limit, merge_stderr=False)
        else:
            result = await original(argv, timeout=timeout, limit=limit, merge_stderr=False)
        returned.append(len(result["output"].encode()) + len(result["stderr"].encode()))
        return result

    monkeypatch.setattr(process, "run_process", query)
    packet = await worker.diagnose_manager_instance(
        control["user"], control["uid"], control["home"], 105.0, 4096, observed_round()
    )
    assert any("StatusText" in argv for argv in calls), (
        "compact path discarded the existing PID1 job source"
    )
    if change != "none":
        assert packet["code"] == "not_observed"
        assert packet["reason"] == ("timeout" if change == "deadline" else "identity_changed")
        assert packet["pending_job"] == {"code": "unavailable", "identifier": "", "type": ""}
        assert packet["internal"] == "not_observed"
        assert ci.manager_instance_facts(packet, allow_legacy=False)
        assert len(calls) <= 6 and packet["used"] <= 4096
        return
    assert packet["code"] == "observed", packet
    assert packet["pending_job"] == {
        "code": "user_job",
        "identifier": "dbus.socket",
        "type": "start",
    }
    assert ci.manager_instance_facts(packet) and ci.manager_instance_matches(
        packet, observed_round()
    )
    assert packet["used"] == inner_used + sum(returned)
    assert len(calls) == 6, "reuse five PID1 calls and one inner command; no additional round"
    assert "1min 21s" not in json.dumps(packet) and "no limit" not in json.dumps(packet)
    assert len(json.dumps(packet, separators=(",", ":")).encode()) <= 1024


@pytest.mark.parametrize(
    "status",
    [
        "User job private.service/start running ...",
        "User job private.service/start running (1s / no limit)...\nSECRET=private",
        "User job /private.service/start running (1s / no limit)...",
        "User job private.service/unknown running (1s / no limit)...",
        "User job private/start running (1s / no limit)...",
        "User job priváte.service/start running (1s / no limit)...",
        "User job private\\name.service/start running (1s / no limit)...",
        "User job " + "x" * 129 + ".service/start running (1s / no limit)...",
        "(0 of 5) User job private.service/start running (1s / no limit)...",
        "(1 of 1) User job private.service/start running (1s / no limit)...",
        "(6 of 5) User job private.service/start running (1s / no limit)...",
        "(2 of 4294967296) User job private.service/start running (1s / no limit)...",
        "(02 of 5) User job private.service/start running (1s / no limit)...",
        "User job private.service/start running (1s / no limit)... extra",
        "User job private.service/start running (1\x00s / no limit)...",
        "x" * 513,
        None,
        True,
    ],
)
def test_pending_job_parser_does_not_export_unrecognized_status(status: object) -> None:
    result = worker.pending_job_projection(status)
    assert result == {"code": "other", "identifier": "", "type": ""}
    assert ci.pending_job_facts(result)


@pytest.mark.parametrize(
    ("status", "code"),
    [("", "empty"), ("Ready.", "ready"), ("Reached basic.target.", "reached_basic")],
)
def test_pending_job_fixed_labels_do_not_invent_a_job(status: str, code: str) -> None:
    value = worker.pending_job_projection(status)
    assert value == {"code": code, "identifier": "", "type": ""}
    assert ci.pending_job_facts(value)


def test_pending_job_new_collector_is_strict_but_old_artifacts_remain_readable() -> None:
    packet = instance_packet()
    assert ci.manager_instance_facts(packet, allow_legacy=False)
    legacy = deepcopy(packet)
    legacy.pop("pending_job")
    assert ci.manager_instance_facts(legacy)
    assert not ci.manager_instance_facts(legacy, allow_legacy=False)
    old = failed_result(legacy)
    assert ci.fixture_result(json.dumps(old).encode()) == old
    for change in (
        {"code": "user_job", "identifier": "dbus.socket", "type": "unknown"},
        {"code": "user_job", "identifier": "../dbus.socket", "type": "start"},
        {"code": "other", "identifier": "secret.service", "type": ""},
        {"code": "unavailable", "identifier": "", "type": ""},
        {"code": "empty", "identifier": "", "type": "", "raw": "SECRET"},
        {"code": True, "identifier": "", "type": ""},
    ):
        corrupted = deepcopy(packet)
        corrupted["pending_job"] = change
        assert not ci.manager_instance_facts(corrupted)
    packet["pending_job"] = {
        "code": "user_job",
        "identifier": "x" * (128 - len(".service")) + ".service",
        "type": "reload-or-start",
    }
    assert ci.manager_instance_facts(packet, allow_legacy=False)
    assert worker.pending_job_facts(packet["pending_job"])
    assert len(json.dumps(packet, separators=(",", ":")).encode()) < 1024
    # Exercise the old artifact readback contract, not the current collector.
    # Current socket-control maxima are covered by socket_control/peer_integration.
    packet.update(
        uid=2**31 - 1,
        pid=2**31 - 1,
        start_ticks=2**64 - 1,
        invocation="f" * 32,
        used=ci.OUTPUT_BYTES - 1024,
        internal={
            "code": "observed",
            "reason": "none",
            "basic_state": "deactivating",
            "njobs": 2**32 - 1,
        },
    )
    maximum = failed_result(packet)
    maximum["resources"]["manager_failure"]["last_completed_round"]["manager"].update(
        {key: packet[key] for key in ("pid", "start_ticks", "invocation")}
    )
    maximum["resources"]["setup_accounting"] = {
        "parent_before": 0,
        "raw_grant": ci.OUTPUT_BYTES - 1024,
        "child_charged": ci.OUTPUT_BYTES - 1024,
        "reply_bytes": 1024,
        "parent_after": ci.OUTPUT_BYTES,
        "settlement": "validated",
    }
    projection = {key: maximum["resources"][key] for key in ("setup", "setup_accounting")}
    assert len(json.dumps(projection, separators=(",", ":")).encode()) <= 1024
    assert ci.fixture_result(json.dumps(maximum).encode()) == maximum


@pytest.mark.parametrize(
    ("identifier", "job_type"),
    [("name\\x2dpart.socket", "verify-active"), ("unit@instance.service", "reload-or-start")],
)
def test_pending_job_parser_preserves_only_safe_identifier_and_job_tokens(
    identifier: str, job_type: str
) -> None:
    text = f"User job {identifier}/{job_type} running (0 / no limit)..."
    result = worker.pending_job_projection(text)
    assert result == {"code": "user_job", "identifier": identifier, "type": job_type}
    assert ci.pending_job_facts(result)


def test_pending_job_description_with_unit_shape_is_only_an_identifier_hint() -> None:
    # v255 unit_status_string(unit, NULL) can return Description instead of Id.
    actual_unit_id, description = "actual.socket", "dbus.socket"
    status = f"User job {description}/start running (1min 21s / no limit)..."
    result = worker.pending_job_projection(status)
    assert result == {"code": "user_job", "identifier": description, "type": "start"}
    assert result["identifier"] != actual_unit_id
    assert ci.pending_job_facts(result)
    assert not {"unit", "unit_id", "verified_unit"}.intersection(result)
    assert not ci.pending_job_facts({"code": "user_job", "unit": description, "type": "start"})


def failed_result(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "completed": False,
        "stage": "manager_setup",
        "code": "setup_failed",
        "timeline": [],
        "generations": [],
        "resources": {
            "setup": packet,
            "manager_failure": {
                "code": "native_deadline_exceeded",
                "stage": "manager_validation",
                "query_status": 0,
                "query_code": "ok",
                "used": 1,
                "last_completed_round": observed_round(),
            },
        },
    }


def test_failed_bound_manager_replaces_large_fallback_and_charges_actual_packet_once(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    native.receipt.setup_phase = "manager_show_before"
    packet = instance_packet(commands.uid)
    native.manager_failure = failed_result(packet)["resources"]["manager_failure"]
    native.save_setup_result({})
    before = native.setup_bytes
    calls: list[tuple[str, ...]] = []
    wire = b""

    def run(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal wire
        values = tuple(argv)
        calls.append(values)
        assert values[:4] == (
            sys.executable,
            "-I",
            str(Path(worker.__file__).resolve()),
            "diagnose-manager-instance",
        ), "old large fallback must be replaced, not followed by another command"
        assert len(values) == 10 and 0 < seconds <= ci.CLEANUP_SECONDS
        assert json.loads(values[9]) == observed_round()
        packet["used"] = int(values[8])
        assert packet["used"] == ci.OUTPUT_BYTES - before - 1024
        wire = (json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n").encode()
        assert len(wire) <= 1024
        return ci.Reply(0, wire)

    native.run = run
    native.observe_setup(ci.FixtureError("native_deadline_exceeded"))
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert saved["resources"]["setup"] == packet and len(calls) == 1
    assert saved["resources"]["manager_failure"] == native.manager_failure
    assert saved["timeline"] == [] and saved["generations"] == []
    assert native.setup_bytes == before + packet["used"] + len(wire) <= ci.OUTPUT_BYTES


def test_compact_setup_requires_exact_same_instance_and_never_invents_internal_state() -> None:
    packet = instance_packet()
    value = failed_result(packet)
    assert ci.fixture_result(json.dumps(value).encode()) == value
    largest = {
        **packet,
        "code": "not_observed",
        "reason": "identity_changed",
        "uid": 2**31 - 1,
        "pid": 2**31 - 1,
        "start_ticks": 2**64 - 1,
        "invocation": "f" * 32,
        "exe": "unavailable",
        "uids": "unavailable",
        "used": ci.OUTPUT_BYTES - 1024,
    }
    # Preserve the original <=300B legacy artifact contract independently of
    # the new collector's mandatory bounded status projection.
    largest.pop("pending_job")
    assert len((json.dumps(largest, separators=(",", ":")) + "\n").encode()) <= 300
    for field, bad in (
        ("invocation", "ff" * 16),
        ("pid", 43),
        ("start_ticks", 101),
        ("version", True),
        ("internal", "ready"),
        ("extra", "must-not-escape"),
    ):
        corrupted = deepcopy(value)
        corrupted["resources"]["setup"][field] = bad
        with pytest.raises(ci.FixtureError, match="native_result_invalid"):
            ci.fixture_result(json.dumps(corrupted).encode())


@pytest.mark.parametrize(
    "case", ["executor", "one-uid-mismatch", "ticks-drift", "image-drift", "uid-drift"]
)
def test_instance_helper_reads_actual_image_and_all_four_uids_without_environment(
    context_reader: dict[str, Any], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from muli.platforms import linux_transaction

    read_instance = getattr(worker, "read_manager_instance", None)
    assert callable(read_instance), "missing bounded same-instance proc projection"
    control = context_reader
    control["change"] = "ticks" if case == "ticks-drift" else "none"
    original = linux_transaction._read_regular
    charged: list[int] = []
    images: list[Path] = []
    known = {"manager": "/usr/lib/systemd/systemd", "executor": "/usr/lib/systemd/systemd-executor"}
    monkeypatch.setattr(worker, "system_executables", lambda: known)
    # This legacy proc-only contract must not attempt the new native connection.
    monkeypatch.setattr(
        peer,
        "read_manager_peer",
        lambda pid, uid, deadline: peer.PeerFacts("not_observed", "unavailable"),
    )

    def read(path: Path, limit: int, *, directory_fd: int | None = None) -> bytes:
        assert path.name in {"stat", "status", "cgroup"}, "no environ/wchan or recovery read"
        data = original(path, limit, directory_fd=directory_fd)
        if path.name == "status" and (
            case == "one-uid-mismatch" or case == "uid-drift" and control["round"] > 1
        ):
            data = b"Uid:\t23145 23145 23145 0\n"
        assert len(data) <= limit
        charged.append(len(data))
        return data

    def image(path: Path) -> str:
        assert path == Path("/proc/42/exe")
        images.append(path)
        result = known[
            "executor"
            if case == "executor" or case == "image-drift" and len(images) > 1
            else "manager"
        ]
        charged.append(len(os.fsencode(result)))
        return result

    monkeypatch.setattr(linux_transaction, "_read_regular", read)
    monkeypatch.setattr(os, "readlink", image)
    limit = 4096
    result = read_instance(42, 23145, 100, "/user.slice/user@23145.service", 105.0, limit)
    assert set(result) == {"code", "reason", "exe", "uids", "used"} | (
        set() if result["code"] == "not_observed" else {"internal"}
    )
    if case in {"ticks-drift", "image-drift", "uid-drift"}:
        assert result == {
            "code": "not_observed",
            "reason": "identity_changed",
            "exe": "unavailable",
            "uids": "unavailable",
            "used": limit,
        }
    else:
        assert result["code"] == "observed" and result["reason"] == "none"
        assert result["exe"] == ("executor" if case == "executor" else "manager")
        assert result["uids"] == ("mismatch" if case == "one-uid-mismatch" else "all_expected")
        assert result["internal"] == (
            {
                "code": "not_observed",
                "reason": "unavailable",
                "sub_state": None,
                "control_pid": None,
                "control": worker.control_unavailable(),
            }
            if case not in {"executor", "one-uid-mismatch"}
            else "not_observed"
        )
        assert result["used"] == sum(charged) and len(images) == 2
        assert [name for name, _ in control["reads"]] == ["stat", "status", "cgroup"] * 2
    assert len((json.dumps(result, separators=(",", ":")) + "\n").encode()) <= 512
    assert not any(
        value in json.dumps(result) for value in ("never-export-me", "/usr/lib", "/proc")
    )


@pytest.mark.parametrize(
    "case", ["1024", "1025", "parent-timeout", "lost-reply", "bad-used", "wrong-instance", "legacy"]
)
def test_compact_parent_accepts_only_bounded_timely_same_instance_reply(
    fixture: tuple[Any, Commands],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    native, commands = fixture
    native.prepare()
    native.receipt.setup_phase = "manager_show_before"
    packet = instance_packet(commands.uid)
    native.manager_failure = failed_result(packet)["resources"]["manager_failure"]
    native.save_setup_result({})
    first = deepcopy(native.manager_failure)
    clock = [100.0]
    monkeypatch.setattr(ci.time, "monotonic", lambda: clock[0])
    native.execute_deadline = 105.0
    before = native.setup_bytes
    calls: list[tuple[str, ...]] = []
    wire = b""

    def run(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal wire
        values = tuple(argv)
        assert values[3] == "diagnose-manager-instance" and seconds <= 5
        calls.append(values)
        if case == "lost-reply":
            raise ci.FixtureError("native_command_timeout")
        packet["used"] = int(values[8]) + 1 if case == "bad-used" else 1
        if case == "wrong-instance":
            packet["invocation"] = "ff" * 16
        elif case == "legacy":
            packet.pop("pending_job")
        wire = (json.dumps(packet, separators=(",", ":")) + "\n").encode()
        if case in {"1024", "1025"}:
            wire = wire.ljust(int(case), b" ")
        if case == "parent-timeout":
            clock[0] = 105.0
        return ci.Reply(0, wire)

    native.run = run
    native.observe_setup(ci.FixtureError("native_deadline_exceeded"))
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    facts = saved["resources"]["setup"]
    assert len(calls) == 1 and saved["resources"]["manager_failure"] == first
    assert saved["timeline"] == [] and saved["generations"] == []
    if case == "1024":
        assert facts == packet
        assert native.setup_bytes == before + 1 + 1024
    else:
        assert facts["code"] == "not_observed" and facts["exe"] == facts["uids"] == "unavailable"
        assert facts["reason"] == (
            "timeout"
            if case in {"parent-timeout", "lost-reply"}
            else "output_budget"
            if case == "1025"
            else "invalid_reply"
        )
        assert facts["used"] == ci.OUTPUT_BYTES - before - 1024
        assert native.setup_bytes == (
            ci.OUTPUT_BYTES
            if case in {"1025", "parent-timeout", "lost-reply"}
            else ci.OUTPUT_BYTES - 1024 + len(wire)
        )
    assert native.setup_bytes <= ci.OUTPUT_BYTES


@pytest.mark.parametrize(
    "case",
    [
        "manager",
        "after-invocation",
        "after-pid",
        "after-group",
        "lost-inner",
        "inner-output-budget",
        "pid1-ready",
    ],
)
async def test_compact_worker_binds_both_bus_sides_without_pid1_internal_inference(
    manager_observation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    from muli import process

    control = manager_observation
    control["states"] = ["activating"]
    original = process.run_process
    returned_bytes: list[int] = []
    raw_grants: list[int] = []
    inner_used = 128

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        if argv[:3] == ["sudo", "--non-interactive", "--"]:
            assert argv[6] == "project-manager-instance" and len(argv) == 13
            assert argv[7:11] == ["42", str(control["uid"]), "100", control["group"]]
            assert float(argv[11]) == 105.0 and limit == worker.CONTEXT_REPLY_BYTES
            control["calls"].append(argv)
            raw_grants.append(int(argv[12]))
            if case == "lost-inner":
                raise TimeoutError
            if case == "inner-output-budget":
                raise process.ProcessOutputLimitError("private diagnostic text must not escape")
            data = {
                "code": "observed",
                "reason": "none",
                "exe": "manager",
                "uids": "all_expected",
                "used": inner_used,
            }
            result = {
                "exit_code": 0,
                "output": json.dumps(data, separators=(",", ":")) + "\n",
                "stderr": "",
            }
            if case == "after-invocation":
                control["overrides"]["InvocationID"] = [11] * 16
            elif case == "after-pid":
                control["overrides"]["MainPID"] = 43
            elif case == "after-group":
                control["overrides"]["ControlGroup"] = "/other/user@23145.service"
        else:
            # PID1 is the only bus peer used by _SetupIO; its internal basic/jobs
            # look ready in this trap, but cannot become user-manager evidence.
            if case == "pid1-ready" and ("basic.target" in argv or "ListJobs" in argv):
                assert argv[:2] == ["busctl", "--system"]
                control["calls"].append(argv)
                result = {
                    "exit_code": 0,
                    "stderr": "",
                    "output": json.dumps(
                        {"type": "a(usssoo)", "data": [[]]}
                        if "ListJobs" in argv
                        else {"type": "s", "data": "active"}
                    ),
                }
            else:
                assert "basic.target" not in argv and "ListJobs" not in argv
                assert "ListUnitsByPatterns" not in argv and "--user" not in argv
                result = await original(
                    argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr
                )
        returned_bytes.append(len(result["output"].encode()) + len(result["stderr"].encode()))
        return result

    monkeypatch.setattr(process, "run_process", query)
    packet = await worker.diagnose_manager_instance(
        control["user"],
        control["uid"],
        control["home"],
        105.0,
        4096,
        observed_round(),
    )
    assert ci.manager_instance_facts(packet) and packet["internal"] == "not_observed"
    assert ci.manager_instance_matches(packet, observed_round())
    if case in {"manager", "pid1-ready"}:
        assert packet["code"] == "observed" and packet["exe"] == "manager"
        assert packet["used"] == sum(returned_bytes) + inner_used
        assert len(control["calls"]) == 6
    else:
        assert packet["code"] == "not_observed" and packet["exe"] == packet["uids"] == "unavailable"
        assert packet["reason"] == (
            "timeout"
            if case == "lost-inner"
            else "output_budget"
            if case == "inner-output-budget"
            else "identity_changed"
        )
        if case in {"lost-inner", "inner-output-budget"}:
            assert packet["used"] == 4096 and len(control["calls"]) == 4
        else:
            assert packet["used"] == sum(returned_bytes) + inner_used
            assert len(control["calls"]) == 6
    assert len(raw_grants) == 1 and 0 < raw_grants[0] < 4096
    assert all("basic.target" not in argv and "ListJobs" not in argv for argv in control["calls"])
    assert control["files"] == []
    assert len((json.dumps(packet, separators=(",", ":")) + "\n").encode()) <= 1024
    assert "private diagnostic text" not in json.dumps(packet)


@pytest.mark.parametrize("case", ["inner-budget", "wire-oversize", "duplicate-key", "bad-uid-tag"])
async def test_inner_reply_never_refunds_an_unvalidated_raw_grant(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    from muli import process

    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.0)
    io = worker._SetupIO(105.0, 4096, {})
    raw_grant = 4096 - worker.CONTEXT_REPLY_BYTES
    wire = ""

    async def run(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        nonlocal wire
        assert argv[6] == "project-manager-instance" and int(argv[-1]) == raw_grant
        assert limit == worker.CONTEXT_REPLY_BYTES and not merge_stderr
        data = {
            "code": "not_observed",
            "reason": "output_budget",
            "exe": "unavailable",
            "uids": "unavailable",
            "used": raw_grant,
        }
        if case == "bad-uid-tag":
            data["uids"] = "23145 23145 23145 0"
        wire = json.dumps(data, separators=(",", ":")) + "\n"
        if case == "wire-oversize":
            wire = wire.ljust(worker.CONTEXT_REPLY_BYTES + 1)
        elif case == "duplicate-key":
            wire = wire.replace('"used":', '"used":0,"used":')
        return {"exit_code": 0, "output": wire, "stderr": ""}

    monkeypatch.setattr(process, "run_process", run)
    if case == "inner-budget":
        result = await io.instance(42, 23145, 100, "/user.slice/user@23145.service")
        assert result["code"] == "not_observed" and result["reason"] == "output_budget"
    else:
        with pytest.raises(worker.NativeError):
            await io.instance(42, 23145, 100, "/user.slice/user@23145.service")
    if case == "wire-oversize":
        assert io.used == 4096
    else:
        assert io.used == raw_grant + len(wire.encode())


@pytest.mark.parametrize("case", ["absent", "not-observed"])
def test_no_bound_checkpoint_keeps_existing_large_diagnostic_contract(
    fixture: tuple[Any, Commands],
    case: str,
) -> None:
    native, commands = fixture
    native.prepare()
    native.receipt.setup_phase = "manager_show_before"
    native.manager_failure = failed_result(instance_packet(commands.uid))["resources"][
        "manager_failure"
    ]
    if case == "absent":
        del native.manager_failure["last_completed_round"]
    else:
        native.manager_failure["last_completed_round"] = {"version": 1, "code": "not_observed"}
    before = native.setup_bytes
    calls: list[tuple[str, ...]] = []

    def run(argv: Any, *, seconds: float = ci.CLEANUP_SECONDS) -> Any:
        values = tuple(argv)
        calls.append(values)
        assert values[3] == "diagnose-setup" and 0 < seconds <= ci.CLEANUP_SECONDS
        return ci.Reply(1, b"")

    native.run = run
    native.observe_setup(ci.FixtureError("native_deadline_exceeded"))
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    facts = saved["resources"]["setup"]
    assert len(calls) == 1 and facts["uid"] == commands.uid
    assert facts["diagnostic_status"] == 1 and facts["diagnostic_code"] == "command_failed"
    assert not ci.manager_instance_facts(facts) and native.setup_bytes == before
    assert saved["resources"]["manager_failure"] == native.manager_failure
