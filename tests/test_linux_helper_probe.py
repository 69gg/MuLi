from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from test_linux_transaction import FakeBus, fixture

from muli.platforms.installation import HelperLimits
from muli.platforms.linux_transaction import ObservationError, ProbeLimits, SystemdProbe

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux helper cgroup identity")
NAME = "muli-transaction-" + "a" * 64 + ".service"


class HelperBus(FakeBus):
    listing: object = [[]]

    async def __call__(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        if "ListUnitsByPatterns" in argv and not self.failure and not self.block:
            self.calls.append(argv)
            assert argv[-4:] == ["asas", "0", "1", NAME]
            return {
                "exit_code": 0,
                "output": json.dumps({"type": "a(ssssssouso)", "data": self.listing}),
            }
        return await super().__call__(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)


def setup(tmp_path: Path) -> tuple[SystemdProbe, HelperBus, tuple[str, ...], Path, Path]:
    service, original, proc = fixture(tmp_path)
    bus = HelperBus(service)
    bus.rows = original.rows
    argv = (service.executable, "-I", "-m", "muli.management_helper", str(tmp_path / "journal"))
    cgroup = "/user.slice/app.slice/" + NAME
    bus.rows.update(
        Id=("s", NAME),
        Type=("s", "exec"),
        ControlGroup=("s", cgroup),
        FragmentPath=("s", "/run/user/1000/systemd/transient/" + NAME),
        ExecStart=("a(sasbttttuii)", [[argv[0], list(argv), False, 0, 0, 0, 0, 123, 0, 0]]),
        Transient=("b", True),
        CollectMode=("s", "inactive"),
        PartOf=("as", []),
        BindsTo=("as", []),
        StopPropagatedFrom=("as", []),
        Requires=("as", ["app.slice"]),
        Requisite=("as", []),
        Conflicts=("as", ["shutdown.target"]),
        StartLimitIntervalUSec=("t", 180_000_000),
        StartLimitBurst=("u", 3),
        Restart=("s", "on-failure"),
        RestartUSec=("t", 500_000),
        RuntimeMaxUSec=("t", 180_000_000),
        TimeoutStopUSec=("t", 2_000_000),
        MemoryMax=("t", 128 * 1024 * 1024),
        MemorySwapMax=("t", 0),
        CPUQuotaPerSecUSec=("t", 250_000),
        TasksMax=("t", 32),
        LimitCORE=("t", 0),
        UMask=("u", 0o077),
        NoNewPrivileges=("b", True),
        OOMPolicy=("s", "kill"),
    )
    (proc / "123/cmdline").write_bytes(b"\x00".join(os.fsencode(a) for a in argv) + b"\x00")
    (proc / "123/cgroup").write_text("0::" + cgroup + "\n")
    root = tmp_path / "cgroup"
    group = root / cgroup[1:]
    group.mkdir(parents=True)
    for name, value in {
        "memory.max": str(128 * 1024 * 1024),
        "memory.swap.max": "0",
        "pids.max": "32",
        "cpu.max": "25000 100000",
        "memory.oom.group": "1",
    }.items():
        (group / name).write_text(value + "\n")
    return SystemdProbe(runner=bus, proc_root=proc, cgroup_root=root), bus, argv, group, proc


async def test_helper_requires_actual_process_and_enforced_kernel_limits(tmp_path: Path) -> None:
    probe, bus, argv, _, _ = setup(tmp_path)
    observed = await probe.read_helper(NAME, argv, HelperLimits())
    assert observed.process.pid == 123
    assert observed.process.argv == argv
    assert observed.unit.service_type == "exec"
    assert observed.limits.memory_bytes == 128 * 1024 * 1024
    assert len(bus.calls) == 5
    assert all("GetUnit" in call or "get-property" in call for call in bus.calls)
    assert all("Environment" not in call for call in bus.calls)


@pytest.mark.parametrize("stopped", [False, True])
async def test_empty_invocation_never_authenticates_a_helper(tmp_path: Path, stopped: bool) -> None:
    probe, bus, argv, _, _ = setup(tmp_path)
    bus.rows["InvocationID"] = ("ay", [])
    if stopped:
        bus.rows.update(MainPID=("u", 0), ActiveState=("s", "inactive"), SubState=("s", "dead"))
    with pytest.raises(ObservationError, match="^(invalid_invocation|unsupported_helper_layout)$"):
        await probe.read_helper(NAME, argv, HelperLimits())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("Transient", False),
        ("CollectMode", "inactive-or-failed"),
        ("PartOf", ["muli.service"]),
        ("BindsTo", ["other.service"]),
        ("StopPropagatedFrom", ["muli.service"]),
        ("StartLimitIntervalUSec", 0),
        ("Requires", ["muli.service"]),
        ("Requisite", ["muli.service"]),
        ("Conflicts", ["muli.service"]),
        ("StartLimitBurst", 4),
        ("Restart", "always"),
        ("RestartUSec", 0),
        ("RuntimeMaxUSec", 2**64 - 1),
        ("TimeoutStopUSec", 10_000_000),
        ("MemoryMax", 2**64 - 1),
        ("MemorySwapMax", 1),
        ("CPUQuotaPerSecUSec", 1_000_000),
        ("TasksMax", 128),
        ("LimitCORE", 100),
        ("UMask", 0o022),
        ("NoNewPrivileges", False),
        ("OOMPolicy", "continue"),
        ("MemorySwapMax", False),
    ],
)
async def test_declared_limits_must_match_prepared_budget(
    tmp_path: Path, name: str, value: object
) -> None:
    probe, bus, argv, _, _ = setup(tmp_path)
    bus.rows[name] = (bus.rows[name][0], value)
    with pytest.raises(ObservationError):
        await probe.read_helper(NAME, argv, HelperLimits())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("memory.max", "max"),
        ("memory.max", str(129 * 1024 * 1024)),
        ("memory.swap.max", "1"),
        ("pids.max", "33"),
        ("cpu.max", "max 100000"),
        ("cpu.max", "25001 100000"),
        ("cpu.max", "1 0"),
        ("cpu.max", "0 100000"),
        ("memory.oom.group", "0"),
        ("pids.max", "0"),
        ("pids.max", "9" * 129),
    ],
)
async def test_declared_limits_do_not_replace_kernel_evidence(
    tmp_path: Path, name: str, value: str
) -> None:
    probe, _, argv, group, _ = setup(tmp_path)
    (group / name).write_text(value + "\n")
    with pytest.raises(ObservationError):
        await probe.read_helper(NAME, argv, HelperLimits())


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo", "parent-link"])
async def test_kernel_files_are_bounded_anchored_and_nonblocking(tmp_path: Path, kind: str) -> None:
    probe, _, argv, group, _ = setup(tmp_path)
    path = group / "memory.max"
    path.unlink()
    if kind == "symlink":
        other = tmp_path / "other"
        other.write_text("1024\n")
        path.symlink_to(other)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "parent-link":
        path.write_text("1024\n")
        moved = group.with_name("moved")
        group.rename(moved)
        group.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ObservationError):
        await probe.read_helper(NAME, argv, HelperLimits())


@pytest.mark.parametrize("change", ["invocation", "kernel", "process", "inactive", "nested"])
async def test_helper_identity_and_observation_changes_fail_closed(
    tmp_path: Path, change: str
) -> None:
    probe, bus, argv, group, proc = setup(tmp_path)
    if change == "inactive":
        bus.rows["ActiveState"] = ("s", "inactive")
    elif change == "nested":
        bus.rows["ControlGroup"] = ("s", "/muli.service/" + NAME)
    else:
        # Trigger on the second actual Unit property read, after kernel and
        # process were captured, regardless of how the interface is batched.
        original = probe.runner
        calls = 0

        async def runner(
            argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
        ) -> dict[str, Any]:
            nonlocal calls
            if "get-property" in argv:
                fields = argv[argv.index("get-property") + 4 :]
                if "InvocationID" in fields:
                    calls += 1
                    if calls == 2:
                        if change == "invocation":
                            bus.rows["InvocationID"] = ("ay", [2] * 16)
                        elif change == "kernel":
                            (group / "pids.max").write_text("31\n")
                        else:
                            (proc / "123/cmdline").write_bytes(b"foreign\x00")
            return await original(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)

        probe.runner = runner
    with pytest.raises(ObservationError):
        await probe.read_helper(NAME, argv, HelperLimits())


async def test_empty_exact_name_listing_is_only_absence_proof(tmp_path: Path) -> None:
    probe, bus, _, _, _ = setup(tmp_path)
    assert await probe.helper_exists(NAME) is False
    bus.listing = [
        [
            [
                NAME,
                "helper",
                "loaded",
                "inactive",
                "dead",
                "",
                "/org/freedesktop/systemd1/unit/helper",
                0,
                "",
                "/",
            ]
        ]
    ]
    assert await probe.helper_exists(NAME) is True
    bus.failure = True
    with pytest.raises(ObservationError, match="unavailable"):
        await probe.helper_exists(NAME)


@pytest.mark.parametrize("listing", [[], [None], [[[]]], [[[], []]], [[], []], [""], [[False]]])
async def test_invalid_listing_is_never_treated_as_absent(tmp_path: Path, listing: object) -> None:
    probe, bus, _, _, _ = setup(tmp_path)
    bus.listing = listing
    with pytest.raises(ObservationError):
        await probe.helper_exists(NAME)


@pytest.mark.parametrize("operation", ["read", "exists"])
async def test_helper_probe_preserves_timeout_and_cancels_runner(
    tmp_path: Path, operation: str
) -> None:
    probe, bus, argv, _, _ = setup(tmp_path)
    probe.limits = ProbeLimits(seconds=0.01)
    bus.block = True
    with pytest.raises(TimeoutError):
        if operation == "read":
            await probe.read_helper(NAME, argv, HelperLimits())
        else:
            await probe.helper_exists(NAME)
    assert bus.cancelled is True


async def test_external_cancellation_is_not_hidden(tmp_path: Path) -> None:
    probe, bus, argv, _, _ = setup(tmp_path)
    bus.block = True
    task = asyncio.create_task(probe.read_helper(NAME, argv, HelperLimits()))
    await bus.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus.cancelled is True
