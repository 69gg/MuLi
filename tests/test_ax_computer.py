from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from typing import Any

import pytest

import muli.tools.ax_computer as ax_module
from muli.config import Settings
from muli.tools.ax_lease import AXLeaseError
from muli.tools.computer import Computer, ComputerError


def record(ref: str = "native", **changes: Any) -> dict[str, Any]:
    return {
        "ref_id": ref,
        "parent_ref_id": None,
        "depth": 0,
        "parent_observed": True,
        "kind": "node",
        "name": "确认",
        "role": "AXButton",
        "subrole": None,
        "pid": 42,
        "actions": ["AXPress"],
        "editable": False,
        "enabled": True,
        "protected": None,
        **changes,
    }


def frame(
    command: str = "snapshot",
    records: list[dict[str, Any]] | None = None,
    scope: str = "foreground_application",
) -> dict[str, Any]:
    rows = records if records is not None else [record()]
    return {
        "scope": scope,
        "targets" if command == "targets" else "nodes": rows,
        "complete": True,
        "truncated": False,
        "visited": len(rows),
        "omissions": [],
    }


ACCEPTED = {"ok": True, "outcome": "accepted", "effect_verified": False}
PROBE = {
    "backend": "ax",
    "dependency_available": True,
    "helper_pid": 42,
    "executable": "/fake/python",
    "permission": {"trusted": True, "subject": "helper_process", "prompted": False},
    "session_accessible": None,
    "discovery_scope": "foreground_application",
}


class FakeLease:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.closed = False
        self.state = "ready"
        self.expires_at = asyncio.get_running_loop().time() + 60
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self.close_gate: asyncio.Event | None = None
        self.closes = 0

    async def request(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        deadline: float | None = None,
        before_send: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if before_send:
            before_send()
        self.calls.append((command, payload))
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return deepcopy(value)  # type: ignore[no-any-return]

    async def aclose(self) -> None:
        self.closes += 1
        self.state = "closing"
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True
        self.state = "closed"


def setup(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Any],
    *,
    output_chars: int = 24000,
    guard: Callable[[], Callable[[], None]] | None = None,
) -> tuple[Computer, FakeLease, list[tuple[str, str] | None]]:
    monkeypatch.setattr(ax_module.sys, "platform", "darwin")
    owner: list[tuple[str, str] | None] = [("main", "run-1")]
    computer = Computer(
        Settings.model_validate(
            {
                "computer": {"backend": "ax", "enabled": True},
                "limits": {"max_output_chars": output_chars},
            }
        ),
        owner=lambda: owner[0],
        dispatch_guard=guard,
    )
    lease = FakeLease(responses)
    monkeypatch.setattr(computer._ax, "_new_lease", lambda **kwargs: lease)
    return computer, lease, owner


def action(snapshot: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "node_id": snapshot["nodes"][0]["id"],
        "action": "AXPress",
        **changes,
    }


async def test_foreground_windows_snapshot_one_action_and_private_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = record("app", kind="application", role="AXApplication")
    window = record("window", kind="window", role="AXWindow", parent_ref_id="app-new", depth=1)
    computer, lease, _ = setup(
        monkeypatch,
        [
            frame("targets", [app]),
            frame(
                "targets", [record("app-new", kind="application"), window], "application_windows"
            ),
            frame(records=[record()], scope="window"),
            ACCEPTED,
        ],
    )
    try:
        apps = await computer.targets({})
        windows = await computer.targets({"application_id": apps["targets"][0]["application_id"]})
        snapshot = await computer.snapshot({"target_id": windows["targets"][1]["target_id"]})
        assert await computer.action(action(snapshot)) == ACCEPTED
        assert lease.closed and not computer._ax._targets
        assert lease.calls == [
            ("targets", {}),
            ("targets", {"application_ref_id": "app"}),
            ("snapshot", {"target_ref_id": "window"}),
            ("action", {"ref_id": "native", "action": "AXPress"}),
        ]
        for result in (apps, windows, snapshot):
            for row in result.get("targets", result.get("nodes", [])):
                assert not {"ref_id", "parent_ref_id", "pid", "path", "identity"} & row.keys()
        with pytest.raises(ComputerError, match="快照"):
            await computer.action(action(snapshot))
        assert len(lease.calls) == 4
    finally:
        await computer.aclose()


async def test_foreign_owner_cannot_invalidate_or_release_current_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, owner = setup(monkeypatch, [frame(), ACCEPTED])
    snapshot = await computer.snapshot({})
    owner[0] = ("worker", "run-2")
    for request in (computer.snapshot({}), computer.targets({}), computer.action(action(snapshot))):
        with pytest.raises(ComputerError) as error:
            await request
        assert error.value.code == "desktop_busy"
    assert await computer.release({}) == {"released": True}
    assert not lease.closed
    owner[0] = ("main", "run-1")
    assert await computer.action(action(snapshot)) == ACCEPTED
    await computer.aclose()


async def test_release_then_delegate_requires_fresh_worker_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, first, owner = setup(monkeypatch, [frame()])
    original = await computer.snapshot({})
    await computer.release({})
    second = FakeLease([frame(), ACCEPTED])
    monkeypatch.setattr(computer._ax, "_new_lease", lambda **kwargs: second)
    owner[0] = ("worker", "run-2")
    with pytest.raises(ComputerError):
        await computer.action(action(original))
    snapshot = await computer.snapshot({})
    assert await computer.action(action(snapshot)) == ACCEPTED
    assert first.closed and second.closed
    await computer.aclose()


async def test_old_release_finishing_does_not_close_new_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, first, owner = setup(monkeypatch, [frame()])
    await computer.snapshot({})
    first.close_gate = asyncio.Event()
    releasing = asyncio.create_task(computer.release_execution("main", "run-1"))
    await asyncio.sleep(0)
    assert first.state == "closing" and not first.closed
    second = FakeLease([frame()])
    monkeypatch.setattr(computer._ax, "_new_lease", lambda **kwargs: second)
    owner[0] = ("main", "run-2")
    with pytest.raises(ComputerError) as error:
        await computer.snapshot({})
    assert error.value.code == "desktop_busy" and not second.calls
    first.close_gate.set()
    await releasing
    snapshot = await computer.snapshot({})
    assert not second.closed and computer._ax._snapshot_id == snapshot["snapshot_id"]
    await computer.release_execution("main", "run-1")
    assert not second.closed
    await computer.aclose()


async def test_new_snapshot_retires_discovery_and_current_lease_is_not_relocated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, _ = setup(
        monkeypatch, [frame("targets", [record(kind="application")]), frame(scope="application")]
    )
    apps = await computer.targets({})
    target = apps["targets"][0]["target_id"]
    await computer.snapshot({"target_id": target})
    with pytest.raises(ComputerError) as error:
        await computer.snapshot({"target_id": target})
    assert error.value.code == "target_invalid"
    assert len(lease.calls) == 2
    await computer.aclose()


@pytest.mark.parametrize("changes", [{"action": "invented"}, {"node_id": "invented"}])
async def test_legal_snapshot_attempt_consumes_whole_lease_even_when_rejected(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, str]
) -> None:
    computer, lease, _ = setup(monkeypatch, [frame()])
    snapshot = await computer.snapshot({})
    with pytest.raises(ComputerError):
        await computer.action(action(snapshot, **changes))
    assert lease.closed and len(lease.calls) == 1 and not computer._ax._snapshot_id
    await computer.aclose()


async def test_guard_rechecks_after_startup_wait_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paused = False

    def capture_guard() -> Callable[[], None]:
        def guard() -> None:
            if paused:
                raise ComputerError("task_paused", "主人已暂停")

        return guard

    computer, lease, _ = setup(monkeypatch, [frame(), ACCEPTED], guard=capture_guard)
    snapshot = await computer.snapshot({})
    lease.gate, lease.entered = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(computer.action(action(snapshot)))
    await lease.entered.wait()
    paused = True
    lease.gate.set()
    with pytest.raises(ComputerError) as error:
        await pending
    assert error.value.code == "task_paused" and error.value.outcome == "not_started"
    assert len(lease.calls) == 1 and lease.closed
    await computer.aclose()


async def test_unknown_native_result_is_preserved_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, _ = setup(
        monkeypatch, [frame(), AXLeaseError("action_unknown_outcome", "回执丢失", "unknown")]
    )
    snapshot = await computer.snapshot({})
    with pytest.raises(ComputerError) as error:
        await computer.action(action(snapshot))
    assert error.value.code == "action_unknown_outcome" and error.value.outcome == "unknown"
    assert lease.closed and len(lease.calls) == 2
    await computer.aclose()


@pytest.mark.parametrize(
    "receipt", [ACCEPTED, AXLeaseError("action_unknown_outcome", "回执丢失", "unknown")]
)
async def test_cleanup_failure_does_not_overwrite_native_outcome(
    monkeypatch: pytest.MonkeyPatch, receipt: Any
) -> None:
    computer, lease, _ = setup(monkeypatch, [frame(), receipt])
    snapshot = await computer.snapshot({})
    original = lease.aclose

    async def broken_close() -> None:
        lease.state = "closing"
        raise OSError("fake failure")

    monkeypatch.setattr(lease, "aclose", broken_close)
    if isinstance(receipt, AXLeaseError):
        with pytest.raises(ComputerError) as error:
            await computer.action(action(snapshot))
        assert error.value.outcome == "unknown"
    else:
        assert await computer.action(action(snapshot)) == ACCEPTED
    assert computer._ax._cleanup_error is not None and computer._ax._lease is lease
    computer._ax._probe_result = {"available": True, "state": "ready"}
    computer._ax._probe_until = asyncio.get_running_loop().time() + 60
    status = await computer.status()
    assert status["state"] == "unavailable" and status["available"] is False
    assert status["busy"] and status["cleanup_error"]
    monkeypatch.setattr(lease, "aclose", original)
    await computer.aclose()


async def test_output_budget_keeps_only_addressable_rows_and_parent_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        record(
            f"n{index}",
            name="确认" * 30,
            depth=index,
            parent_ref_id=f"n{index - 1}" if index else None,
        )
        for index in range(10)
    ]
    computer, _, _ = setup(monkeypatch, [frame(records=records)], output_chars=1000)
    result = await computer.snapshot({})
    ids = {node["id"] for node in result["nodes"]}
    assert result["truncated"] and "output_budget" in result["omission_reasons"]
    assert set(computer._ax._nodes) == ids
    assert all(node["parent_id"] is None or node["parent_id"] in ids for node in result["nodes"])
    await computer.aclose()


@pytest.mark.parametrize(
    "changes", [{"parent_ref_id": "missing"}, {"kind": []}, {"depth": True}, {"editable": "yes"}]
)
async def test_malformed_node_closes_lease_and_publishes_no_id(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    computer, lease, _ = setup(monkeypatch, [frame(records=[record(**changes)])])
    with pytest.raises(ComputerError) as error:
        await computer.snapshot({})
    assert error.value.code == "protocol_error" and lease.closed
    assert not computer._ax._nodes
    await computer.aclose()


async def test_cancelled_observation_closes_owned_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    computer, lease, _ = setup(monkeypatch, [frame()])
    lease.gate = asyncio.Event()
    pending = asyncio.create_task(computer.snapshot({}))
    await lease.entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert lease.closed and not computer._ax._snapshot_id
    await computer.aclose()


async def test_expired_or_anonymous_caller_cannot_use_public_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, owner = setup(monkeypatch, [frame()])
    snapshot = await computer.snapshot({})
    lease.expires_at = asyncio.get_running_loop().time() - 1
    with pytest.raises(ComputerError) as error:
        await computer.action(action(snapshot))
    assert error.value.code == "stale_snapshot" and len(lease.calls) == 1
    owner[0] = None
    with pytest.raises(ComputerError) as error:
        await computer.snapshot({})
    assert error.value.code == "caller_unavailable"
    await computer.aclose()


async def test_status_probe_shared_cached_and_cancelled_observer_does_not_cancel_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, probe, _ = setup(monkeypatch, [PROBE])
    probe.gate = asyncio.Event()
    observer = asyncio.create_task(computer.status())
    await probe.entered.wait()
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    assert not probe.closed
    probe.gate.set()
    first = await computer.status()
    second = await computer.status()
    assert first == second and first["session_accessible"] is None and not first["busy"]
    assert first["permission"]["prompted"] is False
    assert probe.calls == [("probe", {})] and probe.closed
    assert computer._ax._lease is None
    await computer.aclose()


@pytest.mark.parametrize(
    ("platform", "backend", "expected"),
    [
        ("linux", "auto", "atspi"),
        ("darwin", "auto", "ax"),
        ("win32", "auto", "unavailable"),
        ("darwin", "atspi", "atspi"),
        ("linux", "ax", "ax"),
    ],
)
async def test_backend_selection_never_falls_back_or_spawns_when_disabled(
    monkeypatch: pytest.MonkeyPatch, platform: str, backend: str, expected: str
) -> None:
    monkeypatch.setattr(ax_module.sys, "platform", platform)
    computer = Computer(Settings.model_validate({"computer": {"backend": backend}}))
    assert computer.backend == expected
    status = await computer.status()
    assert status["code"] == "disabled" and not status["available"]
    await computer.aclose()
