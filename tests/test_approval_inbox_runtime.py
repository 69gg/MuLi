"""Real live approvals across two authenticated clients and normal Store reopen.

The model has a fixed tool policy. Runtime, Team, permission waiters, ASGI routes
and filesystem effects are real; no approval, receipt or execution is invented.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attention import eventually as settled
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app


async def test_global_live_approvals_resolve_exact_workers_and_do_not_revive_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("approval journey permits only the local ASGI transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "ask",
            "server": {"password": "synthetic-approval-owner"},
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "limits": {
                "max_parallel_agents": 3,
                "max_pending_runs": 3,
                "max_context_chars": 16000,
                "max_output_chars": 6000,
            },
        }
    )
    await runtime.reconfigure()
    labels = ("approved", "rejected", "stopped", "closed")
    paths = {label: tmp_path / f"{label}.txt" for label in labels}
    agents: dict[str, str] = {}
    worker_rounds = dict.fromkeys(labels, 0)
    assigned: set[str] = set()
    main_id = str(runtime.main_session()["id"])

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            assert len(messages) <= 32
            names = {tool["function"]["name"] for tool in tools}
            requested = next(
                (
                    label
                    for label in ("initial", "stopped", "closed")
                    if label not in assigned
                    and any(
                        message.role == "user" and message.content == f"OWNER-ASSIGN-{label}"
                        for message in messages
                    )
                ),
                None,
            )
            if requested is not None:
                assert "spawn_agent" in names
                assigned.add(requested)
                selected = ("approved", "rejected") if requested == "initial" else (requested,)
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            f"spawn-{label}",
                            "spawn_agent",
                            task_name=f"真实审批-{label}",
                            prompt=f"WORKER-{label}：只在该次工具被批准后写入指定合成文件。",
                        )
                        for label in selected
                    ]
                )
            else:
                for label in labels:
                    if any(message.tool_call_id == f"spawn-{label}" for message in messages):
                        result = tool_result(messages, f"spawn-{label}")
                        assert "error" not in result
                        agents[label] = str(result["id"])
                turn = ModelTurn(content="主助手已按真实派工或报告结果收口。")
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            assert len(messages) <= 32
            label = next(
                label
                for label in labels
                if any(f"WORKER-{label}" in message.content for message in messages)
            )
            worker_rounds[label] += 1
            assert worker_rounds[label] <= 2
            if worker_rounds[label] == 1:
                assert "fs_write" in {tool["function"]["name"] for tool in tools}
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            f"write-{label}",
                            "fs_write",
                            path=str(paths[label]),
                            content=f"真实工具写入：{label}",
                        )
                    ]
                )
            else:
                result = tool_result(messages, f"write-{label}")
                if label == "approved":
                    assert "error" not in result
                else:
                    assert label == "rejected" and "error" in result
                turn = ModelTurn(content=f"{label}：该轮已经按真实批准结果结束。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model in {"main", "worker", "reviewer"}
        return Main([], []) if config.model == "main" else Worker([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    historical_ids: list[str] = []

    async def approve_spawns(client: httpx.AsyncClient, count: int) -> None:
        processed: set[str] = set()
        for _ in range(count):
            await eventually(
                runtime,
                lambda: any(
                    item["tool"] == "spawn_agent"
                    and item["session_id"] == main_id
                    and item["id"] not in processed
                    for item in runtime.live_approvals()
                ),
            )
            item = next(
                item
                for item in runtime.live_approvals()
                if item["tool"] == "spawn_agent" and item["session_id"] == main_id
            )
            processed.add(item["id"])
            response = await client.post(
                f"/api/approvals/{item['id']}", json={"decision": "approve"}
            )
            assert response.status_code == 200 and response.json() == {"resolved": True}

    async def assign(client: httpx.AsyncClient, label: str, count: int) -> None:
        response = await client.post(
            f"/api/sessions/{main_id}/messages", json={"content": f"OWNER-ASSIGN-{label}"}
        )
        assert response.status_code == 200
        receipt = response.json()
        await approve_spawns(client, count)
        async with asyncio.timeout(3):
            await runtime.wait(receipt["run_id"])
        assert runtime.store.get("run", receipt["run_id"])["status"] == "completed"

    def started(label: str, active: Runtime) -> list[dict[str, Any]]:
        return [
            event
            for event in active.store.events(session_id=agents[label])
            if event["type"] == "tool.started" and event["data"]["call_id"] == f"write-{label}"
        ]

    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as first,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as second,
        ):
            assert (await second.get("/api/approvals")).status_code == 401
            for client in (first, second):
                login = await client.post(
                    "/api/auth/login", json={"password": "synthetic-approval-owner"}
                )
                assert login.status_code == 200
            patched = await first.patch(f"/api/sessions/{main_id}", json={"mode": "ask"})
            assert patched.status_code == 200 and patched.json()["mode"] == "ask"
            await assign(first, "initial", 2)
            await eventually(
                runtime,
                lambda: (
                    len([item for item in runtime.live_approvals() if item["tool"] == "fs_write"])
                    == 2
                ),
            )
            response = await second.get("/api/approvals")
            assert response.status_code == 200
            pending = response.json()["items"]
            assert len(pending) == 2
            by_label = {
                label: next(item for item in pending if item["session_id"] == agents[label])
                for label in ("approved", "rejected")
            }
            for label, item in by_label.items():
                assert item["status"] == "pending" and item["tool"] == "fs_write"
                assert item["run_id"] == runtime.run_ids[agents[label]]
                assert runtime.store.get("session", agents[label])["mode"] == "ask"
                assert item["arguments"]["path"] == str(paths[label])
                assert not paths[label].exists() and started(label, runtime) == []
                historical_ids.append(item["id"])
            for label, decision in (("approved", "approve"), ("rejected", "reject")):
                item = by_label[label]
                resolved = await second.post(
                    f"/api/approvals/{item['id']}", json={"decision": decision}
                )
                assert resolved.status_code == 200
                remaining = (await first.get("/api/approvals")).json()["items"]
                assert all(entry["id"] != item["id"] for entry in remaining)
                duplicate = await first.post(
                    f"/api/approvals/{item['id']}", json={"decision": "approve"}
                )
                assert duplicate.status_code == 400
            await eventually(
                runtime,
                lambda: all(
                    runtime.team.get(agents[label])["status"] == "completed"
                    for label in ("approved", "rejected")
                ),
            )
            assert paths["approved"].read_text(encoding="utf-8") == "真实工具写入：approved"
            assert not paths["rejected"].exists()
            assert len(started("approved", runtime)) == 1 and started("rejected", runtime) == []
            assert (await second.get("/api/approvals")).json() == {"items": []}

            for label in ("stopped", "closed"):
                await settled(lambda: runtime.team.idle)
                await runtime.team.flush_reports()
                await settled(lambda: main_id not in runtime.tasks)
                await assign(first, label, 1)
                await eventually(
                    runtime,
                    lambda selected=label: any(
                        item["tool"] == "fs_write" and item["session_id"] == agents[selected]
                        for item in runtime.live_approvals()
                    ),
                )
                item = (await second.get("/api/approvals")).json()["items"]
                assert len(item) == 1 and item[0]["session_id"] == agents[label]
                historical_ids.append(item[0]["id"])
                if label == "stopped":
                    stopped = await first.post(f"/api/team/{agents[label]}/stop", json={})
                    assert stopped.status_code == 200
                    assert (await second.get("/api/approvals")).json() == {"items": []}
                    stale = await second.post(
                        f"/api/approvals/{item[0]['id']}", json={"decision": "approve"}
                    )
                    assert stale.status_code == 400
                    assert not paths[label].exists() and started(label, runtime) == []
            # Exit the real app lifespan while the fourth worker truly waits for
            # approval. Normal close cancels its waiter; no state is fabricated.
            assert runtime.permissions.pending and not paths["closed"].exists()
    finally:
        await runtime.close()

    calls_after_reopen = 0

    def no_model(config: ModelConfig) -> FakeClient:
        nonlocal calls_after_reopen
        calls_after_reopen += 1
        raise AssertionError("normal reopen must not replay the old approval or worker")

    reopened = Runtime(ConfigFile(runtime.config.path), runtime.data_dir, client_factory=no_model)
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    try:
        async with (
            reopened_app.router.lifespan_context(reopened_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=reopened_app), base_url="http://127.0.0.1:8765"
            ) as connection,
        ):
            assert (
                await connection.post(
                    "/api/auth/login", json={"password": "synthetic-approval-owner"}
                )
            ).status_code == 200
            assert (await connection.get("/api/approvals")).json() == {"items": []}
            assert reopened.main_session()["id"] == main_id
            assert not reopened.permissions.pending
            for identifier in historical_ids:
                item = reopened.store.get("approval", identifier)
                assert item is not None and item["status"] != "pending"
                stale = await connection.post(
                    f"/api/approvals/{identifier}", json={"decision": "approve"}
                )
                assert stale.status_code == 400
            assert any(
                event["type"] == "approval.requested"
                for event in reopened.store.events(session_id=agents["closed"])
            )
            assert paths["approved"].read_text(encoding="utf-8") == "真实工具写入：approved"
            assert all(not paths[label].exists() for label in ("rejected", "stopped", "closed"))
            assert len(started("approved", reopened)) == 1
            assert all(
                started(label, reopened) == [] for label in ("rejected", "stopped", "closed")
            )
            assert calls_after_reopen == 0
    finally:
        await reopened.close()
