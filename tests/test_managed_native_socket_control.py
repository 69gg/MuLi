"""Fixed loaded dbus.socket observation; no real FFI, proc or private socket.

These tests call the existing public peer/instance readers.  The new result
shape is a test-side oracle, not a replacement reader or a READY assertion.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from helpers import managed_native_peer as peer
from helpers import managed_native_worker as worker
from test_managed_native_instance import ci, instance_packet
from test_managed_native_peer import Library
from test_managed_native_peer import library as library

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux native diagnostics")


class SocketLibrary(Library):
    """Reuse the actual ctypes wrapper with fixed C reply values."""

    def __init__(self) -> None:
        super().__init__()
        self.sub_state = b"start-post"
        self.control_pid = 73

    def invoke(self, name: str, args: tuple[object, ...]) -> object:
        result = super().invoke(name, args)
        if name == "sd_bus_message_read_basic":
            if args[1] == b"u":
                ctypes.cast(cast(ctypes.c_void_p, args[2]), ctypes.POINTER(ctypes.c_uint32))[0] = (
                    self.control_pid
                )
            else:
                value = (
                    b"/org/freedesktop/systemd1/unit/dbus_2esocket"
                    if args[1] == b"o"
                    else self.sub_state
                )
                self.buffers.append(ctypes.create_string_buffer(value))
                self.write_pointer(args[2], ctypes.addressof(self.buffers[-1]))
        return result


@pytest.fixture
def socket_library(library: Library, monkeypatch: pytest.MonkeyPatch) -> SocketLibrary:
    # The reused fixture forbids CDLL/fromfd/open before any reader invocation.
    fake = SocketLibrary()
    api = peer._Library(cast(ctypes.CDLL, fake))
    monkeypatch.setattr(peer, "_load_library", lambda: api)
    monkeypatch.setattr(peer, "_socket_identity", fake.socket_identity)
    monkeypatch.setattr(peer, "_peer_identity", fake.peer_identity)
    monkeypatch.setattr(worker.time, "monotonic", lambda: fake.clock[0])
    return fake


@pytest.mark.parametrize("control_pid", [0, 73], ids=["no-control-pid", "control-pid"])
def test_existing_peer_replaces_basic_jobs_with_fixed_loaded_socket_scalars(
    socket_library: SocketLibrary, control_pid: int
) -> None:
    socket_library.control_pid = control_pid
    result = peer.read_manager_peer(42, 23145, 105.0)
    assert list(socket_library.messages.values()) == [
        (b"GetUnit", [b"dbus.socket"]),
        (b"Get", [b"org.freedesktop.systemd1.Unit", b"SubState"]),
        (b"Get", [b"org.freedesktop.systemd1.Socket", b"ControlPID"]),
    ]
    assert asdict(result) == {
        "code": "observed",
        "reason": "none",
        "sub_state": "start-post",
        "control_pid": control_pid,
    }
    assert socket_library.timeouts == [5_000_000, 4_750_000, 4_500_000]
    assert socket_library.socket_calls == socket_library.peer_calls == 2
    assert sorted(socket_library.freed) == [101, 102, 103, 1101, 1102, 1103]
    for flag in (
        "sd_bus_message_set_auto_start",
        "sd_bus_message_set_allow_interactive_authorization",
    ):
        assert [args[1] for name, args in socket_library.calls if name == flag] == [0, 0, 0]


def test_socket_substate_is_a_fixed_enum_not_a_native_message(
    socket_library: SocketLibrary,
) -> None:
    socket_library.sub_state = b"secret-native-reply"
    result = asdict(peer.read_manager_peer(42, 23145, 105.0))
    assert result == {
        "code": "not_observed",
        "reason": "invalid_reply",
        "sub_state": None,
        "control_pid": None,
    }
    assert "secret" not in json.dumps(result)


@dataclass(frozen=True)
class SocketFacts:
    """New wire oracle; constructing it does not bypass the production validator."""

    code: Literal["observed"] = "observed"
    reason: Literal["none"] = "none"
    sub_state: Literal["start-post"] = "start-post"
    control_pid: int = 73


@pytest.fixture
def socket_proc(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from muli.platforms import linux_transaction

    state: dict[str, Any] = {
        "clock": 100.0,
        "case": "normal",
        "pid": 73,
        "reads": [],
        "images": [],
        "charged": [],
        "rounds": {42: 0, 73: 0},
        "environment": b"SECRET=never-export-me\xff\0XDG_RUNTIME_DIR=/run/user/23145\0",
    }
    group = "/user.slice/user-23145.slice/user@23145.service"
    state["group"] = group
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    monkeypatch.setattr(worker.time, "monotonic", lambda: state["clock"])
    monkeypatch.setattr(
        worker,
        "system_executables",
        lambda: {"manager": "/usr/lib/systemd/systemd"},
    )
    monkeypatch.setattr(worker, "systemctl_executable", lambda: "/usr/bin/systemctl")

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("offline control fixture attempted native loading/socket")

    monkeypatch.setattr(peer, "_load_library", forbidden)
    monkeypatch.setattr(peer.socket, "fromfd", forbidden)

    def internal(pid: int, uid: int, deadline: float) -> SocketFacts:
        assert (pid, uid, deadline) == (42, 23145, 105.0)
        assert state["rounds"][42] == 1 and state["rounds"][73] == 0
        return SocketFacts(control_pid=state["pid"])

    monkeypatch.setattr(peer, "read_manager_peer", internal)

    def read(path: Path, limit: int, *, directory_fd: int | None = None) -> bytes:
        assert directory_fd is None and path.parent.parent == Path("/proc")
        pid = int(path.parent.name)
        assert pid in {42, 73} and limit > 0
        state["reads"].append((pid, path.name, limit))
        case = state["case"]
        if path.name == "stat":
            state["rounds"][pid] += 1
            ticks = 100 if pid == 42 else 200
            if state["rounds"][pid] > 1 and (
                case == "child-ticks" and pid == 73 or case == "manager-ticks" and pid == 42
            ):
                ticks += 1
            data = f"{pid} (never-export-me) S {'0 ' * 18}{ticks}\n".encode()
        elif path.name == "status":
            last = 0 if pid == 73 and case == "child-uid" else 23145
            data = f"Uid:\t23145 23145 23145 {last}\n".encode()
        elif path.name == "cgroup":
            current = "/foreign" if pid == 73 and case == "child-group" else group
            data = (
                f"0::{current}/{'init.scope' if pid == 42 else 'app.slice/dbus.socket'}\n".encode()
            )
        else:
            assert path.name == "environ" and pid == 73, "only child two-key environment"
            data = state["environment"]
            if case == "deadline":
                state["clock"] = 105.0
        if len(data) > limit:
            raise ValueError("invalid_observation_file")
        state["charged"].append(len(data))
        return data

    def image(path: Path) -> str:
        assert path.name == "exe" and path.parent.parent == Path("/proc")
        pid = int(path.parent.name)
        assert pid in {42, 73}
        state["images"].append(pid)
        value = "/usr/lib/systemd/systemd" if pid == 42 else "/usr/bin/systemctl"
        if pid == 73 and state["case"] == "child-image" and state["rounds"][73] > 1:
            value = "/foreign/never-export-me"
        state["charged"].append(len(os.fsencode(value)))
        return value

    monkeypatch.setattr(linux_transaction, "_read_regular", read)
    monkeypatch.setattr(os, "readlink", image)
    return state


def observe(state: dict[str, Any], limit: int = 4096) -> dict[str, Any]:
    return worker.read_manager_instance(42, 23145, 100, state["group"], 105.0, limit)


def test_no_control_pid_is_only_an_absent_observation_and_never_reads_child_proc(
    socket_proc: dict[str, Any],
) -> None:
    socket_proc["pid"] = 0
    result = observe(socket_proc)
    assert result["code"] == "observed"
    assert result["internal"] == {
        **asdict(SocketFacts(control_pid=0)),
        "control": {
            "code": "absent",
            "start_ticks": None,
            "exe": "unavailable",
            "uids": "unavailable",
            "runtime_env": "unavailable",
            "force_bus": "unavailable",
        },
    }
    assert socket_proc["rounds"] == {42: 2, 73: 0}
    assert all(pid == 42 and name != "environ" for pid, name, _ in socket_proc["reads"])
    assert result["used"] == sum(socket_proc["charged"])


@pytest.mark.parametrize(
    ("environment", "runtime", "force"),
    [
        (b"XDG_RUNTIME_DIR=/run/user/23145\0", "matches", "missing"),
        (b"XDG_RUNTIME_DIR=/run/user/23145\0SYSTEMCTL_FORCE_BUS=1\0", "matches", "on"),
        (b"SYSTEMCTL_FORCE_BUS=0\0", "missing", "off"),
        (b"XDG_RUNTIME_DIR=/foreign\0SYSTEMCTL_FORCE_BUS=invalid\0", "other", "other"),
        (b"XDG_RUNTIME_DIR=a\0XDG_RUNTIME_DIR=b\0", "duplicate", "missing"),
        (b"SYSTEMCTL_FORCE_BUS=on\0SYSTEMCTL_FORCE_BUS=off\0", "missing", "duplicate"),
        (b"SYSTEMCTL_FORCE_BUS=YeS\0", "missing", "on"),
    ],
)
def test_start_post_reads_actual_child_in_both_proc_windows_and_classifies_only_two_keys(
    socket_proc: dict[str, Any], environment: bytes, runtime: str, force: str
) -> None:
    socket_proc["environment"] = b"SECRET=never-export-me\xff\0" + environment
    result = observe(socket_proc)
    assert result["code"] == "observed"
    assert result["internal"] == {
        **asdict(SocketFacts()),
        "control": {
            "code": "observed",
            "start_ticks": 200,
            "exe": "systemctl",
            "uids": "all_expected",
            "runtime_env": runtime,
            "force_bus": force,
        },
    }
    assert socket_proc["rounds"] == {42: 2, 73: 2}
    assert [pid for pid, name, _ in socket_proc["reads"] if name == "environ"] == [73]
    assert socket_proc["images"] == [42, 73, 73, 42]
    assert result["used"] == sum(socket_proc["charged"])
    assert len((json.dumps(result, separators=(",", ":")) + "\n").encode()) <= 512
    assert "never-export-me" not in json.dumps(result) and "/run/user" not in json.dumps(result)


@pytest.mark.parametrize(
    "case", ["child-ticks", "child-uid", "child-group", "child-image", "manager-ticks", "deadline"]
)
def test_control_or_manager_window_failure_withdraws_the_whole_observation(
    socket_proc: dict[str, Any], case: str
) -> None:
    socket_proc["case"] = case
    result = observe(socket_proc)
    assert result == worker.instance_unavailable(
        "timeout" if case == "deadline" else "identity_changed", 4096
    )
    assert "control_pid" not in json.dumps(result) and "never-export-me" not in json.dumps(result)


def test_control_environment_cannot_borrow_past_the_original_proc_grant(
    socket_proc: dict[str, Any],
) -> None:
    socket_proc["environment"] = b"SECRET=never-export-me\0" * 1024
    result = observe(socket_proc, 1024)
    assert result["code"] == "not_observed" and result["used"] == 1024
    assert result["reason"] in {"invalid_reply", "output_budget"}
    assert "internal" not in result
    assert sum(socket_proc["charged"]) <= 1024
    assert all(0 < limit <= 1024 for _, _, limit in socket_proc["reads"])
    assert "never-export-me" not in json.dumps(result)


def test_control_pid_cannot_be_the_manager_itself(socket_proc: dict[str, Any]) -> None:
    socket_proc["pid"] = 42
    assert observe(socket_proc) == worker.instance_unavailable("identity_changed", 4096)
    assert all(name != "environ" for _, name, _ in socket_proc["reads"])


def test_new_maximum_compact_control_shape_keeps_original_wire_caps_and_legacy_is_readback_only() -> (
    None
):
    control = {
        "code": "observed",
        "start_ticks": 2**64 - 1,
        "exe": "systemctl",
        "uids": "all_expected",
        "runtime_env": "duplicate",
        "force_bus": "duplicate",
    }
    internal = {
        "code": "observed",
        "reason": "none",
        "sub_state": "stop-pre-sigterm",
        "control_pid": 2**31 - 1,
        "control": control,
    }
    inner = {
        "code": "observed",
        "reason": "none",
        "exe": "manager",
        "uids": "all_expected",
        "used": worker.MAX_BYTES - 1024,
        "internal": internal,
    }
    assert worker.instance_projection(inner) == inner
    assert len((json.dumps(inner, separators=(",", ":")) + "\n").encode()) <= 512
    outer = {**instance_packet(), **inner}
    assert ci.manager_instance_facts(outer, allow_legacy=False)
    assert len((json.dumps(outer, separators=(",", ":")) + "\n").encode()) <= 1024
    legacy = {
        **instance_packet(),
        "internal": {"code": "observed", "reason": "none", "basic_state": "inactive", "njobs": 4},
    }
    assert ci.manager_instance_facts(legacy)
    assert not ci.manager_instance_facts(legacy, allow_legacy=False)
