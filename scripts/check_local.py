"""Run local checks inside a verified, shared Linux cgroup resource budget."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path

# Operator policy: shared across repositories/worktrees, not multiplied per agent.
SLICE = "muli-checks.slice"
MEMORY_BYTES = 1024**3
SHARED_MEMORY_BYTES = 2 * MEMORY_BYTES
TASKS = 128
MAX_SECONDS = 180
NODE_HEAP_MB = 256
NODE_TEST_TIMEOUT_MS = 15000
MANAGER_TIMEOUT = 10


def manager(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=True,
        text=True,
        capture_output=True,
        timeout=MANAGER_TIMEOUT,
    )


def verify_group(path: Path, memory: int, *, service: bool = False) -> None:
    for name, maximum in (("memory.max", memory), ("memory.swap.max", 0), ("pids.max", TASKS)):
        value = (path / name).read_text().strip()
        if value == "max" or int(value) > maximum:
            raise RuntimeError(f"Resource limit not enforced: {name}={value}")
    quota, period = (path / "cpu.max").read_text().split()
    if quota == "max" or int(quota) > int(period):
        raise RuntimeError("CPU quota exceeds one CPU")
    if service and (path / "memory.oom.group").read_text().strip() != "1":
        raise RuntimeError("Whole-service OOM cleanup is not enabled")


def verify_inside(unit: str, seconds: int) -> None:
    if not 1 <= seconds <= MAX_SECONDS:
        raise ValueError("Invalid service time budget")
    membership = Path("/proc/self/cgroup").read_text().strip()
    if not membership.startswith("0::/") or "\n" in membership:
        raise RuntimeError("A unified cgroup v2 hierarchy is required")
    group = Path("/sys/fs/cgroup") / membership.removeprefix("0::/")
    if group.name != unit or group.parent.name != SLICE:
        raise RuntimeError("Check did not enter its dedicated service and shared slice")
    verify_group(group, MEMORY_BYTES, service=True)
    verify_group(group.parent, SHARED_MEMORY_BYTES)
    properties = dict(
        line.split("=", 1)
        for line in manager(
            "show",
            unit,
            "-p",
            "RuntimeMaxUSec",
            "-p",
            "KillMode",
            "-p",
            "SendSIGKILL",
            "-p",
            "Type",
            "-p",
            "Restart",
            "-p",
            "TimeoutStopUSec",
            "-p",
            "LimitCORE",
        ).stdout.splitlines()
    )
    # systemd formats integer seconds as '3min', '1min 30s', etc.
    expected = (
        f"{seconds // 60}min"
        if seconds % 60 == 0
        else (f"{seconds // 60}min {seconds % 60}s" if seconds > 60 else f"{seconds}s")
    )
    if properties != {
        "RuntimeMaxUSec": expected,
        "KillMode": "control-group",
        "SendSIGKILL": "yes",
        "Type": "exec",
        "Restart": "no",
        "TimeoutStopUSec": "2s",
        "LimitCORE": "0",
    }:
        raise RuntimeError(f"Service lifetime limits not enforced: {properties}")


def run_inside(unit: str, seconds: int, command: list[str]) -> int:
    def interrupted(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    # systemd considers several terminating signals clean exits for Type=exec.
    # A supervisor preserves the command's signal failure as a nonzero exit code.
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGPIPE):
        signal.signal(signum, interrupted)
    verify_inside(unit, seconds)
    print("Kernel resource limits verified; starting check.", flush=True)
    code = subprocess.run(command, check=False).returncode
    return code if code >= 0 else 128 - code


def command_for(mode: str, args: list[str]) -> list[str]:
    if args[:1] == ["--"]:
        args = args[1:]
    if mode == "frontend":
        if any(arg.startswith("-") for arg in args):
            raise ValueError("frontend accepts test file paths only")
        paths = args or [str(path) for path in sorted(Path("tests/frontend").glob("*.test.mjs"))]
        if not paths:
            raise ValueError("No frontend tests found")
        args = [
            "node",
            "--test",
            "--test-concurrency=1",
            f"--test-timeout={NODE_TEST_TIMEOUT_MS}",
            *paths,
        ]
    if not args:
        raise ValueError("A command is required")
    executable = shutil.which(args[0])
    if executable is None:
        raise ValueError(f"Executable not found: {args[0]}")
    return [str(Path(executable).absolute()), *args[1:]]


def stop_service(unit: str) -> bool:
    try:
        state = manager("show", unit, "--value", "-p", "LoadState").stdout.strip()
        if state == "not-found":
            return True
        manager("stop", unit)
        return True
    except (subprocess.SubprocessError, OSError):
        # A collected unit is already gone; a stuck stop still gets a bounded kill.
        try:
            manager("kill", "--kill-whom=all", "--signal=KILL", unit)
            manager("stop", unit)
            return True
        except (subprocess.SubprocessError, OSError):
            print(
                f"Could not confirm cleanup of {unit}; runtime limit remains active.",
                file=sys.stderr,
            )
            return False


def run(command: list[str], seconds: int) -> int:
    if sys.platform != "linux" or not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        raise RuntimeError("Local checks require Linux cgroup v2; no unbounded fallback")
    if not 1 <= seconds <= MAX_SECONDS:
        raise ValueError(f"seconds must be between 1 and {MAX_SECONDS}")
    manager(
        "set-property",
        "--runtime",
        SLICE,
        f"MemoryMax={SHARED_MEMORY_BYTES}",
        "MemorySwapMax=0",
        "CPUQuota=100%",
        f"TasksMax={TASKS}",
    )
    manager("start", SLICE)
    unit = f"muli-check-{uuid.uuid4().hex}.service"
    properties = {
        "MemoryMax": str(MEMORY_BYTES),
        "MemorySwapMax": "0",
        "CPUQuota": "100%",
        "TasksMax": str(TASKS),
        "OOMPolicy": "kill",
        "KillMode": "control-group",
        "SendSIGKILL": "yes",
        "RuntimeMaxSec": str(seconds),
        "TimeoutStopSec": "2",
        "TimeoutStartSec": "10",
        "Restart": "no",
        "LimitCORE": "0",
        "Nice": "10",
    }
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
        "PYTHONPATH": str(Path.cwd() / "src"),
        "UV_OFFLINE": "1",
        "UV_NO_SYNC": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "NODE_OPTIONS": f"--max-old-space-size={NODE_HEAP_MB}",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    if cache := os.environ.get("UV_CACHE_DIR"):
        environment["UV_CACHE_DIR"] = cache
    invocation = [
        "systemd-run",
        "--user",
        "--pipe",
        "--wait",
        "--collect",
        "--service-type=exec",
        "--expand-environment=no",
        f"--unit={unit}",
        f"--slice={SLICE}",
        f"--working-directory={Path.cwd()}",
        *(f"--property={key}={value}" for key, value in properties.items()),
        *(f"--setenv={key}={value}" for key, value in environment.items()),
        "--",
        sys.executable,
        str(Path(__file__).resolve()),
        "--inside",
        unit,
        "--seconds",
        str(seconds),
        "run",
        "--",
        *command,
    ]
    print(f"{unit}: 1 GiB / shared 2 GiB, swap 0, shared 1 CPU, {seconds}s", flush=True)
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}

    def interrupted(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    code = 125
    cleaned = False
    try:
        code = subprocess.run(invocation, timeout=seconds + 20, check=False).returncode
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        code = 124
    finally:
        try:
            for signum in previous:
                signal.signal(signum, signal.SIG_IGN)
            cleaned = stop_service(unit)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    return code if cleaned else 125


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=MAX_SECONDS)
    parser.add_argument("--inside", help=argparse.SUPPRESS)
    parser.add_argument("mode", choices=("frontend", "run"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        command = command_for(args.mode, args.command)
        if not 1 <= args.seconds <= MAX_SECONDS:
            raise ValueError(f"seconds must be between 1 and {MAX_SECONDS}")
        if args.inside:
            return run_inside(args.inside, args.seconds, command)
        return run(command, args.seconds)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Local check refused: {exc}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
