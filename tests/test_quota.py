from __future__ import annotations

import asyncio
from pathlib import Path

from test_runtime import make_runtime

from muli.providers import ModelTurn
from muli.quota import QuotaBroker


async def test_broker_queues_then_wakes_after_release() -> None:
    broker = QuotaBroker(lambda: 2)
    first = broker.try_acquire(("s", "", "a"))
    second = broker.try_acquire(("s", "", "b"))
    assert first is not None and second is not None
    assert broker.try_acquire(("s", "", "c")) is None
    assert broker.status()["used"] == 2

    waiter = asyncio.create_task(broker.acquire(("s", "", "c")))
    await asyncio.sleep(0)
    assert not waiter.done()
    assert broker.status()["waiting"] == 1

    broker.release(first)
    lease = await asyncio.wait_for(waiter, 1)
    assert lease.key == ("s", "", "c")
    assert broker.status()["used"] == 2
    broker.release(second)
    broker.release(lease)
    assert broker.status()["used"] == 0


async def test_broker_lease_is_idempotent_and_ignores_stale_release() -> None:
    broker = QuotaBroker(lambda: 1)
    lease = broker.try_acquire(("s", "c", "r"))
    assert lease is not None
    assert broker.try_acquire(("s", "c", "r")) is lease
    broker.release(lease)
    replacement = broker.try_acquire(("s", "c", "r"))
    assert replacement is not None and replacement is not lease
    broker.release(lease)
    assert broker.status()["used"] == 1
    broker.release(replacement)
    assert broker.status()["used"] == 0


async def test_team_agents_wait_for_quota_and_release_it(tmp_path: Path) -> None:
    runtime, _ = make_runtime(
        tmp_path, [ModelTurn(content="一号完成"), ModelTurn(content="二号完成")]
    )
    runtime.config.update(
        {"limits": {"safety_max_agents": 1, "max_parallel_agents": 2}}
    )
    await runtime.reconfigure()
    parent = runtime.new_session(mode="yolo")
    try:
        first = await runtime.team.spawn("first", "执行一", parent_id=parent["id"])
        second = await runtime.team.spawn("second", "执行二", parent_id=parent["id"])
        waiting = runtime.team.get(second["id"])
        assert waiting["status"] == "queued"
        assert waiting.get("quota_state") == "waiting_quota"
        await runtime.team.wait([first["id"], second["id"]], timeout=5)
        for _ in range(100):
            if runtime.team.get(second["id"])["status"] == "completed":
                break
            await asyncio.sleep(0.02)
        assert runtime.team.get(first["id"])["status"] == "completed"
        assert runtime.team.get(second["id"])["status"] == "completed"
        assert "quota_state" not in runtime.team.get(second["id"])
        assert runtime.quota.status()["used"] == 0
    finally:
        await runtime.close()


async def test_team_quota_waiting_agent_starts_after_release(tmp_path: Path) -> None:
    runtime, _ = make_runtime(
        tmp_path, [ModelTurn(content="唯一任务"), ModelTurn(content="排队任务")]
    )
    runtime.config.update({"limits": {"safety_max_agents": 1}})
    await runtime.reconfigure()
    parent = runtime.new_session(mode="yolo")
    try:
        first = await runtime.team.spawn("first", "长任务", parent_id=parent["id"])
        second = await runtime.team.spawn("second", "后续任务", parent_id=parent["id"])
        assert runtime.team.get(second["id"]).get("quota_state") == "waiting_quota"
        await runtime.team.wait([first["id"]], timeout=5)
        for _ in range(50):
            agent = runtime.team.get(second["id"])
            if agent["status"] == "completed":
                break
            await asyncio.sleep(0.02)
        assert runtime.team.get(second["id"])["status"] == "completed"
        assert runtime.quota.status()["used"] == 0
    finally:
        await runtime.close()
