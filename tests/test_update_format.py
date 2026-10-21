from __future__ import annotations

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


def runtime_at(path: Path, *, legacy: bool = False) -> Runtime:
    config = ConfigFile(path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    if legacy:
        Store(path / "data" / "muli.sqlite3").close()
    return Runtime(config, path / "data")


def manager_for(runtime: Runtime) -> SelfManagement:
    async def no_apply(settings: Settings) -> None:
        raise AssertionError("Update eligibility must not apply settings")

    return SelfManagement(
        runtime,
        PluginManager(runtime, FastAPI(), runtime.data_dir / "plugins"),
        UserService(runtime.config.path, runtime.data_dir),
        no_apply,
    )


def forbid_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_discovery(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Format rejection must precede uv discovery")

    async def no_process(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Format rejection must precede subprocesses")

    monkeypatch.setattr("muli.updates.shutil.which", no_discovery)
    monkeypatch.setattr("muli.updates.run_process", no_process)


@pytest.mark.parametrize("version", ["2.0.0", "99.0.0"])
async def test_marked_store_rejects_every_unverified_target_before_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    runtime = runtime_at(tmp_path)
    forbid_installation(monkeypatch)
    try:
        assert runtime.store.format_state is not None
        updates = Updates(runtime.config, runtime.store, runtime.bus)
        assert updates.capability() == {
            "mode": "in_place",
            "format_eligible": False,
            "code": "verified_candidate_required",
        }
        with pytest.raises(ValueError, match="^verified_candidate_required$"):
            await updates.apply(version)
    finally:
        await runtime.close()


async def test_marked_plan_rejected_without_snapshot_or_queue_event(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path)
    manager = manager_for(runtime)
    try:
        before_files = tuple(sorted(manager.directory.glob("*.json")))
        before_event = runtime.store.event_position()
        with pytest.raises(ValueError, match="^verified_candidate_required$"):
            await manager.stage("update", version="99.0.0")
        assert manager.plans() == []
        assert tuple(sorted(manager.directory.glob("*.json"))) == before_files
        assert runtime.store.event_position() == before_event
        status = await runtime.tools["self_status"].handler({})
        assert status["update_capability"] == manager.updates.capability()
        assert runtime.tools["self_update"].requires_approval
    finally:
        await manager.close()
        await runtime.close()


async def test_management_api_keeps_update_diagnostics_readable_after_marker_damage(
    tmp_path: Path,
) -> None:
    runtime = runtime_at(tmp_path)
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get("/api/management")
            assert response.status_code == 200
            assert response.json()["update_capability"]["code"] == "verified_candidate_required"
            with runtime.store.db:
                runtime.store.db.execute(
                    "DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,)
                )
            response = await client.get("/api/management")
            assert response.status_code == 200
            assert response.json()["update_capability"] == {
                "mode": "in_place",
                "format_eligible": False,
                "code": "format_verification_failed",
            }
    finally:
        await app.state.close()


@pytest.mark.parametrize("legacy", [False, True])
async def test_version_notice_and_context_preserve_actual_installation_capability(
    tmp_path: Path, legacy: bool
) -> None:
    runtime = runtime_at(tmp_path, legacy=legacy)
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
        context = await manager.context(runtime.main_session()["id"], [])
        assert len(context) == 1
        if legacy:
            assert "检查安装条件" in notices[0]["content"]
            assert "真实用户批准" in context[0].content
        else:
            assert "先配置来源并核验候选格式" in notices[0]["content"]
            assert "先配置来源并核验" in context[0].content
            assert "可更新到" not in context[0].content
    finally:
        await manager.close()
        await runtime.close()


@pytest.mark.parametrize("damaged", [False, True])
async def test_candidate_notice_is_only_a_configured_hint_not_installation_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damaged: bool
) -> None:
    runtime = runtime_at(tmp_path)
    manager = manager_for(runtime)
    forbid_installation(monkeypatch)
    CandidatePolicyStore(runtime.store).replace(
        CandidateSettings(
            enabled=True,
            wheelhouse=str(tmp_path / "absent-materials"),
            retained_root=str(tmp_path / "absent-retained"),
            uv_executable=str(tmp_path / "absent-uv"),
        )
    )
    if damaged:
        runtime.store.put("update_policy", {"id": "candidate-v1", "schema_version": 999})
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
        notice = runtime.store.list("notification")[0]["content"]
        if damaged:
            assert "先配置来源" in notice
            assert "已配置本地候选来源" not in context[0].content
        else:
            assert "先预检" in notice
            assert "self_update_preview" in context[0].content
            assert "expected_policy" in context[0].content
            assert "不代表候选已经安装" in context[0].content
        assert not manager.plans()
        assert not (tmp_path / "absent-retained").exists()
        assert not manager.updates.capability()["format_eligible"]
    finally:
        await manager.close()
        await runtime.close()


@pytest.mark.parametrize("mutation", ["delete", "unknown"])
async def test_broken_admission_stays_blocked_and_diagnostic_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    runtime = runtime_at(tmp_path)
    forbid_installation(monkeypatch)
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
        assert updates.capability()["code"] == "format_verification_failed"
        with pytest.raises(ValueError, match="^format_verification_failed$"):
            await updates.apply("99.0.0")
        assert runtime.store.format_state == state
    finally:
        await runtime.close()


async def test_update_rechecks_after_uv_directory_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_at(tmp_path, legacy=True)
    root = tmp_path / "tools"
    calls: list[list[str]] = []

    def invalid() -> None:
        raise ValueError("fixture_format_changed")

    async def directory(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(argv)
        assert argv == ["uv", "tool", "dir"]
        monkeypatch.setattr(runtime.store, "revalidate_format", invalid)
        return {"exit_code": 0, "output": str(root)}

    monkeypatch.setattr("muli.updates.shutil.which", lambda _: "uv")
    monkeypatch.setattr("muli.updates.run_process", directory)
    monkeypatch.setattr("muli.updates.sys.prefix", str(root / "muli-agent"))
    try:
        with pytest.raises(ValueError, match="^format_verification_failed$"):
            await Updates(runtime.config, runtime.store, runtime.bus).apply("99.0.0")
        assert calls == [["uv", "tool", "dir"]]
    finally:
        await runtime.close()


def test_unmarked_no_migration_store_still_requires_original_installation_check(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    store = Store(path)
    try:
        Memory(store)
        store.db.executescript(CHANNEL_SCHEMA)
        store.main_session()
    finally:
        store.close()
    opened = Store.open_existing(path)
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
