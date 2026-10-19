"""Offline C-boundary fixtures. No test observes or mutates a real AX desktop."""

from __future__ import annotations

import ctypes
import io
import json
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from muli.tools import ax_helper as helper

AX, STRING, ARRAY, BOOLEAN, OTHER = range(1, 6)


@dataclass
class CFObject:
    kind: int
    value: Any
    refs: int = 1


class Function:
    def __init__(self, callback: Callable[..., Any]) -> None:
        self.callback = callback
        self.argtypes: list[Any] | None = None
        self.restype: Any = ctypes.c_int
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.callback(*args)


class CoreFoundation:
    """Arrays own their entries; borrowed pointers die on the last CFRelease."""

    def __init__(self) -> None:
        self.objects: dict[int, CFObject] = {}
        self.next_pointer = 0x1234_0000_0000
        self.events: list[tuple[str, int]] = []
        self.library = SimpleNamespace(
            **{
                name: Function(callback)
                for name, callback in {
                    "CFGetTypeID": lambda pointer: self.object(pointer).kind,
                    "CFStringGetTypeID": lambda: STRING,
                    "CFArrayGetTypeID": lambda: ARRAY,
                    "CFBooleanGetTypeID": lambda: BOOLEAN,
                    "CFRetain": self.retain,
                    "CFRelease": self.release,
                    "CFEqual": self.equal,
                    "CFHash": self.hash,
                    "CFArrayGetCount": lambda pointer: len(self.object(pointer, ARRAY).value),
                    "CFArrayGetValueAtIndex": self.at,
                    "CFBooleanGetValue": lambda pointer: self.object(pointer, BOOLEAN).value,
                    "CFStringGetLength": lambda pointer: len(self.units(pointer)) // 2,
                    "CFStringGetCharacters": self.characters,
                    "CFStringCreateWithBytes": self.create_string,
                }.items()
            }
        )

    def allocate(self, kind: int, value: Any) -> int:
        pointer = self.next_pointer
        self.next_pointer += 16
        self.objects[pointer] = CFObject(kind, value)
        return pointer

    def object(self, pointer: int, kind: int | None = None) -> CFObject:
        obj = self.objects[pointer]
        assert obj.refs > 0, "use after CFRelease"
        assert kind is None or obj.kind == kind, "wrong CF type access"
        return obj

    def retain(self, pointer: int) -> int:
        self.object(pointer).refs += 1
        self.events.append(("retain", pointer))
        return pointer

    def release(self, pointer: int) -> None:
        assert pointer, "NULL CFRelease"
        obj = self.object(pointer)
        obj.refs -= 1
        self.events.append(("release", pointer))
        if not obj.refs and obj.kind == ARRAY:
            for value in obj.value:
                if value:
                    self.release(value)

    def equal(self, left: int, right: int) -> bool:
        a, b = self.object(left), self.object(right)
        return a.kind == b.kind and (a.value is b.value if a.kind == AX else a.value == b.value)

    def hash(self, pointer: int) -> int:
        obj = self.object(pointer)
        return 1 if obj.kind == AX else hash(str(obj.value)) % (2**32)

    def at(self, pointer: int, index: int) -> int:
        values = self.object(pointer, ARRAY).value
        assert 0 <= index < len(values)
        return int(values[index])

    def units(self, pointer: int) -> bytes:
        return self.object(pointer, STRING).value.encode("utf-16-le", errors="surrogatepass")

    def characters(self, pointer: int, span: helper.CFRange, output: Any) -> None:
        data = self.units(pointer)[span.location * 2 : (span.location + span.length) * 2]
        if sys.byteorder != "little":
            data = b"".join(data[index : index + 2][::-1] for index in range(0, len(data), 2))
        ctypes.memmove(output, data, len(data))

    def create_string(
        self, allocator: Any, data: Any, count: int, encoding: int, external: bool
    ) -> int:
        assert allocator is None and encoding == helper.UTF8_ENCODING and not external
        value = ctypes.string_at(data, count).decode("utf-8", errors="strict")
        return self.allocate(STRING, value)

    def value(self, value: Any) -> int:
        if isinstance(value, Element):
            return self.allocate(AX, value)
        if type(value) is bool:
            return self.allocate(BOOLEAN, value)
        if isinstance(value, str):
            return self.allocate(STRING, value)
        if isinstance(value, list):
            return self.allocate(
                ARRAY, [self.value(item) if item is not None else 0 for item in value]
            )
        return self.allocate(OTHER, value)

    def empty(self) -> None:
        assert not {pointer: obj for pointer, obj in self.objects.items() if obj.refs}


@dataclass
class Fault:
    code: int
    output: int = 0


@dataclass
class Element:
    role: str = "AXButton"
    name: str = "测试控件"
    pid: int = 42
    attrs: dict[str, Any] = field(default_factory=dict)
    actions: Any = field(default_factory=lambda: ["AXPress"])
    settable: bool | Fault = True
    counts: dict[str, int | Fault] = field(default_factory=dict)
    copies: dict[str, Any] = field(default_factory=dict)
    mutation_code: int = 0
    mutation_exception: bool = False
    mutations: list[tuple[str, str]] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.attrs = {
            "AXRole": self.role,
            "AXTitle": self.name,
            "AXEnabled": True,
            "AXFocused": False,
            "AXFrontmost": True,
            "AXChildren": [],
            "AXWindows": [],
            **self.attrs,
        }


class Accessibility:
    def __init__(self, cf: CoreFoundation, application: Element) -> None:
        self.cf = cf
        self.system = Element("AXSystemWide", attrs={"AXFocusedApplication": application})
        self.trusted = True
        self.timeout_code = 0
        self.out_initial_values: list[int | None] = []
        self.library = SimpleNamespace(
            **{
                name: Function(callback)
                for name, callback in {
                    "AXIsProcessTrustedWithOptions": self.trust,
                    "AXUIElementCreateSystemWide": lambda: self.cf.value(self.system),
                    "AXUIElementGetTypeID": lambda: AX,
                    "AXUIElementGetPid": self.pid,
                    "AXUIElementCopyAttributeValue": self.copy,
                    "AXUIElementGetAttributeValueCount": self.count,
                    "AXUIElementCopyAttributeValues": self.children,
                    "AXUIElementCopyActionNames": self.actions,
                    "AXUIElementIsAttributeSettable": self.settable,
                    "AXUIElementPerformAction": self.action,
                    "AXUIElementSetAttributeValue": self.type,
                    "AXUIElementSetMessagingTimeout": self.timeout,
                }.items()
            }
        )

    def trust(self, options: Any) -> bool:
        assert options is None, "must not request a TCC prompt"
        return self.trusted

    def element(self, pointer: int) -> Element:
        value = self.cf.object(pointer, AX).value
        assert isinstance(value, Element)
        return value

    def attribute(self, pointer: int, name: int) -> tuple[Element, str, Any]:
        node = self.element(pointer)
        attribute = self.cf.object(name, STRING).value
        node.reads.append(attribute)
        return node, attribute, node.attrs.get(attribute, Fault(helper.ATTRIBUTE_UNSUPPORTED))

    def copy_out(self, value: Any, output: Any) -> int:
        target = ctypes.cast(output, ctypes.POINTER(helper.P))
        self.out_initial_values.append(target[0])
        if isinstance(value, Fault):
            target[0] = value.output
            return value.code
        target[0] = self.cf.value(value)
        return 0

    def pid(self, pointer: int, output: Any) -> int:
        node = self.element(pointer)
        node.reads.append("pid")
        ctypes.cast(output, ctypes.POINTER(helper.E))[0] = node.pid
        return 0

    def copy(self, pointer: int, name: int, output: Any) -> int:
        _, _, value = self.attribute(pointer, name)
        return self.copy_out(value, output)

    def count(self, pointer: int, name: int, output: Any) -> int:
        node, attribute, value = self.attribute(pointer, name)
        count = node.counts.get(attribute, value if isinstance(value, Fault) else len(value))
        if isinstance(count, Fault):
            return count.code
        ctypes.cast(output, ctypes.POINTER(helper.CFIndex))[0] = count
        return 0

    def children(self, pointer: int, name: int, start: int, count: int, output: Any) -> int:
        node, attribute, values = self.attribute(pointer, name)
        assert start == 0 and 0 < count <= helper.MAX_CHILDREN
        result = (
            node.copies[attribute]
            if attribute in node.copies
            else values
            if isinstance(values, Fault)
            else values[:count]
        )
        return self.copy_out(result, output)

    def actions(self, pointer: int, output: Any) -> int:
        node = self.element(pointer)
        node.reads.append("actions")
        return self.copy_out(node.actions, output)

    def settable(self, pointer: int, name: int, output: Any) -> int:
        node, attribute, _ = self.attribute(pointer, name)
        assert attribute == "AXValue"
        if isinstance(node.settable, Fault):
            return node.settable.code
        ctypes.cast(output, ctypes.POINTER(helper.B))[0] = node.settable
        return 0

    def mutate(self, pointer: int, command: str, value: int) -> int:
        node = self.element(pointer)
        text = self.cf.object(value, STRING).value
        node.mutations.append((command, text))
        if node.mutation_exception:
            raise RuntimeError("SECRET native failure")
        return node.mutation_code

    def action(self, pointer: int, action: int) -> int:
        return self.mutate(pointer, "action", action)

    def type(self, pointer: int, name: int, value: int) -> int:
        assert self.cf.object(name, STRING).value == "AXValue"
        return self.mutate(pointer, "type", value)

    def timeout(self, pointer: int, seconds: float) -> int:
        assert self.element(pointer) is self.system
        assert seconds > 0
        return self.timeout_code


class World:
    def __init__(self, *children: Element, application: Element | None = None) -> None:
        self.cf = CoreFoundation()
        self.application = application or Element(
            "AXApplication", "测试应用", attrs={"AXChildren": list(children)}
        )
        self.ax = Accessibility(self.cf, self.application)
        self.session = helper.Session(self.native)

    def native(self) -> helper.Native:
        return helper.Native(libraries=(self.ax.library, self.cf.library))

    def request(
        self, command: str, payload: dict[str, Any] | None = None, **limits: Any
    ) -> dict[str, Any]:
        return self.session.respond(request(command, payload, **limits), once=command == "probe")

    def result(
        self, command: str, payload: dict[str, Any] | None = None, **limits: Any
    ) -> dict[str, Any]:
        response = self.request(command, payload, **limits)
        assert "error" not in response, response
        return response["result"]

    def close(self) -> None:
        self.session.close()
        self.cf.empty()


def request(command: str, payload: dict[str, Any] | None = None, **limits: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "request_id": "test_request",
        "command": command,
        "payload": {} if payload is None else payload,
        "limits": {
            "timeout_seconds": 12,
            "max_nodes": 1000,
            "max_response_chars": 60000,
            **limits,
        },
    }


@pytest.fixture
def world() -> Iterator[World]:
    value = World(Element())
    try:
        yield value
    finally:
        value.close()


def test_typed_abi_preserves_high_pointers_and_correct_out_widths(world: World) -> None:
    api = world.session.native()
    ax, cf = world.ax.library, world.cf.library
    assert ax.AXUIElementCreateSystemWide.restype is ctypes.c_void_p
    assert ax.AXUIElementCopyAttributeValue.argtypes[-1] == ctypes.POINTER(ctypes.c_void_p)
    assert ax.AXUIElementGetAttributeValueCount.argtypes[-1] == ctypes.POINTER(ctypes.c_long)
    assert ax.AXUIElementIsAttributeSettable.argtypes[-1] == ctypes.POINTER(ctypes.c_ubyte)
    assert ax.AXUIElementGetPid.argtypes[-1] == ctypes.POINTER(ctypes.c_int32)
    assert ax.AXUIElementSetMessagingTimeout.argtypes == [helper.P, ctypes.c_float]
    assert cf.CFStringGetCharacters.argtypes == [
        helper.P,
        helper.CFRange,
        ctypes.POINTER(ctypes.c_uint16),
    ]
    assert ctypes.sizeof(helper.CFRange) == 2 * ctypes.sizeof(ctypes.c_long)
    pointer = api.create_string("高位指针")
    assert pointer > 2**32 and api.string(pointer) == "高位指针"
    api.release(pointer)


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) < 8, reason="64-bit ABI proof")
def test_default_int_restype_really_truncates_a_pointer() -> None:
    high = 0x1234_0000_1234
    callback = ctypes.CFUNCTYPE(ctypes.c_void_p)(lambda: high)
    wrong = ctypes.cast(callback, ctypes.CFUNCTYPE(ctypes.c_int))
    assert callback() == high and wrong() != high


def test_copy_out_is_null_initialized_and_borrowed_children_are_retained(world: World) -> None:
    api = world.session.native()
    api.begin(12, 60000)
    application = api.copy(api.systemwide(), "AXFocusedApplication")
    assert application is not None
    try:
        children = api.children(application, "AXChildren", 1)
        child = children[0]
        assert child is not None and api.pid(child) == 42
        assert world.cf.object(child).refs == 1  # The containing array has already gone.
        api.release(child)
        assert world.cf.objects[child].refs == 0
        assert world.ax.out_initial_values and set(world.ax.out_initial_values) == {None}
    finally:
        api.release(application)


def test_borrowed_without_retain_is_invalid_after_array_release() -> None:
    cf = CoreFoundation()
    array = cf.value([Element()])
    child = cf.at(array, 0)
    cf.release(array)
    with pytest.raises(AssertionError, match="use after CFRelease"):
        cf.object(child)
    cf.empty()


def test_selected_root_retained_before_old_registry_released(world: World) -> None:
    old = world.result("targets")["targets"][0]
    pointer = world.session.refs[old["ref_id"]].pointer
    world.cf.events.clear()
    new = world.result("snapshot", {"target_ref_id": old["ref_id"]})["nodes"][0]
    assert new["ref_id"] != old["ref_id"] and old["ref_id"] not in world.session.refs
    assert world.cf.events.index(("retain", pointer)) < world.cf.events.index(("release", pointer))
    assert world.session.refs[new["ref_id"]].pointer == pointer


def test_strings_are_typed_bounded_before_allocating_and_preserve_nul(world: World) -> None:
    api = world.session.native()
    pointer = api.create_string("沐\0璃😀")
    try:
        assert api.string(pointer) == "沐\0璃😀"
    finally:
        api.release(pointer)
    pointer = world.cf.value("x" * (helper.MAX_STRING_UNITS + 1))
    before = len(world.cf.library.CFStringGetCharacters.calls)
    try:
        with pytest.raises(helper.HelperError, match="预算") as error:
            api.string(pointer)
        assert error.value.code == "output_budget"
        assert len(world.cf.library.CFStringGetCharacters.calls) == before
    finally:
        api.release(pointer)


def test_string_false_is_not_a_cf_boolean(world: World) -> None:
    child = world.application.attrs["AXChildren"][0]
    child.attrs["AXEnabled"] = "false"
    result = world.result("snapshot")
    node = result["nodes"][1]
    assert node["enabled"] is None and node["actions"] == [] and not node["editable"]
    assert "provider_error" in result["omission_reasons"]
    assert not world.cf.library.CFBooleanGetValue.calls or all(
        world.cf.objects[args[0]].kind == BOOLEAN
        for args in world.cf.library.CFBooleanGetValue.calls
    )


def test_focused_only_targets_and_selected_direct_windows(world: World) -> None:
    grandchild = Element(name="窗口内按钮")
    window = Element("AXWindow", "窗口", attrs={"AXChildren": [grandchild]})
    world.application.attrs["AXWindows"] = [window]
    application = world.result("targets")
    assert application["scope"] == "foreground_application"
    assert len(application["targets"]) == 1 and application["visited"] == 2
    assert (
        "AXChildren" not in world.application.reads and "AXWindows" not in world.application.reads
    )
    result = world.result("targets", {"application_ref_id": application["targets"][0]["ref_id"]})
    assert result["scope"] == "application_windows" and result["complete"]
    assert [node["kind"] for node in result["targets"]] == ["application", "window"]
    assert not grandchild.reads and "AXChildren" not in window.reads
    selected = result["targets"][1]["ref_id"]
    snapshot = world.result("snapshot", {"target_ref_id": selected})
    assert snapshot["scope"] == "window" and snapshot["nodes"][0]["depth"] == 0
    assert snapshot["nodes"][1]["parent_ref_id"] == snapshot["nodes"][0]["ref_id"]


def test_discovery_failure_never_restores_old_refs_or_falls_back(world: World) -> None:
    old = world.result("targets")["targets"][0]
    world.application.attrs["AXTitle"] = "新名字"
    error = world.request("targets", {"application_ref_id": old["ref_id"]})["error"]
    assert error["code"] == "target_changed" and not world.session.refs
    before = len(world.ax.library.AXUIElementCopyAttributeValue.calls)
    error = world.request("snapshot", {"target_ref_id": old["ref_id"]})["error"]
    assert error["code"] == "stale_node"
    assert len(world.ax.library.AXUIElementCopyAttributeValue.calls) == before


def test_node_budget_bounds_copy_range_and_normal_truncation_is_not_provider_error() -> None:
    world = World(*(Element(name=str(index)) for index in range(1000)))
    try:
        result = world.result("snapshot", max_nodes=20)
        assert result["visited"] == 20 and len(result["nodes"]) == 19
        assert set(result["omission_reasons"]) == {"children_limit", "node_budget"}
        copied = world.ax.library.AXUIElementCopyAttributeValues.calls
        assert len(copied) == 1 and copied[0][3] == 18
        omissions = {
            item["reason"]: item["omitted_direct_children"] for item in result["omissions"]
        }
        assert omissions == {"children_limit": 900, "node_budget": 82}
    finally:
        world.close()


@pytest.mark.parametrize("actual", [0, 1, 3, "wrong array type"])
def test_short_or_invalid_children_arrays_are_explicit_and_released(actual: Any) -> None:
    children = [Element(name="一"), Element(name="二")]
    world = World(*children)
    world.application.copies["AXChildren"] = (
        children[:actual]
        if type(actual) is int and actual <= 2
        else (children + [Element()] if type(actual) is int else actual)
    )
    try:
        result = world.result("snapshot")
        assert "provider_error" in result["omission_reasons"] and not result["complete"]
        assert len(result["nodes"]) == (1 + actual if type(actual) is int and actual < 2 else 1)
    finally:
        world.close()


def test_bad_array_item_counts_as_visit_and_does_not_become_a_ref(world: World) -> None:
    world.application.attrs["AXChildren"] = ["not AX", Element()]
    result = world.result("snapshot")
    assert result["visited"] == 4 and len(result["nodes"]) == 2
    assert "provider_error" in result["omission_reasons"]


def test_omitted_duplicate_refs_stay_alive_for_cf_equal() -> None:
    broken = Element(attrs={"AXRole": True})
    world = World(broken, broken, Element())
    try:
        result = world.result("snapshot")
        assert result["visited"] == 5 and len(result["nodes"]) == 2
        assert result["omission_reasons"] == ["provider_error"]
        assert (
            world.cf.library.CFEqual.calls
        )  # Forced CFHash collisions include released candidates.
    finally:
        world.close()


def test_no_query_past_budget_and_bounded_depth(world: World) -> None:
    result = world.result("snapshot", max_nodes=1)
    assert result["nodes"] == [] and result["visited"] == 1
    assert result["omissions"] == [{"reason": "node_budget", "omitted_direct_children": None}]
    assert not world.ax.library.AXUIElementCopyAttributeValue.calls
    child = Element("AXGroup")
    root = child
    for _ in range(helper.MAX_DEPTH + 1):
        child.attrs["AXChildren"] = [Element("AXGroup")]
        child = child.attrs["AXChildren"][0]
    world.application.attrs["AXChildren"] = [root]
    result = world.result("snapshot")
    assert max(node["depth"] for node in result["nodes"]) == helper.MAX_DEPTH
    assert result["omissions"] == [{"reason": "depth_limit", "omitted_direct_children": 1}]


def test_native_frame_budget_keeps_whole_ids_parent_prefix_and_releases_dropped_refs() -> None:
    group = Element(
        "AXGroup", "面板", attrs={"AXChildren": [Element(name="沐璃\n\0" * 100) for _ in range(8)]}
    )
    world = World(group)
    try:
        response = world.request("snapshot", max_response_chars=1800)
        text = json.dumps(response, ensure_ascii=False)
        assert len(text) + 1 <= 1800 and len(text.encode("utf-8")) + 1 <= helper.MAX_RESPONSE_BYTES
        result = response["result"]
        assert "output_budget" in result["omission_reasons"] and not result["complete"]
        known: set[str] = set()
        for node in result["nodes"]:
            assert len(node["ref_id"]) == 32
            assert node["parent_ref_id"] is None or node["parent_ref_id"] in known
            known.add(node["ref_id"])
        assert set(world.session.refs) == known
        assert len(known) < 10
    finally:
        world.close()


def test_minimum_budget_returns_valid_empty_observation_with_all_reasons(world: World) -> None:
    world.application.attrs["AXEnabled"] = "invalid Boolean"
    response = world.request("snapshot", max_response_chars=512)
    assert len(json.dumps(response, ensure_ascii=False)) + 1 <= 512
    result = response["result"]
    assert result["nodes"] == [] and not world.session.refs
    assert set(result["omission_reasons"]) == {"output_budget", "provider_error"}


def test_missing_parent_is_explicit_when_oversized_record_is_omitted() -> None:
    group = Element(
        "AXGroup", "x" * (helper.MAX_STRING_UNITS + 1), attrs={"AXChildren": [Element()]}
    )
    world = World(group)
    try:
        result = world.result("snapshot")
        child = result["nodes"][1]
        assert (
            child["depth"] == 2 and child["parent_ref_id"] is None and not child["parent_observed"]
        )
        assert result["omission_reasons"] == ["output_budget"]
    finally:
        world.close()


def test_oversized_action_list_is_not_partially_exposed(world: World) -> None:
    world.application.attrs["AXChildren"][0].actions = [f"AXAction{index}" for index in range(21)]
    result = world.result("snapshot")
    assert len(result["nodes"]) == 1 and result["omission_reasons"] == ["output_budget"]


def test_secure_text_does_not_read_label_value_children_or_actions() -> None:
    secure = Element(
        "AXTextField",
        "SECRET label",
        attrs={
            "AXSubrole": helper.SECURE_SUBROLE,
            "AXValue": "SECRET value",
            "AXDescription": "SECRET description",
            "AXChildren": [Element(name="SECRET descendant")],
        },
    )
    world = World(secure)
    try:
        result = world.result("snapshot")
        node = result["nodes"][1]
        assert node["protected"] is True and not node["editable"] and node["actions"] == []
        assert node["name"] == "受保护文本" and "SECRET" not in json.dumps(result)
        assert not {"AXTitle", "AXDescription", "AXValue", "AXChildren", "actions"}.intersection(
            secure.reads
        )
        response = world.request("type", {"ref_id": node["ref_id"], "text": "new"})
        assert response["error"]["outcome"] == "not_started" and not secure.mutations
        assert not world.session.refs
    finally:
        world.close()


@pytest.mark.parametrize("role", sorted(helper.TEXT_ROLES))
@pytest.mark.parametrize(
    "subrole", [None, Fault(helper.NO_VALUE), Fault(helper.ATTRIBUTE_UNSUPPORTED), False]
)
def test_text_role_never_proves_nonsecure_without_well_typed_subrole(
    role: str, subrole: Any
) -> None:
    node = Element(role)
    if subrole is not None:
        node.attrs["AXSubrole"] = subrole
    world = World(node)
    try:
        result = world.result("snapshot")
        record = result["nodes"][1]
        assert record["protected"] is None and not record["editable"] and record["actions"] == []
        assert ("provider_error" in result["omission_reasons"]) == (subrole is False)
        assert "AXValue" not in node.reads and "AXTitle" not in node.reads
        error = world.request("type", {"ref_id": record["ref_id"], "text": "new"})["error"]
        assert error["outcome"] == "not_started" and not node.mutations
    finally:
        world.close()


def test_known_button_without_optional_subrole_can_dispatch_but_unknown_role_cannot(
    world: World,
) -> None:
    records = world.result("snapshot")["nodes"]
    node = records[1]
    assert node["actions"] == ["AXPress"] and node["protected"] is None
    assert world.result("action", {"ref_id": node["ref_id"], "action": "AXPress"}) == {
        "ok": True,
        "outcome": "accepted",
        "effect_verified": False,
    }
    child = world.application.attrs["AXChildren"][0]
    assert child.mutations == [("action", "AXPress")]
    child.attrs["AXRole"] = "AXCustomUnknown"
    node = world.result("snapshot")["nodes"][1]
    assert node["actions"] == [] and not node["editable"]


def test_explicit_nonsecure_text_settable_rechecked_and_nul_preserved() -> None:
    text = Element("AXTextField", attrs={"AXSubrole": "AXSearchField"})
    world = World(text)
    try:
        node = world.result("snapshot")["nodes"][1]
        assert node["editable"] and node["protected"] is False and node["read_only"] is False
        result = world.result("type", {"ref_id": node["ref_id"], "text": "沐\0璃😀"})
        assert result["outcome"] == "accepted" and result["effect_verified"] is False
        assert text.mutations == [("type", "沐\0璃😀")]
        assert all(
            world.cf.objects[call[1]].value != "AXValue"
            for call in world.ax.library.AXUIElementCopyAttributeValue.calls
        )
    finally:
        world.close()


@pytest.mark.parametrize("native_code", [-25202, -25204, -25208])
def test_any_failure_after_native_dispatch_is_unknown_and_not_retried(
    world: World, native_code: int
) -> None:
    child = world.application.attrs["AXChildren"][0]
    node = world.result("snapshot")["nodes"][1]
    child.mutation_code = native_code
    response = world.request("action", {"ref_id": node["ref_id"], "action": "AXPress"})
    assert response["error"] == {
        "code": "action_unknown_outcome",
        "message": "AX 未确认动作结果；勿自动重试",
        "outcome": "unknown",
        "native_code": native_code,
    }
    assert not world.session.refs and len(child.mutations) == 1
    response = world.request("action", {"ref_id": node["ref_id"], "action": "AXPress"})
    assert response["error"]["code"] == "stale_node" and len(child.mutations) == 1


def test_native_exception_after_dispatch_is_unknown_without_raw_details(world: World) -> None:
    child = world.application.attrs["AXChildren"][0]
    node = world.result("snapshot")["nodes"][1]
    child.mutation_exception = True
    response = world.request("action", {"ref_id": node["ref_id"], "action": "AXPress"})
    assert response["error"]["outcome"] == "unknown" and "SECRET" not in str(response)


@pytest.mark.parametrize("command", ["action", "snapshot"])
def test_larger_fresh_metadata_cannot_dispatch_or_restore_old_refs(
    world: World, command: str
) -> None:
    old = world.result("targets")["targets"][0]
    if command == "action":
        node = world.result("snapshot")["nodes"][1]
        world.application.attrs["AXChildren"][0].attrs["AXTitle"] = "x" * 600
        payload = {"ref_id": node["ref_id"], "action": "AXPress"}
    else:
        world.application.attrs["AXTitle"] = "x" * 600
        payload = {"target_ref_id": old["ref_id"]}
    response = world.request(command, payload, max_response_chars=512)
    assert response["error"]["code"] == "element_unavailable"
    assert response["error"]["outcome"] == "not_started"
    assert not world.session.refs and not world.application.attrs["AXChildren"][0].mutations


@pytest.mark.parametrize(
    ("attribute", "value", "code"),
    [
        ("AXEnabled", False, "element_disabled"),
        ("AXEnabled", "false", "protocol_error"),
        ("AXSubrole", helper.SECURE_SUBROLE, "target_changed"),
        ("AXSubrole", True, "protocol_error"),
        ("AXTitle", "changed", "target_changed"),
        ("AXRole", "AXCheckBox", "target_changed"),
    ],
)
def test_fresh_checks_reject_mutated_or_unreadable_state_before_dispatch(
    world: World, attribute: str, value: Any, code: str
) -> None:
    child = world.application.attrs["AXChildren"][0]
    node = world.result("snapshot")["nodes"][1]
    child.attrs[attribute] = value
    response = world.request("action", {"ref_id": node["ref_id"], "action": "AXPress"})
    assert response["error"]["code"] == code and response["error"]["outcome"] == "not_started"
    assert not child.mutations and not world.session.refs


def test_read_timeout_and_permission_failure_never_look_like_missing_subrole(world: World) -> None:
    child = world.application.attrs["AXChildren"][0]
    for native_code, code in [(-25204, "read_timeout"), (-25211, "permission_required")]:
        child.attrs["AXSubrole"] = Fault(native_code)
        response = world.request("snapshot")
        assert response["error"]["code"] == code and response["error"]["native_code"] == native_code
        assert not world.session.refs


def test_dispatch_uses_retained_original_object_without_focused_relookup(world: World) -> None:
    child = world.application.attrs["AXChildren"][0]
    node = world.result("snapshot")["nodes"][1]
    world.ax.system.attrs["AXFocusedApplication"] = Element("AXApplication", "另一个同 PID 应用")
    assert world.result("action", {"ref_id": node["ref_id"], "action": "AXPress"})["ok"]
    assert child.mutations == [("action", "AXPress")]
    assert world.ax.system.reads == ["AXFocusedApplication"]


def test_deadline_at_prepare_never_resets_native_timeout_to_zero(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = world.session.native()
    api.systemwide()
    api.deadline = 10.0
    samples = iter([9.0, 10.0])
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(samples))
    with pytest.raises(helper.HelperError) as failure:
        api._prepare()
    assert failure.value.code == "read_timeout"
    assert not world.ax.library.AXUIElementSetMessagingTimeout.calls


def test_probe_is_trust_only_for_this_process_and_never_creates_a_root(world: World) -> None:
    world.ax.trusted = False
    result = world.result("probe")
    assert result["permission"] == {
        "trusted": False,
        "subject": "helper_process",
        "prompted": False,
    }
    assert result["applications"] is None and result["session_accessible"] is None
    assert result["executable"] == sys.executable and result["helper_pid"] > 0
    assert not world.ax.library.AXUIElementCreateSystemWide.calls
    assert not world.ax.library.AXUIElementCopyAttributeValue.calls
    assert world.request("snapshot")["error"]["code"] == "permission_required"


def test_failed_copy_with_unpromised_output_retires_without_releasing_the_pointer(
    world: World,
) -> None:
    pointer = world.cf.value("not an owned Copy output")
    child = world.application.attrs["AXChildren"][0]
    child.attrs["AXSubrole"] = Fault(-25204, pointer)
    try:
        response = world.request("snapshot")
        assert response["error"]["code"] == "protocol_error" and not world.session.refs
        assert world.cf.object(pointer).refs == 1
    finally:
        world.cf.release(pointer)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("command", "not-a-command"),
        ("payload", {"target_ref_id": True}),
        ("payload", {"extra": "x"}),
        ("limits", {"timeout_seconds": 0, "max_nodes": 1, "max_response_chars": 512}),
        ("limits", {"timeout_seconds": float("nan"), "max_nodes": 1, "max_response_chars": 512}),
        ("limits", {"timeout_seconds": 1, "max_nodes": True, "max_response_chars": 512}),
        ("limits", {"timeout_seconds": 1, "max_nodes": 1001, "max_response_chars": 512}),
        ("limits", {"timeout_seconds": 1, "max_nodes": 1, "max_response_chars": 511}),
        ("limits", {"timeout_seconds": 1, "max_nodes": 1, "max_response_chars": 60001}),
    ],
)
def test_invalid_request_is_bounded_without_loading_native(key: str, value: Any) -> None:
    def fail() -> helper.Native:
        pytest.fail("invalid request must not load native frameworks")

    frame = request("snapshot")
    frame[key] = value
    response = helper.Session(fail).respond(frame)
    assert response["error"]["code"] == "protocol_error"
    assert len(json.dumps(response, ensure_ascii=False)) <= helper.MIN_RESPONSE_CHARS


def test_recoverable_frame_error_returns_same_id_then_stops(world: World) -> None:
    frame = request("snapshot", {"wrong": "x"})
    source = io.BytesIO(
        (json.dumps(frame) + "\n" + json.dumps(request("snapshot")) + "\n").encode()
    )
    output, errors = io.BytesIO(), io.BytesIO()
    assert helper.serve("--session", source, output, errors, factory=world.native) == 1
    responses = output.getvalue().splitlines()
    assert len(responses) == 1
    assert json.loads(responses[0])["request_id"] == frame["request_id"]
    assert not errors.getvalue()


@pytest.mark.parametrize(
    "line",
    [
        b"{bad json}\n",
        b"\xff\n",
        b"{}\n",
        b"[]\n",
        b"null\n",
        b"\n",
        b'{"v":2,"request_id":"known"}\n',
        b'{"v":true,"request_id":"known"}\n',
        b'{"v":1,"request_id":"bad id"}\n',
        b"{}",
        b"x" * helper.MAX_REQUEST_BYTES + b"\n",
    ],
)
def test_unrecoverable_frame_never_invents_an_id_or_echoes_input(line: bytes, world: World) -> None:
    output, errors = io.BytesIO(), io.BytesIO()
    assert helper.serve("--session", io.BytesIO(line), output, errors, factory=world.native) == 1
    assert output.getvalue() == b"" and errors.getvalue() == b"AX helper protocol failure\n"


def test_session_has_hard_command_limit_and_eof_releases_refs(world: World) -> None:
    source = io.BytesIO((json.dumps(request("snapshot")) + "\n").encode() * 17)
    output, errors = io.BytesIO(), io.BytesIO()
    assert helper.serve("--session", source, output, errors, factory=world.native) == 0
    assert len(output.getvalue().splitlines()) == helper.MAX_REQUESTS
    assert not errors.getvalue()
    world.cf.empty()


def test_once_mode_cannot_accept_observation_and_release_is_idempotent(world: World) -> None:
    assert (
        world.session.respond(request("snapshot"), once=True)["error"]["code"] == "protocol_error"
    )
    world.result("snapshot")
    assert world.result("release") == {"released": True}
    assert world.result("release") == {"released": True}
    assert not world.session.refs


def test_non_darwin_never_attempts_framework_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper.sys, "platform", "linux")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Linux must not load macOS frameworks")

    monkeypatch.setattr(helper.ctypes.util, "find_library", forbidden)
    monkeypatch.setattr(helper.ctypes, "CDLL", forbidden)
    response = helper.Session().respond(request("probe"), once=True)
    assert response["error"]["code"] == "backend_unavailable"


@pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS symbols and owned CF only; never AX desktop"
)
def test_macos_library_symbols_and_owned_cf_string_smoke() -> None:
    api = helper.Native()
    try:
        api.begin(1, 60000)
        pointer = api.create_string("CF 自建字符串\0😀")
        try:
            assert api.string(pointer) == "CF 自建字符串\0😀"
        finally:
            api.release(pointer)
        assert api.system == 0  # No trust probe, system root, focused app or native action.
    finally:
        api.close()
