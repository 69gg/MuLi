from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_attention import attention_inputs, eventually
from test_file_inbox import FakeWatch, configured
from test_runtime import make_runtime

from muli.attention import AttentionInbox
from muli.commitments import Commitments
from muli.file_inbox import EVENT_TYPE, FileInbox, FileInboxError, FileInboxSettings, read_record
from muli.format_admission import FormatAdmissionError
from muli.platforms.file_watch import FileSignal
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.runtime import Runtime


async def setup_project(tmp_path: Path) -> tuple[Runtime, FileInbox, AttentionInbox, str, Path]:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {"workspace_roots": [str(tmp_path)], "attention": {"coalesce_seconds": 0}}
    )
    # Keep admission durable and observable without starting a model request.
    runtime.config.update({"models": {"main": {"api_key": "", "model": ""}}})
    project = await Commitments(runtime.store, runtime.bus).save(
        "项目复核", brief={"goal": "比较当前项目资料", "constraints": ["只读核查"]}
    )
    directory = tmp_path / "inbox"
    directory.mkdir()
    source = FileInbox(runtime, watch_factory=FakeWatch)
    consumer = AttentionInbox(runtime, file_inbox_settings=source.settings_state)
    # These tests drive the real consumer one batch at a time, without its runner.
    consumer._closing = False
    settings = FileInboxSettings.model_validate(
        {
            "enabled": True,
            "path": str(directory),
            "coalesce_seconds": 0,
            "project_id": project["id"],
        }
    )
    async with runtime.configuration_lock:
        await source.configure(settings)
    return runtime, source, consumer, project["id"], directory


async def arrive(runtime: Runtime, source: FileInbox, directory: Path) -> dict[str, Any]:
    (directory / "new.txt").write_text("外部资料正文不是授权", encoding="utf-8")
    await source.ingest([FileSignal("moved_to", "new.txt")])
    await eventually(lambda: bool(runtime.store.list("file_inbox_record")))
    return runtime.store.list("file_inbox_record")[0]


async def test_project_binding_freezes_source_and_web_routing_without_body(tmp_path: Path) -> None:
    runtime, source, consumer, project_id, directory = await setup_project(tmp_path)
    try:
        record = await arrive(runtime, source, directory)
        binding = runtime.store.get("file_inbox_project", "current")
        assert binding is not None and binding["project_id"] == project_id
        assert record["proof"]["project"] == {
            "project_id": project_id,
            "binding_revision": binding["revision"],
        }
        assert record["routing"] == FrozenRouting.bound(ReplyOrigin(kind="web")).dump()
        assert "外部资料正文" not in json.dumps(record, ensure_ascii=False)
        event = next(item for item in runtime.store.events() if item["type"] == EVENT_TYPE)
        assert read_record(runtime.store, event, source.settings_state()) == record
        assert await consumer._consume()
        inputs = attention_inputs(runtime)
        assert len(inputs) == 1
        marker = inputs[0]["project_review"]
        assert marker["project_id"] == project_id
        assert marker["task_snapshot"]["revision"] == 1
        assert marker["sources"][0]["event_id"] == record["event_id"]
        assert "外部资料正文" not in inputs[0]["content"]
    finally:
        await consumer.close()
        await source.close()
        await runtime.close()


async def test_project_snapshot_change_refreshes_once_without_new_arrival(tmp_path: Path) -> None:
    runtime, source, consumer, project_id, directory = await setup_project(tmp_path)
    try:
        await arrive(runtime, source, directory)
        before = consumer.cursor
        original = runtime.submit_attention
        attempted = 0

        async def mutate_then_submit(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
            nonlocal attempted
            attempted += 1
            if attempted == 1:
                async with runtime.configuration_lock:
                    await Commitments(runtime.store, runtime.bus).save(
                        "项目要求已变", identifier=project_id
                    )
            return await original(*args, **kwargs)

        runtime.submit_attention = mutate_then_submit  # type: ignore[method-assign]
        assert await consumer._consume()
        assert consumer.cursor == before
        assert attention_inputs(runtime) == []
        assert runtime.store.list("attention_receipt") == []
        assert await consumer._consume()
        inputs = attention_inputs(runtime)
        assert len(inputs) == 1 and attempted == 2
        assert inputs[0]["project_review"]["task_snapshot"]["revision"] == 2
        await consumer._consume()
        assert len(attention_inputs(runtime)) == 1
    finally:
        await consumer.close()
        await source.close()
        await runtime.close()


async def test_project_repeated_snapshot_conflicts_wait_for_real_bus(tmp_path: Path) -> None:
    runtime, source, consumer, project_id, directory = await setup_project(tmp_path)
    try:
        await arrive(runtime, source, directory)
        original = runtime.submit_attention
        attempts = 0
        cursor = consumer.cursor

        async def conflicting(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
            nonlocal attempts
            attempts += 1
            async with runtime.configuration_lock:
                item = runtime.store.get("commitment", project_id)
                assert item is not None
                runtime.store.put("commitment", {**item, "revision": item["revision"] + 1})
            return await original(*args, **kwargs)

        runtime.submit_attention = conflicting  # type: ignore[method-assign]
        revision = runtime.bus.revision
        assert await consumer._consume()
        assert not await consumer._consume()
        for _ in range(4):
            assert not await consumer._consume()
        assert attempts == 2 and runtime.bus.revision == revision
        assert consumer.cursor == cursor and attention_inputs(runtime) == []
        assert runtime.store.list("attention_receipt") == []
        runtime.submit_attention = original  # type: ignore[method-assign]
        await runtime.bus.notify()
        assert await consumer._consume()
        inputs = attention_inputs(runtime)
        assert len(inputs) == 1
        assert inputs[0]["project_review"]["task_snapshot"]["revision"] == 3
    finally:
        await consumer.close()
        await source.close()
        await runtime.close()


@pytest.mark.parametrize("terminal", ["done", "cancelled", "unbind"])
async def test_project_terminal_or_unbound_fact_never_falls_back_to_generic(
    tmp_path: Path, terminal: str
) -> None:
    runtime, source, consumer, project_id, directory = await setup_project(tmp_path)
    try:
        record = await arrive(runtime, source, directory)
        if terminal == "unbind":
            async with runtime.configuration_lock:
                await source.configure(
                    source.desired_settings().model_copy(update={"project_id": None})
                )
        else:
            await Commitments(runtime.store, runtime.bus).save(
                "项目已终止", identifier=project_id, status=terminal, evidence="主人结束任务"
            )
        for _ in range(3):
            await consumer._consume()
        assert attention_inputs(runtime) == []
        assert consumer.cursor >= record["event_id"]
    finally:
        await consumer.close()
        await source.close()
        await runtime.close()


async def test_project_settings_and_binding_rollback_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, source, consumer, _, _ = await setup_project(tmp_path)
    try:
        settings = source.desired_settings()
        binding = runtime.store.get("file_inbox_project", "current")
        policy = runtime.store.get("file_inbox_policy", "current")
        target = await Commitments(runtime.store, runtime.bus).save(
            "另一个项目", brief={"goal": "不能部分绑定", "constraints": []}
        )
        original = runtime.store._put

        def fail_binding(kind: str, item: dict[str, Any]) -> None:
            if kind == "file_inbox_project":
                raise sqlite3.OperationalError("fixed test failure")
            original(kind, item)

        monkeypatch.setattr(runtime.store, "_put", fail_binding)
        async with runtime.configuration_lock:
            with pytest.raises(FileInboxError, match="file_inbox_settings_write_failed"):
                await source.configure(settings.model_copy(update={"project_id": target["id"]}))
        assert source.desired_settings() == settings
        assert runtime.store.get("file_inbox_project", "current") == binding
        assert runtime.store.get("file_inbox_policy", "current") == policy
        assert source.status()["running"]
    finally:
        await consumer.close()
        await source.close()
        await runtime.close()


async def test_project_metadata_budget_does_not_advance_or_submit(tmp_path: Path) -> None:
    runtime, source, consumer, _, directory = await setup_project(tmp_path)
    try:
        await arrive(runtime, source, directory)
        runtime.config.update({"attention": {"max_chars": 512}})
        cursor = consumer.cursor
        with pytest.raises(FileInboxError, match="file_inbox_project_budget"):
            await consumer._consume()
        assert consumer.cursor == cursor
        assert attention_inputs(runtime) == []
        assert runtime.store.list("attention_receipt") == []
    finally:
        await consumer.close()
        await source.close()
        await runtime.close()


async def test_project_unbind_cancels_only_pending_project_batch(tmp_path: Path) -> None:
    runtime, source, consumer, _, directory = await setup_project(tmp_path)
    try:
        await arrive(runtime, source, directory)
        assert await consumer._consume()
        original = attention_inputs(runtime)
        assert len(original) == 1 and original[0]["state"] == "pending"
        owner = runtime.conversation.enqueue(
            runtime.main_session()["id"],
            "主人后续指令",
            source="web",
            principal="owner",
            mode="ask",
            max_pending=runtime.config.settings.limits.max_pending_runs,
        )[0]
        async with runtime.configuration_lock:
            await source.configure(
                source.desired_settings().model_copy(update={"project_id": None})
            )
        assert attention_inputs(runtime)[0]["state"] == "cancelled"
        pending = runtime.conversation.pending(runtime.main_session()["id"])
        assert [item["id"] for item in pending] == [owner["id"]]
    finally:
        await consumer.close()
        await source.close()
        await runtime.close()


@pytest.mark.parametrize("entry", ["configure", "settings_store"])
async def test_current_format_one_rejects_project_before_any_write(
    tmp_path: Path, entry: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    source = FileInbox(runtime, watch_factory=FakeWatch)
    directory = tmp_path / "inbox"
    directory.mkdir()
    try:
        target = await Commitments(runtime.store, runtime.bus).save(
            "项目", brief={"goal": "真实格式1不能隐式升级", "constraints": []}
        )
        settings = FileInboxSettings(enabled=True, path=str(directory), project_id=target["id"])
        before = runtime.store.events()
        with pytest.raises(FormatAdmissionError, match="project_review_format_required"):
            if entry == "configure":
                async with runtime.configuration_lock:
                    await source.configure(settings)
            else:
                source.settings_store.replace(settings)
        assert runtime.store.get("file_inbox_settings", "current") is None
        assert runtime.store.get("file_inbox_project", "current") is None
        assert runtime.store.events() == before
        assert not source.status()["running"]
    finally:
        await source.close()
        await runtime.close()


async def test_generic_source_keeps_original_proof_and_settings_shape(tmp_path: Path) -> None:
    runtime, source, directory = configured(tmp_path)
    try:
        await source.start()
        record = await arrive(runtime, source, directory)
        settings = runtime.store.get("file_inbox_settings", "current")
        assert settings is not None and "project_id" not in settings["settings"]
        assert runtime.store.get("file_inbox_project", "current") is None
        assert set(record["proof"]) == {"id", "source", "scope", "revision", "watcher_generation"}
        assert record["routing"]["classification"] == "independent"
        event = next(item for item in runtime.store.events() if item["type"] == EVENT_TYPE)
        assert read_record(runtime.store, event, source.settings_state()) == record
    finally:
        await source.close()
        await runtime.close()
