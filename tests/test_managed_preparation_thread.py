from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest
from test_managed_boot import _slot
from test_managed_preparation import Host
from test_managed_preparation import host as host

from muli import managed_preparation
from muli.platforms.installation import PackageLimits
from muli.platforms.managed_deployment import SlotIdentity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed preparation")


async def test_switch_slot_registration_keeps_event_loop_responsive(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    adopted = host.complete(await host.run(host.plan("adopt"), candidate=host.slot))
    candidate = _slot(host.root, "thread-candidate")
    item = host.plan("switch", name="responsive-switch")
    original = managed_preparation.register_local_slot
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    advanced_while_registering: list[bool] = []

    def gated(
        prefix: Path,
        python: Path,
        package_root: Path,
        *,
        limits: PackageLimits | None = None,
    ) -> SlotIdentity:
        value = original(prefix, python, package_root, limits=limits)
        loop.call_soon_threadsafe(entered.set)
        try:
            # This synthetic wait is bounded even in the old synchronous path;
            # an absent loop yield must fail an assertion, not wedge the guard.
            release.wait(timeout=1)
        finally:
            finished.set()
        return value

    async def advance_loop() -> None:
        await entered.wait()
        advanced_while_registering.append(not finished.is_set())
        release.set()

    monkeypatch.setattr(managed_preparation, "register_local_slot", gated)
    observer = asyncio.create_task(advance_loop())
    try:
        prepared = await host.run(item, candidate=candidate)
        await asyncio.wait_for(observer, 4)
    finally:
        release.set()
        if not observer.done():
            observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
    assert prepared.record.phase == "prepared"
    assert prepared.record.generation != adopted.generation
    assert prepared.record.target.installation == candidate.digest
    assert host.journal.read_current() == prepared.record
    assert advanced_while_registering == [True], (
        "真实候选注册尚未返回时，主事件循环必须能处理独立协程"
    )


@pytest.mark.parametrize("cancel_count", [1, 2], ids=["cancel", "repeated-cancel"])
async def test_cancelled_switch_keeps_configuration_lock_until_registration_settles(
    host: Host, monkeypatch: pytest.MonkeyPatch, cancel_count: int
) -> None:
    adopted = host.complete(await host.run(host.plan("adopt"), candidate=host.slot))
    candidate = _slot(host.root, "cancelled-candidate")
    item = host.plan("switch", name="cancelled-switch")
    state_path = host.journal.directory.path / "state.json"
    previous_state = state_path.read_bytes()
    previous_entries = {path.name for path in host.journal.directory.path.iterdir()}
    previous_events = host.runtime.store.event_position()
    original = managed_preparation.register_local_slot
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()

    def gated(
        prefix: Path,
        python: Path,
        package_root: Path,
        *,
        limits: PackageLimits | None = None,
    ) -> SlotIdentity:
        value = original(prefix, python, package_root, limits=limits)
        loop.call_soon_threadsafe(entered.set)
        try:
            release.wait(timeout=1)
        finally:
            finished.set()
        return value

    monkeypatch.setattr(managed_preparation, "register_local_slot", gated)
    preparing = asyncio.create_task(host.run(item, candidate=candidate))
    try:
        await asyncio.wait_for(entered.wait(), 4)
        assert not finished.is_set(), "取消必须实际发生在文件线程仍持有工作时"
        for _ in range(cancel_count):
            assert preparing.cancel()
            await asyncio.sleep(0)
            assert not preparing.done(), "取消观察者不能提前结束未收口的文件线程所有者"
            assert not finished.is_set()
            assert host.runtime.configuration_lock.locked()
            assert state_path.read_bytes() == previous_state
            assert {path.name for path in host.journal.directory.path.iterdir()} == previous_entries
            assert host.runtime.store.event_position() == previous_events
            assert host.runtime.store.get("management", item["id"]) == item
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(preparing, 4)
        assert finished.is_set()
        assert not host.runtime.configuration_lock.locked()
        assert host.journal.read_current() == adopted
        assert state_path.read_bytes() == previous_state
        assert {path.name for path in host.journal.directory.path.iterdir()} == previous_entries
        assert host.runtime.store.event_position() == previous_events
        assert host.runtime.store.get("management", item["id"]) == item
    finally:
        release.set()
        if not preparing.done():
            preparing.cancel()
        await asyncio.wait_for(asyncio.gather(preparing, return_exceptions=True), 4)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("candidate", "managed_slot_changed"),
        ("source-unit", "configuration_changed"),
        ("source-plan", "managed_preparation_plan_changed"),
    ],
)
async def test_switch_revalidates_candidate_and_source_after_registration_returns(
    host: Host, change: str, code: str
) -> None:
    adopted = host.complete(await host.run(host.plan("adopt"), candidate=host.slot))
    candidate = _slot(host.root, "drifting-candidate")
    item = host.plan("switch", name="drifting-switch")
    state_path = host.journal.directory.path / "state.json"
    previous_state = state_path.read_bytes()
    previous_entries = {path.name for path in host.journal.directory.path.iterdir()}
    previous_events = host.runtime.store.event_position()
    changed = False

    def drift() -> None:
        nonlocal changed
        assert not changed
        changed = True
        # Probe.during runs on the owning event loop after candidate registration;
        # the Store mutation must never be moved into the file-only worker.
        if change == "candidate":
            Path(candidate.package.root, "__init__.py").write_bytes(b"changed candidate bytes")
        elif change == "source-unit":
            host.service.path.write_bytes(host.service.path.read_bytes() + b"\n")
        else:
            host.runtime.store.put("management", {**item, "status": "cancelled"})

    host.probe.during = drift
    with pytest.raises(ValueError, match=f"^{code}$"):
        await asyncio.wait_for(host.run(item, candidate=candidate), 4)
    assert changed
    assert host.journal.read_current() == adopted
    assert state_path.read_bytes() == previous_state
    assert {path.name for path in host.journal.directory.path.iterdir()} == previous_entries
    assert host.runtime.store.event_position() == previous_events
    assert host.runtime.store.get("management", item["id"]) == (
        {**item, "status": "cancelled"} if change == "source-plan" else item
    )
