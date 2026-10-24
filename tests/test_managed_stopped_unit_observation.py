"""Real controller/probe lifecycle; manager replies and proc are temporary fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from test_linux_transaction import FakeBus, write_stat
from test_managed_backend import Host
from test_managed_backend import host as host

from muli.managed_controller import ManagedTransactionController
from muli.platforms.linux_transaction import ProbeLimits
from muli.platforms.managed_probe import ManagedSystemdProbe

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux journal/proc fixture")


@pytest.mark.parametrize("host", [("adopt", False)], indirect=True)
async def test_successful_stop_then_collected_unit_advances_same_revision(
    host: Host, tmp_path: Path
) -> None:
    bus = FakeBus(host.legacy)
    process = host.probe.snapshot.process
    assert process is not None
    bus.rows.update(
        InvocationID=("ay", list(bytes.fromhex(host.probe.snapshot.unit.invocation))),
        ControlGroup=("s", process.cgroup),
    )
    proc = tmp_path / "proc"
    directory = proc / str(process.pid)
    directory.mkdir(parents=True)
    write_stat(directory / "stat", start=process.start_ticks)
    (directory / "cmdline").write_bytes(
        b"\x00".join(os.fsencode(argument) for argument in process.argv) + b"\x00"
    )
    (directory / "cgroup").write_text(f"0::{process.cgroup}\n")
    (directory / "exe").symlink_to(process.executable)
    collected = False
    missing_queries: list[list[str]] = []
    property_queries: list[list[str]] = []

    async def manager(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        if collected and "GetUnit" in argv:
            missing_queries.append(list(argv))
            return {
                "exit_code": 1,
                "output": "",
                "stderr": "Call failed: Unit muli.service not loaded.\n",
                "truncated": False,
            }
        if "get-property" in argv:
            assert argv[8] == "/org/freedesktop/systemd1/unit/muli_2eservice"
            if collected:
                property_queries.append(list(argv))
        return await bus(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)

    async def effect(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        nonlocal collected
        assert argv == [
            "systemctl",
            "--user",
            "--no-block",
            "--job-mode=fail",
            "stop",
            "muli.service",
        ]
        reply = await host.command(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)
        # A successful stop releases the actual fixture writer lease. A later
        # Properties RPC can load the regular unit description without starting it.
        bus.rows.update(
            ActiveState=("s", "inactive"),
            SubState=("s", "dead"),
            MainPID=("u", 0),
            # v255 bus_property_get_id128 encodes a missing ID as an empty ay.
            InvocationID=("ay", []),
            ControlGroup=("s", ""),
        )
        collected = True
        return reply

    host.backend.probe = ManagedSystemdProbe(
        runner=manager, limits=ProbeLimits(seconds=0.1), proc_root=proc
    )
    host.backend.runner = effect
    host.move("draining")
    original = host.move("activating", "stop_base")
    driver = ManagedTransactionController(
        host.control, host.backend, host.clock, operation_timeout=10
    )

    first = await driver.step()
    assert first == original and collected
    assert host.commands == ["stop"] and host.writer is None
    assert not missing_queries  # First observation and effects.stop really succeeded.
    second = await driver.step()
    assert (second.phase, second.intent) == ("activating", "install_target")
    assert second.generation == original.generation
    assert second.revision == original.revision + 1 and second.deadline == original.deadline
    assert second.code is None and second.failure_code is None and not second.rollback_attempted
    assert host.commands == ["stop"]
    assert (
        property_queries
    )  # The result came from real snapshot validation, not a fake stopped row.
    assert not missing_queries


@pytest.mark.parametrize("host", [("adopt", False)], indirect=True)
@pytest.mark.parametrize("fault", ["loaded_unit", "populated_cgroup", "writer_busy"])
async def test_collected_unit_properties_do_not_bypass_stopped_safety_gates(
    host: Host, fault: str
) -> None:
    host.move("draining")
    original = host.move("activating", "stop_base")
    host.stopped()
    bus = FakeBus(host.legacy)
    bus.rows.update(
        ActiveState=("s", "inactive"),
        SubState=("s", "dead"),
        MainPID=("u", 0),
        InvocationID=("ay", [0] * 16),
        ControlGroup=("s", "/user.slice/muli.service"),
    )
    if fault == "loaded_unit":
        bus.rows["ExecStart"] = (
            "a(sasbttttuii)",
            [["/foreign/python", ["/foreign/python"], False, 0, 0, 0, 0, 0, 0, 0]],
        )
    elif fault == "populated_cgroup":
        host.events.write_bytes(b"populated 1\n")
    else:
        host.writer = host.journal.writer_lease().__enter__()

    async def manager(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert "GetUnit" not in argv
        assert "get-property" in argv
        assert argv[8] == "/org/freedesktop/systemd1/unit/muli_2eservice"
        return await bus(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)

    host.backend.probe = ManagedSystemdProbe(runner=manager, limits=ProbeLimits(seconds=0.1))
    driver = ManagedTransactionController(
        host.control, host.backend, host.clock, operation_timeout=10
    )
    result = await driver.step()
    assert bus.calls and host.commands == []
    assert Path(host.anchor.unit_path).read_bytes() == host.data["base"].unit
    if fault == "loaded_unit":
        assert result.phase == "rolling_back" and result.failure_code == "backend_failed"
    else:
        assert result == original
