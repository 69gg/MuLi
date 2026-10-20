from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from muli.management_transaction import TransactionError
from muli.platforms.linux_transaction import (
    ObservationError,
    ProbeLimits,
    SystemdProbe,
    clock_reading,
)
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux proc and unit identity")


def test_clock_uses_kernel_boot_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "boot_id"
    identifier = "12345678-1234-5678-90ab-1234567890ab"
    path.write_text(identifier + "\n")
    monkeypatch.setattr("muli.platforms.linux_transaction.time.time", lambda: 100.0)
    monkeypatch.setattr("muli.platforms.linux_transaction.time.monotonic", lambda: 10.0)
    reading = clock_reading(path)
    assert (reading.boot_id, reading.wall, reading.monotonic) == (identifier, 100.0, 10.0)


@pytest.mark.parametrize("content", [b"", b"unit-invocation", b"x" * 129, b"\xff", b"{}\n"])
def test_clock_rejects_invalid_identity(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "boot_id"
    path.write_bytes(content)
    with pytest.raises(TransactionError, match="clock_invalid"):
        clock_reading(path)


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo"])
def test_clock_does_not_follow_replacement(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "boot_id"
    if kind == "symlink":
        original = tmp_path / "original"
        original.write_text("12345678-1234-5678-90ab-1234567890ab\n")
        path.symlink_to(original)
    elif kind == "fifo":
        os.mkfifo(path)
    with pytest.raises(TransactionError, match="clock_invalid"):
        clock_reading(path)


class FakeBus:
    def __init__(self, service: UserService) -> None:
        self.calls: list[list[str]] = []
        self.before_second: Callable[[], None] | None = None
        self.failure = False
        self.block = False
        self.started = asyncio.Event()
        self.cancelled = False
        self.rows: dict[str, tuple[str, Any]] = {
            "Id": ("s", service.unit),
            "LoadState": ("s", "loaded"),
            "ActiveState": ("s", "active"),
            "SubState": ("s", "running"),
            "FragmentPath": ("s", str(service.path.absolute())),
            "SourcePath": ("s", ""),
            "DropInPaths": ("as", []),
            "NeedDaemonReload": ("b", False),
            "InvocationID": ("ay", list(range(16))),
            "Job": ("(uo)", [0, "/"]),
            "Type": ("s", "simple"),
            "MainPID": ("u", 123),
            "ControlGroup": ("s", "/user.slice/example.service"),
            "KillMode": ("s", "control-group"),
            "SendSIGKILL": ("b", True),
            "ExecStart": (
                "a(sasbttttuii)",
                [[service.executable, service._arguments(), False, 0, 0, 0, 0, 123, 0, 0]],
            ),
        }

    async def __call__(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        self.calls.append(argv)
        assert 0 < timeout <= 30 and limit <= 262_144 and merge_stderr is False
        assert "--auto-start=no" in argv and "--allow-interactive-authorization=no" in argv
        if self.block:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.failure:
            return {"exit_code": 1, "output": "", "stderr": "private service diagnostic"}
        if "GetUnit" in argv:
            return {
                "exit_code": 0,
                "output": json.dumps(
                    {"type": "o", "data": ["/org/freedesktop/systemd1/unit/muli_2eservice"]}
                ),
            }
        if len(self.calls) == 4 and self.before_second:
            self.before_second()
        first = argv.index("get-property") + 4
        output = "\n".join(
            json.dumps({"type": self.rows[name][0], "data": self.rows[name][1]})
            for name in argv[first:]
        )
        return {"exit_code": 0, "output": output + "\n"}


def write_stat(path: Path, *, start: int = 100, state: str = "S") -> None:
    # A comm with spaces and parentheses must not shift /proc's starttime field.
    fields = [state, *(["0"] * 18), str(start), "0"]
    path.write_text("123 (name ) with spaces) " + " ".join(fields))


def fixture(tmp_path: Path) -> tuple[UserService, FakeBus, Path]:
    executable = tmp_path / "python"
    executable.write_bytes(b"not executed")
    service = UserService(
        tmp_path / "配置.json",
        tmp_path / "data",
        tmp_path / "units",
        platform="linux",
        executable=str(executable),
    )
    service.path.parent.mkdir()
    service.path.write_text(service.render())
    proc = tmp_path / "proc"
    process = proc / "123"
    process.mkdir(parents=True)
    write_stat(process / "stat")
    (process / "cmdline").write_bytes(
        b"\x00".join(os.fsencode(a) for a in service._arguments()) + b"\x00"
    )
    (process / "cgroup").write_text("0::/user.slice/example.service\n")
    (process / "exe").symlink_to(executable)
    return service, FakeBus(service), proc


async def test_standard_service_is_observed_without_manager_mutations(tmp_path: Path) -> None:
    service, bus, proc = fixture(tmp_path)
    result = await SystemdProbe(runner=bus, proc_root=proc).read(service)
    assert result.process is not None and result.process.start_ticks == 100
    assert result.process.argv == tuple(service._arguments())
    assert result.unit.invocation == bytes(range(16)).hex()
    assert len(bus.calls) == 5
    assert all("GetUnit" in c or "get-property" in c for c in bus.calls)
    assert all("Environment" not in c for c in bus.calls)


async def test_stopped_snapshot_does_not_claim_writer_or_cgroup_is_empty(tmp_path: Path) -> None:
    service, bus, proc = fixture(tmp_path)
    bus.rows.update(MainPID=("u", 0), ActiveState=("s", "inactive"), SubState=("s", "dead"))
    result = await SystemdProbe(runner=bus, proc_root=proc).read(service)
    assert result.process is None
    assert result.unit.cgroup == "/user.slice/example.service"
    assert not hasattr(result, "writer_free")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Id", "foreign.service"),
        ("LoadState", "not-found"),
        ("Type", "forking"),
        ("FragmentPath", "/other/unit"),
        ("SourcePath", "/generator/input"),
        ("DropInPaths", ["/override.conf"]),
        ("NeedDaemonReload", True),
        ("KillMode", "process"),
        ("SendSIGKILL", False),
    ],
)
async def test_nonstandard_layout_is_rejected(tmp_path: Path, field: str, value: Any) -> None:
    service, bus, proc = fixture(tmp_path)
    bus.rows[field] = (bus.rows[field][0], value)
    with pytest.raises(ObservationError, match="unsupported_service_layout"):
        await SystemdProbe(runner=bus, proc_root=proc).read(service)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MainPID", True),
        ("MainPID", -1),
        ("NeedDaemonReload", 0),
        ("KillMode", False),
        ("SendSIGKILL", 1),
        ("InvocationID", [False] * 16),
        ("InvocationID", [256] * 16),
        ("InvocationID", []),
        ("Job", [True, "/"]),
        ("DropInPaths", "/not-an-array"),
        ("ExecStart", []),
        ("ExecStart", [["incomplete"]]),
        ("Type", "simple\nforged"),
    ],
)
async def test_malformed_structured_types_fail_closed(
    tmp_path: Path, field: str, value: Any
) -> None:
    service, bus, proc = fixture(tmp_path)
    bus.rows[field] = (bus.rows[field][0], value)
    with pytest.raises(ObservationError):
        await SystemdProbe(runner=bus, proc_root=proc).read(service)


@pytest.mark.parametrize(
    "part", ["argv", "exe", "cgroup", "zombie", "missing", "fifo", "unit", "symlink"]
)
async def test_actual_process_and_unit_must_match(tmp_path: Path, part: str) -> None:
    service, bus, proc = fixture(tmp_path)
    if part == "argv":
        (proc / "123/cmdline").write_bytes(b"other\x00")
    elif part == "exe":
        (proc / "123/exe").unlink()
        (proc / "123/exe").symlink_to(tmp_path / "other-python")
    elif part == "cgroup":
        (proc / "123/cgroup").write_text("0::/other.service\n")
    elif part == "zombie":
        write_stat(proc / "123/stat", state="Z")
    elif part == "missing":
        (proc / "123/stat").unlink()
    elif part == "fifo":
        (proc / "123/stat").unlink()
        os.mkfifo(proc / "123/stat")
    elif part == "unit":
        service.path.write_text(service.render() + "# changed\n")
    elif part == "symlink":
        copy = tmp_path / "copied-unit"
        copy.write_text(service.render())
        service.path.unlink()
        service.path.symlink_to(copy)
    with pytest.raises(ObservationError):
        await SystemdProbe(runner=bus, proc_root=proc).read(service)


@pytest.mark.parametrize("change", ["invocation", "start", "unit_inode", "argv"])
async def test_mid_observation_change_invalidates_whole_result(tmp_path: Path, change: str) -> None:
    service, bus, proc = fixture(tmp_path)

    def mutate() -> None:
        if change == "invocation":
            bus.rows["InvocationID"] = ("ay", [2] * 16)
        elif change == "start":
            write_stat(proc / "123/stat", start=200)
        elif change == "unit_inode":
            temporary = service.path.with_suffix(".new")
            temporary.write_text(service.render())
            temporary.replace(service.path)
        elif change == "argv":
            (proc / "123/cmdline").write_bytes(b"replaced\x00")

    bus.before_second = mutate
    with pytest.raises(ObservationError, match="changed"):
        await SystemdProbe(runner=bus, proc_root=proc).read(service)


async def test_total_timeout_and_cancellation_settle_owned_runner(tmp_path: Path) -> None:
    service, bus, proc = fixture(tmp_path)
    bus.block = True
    with pytest.raises(TimeoutError):
        await SystemdProbe(runner=bus, proc_root=proc, limits=ProbeLimits(seconds=0.02)).read(
            service
        )
    assert bus.cancelled
    bus.cancelled = False
    task = asyncio.create_task(SystemdProbe(runner=bus, proc_root=proc).read(service))
    bus.started.clear()
    await bus.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus.cancelled


async def test_errors_never_echo_private_manager_stderr(tmp_path: Path) -> None:
    service, bus, proc = fixture(tmp_path)
    bus.failure = True
    with pytest.raises(ObservationError, match="^systemd_observation_unavailable$"):
        await SystemdProbe(runner=bus, proc_root=proc).read(service)


async def test_oversized_output_and_files_rejected(tmp_path: Path) -> None:
    service, bus, proc = fixture(tmp_path)
    bus.rows["SourcePath"] = ("s", "x" * 5000)
    with pytest.raises(ObservationError, match="invalid_bus_output"):
        await SystemdProbe(runner=bus, proc_root=proc, limits=ProbeLimits(command_bytes=1024)).read(
            service
        )
    bus.rows["SourcePath"] = ("s", "")
    (proc / "123/cmdline").write_bytes(b"x" * 4000 + b"\x00")
    with pytest.raises(ObservationError, match="invalid_observation_file"):
        await SystemdProbe(runner=bus, proc_root=proc, limits=ProbeLimits(file_bytes=1024)).read(
            service
        )
