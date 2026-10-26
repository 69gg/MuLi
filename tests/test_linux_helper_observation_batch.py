"""Batch manager properties without losing either independent observation."""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from test_linux_helper_probe import NAME, setup

from muli.platforms.installation import HelperLimits
from muli.platforms.linux_transaction import (
    HelperSnapshot,
    ObservationError,
    ProcessIdentity,
    SystemdProbe,
    UnitSnapshot,
    _KernelResourceLimits,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux helper cgroup identity")


def _observe_files(
    probe: SystemdProbe, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[ProcessIdentity], list[tuple[object, ...]]]:
    processes: list[ProcessIdentity] = []
    kernels: list[tuple[object, ...]] = []
    original_process, original_kernel = probe._process, probe._kernel_limits

    def process(pid: int) -> ProcessIdentity:
        value = original_process(pid)
        processes.append(value)
        return value

    def kernel(cgroup: str, limits: _KernelResourceLimits) -> tuple[object, ...]:
        value = original_kernel(cgroup, limits)
        kernels.append(value)
        return value

    monkeypatch.setattr(probe, "_process", process)
    monkeypatch.setattr(probe, "_kernel_limits", kernel)
    return processes, kernels


async def test_helper_keeps_complete_double_observation_in_five_manager_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe, bus, argv, group, _ = setup(tmp_path)
    limits = HelperLimits()
    processes, kernels = _observe_files(probe, monkeypatch)
    cgroup = "/user.slice/app.slice/" + NAME
    expected_process = ProcessIdentity(
        pid=123,
        start_ticks=100,
        uid=os.geteuid(),
        executable=str(Path(argv[0]).resolve(strict=True)),
        argv=argv,
        cgroup=cgroup,
    )
    identity = group.stat()
    expected_kernel = (
        identity.st_dev,
        identity.st_ino,
        limits.memory_bytes,
        0,
        limits.tasks,
        25_000,
        100_000,
    )

    observed = await probe.read_helper(NAME, argv, limits)

    # Check all returned fields and both real proc/cgroup reads before the
    # command-count oracle; dropping evidence must not make batching pass.
    assert observed == HelperSnapshot(
        unit=UnitSnapshot(
            name=NAME,
            load_state="loaded",
            active_state="active",
            sub_state="running",
            fragment="/run/user/1000/systemd/transient/" + NAME,
            source="",
            drop_ins=(),
            needs_reload=False,
            invocation=bytes(range(16)).hex(),
            job=(0, "/"),
            service_type="exec",
            pid=123,
            cgroup=cgroup,
            executable=argv[0],
            argv=argv,
            ignore_failure=False,
            kill_mode="control-group",
            send_sigkill=True,
        ),
        process=expected_process,
        limits=limits,
    )
    assert processes == [expected_process, expected_process]
    assert kernels == [expected_kernel, expected_kernel]
    fields: Counter[str] = Counter()
    for command in bus.calls:
        if "get-property" in command:
            offset = command.index("get-property")
            assert command[offset + 3] in {
                "org.freedesktop.systemd1.Unit",
                "org.freedesktop.systemd1.Service",
            }
            fields.update(command[offset + 4 :])
        else:
            assert command[-3:] == ["GetUnit", "s", NAME]
    assert fields == Counter({name: 2 for name in bus.rows})
    assert sum("GetUnit" in command for command in bus.calls) == 1
    assert all("Environment" not in command for command in bus.calls)
    # The original path takes nine commands: GetUnit plus four per round.
    assert len(bus.calls) == 5


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("resource", "helper_limits_mismatch"),
        ("invocation", "helper_observation_changed"),
        ("process", "helper_observation_changed"),
        ("kernel", "helper_observation_changed"),
    ],
)
async def test_second_round_changes_still_reject_the_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, reason: str
) -> None:
    probe, bus, argv, group, proc = setup(tmp_path)
    limits = HelperLimits()
    processes, kernels = _observe_files(probe, monkeypatch)
    rounds = 0

    async def runner(
        command: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        nonlocal rounds
        if "get-property" in command:
            fields = command[command.index("get-property") + 4 :]
            if "InvocationID" in fields:
                rounds += 1
                if rounds == 2:
                    # Locate the second observation by its actual fields, not
                    # by the old command count or a private snapshot helper.
                    assert len(processes) == len(kernels) == 1
                    if change == "resource":
                        bus.rows["MemoryMax"] = ("t", limits.memory_bytes + 1)
                    elif change == "invocation":
                        bus.rows["InvocationID"] = ("ay", [2] * 16)
                    elif change == "process":
                        (proc / "123/cmdline").write_bytes(b"foreign\x00")
                    else:
                        # Still within the prepared limit: the second kernel
                        # read must compare identity/values, not just bounds.
                        (group / "pids.max").write_text("31\n")
        return await bus(command, timeout=timeout, limit=limit, merge_stderr=merge_stderr)

    monkeypatch.setattr(probe, "runner", runner)
    with pytest.raises(ObservationError, match=f"^{reason}$"):
        await probe.read_helper(NAME, argv, limits)

    assert rounds == 2
    if change == "process":
        assert len(processes) == 2
        assert processes[0].argv == argv and processes[1].argv == ("foreign",)
    elif change == "kernel":
        assert len(processes) == len(kernels) == 2
        assert processes[0] == processes[1]
        assert kernels[1] == (*kernels[0][:4], 31, *kernels[0][5:])
