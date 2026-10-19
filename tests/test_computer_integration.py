from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from test_atspi_targets import FakeAction, FakeNode, install

import muli.tools.computer as computer_module
from muli.config import Settings
from muli.tools import atspi_helper
from muli.tools.computer import Computer, ComputerError


def facade(*, output_chars: int = 24000) -> Computer:
    return Computer(
        Settings.model_validate(
            {
                "computer": {"enabled": True, "max_nodes": 10},
                "limits": {"max_output_chars": output_chars},
            }
        )
    )


def install_runner(monkeypatch: pytest.MonkeyPatch, desktop: FakeNode) -> list[dict[str, Any]]:
    install(monkeypatch, desktop)
    requests: list[dict[str, Any]] = []

    async def run(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        request = json.loads(kwargs["input_text"])
        requests.append(request)
        response = atspi_helper.main(request)
        output = json.dumps(response, ensure_ascii=False) + "\n"
        assert len(output.encode("utf-8")) < kwargs["limit"]
        return {"output": output, "stderr": "", "exit_code": int("error" in response)}

    monkeypatch.setattr(computer_module, "run_process", run)
    monkeypatch.setattr(computer_module.sys, "platform", "linux")
    return requests


async def test_actual_helper_facade_discovery_target_and_unknown_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated = FakeNode("/first", role="application", children=[FakeNode("/hidden")] * 500)
    native_action = FakeAction(accepted=False)
    button = FakeNode("/button", name="确认", action=native_action)
    window = FakeNode("/window", name="目标窗口", role="frame", children=[button])
    selected = FakeNode("/second", name="目标应用", role="application", children=[window])
    requests = install_runner(monkeypatch, FakeNode(None, children=[unrelated, selected]))
    computer = facade()
    applications = await computer.targets({})
    application = next(item for item in applications["targets"] if item["name"] == "目标应用")
    windows = await computer.targets({"application_id": application["application_id"]})
    target = next(item for item in windows["targets"] if item["kind"] == "window")
    snapshot = await computer.snapshot({"target_id": target["target_id"]})
    node = next(item for item in snapshot["nodes"] if item["name"] == "确认")
    arguments = {"snapshot_id": snapshot["snapshot_id"], "node_id": node["id"], "action": "click"}
    with pytest.raises(ComputerError) as unknown:
        await computer.action(arguments)
    assert unknown.value.code == "action_unknown_outcome"
    assert unknown.value.outcome == "unknown"
    assert native_action.calls == [0]
    with pytest.raises(ComputerError) as stale:
        await computer.action(arguments)
    assert stale.value.code == "stale_snapshot"
    assert len(requests) == 4 and native_action.calls == [0]
    assert "count" not in unrelated.calls
    assert requests[1]["target"]["path"] == [1]
    assert requests[2]["target"]["path"] == [1, 0]
    assert requests[3]["path"] == [1, 0, 0]


async def test_actual_helper_rejects_state_change_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = FakeAction()
    button = FakeNode("/button", action=action)
    install_runner(monkeypatch, FakeNode(None, children=[button]))
    computer = facade()
    snapshot = await computer.snapshot({})
    button.states.remove("ENABLED")
    with pytest.raises(ComputerError) as rejected:
        await computer.action(
            {"snapshot_id": snapshot["snapshot_id"], "node_id": "n0", "action": "click"}
        )
    assert rejected.value.code == "element_disabled"
    assert rejected.value.outcome == "not_started" and not action.calls
    assert computer.snapshot_id == "" and computer.nodes == {}


async def test_helper_and_public_budgets_preserve_usable_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_runner(
        monkeypatch,
        FakeNode(
            None,
            children=[
                FakeNode(f"/node/{index}", name=('\U0001f30a\n\x00"\\' * 10), action=FakeAction())
                for index in range(9)
            ],
        ),
    )
    computer = facade(output_chars=1000)
    observed = await computer.snapshot({})
    assert len(json.dumps(observed, ensure_ascii=False)) <= 1000
    assert observed["complete"] is False and observed["truncated"] is True
    assert observed["nodes"]
    assert all(item["id"] in computer.nodes for item in observed["nodes"])
    assert len(computer.nodes) == len(observed["nodes"])
    assert observed["snapshot_id"] == computer.snapshot_id


async def test_truncated_native_omission_details_never_become_exact_public_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer = facade()

    async def run(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        return {
            "exit_code": 0,
            "output": json.dumps(
                {
                    "nodes": [],
                    "complete": False,
                    "truncated": True,
                    "visited": 10,
                    "omissions": [
                        {"reason": "provider_error", "path": [0], "omitted_direct_children": 1}
                    ],
                    "omissions_truncated": True,
                    "omission_reasons": ["provider_error"],
                }
            ),
        }

    monkeypatch.setattr(computer_module, "run_process", run)
    monkeypatch.setattr(computer_module.sys, "platform", "linux")
    observed = await computer.snapshot({})
    assert observed["omissions_truncated"] is True
    assert observed["omissions"] == [{"reason": "provider_error", "omitted_direct_children": None}]


async def test_new_discovery_cancelled_while_snapshot_runs_prevents_old_target_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = FakeNode("/application", role="application", children=[FakeNode("/child")])
    install_runner(monkeypatch, FakeNode(None, children=[native]))
    computer = facade()
    apps = await computer.targets({})
    identifier = apps["targets"][0]["target_id"]
    original_call = computer.call
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused_call(request: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return await original_call(request)

    monkeypatch.setattr(computer, "call", paused_call)
    reading = asyncio.create_task(computer.snapshot({"target_id": identifier}))
    await asyncio.wait_for(entered.wait(), 1)
    replacing = asyncio.create_task(computer.targets({}))
    await asyncio.sleep(0)
    assert computer.discovery_id == ""
    replacing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await replacing
    release.set()
    with pytest.raises(ComputerError):
        await reading
    assert computer.snapshot_id == "" and computer.nodes == {}


async def test_real_helper_projection_preserves_distinct_parents_and_safe_budget_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_button = FakeNode("/first/button", name="确认", action=FakeAction())
    second_button = FakeNode("/second/button", name="确认", action=FakeAction())
    first = FakeNode("/first", name="面板甲", children=[first_button])
    second = FakeNode("/second", name="面板乙", children=[second_button])
    window = FakeNode("/window", role="frame", children=[first, second])
    application = FakeNode("/application", role="application", children=[window])
    install_runner(monkeypatch, FakeNode(None, children=[application]))
    computer = facade()
    applications = await computer.targets({})
    windows = await computer.targets(
        {"application_id": applications["targets"][0]["application_id"]}
    )
    target = next(item for item in windows["targets"] if item["kind"] == "window")
    observed = await computer.snapshot({"target_id": target["target_id"]})
    by_id = {item["id"]: item for item in observed["nodes"]}
    buttons = [item for item in observed["nodes"] if item["name"] == "确认"]
    assert len(buttons) == 2 and buttons[0]["parent_id"] != buttons[1]["parent_id"]
    assert {by_id[item["parent_id"]]["name"] for item in buttons} == {"面板甲", "面板乙"}
    assert all(item["depth"] == 2 and item["parent_observed"] for item in buttons)
    root = observed["nodes"][0]
    assert root["depth"] == 0 and root["parent_id"] is None and root["parent_observed"]
    computer.settings.limits.max_output_chars = 1000
    partial = await computer.snapshot({"target_id": target["target_id"]})
    retained = {item["id"] for item in partial["nodes"]}
    assert len(json.dumps(partial, ensure_ascii=False)) <= 1000
    assert partial["truncated"] is True and retained
    assert all(
        item["parent_id"] is None or item["parent_id"] in retained for item in partial["nodes"]
    )


async def test_provider_omitted_parent_is_not_fabricated_or_linked_to_wrong_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaf = FakeNode("/leaf", name="确认", action=FakeAction())
    unreadable = FakeNode("/panel", failures={"name"}, children=[leaf])
    application = FakeNode("/application", role="application", children=[unreadable])
    install_runner(monkeypatch, FakeNode(None, children=[application]))
    computer = facade()
    observed = await computer.snapshot({})
    leaf_row = next(item for item in observed["nodes"] if item["name"] == "确认")
    assert leaf_row["depth"] == 3
    assert leaf_row["parent_id"] is None and leaf_row["parent_observed"] is False
    assert observed["complete"] is False and "provider_error" in observed["omission_reasons"]
