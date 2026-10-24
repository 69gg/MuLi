"""Integration contracts with real readers/accounting and injected native effects.

These are protocol/budget tests, not FFI authentication or READY experiments.
"""

from __future__ import annotations

import importlib.machinery
import json
import os
import runpy
import sys
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from helpers import managed_native_peer as peer
from test_managed_native_checkpoint import observed_round
from test_managed_native_ci import Commands, ci, worker
from test_managed_native_ci import fixture as fixture
from test_managed_native_ci import forbid_real_effects as forbid_real_effects
from test_managed_native_ci import manager_observation as manager_observation
from test_managed_native_instance import failed_result, instance_packet

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux native diagnostics")


def internal_packet(*, maximum: bool = False) -> dict[str, Any]:
    return {
        **asdict(
            peer.PeerFacts(
                "observed",
                "none",
                "stop-pre-sigterm" if maximum else "listening",
                2**31 - 1 if maximum else 0,
            )
        ),
        "control": {
            "code": "observed",
            "start_ticks": 2**64 - 1,
            "exe": "systemctl",
            "uids": "all_expected",
            "runtime_env": "duplicate",
            "force_bus": "duplicate",
        }
        if maximum
        else worker.control_unavailable("absent"),
    }


@pytest.fixture(autouse=True)
def forbid_peer_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("integration fixture attempted real FFI or a private socket")

    monkeypatch.setattr(peer, "_load_library", forbidden)
    monkeypatch.setattr(peer.socket, "fromfd", forbidden)


@pytest.mark.parametrize(
    "case",
    ["manager", "after-proc", "after-pid1", "executor", "uid-mismatch", "expired"],
)
async def test_peer_facts_are_inside_both_real_identity_windows(
    manager_observation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    from muli import process
    from muli.platforms import linux_transaction

    control = manager_observation
    control["states"] = ["activating"]
    if case == "uid-mismatch":
        control["process_uid"] = 0
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        worker,
        "system_executables",
        lambda: {
            "manager": "/usr/lib/systemd/systemd",
            "executor": "/usr/lib/systemd/systemd-executor",
        },
    )
    original_read = linux_transaction._read_regular
    original_run = process.run_process
    proc_bytes: list[int] = []
    image_calls: list[Path] = []
    returned_bytes: list[int] = []
    peer_calls: list[tuple[int, int, float]] = []
    inner_wires: list[bytes] = []

    def read(path: Path, limit: int, *, directory_fd: int | None = None) -> bytes:
        assert directory_fd is None
        data = original_read(path, limit)
        proc_bytes.append(len(data))
        return data

    def image(path: Path) -> str:
        assert path == Path("/proc/42/exe")
        image_calls.append(path)
        value = "/usr/lib/systemd/systemd" + ("-executor" if case == "executor" else "")
        proc_bytes.append(len(os.fsencode(value)))
        return value

    def internal(pid: int, uid: int, deadline: float) -> peer.PeerFacts:
        peer_calls.append((pid, uid, deadline))
        # Actual first proc binding and PID1 Unit/Service reads must precede
        # the peer; neither second proc nor second PID1 binding has run yet.
        assert control["read_count"] == 1 and len(image_calls) == 1
        assert [path.name for path in control["files"]] == ["stat", "status", "cgroup"]
        assert len(control["calls"]) == 4  # GetUnit, two properties, root helper.
        if case == "after-proc":
            control["change_ticks"] = True
        elif case == "after-pid1":
            control["overrides"]["InvocationID"] = [11] * 16
        elif case == "expired":
            control["clock"] = deadline
        return peer.PeerFacts("observed", "none", "listening", 0)

    async def run(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert not merge_stderr and 0 < timeout <= worker.CLEANUP_SECONDS
        if argv[:3] == ["sudo", "--non-interactive", "--"]:
            assert argv[6] == "project-manager-instance" and len(argv) == 13
            assert limit == worker.CONTEXT_REPLY_BYTES
            control["calls"].append(argv)
            data = worker.read_manager_instance(
                int(argv[7]),
                int(argv[8]),
                int(argv[9]),
                argv[10],
                float(argv[11]),
                int(argv[12]),
            )
            wire = (json.dumps(data, separators=(",", ":")) + "\n").encode()
            assert len(wire) <= worker.CONTEXT_REPLY_BYTES
            inner_wires.append(wire)
            result = {"exit_code": 0, "output": wire.decode(), "stderr": ""}
        else:
            assert "basic.target" not in argv and "ListJobs" not in argv and "--user" not in argv
            result = await original_run(
                argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr
            )
        returned_bytes.append(len(result["output"].encode()) + len(result["stderr"].encode()))
        return result

    monkeypatch.setattr(linux_transaction, "_read_regular", read)
    monkeypatch.setattr(os, "readlink", image)
    monkeypatch.setattr(peer, "read_manager_peer", internal)
    monkeypatch.setattr(process, "run_process", run)
    packet = await worker.diagnose_manager_instance(
        control["user"], control["uid"], control["home"], 105.0, 4096, observed_round()
    )
    assert ci.manager_instance_facts(packet)
    assert ci.manager_instance_matches(packet, observed_round())
    assert len((json.dumps(packet, separators=(",", ":")) + "\n").encode()) <= 1024
    if case in {"executor", "uid-mismatch"}:
        assert peer_calls == []
        assert packet["code"] == "observed" and packet["internal"] == "not_observed"
        assert packet["exe"] == ("executor" if case == "executor" else "manager")
        assert packet["uids"] == ("mismatch" if case == "uid-mismatch" else "all_expected")
    else:
        assert peer_calls == [(42, control["uid"], 105.0)]
        if case == "manager":
            assert packet["code"] == "observed"
            assert packet["internal"] == internal_packet()
            assert len(image_calls) == 2 and control["read_count"] == 2
            assert len(control["calls"]) == 6
            assert packet["used"] == sum(returned_bytes) + sum(proc_bytes)
        else:
            assert packet["code"] == "not_observed"
            assert packet["reason"] == ("timeout" if case == "expired" else "identity_changed")
            assert packet["exe"] == packet["uids"] == "unavailable"
            assert packet["internal"] == "not_observed"
            assert "control_pid" not in json.dumps(packet)
    assert "secret" not in json.dumps(packet) and inner_wires


def test_proc_budget_exhaustion_never_opens_peer(
    manager_observation: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    def internal(pid: int, uid: int, deadline: float) -> peer.PeerFacts:
        calls.append(True)
        raise AssertionError("no proc budget remains for a peer window")

    monkeypatch.setattr(peer, "read_manager_peer", internal)
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    result = worker.read_manager_instance(42, 23145, 100, manager_observation["group"], 105.0, 1)
    assert result["code"] == "not_observed" and calls == []
    assert result["used"] == 1


def test_fixed_sibling_loader_reuses_live_script_worker_without_search_path_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Execute definitions only: no CLI main, new process, FFI or private socket.
    namespace = runpy.run_path(
        str(Path(worker.__file__).resolve()), run_name="_native_worker_fixture"
    )
    main = ModuleType("__main__")
    main.__dict__.update(namespace)
    monkeypatch.setitem(sys.modules, "__main__", main)
    name = "_muli_managed_native_peer"
    monkeypatch.setitem(sys.modules, name, None)
    original_exec = importlib.machinery.SourceFileLoader.exec_module
    paths = tuple(sys.path)
    loaded: list[ModuleType] = []

    def load(loader: importlib.machinery.SourceFileLoader, module: ModuleType) -> None:
        assert loader.path == str(Path(worker.__file__).with_name("managed_native_peer.py"))
        assert sys.modules[name] is module  # dataclasses require the live module.
        original_exec(loader, module)
        assert module.NativeError is namespace["NativeError"]
        assert module.remaining is namespace["remaining"]
        assert module.require is namespace["require"]
        loaded.append(module)

        def read(pid: int, uid: int, deadline: float) -> Any:
            assert (pid, uid, deadline) == (42, 23145, 105.0)
            return module.PeerFacts("not_observed", "unavailable")

        monkeypatch.setattr(module, "read_manager_peer", read)

    monkeypatch.setattr(importlib.machinery.SourceFileLoader, "exec_module", load)
    for _ in range(2):
        result = namespace["manager_peer"](42, 23145, 105.0)
        assert result.code == "not_observed" and result.reason == "unavailable"
    assert len(loaded) == 1 and tuple(sys.path) == paths
    assert sys.modules.get("helpers.managed_native_worker") is worker


def test_maximum_internal_projection_fits_unchanged_inner_reserve() -> None:
    value = {
        "code": "observed",
        "reason": "none",
        "exe": "manager",
        "uids": "all_expected",
        "used": worker.MAX_BYTES,
        "internal": internal_packet(maximum=True),
    }
    assert len((json.dumps(value, separators=(",", ":")) + "\n").encode()) <= 512
    assert worker.instance_projection(value) == value


@pytest.mark.parametrize("case", ["bool-pid", "extra", "executor", "uid-mismatch"])
def test_internal_facts_cannot_bypass_exact_shape_or_process_tags(case: str) -> None:
    value = {
        "code": "observed",
        "reason": "none",
        "exe": "manager",
        "uids": "all_expected",
        "used": 128,
        "internal": internal_packet(),
    }
    internal = value["internal"]
    assert isinstance(internal, dict)
    if case == "bool-pid":
        internal["control_pid"] = True
    elif case == "extra":
        internal["unbounded_native_error"] = "must-not-escape"
    elif case == "executor":
        value["exe"] = "executor"
    else:
        value["uids"] = "mismatch"
    with pytest.raises(worker.NativeError, match="^native_diagnostic_invalid$"):
        worker.instance_projection(value)


@pytest.mark.parametrize("case", ["validated", "1024", "1025", "lost", "no-grant"])
def test_parent_accounting_uses_real_ledger_and_wire_without_enlarging_packet(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    native, commands = fixture
    native.prepare()
    native.receipt.setup_phase = "manager_show_before"
    packet = instance_packet(commands.uid)
    packet["internal"] = internal_packet()
    if case in {"1024", "1025"}:
        packet.update(pid=2**31 - 1, start_ticks=2**64 - 1, invocation="f" * 32)
        packet["internal"] = internal_packet(maximum=True)
    native.manager_failure = failed_result(packet)["resources"]["manager_failure"]
    native.manager_failure["last_completed_round"]["manager"].update(
        {key: packet[key] for key in ("pid", "start_ticks", "invocation")}
    )
    original_failure = deepcopy(native.manager_failure)
    native.save_setup_result({})
    if case == "no-grant":
        native.setup_bytes = ci.OUTPUT_BYTES - 1024
    before = native.setup_bytes
    grant = ci.OUTPUT_BYTES - before - 1024
    actual_wire = b""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(ci.time, "monotonic", lambda: 100.0)

    def run(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal actual_wire
        arguments = tuple(argv)
        calls.append(arguments)
        assert arguments[3] == "diagnose-manager-instance"
        assert int(arguments[8]) == grant and 0 < seconds <= 5
        if case == "lost":
            raise TimeoutError("private loss must not become evidence")
        packet["used"] = grant if case in {"1024", "1025"} else 128
        actual_wire = (json.dumps(packet, separators=(",", ":")) + "\n").encode()
        assert len(actual_wire) < 1024  # accounting never rides in the worker packet.
        if case in {"1024", "1025"}:
            actual_wire = actual_wire.ljust(int(case), b" ")
        return ci.Reply(0, actual_wire)

    native.run = run
    native.observe_setup(ci.FixtureError("native_deadline_exceeded"))
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    facts = saved["resources"]["setup"]
    accounting = saved["resources"].get("setup_accounting")
    assert isinstance(accounting, dict), "parent charged/raw grant must survive in its own receipt"
    expected_charged = (
        0 if case == "no-grant" else grant if case in {"lost", "1025"} else packet["used"]
    )
    expected_wire: int | None = None if case == "lost" else len(actual_wire)
    expected_after = (
        ci.OUTPUT_BYTES
        if case in {"lost", "1025"}
        else before + expected_charged + (expected_wire or 0)
    )
    assert accounting == {
        "parent_before": before,
        "raw_grant": grant,
        "child_charged": expected_charged,
        "reply_bytes": expected_wire,
        "parent_after": expected_after,
        "settlement": "not_started"
        if case == "no-grant"
        else "unvalidated"
        if case in {"lost", "1025"}
        else "validated",
    }
    assert native.setup_bytes == expected_after <= ci.OUTPUT_BYTES
    assert (
        len(
            json.dumps(
                {"setup": facts, "setup_accounting": accounting},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        <= 1024
    )
    assert saved["resources"]["manager_failure"] == original_failure
    assert saved["timeline"] == saved["generations"] == []
    if case == "no-grant":
        assert not calls and facts["code"] == "not_observed" and facts["reason"] == "output_budget"
    elif case in {"lost", "1025"}:
        assert len(calls) == 1 and facts["code"] == "not_observed"
        assert facts["reason"] == ("timeout" if case == "lost" else "output_budget")
        assert facts["internal"] == "not_observed"
    else:
        assert len(calls) == 1 and facts == packet
        assert before + packet["used"] + len(actual_wire) <= ci.OUTPUT_BYTES
    assert "private loss" not in json.dumps(saved)


@pytest.mark.parametrize(
    "case",
    ["extra", "bool", "grant", "after", "missing-wire", "refund", "setup-used", "settlement"],
)
def test_saved_parent_accounting_rejects_forged_settlement(case: str) -> None:
    packet = instance_packet()
    value = failed_result(packet)
    accounting: dict[str, Any] = {
        "parent_before": 128,
        "raw_grant": ci.OUTPUT_BYTES - 128 - 1024,
        "child_charged": 128,
        "reply_bytes": 500,
        "parent_after": 756,
        "settlement": "validated",
    }
    value["resources"]["setup_accounting"] = accounting
    assert ci.fixture_result(json.dumps(value).encode()) == value
    if case == "extra":
        accounting["native_error"] = "must-not-escape"
    elif case == "bool":
        accounting["child_charged"] = True
    elif case == "grant":
        accounting["raw_grant"] -= 1
    elif case == "after":
        accounting["parent_after"] -= 1
    elif case == "missing-wire":
        accounting["reply_bytes"] = None
    elif case == "refund":
        accounting.update(settlement="unvalidated", reply_bytes=None, parent_after=ci.OUTPUT_BYTES)
    elif case == "setup-used":
        packet["used"] += 1
    else:
        accounting["settlement"] = "unknown-native-text"
    with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
        ci.fixture_result(json.dumps(value).encode())
