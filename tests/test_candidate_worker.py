from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import sys
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest
from test_candidate_process import NAME, spec_for, unit_for

from muli.candidate_policy import CandidateLimits
from muli.platforms import candidate_process as module
from muli.platforms.candidate_process import (
    CandidateObservation,
    CandidateProbe,
    CandidateProcessError,
)
from muli.platforms.linux_transaction import ProcessIdentity
from muli.process import run_process

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux abstract credential gate")


async def test_worker_requires_real_parent_peer_nonce_and_explicit_gate(tmp_path: Path) -> None:
    spec = spec_for(tmp_path)
    name, nonce = "muli-candidate-" + "b" * 32, "c" * 64
    path = spec.private_root / "control.json"
    marker = spec.private_root / "executed"
    payload = {
        "argv": [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('yes')",
            str(marker),
        ],
        "env": {},
        "cwd": str(spec.cwd),
        "deadline": spec.deadline,
        "parent": os.getpid(),
        "uid": os.geteuid(),
        "gate": name,
        "nonce": nonce,
    }
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind("\x00" + name)
        listener.listen(1)
        listener.setblocking(False)
        worker = str(Path(module.__file__).with_name("_candidate_worker.py"))
        task = asyncio.create_task(
            run_process([sys.executable, "-I", "-S", worker, str(path)], timeout=5, limit=1024)
        )
        connection, _ = await asyncio.wait_for(asyncio.get_running_loop().sock_accept(listener), 2)
        try:
            connection.setblocking(False)
            pid, uid, _ = struct.unpack(
                "iII", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            assert pid != os.getpid() and uid == os.geteuid()
            assert await module._receive(connection, 64) == nonce.encode()
            assert not marker.exists()
            await asyncio.get_running_loop().sock_sendall(connection, b"G")
        finally:
            connection.close()
        result = await task
    assert result["exit_code"] == 0 and marker.read_text() == "yes"


@pytest.mark.parametrize("mode", ["accept", "wrong-nonce", "changed-proof"])
async def test_real_parent_gate_rejects_other_peer_without_spending_permission(
    tmp_path: Path,
    mode: str,
) -> None:
    tmp_path = tmp_path / ("r" * 100)
    tmp_path.mkdir()
    spec = spec_for(tmp_path)
    assert len(os.fsencode(spec.private_root)) > 108
    gate, nonce = "muli-candidate-" + os.urandom(16).hex(), "d" * 64
    path, marker = spec.private_root / "control.json", spec.private_root / "executed"
    path.write_text(
        json.dumps(
            {
                "argv": [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                    str(marker),
                ],
                "env": {},
                "cwd": str(spec.cwd),
                "deadline": spec.deadline,
                "parent": os.getpid(),
                "uid": os.geteuid(),
                "gate": gate,
                "nonce": nonce,
            }
        )
    )
    path.chmod(0o600)
    worker = (
        sys.executable,
        "-I",
        "-S",
        str(Path(module.__file__).with_name("_candidate_worker.py")),
        str(path),
    )
    loop = asyncio.get_running_loop()
    with (
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wrong,
    ):
        listener.bind("\x00" + gate)
        listener.listen(8)
        listener.setblocking(False)
        wrong.setblocking(False)
        # This connection has our PID, not the actual waiting worker's PID.
        await loop.sock_connect(wrong, "\x00" + gate)
        process = await asyncio.create_subprocess_exec(
            *worker, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        observation = CandidateObservation(
            replace(unit_for(NAME, worker), pid=process.pid),
            ProcessIdentity(
                process.pid,
                1,
                os.geteuid(),
                str(Path(sys.executable).resolve()),
                worker,
                "/app.slice/" + NAME,
            ),
        )

        class GateProbe(CandidateProbe):
            async def waiting(
                self, name: str, argv: tuple[str, ...], limits: CandidateLimits, runtime_us: int
            ) -> CandidateObservation:
                assert name == NAME and argv == worker and not marker.exists()
                if mode == "changed-proof":
                    return replace(observation, unit=replace(observation.unit, invocation="2" * 32))
                return observation

        directory = module._open_directory(spec.private_root)
        try:
            async with asyncio.timeout(2):
                with nullcontext() if mode == "accept" else pytest.raises(CandidateProcessError):
                    await module._permit(
                        listener,
                        observation,
                        "e" * 64 if mode == "wrong-nonce" else nonce,
                        probe=GateProbe(),
                        name=NAME,
                        worker=worker,
                        spec=spec,
                        runtime_us=1_000_000,
                        directory=directory,
                        permit_deadline=spec.deadline,
                    )
                assert await process.wait() == (0 if mode == "accept" else 125)
        finally:
            os.close(directory)
            if process.returncode is None:
                process.kill()
                await process.wait()
    assert marker.exists() is (mode == "accept")


@pytest.mark.parametrize("failure", ["disconnect", "wrong-parent", "deadline"])
async def test_worker_does_not_execute_without_permission(tmp_path: Path, failure: str) -> None:
    spec = spec_for(tmp_path)
    gate = "muli-candidate-" + os.urandom(16).hex()
    marker = spec.private_root / "must-not-exist"
    path = spec.private_root / "control.json"
    path.write_text(
        json.dumps(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                    str(marker),
                ],
                "env": {},
                "cwd": str(spec.cwd),
                "deadline": time.monotonic() - 1 if failure == "deadline" else spec.deadline,
                "parent": os.getpid() + 1 if failure == "wrong-parent" else os.getpid(),
                "uid": os.geteuid(),
                "gate": gate,
                "nonce": "a" * 64,
            }
        )
    )
    path.chmod(0o600)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind("\x00" + gate)
        listener.listen(1)
        listener.setblocking(False)
        task = asyncio.create_task(
            run_process(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    str(Path(module.__file__).with_name("_candidate_worker.py")),
                    str(path),
                ],
                timeout=5,
                limit=1024,
            )
        )
        if failure != "deadline":
            connection, _ = await asyncio.wait_for(
                asyncio.get_running_loop().sock_accept(listener), 2
            )
            connection.close()
        result = await task
    assert result["exit_code"] == 125 and not marker.exists()
    assert result["output"].strip() == "candidate_gate_failed"
