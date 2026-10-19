from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import muli.capture as capture_module
from muli.capture import Capture
from muli.config import CaptureSettings
from muli.tools.portal_helper import PortalRequest, copy_png, local_path, request_path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
SYSTEM_PYTHON = "/usr/bin/python3"


@dataclass
class FakePortal:
    process: asyncio.subprocess.Process
    log: Path

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    async def wait(self, event: str) -> dict[str, Any]:
        async def receive() -> dict[str, Any]:
            assert self.process.stdout is not None
            while line := await self.process.stdout.readline():
                value = json.loads(line)
                if value["event"] == event:
                    return value
            raise AssertionError("fake Portal exited before " + event)

        return await asyncio.wait_for(receive(), 4)


async def stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            process.kill()
            await process.wait()


@pytest.fixture
async def private_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    if (
        sys.platform != "linux"
        or not shutil.which("dbus-daemon")
        or not Path(SYSTEM_PYTHON).is_file()
    ):
        pytest.skip("requires existing Linux system Python, Gio and dbus-daemon")
    check = subprocess.run(
        [
            SYSTEM_PYTHON,
            "-c",
            "import gi; gi.require_version('Gio','2.0'); from gi.repository import Gio",
        ],
        capture_output=True,
        timeout=3,
    )
    if check.returncode:
        pytest.skip("system Python has no Gio")
    configuration = tmp_path / "bus.conf"
    configuration.write_text("""<!DOCTYPE busconfig PUBLIC
      "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
      "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
      <busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen><auth>EXTERNAL</auth>
      <policy context="default"><allow send_destination="*"/>
        <allow receive_sender="*"/><allow own="*"/></policy></busconfig>""")
    bus = await asyncio.create_subprocess_exec(
        "dbus-daemon",
        "--nofork",
        "--nosyslog",
        "--config-file=" + str(configuration),
        "--print-address=1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert bus.stdout is not None
        address = (await asyncio.wait_for(bus.stdout.readline(), 3)).decode().strip()
        assert address.startswith("unix:")
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
        monkeypatch.setenv("MULI_TEST_PRIVATE_BUS", address)
        yield address
    finally:
        await stop(bus)


@pytest.fixture
async def fake_portal(
    private_bus: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> AsyncIterator[FakePortal]:
    image = tmp_path / "portal result.png"
    image.write_bytes(PNG)
    log = tmp_path / "portal.jsonl"
    configuration = tmp_path / "fake.json"
    configuration.write_text(
        json.dumps(
            {
                "uri": image.as_uri(),
                "log": str(log),
                **getattr(request, "param", {}),
            }
        )
    )
    process = await asyncio.create_subprocess_exec(
        SYSTEM_PYTHON,
        str(Path(__file__).parent / "fixtures" / "portal_fake.py"),
        str(configuration),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(
            os.environ, DBUS_SESSION_BUS_ADDRESS=private_bus, MULI_TEST_PRIVATE_BUS=private_bus
        ),
    )
    fake = FakePortal(process, log)
    try:
        await fake.wait("ready")
        yield fake
    finally:
        await stop(process)
        assert process.stderr is not None
        error = (await process.stderr.read()).decode()
        assert not error, error


def portal_capture(tmp_path: Path) -> Capture:
    return Capture(
        CaptureSettings(enabled=True, backend="portal", portal_timeout_seconds=1),
        tmp_path / "muli",
        platform="linux",
        python_path=SYSTEM_PYTHON,
    )


@pytest.mark.parametrize(
    "fake_portal", [{"version": 1}, {"version": 2}, {"version": 3}], indirect=True
)
async def test_portal_versions_import_private_png(fake_portal: FakePortal, tmp_path: Path) -> None:
    capture = portal_capture(tmp_path)
    result = await asyncio.wait_for(capture.screenshot(), 5)
    assert result["backend"] == "portal"
    path = Path(result["path"])
    assert path.read_bytes() == PNG
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    call = next(item for item in fake_portal.events() if item["event"] == "screenshot")
    version = capture.status()["portal"]["version"]
    assert call["parent"] == ""
    assert ("interactive" in call["options"]) is (version >= 2)
    assert "target" not in call["options"]
    assert capture.status()["permission_verified"] is False


@pytest.mark.parametrize(
    "fake_portal",
    [{"early": True}, {"early": True, "legacy": True}, {"spoof": True}],
    indirect=True,
)
async def test_early_response_waits_for_returned_handle(
    fake_portal: FakePortal, tmp_path: Path
) -> None:
    assert Path((await portal_capture(tmp_path).screenshot())["path"]).read_bytes() == PNG


@pytest.mark.parametrize("fake_portal", [{"early": True, "method_error": True}], indirect=True)
async def test_early_success_is_discarded_if_method_fails(
    fake_portal: FakePortal,
    tmp_path: Path,
) -> None:
    capture = portal_capture(tmp_path)
    with pytest.raises(ValueError, match="请求失败"):
        await capture.screenshot()
    assert list(capture.files.root.iterdir()) == []


@pytest.mark.parametrize("fake_portal", [{"response": 1}, {"response": 2}], indirect=True)
async def test_portal_rejection_never_falls_back(fake_portal: FakePortal, tmp_path: Path) -> None:
    capture = portal_capture(tmp_path)
    with pytest.raises(ValueError, match="取消|未完成"):
        await capture.screenshot()
    assert list(capture.files.root.iterdir()) == []
    assert len([item for item in fake_portal.events() if item["event"] == "screenshot"]) == 1


@pytest.mark.parametrize("fake_portal", [{"never": True}], indirect=True)
async def test_response_deadline_closes_without_waiting_for_response(
    fake_portal: FakePortal,
    tmp_path: Path,
) -> None:
    capture = portal_capture(tmp_path)
    with pytest.raises(ValueError, match="等待超时"):
        await asyncio.wait_for(capture.screenshot(), 4)
    assert any(item["event"] == "closed" for item in fake_portal.events())
    assert not any(item["event"] == "response" for item in fake_portal.events())
    assert list(capture.files.root.iterdir()) == []


@pytest.mark.parametrize(
    "fake_portal",
    [
        {"never": True},
        {"never": True, "legacy": True, "reply_delay": 400},
        {"never": True, "close_delay": 300},
    ],
    indirect=True,
)
async def test_cancel_closes_real_request_and_settles_helper(
    fake_portal: FakePortal,
    tmp_path: Path,
) -> None:
    capture = portal_capture(tmp_path)
    task = asyncio.create_task(capture.screenshot())
    call = await fake_portal.wait("screenshot")
    task.cancel()
    closed = await fake_portal.wait("close")
    task.cancel()  # Repeated cancellation must still wait for the helper's Close.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 4)
    assert closed["handle"] == call["handle"]
    assert any(item["event"] == "closed" for item in fake_portal.events())
    assert list(capture.files.root.iterdir()) == []


@pytest.mark.parametrize(
    "fake_portal",
    [{"never": True, "register_after_close": True}],
    indirect=True,
)
async def test_cancel_retries_unknown_predicted_handle_once_after_registration(
    fake_portal: FakePortal, tmp_path: Path
) -> None:
    capture = portal_capture(tmp_path)
    task = asyncio.create_task(capture.screenshot())
    await fake_portal.wait("screenshot")
    assert not any(item["event"] == "registered" for item in fake_portal.events())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 4)
    events = fake_portal.events()
    names = [item["event"] for item in events]
    assert names.count("close_missing") == names.count("close") == names.count("closed") == 1
    assert names.index("close_missing") < names.index("registered") < names.index("close")
    assert list(capture.files.root.iterdir()) == []


@pytest.mark.parametrize("handle_first", [False, True])
@pytest.mark.parametrize("confirmed_failure", [False, True])
def test_unknown_close_retries_only_once_when_real_handle_becomes_known(
    handle_first: bool, confirmed_failure: bool
) -> None:
    class Connection:
        def __init__(self) -> None:
            self.callbacks: list[Any] = []

        def get_unique_name(self) -> str:
            return ":1.99"

        def call(self, *args: Any) -> None:
            self.callbacks.append(args[-2])

        def call_finish(self, result: Any) -> Any:
            if isinstance(result, Exception):
                raise result
            return result

    connection = Connection()
    glib = SimpleNamespace(
        MainLoop=lambda: SimpleNamespace(quit=lambda: None),
        VariantType=SimpleNamespace(new=lambda value: value),
        Error=RuntimeError,
    )
    gio = SimpleNamespace(DBusCallFlags=SimpleNamespace(NO_AUTO_START=0))
    request = PortalRequest(connection, ":1.1", 3, 1, gio, glib)
    request.cancel_reason = "stopped"
    request.close_request(request.predicted)
    returned = SimpleNamespace(unpack=lambda: (request.predicted,))
    if handle_first:
        request.returned(connection, returned, None)
    connection.callbacks[0](connection, RuntimeError("UnknownObject"), None)
    if not handle_first:
        request.returned(connection, returned, None)
    assert len(connection.callbacks) == 2
    assert request.closing == {request.handle}
    connection.callbacks[1](
        connection, RuntimeError("UnknownObject") if confirmed_failure else None, None
    )
    assert len(connection.callbacks) == 2
    assert not request.closing
    assert request.closed == (set() if confirmed_failure else {request.handle})
    assert request.done is not confirmed_failure


@pytest.mark.parametrize("fake_portal", [{"release_owner": True, "early": True}], indirect=True)
async def test_owner_replacement_discards_old_success(
    fake_portal: FakePortal, tmp_path: Path
) -> None:
    capture = portal_capture(tmp_path)
    with pytest.raises(ValueError, match="更换|退出"):
        await capture.screenshot()
    assert any(item["event"] == "closed" for item in fake_portal.events())
    assert list(capture.files.root.iterdir()) == []


async def test_probe_never_activates_missing_service(private_bus: str, tmp_path: Path) -> None:
    capture = portal_capture(tmp_path)
    state = await capture.refresh_portal()
    assert state["state"] == "unknown"
    assert state["available"] is None


async def test_probe_does_not_take_screenshot(fake_portal: FakePortal, tmp_path: Path) -> None:
    capture = portal_capture(tmp_path)
    assert (await capture.refresh_portal())["state"] == "ready"
    assert all(item["event"] != "screenshot" for item in fake_portal.events())


async def test_quota_and_recording_reject_before_portal(
    fake_portal: FakePortal,
    tmp_path: Path,
) -> None:
    capture = portal_capture(tmp_path)
    with pytest.raises(ValueError, match="仅支持 Linux 截图"):
        await capture.record(1)
    capture.config.max_total_bytes = 1024
    capture.files.max_total_bytes = 1024
    capture.files.root.mkdir(parents=True)
    (capture.files.root / "previous.png").write_bytes(b"x" * capture.config.max_total_bytes)
    with pytest.raises(ValueError, match="容量限制"):
        await capture.screenshot()
    assert all(item["event"] != "screenshot" for item in fake_portal.events())


def test_predicted_handle_and_strict_uri() -> None:
    assert request_path(":1.23", "muli_a") == "/org/freedesktop/portal/desktop/request/1_23/muli_a"
    assert local_path("file://localhost/tmp/a%20b.png") == Path("/tmp/a b.png")
    assert local_path("file:///tmp/%2520.png").name == "%20.png"


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.invalid/x.png",
        "file://other/tmp/x",
        "file://user@localhost/tmp/x",
        "file:///tmp/x?",
        "file:///tmp/x#",
        "file:///tmp/%",
        "file:///tmp/%00x",
        "file:///tmp/../x",
        "file:///tmp/%2e%2e/x",
        "\nfile:///tmp/x",
        "file:///tmp/a\tb",
        "file:///tmp/%0ax",
        "file:relative.png",
        "file:////host/x",
    ],
)
def test_uri_rejects_remote_ambiguous_or_control_input(uri: str) -> None:
    with pytest.raises(ValueError):
        local_path(uri)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor flags")
def test_copy_accepts_documents_style_path_and_keeps_source(tmp_path: Path) -> None:
    source = tmp_path / "doc" / "abc123" / "screenshot.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(PNG)
    output = tmp_path / "owned.png"
    output.touch(mode=0o600)
    copy_png(source.as_uri(), output, len(PNG))
    assert output.read_bytes() == source.read_bytes() == PNG


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor flags")
@pytest.mark.parametrize(
    "kind", ["symlink", "parent_link", "output_link", "fifo", "directory", "huge", "bad"]
)
def test_copy_rejects_unsafe_files_without_deleting_source(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(PNG)
    output = tmp_path / "owned.png"
    output.touch(mode=0o600)
    original = source
    if kind == "symlink":
        source = tmp_path / "source-link"
        source.symlink_to(original)
    elif kind == "parent_link":
        (tmp_path / "dir").mkdir()
        (tmp_path / "dir" / "source.png").write_bytes(PNG)
        (tmp_path / "link").symlink_to(tmp_path / "dir", target_is_directory=True)
        source = tmp_path / "link" / "source.png"
    elif kind == "output_link":
        output.unlink()
        output.symlink_to(original)
    elif kind == "fifo":
        source = tmp_path / "fifo"
        os.mkfifo(source)
    elif kind == "directory":
        source = tmp_path
    elif kind == "huge":
        source.write_bytes(PNG + b"x")
    elif kind == "bad":
        source.write_bytes(b"not png")
    with pytest.raises((ValueError, OSError)):
        copy_png(source.as_uri(), output, len(PNG))
    assert original.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor flags")
def test_copy_growth_after_stat_reads_only_budget_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muli.tools import portal_helper

    source = tmp_path / "source.png"
    source.write_bytes(PNG)
    output = tmp_path / "output.png"
    output.touch(mode=0o600)
    inode = source.stat().st_ino
    original_fstat = portal_helper.os.fstat
    checks = 0

    def changing(descriptor: int) -> os.stat_result:
        nonlocal checks
        details = original_fstat(descriptor)
        if details.st_ino == inode:
            checks += 1
            if checks == 2:
                with source.open("ab") as appended:
                    appended.write(b"x" * 1024)
        return details

    monkeypatch.setattr(portal_helper.os, "fstat", changing)
    with pytest.raises(ValueError, match="容量限制"):
        copy_png(source.as_uri(), output, len(PNG))
    assert output.stat().st_size <= len(PNG)
    assert source.stat().st_size == len(PNG) + 1024


async def test_sync_status_disabled_and_auto_never_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not launch helper")

    monkeypatch.setattr(capture_module, "run_process", forbidden)
    for settings in (CaptureSettings(backend="portal"), CaptureSettings(enabled=True)):
        capture = Capture(settings, tmp_path, platform="linux")
        assert capture.status()["portal"]["state"] == "unknown"
        await capture.refresh_portal()


async def test_shared_probe_survives_one_waiter_cancel_and_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await release.wait()
            return {"exit_code": 0, "output": '{"state":"ready","available":true,"version":3}'}
        finally:
            settled.set()

    monkeypatch.setattr(capture_module, "run_process", run)
    capture = portal_capture(tmp_path)
    first = asyncio.create_task(capture.refresh_portal())
    await entered.wait()
    second = asyncio.create_task(capture.refresh_portal())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second)["available"] is True
    assert calls == 1
    assert (await capture.refresh_portal())["available"] is True
    assert calls == 1
    settled.clear()
    release.clear()
    entered.clear()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/new-test-bus")
    third = asyncio.create_task(capture.refresh_portal())
    await asyncio.wait_for(entered.wait(), 1)
    assert calls == 2
    await capture.aclose()
    with pytest.raises(asyncio.CancelledError):
        await third
    assert settled.is_set()
    await capture.refresh_portal()
    assert calls == 2


async def test_probe_ttl_failure_and_old_generation_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [100.0]
    calls = 0

    async def run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("system Python has no GI")
        if calls == 3:
            monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/another-private-bus")
        return {"exit_code": 0, "output": '{"state":"ready","available":true,"version":3}'}

    monkeypatch.setattr(capture_module, "run_process", run)
    monkeypatch.setattr(capture_module.time, "monotonic", lambda: current[0])
    capture = portal_capture(tmp_path)
    assert (await capture.refresh_portal())["state"] == "error"
    await capture.refresh_portal()
    assert calls == 1
    current[0] += 11
    assert (await capture.refresh_portal())["state"] == "ready"
    current[0] += 61
    assert (await capture.refresh_portal())["state"] == "unknown"
    assert capture.status()["portal"]["available"] is None
    assert (await capture.refresh_portal())["state"] == "ready"
    assert calls == 4
