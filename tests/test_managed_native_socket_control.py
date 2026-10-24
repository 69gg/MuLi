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
        "cmdline": (
            b"/usr/bin/systemctl\0--user\0set-environment\0"
            b"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/23145/bus\0"
        ),
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
        elif path.name == "cmdline":
            assert pid == 73 and limit <= 512
            assert state["rounds"][73] == 1
            data = state["cmdline"]
            if case == "cmdline-deadline":
                state["clock"] = 105.0
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
        if pid == 73 and state["case"] == "child-other-image":
            value = "/foreign/never-export-me"
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
            "env": "---",
            "cmd": "unavailable",
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
def test_start_post_reads_actual_child_in_both_proc_windows_and_classifies_original_environment_cases(
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
            "env": {"matches": "e", "missing": "m", "other": "o", "duplicate": "d"}[runtime]
            + "m"
            + {"on": "1", "off": "0", "missing": "m", "other": "o", "duplicate": "d"}[force],
            "cmd": "set_bus",
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
        "env": "ddd",
        "cmd": "set_other",
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


@pytest.mark.parametrize(
    ("environment", "environment_code"),
    [
        (
            b"XDG_RUNTIME_DIR=/foreign\0DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/23145/bus\0",
            "oem",
        ),
        (
            b"XDG_RUNTIME_DIR=/run/user/23145\0"
            b"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/23145/bus\0"
            b"SYSTEMCTL_FORCE_BUS=YeS\0",
            "ee1",
        ),
        (b"SYSTEMCTL_FORCE_BUS=0\0", "mm0"),
        (
            b"XDG_RUNTIME_DIR=a\0XDG_RUNTIME_DIR=b\0"
            b"DBUS_SESSION_BUS_ADDRESS=a\0DBUS_SESSION_BUS_ADDRESS=b\0"
            b"SYSTEMCTL_FORCE_BUS=on\0SYSTEMCTL_FORCE_BUS=off\0",
            "ddd",
        ),
    ],
)
def test_control_joint_sample_uses_real_cmdline_and_same_environment_without_raw_values(
    socket_proc: dict[str, Any],
    environment: bytes,
    environment_code: str,
) -> None:
    socket_proc["environment"] = b"SECRET=never-export-me\xff\0" + environment
    result = observe(socket_proc)
    assert result["code"] == "observed"
    assert result["internal"]["control"] == {
        "code": "observed",
        "start_ticks": 200,
        "exe": "systemctl",
        "uids": "all_expected",
        "env": environment_code,
        "cmd": "set_bus",
    }
    selected = [(pid, name) for pid, name, _ in socket_proc["reads"]]
    assert selected.count((73, "cmdline")) == selected.count((73, "environ")) == 1
    assert socket_proc["rounds"] == {42: 2, 73: 2}
    assert socket_proc["images"] == [42, 73, 73, 42]
    assert result["used"] == sum(socket_proc["charged"])
    encoded = (json.dumps(result, separators=(",", ":")) + "\n").encode()
    assert len(encoded) <= 512
    assert b"never-export-me" not in encoded and b"/run/user" not in encoded


@pytest.mark.parametrize(
    ("arguments", "command"),
    [
        (
            [
                b"/bin/systemctl",
                b"--user",
                b"set-environment",
                b"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/23145/bus",
            ],
            "set_bus",
        ),
        (
            [
                b"/usr/bin/systemctl",
                b"--user",
                b"set-environment",
                b"DBUS_SESSION_BUS_ADDRESS=never-export-me",
            ],
            "set_other",
        ),
        (
            [
                b"/usr/bin/systemctl",
                b"--system",
                b"set-environment",
                b"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/23145/bus",
            ],
            "other",
        ),
        (
            [
                b"/usr/bin/systemctl",
                b"--user",
                b"set-environment",
                b"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/23145/bus",
                b"EXTRA=secret",
            ],
            "other",
        ),
    ],
)
def test_control_command_requires_the_complete_fixed_argument_shape(
    socket_proc: dict[str, Any],
    arguments: list[bytes],
    command: str,
) -> None:
    socket_proc["cmdline"] = b"\0".join(arguments) + b"\0"
    result = observe(socket_proc)
    assert result["code"] == "observed"
    child = result["internal"]["control"]
    assert child == {
        "code": "observed",
        "start_ticks": 200,
        "exe": "systemctl",
        "uids": "all_expected",
        "env": "emm",
        "cmd": command,
    }
    assert result["used"] == sum(socket_proc["charged"])
    assert "never-export-me" not in json.dumps(result) and "EXTRA" not in json.dumps(result)


@pytest.mark.parametrize("length", [512, 513])
def test_control_cmdline_byte_boundary_is_charged_to_the_existing_raw_grant(
    socket_proc: dict[str, Any],
    length: int,
) -> None:
    prefix = b"/usr/bin/systemctl\0--user\0set-environment\0DBUS_SESSION_BUS_ADDRESS="
    socket_proc["cmdline"] = prefix + b"x" * (length - len(prefix) - 1) + b"\0"
    result = observe(socket_proc)
    if length == 512:
        assert result["code"] == "observed"
        assert result["internal"]["control"]["cmd"] == "set_other"
        assert result["used"] == sum(socket_proc["charged"])
        assert (73, "cmdline", 512) in socket_proc["reads"]
    else:
        assert result["code"] == "not_observed" and result["used"] == 4096
        assert "internal" not in result
    assert all(0 < limit <= 4096 for _, _, limit in socket_proc["reads"])


@pytest.mark.parametrize("cmdline", [b"", b"systemctl\0--user", b"\0--user\0", b"systemctl\0\0"])
def test_control_invalid_or_incomplete_nul_cmdline_withdraws_the_whole_sample(
    socket_proc: dict[str, Any],
    cmdline: bytes,
) -> None:
    socket_proc["cmdline"] = cmdline
    result = observe(socket_proc)
    assert result == worker.instance_unavailable("invalid_reply", 4096)
    assert "internal" not in result


def test_compact_joint_control_is_current_only_and_retains_both_artifact_legacy_shapes() -> None:
    child = {
        "code": "observed",
        "start_ticks": 2**64 - 1,
        "exe": "systemctl",
        "uids": "all_expected",
        "env": "ddd",
        "cmd": "set_other",
    }
    internal = {
        "code": "observed",
        "reason": "none",
        "sub_state": "stop-pre-sigterm",
        "control_pid": 2**31 - 1,
        "control": child,
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
    packet = {**instance_packet(), **inner}
    assert ci.manager_instance_facts(packet, allow_legacy=False)
    assert len((json.dumps(packet, separators=(",", ":")) + "\n").encode()) <= 1024
    socket_legacy = {
        **packet,
        "internal": {
            **internal,
            "control": {
                "code": "observed",
                "start_ticks": 200,
                "exe": "systemctl",
                "uids": "all_expected",
                "runtime_env": "other",
                "force_bus": "missing",
            },
        },
    }
    basic_legacy = {
        **packet,
        "internal": {
            "code": "observed",
            "reason": "none",
            "basic_state": "inactive",
            "njobs": 5,
        },
    }
    for legacy in (socket_legacy, basic_legacy):
        assert ci.manager_instance_facts(legacy)
        assert not ci.manager_instance_facts(legacy, allow_legacy=False)
        with pytest.raises(worker.NativeError, match="^native_diagnostic_invalid$"):
            worker.instance_projection({key: legacy[key] for key in inner})


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("env", "ee"),
        ("env", "eeee"),
        ("env", "EE1"),
        ("env", "ee2"),
        ("env", "---"),
        ("env", None),
        ("env", ["e", "e", "m"]),
        ("cmd", "set-environment"),
        ("cmd", "unavailable"),
        ("cmd", ["set_bus"]),
        ("start_ticks", True),
        ("runtime_env", "matches"),
        ("code", "absent"),
        ("exe", "other"),
    ],
)
def test_joint_control_rejects_bad_compact_fields_in_both_current_validators(
    field: str,
    bad: object,
) -> None:
    child = {
        "code": "observed",
        "start_ticks": 200,
        "exe": "systemctl",
        "uids": "all_expected",
        "env": "eem",
        "cmd": "set_bus",
        field: bad,
    }
    internal = {**asdict(SocketFacts()), "control": child}
    assert not worker.peer_facts(internal, control=True)
    assert not ci.peer_facts(internal, allow_legacy=False)
    assert not ci.peer_facts(internal)


@pytest.mark.parametrize("short", [False, True], ids=["exact-charge", "missing-final-image-room"])
def test_joint_sample_tight_raw_grant_never_refunds_or_skips_required_identity_reads(
    socket_proc: dict[str, Any],
    short: bool,
) -> None:
    first = observe(socket_proc)
    assert first["code"] == "observed"
    assert first["internal"]["control"]["cmd"] == "set_bus"
    required = first["used"]
    assert type(required) is int and required == sum(socket_proc["charged"])
    final_image_bytes = socket_proc["charged"][-1]
    socket_proc.update(reads=[], images=[], charged=[], rounds={42: 0, 73: 0})
    grant = required - final_image_bytes if short else required
    result = observe(socket_proc, grant)
    if short:
        assert result["code"] == "not_observed" and result["used"] == grant
        assert "internal" not in result
    else:
        assert result["code"] == "observed" and result["used"] == grant
        assert socket_proc["rounds"] == {42: 2, 73: 2}
        assert socket_proc["images"] == [42, 73, 73, 42]
    assert sum(socket_proc["charged"]) <= grant


def test_cmdline_overflow_probe_is_reserved_inside_the_original_grant(
    socket_proc: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muli.platforms import linux_transaction

    original = linux_transaction._read_regular
    prefix_bytes: list[int] = []

    def capture(path: Path, limit: int, *, directory_fd: int | None = None) -> bytes:
        if path.name == "cmdline":
            prefix_bytes.append(sum(socket_proc["charged"]))
        return original(path, limit, directory_fd=directory_fd)

    monkeypatch.setattr(linux_transaction, "_read_regular", capture)
    assert observe(socket_proc)["code"] == "observed"
    assert len(prefix_bytes) == 1
    grant = prefix_bytes[0] + 512
    socket_proc.update(reads=[], images=[], charged=[], rounds={42: 0, 73: 0})
    probe_bytes: list[int] = []
    socket_proc["cmdline"] = b"x" * 513

    def bounded(path: Path, limit: int, *, directory_fd: int | None = None) -> bytes:
        if path.name == "cmdline":
            remaining = grant - sum(socket_proc["charged"])
            assert 0 < limit <= 512 and limit + 1 <= remaining
            probe_bytes.append(min(len(socket_proc["cmdline"]), limit + 1))
        return original(path, limit, directory_fd=directory_fd)

    monkeypatch.setattr(linux_transaction, "_read_regular", bounded)
    result = observe(socket_proc, grant)
    assert result["code"] == "not_observed" and result["used"] == grant
    assert len(probe_bytes) == 1
    assert sum(socket_proc["charged"]) + probe_bytes[0] <= grant
    assert "internal" not in result


@pytest.mark.parametrize("case", ["cmdline-deadline", "child-other-image"])
def test_joint_command_does_not_grant_a_hook_on_timeout_or_another_image(
    socket_proc: dict[str, Any],
    case: str,
) -> None:
    socket_proc["case"] = case
    result = observe(socket_proc)
    if case == "cmdline-deadline":
        assert result == worker.instance_unavailable("timeout", 4096)
    else:
        assert result["code"] == "observed"
        assert result["internal"]["control"]["exe"] == "other"
        assert result["internal"]["control"]["cmd"] == "other"
    assert "never-export-me" not in json.dumps(result)
