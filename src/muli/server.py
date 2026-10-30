"""Same-origin local management API and static workspace."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import unquote
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from muli import __version__
from muli.attachments import (
    MAX_ATTACHMENT_IDS,
    SAFE_INLINE,
    Attachments,
    attachment_ids_schema,
    validate_attachment_ids,
)
from muli.attention import AttentionInbox
from muli.auth import COOKIE_NAME, Authentication
from muli.candidate_online import OnlineSettings
from muli.candidate_policy import CandidateSettings
from muli.capability_catalog import CapabilityCatalog, CapabilityCatalogError
from muli.channel_application import ChannelApplication
from muli.channel_history import CHANNEL_HISTORY_PAGE_LIMIT
from muli.channels import ChannelError
from muli.config import ConfigFile, Settings, default_config_path, default_data_dir
from muli.conversation import MAX_EVENT_ID_CHARS
from muli.evidence import EvidenceError
from muli.extensions import ExtensionError
from muli.file_inbox import FileInbox, FileInboxSettings
from muli.history_evidence import HistoryEvidence
from muli.integrations.attention import ChiliarchAttentionInput
from muli.local_services import LocalServices
from muli.maintenance import BootPermit, MaintenanceError
from muli.maintenance_api import (
    INTERNAL_METHODS,
    install_maintenance_routes,
    passive_request_allowed,
    request_has_effect,
)
from muli.managed_boot import ManagedBootPermit
from muli.managed_requests import LocalCandidate, validate_candidate
from muli.management import SelfManagement
from muli.management_transaction import ClockReading
from muli.mcp_policy import ReceivePolicy
from muli.memory_application import MemoryApplication
from muli.memory_evidence import SOURCE_PAGE_LIMIT, MemoryEvidence, memory_listing
from muli.middleware import BodyLimitMiddleware
from muli.permissions import Mode
from muli.personal_io import PersonalIO
from muli.plugins import PluginManager
from muli.process import _wait_cleanup
from muli.providers import ProviderError
from muli.reply_origin import ReplyOrigin
from muli.request_operation import request_operation
from muli.runtime import Runtime
from muli.saved_answer_voice import SavedAnswerVoice, SavedAnswerVoiceError
from muli.service import UserService
from muli.session_records import SessionRecords
from muli.store import SQLITE_MAX_INTEGER, now
from muli.task_control import ControlConflict, ControlUnavailable, OwnerControlRequest
from muli.web_voice import WebVoiceError, WebVoiceInput

if TYPE_CHECKING:
    from muli.platforms.linux_bootstrap import BootSession

_HISTORY_OUTPUT_CHARS = 32_000
_MAX_WEB_SEQUENCE = (1 << 53) - 1  # JSON numbers must round-trip through browser Number.


def _web_archive_result(result: dict[str, Any]) -> dict[str, Any]:
    sequences = [result.get("seq"), result.get("through_seq"), result.get("next_before_seq")]
    sequences.extend(item["seq"] for item in result.get("messages", []))
    if any(sequence is not None and sequence > _MAX_WEB_SEQUENCE for sequence in sequences):
        raise EvidenceError("archive_sequence_range", "此归档序号超出浏览器精确整数范围")
    return result


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VoiceSetupInput(Input):
    id: UUID = Field(default_factory=uuid4)
    operation: Literal["enroll", "verify"]
    confirmed: bool = Field(strict=True)
    seconds: float = Field(default=5, ge=1, le=15, strict=True, allow_inf_nan=False)


class VoiceSetupCancelInput(Input):
    id: UUID


class SessionInput(Input):
    title: str = Field(default="新会话", max_length=120)
    mode: Mode | None = None


AttachmentReferences = Annotated[
    list[str],
    BeforeValidator(validate_attachment_ids),
    Field(max_length=MAX_ATTACHMENT_IDS, json_schema_extra=attachment_ids_schema()),
]


class ChatInput(Input):
    content: str = Field(default="", max_length=64000)
    mode: Mode | None = None
    attachment_ids: AttachmentReferences = Field(default_factory=list)


class TranscriptionInput(Input):
    attachment_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class SavedAnswerSpeechInput(Input):
    run_id: str = Field(min_length=1, max_length=200, strict=True)
    revision: str = Field(min_length=1, max_length=200, strict=True)
    confirmed: bool = Field(strict=True)


class AttachmentCollectionInput(Input):
    source_revision: str = Field(pattern=r"^[a-f0-9]{64}$", strict=True)
    document_revision: str | None = Field(pattern=r"^[a-f0-9]{64}$", strict=True)
    registered_revision: str = Field(pattern=r"^[a-f0-9]{64}$", strict=True)


class LoginInput(Input):
    password: str = Field(min_length=1, max_length=1024)


class TeamMessageInput(Input):
    content: str = Field(min_length=1, max_length=64000)
    attachment_ids: AttachmentReferences = Field(default_factory=list)


class TaskControlInput(Input):
    action: Literal["pause", "request_replan"]
    expected_epoch: int = Field(ge=0, strict=True)
    request_id: str = Field(min_length=1, max_length=MAX_EVENT_ID_CHARS, strict=True)
    additional_requirements: str = Field(default="", strict=True)


class ApprovalInput(Input):
    decision: Literal["approve", "reject"]


class MemoryInput(Input):
    content: str = Field(min_length=1, max_length=12000)
    kind: str = "observation"
    source: str = "user"
    entities: list[str] = Field(default_factory=list, max_length=30)
    relations: list[dict[str, str]] = Field(default_factory=list, max_length=60)
    confidence: float = Field(default=1, ge=0, le=1)


class MemoryIndexInput(Input):
    action: Literal["start", "status", "cancel"]
    source_ids: list[Annotated[str, Field(min_length=1, strict=True)]] | None = None
    key: str | None = Field(default=None, min_length=1, strict=True)
    plan_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$", strict=True)


class MemorySourceRetryInput(Input):
    source_id: str = Field(min_length=1, max_length=200, strict=True)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$", strict=True)


class MemoryWatchCancelInput(Input):
    id: str = Field(pattern=r"^[a-f0-9]{64}$", strict=True)


class MemorySourceCompareInput(Input):
    left_id: str = Field(min_length=1, max_length=200, strict=True)
    left_revision: str = Field(pattern=r"^[a-f0-9]{64}$", strict=True)
    right_id: str = Field(min_length=1, max_length=200, strict=True)
    right_revision: str = Field(pattern=r"^[a-f0-9]{64}$", strict=True)
    cursor: str | None = Field(default=None, min_length=1, strict=True)
    view: Literal["changes", "overview"] = "changes"


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


class TransactionRequest(Input):
    action: Literal["configure", "restart", "adopt", "switch"] = "restart"
    changes: dict[str, Any] = Field(default_factory=dict)
    candidate: LocalCandidate | None = None


class DeliveryRetryInput(Input):
    allow_unknown: bool = False


class ReminderInput(Input):
    title: str = Field(min_length=1, max_length=120)
    due_at: float
    content: str = Field(default="", max_length=4000)
    interval_seconds: int | None = Field(default=None, ge=60, le=31536000)
    commitment_id: str | None = Field(default=None, min_length=1)


class CalendarReminderInput(Input):
    title: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=200)
    local_time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    weekdays: list[Annotated[int, Field(strict=True, ge=1, le=7)]] = Field(
        min_length=1, max_length=7
    )
    content: str = Field(default="", max_length=4000)


def create_app(
    config: ConfigFile | None = None,
    data_dir: Path | None = None,
    runtime: Runtime | None = None,
    *,
    listen_port: int | None = None,
    boot_permit: BootPermit | ManagedBootPermit | None = None,
    boot_session: BootSession | None = None,
    maintenance_clock: Callable[[], ClockReading] | None = None,
) -> FastAPI:
    if runtime is not None and (boot_permit is not None or boot_session is not None):
        raise ValueError("boot_permit 只能由一个运行时持有")
    permit = boot_session.permit if boot_session is not None else boot_permit
    if isinstance(permit, ManagedBootPermit):
        if config is not None and config is not permit.config:
            raise ValueError("managed_configuration_owner_required")
        config = permit.config
    config = config or ConfigFile(default_config_path())
    owned = runtime is None
    runtime = runtime or Runtime(
        config, data_dir or default_data_dir(), boot_permit=boot_permit, boot_session=boot_session
    )
    config = runtime.config
    try:
        return _create_app(config, runtime, listen_port, maintenance_clock)
    except BaseException:
        if owned:
            runtime.discard_unstarted()
        raise


def _create_app(
    config: ConfigFile,
    runtime: Runtime,
    listen_port: int | None,
    maintenance_clock: Callable[[], ClockReading] | None,
) -> FastAPI:
    bound_host, bound_port = config.settings.server.host, listen_port or config.settings.server.port
    bound_loopback = bound_host == "localhost" or ipaddress.ip_address(bound_host).is_loopback
    file_inbox = FileInbox(runtime, closing=lambda: configuration_closing)
    runtime.add_tool_provider(file_inbox.tools)
    attention = AttentionInbox(
        runtime,
        lambda: config.settings.system_events,
        chiliarch_settings=lambda: integration.attention_settings(),
        file_inbox_settings=file_inbox.settings_state,
    )
    service = UserService(config.path, runtime.data_dir)
    authentication = Authentication(config)
    attachments = Attachments(runtime.store, runtime.data_dir / "attachments", config)
    local_services = LocalServices(runtime, attachments)
    web_voice = WebVoiceInput(local_services)
    memory_application = MemoryApplication(
        runtime,
        local_services.media,
        attachment_reader=local_services.memory_attachment,
        document_reader=local_services.memory_document,
        attachment_preparer=local_services.prepare_memory_attachment,
        transcript_reader=local_services.memory_transcript,
        webpage_reader=local_services.memory_webpage,
    )
    channel_application = ChannelApplication(runtime, attachments)
    personal_io = PersonalIO(runtime, local_services, attachments)
    from muli.integrations.manager import IntegrationManager

    integration = IntegrationManager(runtime, closing=lambda: configuration_closing)
    runtime.add_tool_provider(integration.tools)
    runtime.maintenance.owner("integration", lambda: integration.idle)
    runtime.maintenance.owner("memory_recovery", lambda: memory_application.idle)
    runtime.maintenance.owner("personal_io", lambda: personal_io.idle)
    configuration_closing = False
    saved_answer_voice = SavedAnswerVoice(local_services, closing=lambda: configuration_closing)
    runtime.maintenance.owner("saved_answer_voice", lambda: saved_answer_voice.idle)
    onboarding_task: asyncio.Task[None] | None = None
    runtime.maintenance.owner(
        "onboarding", lambda: onboarding_task is None or onboarding_task.done()
    )

    async def apply_configuration(target: Settings) -> None:
        runtime.maintenance.require_configuration()
        if (target.server.host, target.server.port) != (
            config.settings.server.host,
            config.settings.server.port,
        ):
            raise MaintenanceError("listener_restart_required")
        runtime.store.revalidate_format()
        if configuration_closing:
            raise ValueError("服务正在关闭，不能应用新配置")
        if not bound_loopback and not target.server.password_hash:
            raise ValueError(
                "当前进程仍监听非回环地址。请先保留密码、改为回环监听并重启，然后再清除密码"
            )
        runtime.validate_proactive_settings(target)
        if runtime.extensions.configuration_only(config.settings, target):
            saved_answer_voice.invalidate()
            try:
                await runtime.extensions.apply_locked(target, persist=True)
            finally:
                saved_answer_voice.reopen()
            return
        memory_application.pipeline.check_index_budget(target.limits.max_output_chars * 4)
        previous = config.settings.model_copy(deep=True)

        async def rebuild() -> None:
            await runtime.reconfigure()
            await memory_application.reconfigure()
            await channel_application.reconfigure()
            await personal_io.reconfigure()
            await integration.start()
            await file_inbox.reconfigure()
            await attention.reconfigure()
            await web_voice.reconfigure()
            saved_answer_voice.reopen()

        async def apply() -> None:
            try:
                await file_inbox.close()
                await attention.close()
                await web_voice.aclose()
                await integration.close()
                await channel_application.close()
                await personal_io.close()
                await memory_application.close()
                runtime.maintenance.require_configuration()
                runtime.validate_proactive_settings(target)
                runtime.store.revalidate_format()
                config.replace(target)
                await rebuild()
            except (Exception, asyncio.CancelledError):
                config.replace(previous)
                await rebuild()
                raise

        # The caller keeps configuration_lock until commit or recovery settles.
        # Disconnecting an observer must not strand half of the application stopped.
        saved_answer_voice.invalidate()
        applying = asyncio.create_task(apply(), name="muli-configuration")
        if await _wait_cleanup(applying):
            raise asyncio.CancelledError

    async def onboarding() -> None:
        if runtime.maintenance.configuration_blocked:
            return
        if config.settings.onboarding_pending and config.settings.models.main.configured:
            await runtime.submit(
                "请先和我打个招呼，并带我逐步完善配置。先询问我最希望你帮忙的事情，再按实际需要配置"
                "记忆模型、视觉、搜索、其它 agent、chiliarch 配对、通讯渠道和语音。一次只处理少量问题，"
                "不要假装尚未配置的连接可用。你可以在征求我的偏好后完善自己的自我介绍。",
                source="onboarding",
                event_id="first-setup-v2",
            )
            async with runtime.configuration_lock:
                if not runtime.maintenance.configuration_blocked:
                    config.update({"onboarding_pending": False})

    def wake_onboarding() -> None:
        nonlocal onboarding_task
        if (
            not configuration_closing
            and runtime.started
            and config.settings.onboarding_pending
            and (onboarding_task is None or onboarding_task.done())
        ):

            async def admitted_onboarding() -> None:
                with runtime.maintenance.admission.operation():
                    await onboarding()

            onboarding_task = asyncio.create_task(admitted_onboarding(), name="muli-onboarding")

    runtime.maintenance.on_configuration_release(wake_onboarding)

    async def activate_components() -> None:
        runtime._main_wake_deferred = True
        await plugins.load()
        if runtime.maintenance.permit and runtime.maintenance.permit.probation:
            await runtime.activate_components()
        else:
            await runtime.startup()
        await attention.start()
        await file_inbox.start()
        await memory_application.start()
        await channel_application.start()
        await personal_io.start()
        await integration.start()
        await management.start()
        runtime._main_wake_deferred = False
        runtime._wake_main()
        await onboarding()

    runtime.maintenance.bind_activation(activate_components)

    close_task: asyncio.Task[None] | None = None

    async def close_resources() -> None:
        nonlocal configuration_closing
        configuration_closing = True
        # Invalidate before awaiting a configuration owner; join only outside its lock.
        await saved_answer_voice.aclose()
        await memory_application.close_comparisons()
        try:
            await runtime.maintenance.settle_activation()
        except Exception:
            pass  # The failed activation remains blocked; still close its components.
        if onboarding_task is not None:
            onboarding_task.cancel()
            await asyncio.gather(onboarding_task, return_exceptions=True)
        # Join an owned apply/rollback before stopping its components.
        await management.close()
        async with runtime.configuration_lock:
            pass  # An admitted API transaction must settle before Store closes.
        await file_inbox.close()
        await attention.close()
        await web_voice.aclose()
        await integration.close()
        await channel_application.close()
        await personal_io.close()
        await memory_application.close()
        await plugins.close()
        await runtime.close()

    async def close() -> None:
        nonlocal close_task
        if close_task is None:
            close_task = asyncio.create_task(close_resources(), name="muli-application-close")
        if await _wait_cleanup(close_task):
            raise asyncio.CancelledError

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await management.recovery.recover()
            if not (runtime.maintenance.permit and runtime.maintenance.permit.probation):
                await activate_components()
            yield
        finally:
            await close()

    app = FastAPI(
        title="MuLi",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.runtime, app.state.integration = runtime, integration
    app.state.close = close
    app.state.attachments, app.state.authentication = attachments, authentication
    app.state.local_services = local_services
    app.state.web_voice = web_voice
    app.state.saved_answer_voice = saved_answer_voice
    plugins = PluginManager(runtime, app, config.path.resolve().parent / "plugins")
    capabilities = CapabilityCatalog(runtime, plugins)

    def legacy_plugins_present() -> bool:
        if plugins.contexts or any(item["status"] == "failed" for item in plugins.inventory()):
            return True
        if not config.settings.plugins.enabled or not plugins.directory.exists():
            return False
        # Candidate boots must reject before importing unknown startup hooks.
        try:
            return any(
                entry.is_dir()
                and not entry.is_symlink()
                and entry.name not in config.settings.plugins.disabled
                and (entry / "plugin.json").exists()
                for entry in plugins.directory.iterdir()
            )
        except OSError:
            return True

    runtime.maintenance.unsupported("legacy_plugins", legacy_plugins_present)
    management = SelfManagement(
        runtime, plugins, service, apply_configuration, bound_address=(bound_host, bound_port)
    )
    runtime.maintenance.seal(
        {
            "runtime",
            "integration",
            "memory_recovery",
            "personal_io",
            "saved_answer_voice",
            "management",
            "onboarding",
        }
    )
    app.state.management, app.state.plugins = management, plugins
    app.state.memory_application = memory_application
    app.state.attention = attention
    app.state.file_inbox = file_inbox
    app.state.channel_application, app.state.personal_io = channel_application, personal_io
    app.add_middleware(BodyLimitMiddleware)
    install_maintenance_routes(
        app, runtime, maintenance_clock, on_resolved=management.recovery.project
    )

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
        internal_handshake = (request.method, request.url.path) in INTERNAL_METHODS
        if (
            request.url.path.startswith("/api/")
            and request.url.path
            not in {
                "/api/auth/status",
                "/api/auth/login",
            }
            and not signed_webhook
            and not internal_handshake
            and not authentication.authenticated(request.cookies.get(COOKIE_NAME, ""))
        ):
            return JSONResponse({"detail": "需要登录"}, status_code=401)
        if (
            not internal_handshake
            and not signed_webhook
            and request.url.path.startswith("/api/")
            and authentication.authenticated(request.cookies.get(COOKIE_NAME, ""))
        ):
            # A lost final helper hint must not leave configuration permanently
            # paused. Projection is idempotent and keeps the original reply route.
            await management.recovery.project()
            runtime.maintenance.reconcile()
        durable_webhook = (
            signed_webhook
            and runtime.started
            and channel_application.manager.receiving
            and runtime.maintenance.accepts_external_input
        )
        main_session = (
            runtime.store.existing_main_session_id()
            if request.method == "POST"
            and request.url.path.startswith("/api/sessions/")
            and request.url.path.endswith("/messages")
            else None
        )
        if (
            not runtime.maintenance.admission.allowed
            and not durable_webhook
            and not passive_request_allowed(request.method, request.url.path, main_session)
        ):
            return JSONResponse({"detail": "execution_paused"}, status_code=409)
        if not durable_webhook and request_has_effect(
            request.method, request.url.path, main_session
        ):
            with runtime.maintenance.admission.operation():
                response = await call_next(request)
        else:
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
        status = (
            409
            if isinstance(exc, ExtensionError)
            and exc.code in {"runtime_busy", "generation_draining", "candidate_pending"}
            else 400
        )
        return JSONResponse({"detail": str(exc)[:1000]}, status_code=status)

    @app.exception_handler(MaintenanceError)
    async def maintenance_error(request: Request, exc: MaintenanceError) -> JSONResponse:
        status = (
            401
            if exc.code == "transaction_unauthorized"
            else 503
            if exc.code
            in {
                "transaction_unavailable",
                "maintenance_incomplete",
            }
            else 409
        )
        return JSONResponse(
            {"detail": exc.code, **({"reason": exc.reason} if exc.reason else {})},
            status_code=status,
        )

    @app.exception_handler(EvidenceError)
    async def unavailable_evidence(request: Request, exc: EvidenceError) -> JSONResponse:
        status = {"evidence_not_found": 404, "evidence_changed": 409}.get(exc.code, 400)
        return JSONResponse(
            {"detail": {"code": exc.code, "message": exc.message}}, status_code=status
        )

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

        try:
            return await request_operation(
                request, web_voice.transcribe(body.attachment_id), cancelled_message="转写已取消"
            )
        except WebVoiceError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(504, "转写达到时间上限") from exc

    @app.get("/api/commitments")
    async def commitments(status: str = "active") -> dict[str, Any]:
        return {"items": local_services.commitments.list(status)}

    @app.get("/api/commitment-directory")
    async def commitment_directory(
        status: str = "active",
        cursor: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        return local_services.commitments.directory(status=status, cursor=cursor, limit=limit)

    def task_control_error(
        identifier: str, exc: ControlConflict | ControlUnavailable
    ) -> HTTPException:
        detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
        try:
            detail["current"] = runtime.task_control_current(identifier)
        except (KeyError, ControlUnavailable):
            pass
        status = 409 if isinstance(exc, ControlConflict) else 503
        if exc.code == "queue_full":
            status = 429
        return HTTPException(status, detail)

    @app.get("/api/commitments/{identifier}")
    async def commitment_detail(identifier: str) -> dict[str, Any]:
        item = local_services.commitments.get(identifier)
        try:
            current = runtime.task_control_current(identifier)
        except (ControlConflict, ControlUnavailable) as exc:
            raise task_control_error(identifier, exc) from exc
        return {
            **item,
            "control": {**item.get("control", {}), **current["control"]},
            "scope": current["scope"],
        }

    @app.get("/api/commitments/{identifier}/inspection")
    async def inspect_task(
        identifier: str,
        section: str = "overview",
        agent_id: Annotated[str | None, Query(min_length=1)] = None,
        cursor: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        query: str | None = None,
        revision: Annotated[str | None, Query(min_length=1)] = None,
        record_id: Annotated[str | None, Query(min_length=1)] = None,
        record_revision: Annotated[str | None, Query(min_length=1)] = None,
        offset_bytes: Annotated[int, Query(ge=0)] = 0,
        length_bytes: Annotated[int | None, Query(ge=1)] = None,
    ) -> dict[str, Any]:
        return local_services.task_inspection.read(
            identifier,
            section=section,
            agent_id=agent_id,
            cursor=cursor,
            limit=limit,
            query=query,
            revision=revision,
            record_id=record_id,
            record_revision=record_revision,
            offset_bytes=offset_bytes,
            length_bytes=length_bytes,
        )

    @app.post("/api/commitments/{identifier}/control")
    async def control_task(identifier: str, body: TaskControlInput) -> dict[str, Any]:
        try:
            result = await runtime.submit_task_control(
                identifier,
                OwnerControlRequest(
                    action=body.action,
                    expected_epoch=body.expected_epoch,
                    request_id=body.request_id,
                    additional_requirements=body.additional_requirements,
                ),
            )
        except (ControlConflict, ControlUnavailable) as exc:
            raise task_control_error(identifier, exc) from exc
        return result.public()

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

    @app.get("/api/uploads/{identifier}/memory-preview")
    async def prepare_attachment_collection(identifier: str, request: Request) -> dict[str, Any]:
        try:
            return await request_operation(
                request,
                memory_application.attachment_collection(
                    identifier, None, closing=lambda: configuration_closing
                ),
                cancelled_message="资料核对已取消",
            )
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error

    @app.post("/api/uploads/{identifier}/memory-collection")
    async def collect_attachment(
        identifier: str, body: AttachmentCollectionInput, request: Request
    ) -> dict[str, Any]:
        try:
            return await request_operation(
                request,
                memory_application.attachment_collection(
                    identifier, body.model_dump(), closing=lambda: configuration_closing
                ),
                cancelled_message="收藏请求已取消，请核对准确来源",
            )
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error

    @app.get("/api/conversation")
    async def conversation() -> dict[str, Any]:
        session = runtime.main_session()
        # Keep these reads synchronous: facts and the global continuation position
        # belong to one event-loop turn, without a snapshot/SSE subscription gap.
        messages = visible_messages(session["id"])
        events = runtime.store.recent_events(session["id"])
        candidate_run_id = next(
            (
                identifier
                for event in reversed(events)
                if event["type"].startswith("run.")
                and isinstance(
                    identifier := event["data"].get("run_id") or event["data"].get("id"), str
                )
            ),
            None,
        )
        live = runtime.session_live(session, candidate_run_id=candidate_run_id)
        after = str(runtime.store.event_position())
        return {
            "session": session,
            "messages": messages,
            "events": [{**event, "id": str(event["id"])} for event in events],
            "live": live,
            "after": after,
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
            return await runtime.team.send(
                identifier, body.content, attachment_ids=body.attachment_ids
            )

    @app.post("/api/team/{identifier}/stop")
    async def stop_agent(identifier: str) -> dict[str, Any]:
        return await runtime.team.stop(identifier)

    @app.get(
        "/api/capabilities",
        description=(
            "只读列出当前服务的完整轻量已注册工具目录（scope=registered_tools、complete=true），"
            "不含 parameters；名称、用途和来源供客户端搜索筛选。enabled 仅反映已保存的停用设置，"
            "目录不代表任一模型轮的会话、角色和权限裁剪结果。revision 是进程内唯一当前列表及对象"
            "观察身份；同名对象替换、列表元数据、真实来源或停用设置变化会使旧详情失效。"
            "不调用 schema/provider factory、模型、远程 RPC、插件 hook 或重载，不扫描配置目录。"
            "成功 JSON envelope 受 limits.max_output_chars * 4 的 UTF-8 字节预算约束，"
            "默认 96000 字节；超限整条拒绝，不返回部分目录。认证与同源规则保持，no-store；"
            "维护状态仅精确放行本 GET 及 /api/capabilities/tools/{identifier} 的 GET。"
        ),
        responses={
            413: {"description": "capability_payload_too_large：完整列表超过现有输出预算"},
            422: {
                "description": "capability_contract_invalid：非 JSON 元数据；"
                "capability_contract_sensitive：真实注册名称或风险标签包含已发布代的已知凭据，整条拒绝"
            },
        },
    )
    async def capability_list() -> JSONResponse:
        try:
            return JSONResponse(capabilities.listing())
        except CapabilityCatalogError as error:
            raise HTTPException(error.status_code, {"code": error.code}) from None

    @app.get(
        "/api/capabilities/tools/{identifier}",
        description=(
            "只读展开 scope=registered_tools 的一个工具，revision 必须来自当前完整列表。"
            "返回真实已注册 Tool.parameters 的完整 JSON，保留 required、enum、嵌套及 $ref，"
            "不调用可覆盖的 schema 方法、provider factory、handler、模型、RPC、插件 hook 或重载。"
            "revision 仅标识进程内当前列表和对象；同名替换使旧详情 409，不保存历史契约。"
            "同一对象的参数就地更新时读取当前数据，不承诺旧参数回放或本轮可执行权限。"
            "整个成功 JSON envelope 受 limits.max_output_chars * 4 UTF-8 字节预算限制，"
            "默认 96000 字节。超限 413，非法 JSON/非有限数/循环 422；参数包含已发布 MCP 代"
            "已知凭据时整体 422，不删字段、不截 JSON、不把脱敏内容称为原契约。"
            "no-store；维护时仅精确放行单段非空 identifier 的本 GET，不放行其他子路径或写请求。"
        ),
        responses={
            404: {"description": "capability_not_found：当前 revision 内没有该不透明工具 id"},
            409: {"description": "capability_catalog_stale：观察已变化，需刷新完整目录"},
            413: {"description": "capability_payload_too_large：完整契约超过现有 UTF-8 字节预算"},
            422: {
                "description": "revision 缺失或查询参数无效；capability_contract_invalid："
                "契约不是完整合法 JSON；capability_contract_sensitive：含已知凭据，完整契约被拒绝"
            },
        },
    )
    async def capability_detail(
        identifier: str, revision: Annotated[str, Query(min_length=1, max_length=200)]
    ) -> JSONResponse:
        try:
            return JSONResponse(capabilities.detail(identifier, revision))
        except CapabilityCatalogError as error:
            raise HTTPException(error.status_code, {"code": error.code}) from None

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
            "extensions": runtime.extensions.status(),
            "notifications": runtime.notifications.status(),
            "channels": channel_application.status(),
            "capture": personal_io.capture.status(),
            "voice": personal_io.voice.status(),
            "web_voice": web_voice.status(),
            "attention": attention.status(),
            "file_inbox": file_inbox.status(),
            "system_events": runtime.system_events.status(),
            "topic_reminders": runtime.topic_reminders.status(),
            "limits": settings.limits.model_dump(),
        }

    @app.get("/api/file-inbox")
    async def file_inbox_records(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return {"status": file_inbox.status(), "items": file_inbox.list_records(limit)}

    @app.get("/api/file-inbox/settings")
    async def file_inbox_settings() -> dict[str, Any]:
        return file_inbox.status()

    @app.put("/api/file-inbox/settings")
    async def configure_file_inbox(body: FileInboxSettings) -> dict[str, Any]:
        async with runtime.configuration_lock:
            return await file_inbox.configure(body)

    @app.get("/api/file-inbox/{identifier}")
    async def file_inbox_record(identifier: str) -> dict[str, Any]:
        record = file_inbox.locate(identifier)
        if record is None:
            raise HTTPException(status_code=404, detail="资料记录不存在或授权已撤销")
        return record

    @app.get("/api/settings")
    async def settings() -> dict[str, Any]:
        return config.public()

    @app.get("/api/settings/schema")
    async def settings_schema() -> dict[str, Any]:
        return Settings.model_json_schema()

    @app.get("/api/management")
    async def management_status() -> dict[str, Any]:
        await management.recovery.project()
        return {
            "plans": management.plans(),
            "recovery": management.recovery.current(),
            "plugins": plugins.inventory(),
            "plugins_restart_required": plugins.restart_required,
            "update": runtime.store.get("update", "latest"),
            "update_capability": management.updates.capability(),
            "extensions": runtime.extensions.status(),
            "system_events": runtime.system_events.status(),
            "topic_reminders": runtime.topic_reminders.status(),
        }

    @app.get("/api/management/transactions/current")
    async def transaction_status() -> dict[str, Any]:
        await management.recovery.project()
        return management.recovery.current()

    @app.get("/api/management/update-policy")
    async def update_policy() -> dict[str, Any]:
        return management.update_policy.read().model_dump(mode="json")

    @app.get("/api/management/mcp-receive-policy")
    async def mcp_receive_policy() -> dict[str, Any]:
        return runtime.extensions.receive_policy_status()

    @app.put("/api/management/mcp-receive-policy")
    async def replace_mcp_receive_policy(body: ReceivePolicy) -> dict[str, Any]:
        async with runtime.configuration_lock:
            if configuration_closing:
                raise MaintenanceError("transaction_unavailable", "service_closing")
            result = management.replace_mcp_policy(body)
        await runtime.bus.notify()
        return result

    @app.get("/api/management/update-preview")
    async def update_preview(
        version: Annotated[str | None, Query(max_length=40, pattern=r"^\d+\.\d+\.\d+$")] = None,
    ) -> dict[str, Any]:
        return await management.preview_update(version)

    @app.get("/api/management/update-online-policy")
    async def online_update_policy() -> dict[str, Any]:
        return management.online_policy.read().model_dump(mode="json")

    @app.put("/api/management/update-online-policy")
    async def replace_online_update_policy(body: OnlineSettings) -> dict[str, Any]:
        async with runtime.configuration_lock:
            if configuration_closing:
                raise MaintenanceError("transaction_unavailable", "service_closing")
            result = management.replace_online_policy(body)
        await runtime.bus.notify()
        return result.model_dump(mode="json")

    @app.put("/api/management/update-policy")
    async def replace_update_policy(body: CandidateSettings) -> dict[str, Any]:
        async with runtime.configuration_lock:
            if configuration_closing:
                raise MaintenanceError("transaction_unavailable", "service_closing")
            result = management.replace_update_policy(body)
        await runtime.bus.notify()
        return result.model_dump(mode="json")

    @app.post("/api/management/transactions")
    async def prepare_transaction(body: TransactionRequest) -> dict[str, Any]:
        if body.action != "configure" and body.changes:
            raise HTTPException(422, "只有配置操作接受配置变更")
        try:
            validate_candidate(body.action, body.candidate)
        except ValueError:
            raise HTTPException(
                422, "迁入或切换需要完整本地安装路径，其它操作不接受候选路径"
            ) from None
        return await management.stage(
            body.action,
            body.changes,
            apply_mode="restart_transaction",
            candidate=body.candidate,
        )

    @app.post("/api/management/transactions/recover")
    async def recover_transaction(body: Input) -> dict[str, Any]:
        async with runtime.configuration_lock:
            if configuration_closing:
                raise MaintenanceError("transaction_unavailable", "service_closing")
        return await management.recover()

    @app.get("/api/extensions")
    async def extension_status() -> dict[str, Any]:
        return runtime.extensions.status()

    @app.post("/api/extensions/reload")
    async def reload_extensions() -> dict[str, Any]:
        runtime.maintenance.require_configuration()
        return runtime.extensions.request_reload()

    @app.post("/api/management/{identifier}/cancel")
    async def cancel_management(identifier: str) -> dict[str, Any]:
        return await management.cancel(identifier)

    @app.get("/api/memory/status")
    async def memory_status() -> dict[str, Any]:
        return memory_application.pipeline.status()

    @app.get("/api/memory/watches")
    async def memory_watches(
        after_id: Annotated[str, Query(pattern=r"^(?:[a-f0-9]{64})?$")] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        try:
            return memory_application.watch_page(after_id=after_id, limit=limit)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.post("/api/memory/watches/cancel")
    async def cancel_memory_watch(body: MemoryWatchCancelInput) -> dict[str, Any]:
        async with runtime.configuration_lock:
            if configuration_closing:
                raise MaintenanceError("transaction_unavailable", "service_closing")
            main_id = runtime.store.existing_main_session_id()
            if main_id is None:
                raise ValueError("主会话身份无法核查")
            try:
                return await memory_application.cancel_watch(main_id, body.id)
            except PermissionError as error:
                raise HTTPException(status_code=403, detail=str(error)) from error

    @app.get("/api/memory/sources")
    async def memory_sources(
        cursor: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=SOURCE_PAGE_LIMIT)] = 20,
        state: str | None = None,
    ) -> dict[str, Any]:
        return MemoryEvidence(runtime.memory).sources(
            cursor=cursor,
            limit=limit,
            state=state,
            max_output_chars=config.settings.limits.max_output_chars,
        )

    @app.get("/api/memory/source")
    async def read_memory_source(
        identifier: Annotated[str, Query(alias="id", min_length=1, max_length=200)],
        offset_bytes: Annotated[int, Query(ge=0)] = 0,
        length_bytes: Annotated[int | None, Query(ge=1)] = None,
        revision: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    ) -> dict[str, Any]:
        return MemoryEvidence(runtime.memory).read(
            identifier,
            by_memory=False,
            offset_bytes=offset_bytes,
            length_bytes=length_bytes,
            revision=revision,
            max_output_chars=config.settings.limits.max_output_chars,
        )

    @app.post("/api/memory/sources/compare")
    async def compare_memory_sources(body: MemorySourceCompareInput) -> dict[str, Any]:
        if configuration_closing:
            raise MaintenanceError("transaction_unavailable", "service_closing")
        return await memory_application.compare_sources(body.model_dump(exclude_none=True))

    @app.post("/api/memory/sources/retry")
    async def retry_memory_source(body: MemorySourceRetryInput) -> dict[str, Any]:
        async with runtime.configuration_lock:
            if configuration_closing:
                raise MaintenanceError("transaction_unavailable", "service_closing")
            result = memory_application.retry_source(body.model_dump())
            pipeline = memory_application.pipeline
        if result["accepted"]:
            await pipeline._publish(result["source_id"])
        return result

    @app.post("/api/memory/reindex")
    async def memory_reindex(body: MemoryIndexInput) -> dict[str, Any]:
        async with runtime.configuration_lock:
            if configuration_closing:
                raise MaintenanceError("transaction_unavailable", "service_closing")
            result = memory_application.reindex(body.model_dump(exclude_none=True))
            pipeline = memory_application.pipeline
        if body.action != "status":
            await pipeline._publish()
        return result

    @app.get("/api/channels")
    async def channel_status() -> dict[str, Any]:
        return {
            **channel_application.status(),
            "deliveries": channel_application.manager.deliveries(),
        }

    @app.get(
        "/api/channels/deliveries",
        description=(
            "只读查询 MuLi 本地保存的当前绑定主人私聊发送记录。账号与主会话由当前配置确定；"
            "游标不透明，目录不含正文或原始投递元数据，读取不会发送或重试。"
        ),
        responses={
            400: {"description": "读取器拒绝无效游标、范围或输出预算"},
            403: {"description": "渠道已停用或主人绑定已撤销"},
            404: {"description": "不存在已保存的主会话"},
            422: {"description": "查询参数缺失或未通过类型、边界校验"},
        },
    )
    async def channel_deliveries(
        channel: Annotated[str, Query(min_length=1)],
        owner_id: Annotated[str, Query(min_length=1)],
        cursor: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=CHANNEL_HISTORY_PAGE_LIMIT)] = 20,
    ) -> dict[str, Any]:
        async with runtime.configuration_lock:
            try:
                return channel_application.delivery_history(
                    channel, owner_id, cursor=cursor, limit=limit
                )
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc

    @app.get(
        "/api/channels/deliveries/{identifier}/content",
        description=(
            "只读核查当前绑定主人私聊的准确发送分段。文本按 UTF-8 字节窗口返回，续读须携带"
            "该分段 revision；附件分段只返回准确引用和空正文。平台确认不表示已读或已播放。"
        ),
        responses={
            400: {"description": "读取器拒绝无效偏移、分段、范围或输出预算"},
            403: {"description": "渠道已停用或主人绑定已撤销"},
            404: {"description": "主会话、投递或分段不存在，或投递不属于当前私聊范围"},
            409: {"description": "该分段正文已变化，需从字节偏移 0 重新读取"},
            422: {"description": "查询参数缺失或未通过类型、边界校验"},
        },
    )
    async def channel_delivery_content(
        identifier: str,
        channel: Annotated[str, Query(min_length=1)],
        owner_id: Annotated[str, Query(min_length=1)],
        part: Annotated[int, Query(ge=0)] = 0,
        offset_bytes: Annotated[int, Query(ge=0)] = 0,
        revision: Annotated[str | None, Query(min_length=1)] = None,
    ) -> dict[str, Any]:
        async with runtime.configuration_lock:
            try:
                return channel_application.delivery_content(
                    channel,
                    owner_id,
                    delivery_id=identifier,
                    part=part,
                    offset_bytes=offset_bytes,
                    revision=revision,
                )
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc

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
        from muli.voice_configuration import setup_guidance

        result = personal_io.voice.status()
        return {**result, "setup_next_step": setup_guidance(result)["next_step"]}

    @app.post("/api/voice/setup")
    async def voice_setup(body: VoiceSetupInput, request: Request) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(400, "请明确确认本次有限录音")
        try:
            return await request_operation(
                request,
                personal_io.voice_setup(body.operation, body.seconds, identifier=str(body.id)),
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            raise HTTPException(409, "本次语音设置已取消") from None

    @app.post("/api/voice/setup/cancel")
    async def cancel_voice_setup(body: VoiceSetupCancelInput) -> dict[str, Any]:
        try:
            return await personal_io.voice_setup("cancel", identifier=str(body.id))
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/memory/query")
    async def memory_query(
        query: str,
        limit: int = 8,
        source_id: Annotated[list[str] | None, Query()] = None,
    ) -> dict[str, Any]:
        try:
            return await memory_application.query(
                query, limit=min(max(1, limit), 100), source_ids=source_id
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.patch("/api/settings")
    async def update_settings(changes: dict[str, Any]) -> dict[str, Any]:
        async with runtime.configuration_lock:
            target = config.validate_changes(changes)
            if runtime.busy and not runtime.extensions.configuration_only(config.settings, target):
                raise HTTPException(409, "请先停止运行中的任务")
            await apply_configuration(target)
        await onboarding()
        return config.public()

    @app.get("/api/sessions")
    async def sessions() -> dict[str, Any]:
        return {"items": runtime.store.list("session")}

    @app.get("/api/session-records")
    async def session_records(
        query: Annotated[str, Query(max_length=120)] = "",
        cursor: Annotated[str | None, Query(min_length=1, max_length=_HISTORY_OUTPUT_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        return SessionRecords(runtime.store).read(
            query=query,
            cursor=cursor,
            limit=limit,
            max_output_chars=min(config.settings.limits.max_output_chars, _HISTORY_OUTPUT_CHARS),
        )

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

    @app.get("/api/sessions/{session_id}/messages")
    async def archived_messages(
        session_id: str,
        before_seq: Annotated[int | None, Query(ge=1, le=_MAX_WEB_SEQUENCE)] = None,
        through_seq: Annotated[int | None, Query(ge=0, le=_MAX_WEB_SEQUENCE)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        return _web_archive_result(
            HistoryEvidence(runtime.store).archive(
                session_id,
                before_seq=before_seq,
                through_seq=through_seq,
                limit=limit,
                max_output_chars=min(
                    config.settings.limits.max_output_chars, _HISTORY_OUTPUT_CHARS
                ),
            )
        )

    @app.get("/api/sessions/{session_id}/messages/{message_id}/content")
    async def archived_content(
        session_id: str,
        message_id: str,
        offset_bytes: Annotated[int, Query(ge=0)] = 0,
        length_bytes: Annotated[int | None, Query(ge=1)] = None,
        revision: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    ) -> dict[str, Any]:
        return _web_archive_result(
            HistoryEvidence(runtime.store).read_content(
                session_id,
                message_id=message_id,
                offset_bytes=offset_bytes,
                length_bytes=length_bytes,
                revision=revision,
                max_output_chars=min(
                    config.settings.limits.max_output_chars, _HISTORY_OUTPUT_CHARS
                ),
            )
        )

    @app.patch("/api/sessions/{session_id}")
    async def update_session(session_id: str, body: SessionInput) -> dict[str, Any]:
        async with runtime.configuration_lock:
            session = runtime.store.get("session", session_id)
            if not session:
                raise KeyError(session_id)
            if session_id in runtime.tasks:
                raise HTTPException(409, "运行中不能变更会话权限")
            previous_mode = session["mode"]
            session.update(body.model_dump(exclude_unset=True, exclude_none=True))
            session["updated_at"] = now()
            with runtime.store.db:
                updated = runtime._store_session(session, previous_mode=previous_mode)
            runtime.shell_mode_changed(session_id, previous_mode, session["mode"])
            return updated

    @app.post("/api/sessions/{session_id}/messages/{message_id}/speech")
    async def saved_answer_speech(
        session_id: str, message_id: str, body: SavedAnswerSpeechInput, request: Request
    ) -> dict[str, Any]:
        if body.confirmed is not True:
            raise HTTPException(400, "请明确确认将这条完整答复发送给语音服务并保存附件")
        try:
            return await request_operation(
                request,
                saved_answer_voice.synthesize(session_id, body.run_id, message_id, body.revision),
                cancelled_message="语音请求已取消，执行结果未知且可能已计费；请勿自动重新生成",
            )
        except SavedAnswerVoiceError as error:
            raise HTTPException(error.status_code, str(error)) from error
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            raise HTTPException(
                409, "语音请求因关闭或配置变化取消，执行结果未知且可能已计费；请勿自动重新生成"
            ) from None

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
            runtime.shell_receipts.delete_session(session_id)
            runtime.store.db.execute(
                "DELETE FROM objects WHERE kind IN ('message','run','approval') AND json_extract(data,'$.session_id')=?",
                (session_id,),
            )
            runtime.store.db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            runtime.store.db.execute("DELETE FROM ingress WHERE session_id=?", (session_id,))
            runtime.store.db.execute("DELETE FROM message_order WHERE session_id=?", (session_id,))
            deleted = bool(
                runtime.store.db.execute(
                    "DELETE FROM objects WHERE kind='session' AND id=?", (session_id,)
                ).rowcount
            )
        return {"deleted": deleted}

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: ChatInput) -> dict[str, Any]:
        attachments.validate_references(body.attachment_ids)
        content = body.content or ("请查看附件" if body.attachment_ids else "")
        if session_id == runtime.main_session()["id"]:
            result = await runtime.submit(
                content,
                body.mode,
                attachment_ids=body.attachment_ids,
                _owner_text_chars=len(body.content) if body.content.strip() else None,
            )
        elif body.attachment_ids:
            raise ValueError("请在主 MuLi 对话中发送附件")
        else:
            try:
                result = await runtime.start(session_id, content, body.mode)
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc
        if "message" in result:
            result["message"] = decorated_message(result["message"])
        return result

    @app.post("/api/sessions/{session_id}/stop")
    async def stop(session_id: str) -> dict[str, bool]:
        await runtime.stop(session_id)
        return {"stopped": True}

    @app.get("/api/ui/event-position")
    async def event_position() -> dict[str, str]:
        return {"after": str(runtime.store.event_position())}

    def event_stream(
        request: Request, after: int, session_id: str | None = None
    ) -> StreamingResponse:
        cursor = request.headers.get("last-event-id")
        if cursor:
            try:
                header_after = int(cursor)
                if not 0 <= header_after <= SQLITE_MAX_INTEGER:
                    raise ValueError
            except ValueError as exc:
                raise HTTPException(400, "事件游标无效") from exc
            after = max(after, header_after)
        return StreamingResponse(
            runtime.bus.stream(after, session_id),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get("/api/events")
    async def events(
        request: Request, after: Annotated[int, Query(ge=0, le=SQLITE_MAX_INTEGER)] = 0
    ) -> StreamingResponse:
        return event_stream(request, after)

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(
        request: Request,
        session_id: str,
        after: Annotated[int, Query(ge=0, le=SQLITE_MAX_INTEGER)] = 0,
    ) -> StreamingResponse:
        if not runtime.store.get("session", session_id):
            raise KeyError(session_id)
        return event_stream(request, after, session_id)

    @app.get("/api/approvals")
    async def approvals() -> dict[str, Any]:
        return {"items": runtime.live_approvals()}

    @app.post("/api/approvals/{identifier}")
    async def approve(identifier: str, body: ApprovalInput) -> dict[str, bool]:
        await runtime.permissions.resolve(identifier, body.decision)
        return {"resolved": True}

    @app.get("/api/memories")
    async def memories(query: str = "") -> dict[str, Any]:
        return memory_listing(
            runtime.memory.search(query, 100),
            revision=runtime.memory.revision,
            max_path_chars=config.settings.limits.max_output_chars,
        )

    @app.get("/api/memories/graph")
    async def graph() -> dict[str, Any]:
        return runtime.memory.graph()

    @app.get("/api/memories/{identifier}")
    async def memory_detail(identifier: str) -> dict[str, Any]:
        return MemoryEvidence(runtime.memory).detail(identifier)

    @app.get("/api/memories/{identifier}/source")
    async def memory_source(
        identifier: str,
        offset_bytes: Annotated[int, Query(ge=0)] = 0,
        length_bytes: Annotated[int | None, Query(ge=1)] = None,
        revision: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    ) -> dict[str, Any]:
        return MemoryEvidence(runtime.memory).read(
            identifier,
            offset_bytes=offset_bytes,
            length_bytes=length_bytes,
            revision=revision,
            max_output_chars=config.settings.limits.max_output_chars,
        )

    @app.get("/api/memories/{identifier}/revisions")
    async def memory_revisions(identifier: str) -> dict[str, Any]:
        if runtime.memory.get(identifier) is None:
            raise KeyError(identifier)
        return {"items": runtime.memory.revisions(identifier)}

    @app.post("/api/memories")
    async def remember(body: MemoryInput) -> dict[str, Any]:
        item = runtime.memory.save(**body.model_dump())
        await runtime.bus.emit(
            "memory.updated", {"memory_ids": [item["id"]], "source_id": item["source_id"]}
        )
        return item

    @app.patch("/api/memories/{identifier}")
    async def edit_memory(identifier: str, body: MemoryInput) -> dict[str, Any]:
        revision = runtime.memory.revision
        item = runtime.memory.edit(identifier, **body.model_dump(exclude_unset=True))
        if runtime.memory.revision != revision:
            await runtime.bus.emit(
                "memory.updated", {"memory_ids": [identifier], "source_id": item["source_id"]}
            )
        return item

    @app.delete("/api/memories/{identifier}")
    async def forget(identifier: str) -> dict[str, bool]:
        item = runtime.memory.get(identifier)
        deleted = runtime.memory.delete(identifier)
        if deleted and item:
            await runtime.bus.emit(
                "memory.forgotten", {"memory_ids": [identifier], "source_id": item["source_id"]}
            )
        return {"deleted": deleted}

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

    @app.get("/api/workflow-runs/active")
    async def active_workflow_runs(
        after_id: Annotated[str, Query(max_length=200)] = "",
        limit: Annotated[int, Query(ge=1, le=50)] = 50,
    ) -> dict[str, Any]:
        identifiers = runtime.workflows.unfinished_run_ids(after_id=after_id, limit=limit + 1)
        has_more = len(identifiers) > limit
        items = [
            item
            for identifier in identifiers[:limit]
            if (item := runtime.store.workflow_run_summary(identifier)) is not None
        ]
        return {
            "items": items,
            "next_after": identifiers[limit - 1] if has_more else None,
            "has_more": has_more,
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

    @app.get("/api/workflow-schedules")
    async def workflow_schedules(
        after_id: Annotated[str, Query(max_length=200)] = "",
        limit: Annotated[int, Query(ge=1, le=50)] = 50,
        after_kind: Literal["workflow_schedule", "calendar_workflow_schedule"] | None = None,
    ) -> dict[str, Any]:
        return runtime.scheduled_workflows.list(
            after_id=after_id, after_kind=after_kind, limit=limit
        )

    @app.post("/api/workflow-schedules/{identifier}/cancel")
    async def cancel_workflow_schedule(
        identifier: str,
        kind: Literal["workflow_schedule", "calendar_workflow_schedule"] = "workflow_schedule",
    ) -> dict[str, Any]:
        async with runtime.configuration_lock:
            if configuration_closing:
                raise MaintenanceError("transaction_unavailable", "service_closing")
            runtime.maintenance.require_configuration()
            return await runtime.scheduled_workflows.cancel(identifier, kind=kind)

    @app.delete("/api/workflows/{identifier}")
    async def delete_workflow(identifier: str) -> dict[str, bool]:
        if runtime.workflows.has_unfinished(identifier):
            raise HTTPException(409, "工作流仍在运行")
        return {"deleted": runtime.store.delete("workflow", identifier)}

    @app.get("/api/notifications")
    async def notifications() -> dict[str, Any]:
        return {"items": runtime.store.list("notification")}

    @app.get("/api/reminders")
    async def reminders() -> dict[str, Any]:
        return {"items": runtime.reminders.records()}

    @app.post("/api/reminders")
    async def create_reminder(body: ReminderInput) -> dict[str, Any]:
        item = runtime.reminders.create(**body.model_dump(), reply_origin=ReplyOrigin("web"))
        await runtime.bus.emit("reminder.updated", item)
        return item

    @app.post("/api/calendar-reminders")
    async def create_calendar_reminder(body: CalendarReminderInput) -> dict[str, Any]:
        item = runtime.reminders.create_calendar(
            **body.model_dump(), reply_origin=ReplyOrigin("web")
        )
        await runtime.bus.emit("reminder.updated", item)
        return item

    @app.patch("/api/reminders/{identifier}")
    async def edit_reminder(
        identifier: str,
        changes: dict[str, Any],
        kind: Literal["reminder", "calendar_reminder"] = "reminder",
    ) -> dict[str, Any]:
        item = runtime.reminders.update(identifier, changes, kind=kind)
        await runtime.bus.emit("reminder.updated", item)
        return item

    @app.delete("/api/reminders/{identifier}")
    async def delete_reminder(
        identifier: str, kind: Literal["reminder", "calendar_reminder"] = "reminder"
    ) -> dict[str, bool]:
        return {"deleted": runtime.reminders.delete(identifier, kind=kind)}

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

    @app.get("/api/integrations/chiliarch/attention")
    async def chiliarch_attention() -> dict[str, Any]:
        return integration.attention_settings().public()

    @app.put("/api/integrations/chiliarch/attention")
    async def configure_chiliarch_attention(body: ChiliarchAttentionInput) -> dict[str, Any]:
        async with runtime.configuration_lock:
            if configuration_closing:
                raise MaintenanceError("transaction_unavailable", "service_closing")
            result = integration.replace_attention(body.enabled)
        await runtime.bus.notify()
        return result

    @app.get("/api/service")
    async def service_status() -> dict[str, Any]:
        return await service.status()

    @app.post("/api/service")
    async def service_action(body: ServiceInput) -> dict[str, Any]:
        async with runtime.configuration_lock:
            runtime.maintenance.require_configuration()
            from muli.management_cli import guarded_service_action

            return await guarded_service_action(service, body.action)

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
