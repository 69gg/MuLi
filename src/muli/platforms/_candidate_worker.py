"""Private stdlib-only gate. Executed as a file with Python -I -S, never imported."""

from __future__ import annotations

import json
import math
import os
import re
import socket
import stat
import struct
import sys
import time
from typing import Any

_MAX_METADATA = 1_048_576


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _text(value: object) -> bool:
    return isinstance(value, str) and "\x00" not in value


def main(path: str) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > _MAX_METADATA
        ):
            raise ValueError
        raw = source.read(_MAX_METADATA + 1)
        after = os.fstat(source.fileno())
    if len(raw) > _MAX_METADATA or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError
    value = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(value, dict) or set(value) != {
        "argv",
        "env",
        "cwd",
        "deadline",
        "parent",
        "uid",
        "gate",
        "nonce",
    }:
        raise ValueError
    argv, env, cwd = value["argv"], value["env"], value["cwd"]
    deadline, parent, uid = value["deadline"], value["parent"], value["uid"]
    gate, nonce = value["gate"], value["nonce"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(_text(item) for item in argv)
        or not os.path.isabs(argv[0])
        or not isinstance(env, dict)
        or not all(_text(k) and k and "=" not in k and _text(v) for k, v in env.items())
        or not _text(cwd)
        or not os.path.isabs(cwd)
        or type(deadline) not in {int, float}
        or not math.isfinite(deadline)
        or type(parent) is not int
        or parent <= 0
        or type(uid) is not int
        or uid != os.geteuid()
        or not isinstance(gate, str)
        or not re.fullmatch(r"muli-candidate-[a-f0-9]{32}", gate)
        or not isinstance(nonce, str)
        or not re.fullmatch(r"[a-f0-9]{64}", nonce)
    ):
        raise ValueError
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        connection.settimeout(remaining)
        connection.connect("\x00" + gate)
        pid, peer_uid, _ = struct.unpack(
            "iII", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if (pid, peer_uid) != (parent, uid):
            raise ValueError
        connection.sendall(nonce.encode("ascii"))
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        if connection.recv(1) != b"G" or time.monotonic() >= deadline:
            raise ValueError
    os.chdir(cwd)
    os.execve(argv[0], argv, env)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError
        main(sys.argv[1])
    except Exception:
        # Never print argv, environment, file contents or arbitrary OS diagnostics.
        sys.stderr.write("candidate_gate_failed\n")
        sys.exit(125)
