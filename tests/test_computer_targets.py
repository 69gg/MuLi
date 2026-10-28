from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

import muli.tools.computer as computer_module
from muli.config import Settings
from muli.tools.computer import Computer, ComputerError


@pytest.fixture(autouse=True)
def linux_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(computer_module.sys, "platform", "linux")


def node(path: list[int], *, name: str = "核查", **changes: Any) -> dict[str, Any]:
    return {
        "path": path,
        "name": name,
        "role": "button",
        "pid": 42,
        "identity": {"bus_name": ":1.42", "object_path": "/node/" + "/".join(map(str, path))},
        "actions": ["click"],
        "editable": True,
        "enabled": True,
        "sensitive": True,
        "defunct": False,
        "protected": False,
        "focused": None,
        "active": None,
        "read_only": False,
        **changes,
    }


def frame(field: str, records: list[dict[str, Any]], **changes: Any) -> dict[str, Any]:
    return {
        field: records,
        "complete": True,
        "truncated": False,
        "visited": len(records) + 1,
        "omissions": [],
        **changes,
    }


def computer(**sections: Any) -> Computer:
    return Computer(
        Settings.model_validate(
            {
                "computer": {"enabled": True, "backend": "atspi", **sections.pop("computer", {})},
                **sections,
            }
        ),
        owner=lambda: ("computer-target-tests", "run-1"),
    )


def fake_helper(
    monkeypatch: pytest.MonkeyPatch, target: Computer, responses: list[Any]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def call(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(deepcopy(request))
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return deepcopy(response)

    monkeypatch.setattr(target, "call", call)
    return calls


async def test_discovery_window_snapshot_and_action_use_only_host_cached_native_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    application, window = node([1], name="第二应用", role="application"), node([1, 0], role="frame")
    button = node([1, 0, 2])
    calls = fake_helper(
        monkeypatch,
        target,
        [
            frame("targets", [application]),
            frame("targets", [window]),
            frame("nodes", [button]),
            {"ok": True},
        ],
    )
    apps = await target.targets({})
    application_id = apps["targets"][0]["application_id"]
    assert application_id == apps["targets"][0]["target_id"] and len(application_id) == 32
    windows = await target.targets({"application_id": application_id})
    assert windows["discovery_id"] != apps["discovery_id"]
    assert [row["kind"] for row in windows["targets"]] == ["application", "window"]
    assert windows["targets"][0]["target_id"] != application_id
    window_id = windows["targets"][1]["target_id"]
    snapshot = await target.snapshot({"target_id": window_id})
    assert snapshot["scope"] == {"kind": "window", "target_id": window_id}
    assert calls[1]["target"] == target._locator(application)
    assert calls[2]["target"] == target._locator(window)
    assert await target.action(
        {"snapshot_id": snapshot["snapshot_id"], "node_id": "n0", "action": "click"}
    ) == {"ok": True}
    assert calls[3] == {"operation": "action", **target._locator(button), "action": "click"}
    for public in (apps, windows, snapshot):
        for row in public.get("targets", public.get("nodes", [])):
            assert not {"path", "pid", "identity", "object_path", "bus_name"} & row.keys()
    with pytest.raises(ComputerError, match="当前发现"):
        await target.snapshot({"target_id": application_id})
    assert len(calls) == 4


async def test_zero_argument_snapshot_preserves_desktop_scope_and_root_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    native = node([])
    calls = fake_helper(monkeypatch, target, [frame("nodes", [native])])
    result = await target.snapshot({})
    assert calls == [{"operation": "snapshot", "max_nodes": target.config.max_nodes}]
    assert result["scope"] == "desktop" and result["complete"] is True
    assert result["nodes"][0]["actions"] == [] and result["nodes"][0]["editable"] is False
    assert native["actions"] == ["click"] and "id" not in native


async def test_snapshot_retains_nested_same_name_structure_and_marks_unobserved_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    fake_helper(
        monkeypatch,
        target,
        [
            frame("targets", [node([1])]),
            # Deliberately reverse parent/child order; no parent identity is available
            # for the last record's immediate parent [1, 2].
            frame("nodes", [node([1, 1, 0]), node([1, 1]), node([1]), node([1, 2, 0])]),
        ],
    )
    apps = await target.targets({})
    result = await target.snapshot({"target_id": apps["targets"][0]["target_id"]})
    rows = result["nodes"]
    assert len({row["name"] for row in rows}) == 1
    assert [
        (row["id"], row["depth"], row["parent_id"], row["parent_observed"]) for row in rows
    ] == [
        ("n0", 0, None, True),
        ("n1", 1, "n0", True),
        ("n2", 2, "n1", True),
        ("n3", 2, None, False),
    ]
    assert not any("path" in row or "identity" in row for row in rows)


async def test_output_budget_never_retains_a_reference_to_an_omitted_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer(limits={"max_output_chars": 1000})
    records = [node([0] * depth) for depth in range(1, 9)]
    fake_helper(monkeypatch, target, [frame("nodes", list(reversed(records)))])
    result = await target.snapshot({})
    rows = result["nodes"]
    assert 1 < len(rows) < len(records) and len(json.dumps(result, ensure_ascii=False)) <= 1000
    observed: set[str] = set()
    for row in rows:
        if row["parent_id"] is not None:
            assert row["parent_observed"] is True and row["parent_id"] in observed
        observed.add(row["id"])
    assert rows[0]["parent_id"] is None and rows[0]["parent_observed"] is False
    assert "output_budget" in result["omission_reasons"]


@pytest.mark.parametrize("operation", ["targets", "snapshot"])
async def test_final_unicode_json_budget_keeps_complete_usable_ids_and_drops_whole_records(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    target = computer(limits={"max_output_chars": 1000})
    field = "targets" if operation == "targets" else "nodes"
    records = [
        node([index], name=('蓝\n"\\\x00' * 12) + str(index), actions=["confirm_完整动作"])
        for index in range(20)
    ]
    calls = fake_helper(
        monkeypatch, target, [frame(field, records), frame("nodes", [node([0])]), {"ok": True}]
    )
    result = await getattr(target, operation)({})
    assert len(json.dumps(result, ensure_ascii=False)) <= 1000
    assert result["complete"] is False and result["truncated"] is True
    assert len(result[field]) < len(records)
    omission = next(item for item in result["omissions"] if item["reason"] == "output_budget")
    assert omission["omitted_records"] == len(records) - len(result[field])
    assert omission["omitted_direct_children"] is None
    assert "output_budget" in result["omission_reasons"]
    assert result[field]
    if operation == "targets":
        row = result[field][0]
        assert len(row["target_id"]) == len(row["application_id"]) == 32
        snapshot = await target.snapshot({"target_id": row["target_id"]})
        assert calls[1]["target"]["path"] == [0]
    else:
        snapshot = result
        assert snapshot["nodes"][0]["actions"] == ["confirm_完整动作"]
        assert len(snapshot["snapshot_id"]) == 32
    # Mutating returned arrays cannot change the native action allow-list.
    snapshot["nodes"][0]["actions"].append("forged")
    with pytest.raises(ComputerError, match="不存在"):
        await target.action(
            {"snapshot_id": snapshot["snapshot_id"], "node_id": "n0", "action": "forged"}
        )
    assert len(calls) == (2 if operation == "targets" else 1)


async def test_many_native_omissions_preserve_all_reasons_and_unknown_counts_without_locators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer(limits={"max_output_chars": 1000})
    omissions = [
        {"reason": "provider_error", "path": [index], "omitted_direct_children": None}
        for index in range(100)
    ]
    omissions.append({"reason": "children_limit", "path": [], "omitted_direct_children": 7})
    fake_helper(
        monkeypatch,
        target,
        [frame("nodes", [], complete=False, truncated=True, visited=200, omissions=omissions)],
    )
    result = await target.snapshot({})
    assert len(json.dumps(result, ensure_ascii=False)) <= 1000
    assert result["omissions_truncated"] is True
    assert result["omission_reasons"] == ["children_limit", "provider_error"]
    assert result["omissions"] == [
        {"reason": "provider_error", "omitted_direct_children": None},
        {"reason": "children_limit", "omitted_direct_children": 7},
    ]


async def test_native_frame_summary_does_not_silence_omitted_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer(limits={"max_output_chars": 1000})
    fake_helper(
        monkeypatch,
        target,
        [
            frame(
                "targets",
                [],
                complete=False,
                truncated=True,
                omissions_truncated=True,
                omission_reasons=["provider_error", "output_budget"],
            )
        ],
    )
    result = await target.targets({})
    assert result["omissions_truncated"] is True and result["complete"] is False
    assert result["omission_reasons"] == ["output_budget", "provider_error"]
    assert all(item["omitted_direct_children"] is None for item in result["omissions"])


async def test_truncated_native_omission_details_never_present_subtotals_as_complete_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    fake_helper(
        monkeypatch,
        target,
        [
            frame(
                "nodes",
                [],
                complete=False,
                truncated=True,
                visited=10,
                omissions=[{"reason": "provider_error", "path": [0], "omitted_direct_children": 1}],
                omissions_truncated=True,
                omission_reasons=["provider_error"],
            )
        ],
    )
    result = await target.snapshot({})
    assert result["omissions"] == [{"reason": "provider_error", "omitted_direct_children": None}]
    assert result["omissions_truncated"] is True and result["omission_reasons"] == [
        "provider_error"
    ]


@pytest.mark.parametrize("bad", ["unknown", None, True])
async def test_invalid_snapshot_target_retires_existing_action_token_before_any_helper_call(
    monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    target = computer()
    calls = fake_helper(monkeypatch, target, [frame("nodes", [node([0])])])
    await target.snapshot({})
    with pytest.raises(ComputerError) as caught:
        await target.snapshot({"target_id": bad})
    assert caught.value.code == "target_invalid"
    assert not target.snapshot_id and not target.nodes and len(calls) == 1


async def test_invalid_application_discovery_clears_previous_cache_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    calls = fake_helper(monkeypatch, target, [frame("targets", [node([0])])])
    await target.targets({})
    with pytest.raises(ComputerError):
        await target.targets({"application_id": "missing"})
    assert not target.discovery_id and not target._targets and len(calls) == 1


async def test_window_id_cannot_be_used_as_an_application_and_failure_clears_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    calls = fake_helper(
        monkeypatch, target, [frame("targets", [node([0])]), frame("targets", [node([0, 0])])]
    )
    apps = await target.targets({})
    windows = await target.targets({"application_id": apps["targets"][0]["application_id"]})
    with pytest.raises(ComputerError, match="应用 ID"):
        await target.targets({"application_id": windows["targets"][1]["target_id"]})
    assert not target._targets and len(calls) == 2


@pytest.mark.parametrize("operation", ["snapshot", "targets"])
async def test_discovery_uses_configured_monotonic_ttl(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    target = computer(computer={"snapshot_ttl_seconds": 2})
    clock = [10.0]
    monkeypatch.setattr(computer_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = fake_helper(monkeypatch, target, [frame("targets", [node([0])])])
    apps = await target.targets({})
    clock[0] = 12.0
    key = "target_id" if operation == "snapshot" else "application_id"
    with pytest.raises(ComputerError) as caught:
        await getattr(target, operation)({key: apps["targets"][0][key]})
    assert caught.value.code == "target_expired" and len(calls) == 1 and not target._targets


class ObservedLock(asyncio.Lock):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def acquire(self) -> bool:
        self.entered.set()
        return await super().acquire()


async def test_waiting_and_cancelled_snapshot_retires_token_before_lock_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    fake_helper(monkeypatch, target, [frame("nodes", [node([0])])])
    await target.snapshot({})
    lock = ObservedLock()
    await lock.acquire()
    lock.entered.clear()
    monkeypatch.setattr(target, "_lock", lock)
    waiting = asyncio.create_task(target.snapshot({}))
    await asyncio.wait_for(lock.entered.wait(), 1)
    assert not target.snapshot_id and not target.nodes
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    lock.release()
    assert not target.snapshot_id


@pytest.mark.parametrize("operation", ["snapshot", "targets"])
async def test_selected_target_expiring_while_waiting_for_lock_never_reaches_helper(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    target = computer(computer={"snapshot_ttl_seconds": 2})
    clock = [10.0]
    monkeypatch.setattr(computer_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = fake_helper(monkeypatch, target, [frame("targets", [node([0])])])
    apps = await target.targets({})
    lock = ObservedLock()
    await lock.acquire()
    lock.entered.clear()
    monkeypatch.setattr(target, "_lock", lock)
    key = "target_id" if operation == "snapshot" else "application_id"
    waiting = asyncio.create_task(getattr(target, operation)({key: apps["targets"][0][key]}))
    await asyncio.wait_for(lock.entered.wait(), 1)
    clock[0] = 12.0
    lock.release()
    with pytest.raises(ComputerError) as caught:
        await waiting
    assert caught.value.code == "target_expired" and len(calls) == 1
    assert not target.discovery_id and not target._targets


async def test_new_discovery_invalidates_targeted_snapshot_waiting_for_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    calls = fake_helper(monkeypatch, target, [frame("targets", [node([0])])])
    apps = await target.targets({})
    lock = ObservedLock()
    await lock.acquire()
    lock.entered.clear()
    monkeypatch.setattr(target, "_lock", lock)
    waiting = asyncio.create_task(target.snapshot({"target_id": apps["targets"][0]["target_id"]}))
    await asyncio.wait_for(lock.entered.wait(), 1)
    with pytest.raises(ComputerError):
        await target.targets({"application_id": "missing"})
    lock.release()
    with pytest.raises(ComputerError) as caught:
        await waiting
    assert caught.value.code == "target_invalid" and len(calls) == 1 and not target.snapshot_id


async def test_cancelled_helper_read_and_action_never_restore_consumed_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    entered = asyncio.Event()
    block = False

    async def call(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if block:
            entered.set()
            await asyncio.Event().wait()
        return frame("nodes", [node([0])])

    monkeypatch.setattr(target, "call", call)
    original = await target.snapshot({})
    block = True
    action = asyncio.create_task(
        target.action({"snapshot_id": original["snapshot_id"], "node_id": "n0", "action": "click"})
    )
    await asyncio.wait_for(entered.wait(), 1)
    assert not target.snapshot_id
    action.cancel()
    with pytest.raises(asyncio.CancelledError):
        await action
    block = False
    await target.snapshot({})
    block = True
    entered.clear()
    reading = asyncio.create_task(target.snapshot({}))
    await asyncio.wait_for(entered.wait(), 1)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    assert not target.snapshot_id and not target.nodes and not target._lock.locked()


async def test_late_snapshot_success_cannot_revive_token_after_newer_invalid_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    entered, release = asyncio.Event(), asyncio.Event()

    async def call(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return frame("nodes", [node([0])])

    monkeypatch.setattr(target, "call", call)
    old = asyncio.create_task(target.snapshot({}))
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(ComputerError):
        await target.snapshot({"target_id": "missing"})
    release.set()
    with pytest.raises(ComputerError) as caught:
        await old
    assert caught.value.code == "stale_snapshot" and not target.snapshot_id


async def test_late_discovery_success_cannot_revive_cache_after_newer_failed_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    entered, release = asyncio.Event(), asyncio.Event()

    async def call(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return frame("targets", [node([0])])

    monkeypatch.setattr(target, "call", call)
    old = asyncio.create_task(target.targets({}))
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(ComputerError):
        await target.targets({"application_id": "missing"})
    release.set()
    with pytest.raises(ComputerError) as caught:
        await old
    assert caught.value.code == "target_changed" and not target.discovery_id and not target._targets


async def test_failed_snapshot_does_not_restore_old_token(monkeypatch: pytest.MonkeyPatch) -> None:
    target = computer()
    fake_helper(
        monkeypatch, target, [frame("nodes", [node([0])]), ComputerError("read_timeout", "超时")]
    )
    await target.snapshot({})
    with pytest.raises(ComputerError):
        await target.snapshot({})
    assert not target.snapshot_id and not target.nodes


async def test_concurrent_actions_consume_one_snapshot_and_stale_actions_cannot_revoke_newer_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def call(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        if request["operation"] == "snapshot":
            return frame("nodes", [node([0])])
        calls += 1
        entered.set()
        await release.wait()
        return {"ok": True}

    monkeypatch.setattr(target, "call", call)
    original = await target.snapshot({})
    arguments = {"snapshot_id": original["snapshot_id"], "node_id": "n0", "action": "click"}
    first = asyncio.create_task(target.action(arguments))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(target.action(arguments))
    release.set()
    assert await first == {"ok": True}
    with pytest.raises(ComputerError):
        await second
    assert calls == 1
    fresh = await target.snapshot({})
    with pytest.raises(ComputerError):
        await target.action(arguments)
    assert target.snapshot_id == fresh["snapshot_id"]


@pytest.mark.parametrize("bad_path", [[True], [0] * 13, [-1], ["0"]])
async def test_invalid_native_paths_fail_without_publishing_snapshot(
    monkeypatch: pytest.MonkeyPatch, bad_path: Any
) -> None:
    target = computer()
    fake_helper(monkeypatch, target, [frame("nodes", [node(bad_path)])])
    with pytest.raises(ComputerError) as caught:
        await target.snapshot({})
    assert caught.value.code == "protocol_error" and not target.snapshot_id


async def test_targeted_snapshot_cannot_escape_selected_native_subtree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    fake_helper(
        monkeypatch, target, [frame("targets", [node([1])]), frame("nodes", [node([0, 0])])]
    )
    apps = await target.targets({})
    with pytest.raises(ComputerError, match="目标以外"):
        await target.snapshot({"target_id": apps["targets"][0]["target_id"]})
    assert not target.snapshot_id


@pytest.mark.parametrize(
    "changes",
    [
        {"complete": True, "truncated": True},
        {"complete": False, "truncated": True},
        {"visited": True},
        {"omissions": [{"reason": "provider_error", "omitted_direct_children": True}]},
        {"omission_reasons": ["unknown"]},
    ],
)
async def test_invalid_observation_metadata_does_not_publish_snapshot(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    target = computer()
    fake_helper(monkeypatch, target, [frame("nodes", [node([0])], **changes)])
    with pytest.raises(ComputerError) as caught:
        await target.snapshot({})
    assert caught.value.code == "protocol_error" and not target.snapshot_id


async def test_helper_process_uses_configured_deadline_and_finite_native_frame_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer(
        computer={"python_path": "/system/python with spaces", "operation_timeout_seconds": 3.5}
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    async def run(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        kwargs["input_guard"]()
        calls.append((argv, kwargs))
        return {
            "exit_code": 0,
            "output": json.dumps(frame("nodes", [])),
            "stderr": "never expose me",
        }

    monkeypatch.setattr(computer_module, "run_process", run)
    monkeypatch.setattr(computer_module.sys, "platform", "linux")
    await target.snapshot({})
    argv, kwargs = calls[0]
    assert argv[0] == "/system/python with spaces" and len(argv) == 2
    assert (
        kwargs["timeout"] == 3.5 and kwargs["limit"] == 250000 and kwargs["merge_stderr"] is False
    )
    assert json.loads(kwargs["input_text"])["max_response_chars"] * 4 + 1 < kwargs["limit"]


async def test_helper_location_budget_failure_keeps_stable_semantic_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()

    async def run(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        kwargs["input_guard"]()
        return {
            "exit_code": 1,
            "output": json.dumps(
                {
                    "code": "node_budget",
                    "error": "定位目标时已达到节点预算",
                    "outcome": "not_started",
                    "visited": 1,
                }
            ),
        }

    monkeypatch.setattr(computer_module, "run_process", run)
    monkeypatch.setattr(computer_module.sys, "platform", "linux")
    with pytest.raises(ComputerError) as caught:
        await target.call({"operation": "snapshot"})
    assert caught.value.code == "node_budget" and caught.value.outcome == "not_started"


@pytest.mark.parametrize(
    "exception,operation,code,outcome",
    [
        (FileNotFoundError(), "snapshot", "dependency_missing", "not_started"),
        (TimeoutError(), "snapshot", "read_timeout", "not_started"),
        (TimeoutError(), "action", "action_unknown_outcome", "unknown"),
        (ValueError("secret raw process output"), "snapshot", "limit_exceeded", "not_started"),
        (OSError(), "action", "action_unknown_outcome", "unknown"),
    ],
)
async def test_process_failures_are_structured_and_do_not_echo_raw_diagnostics(
    monkeypatch: pytest.MonkeyPatch, exception: Exception, operation: str, code: str, outcome: str
) -> None:
    target = computer()

    async def run(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        kwargs["input_guard"]()
        raise exception

    monkeypatch.setattr(computer_module, "run_process", run)
    monkeypatch.setattr(computer_module.sys, "platform", "linux")
    with pytest.raises(ComputerError) as caught:
        await target.call({"operation": operation})
    assert isinstance(caught.value, ValueError)
    assert caught.value.code == code and caught.value.outcome == outcome
    assert "secret" not in json.dumps(caught.value.as_result())


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"error": "raw secret"},
        {"error": "raw secret", "code": []},
        {"error": "raw secret", "code": "target_invalid", "outcome": []},
    ],
)
async def test_malformed_native_responses_fail_with_stable_code(
    monkeypatch: pytest.MonkeyPatch, data: Any
) -> None:
    target = computer()

    async def run(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        kwargs["input_guard"]()
        return {"exit_code": 1, "output": json.dumps(data)}

    monkeypatch.setattr(computer_module, "run_process", run)
    monkeypatch.setattr(computer_module.sys, "platform", "linux")
    with pytest.raises(ComputerError) as caught:
        await target.call({"operation": "snapshot"})
    assert caught.value.code == "protocol_error" and "secret" not in caught.value.message


@pytest.mark.parametrize(
    "enabled,platform,code",
    [
        (False, "linux", "disabled"),
        (True, "darwin", "backend_unavailable"),
        (True, "win32", "backend_unavailable"),
    ],
)
async def test_disabled_or_unsupported_status_never_starts_helper(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, platform: str, code: str
) -> None:
    target = computer(computer={"enabled": enabled})

    async def forbidden(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("must not start native helper")

    monkeypatch.setattr(computer_module, "run_process", forbidden)
    monkeypatch.setattr(computer_module.sys, "platform", platform)
    status = await target.status()
    assert status["code"] == code and status["available"] is False
    assert status["supported_operations"] == [] and status["capture"]["enabled"] is False


async def test_status_reports_actual_native_capabilities_without_claiming_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = computer()
    fake_helper(
        monkeypatch,
        target,
        [
            {
                "available": True,
                "applications": 2,
                "supported_operations": ["targets", "snapshot", "action", "type"],
                "discovery_scope": "desktop_applications",
            }
        ],
    )
    status = await target.status()
    assert status["state"] == "ready" and status["session_accessible"] is True
    assert (
        status["discovery_scope"] == "desktop_applications"
        and "targets" in status["supported_operations"]
    )
    assert "permission" not in status


def test_tool_contracts_preserve_read_risk_and_hide_native_locators() -> None:
    tools = {tool.name: tool for tool in computer().tools()}
    for name, key in [("computer_targets", "application_id"), ("computer_snapshot", "target_id")]:
        assert tools[name].parameters["properties"].keys() == {key}
        assert tools[name].parameters["required"] == [] and tools[name].risk == "read"
        assert tools[name].parameters["additionalProperties"] is False
    assert tools["computer_action"].risk == tools["computer_type"].risk == "computer"
    assert not any(tool.requires_approval for tool in tools.values())


def test_structured_error_fits_minimum_serialized_output_budget_without_losing_semantics() -> None:
    error = ComputerError("action_unknown_outcome", '\x00\n\\"蓝' * 500, "unknown")
    result = error.as_result()
    assert len(json.dumps(result, ensure_ascii=False)) <= 1000
    assert result["code"] == error.code and result["outcome"] == "unknown"
    assert result["error"] == result["message"] and result["message"].endswith("…")
