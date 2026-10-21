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
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from helpers.managed_recovery_worker import BOOT, PREFIX, deployment, prepare, read_record
from test_management_process_recovery import CLEANUP_SECONDS, checkpoint, cleanup, remaining

import muli
from muli.managed_transaction import HealthProofV2, format_digest
from muli.management_projection import read_projection
from muli.management_transaction import Role, TransactionJournal
from muli.platforms.filesystem_identity import FilesystemError, verify_local_filesystems
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


def finish_service(process: subprocess.Popen[bytes]) -> None:
    # Only after all timed recovery proofs: cleanup has its own existing cap.
    process.terminate()
    assert process.wait(timeout=CLEANUP_SECONDS) in {0, -signal.SIGTERM}


class _ExitProcess:
    def __init__(self, *, exit_code: int = 0, exit_seconds: float = 0.1) -> None:
        self.exit_code = exit_code
        self.exit_seconds = exit_seconds
        self.terminated = 0
        self.waits: list[float] = []

    def terminate(self) -> None:
        self.terminated += 1

    def wait(self, timeout: float | None = None) -> int:
        assert self.terminated and timeout is not None, "exit wait must be signalled and bounded"
        self.waits.append(timeout)
        if timeout < self.exit_seconds:
            raise subprocess.TimeoutExpired("fixture-service", timeout)
        return self.exit_code


def test_final_service_exit_has_cleanup_budget_when_operation_time_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline = CASE_SECONDS
    clock = deadline - 0.032199681
    monkeypatch.setattr(
        "test_management_process_recovery.time", SimpleNamespace(monotonic=lambda: clock)
    )
    process = _ExitProcess()
    process.terminate()
    # Reproduce the CI boundary deterministically without sleeping or spawning.
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=min(CLEANUP_SECONDS, remaining(deadline)))
    assert 0 < process.waits[0] < process.exit_seconds
    clock = deadline + 1
    process.waits.clear()
    finish_service(cast(subprocess.Popen[bytes], process))
    assert process.waits == [CLEANUP_SECONDS] and process.terminated == 2


@pytest.mark.parametrize("exit_code", [1, -signal.SIGKILL])
def test_final_service_exit_still_rejects_unexpected_status(exit_code: int) -> None:
    process = _ExitProcess(exit_code=exit_code)
    with pytest.raises(AssertionError):
        finish_service(cast(subprocess.Popen[bytes], process))
    assert process.waits == [CLEANUP_SECONDS] and process.terminated == 1


def test_final_service_exit_does_not_extend_cleanup_or_retry_a_timeout() -> None:
    process = _ExitProcess(exit_seconds=CLEANUP_SECONDS + 1)
    with pytest.raises(subprocess.TimeoutExpired):
        finish_service(cast(subprocess.Popen[bytes], process))
    assert process.waits == [CLEANUP_SECONDS] and process.terminated == 1


def fixture_parent(repository: Path) -> Path:
    configured = os.environ.get("MULI_MANAGED_TEST_ROOT")
    if configured is not None:
        parent = Path(configured)
        verify_local_filesystems((parent,))
        return parent
    unsupported: list[str] = []
    for parent in (repository, Path(tempfile.gettempdir()).resolve()):
        try:
            verify_local_filesystems((parent,))
        except FilesystemError as error:
            if error.code not in {"filesystem_unsupported", "filesystem_durability_unsupported"}:
                raise
            unsupported.append(error.code)
        else:
            return parent
    raise AssertionError("managed_fixture_no_supported_filesystem:" + ",".join(unsupported))


@pytest.mark.parametrize("code", ["filesystem_unsupported", "filesystem_durability_unsupported"])
def test_fixture_parent_uses_supported_second_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    monkeypatch.delenv("MULI_MANAGED_TEST_ROOT", raising=False)
    repository, temporary = tmp_path / "checkout", tmp_path / "temporary"
    repository.mkdir()
    temporary.mkdir()
    observed: list[Path] = []

    def verify(directories: Sequence[Path]) -> None:
        assert len(directories) == 1
        observed.append(directories[0])
        if directories[0] == repository:
            raise FilesystemError(code)
        assert directories[0] == temporary

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temporary))
    monkeypatch.setattr(sys.modules[__name__], "verify_local_filesystems", verify)
    assert fixture_parent(repository) == temporary
    assert observed == [repository, temporary]


def test_fixture_parent_fails_when_both_candidates_are_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MULI_MANAGED_TEST_ROOT", raising=False)
    repository, temporary = tmp_path / "checkout", tmp_path / "temporary"
    repository.mkdir()
    temporary.mkdir()
    observed: list[Path] = []

    def verify(directories: Sequence[Path]) -> None:
        assert len(directories) == 1
        observed.append(directories[0])
        raise FilesystemError(
            "filesystem_unsupported"
            if directories[0] == repository
            else "filesystem_durability_unsupported"
        )

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temporary))
    monkeypatch.setattr(sys.modules[__name__], "verify_local_filesystems", verify)
    with pytest.raises(
        AssertionError,
        match="^managed_fixture_no_supported_filesystem:"
        "filesystem_unsupported,filesystem_durability_unsupported$",
    ):
        fixture_parent(repository)
    assert observed == [repository, temporary]


@pytest.mark.parametrize(
    "error", [FilesystemError("filesystem_observation_unavailable"), OSError("fixture observation")]
)
def test_fixture_parent_does_not_hide_observation_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.delenv("MULI_MANAGED_TEST_ROOT", raising=False)
    observed: list[Path] = []

    def verify(directories: Sequence[Path]) -> None:
        observed.extend(directories)
        raise error

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "temporary"))
    monkeypatch.setattr(sys.modules[__name__], "verify_local_filesystems", verify)
    with pytest.raises(type(error)) as caught:
        fixture_parent(tmp_path)
    assert caught.value is error
    assert observed == [tmp_path]


def test_fixture_parent_verifies_explicit_root_without_implicit_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "explicit"
    selected.mkdir()
    observed: list[Path] = []

    def verify(directories: Sequence[Path]) -> None:
        observed.extend(directories)

    monkeypatch.setenv("MULI_MANAGED_TEST_ROOT", str(selected))
    monkeypatch.setattr(sys.modules[__name__], "verify_local_filesystems", verify)
    assert fixture_parent(tmp_path / "unused") == selected
    assert observed == [selected]


@pytest.mark.parametrize(
    "code", ["filesystem_durability_unsupported", "filesystem_observation_unavailable"]
)
def test_fixture_parent_explicit_root_errors_never_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    selected = tmp_path / "explicit"
    observed: list[Path] = []

    def verify(directories: Sequence[Path]) -> None:
        observed.extend(directories)
        raise FilesystemError(code)

    monkeypatch.setenv("MULI_MANAGED_TEST_ROOT", str(selected))
    monkeypatch.setattr(sys.modules[__name__], "verify_local_filesystems", verify)
    with pytest.raises(FilesystemError, match=f"^{code}$"):
        fixture_parent(tmp_path)
    assert observed == [selected]


@pytest.mark.parametrize("value", ["", "relative", "/tmp/../tmp"])
def test_fixture_parent_explicit_root_rejects_invalid_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MULI_MANAGED_TEST_ROOT", value)
    with pytest.raises(FilesystemError, match="^filesystem_invalid_path$"):
        fixture_parent(tmp_path)


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
    # Retain the actual package declaration unchanged, rather than inventing fixture evidence.
    shutil.copytree(source, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    dependencies = Path(sys.prefix) / "lib" / version / "site-packages"
    # Simulate an already imported system hook without changing the host Python.
    (site / "000_muli_sitecustomize_noop.pth").write_text(
        "import sys, types; sys.modules['sitecustomize'] = types.ModuleType('sitecustomize'); "
        "sys.modules['sitecustomize'].__dict__['_muli_fixture_noop'] = True\n",
        encoding="utf-8",
    )
    (site / "fixture.pth").write_text(
        f"{dependencies}\n{repository / 'tests/helpers'}\n"
        "import _muli_managed_process_fixture_hook\n",
        encoding="utf-8",
    )
    (site / "_muli_managed_process_fixture_hook.py").write_text(
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
    # Admit the explicit CI volume or the two local parents. Boot rechecks the private
    # child directories; unsupported storage never becomes a fake observation.
    with tempfile.TemporaryDirectory(
        prefix=".managed-process-", dir=fixture_parent(repository)
    ) as temporary:
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
                finish_service(live)
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
                        assert booted["sitecustomize_noop"] is True
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
