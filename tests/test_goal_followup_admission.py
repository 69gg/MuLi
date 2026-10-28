"""Finite revocation prefixes through real reminder APIs and Attention admission."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal, NoReturn

import httpx
import pytest
from fastapi import FastAPI
from test_attention import attention_inputs, eventually
from test_reminders import fire_due
from test_runtime import make_runtime

from muli.providers import Message, ModelTurn
from muli.reminder_followup import read_followup_notice
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import now
from muli.system_events import attention_source


@pytest.fixture
async def application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Runtime, FastAPI, httpx.AsyncClient, list[list[Message]]]]:
    async def offline(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("复盘接纳负控不允许外部 HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline)
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="普通有效提醒仍正常说明")])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": True, "events": ["reminder"], "coalesce_seconds": 0},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
        }
    )
    await runtime.reconfigure()
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            yield runtime, app, web, seen
    finally:
        await runtime.close()


def notice_event(runtime: Runtime, identifier: str) -> dict[str, Any]:
    return next(
        event
        for event in runtime.store.events()
        if event["type"] == "notification.created" and event["data"]["id"] == identifier
    )


@pytest.mark.parametrize(
    "change", ["disable_same", "edit", "delete", "unbind_rebind", "done", "cancelled"]
)
async def test_explicit_change_revokes_the_notice_at_final_admission(
    application: tuple[Runtime, FastAPI, httpx.AsyncClient, list[list[Message]]],
    monkeypatch: pytest.MonkeyPatch,
    change: Literal["disable_same", "edit", "delete", "unbind_rebind", "done", "cancelled"],
) -> None:
    runtime, app, web, seen = application
    task = await app.state.local_services.commitments.save(
        "只读复盘目标", brief={"goal": "说明已有依据", "constraints": ["不执行任务"]}
    )
    created = await web.post(
        "/api/reminders",
        json={
            "title": "必须撤销的旧复盘",
            "content": "不应进入主会话",
            "due_at": now() + 60,
            "commitment_id": task["id"],
        },
    )
    assert created.status_code == 200
    item = created.json()
    attempted = asyncio.Event()
    original = runtime.submit_attention

    async def observed(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        attempted.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime, "submit_attention", observed)
    async with runtime.configuration_lock:
        await fire_due(runtime.reminders, item)
        identifier = f"reminder:{item['id']}:{item['due_at']}"
        event = notice_event(runtime, identifier)
        await asyncio.wait_for(attempted.wait(), 3)
        assert read_followup_notice(runtime.store, identifier, 6000) is not None
        assert runtime.store.get("reminder", item["id"])["enabled"] is False
        assert not attention_inputs(runtime)
        if change == "delete":
            response = await web.delete(f"/api/reminders/{item['id']}")
            assert response.status_code == 200 and response.json()["deleted"] is True
        elif change in {"done", "cancelled"}:
            await app.state.local_services.commitments.save(
                task["title"], identifier=task["id"], status=change, evidence="测试中明确终止跟进"
            )
        else:
            changes: dict[str, Any] = (
                {"commitment_id": None}
                if change == "unbind_rebind"
                else {"content": "新的约定文案"}
                if change == "edit"
                else {"enabled": False}
            )
            response = await web.patch(f"/api/reminders/{item['id']}", json=changes)
            assert response.status_code == 200
            assert response.json()["followup_revision"] == 2
            if change == "unbind_rebind":
                assert "followup" not in response.json()
                response = await web.patch(
                    f"/api/reminders/{item['id']}", json={"commitment_id": task["id"]}
                )
                assert response.status_code == 200
                assert response.json()["followup"] == {
                    "version": 1,
                    "commitment_id": task["id"],
                    "revision": 3,
                }
        assert attention_source(runtime.store, event, ["reminder"], max_output_chars=6000) is None
        assert read_followup_notice(runtime.store, identifier, 6000) is None
    await eventually(lambda: app.state.attention.cursor >= event["id"])
    assert not seen and not attention_inputs(runtime)
    assert runtime.store.list("attention_receipt") == []
    assert app.state.attention.last_error is None
    # A rejected old proof must not strand the consumer or an unrelated reminder.
    ordinary_response = await web.post(
        "/api/reminders",
        json={
            "title": "仍有效的普通提醒",
            "due_at": now() + 60,
        },
    )
    ordinary = ordinary_response.json()
    assert ordinary_response.status_code == 200
    assert "followup" not in ordinary and "followup_revision" not in ordinary
    await fire_due(runtime.reminders, ordinary)
    await eventually(lambda: len(seen) == 1 and not runtime.tasks)
    assert "仍有效的普通提醒" in seen[0][-1].content
    assert "必须撤销的旧复盘" not in seen[0][-1].content
    assert len(attention_inputs(runtime)) == 1
    assert len(runtime.store.list("attention_receipt")) == 1


async def test_stale_scheduler_snapshot_cannot_mix_old_text_with_new_binding(
    application: tuple[Runtime, FastAPI, httpx.AsyncClient, list[list[Message]]],
) -> None:
    runtime, app, web, _ = application
    await app.state.attention.close()
    first = await app.state.local_services.commitments.save("旧目标")
    second = await app.state.local_services.commitments.save("新目标")
    response = await web.post(
        "/api/reminders",
        json={
            "title": "旧文案",
            "content": "旧约定",
            "due_at": now() + 60,
            "commitment_id": first["id"],
        },
    )
    assert response.status_code == 200
    stale = response.json()
    response = await web.patch(
        f"/api/reminders/{stale['id']}",
        json={
            "title": "新文案",
            "content": "新约定",
            "interval_seconds": 60,
            "commitment_id": second["id"],
        },
    )
    assert response.status_code == 200
    current = response.json()
    # Pass the actual earlier scheduler snapshot through its normal admitted path.
    await fire_due(runtime.reminders, stale)
    identifier = f"reminder:{stale['id']}:{stale['due_at']}"
    notice = runtime.store.get("notification", identifier)
    assert notice["title"] == "新文案" and notice["content"] == "新约定"
    assert notice["followup"] == current["followup"]
    updated = runtime.store.get("reminder", stale["id"])
    assert updated["enabled"] is True and updated["due_at"] == stale["due_at"] + 60
    assert updated["followup_revision"] == 2
    assert read_followup_notice(runtime.store, identifier, 6000) is not None


async def test_natural_recurrence_and_task_revision_do_not_revoke_earlier_occurrence(
    application: tuple[Runtime, FastAPI, httpx.AsyncClient, list[list[Message]]],
) -> None:
    runtime, app, web, _ = application
    await app.state.attention.close()
    task = await app.state.local_services.commitments.save(
        "可更新的目标", brief={"goal": "旧要求", "constraints": []}
    )
    response = await web.post(
        "/api/reminders",
        json={
            "title": "周期复盘",
            "due_at": now() + 60,
            "interval_seconds": 60,
            "commitment_id": task["id"],
        },
    )
    assert response.status_code == 200
    first = response.json()
    await fire_due(runtime.reminders, first)
    identifier = f"reminder:{first['id']}:{first['due_at']}"
    proof = read_followup_notice(runtime.store, identifier, 6000)
    assert proof is not None
    await app.state.local_services.commitments.save(
        task["title"], identifier=task["id"], brief={"goal": "新要求", "constraints": []}
    )
    second = runtime.store.get("reminder", first["id"])
    await fire_due(runtime.reminders, second)
    assert read_followup_notice(runtime.store, identifier, 6000) == proof
    later = f"reminder:{second['id']}:{second['due_at']}"
    later_proof = read_followup_notice(runtime.store, later, 6000)
    assert later_proof is not None and later_proof.occurrence == 2
    stored = runtime.store.get("reminder", first["id"])
    assert stored["followup_revision"] == 1 and stored["followup"] == first["followup"]
    assert app.state.local_services.commitments.get(task["id"])["revision"] == 2


async def test_task_lookup_and_host_binding_fields_are_not_caller_controlled(
    application: tuple[Runtime, FastAPI, httpx.AsyncClient, list[list[Message]]],
) -> None:
    runtime, app, web, _ = application
    base = {"title": "准确关联", "due_at": now() + 60}
    missing = await web.post("/api/reminders", json={**base, "commitment_id": "missing-task"})
    assert missing.status_code == 404 and not runtime.store.list("reminder")
    task = await app.state.local_services.commitments.save("真实目标")
    forged = await web.post(
        "/api/reminders",
        json={
            **base,
            "commitment_id": task["id"],
            "followup_revision": 9,
        },
    )
    assert forged.status_code == 422 and not runtime.store.list("reminder")
    response = await web.post("/api/reminders", json={**base, "commitment_id": task["id"]})
    assert response.status_code == 200
    item = response.json()
    forged_edit = await web.patch(f"/api/reminders/{item['id']}", json={"followup_revision": 9})
    assert forged_edit.status_code == 400
    assert runtime.store.get("reminder", item["id"]) == item


async def test_damaged_followup_notice_cannot_fall_back_to_an_ordinary_reminder(
    application: tuple[Runtime, FastAPI, httpx.AsyncClient, list[list[Message]]],
) -> None:
    runtime, app, web, seen = application
    await app.state.attention.close()
    task = await app.state.local_services.commitments.save("不可降级的目标")
    response = await web.post(
        "/api/reminders",
        json={
            "title": "损坏绑定不能普通提醒",
            "due_at": now() + 60,
            "commitment_id": task["id"],
        },
    )
    assert response.status_code == 200
    item = response.json()
    await fire_due(runtime.reminders, item)
    identifier = f"reminder:{item['id']}:{item['due_at']}"
    assert read_followup_notice(runtime.store, identifier, 6000) is not None
    # Honest reader corruption: change only the binding version of an actual notice.
    notice = runtime.store.get("notification", identifier)
    notice["followup"]["version"] = 2
    runtime.store.put("notification", notice)
    event = await runtime.bus.emit("notification.created", notice)
    assert attention_source(runtime.store, event, ["reminder"], max_output_chars=6000) is None
    assert read_followup_notice(runtime.store, identifier, 6000) is None
    await app.state.attention.start()
    await eventually(lambda: app.state.attention.cursor >= event["id"])
    assert not seen and not attention_inputs(runtime)
    assert runtime.store.list("attention_receipt") == []
