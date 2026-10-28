"""Linux ownership and dispatch fences use the existing fake AT-SPI desktop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

import pytest
from test_atspi_targets import FakeAction, FakeNode
from test_computer_integration import install_runner
from test_computer_targets import ObservedLock

import muli.tools.computer as computer_module
from muli.config import Settings
from muli.tools.computer import Computer, ComputerError

Owner = tuple[str, str]


def setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ttl: float = 60,
    dispatch_guard: Callable[[], Callable[[], None]] | None = None,
) -> tuple[Computer, ContextVar[Owner | None], FakeAction, list[dict[str, Any]]]:
    caller: ContextVar[Owner | None] = ContextVar("atspi_test_owner", default=("main", "run-1"))
    action = FakeAction()
    application = FakeNode(
        "/application",
        role="application",
        children=[FakeNode("/button", action=action)],
    )
    requests = install_runner(monkeypatch, FakeNode(None, children=[application]))
    computer = Computer(
        Settings.model_validate(
            {"computer": {"enabled": True, "backend": "atspi", "snapshot_ttl_seconds": ttl}}
        ),
        owner=caller.get,
        dispatch_guard=dispatch_guard,
    )
    return computer, caller, action, requests


def click(snapshot: dict[str, Any]) -> dict[str, Any]:
    button = next(row for row in snapshot["nodes"] if row["actions"])
    return {"snapshot_id": snapshot["snapshot_id"], "node_id": button["id"], "action": "click"}


async def test_atspi_probe_needs_no_owner_and_never_reserves_an_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, caller, action, requests = setup(monkeypatch)
    caller.set(None)
    assert (await computer.status())["available"] is True
    for operation in (computer.targets, computer.snapshot, computer.release):
        with pytest.raises(ComputerError) as rejected:
            await operation({})
        assert rejected.value.code == "caller_unavailable"
    with pytest.raises(ComputerError) as rejected:
        await computer.action({"snapshot_id": "missing", "node_id": "n0", "action": "click"})
    assert rejected.value.code == "caller_unavailable"
    caller.set(("worker", "run-2"))
    snapshot = await computer.snapshot({})
    caller.set(None)
    assert (await computer.status())["available"] is True
    caller.set(("worker", "run-2"))
    assert (await computer.action(click(snapshot)))["outcome"] == "accepted"
    assert action.calls == [0]
    assert [request["operation"] for request in requests] == [
        "probe",
        "snapshot",
        "probe",
        "action",
    ]


async def test_atspi_read_operations_do_not_consume_the_computer_action_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = PermissionError("只读执行不能派发电脑动作")

    def action_guard() -> Callable[[], None]:
        raise denied

    computer, _, action, requests = setup(monkeypatch, dispatch_guard=action_guard)
    assert (await computer.status())["available"] is True
    applications = await computer.targets({})
    snapshot = await computer.snapshot({"target_id": applications["targets"][0]["target_id"]})
    with pytest.raises(PermissionError) as rejected:
        await computer.action(click(snapshot))
    assert rejected.value is denied and not action.calls
    assert [request["operation"] for request in requests] == ["probe", "targets", "snapshot"]


@pytest.mark.parametrize("operation", ["targets", "snapshot", "action", "release"])
@pytest.mark.parametrize(
    "enabled,platform,code",
    [(False, "linux", "disabled"), (True, "darwin", "backend_unavailable")],
)
async def test_atspi_availability_errors_precede_missing_owner(
    monkeypatch: pytest.MonkeyPatch, operation: str, enabled: bool, platform: str, code: str
) -> None:
    computer, caller, _, requests = setup(monkeypatch)
    caller.set(None)
    computer.config.enabled = enabled
    monkeypatch.setattr(computer_module.sys, "platform", platform)
    with pytest.raises(ComputerError) as rejected:
        await getattr(computer, operation)({})
    assert rejected.value.code == code and not requests


async def test_atspi_foreign_execution_cannot_read_act_or_release_owner_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, caller, action, _ = setup(monkeypatch)
    applications = await computer.targets({})
    identifier = applications["targets"][0]["target_id"]
    snapshot = await computer.snapshot({"target_id": identifier})
    caller.set(("worker", "run-2"))
    for operation, arguments in (
        (computer.targets, {}),
        (computer.snapshot, {"target_id": identifier}),
        (computer.action, click(snapshot)),
        (computer.release, {}),
    ):
        with pytest.raises(ComputerError) as rejected:
            await operation(arguments)
        assert rejected.value.code == "desktop_busy" and rejected.value.outcome == "not_started"
    assert computer.snapshot_id == snapshot["snapshot_id"]
    assert computer.discovery_id == applications["discovery_id"]
    caller.set(("main", "run-1"))
    assert (await computer.action(click(snapshot)))["outcome"] == "accepted"
    assert action.calls == [0]


async def test_atspi_release_requires_new_observation_and_late_old_finalizer_preserves_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, caller, action, _ = setup(monkeypatch)
    applications = await computer.targets({})
    snapshot = await computer.snapshot({})
    assert await computer.release({}) == {"released": True}
    assert not computer.discovery_id and not computer.snapshot_id
    caller.set(("worker", "run-2"))
    with pytest.raises(ComputerError):
        await computer.snapshot({"target_id": applications["targets"][0]["target_id"]})
    with pytest.raises(ComputerError):
        await computer.action(click(snapshot))
    entered, finish = asyncio.Event(), asyncio.Event()

    async def old_ax_cleanup(owner: Owner) -> None:
        assert owner == ("main", "run-1")
        entered.set()
        await finish.wait()

    monkeypatch.setattr(computer._ax, "release_execution", old_ax_cleanup)
    finalizer = asyncio.create_task(computer.release_execution("main", "run-1"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        fresh = await computer.snapshot({})
        finish.set()
        await finalizer
        assert computer.snapshot_id == fresh["snapshot_id"]
        assert (await computer.action(click(fresh)))["outcome"] == "accepted"
        assert action.calls == [0]
    finally:
        finish.set()
        await asyncio.gather(finalizer, return_exceptions=True)


async def test_atspi_finalizer_retires_exact_owner_before_await_and_keeps_new_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, caller, action, _ = setup(monkeypatch)
    await computer.targets({})
    await computer.snapshot({})
    entered, finish = asyncio.Event(), asyncio.Event()

    async def paused_ax_cleanup(owner: Owner) -> None:
        assert owner == ("main", "run-1")
        entered.set()
        await finish.wait()

    monkeypatch.setattr(computer._ax, "release_execution", paused_ax_cleanup)
    # The finalizer's ambient caller is already different. Only its explicit
    # execution identity is allowed to choose which Linux references to retire.
    caller.set(("worker", "run-2"))
    finalizer = asyncio.create_task(computer.release_execution("main", "run-1"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not computer.discovery_id and not computer.snapshot_id
        fresh = await computer.snapshot({})
        finish.set()
        await finalizer
        assert computer.snapshot_id == fresh["snapshot_id"]
        assert (await computer.action(click(fresh)))["outcome"] == "accepted"
        assert action.calls == [0]
    finally:
        finish.set()
        await asyncio.gather(finalizer, return_exceptions=True)


async def test_atspi_close_retires_before_waiting_for_other_backend_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, _, _, requests = setup(monkeypatch)
    await computer.targets({})
    await computer.snapshot({})
    entered, finish = asyncio.Event(), asyncio.Event()

    async def paused_ax_close() -> None:
        entered.set()
        await finish.wait()

    monkeypatch.setattr(computer._ax, "aclose", paused_ax_close)
    closing = asyncio.create_task(computer.aclose())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not computer.discovery_id and not computer.snapshot_id
        assert not computer._targets and not computer.nodes
        with pytest.raises(ComputerError) as rejected:
            await computer.snapshot({})
        assert rejected.value.code == "backend_closed" and len(requests) == 2
    finally:
        finish.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.parametrize("operation", ["targets", "snapshot"])
@pytest.mark.parametrize("revoke", ["release", "execution", "close"])
async def test_atspi_inflight_read_cannot_publish_after_revoke(
    monkeypatch: pytest.MonkeyPatch, operation: str, revoke: str
) -> None:
    computer, caller, _, requests = setup(monkeypatch)
    runner = computer_module.run_process
    entered, deliver = asyncio.Event(), asyncio.Event()

    async def paused_response(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        response = await runner(argv, **kwargs)
        entered.set()
        await deliver.wait()
        return response

    monkeypatch.setattr(computer_module, "run_process", paused_response)
    reading = asyncio.create_task(getattr(computer, operation)({}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if revoke == "release":
            await computer.release({})
        elif revoke == "execution":
            caller.set(("worker", "run-2"))
            await computer.release_execution("main", "run-1")
        else:
            await computer.aclose()
        deliver.set()
        with pytest.raises(ComputerError) as rejected:
            await reading
        assert rejected.value.outcome == "not_started"
        assert not computer.discovery_id and not computer.snapshot_id
        assert not computer._targets and not computer.nodes
        if revoke == "close":
            with pytest.raises(ComputerError) as closed:
                await computer.snapshot({})
            assert closed.value.code == "backend_closed" and len(requests) == 1
    finally:
        deliver.set()
        await asyncio.gather(reading, return_exceptions=True)


@pytest.mark.parametrize("operation", ["targets", "snapshot", "action"])
@pytest.mark.parametrize("revoke", ["release", "close"])
async def test_atspi_waiting_request_is_revoked_before_starting_helper(
    monkeypatch: pytest.MonkeyPatch, operation: str, revoke: str
) -> None:
    computer, _, action, requests = setup(monkeypatch)
    snapshot = await computer.snapshot({})
    lock = ObservedLock()
    await lock.acquire()
    lock.entered.clear()
    monkeypatch.setattr(computer, "_lock", lock)
    arguments = click(snapshot) if operation == "action" else {}
    waiting = asyncio.create_task(getattr(computer, operation)(arguments))
    try:
        await asyncio.wait_for(lock.entered.wait(), 1)
        await (computer.release({}) if revoke == "release" else computer.aclose())
        lock.release()
        with pytest.raises(ComputerError):
            await waiting
        assert not action.calls and len(requests) == 1
        assert not computer.discovery_id and not computer.snapshot_id
    finally:
        if lock.locked():
            lock.release()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_atspi_reads_do_not_renew_owner_ttl_and_expiry_allows_fresh_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, caller, action, _ = setup(monkeypatch, ttl=2)
    clock = [10.0]
    monkeypatch.setattr(computer_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    applications = await computer.targets({})
    clock[0] = 11.0
    snapshot = await computer.snapshot({})
    clock[0] = 12.0
    caller.set(("worker", "run-2"))
    fresh_applications = await computer.targets({})
    assert fresh_applications["discovery_id"] != applications["discovery_id"]
    with pytest.raises(ComputerError):
        await computer.action(click(snapshot))
    assert not action.calls
    fresh = await computer.snapshot({})
    assert (await computer.action(click(fresh)))["outcome"] == "accepted"
    assert action.calls == [0]


@pytest.mark.parametrize("cancel", [False, True])
async def test_atspi_expired_or_cancelled_inflight_read_never_restores_references(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    computer, caller, action, _ = setup(monkeypatch, ttl=2)
    clock = [10.0]
    monkeypatch.setattr(computer_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    runner = computer_module.run_process
    entered, deliver = asyncio.Event(), asyncio.Event()

    async def paused_response(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        result = await runner(argv, **kwargs)
        entered.set()
        await deliver.wait()
        return result

    monkeypatch.setattr(computer_module, "run_process", paused_response)
    reading = asyncio.create_task(computer.snapshot({}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if cancel:
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading
        else:
            clock[0] = 12.0
            deliver.set()
            with pytest.raises(ComputerError):
                await reading
        assert not computer.snapshot_id and not computer.nodes
        await computer.release_execution("main", "run-1")
        caller.set(("worker", "run-2"))
        monkeypatch.setattr(computer_module, "run_process", runner)
        fresh = await computer.snapshot({})
        assert (await computer.action(click(fresh)))["outcome"] == "accepted"
        assert action.calls == [0]
    finally:
        deliver.set()
        await asyncio.gather(reading, return_exceptions=True)


@pytest.mark.parametrize("revoke", ["release", "close", "new_observation", "runtime"])
async def test_atspi_late_start_rechecks_original_action_before_any_request_byte(
    monkeypatch: pytest.MonkeyPatch, revoke: str
) -> None:
    policy = [1]

    def dispatch_guard() -> Callable[[], None]:
        captured = policy[0]

        def guard() -> None:
            if policy[0] != captured:
                raise ComputerError("input_pending", "收到新的要求")

        return guard

    computer, _, action, requests = setup(monkeypatch, dispatch_guard=dispatch_guard)
    snapshot = await computer.snapshot({})
    runner = computer_module.run_process
    entered, start = asyncio.Event(), asyncio.Event()

    async def delayed_start(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        entered.set()
        await start.wait()
        return await runner(argv, **kwargs)

    monkeypatch.setattr(computer_module, "run_process", delayed_start)
    lock = ObservedLock()
    monkeypatch.setattr(computer, "_lock", lock)
    acting = asyncio.create_task(computer.action(click(snapshot)))
    replacing: asyncio.Task[dict[str, Any]] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if revoke == "release":
            await computer.release({})
        elif revoke == "close":
            await computer.aclose()
        elif revoke == "runtime":
            policy[0] = 2
        else:
            # The action already owns the original lock; the newer read retires
            # its token synchronously before waiting on that same lock.
            lock.entered.clear()
            replacing = asyncio.create_task(computer.snapshot({}))
            await asyncio.wait_for(lock.entered.wait(), 1)
            replacing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await replacing
        start.set()
        with pytest.raises(ComputerError) as rejected:
            await acting
        assert rejected.value.outcome == "not_started"
        if revoke == "runtime":
            assert rejected.value.code == "input_pending"
        assert not action.calls
        assert [request["operation"] for request in requests] == ["snapshot"]
        assert not computer.snapshot_id
    finally:
        start.set()
        await asyncio.gather(acting, *([replacing] if replacing else []), return_exceptions=True)
