from __future__ import annotations

import asyncio
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from helpers.managed_recovery_worker import BOOT, PREFIX, deployment, prepare, read_record
from test_management_process_recovery import checkpoint, cleanup, remaining

import muli
from muli.managed_transaction import HealthProofV2, format_digest
from muli.management_projection import read_projection
from muli.management_transaction import Role, TransactionJournal
from muli.platforms.installation import PackageLimits
from muli.platforms.managed_deployment import SlotIdentity, register_local_slot
from muli.platforms.managed_launcher import package_fingerprint
from muli.platforms.managed_probe import managed_unit_arguments
from muli.store import Store

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux real managed processes/flock"
)
CASE_SECONDS = 20.0
HTTP_BYTES = 4096


def expect(
    process: subprocess.Popen[bytes], expected: bytes, output: bytearray, deadline: float
) -> None:
    try:
        checkpoint(process, expected, output, deadline)
    except AssertionError:
        # Keep the useful tail of an early exception, within the same byte cap.
        assert process.stdout is not None
        until = min(deadline, time.monotonic() + 0.25)
        while len(output) < HTTP_BYTES and time.monotonic() < until:
            if not select.select([process.stdout], [], [], until - time.monotonic())[0]:
                break
            data = os.read(process.stdout.fileno(), HTTP_BYTES - len(output))
            if not data:
                break
            output.extend(data)
        raise AssertionError(output.decode("utf-8", errors="replace")) from None


def slot(root: Path, name: str, repository: Path) -> SlotIdentity:
    """Retain only bounded MuLi bytes; reuse installed dependencies without a sync."""
    assert muli.__file__ is not None
    source = Path(muli.__file__).parent
    limits = PackageLimits()
    package_fingerprint(source, limits.model_dump())
    prefix = root / name
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = prefix / "lib" / version / "site-packages"
    python = prefix / "bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys.executable).resolve())
    base = Path(getattr(sys, "_base_executable", sys.executable)).resolve().parent
    (prefix / "pyvenv.cfg").write_text(
        f"home = {base}\ninclude-system-site-packages = false\n", encoding="utf-8"
    )
    package = site / "muli"
    shutil.copytree(source, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    # The declaration is a fixture of this same checkout, not a released-package attestation.
    (package / "compatibility.json").write_text(
        json.dumps(
            {
                "metadata_version": 1,
                "distribution": "muli-agent",
                "release": muli.__version__,
                "handoff_protocol": 2,
                "requires_migration": False,
                "formats": {
                    domain: {"reads": [1], "writes": 1}
                    for domain in (
                        "database",
                        "objects",
                        "configuration",
                        "ingress",
                        "outbox",
                        "memory",
                        "install_profile",
                    )
                },
            }
        ),
        encoding="utf-8",
    )
    dependencies = Path(sys.prefix) / "lib" / version / "site-packages"
    (site / "fixture.pth").write_text(
        f"{dependencies}\n{repository / 'tests/helpers'}\n", encoding="utf-8"
    )
    (site / "sitecustomize.py").write_text(
        "import os\n"
        "if os.environ.get('MULI_RECOVERY_ROLE') in {'base','target'}:\n"
        "    from managed_recovery_worker import install_hooks\n"
        "    install_hooks()\n",
        encoding="utf-8",
    )
    return register_local_slot(prefix, python, package)


def environment(root: Path, *, failure: bool) -> dict[str, str]:
    values = dict(os.environ)
    values.update(
        MULI_HOME=str(root / "home"),
        XDG_CONFIG_HOME=str(root / "xdg"),
        MULI_RECOVERY_ROOT=str(root),
        MULI_RECOVERY_FAILURE=str(int(failure)),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
    )
    for name in ("MULI_CONFIG", "MULI_DATA_DIR", "PYTHONPATH", "PYTHONOPTIMIZE"):
        values.pop(name, None)
    return values


async def request(
    client: httpx.AsyncClient,
    root: Path,
    role: Role,
    deadline: float,
    *,
    wrong_nonce: bool = False,
) -> tuple[int, dict[str, Any]]:
    with TransactionJournal(root / "journal", root / "data/muli.sqlite3", create=False) as journal:
        record = read_record(journal)
        nonce = record.challenge if role == "target" else record.rollback_challenge
        assert nonce is not None
    # HTTPX's read timeout is per chunk; include the whole response and closure.
    async with asyncio.timeout(min(2.0, remaining(deadline))):
        async with client.stream(
            "GET",
            PREFIX + "readiness",
            params={"generation": record.generation, "boot": BOOT[role]},
            headers={"X-MuLi-Nonce": "wrong-nonce" if wrong_nonce else nonce},
        ) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=512):
                body.extend(chunk)
                assert len(body) <= HTTP_BYTES, "managed readiness exceeded its output budget"
            value = json.loads(body)
            assert isinstance(value, dict)
            return response.status_code, value


@pytest.mark.parametrize("failure", [False, True], ids=["switch", "target-rejected-rollback"])
async def test_actual_cli_boot_activation_and_projection_preserve_latest_database(
    failure: bool,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    worker = repository / "tests/helpers/managed_recovery_worker.py"
    # /tmp may be tmpfs. Keep the private, automatically removed fixture on the
    # checkout's real filesystem so the actual supported-filesystem check runs.
    with tempfile.TemporaryDirectory(prefix=".managed-process-", dir=repository) as temporary:
        root = Path(temporary)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            port = listener.getsockname()[1]
            base, target = slot(root, "base", repository), slot(root, "target", repository)
            before = prepare(root, port, base, target)
            database = root / "data/muli.sqlite3"
            inode = database.stat().st_ino
            with closing(Store.open_existing(database, durable=True)) as store:
                plan = store.get("management", before.request_id)
                unknown = store.get("message", "existing-unknown")
            assert plan is not None and unknown is not None
            values = environment(root, failure=failure)
            processes: list[subprocess.Popen[bytes]] = []
            deadline = time.monotonic() + CASE_SECONDS
            live: subprocess.Popen[bytes] | None = None

            def spawn(
                argv: list[str], role: str, *, service: bool = False
            ) -> subprocess.Popen[bytes]:
                assert len(processes) < 3, "fixture exceeded its fresh-process budget"
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env={
                        **values,
                        "MULI_RECOVERY_ROLE": role,
                        "MULI_RECOVERY_SOCKET": str(listener.fileno()),
                    },
                    close_fds=True,
                    pass_fds=(listener.fileno(),) if service else (),
                    pipesize=HTTP_BYTES,
                )
                processes.append(process)
                return process

            def publish(pid: int, role: Role) -> None:
                (root / "manager.json").write_text(
                    json.dumps({"pid": pid, "role": role}), encoding="utf-8"
                )
                (root / "cgroup/fixture.slice/muli.service/cgroup.events").write_bytes(
                    b"populated 1\n" if pid else b"populated 0\n"
                )

            def start(role: Role) -> subprocess.Popen[bytes]:
                assert live is None or live.poll() is not None, (
                    "two service children cannot coexist"
                )
                with TransactionJournal(root / "journal", database, create=False) as journal:
                    current = read_record(journal)
                    assert current.intent == ("start_target" if role == "target" else "start_base")
                    anchor, _ = deployment(journal)
                service = spawn(list(managed_unit_arguments(anchor, base)), role, service=True)
                expect(service, b"service-ready", bytearray(), deadline)
                publish(service.pid, role)
                return service

            def continue_controller() -> None:
                assert driver.stdin is not None
                driver.stdin.write(b"G")
                driver.stdin.flush()

            try:
                driver = spawn(
                    [base.lexical_python, "-I", str(worker), "controller", str(root)], "controller"
                )
                output = bytearray()
                expect(driver, b"start-target", output, deadline)
                live = start("target")
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}",
                    trust_env=False,
                    follow_redirects=False,
                    timeout=2.0,
                    headers={"Host": "localhost", "Accept-Encoding": "identity"},
                ) as client:
                    status, error = await request(
                        client, root, "target", deadline, wrong_nonce=True
                    )
                    assert status == 401 and error["detail"] == "transaction_unauthorized"
                    status, value = await request(client, root, "target", deadline)
                    if failure:
                        assert status == 409 and value["detail"] == "maintenance_unsupported"
                    else:
                        proof = HealthProofV2.model_validate(value)
                        assert status == 200 and proof.admission == "paused"
                        assert proof.installation == target.digest and proof.boot == BOOT["target"]
                    continue_controller()
                    selected: Role = "target"
                    if failure:
                        expect(driver, b"stop-target", output, deadline)
                        live.terminate()
                        assert live.wait(timeout=min(2.0, remaining(deadline))) in {
                            0,
                            -signal.SIGTERM,
                        }
                        publish(0, "target")
                        continue_controller()
                        expect(driver, b"start-base", output, deadline)
                        live = start("base")
                        selected = "base"
                        status, value = await request(client, root, selected, deadline)
                        assert status == 200 and value["admission"] == "paused"
                        continue_controller()
                    expect(driver, b"controller-resolved", output, deadline)
                    assert driver.wait(timeout=remaining(deadline)) == 0, bytes(output)
                    # These are real nonce routes and the real host's projection callback.
                    for _ in range(2):
                        status, value = await request(client, root, selected, deadline)
                        proof = HealthProofV2.model_validate(value)
                        assert status == 200 and proof.admission == "released"
                        assert proof.role == selected and proof.boot == BOOT[selected]
                        assert proof.installation == (base.digest if failure else target.digest)
                        assert proof.format_digest == format_digest(before.observed_format)
                live.terminate()
                assert live.wait(timeout=min(2.0, remaining(deadline))) in {0, -signal.SIGTERM}
                with (
                    TransactionJournal(root / "journal", database, create=False) as journal,
                    journal.writer_lease(),
                    closing(Store.open_existing(database, durable=True)) as store,
                ):
                    after = read_record(journal)
                    assert after.resolved and after.phase == (
                        "rolled_back" if failure else "committed"
                    )
                    assert (
                        after.deadline == before.deadline and after.generation == before.generation
                    )
                    assert after.identity == before.identity and after.challenge == before.challenge
                    assert after.rollback_attempted is failure
                    assert after.base.configuration == after.target.configuration
                    assert after.base.installation != after.target.installation
                    assert after.health is not None and after.health.boot == BOOT[selected]
                    assert database.stat().st_ino == inode
                    assert store.instance_id == before.identity.store_instance
                    assert store.existing_main_session_id() == before.identity.main_session
                    latest = store.get("fixture", "latest")
                    assert latest is not None and latest["after_prepare"] == before.generation
                    assert store.pending_inputs(before.identity.main_session) == 1
                    row = store.db.execute(
                        "SELECT state FROM ingress WHERE id='latest-input'"
                    ).fetchone()
                    assert row is not None and row[0] == "pending"
                    row = store.db.execute(
                        "SELECT state,payload FROM channel_outbox WHERE id='latest-outbox'"
                    ).fetchone()
                    assert row is not None and row[0] == "unknown"
                    assert json.loads(row[1]) == {
                        "parts": [{"text": "reply prepared after transaction"}]
                    }
                    assert store.get("message", "existing-unknown") == unknown
                    final = store.get("management", before.request_id)
                    assert final is not None and final["routing"] == plan["routing"]
                    assert final["status"] == ("failed" if failure else "completed")
                    receipt = read_projection(store)
                    assert receipt is not None and receipt.generation == after.generation
                    assert receipt.revision == after.revision
                    assert (
                        store.db.execute(
                            "SELECT count(*) FROM objects WHERE kind='management_projection'"
                        ).fetchone()[0]
                        == 1
                    )
                    events = store.db.execute(
                        "SELECT data FROM events WHERE type='management.completed'"
                    ).fetchall()
                    assert len(events) == 1 and json.loads(events[0][0]) == final
                    roles: tuple[Role, ...] = ("target", "base") if failure else ("target",)
                    for role in roles:
                        booted = store.get("fixture", "boot-" + role)
                        assert booted is not None and booted["passive"] is True
                        chosen = base if role == "base" else target
                        assert (
                            booted["prefix"] == chosen.prefix
                            and booted["python"] == chosen.lexical_python
                        )
                        observed = store.get("fixture", "http-" + role)
                        assert observed is not None and observed["posts"] == (
                            0 if failure and role == "target" else 1
                        )
                        assert observed["allowed"] is (not failure or role == "base")
                        assert observed["started"] is (not failure or role == "base")
            finally:
                cleanup(processes)
