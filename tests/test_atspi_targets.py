"""Pure fake GI/provider fixtures: never import native GI or touch a desktop."""

from __future__ import annotations

import io
import json
import runpy
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from muli.tools import atspi_helper as helper


@dataclass
class FakeStates:
    values: set[str]

    def contains(self, value: str) -> bool:
        return value in self.values


@dataclass
class FakeAction:
    names: list[str] = field(default_factory=lambda: ["click"])
    calls: list[int] = field(default_factory=list)
    accepted: bool = True
    failure: bool = False

    def get_n_actions(self) -> int:
        return len(self.names)

    def get_action_name(self, index: int) -> str:
        return self.names[index]

    def do_action(self, index: int) -> bool:
        self.calls.append(index)
        if self.failure:
            raise RuntimeError("SECRET native provider details")
        return self.accepted


@dataclass
class FakeEditable:
    calls: list[str] = field(default_factory=list)
    accepted: bool = True
    failure: bool = False

    def set_text_contents(self, text: str) -> bool:
        self.calls.append(text)
        if self.failure:
            raise RuntimeError("SECRET native provider details")
        return self.accepted


@dataclass
class FakeNode:
    path: str | None
    name: str = "测试控件"
    role: str = "panel"
    pid: int = 42
    children: list[FakeNode | None] = field(default_factory=list)
    app: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(bus_name=":1.42"))
    states: set[str] = field(default_factory=lambda: {"ENABLED", "SENSITIVE", "EDITABLE"})
    action: FakeAction | None = None
    editable: FakeEditable | None = None
    failures: set[str] = field(default_factory=set)
    count: int | None = None
    calls: list[str] = field(default_factory=list)
    clear_hook: Callable[[], None] | None = None

    def _read(self, key: str) -> None:
        self.calls.append(key)
        if key in self.failures:
            raise RuntimeError("SECRET provider details")

    def clear_cache(self) -> None:
        self._read("clear_cache")
        if self.clear_hook is not None:
            self.clear_hook()

    def get_name(self) -> str:
        self._read("name")
        return self.name

    def get_role_name(self) -> str:
        self._read("role")
        return self.role

    def get_role(self) -> str:
        self._read("native_role")
        return self.role

    def get_process_id(self) -> int:
        self._read("pid")
        return self.pid

    def get_action_iface(self) -> FakeAction | None:
        self._read("action")
        return self.action

    def get_editable_text_iface(self) -> FakeEditable | None:
        self._read("editable")
        return self.editable

    def get_state_set(self) -> FakeStates:
        self._read("states")
        return FakeStates(set(self.states))

    def get_child_count(self) -> int:
        self._read("count")
        return self.count if self.count is not None else len(self.children)

    def get_child_at_index(self, index: int) -> FakeNode | None:
        self._read(f"child:{index}")
        return self.children[index]


def install(monkeypatch: pytest.MonkeyPatch, desktop: FakeNode | None) -> SimpleNamespace:
    def require_version(namespace: str, version: str) -> None:
        assert (namespace, version) == ("Atspi", "2.0")

    def set_timeout(normal: int, startup: int) -> None:
        assert normal > 0 and startup > 0

    def get_desktop(index: int) -> FakeNode | None:
        assert index == 0
        return desktop

    states = SimpleNamespace(
        **{
            name: name
            for name in (
                "ENABLED",
                "SENSITIVE",
                "DEFUNCT",
                "FOCUSED",
                "ACTIVE",
                "READ_ONLY",
                "EDITABLE",
            )
        }
    )
    atspi = SimpleNamespace(
        set_timeout=set_timeout,
        get_desktop=get_desktop,
        StateType=states,
        Role=SimpleNamespace(PASSWORD_TEXT="password text"),
    )
    gi, repository = ModuleType("gi"), ModuleType("gi.repository")
    gi.require_version = require_version  # type: ignore[attr-defined]
    repository.Atspi = atspi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    return atspi


def target(node: FakeNode, path: list[int]) -> dict[str, Any]:
    return {
        "path": path,
        "expected_identity": helper.identity(node),
        "expected_name": node.name[: helper.MAX_LABEL_CHARS],
        "expected_role": node.role,
        "expected_pid": node.pid,
    }


def reasons(result: dict[str, Any]) -> set[str]:
    return {entry["reason"] for entry in result["omissions"]} | set(
        result.get("omission_reasons", [])
    )


def test_application_and_window_discovery_never_reads_unselected_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden = FakeNode("/hidden", children=[FakeNode("/secret")])
    first = FakeNode("/first", role="application", children=[hidden] * 500)
    window = FakeNode("/window", role="frame", children=[FakeNode("/content")] * 500)
    second = FakeNode("/second", role="application", children=[window])
    desktop = FakeNode(None, children=[first, second])
    install(monkeypatch, desktop)
    applications = helper.main({"operation": "targets", "max_nodes": 3})
    assert applications["complete"] and applications["visited"] == 3
    assert [record["path"] for record in applications["targets"]] == [[0], [1]]
    assert "count" not in first.calls and "count" not in second.calls
    assert not hidden.calls and not window.calls
    windows = helper.main({"operation": "targets", "target": target(second, [1]), "max_nodes": 3})
    assert windows["complete"] and windows["visited"] == 3
    assert [record["path"] for record in windows["targets"]] == [[1, 0]]
    assert "count" not in window.calls and not hidden.calls


def test_targeted_snapshot_reaches_second_application_under_small_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = FakeNode("/first", children=[FakeNode(f"/huge/{i}") for i in range(200)])
    button = FakeNode("/button", action=FakeAction())
    window = FakeNode("/window", children=[button])
    second = FakeNode("/second", children=[window])
    desktop = FakeNode(None, children=[first, second])
    install(monkeypatch, desktop)
    result = helper.main(
        {"operation": "snapshot", "target": target(window, [1, 0]), "max_nodes": 4}
    )
    assert result["complete"] and result["visited"] == 4
    assert [node["path"] for node in result["nodes"]] == [[1, 0], [1, 0, 0]]
    assert result["nodes"][1]["actions"] == ["click"]
    assert first.calls == []


def test_identityless_desktop_is_only_a_container(monkeypatch: pytest.MonkeyPatch) -> None:
    desktop = FakeNode(None, children=[FakeNode("/application")])
    install(monkeypatch, desktop)
    result = helper.main({"operation": "snapshot"})
    assert result["complete"] and result["visited"] == 2
    assert [node["path"] for node in result["nodes"]] == [[0]]
    desktop.path, desktop.action, desktop.editable = "/desktop", FakeAction(), FakeEditable()
    result = helper.main({"operation": "snapshot"})
    assert result["nodes"][0]["path"] == []
    assert result["nodes"][0]["actions"] == [] and not result["nodes"][0]["editable"]


@pytest.mark.parametrize("failure", ["name", "states", "action"])
def test_identifiable_desktop_provider_failure_is_not_silently_complete(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    desktop = FakeNode("/desktop", failures={failure}, children=[FakeNode("/application")])
    install(monkeypatch, desktop)
    result = helper.main({"operation": "snapshot"})
    assert not result["complete"] and result["truncated"]
    assert [record["path"] for record in result["nodes"]] == [[0]]
    assert result["omissions"] == [
        {"reason": "provider_error", "path": [], "omitted_direct_children": None}
    ]


@pytest.mark.parametrize("changed", ["name", "role", "pid", "path", "bus_name"])
def test_changed_target_never_falls_back_to_desktop(
    monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    node, unrelated = FakeNode("/old"), FakeNode("/unrelated")
    desktop = FakeNode(None, children=[node, unrelated])
    install(monkeypatch, desktop)
    locator = target(node, [0])
    if changed == "bus_name":
        node.app.bus_name = ":1.new"
    else:
        setattr(node, changed, 99 if changed == "pid" else "replacement")
    result = helper.main({"operation": "snapshot", "target": locator})
    assert result["code"] == "target_changed" and result["outcome"] == "not_started"
    assert "nodes" not in result and unrelated.calls == []


@pytest.mark.parametrize("path", [[], [True], [-1], [0.5], ["0"], [0] * 13])
def test_malformed_target_path_is_rejected_before_provider_walk(
    monkeypatch: pytest.MonkeyPatch, path: list[Any]
) -> None:
    node, desktop = FakeNode("/application"), FakeNode(None)
    install(monkeypatch, desktop)
    result = helper.main({"operation": "targets", "target": target(node, path)})
    assert result["code"] == "target_invalid" and not desktop.calls


def test_missing_target_and_insufficient_validation_budget_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node, desktop = FakeNode("/application"), FakeNode(None, children=[None])
    install(monkeypatch, desktop)
    result = helper.main({"operation": "snapshot", "target": target(node, [0])})
    assert result["code"] == "target_changed" and "nodes" not in result
    result = helper.main({"operation": "snapshot", "target": target(node, [0]), "max_nodes": 1})
    assert result["code"] == "node_budget" and result["visited"] == 1


def test_wide_tree_reports_exact_direct_child_omission(monkeypatch: pytest.MonkeyPatch) -> None:
    desktop = FakeNode(None, children=[FakeNode(f"/child/{i}") for i in range(101)])
    install(monkeypatch, desktop)
    result = helper.main({"operation": "snapshot", "max_nodes": 300})
    assert len(result["nodes"]) == 100 and result["visited"] == 101
    assert result["truncated"] and not result["complete"]
    assert result["omissions"] == [
        {"reason": "children_limit", "path": [], "omitted_direct_children": 1}
    ]


def test_depth_boundary_and_target_path_share_the_action_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaf = FakeNode("/depth/13")
    for depth in reversed(range(1, 13)):
        leaf = FakeNode(f"/depth/{depth}", children=[leaf], action=FakeAction())
    desktop = FakeNode(None, children=[leaf])
    install(monkeypatch, desktop)
    result = helper.main({"operation": "snapshot"})
    assert result["visited"] == 13 and len(result["nodes"]) == 12
    assert max(len(record["path"]) for record in result["nodes"]) == 12
    omission = result["omissions"][0]
    assert omission == {"reason": "depth_limit", "path": [0] * 12, "omitted_direct_children": 1}
    selected = desktop
    for _ in range(12):
        child = selected.children[0]
        assert child is not None
        selected = child
    focused = helper.main(
        {"operation": "snapshot", "target": target(selected, [0] * 12), "max_nodes": 20}
    )
    assert len(focused["nodes"]) == 1 and reasons(focused) == {"depth_limit"}
    performed = helper.main(
        {"operation": "action", **target(selected, [0] * 12), "action": "click"}
    )
    assert performed == {"ok": True, "outcome": "accepted", "effect_verified": False}


def test_failed_child_attempts_consume_budget_and_do_not_claim_empty_complete_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = FakeNode(None, children=[None] * 100, failures={"child:0"})
    install(monkeypatch, desktop)
    result = helper.main({"operation": "snapshot", "max_nodes": 3})
    assert result["nodes"] == [] and result["visited"] == 3
    assert reasons(result) == {"node_budget", "provider_error"}
    assert sum(call.startswith("child:") for call in desktop.calls) == 2
    assert {entry["reason"]: entry["omitted_direct_children"] for entry in result["omissions"]} == {
        "node_budget": 98,
        "provider_error": 2,
    }


@pytest.mark.parametrize("failure", ["count", "negative", "name", "identity"])
def test_provider_failures_always_leave_unknown_or_exact_omission_evidence(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    node = FakeNode("/application")
    if failure == "negative":
        node.count = -1
    elif failure == "identity":
        node.path = None
    else:
        node.failures.add(failure)
    desktop = FakeNode(None, children=[node])
    install(monkeypatch, desktop)
    result = helper.main({"operation": "snapshot"})
    assert not result["complete"] and result["truncated"] and result["visited"] == 2
    assert result["omissions"] == [
        {"reason": "provider_error", "path": [0], "omitted_direct_children": None}
    ]
    assert "SECRET" not in json.dumps(result)


def test_repeated_native_identity_and_cycles_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    repeated = FakeNode("/same")
    alias = FakeNode("/same")
    application = FakeNode("/application", children=[repeated, alias])
    repeated.children.append(application)
    desktop = FakeNode(None, children=[application])
    install(monkeypatch, desktop)
    result = helper.main({"operation": "snapshot", "max_nodes": 20})
    assert result["visited"] == 5 and len(result["nodes"]) == 2
    assert reasons(result) == {"provider_error"} and not result["complete"]
    assert "count" not in alias.calls


@pytest.mark.parametrize("operation", ["snapshot", "targets"])
def test_json_response_budget_keeps_whole_records_and_nontruncated_actions(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    nodes = [
        FakeNode(f"/child/{i}", name=('中\\"\n\t' * 100), action=FakeAction(names=["完整动作"]))
        for i in range(100)
    ]
    install(monkeypatch, FakeNode(None, children=nodes))
    result = helper.main({"operation": operation, "max_nodes": 101, "max_response_chars": 2500})
    serialized = json.dumps(result, ensure_ascii=False)
    assert len(serialized) <= 2500 and result["visited"] == 101
    assert not result["complete"] and "output_budget" in reasons(result)
    records = result["targets" if operation == "targets" else "nodes"]
    assert 0 < len(records) < 100
    for record in records:
        assert record["actions"] == ["完整动作"]
        assert record["identity"]["object_path"] == f"/child/{record['path'][0]}"


def test_omission_metadata_is_bounded_but_never_hides_actual_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [FakeNode(None, count=101, failures={"count"} if i % 2 else set()) for i in range(50)]
    install(monkeypatch, FakeNode(None, children=nodes))
    result = helper.main({"operation": "snapshot", "max_nodes": 100, "max_response_chars": 512})
    assert len(json.dumps(result, ensure_ascii=False)) <= 512
    assert result["omissions_truncated"] and not result["complete"]
    assert reasons(result) >= {"provider_error", "children_limit", "node_budget"}


@pytest.mark.parametrize("field", ["role", "action_name", "action_count"])
def test_oversized_native_metadata_omits_whole_record_with_output_budget(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    action = FakeAction()
    node = FakeNode("/oversized", action=action, children=[FakeNode("/readable-child")])
    if field == "role":
        node.role = "r" * (helper.MAX_LABEL_CHARS + 1)
    elif field == "action_name":
        action.names = ["a" * (helper.MAX_LABEL_CHARS + 1)]
    else:
        action.names = [f"action-{i}" for i in range(helper.MAX_ACTIONS + 1)]
    install(monkeypatch, FakeNode(None, children=[node]))
    result = helper.main({"operation": "snapshot"})
    assert result["visited"] == 3 and not result["complete"]
    assert [record["path"] for record in result["nodes"]] == [[0, 0]]
    assert result["omissions"] == [
        {"reason": "output_budget", "path": [0], "omitted_direct_children": None}
    ]
    assert action.calls == []


@pytest.mark.parametrize("requested", [None, 10**7])
def test_response_default_and_hard_cap_fit_utf8_process_limit(
    monkeypatch: pytest.MonkeyPatch, requested: int | None
) -> None:
    text = "\U0001f642" * helper.MAX_LABEL_CHARS
    action = FakeAction(names=[text] * helper.MAX_ACTIONS)
    nodes = [
        FakeNode(
            f"/{i}" + "\U0001f642" * (helper.MAX_IDENTITY_CHARS - len(f"/{i}")),
            name=text,
            role=text,
            app=SimpleNamespace(bus_name="\U0001f642" * helper.MAX_IDENTITY_CHARS),
            action=action,
        )
        for i in range(10)
    ]
    install(monkeypatch, FakeNode(None, children=nodes))
    request: dict[str, Any] = {"operation": "snapshot"}
    if requested is not None:
        request["max_response_chars"] = requested
    result = helper.main(request)
    serialized = json.dumps(result, ensure_ascii=False)
    limit = helper.DEFAULT_RESPONSE_CHARS if requested is None else helper.MAX_RESPONSE_CHARS
    assert len(serialized) <= limit
    assert len((serialized + "\n").encode("utf-8")) <= 240001 < 250000
    assert 0 < len(result["nodes"]) < len(nodes)
    assert reasons(result) == {"output_budget"} and result["visited"] == 11
    assert all(record["actions"] == action.names for record in result["nodes"])


@pytest.mark.parametrize("operation", ["action", "type"])
def test_actions_recheck_fresh_native_state_after_cache_clear(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    node = FakeNode("/control", action=FakeAction(), editable=FakeEditable())
    desktop = FakeNode(None, children=[node])
    install(monkeypatch, desktop)
    observed = helper.main({"operation": "snapshot"})["nodes"][0]
    assert observed["actions"] == ["click"] and observed["editable"]

    def invalidate() -> None:
        node.states.discard("ENABLED")

    node.clear_hook = invalidate
    result = helper.main(
        {"operation": operation, **target(node, [0]), "action": "click", "text": "秘密输入"}
    )
    assert result["code"] == "element_disabled" and result["outcome"] == "not_started"
    assert node.action is not None and node.action.calls == []
    assert node.editable is not None and node.editable.calls == []
    assert "秘密输入" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    ("flag", "code"), [("DEFUNCT", "element_unavailable"), ("READ_ONLY", "element_disabled")]
)
def test_defunct_or_readonly_text_is_not_dispatched(
    monkeypatch: pytest.MonkeyPatch, flag: str, code: str
) -> None:
    node = FakeNode("/entry", editable=FakeEditable())
    install(monkeypatch, FakeNode(None, children=[node]))
    node.states.add(flag)
    result = helper.main({"operation": "type", **target(node, [0]), "text": "输入"})
    assert result["code"] == code and node.editable is not None and node.editable.calls == []


def test_password_role_and_unknown_flags_are_not_misreported_as_editable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = FakeNode("/password", role="password text", editable=FakeEditable())
    native = install(monkeypatch, FakeNode(None, children=[node]))
    observed = helper.main({"operation": "snapshot"})["nodes"][0]
    assert observed["protected"] is True and not observed["editable"]
    result = helper.main({"operation": "type", **target(node, [0]), "text": "输入"})
    assert result["code"] == "protected_element"
    delattr(native.StateType, "READ_ONLY")
    node.role = "text"
    observed = helper.main({"operation": "snapshot"})["nodes"][0]
    assert observed["read_only"] is None and not observed["editable"]
    result = helper.main({"operation": "type", **target(node, [0]), "text": "输入"})
    assert result["code"] == "element_disabled"


@pytest.mark.parametrize("unsupported", [False, True])
def test_editable_interface_alone_never_promises_current_editability(
    monkeypatch: pytest.MonkeyPatch, unsupported: bool
) -> None:
    editable = FakeEditable()
    node = FakeNode("/entry", editable=editable)
    native = install(monkeypatch, FakeNode(None, children=[node]))
    if unsupported:
        delattr(native.StateType, "EDITABLE")
    else:
        node.states.discard("EDITABLE")
    observed = helper.main({"operation": "snapshot"})["nodes"][0]
    assert not observed["editable"]
    result = helper.main({"operation": "type", **target(node, [0]), "text": "输入"})
    assert result["code"] == "element_disabled" and editable.calls == []


@pytest.mark.parametrize("operation", ["action", "type"])
@pytest.mark.parametrize("failure", [False, True])
def test_native_false_or_exception_after_dispatch_is_unknown_and_never_retried(
    monkeypatch: pytest.MonkeyPatch, operation: str, failure: bool
) -> None:
    action = FakeAction(accepted=False, failure=failure)
    editable = FakeEditable(accepted=False, failure=failure)
    node = FakeNode("/control", action=action, editable=editable)
    install(monkeypatch, FakeNode(None, children=[node]))
    result = helper.main(
        {"operation": operation, **target(node, [0]), "action": "click", "text": "秘密输入"}
    )
    assert result["code"] == "action_unknown_outcome" and result["outcome"] == "unknown"
    assert len(action.calls if operation == "action" else editable.calls) == 1
    assert "SECRET" not in json.dumps(result) and "秘密输入" not in json.dumps(
        result, ensure_ascii=False
    )


def test_native_named_action_and_whole_text_replacement_remain_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action, editable = FakeAction(names=["open", "confirm"]), FakeEditable()
    node = FakeNode("/control", action=action, editable=editable)
    install(monkeypatch, FakeNode(None, children=[node]))
    result = helper.main({"operation": "action", **target(node, [0]), "action": "confirm"})
    assert result == {"ok": True, "outcome": "accepted", "effect_verified": False}
    result = helper.main({"operation": "type", **target(node, [0]), "text": "完整替换内容"})
    assert result["ok"] and action.calls == [1] and editable.calls == ["完整替换内容"]
    result = helper.main({"operation": "action", **target(node, [0]), "action": "invented"})
    assert result["code"] == "unsupported_operation" and action.calls == [1]


def test_probe_dependency_and_session_errors_are_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, None)
    result = helper.main({"operation": "probe"})
    assert result == {
        "code": "no_desktop",
        "error": "当前桌面会话没有 AT-SPI 服务",
        "outcome": "not_started",
    }
    install(monkeypatch, FakeNode(None, children=[FakeNode("/application")]))
    result = helper.main({"operation": "probe"})
    assert result["available"] and result["applications"] == 1
    assert result["supported_operations"] == ["targets", "snapshot", "action", "type"]
    monkeypatch.setitem(sys.modules, "gi", None)
    result = helper.main({"operation": "snapshot"})
    assert result["code"] == "dependency_missing" and result["outcome"] == "not_started"


@pytest.mark.parametrize(
    "budget", [{"max_nodes": True}, {"max_nodes": 0}, {"max_response_chars": 1}]
)
def test_invalid_budgets_are_explicit_protocol_errors(
    monkeypatch: pytest.MonkeyPatch, budget: dict[str, Any]
) -> None:
    install(monkeypatch, FakeNode(None))
    result = helper.main({"operation": "snapshot", **budget})
    assert result["code"] == "protocol_error" and result["outcome"] == "not_started"


@pytest.mark.parametrize("body", ['{"operation":"targets"}', "[]", "{broken"])
def test_cli_stdout_is_exactly_one_json_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    install(monkeypatch, FakeNode(None))
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(Path(helper.__file__)), run_name="__main__")
    output = capsys.readouterr().out
    result = json.loads(output)
    assert output.count("\n") == 1
    assert stopped.value.code == (1 if "error" in result else 0)
