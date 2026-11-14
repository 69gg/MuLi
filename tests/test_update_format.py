from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from muli.candidate_policy import CandidateSettings
from muli.candidate_policy_store import CandidatePolicyStore
from muli.config import ConfigFile, Settings
from muli.events import EventBus
from muli.format_evidence import FORMAT_STATE_KEY
from muli.management import SelfManagement
from muli.memory import Memory
from muli.plugins import PluginManager
from muli.runtime import Runtime
from muli.server import create_app
from muli.service import UserService
from muli.store import Store
from muli.store_schema import CHANNEL_SCHEMA
from muli.updates import Updates


def runtime_at(path: Path) -> Runtime:
    config = ConfigFile(path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    return Runtime(config, path / "data")


def legacy_store(path: Path) -> Store:
    store = Store(path)
    try:
        Memory(store)
        store.db.executescript(CHANNEL_SCHEMA)
        store.main_session()
    finally:
        store.close()
    return Store.open_existing(path)


def manager_for(runtime: Runtime) -> SelfManagement:
    async def no_apply(settings: Settings) -> None:
        raise AssertionError("Update eligibility must not apply settings")

    return SelfManagement(
        runtime,
        PluginManager(runtime, FastAPI(), runtime.data_dir / "plugins"),
        UserService(runtime.config.path, runtime.data_dir),
        no_apply,
    )


def fake_installation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    root = tmp_path / "tools"
    calls: list[list[str]] = []

    async def process(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(argv)
        if argv[1:] == ["tool", "dir"]:
            return {"exit_code": 0, "output": str(root)}
        if argv[1:2] == ["tool"] and "upgrade" in argv:
            return {"exit_code": 0, "output": ""}
        raise AssertionError(f"unexpected subprocess: {argv}")

    async def healthy(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr("muli.updates.shutil.which", lambda _: "uv")
    monkeypatch.setattr("muli.updates.run_process", process)
    monkeypatch.setattr("muli.updates.sys.prefix", str(root / "muli-agent"))
    monkeypatch.setattr(Updates, "_health", healthy)
    return calls


async def test_marked_store_reports_direct_in_place_update(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path)
    try:
        assert runtime.store.format_state is not None
        updates = Updates(runtime.config, runtime.store, runtime.bus)
        assert updates.capability() == {
            "mode": "in_place",
            "format_eligible": True,
            "code": "installation_check_required",
        }
    finally:
        await runtime.close()


async def test_marked_store_upgrades_directly_with_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_at(tmp_path)
    calls = fake_installation(monkeypatch, tmp_path)
    try:
        updates = Updates(runtime.config, runtime.store, runtime.bus)
        result = await updates.apply("99.0.0")
        assert result["installed"] == "99.0.0"
        assert result["health"] == "passed" and result["restart_required"] is True
        assert any("upgrade" in argv for argv in calls)
    finally:
        await runtime.close()


async def test_marked_plan_queues_without_a_candidate_snapshot(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path)
    manager = manager_for(runtime)
    try:
        item = await manager.stage("update", version="99.0.0")
        assert item["status"] == "pending" and item["action"] == "update"
        assert [plan["id"] for plan in manager.plans()] == [item["id"]]
        assert "candidate" not in item
    finally:
        await manager.close()
        await runtime.close()


async def test_management_api_reports_in_place_after_marker_damage(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path)
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get("/api/management")
            assert response.status_code == 200
            assert response.json()["update_capability"]["mode"] == "in_place"
            with runtime.store.db:
                runtime.store.db.execute(
                    "DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,)
                )
            response = await client.get("/api/management")
            assert response.status_code == 200
            assert response.json()["update_capability"] == {
                "mode": "in_place",
                "format_eligible": True,
                "code": "installation_check_required",
            }
    finally:
        await app.state.close()


async def test_periodic_update_check_uses_the_configured_interval(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path)
    runtime.config.update({"updates": {"check_on_start": False, "interval_hours": 1 / 3600}})
    manager = manager_for(runtime)
    calls: list[bool] = []

    async def fake_check(*, independent: bool = False) -> dict[str, Any]:
        calls.append(independent)
        return {"available": False}

    manager.updates.check = fake_check  # type: ignore[method-assign]
    try:
        await manager.start()
        for _ in range(40):
            if calls:
                break
            await asyncio.sleep(0.05)
        assert calls == [True]
    finally:
        await manager.close()
        await runtime.close()


async def test_zero_update_interval_disables_periodic_checks(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path)
    runtime.config.update({"updates": {"check_on_start": False, "interval_hours": 0}})
    manager = manager_for(runtime)
    calls: list[bool] = []

    async def fake_check(*, independent: bool = False) -> dict[str, Any]:
        calls.append(independent)
        return {"available": False}

    manager.updates.check = fake_check  # type: ignore[method-assign]
    try:
        await manager.start()
        await asyncio.sleep(0.2)
        assert calls == []
    finally:
        await manager.close()
        await runtime.close()


async def test_version_notice_and_context_recommend_direct_update(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path)
    manager = manager_for(runtime)
    try:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"info": {"name": "muli-agent", "version": "99.0.0"}}
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            assert (await manager.updates.check(client, independent=True))["available"]
            assert (await manager.updates.check(client, independent=True))["available"]
        notices = runtime.store.list("notification")
        assert len(notices) == 1
        assert "99.0.0 已发布" in notices[0]["content"]
        assert "直接升级并重启" in notices[0]["content"]
        context = await manager.context(runtime.main_session()["id"], [])
        assert len(context) == 1
        assert "self_update" in context[0].content
        assert "候选" not in context[0].content
    finally:
        await manager.close()
        await runtime.close()


async def test_legacy_update_diagnostics_do_not_need_runtime_admission(tmp_path: Path) -> None:
    # Normal Runtime refuses this unmarked store. The low-level diagnostic reader
    # still reports the same direct-update capability.
    store = legacy_store(tmp_path / "legacy.sqlite3")
    updates = Updates(ConfigFile(tmp_path / "config.json"), store, EventBus(store))
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"info": {"name": "muli-agent", "version": "99.0.0"}}
                )
            )
        ) as client:
            assert (await updates.check(client, independent=True))["available"]
            assert (await updates.check(client, independent=True))["available"]
        notices = store.list("notification")
        assert len(notices) == 1
        assert "99.0.0 已发布" in notices[0]["content"]
        assert "直接升级并重启" in notices[0]["content"]
        assert store.format_state is None
    finally:
        store.close()


async def test_candidate_policy_is_only_an_optional_hint(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path)
    manager = manager_for(runtime)
    CandidatePolicyStore(runtime.store).replace(
        CandidateSettings(
            enabled=True,
            wheelhouse=str(tmp_path / "absent-materials"),
            retained_root=str(tmp_path / "absent-retained"),
            uv_executable=str(tmp_path / "absent-uv"),
        )
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"info": {"name": "muli-agent", "version": "99.0.0"}}
                )
            )
        ) as client:
            result = await manager.updates.check(client, independent=True)
        assert result["available"] and result["status"] == "checked"
        context = await manager.context(runtime.main_session()["id"], [])
        assert "候选" not in context[0].content
        assert manager.updates.capability()["format_eligible"]
    finally:
        await manager.close()
        await runtime.close()


@pytest.mark.parametrize("mutation", ["delete", "unknown"])
async def test_damaged_marker_does_not_block_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    runtime = runtime_at(tmp_path)
    calls = fake_installation(monkeypatch, tmp_path)
    try:
        state = runtime.store.format_state
        assert state is not None
        with runtime.store.db:
            if mutation == "delete":
                runtime.store.db.execute(
                    "DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,)
                )
            else:
                changed = state.model_dump()
                changed["formats"]["objects"] += 1
                runtime.store.db.execute(
                    "UPDATE runtime_state SET value=? WHERE name=?",
                    (json.dumps(changed), FORMAT_STATE_KEY),
                )
        updates = Updates(runtime.config, runtime.store, runtime.bus)
        assert updates.capability()["mode"] == "in_place"
        assert (await updates.apply("99.0.0"))["health"] == "passed"
        assert calls
    finally:
        await runtime.close()


async def test_health_timeout_is_reported_as_a_failed_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_at(tmp_path)
    try:
        updates = Updates(runtime.config, runtime.store, runtime.bus)
        prefix = tmp_path / "prefix"
        executable = prefix / "bin" / "muli"
        executable.parent.mkdir(parents=True)
        executable.write_text("", encoding="utf-8")

        async def timeout(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise TimeoutError("health check timed out")

        monkeypatch.setattr("muli.updates.run_process", timeout)
        assert await updates._health(prefix, "9.9.9", {}) is False
    finally:
        await runtime.close()


async def test_update_does_not_recheck_the_database_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = legacy_store(tmp_path / "legacy.sqlite3")
    updates = Updates(ConfigFile(tmp_path / "config.json"), store, EventBus(store))
    root = tmp_path / "tools"
    calls: list[list[str]] = []

    async def process(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(argv)
        if argv[1:] == ["tool", "dir"]:
            return {"exit_code": 0, "output": str(root)}
        if argv[1:2] == ["tool"] and "upgrade" in argv:
            return {"exit_code": 0, "output": ""}
        raise AssertionError(f"unexpected subprocess: {argv}")

    async def healthy(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr("muli.updates.shutil.which", lambda _: "uv")
    monkeypatch.setattr("muli.updates.run_process", process)
    monkeypatch.setattr("muli.updates.sys.prefix", str(root / "muli-agent"))
    monkeypatch.setattr(Updates, "_health", healthy)
    try:
        result = await updates.apply("99.0.0")
        assert result["health"] == "passed"
        assert calls[0] == ["uv", "tool", "dir"]
        assert any("upgrade" in argv for argv in calls[1:])
    finally:
        store.close()


def test_unmarked_no_migration_store_reports_in_place(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    opened = legacy_store(path)
    try:
        assert not opened.migration_allowed and opened.format_state is None
        updates = Updates(ConfigFile(tmp_path / "config.json"), opened, EventBus(opened))
        assert updates.capability() == {
            "mode": "in_place",
            "format_eligible": True,
            "code": "installation_check_required",
        }
    finally:
        opened.close()


async def test_managed_installation_uses_the_same_in_place_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_at(tmp_path)
    calls = fake_installation(monkeypatch, tmp_path)
    try:
        updates = Updates(runtime.config, runtime.store, runtime.bus)
        result = await updates.apply("2.0.0")
        assert result["health"] == "passed"
        assert any("upgrade" in argv for argv in calls)
    finally:
        await runtime.close()
