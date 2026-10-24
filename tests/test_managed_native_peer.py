"""Offline sd-bus choreography: no CDLL loading, socket or service effects."""

from __future__ import annotations

import ctypes
import errno
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from typing import cast

import pytest
from helpers import managed_native_peer as peer
from helpers import managed_native_worker as worker

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux native peer helper")


class Function:
    def __init__(self, name: str, invoke: Callable[[str, tuple[object, ...]], object]) -> None:
        self.name = name
        self.invoke = invoke
        self.argtypes: list[object] = []
        self.restype: object = None

    def __call__(self, *arguments: object) -> object:
        return self.invoke(self.name, arguments)


class Library:
    """Fake C symbols; output writes exercise the actual pointer wrapper."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.messages: dict[int, tuple[bytes, list[bytes]]] = {}
        self.buffers: list[ctypes.Array[ctypes.c_char]] = []
        self.reply_for: dict[int, int] = {}
        self.freed: list[int] = []
        self.next_id = 100
        self.mode = "normal"
        self.fail = ""
        self.errno = errno.EIO
        self.clock = [100.0]
        self.timeouts: list[int] = []
        self.socket_calls = 0
        self.peer_calls = 0

    def __getattr__(self, name: str) -> Function:
        assert name.startswith("sd_bus_")
        return Function(name, self.invoke)

    def write_pointer(self, argument: object, value: int) -> None:
        ctypes.cast(cast(ctypes.c_void_p, argument), ctypes.POINTER(ctypes.c_void_p))[0] = (
            ctypes.c_void_p(value)
        )

    def pointer(self, value: object) -> int:
        assert isinstance(value, ctypes.c_void_p) and value.value is not None
        return value.value

    def invoke(self, name: str, args: tuple[object, ...]) -> object:
        self.calls.append((name, args))
        if self.fail == name:
            if name == "sd_bus_call":
                # Failure may still return an owned message.
                self.write_pointer(args[4], 999)
            return -self.errno
        if name == "sd_bus_new":
            self.write_pointer(args[0], 1)
        elif name == "sd_bus_get_fd":
            return 123
        elif name == "sd_bus_message_new_method_call":
            self.next_id += 1
            self.write_pointer(args[1], self.next_id)
            assert args[2] is None and isinstance(args[5], bytes)
            self.messages[self.next_id] = (args[5], [])
        elif name == "sd_bus_message_append_basic":
            assert args[1] == b"s"
            # append(s) receives char[], never char**.
            raw = ctypes.cast(cast(ctypes.c_void_p, args[2]), ctypes.POINTER(ctypes.c_ubyte))
            content = bytearray()
            for index in range(128):
                if raw[index] == 0:
                    break
                content.append(raw[index])
            else:
                raise AssertionError("unterminated fixed request")
            self.messages[self.pointer(args[0])][1].append(bytes(content))
        elif name == "sd_bus_call":
            assert isinstance(args[2], int) and 0 < args[2] <= 5_000_000
            self.timeouts.append(args[2])
            self.clock[0] += 0.25
            request = self.pointer(args[1])
            self.write_pointer(args[4], request + 1000)
            self.reply_for[request + 1000] = request
            if self.mode == "last-call-expired" and len(self.timeouts) == 3:
                self.clock[0] = 105.0
        elif name == "sd_bus_message_has_signature":
            member, _ = self.messages[self.reply_for[self.pointer(args[0])]]
            assert args[1] == (b"o" if member == b"GetUnit" else b"v")
            return 0 if self.mode == "signature" else 1
        elif name == "sd_bus_message_enter_container":
            assert args[1] == b"v" and args[2] in (b"s", b"u")
            return 0 if self.mode == "variant" else 1
        elif name == "sd_bus_message_read_basic":
            if self.mode == "null-string" and args[1] != b"u":
                return 1
            if args[1] == b"u":
                ctypes.cast(cast(ctypes.c_void_p, args[2]), ctypes.POINTER(ctypes.c_uint32))[0] = 7
            else:
                assert args[1] in (b"s", b"o")
                value = (
                    b"/org/freedesktop/systemd1/unit/basic_2etarget"
                    if args[1] == b"o"
                    else b"active"
                )
                if self.mode == "long-string":
                    value = b"x" * (peer.CONTEXT_REPLY_BYTES + 1)
                if self.mode == "unknown-state" and args[1] == b"s":
                    value = b"secret-native-error-text"
                self.buffers.append(ctypes.create_string_buffer(value))
                self.write_pointer(args[2], ctypes.addressof(self.buffers[-1]))
            return 0 if self.mode == "missing-value" else 1
        elif name == "sd_bus_message_at_end":
            return 0 if self.mode == "trailing" else 1
        elif name == "sd_bus_message_exit_container":
            return 0 if self.mode == "exit-container" else 1
        elif name == "sd_bus_message_unref":
            self.freed.append(self.pointer(args[0]))
        return 0

    def socket_identity(self, uid: int) -> tuple[int, int, int]:
        assert uid == 23145
        self.socket_calls += 1
        return (1, 45 if self.mode == "socket-drift" and self.socket_calls == 2 else 44, uid)

    def peer_identity(self, descriptor: int) -> tuple[int, int]:
        assert descriptor == 123
        self.peer_calls += 1
        if self.mode == "wrong-pid":
            return 43, 23145
        if self.mode == "wrong-uid":
            return 42, 0
        if self.mode == "peer-drift" and self.peer_calls == 2:
            return 43, 23145
        return 42, 23145


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch) -> Library:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("offline test attempted native loading or a real socket")

    monkeypatch.setattr(ctypes, "CDLL", forbidden)
    monkeypatch.setattr(peer.socket, "fromfd", forbidden)
    monkeypatch.setattr(peer.os, "open", forbidden)
    fake = Library()
    api = peer._Library(cast(ctypes.CDLL, fake))
    monkeypatch.setattr(peer, "_load_library", lambda: api)
    monkeypatch.setattr(peer, "_socket_identity", fake.socket_identity)
    monkeypatch.setattr(peer, "_peer_identity", fake.peer_identity)
    monkeypatch.setattr(peer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(worker.time, "monotonic", lambda: fake.clock[0])
    return fake


def test_fixed_v255_nonvariadic_symbol_declarations(library: Library) -> None:
    """Independent header/man oracle; fake invocation cannot prove C return ABI."""
    api = peer._Library(cast(ctypes.CDLL, library))
    pointer = ctypes.c_void_p
    pointer_out = ctypes.POINTER(ctypes.c_void_p)
    error_out = ctypes.POINTER(peer._Error)
    declarations: list[tuple[str, list[object], object]] = [
        ("new", [pointer_out], ctypes.c_int),
        ("address", [pointer, ctypes.c_char_p], ctypes.c_int),
        ("client", [pointer, ctypes.c_int], ctypes.c_int),
        ("start", [pointer], ctypes.c_int),
        ("fd", [pointer], ctypes.c_int),
        ("close", [pointer], None),
        ("unref", [pointer], pointer),
        (
            "new_method",
            [
                pointer,
                pointer_out,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
            ],
            ctypes.c_int,
        ),
        ("auto_start", [pointer, ctypes.c_int], ctypes.c_int),
        ("interactive", [pointer, ctypes.c_int], ctypes.c_int),
        ("append", [pointer, ctypes.c_char, pointer], ctypes.c_int),
        ("call", [pointer, pointer, ctypes.c_uint64, error_out, pointer_out], ctypes.c_int),
        ("signature", [pointer, ctypes.c_char_p], ctypes.c_int),
        ("read", [pointer, ctypes.c_char, pointer], ctypes.c_int),
        ("enter", [pointer, ctypes.c_char, ctypes.c_char_p], ctypes.c_int),
        ("exit", [pointer], ctypes.c_int),
        ("at_end", [pointer, ctypes.c_int], ctypes.c_int),
        ("message_unref", [pointer], pointer),
        # v255 man/sd_bus_error.xml declares void, unlike pointer-returning unref.
        ("error_free", [error_out], None),
    ]
    for name, arguments, result in declarations:
        function = cast(peer._Function, getattr(api, name))
        assert function.argtypes == arguments, name
        assert function.restype is result, name
    assert not library.calls


def test_fixed_peer_reads_actual_scalars_with_one_deadline_and_releases_all(
    library: Library,
) -> None:
    result = peer.read_manager_peer(42, 23145, 105.0)
    assert asdict(result) == {
        "code": "observed",
        "reason": "none",
        "basic_state": "active",
        "njobs": 7,
    }
    assert list(library.messages.values()) == [
        (b"GetUnit", [b"basic.target"]),
        (b"Get", [b"org.freedesktop.systemd1.Unit", b"ActiveState"]),
        (b"Get", [b"org.freedesktop.systemd1.Manager", b"NJobs"]),
    ]
    assert library.timeouts == [5_000_000, 4_750_000, 4_500_000]
    assert library.socket_calls == library.peer_calls == 2
    assert sorted(library.freed) == [101, 102, 103, 1101, 1102, 1103]
    names = [name for name, _ in library.calls]
    assert names.count("sd_bus_new") == names.count("sd_bus_start") == 1
    assert names.count("sd_bus_close") == names.count("sd_bus_unref") == 1
    assert names.count("sd_bus_error_free") == 3
    assert [args[1] for name, args in library.calls if name == "sd_bus_set_bus_client"] == [0]
    assert [args[1] for name, args in library.calls if name == "sd_bus_set_address"] == [
        b"unix:path=/run/user/23145/systemd/private"
    ]
    for flag in (
        "sd_bus_message_set_auto_start",
        "sd_bus_message_set_allow_interactive_authorization",
    ):
        assert [args[1] for name, args in library.calls if name == flag] == [0, 0, 0]
    assert len(json.dumps(asdict(result)).encode()) <= peer.CONTEXT_REPLY_BYTES


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("signature", "invalid_reply"),
        ("variant", "invalid_reply"),
        ("trailing", "invalid_reply"),
        ("exit-container", "invalid_reply"),
        ("missing-value", "invalid_reply"),
        ("null-string", "invalid_reply"),
        ("long-string", "invalid_reply"),
        ("unknown-state", "invalid_reply"),
        ("wrong-pid", "identity_changed"),
        ("wrong-uid", "identity_changed"),
        ("peer-drift", "identity_changed"),
        ("socket-drift", "identity_changed"),
        ("last-call-expired", "timeout"),
    ],
)
def test_rejects_bad_reply_or_identity_without_partial_facts(
    library: Library, mode: str, reason: str
) -> None:
    library.mode = mode
    result = peer.read_manager_peer(42, 23145, 105.0)
    assert asdict(result) == {
        "code": "not_observed",
        "reason": reason,
        "basic_state": None,
        "njobs": None,
    }
    assert "secret" not in json.dumps(asdict(result))
    assert sorted(library.freed) == sorted(list(library.messages) + list(library.reply_for))
    assert [name for name, _ in library.calls].count("sd_bus_close") == 1
    if mode in {"wrong-pid", "wrong-uid"}:
        assert not library.messages


@pytest.mark.parametrize(
    "function",
    [
        "sd_bus_new",
        "sd_bus_set_address",
        "sd_bus_set_bus_client",
        "sd_bus_start",
        "sd_bus_get_fd",
        "sd_bus_message_new_method_call",
        "sd_bus_message_set_auto_start",
        "sd_bus_message_set_allow_interactive_authorization",
        "sd_bus_message_append_basic",
        "sd_bus_call",
    ],
)
def test_native_error_is_fixed_and_owned_resources_are_released(
    library: Library, function: str
) -> None:
    library.fail = function
    result = peer.read_manager_peer(42, 23145, 105.0)
    assert result.code == "not_observed" and result.reason == "unavailable"
    assert result.basic_state is None and result.njobs is None
    names = [name for name, _ in library.calls]
    assert names.count("sd_bus_start") <= 1
    inside_request = function in {
        "sd_bus_message_new_method_call",
        "sd_bus_message_set_auto_start",
        "sd_bus_message_set_allow_interactive_authorization",
        "sd_bus_message_append_basic",
        "sd_bus_call",
    }
    assert names.count("sd_bus_error_free") == int(inside_request)
    if function == "sd_bus_call":
        assert 999 in library.freed
    if function != "sd_bus_new":
        assert names.count("sd_bus_close") == names.count("sd_bus_unref") == 1


def test_timeout_or_missing_library_never_falls_back(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    library.errno = errno.ETIMEDOUT
    library.fail = "sd_bus_call"
    assert peer.read_manager_peer(42, 23145, 105.0).reason == "timeout"
    library.calls.clear()
    library.clock[0] = 105.0
    assert peer.read_manager_peer(42, 23145, 105.0).reason == "timeout"
    assert not library.calls

    def missing() -> peer._Library:
        raise OSError("secret library location")

    library.clock[0] = 100.0
    monkeypatch.setattr(peer, "_load_library", missing)
    assert asdict(peer.read_manager_peer(42, 23145, 105.0)) == {
        "code": "not_observed",
        "reason": "unavailable",
        "basic_state": None,
        "njobs": None,
    }


def test_borrowed_strings_are_copied_only_through_the_first_nul() -> None:
    value = ctypes.create_string_buffer(b"active\x00secret")
    pointer = ctypes.c_void_p(ctypes.addressof(value))
    assert peer._bounded_ascii(pointer, len(b"active")) == b"active"
    with pytest.raises(peer.NativeError, match="^native_diagnostic_invalid$"):
        peer._bounded_ascii(pointer, len(b"active") - 1)


def test_peer_credentials_close_only_the_fromfd_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int]] = []
    closed: list[bool] = []

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *arguments: object) -> None:
            closed.append(True)

        def getsockopt(self, level: int, option: int, size: int) -> bytes:
            assert (level, option, size) == (
                peer.socket.SOL_SOCKET,
                peer.socket.SO_PEERCRED,
                peer.struct.calcsize("3i"),
            )
            return peer.struct.pack("3i", 42, 23145, 999)

    def duplicate(descriptor: int, family: int, kind: int) -> Connection:
        calls.append((descriptor, family, kind))
        return Connection()

    def forbidden_close(descriptor: int) -> None:
        raise AssertionError("borrowed bus FD must not be closed")

    monkeypatch.setattr(peer.socket, "fromfd", duplicate)
    monkeypatch.setattr(peer.os, "close", forbidden_close)
    assert peer._peer_identity(123) == (42, 23145)
    assert calls == [(123, peer.socket.AF_UNIX, peer.socket.SOCK_STREAM)]
    assert closed == [True]
