"""Same-origin local management API and static workspace."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from muli import __version__
from muli.config import ConfigFile, default_config_path, default_data_dir
from muli.middleware import BodyLimitMiddleware
from muli.permissions import Mode
from muli.runtime import Runtime
from muli.service import UserService
from muli.store import now


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionInput(Input):
    title: str = Field(default="新会话", max_length=120)
    mode: Mode | None = None


class ChatInput(Input):
    content: str = Field(min_length=1, max_length=64000)
    mode: Mode | None = None


class ApprovalInput(Input):
    decision: Literal["approve", "reject"]


class MemoryInput(Input):
    content: str = Field(min_length=1, max_length=12000)
    kind: str = "observation"
    source: str = "user"
    entities: list[str] = Field(default_factory=list, max_length=30)
    relations: list[dict[str, str]] = Field(default_factory=list, max_length=60)
    confidence: float = Field(default=1, ge=0, le=1)


class WorkflowInput(Input):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=32)
    mode: Mode | None = None


class PairInput(Input):
    code: str = Field(min_length=1, max_length=256)
    name: str = Field(default="MuLi", min_length=1, max_length=120)


class ServiceInput(Input):
    action: Literal["install", "start", "restart", "stop", "enable", "disable"]


class ReminderInput(Input):
    title: str = Field(min_length=1, max_length=120)
    due_at: float
    content: str = Field(default="", max_length=4000)
    interval_seconds: int | None = Field(default=None, ge=60, le=31536000)


def create_app(
    config: ConfigFile | None = None, data_dir: Path | None = None, runtime: Runtime | None = None
) -> FastAPI:
    config = config or ConfigFile(default_config_path())
    runtime = runtime or Runtime(config, data_dir or default_data_dir())
    service = UserService(config.path, runtime.data_dir)
    from muli.integrations.manager import IntegrationManager

    integration = IntegrationManager(runtime)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.startup()
        await integration.start()
        try:
            yield
        finally:
            await integration.close()
            await runtime.close()

    app = FastAPI(
        title="MuLi",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.runtime, app.state.integration = runtime, integration
    app.add_middleware(BodyLimitMiddleware)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next: Any) -> Any:
        host = request.url.hostname
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"detail": "无效管理地址"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "管理入口仅接受同源请求"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "不接受跨站请求"}, status_code=403)
        if request.method in {"POST", "PATCH", "PUT", "DELETE"} and request.url.path.startswith(
            "/api/"
        ):
            if request.method != "DELETE" and not request.headers.get(
                "content-type", ""
            ).startswith("application/json"):
                return JSONResponse({"detail": "需要 application/json"}, status_code=415)
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "无效请求长度"}, status_code=400)
        if size > 1_048_576:
            return JSONResponse({"detail": "请求体超过限制"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith("/api/") else "no-cache"
        )
        return response

    @app.exception_handler(ValueError)
    async def bad_input(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)[:1000]}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": "记录不存在"}, status_code=404)

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        settings = config.settings
        return {
            "version": __version__,
            "configured": settings.models.main.configured,
            "model": settings.models.main.model,
            "provider": settings.models.main.protocol,
            "mode": settings.default_mode,
            "running": len(runtime.tasks),
            "platform": sys.platform,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "risk": tool.risk,
                    "enabled": tool.name not in settings.disabled_tools,
                }
                for tool in runtime.tools.values()
            ],
            "computer": await runtime.computer.status(),
            "chiliarch": integration.status(),
            "mcp": runtime.mcp.status(),
            "notifications": runtime.notifications.status(),
            "limits": settings.limits.model_dump(),
        }

    @app.get("/api/settings")
    async def settings() -> dict[str, Any]:
        return config.public()

    @app.patch("/api/settings")
    async def update_settings(changes: dict[str, Any]) -> dict[str, Any]:
        async with runtime.configuration_lock:
            if runtime.busy:
                raise HTTPException(409, "请先停止运行中的任务")
            config.update(changes)
            await integration.close()
            await runtime.reconfigure()
            await integration.start()
            return config.public()

    @app.get("/api/sessions")
    async def sessions() -> dict[str, Any]:
        return {"items": runtime.store.list("session")}

    @app.post("/api/sessions")
    async def new_session(body: SessionInput) -> dict[str, Any]:
        return runtime.new_session(body.title, body.mode)

    @app.get("/api/sessions/{session_id}")
    async def session_detail(session_id: str) -> dict[str, Any]:
        session = runtime.store.get("session", session_id)
        if not session:
            raise KeyError(session_id)
        return {
            "session": session,
            "messages": runtime.store.messages(session_id),
            "events": runtime.store.recent_events(session_id, 3000),
        }

    @app.patch("/api/sessions/{session_id}")
    async def update_session(session_id: str, body: SessionInput) -> dict[str, Any]:
        session = runtime.store.get("session", session_id)
        if not session:
            raise KeyError(session_id)
        if session_id in runtime.tasks:
            raise HTTPException(409, "运行中不能变更会话权限")
        session.update(body.model_dump(exclude_unset=True, exclude_none=True))
        session["updated_at"] = now()
        return runtime.store.put("session", session)

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, bool]:
        await runtime.stop(session_id)
        with runtime.store.db:
            runtime.store.db.execute(
                "DELETE FROM objects WHERE kind IN ('message','run','approval') AND json_extract(data,'$.session_id')=?",
                (session_id,),
            )
            runtime.store.db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
        return {"deleted": runtime.store.delete("session", session_id)}

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: ChatInput) -> dict[str, Any]:
        return await runtime.start(session_id, body.content, body.mode)

    @app.post("/api/sessions/{session_id}/stop")
    async def stop(session_id: str) -> dict[str, bool]:
        await runtime.stop(session_id)
        return {"stopped": True}

    def event_stream(
        request: Request, after: int, session_id: str | None = None
    ) -> StreamingResponse:
        cursor = request.headers.get("last-event-id")
        if cursor:
            try:
                after = max(after, int(cursor))
            except ValueError as exc:
                raise HTTPException(400, "事件游标无效") from exc
        return StreamingResponse(
            runtime.bus.stream(max(0, after), session_id),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get("/api/events")
    async def events(request: Request, after: int = 0) -> StreamingResponse:
        return event_stream(request, after)

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(
        request: Request, session_id: str, after: int = 0
    ) -> StreamingResponse:
        if not runtime.store.get("session", session_id):
            raise KeyError(session_id)
        return event_stream(request, after, session_id)

    @app.get("/api/approvals")
    async def approvals() -> dict[str, Any]:
        return {
            "items": [
                item for item in runtime.store.list("approval") if item["status"] == "pending"
            ]
        }

    @app.post("/api/approvals/{identifier}")
    async def approve(identifier: str, body: ApprovalInput) -> dict[str, bool]:
        await runtime.permissions.resolve(identifier, body.decision)
        return {"resolved": True}

    @app.get("/api/memories")
    async def memories(query: str = "") -> dict[str, Any]:
        return {"items": runtime.memory.search(query, 100)}

    @app.get("/api/memories/graph")
    async def graph() -> dict[str, Any]:
        return runtime.memory.graph()

    @app.get("/api/memories/{identifier}")
    async def memory_detail(identifier: str) -> dict[str, Any]:
        item = runtime.memory.get(identifier)
        if item is None:
            raise KeyError(identifier)
        return item

    @app.get("/api/memories/{identifier}/revisions")
    async def memory_revisions(identifier: str) -> dict[str, Any]:
        if runtime.memory.get(identifier) is None:
            raise KeyError(identifier)
        return {"items": runtime.memory.revisions(identifier)}

    @app.post("/api/memories")
    async def remember(body: MemoryInput) -> dict[str, Any]:
        return runtime.memory.save(**body.model_dump())

    @app.patch("/api/memories/{identifier}")
    async def edit_memory(identifier: str, body: MemoryInput) -> dict[str, Any]:
        return runtime.memory.save(identifier=identifier, **body.model_dump())

    @app.delete("/api/memories/{identifier}")
    async def forget(identifier: str) -> dict[str, bool]:
        return {"deleted": runtime.memory.delete(identifier)}

    @app.get("/api/workflows")
    async def workflows() -> dict[str, Any]:
        definitions = runtime.store.list("workflow")
        runs = runtime.store.list("workflow_run")
        return {
            "items": [
                {**item, "runs": [run for run in runs if run.get("workflow_id") == item["id"]]}
                for item in definitions
            ],
            "runs": runs,
        }

    @app.post("/api/workflows")
    async def create_workflow(body: WorkflowInput) -> dict[str, Any]:
        return runtime.workflows.create(
            body.name, body.steps, body.description, body.mode or config.settings.default_mode
        )

    @app.post("/api/workflows/{identifier}/run")
    async def run_workflow(identifier: str) -> dict[str, Any]:
        async with runtime.configuration_lock:
            runtime.model_config("main")
            return await runtime.workflows.start(identifier)

    @app.post("/api/workflow-runs/{identifier}/cancel")
    async def cancel_workflow(identifier: str) -> dict[str, Any]:
        return await runtime.workflows.cancel(identifier)

    @app.delete("/api/workflows/{identifier}")
    async def delete_workflow(identifier: str) -> dict[str, bool]:
        if any(
            run.get("workflow_id") == identifier and run.get("status") in {"queued", "running"}
            for run in runtime.store.list("workflow_run")
        ):
            raise HTTPException(409, "工作流仍在运行")
        return {"deleted": runtime.store.delete("workflow", identifier)}

    @app.get("/api/notifications")
    async def notifications() -> dict[str, Any]:
        return {"items": runtime.store.list("notification")}

    @app.get("/api/reminders")
    async def reminders() -> dict[str, Any]:
        return {"items": runtime.store.list("reminder")}

    @app.post("/api/reminders")
    async def create_reminder(body: ReminderInput) -> dict[str, Any]:
        item = runtime.reminders.create(**body.model_dump())
        await runtime.bus.emit("reminder.updated", item)
        return item

    @app.patch("/api/reminders/{identifier}")
    async def edit_reminder(identifier: str, changes: dict[str, Any]) -> dict[str, Any]:
        item = runtime.reminders.update(identifier, changes)
        await runtime.bus.emit("reminder.updated", item)
        return item

    @app.delete("/api/reminders/{identifier}")
    async def delete_reminder(identifier: str) -> dict[str, bool]:
        return {"deleted": runtime.reminders.delete(identifier)}

    @app.patch("/api/notifications/{identifier}")
    async def notification_read(identifier: str, body: dict[str, bool]) -> dict[str, Any]:
        item = runtime.store.get("notification", identifier)
        if not item:
            raise KeyError(identifier)
        item["read"] = body.get("read", True)
        return runtime.store.put("notification", item)

    @app.post("/api/integrations/chiliarch/pair")
    async def pair(body: PairInput) -> dict[str, Any]:
        async with runtime.configuration_lock:
            return await integration.pair(body.code, body.name)

    @app.get("/api/service")
    async def service_status() -> dict[str, Any]:
        return await service.status()

    @app.post("/api/service")
    async def service_action(body: ServiceInput) -> dict[str, Any]:
        return await service.action(body.action)

    @app.get("/api/export")
    async def export() -> StreamingResponse:
        async def records() -> AsyncIterator[str]:
            yield (
                json.dumps(
                    {"kind": "metadata", "version": __version__, "created_at": now()},
                    ensure_ascii=False,
                )
                + "\n"
            )
            for table in ("objects", "memories", "relations", "memory_revisions"):
                cursor = runtime.store.db.execute(f"SELECT * FROM {table}")
                try:
                    while rows := cursor.fetchmany(100):
                        for row in rows:
                            yield (
                                json.dumps(
                                    {"table": table, "record": dict(row)}, ensure_ascii=False
                                )
                                + "\n"
                            )
                        await asyncio.sleep(0)
                finally:
                    cursor.close()

        return StreamingResponse(
            records(),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="muli-export.jsonl"'},
        )

    source_web = Path(__file__).resolve().parents[2] / "web"
    packaged_web = Path(__file__).with_name("web")
    web = packaged_web if packaged_web.exists() else source_web

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs() -> FileResponse:
        return FileResponse(web / "api-docs.html")

    if web.exists():
        app.mount("/", StaticFiles(directory=web, html=True), name="workspace")
    return app
