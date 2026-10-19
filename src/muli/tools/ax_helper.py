"""Bounded macOS AX JSON-lines helper. Importing this module never loads AX.

Only this process owns native references. Its random handles cannot recreate an
element after process exit, and no PID/index/name fallback is attempted.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import math
import os
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, BinaryIO

MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 250000
MAX_RESPONSE_CHARS = 60000
MIN_RESPONSE_CHARS = 512
MAX_LIFETIME_BYTES = 1000000
MAX_REQUESTS = 16
MAX_NODES = 1000
MAX_DEPTH = 12
MAX_CHILDREN = 100
MAX_ACTIONS = 20
MAX_STRING_UNITS = 4096
UTF8_ENCODING = 0x08000100
ATTRIBUTE_UNSUPPORTED = -25205
NO_VALUE = -25212
SECURE_SUBROLE = "AXSecureTextField"
TEXT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox"})
ACTION_ROLES = frozenset(
    {
        "AXButton",
        "AXRadioButton",
        "AXCheckBox",
        "AXPopUpButton",
        "AXMenuButton",
        "AXMenuItem",
        "AXMenuBarItem",
        "AXWindow",
        "AXSheet",
        "AXDrawer",
        "AXDisclosureTriangle",
        "AXRow",
        "AXDockItem",
    }
)
COMMANDS = frozenset({"probe", "targets", "snapshot", "action", "type", "release"})


class HelperError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        outcome: str = "not_started",
        native_code: int | None = None,
        fatal: bool = False,
    ) -> None:
        super().__init__(message)
        self.code, self.message, self.outcome, self.native_code = (
            code,
            message,
            outcome,
            native_code,
        )
        self.fatal = fatal

    def result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "outcome": self.outcome,
        }
        if self.native_code is not None:
            result["native_code"] = self.native_code
        return result


class CFRange(ctypes.Structure):
    _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]


P = ctypes.c_void_p
CFIndex = ctypes.c_long
T = ctypes.c_ulong
B = ctypes.c_ubyte
E = ctypes.c_int32
U = ctypes.c_uint16


def _bind(library: Any, name: str, arguments: list[Any], result: Any) -> Any:
    function = getattr(library, name)
    function.argtypes, function.restype = arguments, result
    return function


class Native:
    """Small typed C boundary, injectable with fake libraries for offline tests."""

    def __init__(self, *, libraries: tuple[Any, Any] | None = None) -> None:
        if libraries is None:
            if sys.platform != "darwin":
                raise HelperError("backend_unavailable", "AX 后端仅适用于 macOS")
            try:
                ax_path = ctypes.util.find_library("ApplicationServices")
                cf_path = ctypes.util.find_library("CoreFoundation")
                if not ax_path or not cf_path:
                    raise OSError("framework missing")
                libraries = (ctypes.CDLL(ax_path), ctypes.CDLL(cf_path))
            except OSError as exc:
                raise HelperError("dependency_missing", "无法加载系统 AX 框架") from exc
        ax, cf = libraries
        try:
            self.ax = {
                name: _bind(ax, name, args, result)
                for name, args, result in (
                    ("AXIsProcessTrustedWithOptions", [P], B),
                    ("AXUIElementCreateSystemWide", [], P),
                    ("AXUIElementGetTypeID", [], T),
                    ("AXUIElementGetPid", [P, ctypes.POINTER(E)], E),
                    ("AXUIElementCopyAttributeValue", [P, P, ctypes.POINTER(P)], E),
                    ("AXUIElementGetAttributeValueCount", [P, P, ctypes.POINTER(CFIndex)], E),
                    (
                        "AXUIElementCopyAttributeValues",
                        [P, P, CFIndex, CFIndex, ctypes.POINTER(P)],
                        E,
                    ),
                    ("AXUIElementCopyActionNames", [P, ctypes.POINTER(P)], E),
                    ("AXUIElementIsAttributeSettable", [P, P, ctypes.POINTER(B)], E),
                    ("AXUIElementPerformAction", [P, P], E),
                    ("AXUIElementSetAttributeValue", [P, P, P], E),
                    ("AXUIElementSetMessagingTimeout", [P, ctypes.c_float], E),
                )
            }
            self.cf = {
                name: _bind(cf, name, args, result)
                for name, args, result in (
                    ("CFGetTypeID", [P], T),
                    ("CFStringGetTypeID", [], T),
                    ("CFArrayGetTypeID", [], T),
                    ("CFBooleanGetTypeID", [], T),
                    ("CFRetain", [P], P),
                    ("CFRelease", [P], None),
                    ("CFEqual", [P, P], B),
                    ("CFHash", [P], T),
                    ("CFArrayGetCount", [P], CFIndex),
                    ("CFArrayGetValueAtIndex", [P, CFIndex], P),
                    ("CFBooleanGetValue", [P], B),
                    ("CFStringGetLength", [P], CFIndex),
                    ("CFStringGetCharacters", [P, CFRange, ctypes.POINTER(U)], None),
                    (
                        "CFStringCreateWithBytes",
                        [P, ctypes.POINTER(B), CFIndex, ctypes.c_uint32, B],
                        P,
                    ),
                )
            }
        except AttributeError as exc:
            raise HelperError("dependency_missing", "系统 AX 框架缺少必要符号") from exc
        self.constants: dict[str, int] = {}
        self.system = 0
        self.deadline = math.inf
        self.string_units = MAX_STRING_UNITS

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise HelperError("read_timeout", "AX 操作已超过期限")

    def begin(self, timeout: float, response_chars: int) -> None:
        self.deadline = time.monotonic() + timeout
        self.string_units = min(MAX_STRING_UNITS, response_chars)

    def trusted(self) -> bool:
        self.check()
        return bool(self.ax["AXIsProcessTrustedWithOptions"](None))

    @staticmethod
    def error(code: int) -> None:
        if code:
            category = {
                -25202: "element_unavailable",
                -25204: "read_timeout",
                -25211: "permission_required",
                -25208: "unsupported_operation",
            }.get(code, "protocol_error")
            raise HelperError(category, "无法读取当前 AX 对象", native_code=code)

    def _prepare(self) -> None:
        self.check()
        if self.system:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise HelperError("read_timeout", "AX 操作已超过期限")
            self.error(self.ax["AXUIElementSetMessagingTimeout"](self.system, remaining))
            self.check()

    def _call(self, name: str, *arguments: Any) -> int:
        self._prepare()
        return int(self.ax[name](*arguments))

    def release(self, pointer: int) -> None:
        if pointer:
            self.cf["CFRelease"](pointer)

    def retain(self, pointer: int) -> int:
        return int(self.cf["CFRetain"](pointer))

    def is_type(self, pointer: int, kind: str) -> bool:
        return bool(pointer) and self.cf["CFGetTypeID"](pointer) == (
            self.ax["AXUIElementGetTypeID"]() if kind == "ax" else self.cf[f"CF{kind}GetTypeID"]()
        )

    def systemwide(self) -> int:
        self.check()
        if not self.system:
            pointer = self.ax["AXUIElementCreateSystemWide"]()
            if not pointer:
                raise HelperError("no_session", "无法取得 AX 系统对象")
            if not self.is_type(pointer, "ax"):
                self.release(pointer)
                raise HelperError("protocol_error", "AX 系统对象类型无效")
            self.system = pointer
        return self.system

    def create_string(self, value: str) -> int:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise HelperError("protocol_error", "文本不是有效 Unicode") from exc
        data = (B * len(encoded)).from_buffer_copy(encoded)
        pointer = self.cf["CFStringCreateWithBytes"](None, data, len(encoded), UTF8_ENCODING, False)
        if not pointer:
            raise HelperError("protocol_error", "无法构造 AX 文本")
        return int(pointer)

    def constant(self, name: str) -> int:
        if name not in self.constants:
            self.constants[name] = self.create_string(name)
        return self.constants[name]

    def string(self, pointer: int) -> str:
        if not self.is_type(pointer, "String"):
            raise HelperError("protocol_error", "AX 字符串属性类型无效")
        length = self.cf["CFStringGetLength"](pointer)
        if not 0 <= length <= self.string_units:
            raise HelperError("output_budget", "AX 字符串超过响应预算")
        if not length:
            return ""
        buffer = (U * length)()
        self.cf["CFStringGetCharacters"](pointer, CFRange(0, length), buffer)
        try:
            return bytes(buffer).decode("utf-16-le" if sys.byteorder == "little" else "utf-16-be")
        except UnicodeError as exc:
            raise HelperError("protocol_error", "AX 字符串编码无效") from exc

    def copy(self, pointer: int, attribute: str) -> int | None:
        output = P()
        code = self._call(
            "AXUIElementCopyAttributeValue", pointer, self.constant(attribute), ctypes.byref(output)
        )
        if code and output.value:
            # A failed Copy has no documented owned result. Do not dereference or
            # CFRelease an unpromised pointer; retire this malformed session.
            raise HelperError("protocol_error", "失败的 AX 属性读取返回了对象", fatal=True)
        if code in {ATTRIBUTE_UNSUPPORTED, NO_VALUE}:
            return None
        self.error(code)
        if not output.value:
            raise HelperError("protocol_error", "AX 属性没有返回有效对象")
        return output.value

    def scalar(self, pointer: int, attribute: str, kind: str) -> str | bool | None:
        value = self.copy(pointer, attribute)
        if value is None:
            return None
        try:
            if kind == "String":
                return self.string(value)
            if not self.is_type(value, "Boolean"):
                raise HelperError("protocol_error", "AX 布尔属性类型无效")
            return bool(self.cf["CFBooleanGetValue"](value))
        finally:
            self.release(value)

    def pid(self, pointer: int) -> int:
        output = E()
        self.error(self._call("AXUIElementGetPid", pointer, ctypes.byref(output)))
        if output.value < 0:
            raise HelperError("protocol_error", "AX PID 无效")
        return output.value

    def count(self, pointer: int, attribute: str) -> int | None:
        output = CFIndex()
        code = self._call(
            "AXUIElementGetAttributeValueCount",
            pointer,
            self.constant(attribute),
            ctypes.byref(output),
        )
        if code in {ATTRIBUTE_UNSUPPORTED, NO_VALUE}:
            return None
        self.error(code)
        if output.value < 0:
            raise HelperError("protocol_error", "AX 子节点数量无效")
        return output.value

    def children(self, pointer: int, attribute: str, count: int) -> list[int | None]:
        output = P()
        code = self._call(
            "AXUIElementCopyAttributeValues",
            pointer,
            self.constant(attribute),
            0,
            count,
            ctypes.byref(output),
        )
        if code and output.value:
            raise HelperError("protocol_error", "失败的 AX 子节点读取返回了对象", fatal=True)
        self.error(code)
        array = output.value or 0
        retained: list[int | None] = []
        try:
            if not self.is_type(array, "Array"):
                raise HelperError("protocol_error", "AX 子节点数组类型无效")
            actual = self.cf["CFArrayGetCount"](array)
            if not 0 <= actual <= count:
                raise HelperError("protocol_error", "AX 子节点数组超过请求范围")
            for index in range(actual):
                child = self.cf["CFArrayGetValueAtIndex"](array, index)
                retained.append(self.retain(child) if self.is_type(child, "ax") else None)
            return retained
        except BaseException:
            for child in retained:
                self.release(child or 0)
            raise
        finally:
            self.release(array)

    def actions(self, pointer: int) -> list[str]:
        output = P()
        code = self._call("AXUIElementCopyActionNames", pointer, ctypes.byref(output))
        if code and output.value:
            raise HelperError("protocol_error", "失败的 AX 动作读取返回了对象", fatal=True)
        if code in {ATTRIBUTE_UNSUPPORTED, NO_VALUE, -25206}:
            return []
        self.error(code)
        array = output.value or 0
        try:
            if not self.is_type(array, "Array"):
                raise HelperError("protocol_error", "AX 动作数组类型无效")
            count = self.cf["CFArrayGetCount"](array)
            if not 0 <= count <= MAX_ACTIONS:
                raise HelperError("output_budget", "AX 动作列表超过预算")
            names = [
                self.string(self.cf["CFArrayGetValueAtIndex"](array, index))
                for index in range(count)
            ]
            if any(not name for name in names):
                raise HelperError("protocol_error", "AX 动作名称为空")
            return names
        finally:
            self.release(array)

    def settable(self, pointer: int) -> bool | None:
        output = B()
        code = self._call(
            "AXUIElementIsAttributeSettable",
            pointer,
            self.constant("AXValue"),
            ctypes.byref(output),
        )
        if code in {ATTRIBUTE_UNSUPPORTED, NO_VALUE}:
            return None
        self.error(code)
        return bool(output.value)

    def perform(self, pointer: int, command: str, value: str) -> dict[str, Any]:
        argument = self.create_string(value)
        try:
            attribute = self.constant("AXValue") if command == "type" else 0
            self._prepare()  # A deadline failure here is still before native mutation.
            try:
                code = (
                    self.ax["AXUIElementSetAttributeValue"](pointer, attribute, argument)
                    if command == "type"
                    else self.ax["AXUIElementPerformAction"](pointer, argument)
                )
            except Exception as exc:
                raise HelperError(
                    "action_unknown_outcome", "AX 已调用，结果未知；请先核查界面", outcome="unknown"
                ) from exc
            if code:
                raise HelperError(
                    "action_unknown_outcome",
                    "AX 未确认动作结果；勿自动重试",
                    outcome="unknown",
                    native_code=int(code),
                )
            return {"ok": True, "outcome": "accepted", "effect_verified": False}
        finally:
            self.release(argument)

    def close(self) -> None:
        self.release(self.system)
        self.system = 0
        for pointer in self.constants.values():
            self.release(pointer)
        self.constants.clear()


@dataclass(frozen=True)
class Limits:
    timeout_seconds: float
    max_nodes: int
    max_response_chars: int


def validate(request: Any) -> tuple[str, str, dict[str, Any], Limits]:
    if not isinstance(request, dict) or set(request) != {
        "v",
        "request_id",
        "command",
        "payload",
        "limits",
    }:
        raise HelperError("protocol_error", "AX 请求外层格式无效")
    identifier, command, payload, limits = (
        request[key] for key in ("request_id", "command", "payload", "limits")
    )
    if type(request["v"]) is not int or request["v"] != 1 or not valid_id(identifier):
        raise HelperError("protocol_error", "AX 协议版本或请求 ID 无效")
    if not isinstance(command, str) or command not in COMMANDS or not isinstance(payload, dict):
        raise HelperError("protocol_error", "AX 命令格式无效")
    if not isinstance(limits, dict) or set(limits) != {
        "timeout_seconds",
        "max_nodes",
        "max_response_chars",
    }:
        raise HelperError("protocol_error", "AX 预算格式无效")
    timeout, nodes, chars = (
        limits[key] for key in ("timeout_seconds", "max_nodes", "max_response_chars")
    )
    if (
        type(timeout) not in {int, float}
        or not math.isfinite(timeout)
        or not 0 < timeout <= 120
        or type(nodes) is not int
        or not 1 <= nodes <= MAX_NODES
        or type(chars) is not int
        or not MIN_RESPONSE_CHARS <= chars <= MAX_RESPONSE_CHARS
    ):
        raise HelperError("protocol_error", "AX 预算必须为有限范围的数值")
    allowed = {
        "probe": set(),
        "release": set(),
        "targets": {"application_ref_id"},
        "snapshot": {"target_ref_id"},
        "action": {"ref_id", "action"},
        "type": {"ref_id", "text"},
    }[command]
    required = allowed if command in {"action", "type"} else set()
    if not required <= payload.keys() <= allowed or any(
        not isinstance(value, str) for value in payload.values()
    ):
        raise HelperError("protocol_error", "AX 命令参数无效")
    return identifier, command, payload, Limits(float(timeout), nodes, chars)


def valid_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value.isascii()
        and all(character.isalnum() or character in "_-" for character in value)
    )


@dataclass
class Observation:
    key: str
    scope: str
    maximum: int
    visited: int = 0
    records: list[dict[str, Any]] = field(default_factory=list)
    omissions: dict[str, int | None] = field(default_factory=dict)
    seen: dict[int, list[int]] = field(default_factory=dict)

    def omit(self, reason: str, count: int | None = None) -> None:
        previous = self.omissions.get(reason, 0)
        self.omissions[reason] = None if count is None or previous is None else previous + count

    def result(self) -> dict[str, Any]:
        return {
            self.key: self.records,
            "scope": self.scope,
            "visited": self.visited,
            "complete": not self.omissions,
            "truncated": bool(self.omissions),
            "omissions": [
                {"reason": reason, "omitted_direct_children": count}
                for reason, count in self.omissions.items()
            ],
            "omission_reasons": sorted(self.omissions),
            "omissions_truncated": False,
        }


@dataclass
class Reference:
    pointer: int
    record: dict[str, Any]


class Session:
    def __init__(self, factory: Callable[[], Native] = Native) -> None:
        self.factory = factory
        self.api: Native | None = None
        self.refs: dict[str, Reference] = {}

    def native(self) -> Native:
        if self.api is None:
            self.api = self.factory()
        return self.api

    def clear(self) -> None:
        if self.api:
            for reference in self.refs.values():
                self.api.release(reference.pointer)
        self.refs.clear()

    def close(self) -> None:
        self.clear()
        if self.api:
            self.api.close()
            self.api = None

    def record(
        self,
        pointer: int,
        kind: str,
        observation: Observation | None = None,
        *,
        strict: bool = False,
    ) -> dict[str, Any]:
        api = self.native()
        metadata_valid = True

        def optional(attribute: str, value_kind: str = "Boolean") -> Any:
            nonlocal metadata_valid
            try:
                return api.scalar(pointer, attribute, value_kind)
            except HelperError as exc:
                if (
                    strict
                    or exc.fatal
                    or exc.code in {"output_budget", "read_timeout", "permission_required"}
                ):
                    raise
                metadata_valid = False
                if observation:
                    observation.omit("provider_error")
                return None

        role = api.scalar(pointer, "AXRole", "String")
        if not isinstance(role, str) or not role:
            raise HelperError("protocol_error", "AX 角色不可验证")
        if (kind == "application" and role != "AXApplication") or (
            kind == "window" and role != "AXWindow"
        ):
            raise HelperError("protocol_error", "AX 目标角色与声明范围不符")
        if kind == "node" and role in {"AXApplication", "AXWindow"}:
            kind = "application" if role == "AXApplication" else "window"
        subrole = optional("AXSubrole", "String")
        protected = (
            subrole == SECURE_SUBROLE
            if isinstance(subrole, str) and subrole and subrole != "AXUnknown"
            else None
        )
        text = role in TEXT_ROLES
        safe = protected is not True and (role in ACTION_ROLES or (text and protected is False))
        name = (
            "受保护文本"
            if protected
            else "文本控件"
            if text and protected is None
            else optional("AXTitle", "String") or optional("AXDescription", "String") or ""
        )
        enabled = optional("AXEnabled")
        settable: bool | None = None
        if text and protected is False and enabled is True:
            try:
                settable = api.settable(pointer)
            except HelperError as exc:
                if strict or exc.fatal or exc.code in {"read_timeout", "permission_required"}:
                    raise
                metadata_valid = False
                if observation:
                    observation.omit("provider_error")
        focused = optional("AXFocused")
        active = optional("AXFrontmost") if role == "AXApplication" else None
        return {
            "kind": kind,
            "name": name,
            "role": role,
            "subrole": subrole,
            "pid": api.pid(pointer),
            "actions": api.actions(pointer)
            if safe and metadata_valid and enabled is True and kind != "application"
            else [],
            "editable": metadata_valid
            and text
            and protected is False
            and enabled is True
            and settable is True,
            "enabled": enabled,
            "sensitive": None,
            "defunct": None,
            "protected": protected,
            "focused": focused,
            "active": active,
            "read_only": not settable if settable is not None else None,
        }

    def observe(self, command: str, payload: dict[str, Any], limits: Limits) -> dict[str, Any]:
        api = self.native()
        key = "application_ref_id" if command == "targets" else "target_ref_id"
        selected = self.refs.get(payload.get(key, "")) if key in payload else None
        root = 0
        kind = "application"
        expected: dict[str, Any] | None = None
        queue: deque[tuple[int | None, str | None, int, str]] = deque()
        observation: Observation | None = None
        try:
            if key in payload:
                if selected is None or selected.record["kind"] not in (
                    {"application"} if command == "targets" else {"application", "window"}
                ):
                    raise HelperError("stale_node", "AX 目标不在当前引用组中")
                root, kind, expected = (
                    api.retain(selected.pointer),
                    selected.record["kind"],
                    selected.record,
                )
            self.clear()
            observation = Observation(
                "targets" if command == "targets" else "nodes",
                "application_windows"
                if command == "targets" and selected
                else kind
                if selected
                else "foreground_application",
                limits.max_nodes,
            )
            if root:
                queue.append((root, None, 0, kind))
                root = 0  # The queue now owns this retain.
            else:
                system = api.systemwide()
                observation.visited = 1
                if limits.max_nodes <= 1:
                    observation.omit("node_budget")
                    return observation.result()
                root = api.copy(system, "AXFocusedApplication") or 0
                if not root:
                    raise HelperError("no_session", "当前没有可读取的前台应用")
                if not api.is_type(root, "ax"):
                    raise HelperError("protocol_error", "前台应用不是 AX 对象")
                queue.append((root, None, 0, kind))
                root = 0
            while queue:
                api.check()
                pointer, parent_id, depth, kind = queue.popleft()
                observation.visited += 1
                if not pointer:
                    observation.omit("provider_error", 1)
                    continue
                owned = True
                try:
                    hashed = int(api.cf["CFHash"](pointer))
                    bucket = observation.seen.setdefault(hashed, [])
                    if any(api.cf["CFEqual"](pointer, other) for other in bucket):
                        observation.omit("provider_error")
                        continue
                    # Even an omitted record can be compared with a later
                    # duplicate. Keep its CF reference until traversal ends.
                    bucket.append(api.retain(pointer))
                    record: dict[str, Any] | None = None
                    try:
                        record = self.record(pointer, kind, observation)
                        if expected is not None:
                            if any(
                                record[field] != expected[field]
                                for field in ("pid", "role", "name")
                            ):
                                raise HelperError("target_changed", "AX 原目标元数据已改变")
                            expected = None
                        identifier = uuid.uuid4().hex
                        record.update(
                            ref_id=identifier,
                            parent_ref_id=parent_id,
                            depth=depth,
                            parent_observed=depth == 0 or parent_id is not None,
                        )
                        observation.records.append(record)
                        self.refs[identifier] = Reference(pointer, record)
                        owned = False
                    except HelperError as exc:
                        if (
                            expected is not None
                            or exc.fatal
                            or exc.code in {"read_timeout", "permission_required", "target_changed"}
                            or (depth == 0 and exc.code != "output_budget")
                        ):
                            raise
                        observation.omit(
                            "output_budget" if exc.code == "output_budget" else "provider_error"
                        )
                    if record and record["protected"] is True:
                        observation.omit(
                            "provider_error"
                        )  # Protected descendants are intentionally unread.
                        continue
                    if command == "targets" and not selected:
                        continue  # Foreground discovery promises one application, not its descendants.
                    if command == "targets" and depth:
                        continue
                    attribute = "AXWindows" if command == "targets" else "AXChildren"
                    try:
                        count = api.count(pointer, attribute)
                        if count is None:
                            if attribute == "AXWindows":
                                observation.omit("provider_error")
                            continue
                        if depth >= MAX_DEPTH:
                            if count:
                                observation.omit("depth_limit", count)
                            continue
                        allowed = min(count, MAX_CHILDREN)
                        if count > allowed:
                            observation.omit("children_limit", count - allowed)
                        capacity = max(0, limits.max_nodes - observation.visited - len(queue))
                        wanted = min(allowed, capacity)
                        if wanted < allowed:
                            observation.omit("node_budget", allowed - wanted)
                        if not wanted:
                            continue
                        children = api.children(pointer, attribute, wanted)
                        if len(children) < wanted:
                            observation.omit("provider_error", wanted - len(children))
                        queue.extend(
                            (
                                child,
                                record["ref_id"] if record and not owned else None,
                                depth + 1,
                                "window" if command == "targets" else "node",
                            )
                            for child in children
                        )
                    except HelperError as exc:
                        if exc.fatal or exc.code in {"read_timeout", "permission_required"}:
                            raise
                        observation.omit("provider_error")
                finally:
                    if owned:
                        api.release(pointer)
            return observation.result()
        except BaseException:
            self.clear()
            raise
        finally:
            api.release(root)
            for pointer, _, _, _ in queue:
                api.release(pointer or 0)
            if observation:
                for bucket in observation.seen.values():
                    for pointer in bucket:
                        api.release(pointer)

    def perform(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            reference = self.refs.get(payload["ref_id"])
            if reference is None:
                raise HelperError("stale_node", "AX 节点不在当前引用组中")
            api = self.native()
            fresh = self.record(reference.pointer, reference.record["kind"], strict=True)
            if any(fresh[key] != reference.record[key] for key in ("pid", "role", "name")):
                raise HelperError("target_changed", "AX 原节点已改变")
            if fresh["protected"] is True or (
                fresh["role"] not in ACTION_ROLES and fresh["protected"] is not False
            ):
                raise HelperError("protected_element", "不能操作保护状态未知或受保护的文本")
            if fresh["enabled"] is not True:
                raise HelperError("element_disabled", "AX 节点不可操作或状态未知")
            if command == "type":
                if not fresh["editable"]:
                    raise HelperError("unsupported_operation", "AX 节点当前不可设置文本")
                value = payload["text"]
            else:
                value = payload["action"]
                if value not in fresh["actions"]:
                    raise HelperError("unsupported_operation", "AX 节点没有该具名动作")
            return api.perform(reference.pointer, command, value)
        finally:
            self.clear()

    def respond(self, request: Any, *, once: bool = False) -> dict[str, Any]:
        identifier = request.get("request_id") if isinstance(request, dict) else None
        if not valid_id(identifier) or type(request.get("v")) is not int or request["v"] != 1:
            raise HelperError("protocol_error", "AX 请求没有合法 ID")
        envelope: dict[str, Any] = {"v": 1, "request_id": identifier}
        budget = MIN_RESPONSE_CHARS
        command: str | None = None
        try:
            _, command, payload, limits = validate(request)
            budget = limits.max_response_chars
            if once != (command == "probe"):
                raise HelperError("protocol_error", "AX 启动模式与命令不匹配")
            if command == "release":
                self.clear()
                envelope["result"] = {"released": True}
            else:
                api = self.native()
                api.begin(limits.timeout_seconds, limits.max_response_chars)
                trusted = api.trusted()
                if command == "probe":
                    envelope["result"] = {
                        "backend": "ax",
                        "dependency_available": True,
                        "permission": {
                            "trusted": trusted,
                            "subject": "helper_process",
                            "prompted": False,
                        },
                        "session_accessible": None,
                        "applications": None,
                        "discovery_scope": "foreground_application",
                        "supported_operations": [
                            "targets",
                            "snapshot",
                            "action",
                            "type",
                            "release",
                        ],
                        "helper_pid": os.getpid(),
                        "executable": sys.executable,
                    }
                else:
                    if not trusted:
                        raise HelperError(
                            "permission_required", "当前 Python helper 尚未获得辅助功能信任"
                        )
                    envelope["result"] = (
                        self.observe(command, payload, limits)
                        if command in {"targets", "snapshot"}
                        else self.perform(command, payload)
                    )
        except HelperError as exc:
            self.clear()
            envelope["error"] = (
                HelperError("element_unavailable", "当前预算不足以完整验证原 AX 节点").result()
                if exc.code == "output_budget"
                else exc.result()
            )
        except Exception:
            self.clear()
            envelope["error"] = HelperError("protocol_error", "无法读取 AX 响应").result()
        self.fit(envelope, budget)
        return envelope

    def fit(self, envelope: dict[str, Any], budget: int) -> None:
        def fits() -> bool:
            text = json.dumps(envelope, ensure_ascii=False)
            return len(text) + 1 <= budget and len(text.encode("utf-8")) + 1 <= MAX_RESPONSE_BYTES

        result = envelope.get("result", {})
        key = "nodes" if "nodes" in result else "targets" if "targets" in result else None
        if key:
            records = result[key]
            while not fits() and records:
                removed = records.pop()
                self.native().release(self.refs.pop(removed["ref_id"]).pointer)
                result.update(complete=False, truncated=True)
                if "output_budget" not in result["omission_reasons"]:
                    result["omission_reasons"].append("output_budget")
                    result["omission_reasons"].sort()
                    result["omissions"].append(
                        {"reason": "output_budget", "omitted_direct_children": None}
                    )
            if not fits() and result["omissions"]:
                result["omissions"] = []
                result["omissions_truncated"] = True
        if not fits():
            self.clear()
            envelope.pop("result", None)
            envelope["error"] = HelperError("protocol_error", "AX 响应元数据超过预算").result()
        if not fits():
            raise HelperError("protocol_error", "AX 必要响应超过预算")


def serve(
    mode: str,
    source: BinaryIO,
    output: BinaryIO,
    errors: BinaryIO,
    *,
    factory: Callable[[], Native] = Native,
) -> int:
    session = Session(factory)
    written = 0
    try:
        for _ in range(MAX_REQUESTS):
            line = source.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                return 0
            if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                raise HelperError("protocol_error", "AX 输入帧无效")
            try:
                request = json.loads(line.decode("utf-8", errors="strict"))
            except (UnicodeError, ValueError) as exc:
                raise HelperError("protocol_error", "AX 输入帧无效") from exc
            response = session.respond(request, once=mode == "--once")
            encoded = (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
            written += len(encoded)
            if written > MAX_LIFETIME_BYTES:
                raise HelperError("protocol_error", "AX 会话输出超过预算")
            output.write(encoded)
            output.flush()
            if (
                mode == "--once"
                or request.get("command") in {"action", "type"}
                or response.get("error", {}).get("code") == "protocol_error"
            ):
                return 1 if "error" in response else 0
        return 0
    except (HelperError, OSError, ValueError):
        errors.write(b"AX helper protocol failure\n")
        errors.flush()
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"--once", "--session"}:
        sys.stderr.write("AX helper requires --once or --session\n")
        sys.exit(2)
    sys.exit(serve(sys.argv[1], sys.stdin.buffer, sys.stdout.buffer, sys.stderr.buffer))
