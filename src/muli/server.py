"""Same-origin local management API and static workspace."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from muli import __version__
from muli.attachments import SAFE_INLINE, Attachments
from muli.attention import AttentionInbox
from muli.auth import COOKIE_NAME, Authentication
from muli.channel_application import ChannelApplication
from muli.channels import ChannelError
from muli.config import ConfigFile, Settings, default_config_path, default_data_dir
from muli.local_services import LocalServices
from muli.management import SelfManagement
from muli.memory_application import MemoryApplication
from muli.middleware import BodyLimitMiddleware
from muli.permissions import Mode
from muli.personal_io import PersonalIO
from muli.plugins import PluginManager
from muli.providers import ProviderError
from muli.runtime import Runtime
from muli.service import UserService
from muli.store import now
from muli.web_voice import WebVoiceError, WebVoiceInput


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionInput(Input):
    title: str = Field(default="新会话", max_length=120)
    mode: Mode | None = None


class ChatInput(Input):
    content: str = Field(default="", max_length=64000)
    mode: Mode | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=32)


class TranscriptionInput(Input):
    attachment_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class LoginInput(Input):
    password: str = Field(min_length=1, max_length=1024)


class TeamMessageInput(Input):
    content: str = Field(min_length=1, max_length=64000)


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
    steps: list[dict[str, Any]] = Field(min_length=1)
    mode: Mode | None = None


class PairInput(Input):
    code: str = Field(min_length=1, max_length=256)
    name: str = Field(default="MuLi", min_length=1, max_length=120)


class ServiceInput(Input):
    action: Literal["install", "start", "restart", "stop", "enable", "disable"]


class DeliveryRetryInput(Input):
    allow_unknown: bool = False


class ReminderInput(Input):
    title: str = Field(min_length=1, max_length=120)
    due_at: float
    content: str = Field(default="", max_length=4000)
    interval_seconds: int | None = Field(default=None, ge=60, le=31536000)


def create_app(
    config: ConfigFile | None = None,
    data_dir: Path | None = None,
    runtime: Runtime | None = None,
    *,
    listen_port: int | None = None,
) -> FastAPI:
    config = config or ConfigFile(default_config_path())
    bound_host, bound_port = config.settings.server.host, listen_port or config.settings.server.port
    bound_loopback = bound_host == "localhost" or ipaddress.ip_address(bound_host).is_loopback
    runtime = runtime or Runtime(config, data_dir or default_data_dir())
    attention = AttentionInbox(runtime)
    service = UserService(config.path, runtime.data_dir)
    authentication = Authentication(config)
    attachments = Attachments(runtime.store, runtime.data_dir / "attachments", config)
    local_services = LocalServices(runtime, attachments)
    web_voice = WebVoiceInput(local_services)
    memory_application = MemoryApplication(runtime, local_services.media)
    channel_application = ChannelApplication(runtime, attachments)
    personal_io = PersonalIO(runtime, local_services, attachments)
    from muli.integrations.manager import IntegrationManager

    integration = IntegrationManager(runtime)

    async def apply_configuration(target: Settings) -> None:
        if not bound_loopback and not target.server.password_hash:
            raise ValueError(
                "当前进程仍监听非回环地址。请先保留密码、改为回环监听并重启，然后再清除密码"
            )
        previous = config.settings.model_copy(deep=True)
        await attention.close()
        await web_voice.aclose()
        await integration.close()
        await channel_application.close()
        await personal_io.close()
        await memory_application.close()
        try:
            config.replace(target)
            await runtime.reconfigure()
            await memory_application.reconfigure()
            await channel_application.reconfigure()
            await personal_io.reconfigure()
            await integration.start()
            await attention.reconfigure()
            await web_voice.reconfigure()
        except Exception:
            config.replace(previous)
            await runtime.reconfigure()
            await memory_application.reconfigure()
            await channel_application.reconfigure()
            await personal_io.reconfigure()
            await integration.start()
            await attention.reconfigure()
            await web_voice.reconfigure()
            raise

    async def onboarding() -> None:
        if config.settings.onboarding_pending and config.settings.models.main.configured:
            await runtime.submit(
                "请先和我打个招呼，并带我逐步完善配置。先询问我最希望你帮忙的事情，再按实际需要配置"
                "记忆模型、视觉、搜索、其它 agent、chiliarch 配对、通讯渠道和语音。一次只处理少量问题，"
                "不要假装尚未配置的连接可用。你可以在征求我的偏好后完善自己的自我介绍。",
                source="onboarding",
                event_id="first-setup-v2",
            )
            config.update({"onboarding_pending": False})

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await plugins.load()
            await runtime.startup()
            await attention.start()
            await memory_application.start()
            await channel_application.start()
            await personal_io.start()
            await integration.start()
            await management.start()
            await onboarding()
            yield
        finally:
            await attention.close()
            await web_voice.aclose()
            await management.close()
            await integration.close()
            await channel_application.close()
            await personal_io.close()
            await memory_application.close()
            await plugins.close()
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
    app.state.attachments, app.state.authentication = attachments, authentication
    app.state.local_services = local_services
    app.state.web_voice = web_voice
    plugins = PluginManager(runtime, app, config.path.resolve().parent / "plugins")
    management = SelfManagement(runtime, plugins, service, apply_configuration)
    app.state.management, app.state.plugins = management, plugins
    app.state.memory_application = memory_application
    app.state.attention = attention
    app.state.channel_application, app.state.personal_io = channel_application, personal_io
    app.add_middleware(BodyLimitMiddleware)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next: Any) -> Any:
        host = request.url.hostname
        allowed_hosts = {"localhost", "127.0.0.1", "::1", *config.settings.server.allowed_hosts}
        if not bound_loopback:
            try:
                if host:
                    ipaddress.ip_address(host)
                    allowed_hosts.add(host)
            except ValueError:
                pass
        if host not in allowed_hosts:
            return JSONResponse({"detail": "无效管理地址"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "管理入口仅接受同源请求"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "不接受跨站请求"}, status_code=403)
        if request.method in {"POST", "PATCH", "PUT", "DELETE"} and request.url.path.startswith(
            "/api/"
        ):
            if (
                request.url.path != "/api/uploads"
                and request.method != "DELETE"
                and not request.headers.get("content-type", "").startswith("application/json")
            ):
                return JSONResponse({"detail": "需要 application/json"}, status_code=415)
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "无效请求长度"}, status_code=400)
        upload = request.url.path == "/api/uploads" and request.method == "POST"
        body_limit = config.settings.uploads.max_file_bytes if upload else 1_048_576
        if size < 0 or size > body_limit:
            return JSONResponse({"detail": "请求体超过限制"}, status_code=413)
        signed_webhook = request.method == "POST" and request.url.path in {
            "/api/channels/onebot/webhook",
            "/api/channels/qq/webhook",
        }
        if (
            request.url.path.startswith("/api/")
            and request.url.path
            not in {
                "/api/auth/status",
                "/api/auth/login",
            }
            and not signed_webhook
            and not authentication.authenticated(request.cookies.get(COOKIE_NAME, ""))
        ):
            return JSONResponse({"detail": "需要登录"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
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

    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> dict[str, bool]:
        return {
            "required": authentication.required,
            "authenticated": authentication.authenticated(request.cookies.get(COOKIE_NAME, "")),
        }

    @app.post("/api/auth/login")
    async def login(request: Request, body: LoginInput) -> JSONResponse:
        try:
            token = await authentication.login(body.password)
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(429, str(exc)) from exc
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            samesite="strict",
            secure=config.settings.server.secure_cookie or request.url.scheme == "https",
            max_age=config.settings.server.session_ttl_seconds,
        )
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        authentication.logout(request.cookies.get(COOKIE_NAME, ""))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE_NAME)
        return response

    def decorated_message(message: dict[str, Any]) -> dict[str, Any]:
        files = []
        for identifier in message.get("attachment_ids", []):
            try:
                files.append(attachments.public(attachments.get(identifier)))
            except KeyError:
                continue
        return {**message, "attachments": files}

    def visible_messages(session_id: str) -> list[dict[str, Any]]:
        history = runtime.store.messages(session_id)
        delivered = {item["id"] for item in history}
        pending = runtime.conversation.pending(session_id)
        return [
            decorated_message(item)
            for item in [
                *history,
                *(item for item in pending if item["id"] not in delivered),
            ]
        ]

    @app.get("/api/uploads")
    async def list_uploads() -> dict[str, Any]:
        return {"items": attachments.list(), "used_bytes": attachments.used_bytes()}

    @app.get("/api/media")
    async def media_status() -> dict[str, Any]:
        async with local_services.media() as media:
            return media.status()

    @app.post("/api/media/transcribe")
    async def transcribe_audio(body: TranscriptionInput, request: Request) -> dict[str, Any]:
        """转写本地音频，只返回待用户编辑与发送的草稿，不提交对话。"""

        async def disconnected() -> None:
            # FastAPI has already consumed the JSON body. Await the ASGI event;
            # do not periodically poll or leave a paid request behind an abort.
            while (await request.receive())["type"] != "http.disconnect":
                pass

        operation = asyncio.create_task(web_voice.transcribe(body.attachment_id))
        disconnect = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait(
                (operation, disconnect), return_when=asyncio.FIRST_COMPLETED
            )
            if disconnect in done:
                raise HTTPException(499, "转写已取消")
            return await operation
        except WebVoiceError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(504, "转写达到时间上限") from exc
        finally:
            operation.cancel()
            disconnect.cancel()
            cleanup = asyncio.gather(operation, disconnect, return_exceptions=True)
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError

    @app.get("/api/commitments")
    async def commitments(status: str = "active") -> dict[str, Any]:
        return {"items": local_services.commitments.list(status)}

    @app.post("/api/uploads")
    async def upload(request: Request) -> dict[str, Any]:
        filename = unquote(request.headers.get("x-file-name", ""), errors="strict")
        try:
            return await attachments.save(
                request.stream(), filename, request.headers.get("content-type", "")
            )
        except ValueError as exc:
            if "限制" in str(exc) or "配额" in str(exc):
                raise HTTPException(413, str(exc)) from exc
            raise

    @app.get("/api/uploads/{identifier}/download")
    @app.get("/api/uploads/{identifier}/content")
    async def attachment_content(identifier: str, request: Request) -> FileResponse:
        item = attachments.get(identifier)
        inline = request.url.path.endswith("/content") and item["mime_type"] in SAFE_INLINE
        return FileResponse(
            attachments.path(identifier),
            filename=item["name"],
            media_type=item["mime_type"] if inline else "application/octet-stream",
            content_disposition_type="inline" if inline else "attachment",
            headers={"Content-Security-Policy": "sandbox; default-src 'none'"},
        )

    @app.get("/api/conversation")
    async def conversation() -> dict[str, Any]:
        session = runtime.main_session()
        return {
            "session": session,
            "messages": visible_messages(session["id"]),
            "events": runtime.store.recent_events(session["id"]),
        }

    @app.get("/api/team")
    async def team(
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        active_only: bool = False,
    ) -> dict[str, Any]:
        page = runtime.team.page(limit=limit, offset=offset, active_only=active_only)
        members = page["members"]
        messages: list[dict[str, Any]] = []
        for member in members:
            history = runtime.store.message_previews(member["session_id"])
            if history:
                member["last_message"] = history[-1]["content"]
                member["last_message_truncated"] = history[-1]["truncated"]
            for item in history:
                messages.append(
                    {
                        **item,
                        "agent_id": member["id"],
                        "from_name": member["name"],
                    }
                )
        return {**page, "messages": sorted(messages, key=lambda item: item["seq"])[-100:]}

    @app.post("/api/team/{identifier}/messages")
    async def team_message(identifier: str, body: TeamMessageInput) -> dict[str, Any]:
        async with runtime.configuration_lock:
            return await runtime.team.send(identifier, body.content)

    @app.post("/api/team/{identifier}/stop")
    async def stop_agent(identifier: str) -> dict[str, Any]:
        return await runtime.team.stop(identifier)

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
            "listener": {
                "host": bound_host,
                "port": bound_port,
                "restart_required": config.settings.server.host != bound_host
                or config.settings.server.port != bound_port,
            },
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "risk": tool.risk,
                    "requires_approval": tool.requires_approval,
                    "enabled": tool.name not in settings.disabled_tools,
                }
                for tool in runtime.tools.values()
            ],
            "computer": await runtime.computer.status(),
            "chiliarch": integration.status(),
            "mcp": runtime.mcp.status(),
            "notifications": runtime.notifications.status(),
            "channels": channel_application.manager.status(),
            "capture": personal_io.capture.status(),
            "voice": personal_io.voice.status(),
            "web_voice": web_voice.status(),
            "attention": attention.status(),
            "limits": settings.limits.model_dump(),
        }

    @app.get("/api/settings")
    async def settings() -> dict[str, Any]:
        return config.public()

    @app.get("/api/settings/schema")
    async def settings_schema() -> dict[str, Any]:
        return Settings.model_json_schema()

    @app.get("/api/management")
    async def management_status() -> dict[str, Any]:
        return {
            "plans": management.plans(),
            "plugins": plugins.inventory(),
            "plugins_restart_required": plugins.restart_required,
            "update": runtime.store.get("update", "latest"),
        }

    @app.post("/api/management/{identifier}/cancel")
    async def cancel_management(identifier: str) -> dict[str, Any]:
        return await management.cancel(identifier)

    @app.get("/api/memory/status")
    async def memory_status() -> dict[str, Any]:
        return memory_application.pipeline.status()

    @app.get("/api/channels")
    async def channel_status() -> dict[str, Any]:
        return {
            **channel_application.manager.status(),
            "deliveries": channel_application.manager.deliveries(),
        }

    @app.post("/api/channels/{name}/webhook")
    async def channel_webhook(name: str, request: Request) -> dict[str, Any]:
        if name not in {"onebot", "qq"}:
            raise HTTPException(404, "该渠道不使用此webhook入口")
        try:
            return await channel_application.manager.webhook(
                name, await request.body(), request.headers
            )
        except ChannelError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    @app.post("/api/channels/deliveries/{identifier}/retry")
    async def retry_delivery(identifier: str, body: DeliveryRetryInput) -> dict[str, Any]:
        async with runtime.configuration_lock:
            return await channel_application.manager.retry(
                identifier, allow_unknown=body.allow_unknown
            )

    @app.get("/api/capture")
    async def capture_status() -> dict[str, Any]:
        return await personal_io.capture_status()

    @app.get("/api/voice")
    async def voice_status() -> dict[str, Any]:
        return personal_io.voice.status()

    @app.get("/api/memory/query")
    async def memory_query(query: str, limit: int = 8) -> dict[str, Any]:
        return await memory_application.pipeline.query(query, limit=min(max(1, limit), 100))

    @app.patch("/api/settings")
    async def update_settings(changes: dict[str, Any]) -> dict[str, Any]:
        async with runtime.configuration_lock:
            if runtime.busy:
                raise HTTPException(409, "请先停止运行中的任务")
            await apply_configuration(config.validate_changes(changes))
        await onboarding()
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
            "messages": [decorated_message(item) for item in runtime.store.messages(session_id)],
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
        if session_id == runtime.main_session()["id"]:
            raise HTTPException(409, "主 MuLi 会话是持续身份，不能按普通窗口删除")
        session = runtime.store.get("session", session_id)
        if session is not None and session.get("workflow_run_id"):
            raise HTTPException(409, "该记录属于持久工作流，请使用工作流取消功能，保留运行记录")
        if runtime.store.db.execute(
            "SELECT 1 FROM objects WHERE kind='agent' AND json_extract(data,'$.session_id')=? LIMIT 1",
            (session_id,),
        ).fetchone():
            raise HTTPException(409, "该记录属于持久队友，请使用团队停止功能，保留身份与任务记录")
        await runtime.stop(session_id)
        await runtime.compaction.forget(session_id)
        with runtime.store.db:
            runtime.store.db.execute(
                "DELETE FROM objects WHERE kind IN ('message','run','approval') AND json_extract(data,'$.session_id')=?",
                (session_id,),
            )
            runtime.store.db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            runtime.store.db.execute("DELETE FROM ingress WHERE session_id=?", (session_id,))
            runtime.store.db.execute("DELETE FROM message_order WHERE session_id=?", (session_id,))
        return {"deleted": runtime.store.delete("session", session_id)}

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: ChatInput) -> dict[str, Any]:
        for identifier in body.attachment_ids:
            attachments.path(identifier)
        content = body.content or ("请查看附件" if body.attachment_ids else "")
        if session_id == runtime.main_session()["id"]:
            result = await runtime.submit(content, body.mode, attachment_ids=body.attachment_ids)
        elif body.attachment_ids:
            raise ValueError("请在主 MuLi 对话中发送附件")
        else:
            result = await runtime.start(session_id, content, body.mode)
        if "message" in result:
            result["message"] = decorated_message(result["message"])
        return result

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
            for table in (
                "objects",
                "message_order",
                "ingress",
                "memories",
                "relations",
                "memory_revisions",
                "memory_sources",
                "memory_forgotten_sources",
                "memory_meta",
                "memory_entities",
                "memory_aliases",
                "memory_mentions",
                "memory_edges",
                "memory_vectors",
                "memory_conflicts",
                "memory_communities",
                "memory_community_members",
                "memory_community_nodes",
                "channel_inbound",
                "channel_outbox",
            ):
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
