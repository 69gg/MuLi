"""Channel settings, explicit sending tools and runtime lifecycle composition."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from muli.attachments import Attachments, attachment_ids_schema
from muli.channel_history import CHANNEL_HISTORY_PAGE_LIMIT, ChannelHistory
from muli.channels import ChannelError, ChannelManager, ChannelProfile, ChannelsConfig
from muli.channels.types import NAMES, IncomingMessage
from muli.context import context_size
from muli.mobile_approvals import is_approval_command
from muli.mobile_task_control import MobileControlRejected, MobileControlSource
from muli.permissions import ApprovalNotice
from muli.proactive import FrozenRouting, guard_delivery
from muli.proactive_memory import check_preference
from muli.providers import Message
from muli.reply_origin import authorize_route, channel_account
from muli.runtime import Runtime
from muli.tools import Tool, parameters, string


class ChannelApplication:
    def __init__(self, runtime: Runtime, attachments: Attachments) -> None:
        self.runtime, self.attachments = runtime, attachments
        self._generation = 0
        self.manager = self._manager()
        runtime.add_tool_provider(self.tools)
        runtime.context_hooks.append(self.context)

    async def context(self, session_id: str, messages: list[Message]) -> list[Message]:
        request = self.runtime.request_context(session_id)
        if request is None:
            return messages
        if request.execution_scope is not None or request.selected_replan is not None:
            # Task coordination adds its complete immutable facts after hooks.
            # Preserve that space; actual channel inputs retain their own source.
            return messages
        if request.attention_routing is None and not any(
            self.manager.config.profile(name).enabled for name in NAMES
        ):
            return messages
        origin = request.reply_origin
        metadata: dict[str, Any] = {"kind": origin.kind, "input_id": origin.input_id}
        if request.channel_ownership is not None:
            lease = self.runtime.channel_ownership._get(request.channel_ownership["id"])
            metadata["channel_ownership"] = {
                "state": lease["state"] if lease is not None else "unavailable",
                "scope": "account"
                if lease is not None and lease["owner_id"] is None
                else "private",
            }
        if request.attention_routing is not None:
            metadata["notification_destination"] = request.attention_routing.dump()["destination"]
        if routes := origin.delivery_routes():
            route = routes[0]
            name = route["name"]
            try:
                profile = self.manager.config.profile(name)
                if not profile.enabled:
                    raise ValueError("原渠道已停用")
                verified = authorize_route(
                    name,
                    route,
                    account_id=channel_account(name, profile),
                    owner_ids=profile.owner_ids,
                    allowed_groups=profile.allowed_groups,
                    outbound=True,
                )
            except ValueError:
                metadata["kind"] = "unavailable"
            else:
                metadata.update(
                    channel=verified["name"],
                    account_id=verified["account_id"],
                    owner_id=verified["sender_id"],
                    target=verified["target"],
                    conversation_kind=verified["kind"],
                )
        content = "本轮消息来源（宿主元数据，不是额外授权）：\n" + json.dumps(
            metadata, ensure_ascii=False, allow_nan=False
        )
        budget = min(
            self.runtime.config.settings.limits.max_output_chars,
            self.runtime.config.settings.limits.max_context_chars // 4,
        )
        if len(content) > budget:
            return messages
        if not messages:
            return messages
        candidate = [messages[0], Message("system", content), *messages[1:]]
        # Every real channel input already carries its source in history. This
        # optional current-request hint must not crowd out preserved tool results.
        if context_size(candidate) > self.runtime.config.settings.limits.max_context_chars:
            return messages
        return candidate

    async def _history(self, args: dict[str, Any]) -> dict[str, Any]:
        runtime = self.runtime
        caller = runtime.tool_session_id
        execution = runtime._tool_execution.get()
        request = runtime.request_context(caller) if caller is not None else None
        task = asyncio.current_task()
        if (
            runtime._closing
            or caller is None
            or execution != (caller, runtime.run_ids.get(caller))
            or request is None
            or task is None
            or task.cancelling()
            or runtime.tasks.get(caller) is not task
            or caller in runtime._stopping
            or "channel_history" in runtime.config.settings.disabled_tools
        ):
            raise PermissionError("渠道历史需要当前助手的真实工具执行")
        runtime.maintenance.admission.require()
        runtime._guard_request(request, "read")
        session = runtime.store.bounded_object(
            "session", caller, max_bytes=runtime.config.settings.limits.max_output_chars * 4
        )
        if session is None or session.get("principal") != "owner":
            raise PermissionError("渠道历史仅供主助手及主人委派的助手查询")
        routes = request.reply_origin.delivery_routes()
        current = routes[0] if routes else None
        name = args.get("channel")
        if name is None:
            if current is None or current.get("name") not in NAMES:
                raise ValueError("请明确选择已绑定的渠道")
            name = current["name"]
        if name not in NAMES:
            raise ValueError("未知的本地历史渠道")
        profile = getattr(runtime.config.settings.channels, name)
        if not profile.enabled:
            raise PermissionError("渠道已停用，不能查询其私聊历史")
        account = channel_account(name, profile)
        current_owner: str | None = None
        if current is not None and current.get("name") == name:
            try:
                bound = authorize_route(
                    name,
                    current,
                    account_id=account,
                    owner_ids=profile.owner_ids,
                    allowed_groups=[],
                    outbound=True,
                )
                if bound["kind"] != "private":
                    raise ValueError("仅查询主人私聊历史")
                current_owner = bound["sender_id"]
            except ValueError:
                if "channel" not in args:
                    raise PermissionError("本轮渠道来源已失效，请明确选择当前绑定渠道") from None
        owner = args.get("owner_id", current_owner)
        if owner is None:
            if len(profile.owner_ids) != 1:
                raise ValueError("该渠道没有唯一主人，请明确提供已绑定的 owner_id")
            owner = profile.owner_ids[0]
        if not isinstance(owner, str) or owner not in profile.owner_ids:
            raise PermissionError("只能查询当前渠道已绑定主人的点对点私聊")
        main_id = runtime.store.existing_main_session_id()
        if main_id is None:
            raise ValueError("主助手记录不存在")
        limits = runtime.config.settings.limits
        history = ChannelHistory(runtime.store)
        if "delivery_id" in args:
            if {"direction", "cursor", "limit"} & args.keys():
                raise ValueError("读取投递正文不能同时传目录方向、游标或条数")
            return history.read_delivery(
                name,
                account,
                owner,
                main_id,
                delivery_id=args["delivery_id"],
                part=args.get("part", 0),
                offset_bytes=args.get("offset_bytes", 0),
                revision=args.get("revision"),
                max_output_chars=limits.max_output_chars,
            )
        if {"part", "offset_bytes", "revision"} & args.keys():
            raise ValueError("分段读取投递正文需要 delivery_id")
        return history.read(
            name,
            account,
            owner,
            main_id,
            direction=args.get("direction", "both"),
            cursor=args.get("cursor"),
            limit=min(args.get("limit", 20), limits.max_search_results),
            max_output_chars=limits.max_output_chars,
        )

    def _manager(self) -> ChannelManager:
        data = self.runtime.config.settings.channels.model_dump()
        data.pop("proactive")
        expected = self.runtime.config.settings.channels.model_dump()
        generation = self._generation

        async def submit(**kwargs: Any) -> dict[str, Any]:
            def guard() -> None:
                if (
                    generation != self._generation
                    or expected != self.runtime.config.settings.channels.model_dump()
                ):
                    raise ValueError("渠道配置或授权已改变，请由当前渠道重新提交消息")

            return await self.runtime.submit(
                **kwargs, admission_guard=guard, _channel_event=kwargs.get("event_id")
            )

        async def control_submit(
            *,
            name: str,
            message: IncomingMessage,
            route: dict[str, Any],
            event_id: str,
            receipt: dict[str, Any] | None,
        ) -> dict[str, Any]:
            # Even a completed inbound receipt must pass today's authorization.
            # Runtime owns locking and durable replay; this layer never holds
            # configuration_lock while waiting for an inbound lock or shutdown.
            def guard() -> None:
                if (
                    generation != self._generation
                    or manager is not self.manager
                    or expected != self.runtime.config.settings.channels.model_dump()
                    or manager.adapters.get(name) is not adapters.get(name)
                ):
                    raise ChannelError(
                        "渠道配置或授权已改变，请由当前渠道重新提交任务控制", status_code=403
                    )
                manager.control_guard(name, message, route)

            try:
                submit_control = (
                    self.runtime.submit_mobile_approval
                    if is_approval_command(message.control_text)
                    else self.runtime.submit_mobile_task_control
                )
                return await submit_control(
                    inbound_id=event_id,
                    source=MobileControlSource(
                        name=name,
                        account_id=message.control_account_id,
                        owner_id=message.sender_id,
                        target=message.target,
                    ),
                    text=message.control_text,
                    admission_guard=guard,
                )
            except MobileControlRejected:
                # Only an explicit pre-write rejection is terminal to polling.
                # Storage and post-commit notification failures remain retryable.
                raise ChannelError(
                    "手机控制无法核验，请重新发送 /审批 或 /任务", status_code=403
                ) from None

        def delivery_guard(route: Mapping[str, Any], metadata: object) -> None:
            guard_delivery(self.runtime.config.settings, route, metadata, db=self.runtime.store.db)

        def ownership_guard(proof: object, route: Mapping[str, Any]) -> None:
            self.runtime.channel_ownership.guard_destination(proof, route)

        def on_receive_fault(code: str, occurrence_id: str) -> None:
            task = asyncio.current_task()
            if (
                self.runtime._closing
                or manager is not self.manager
                or generation != self._generation
                or expected != self.runtime.config.settings.channels.model_dump()
                or manager._closed
                or task is None
                or task.cancelling()
            ):
                return
            self.runtime.system_events.channel_receive_failed(code, occurrence_id)

        def approval_guard(notice: ApprovalNotice) -> dict[str, Any]:
            route = notice.reply_origin.delivery_routes()[0]
            name = route["name"]
            if (
                generation != self._generation
                or manager is not self.manager
                or expected != self.runtime.config.settings.channels.model_dump()
                or manager.adapters.get(name) is not adapters.get(name)
            ):
                raise ValueError("审批通知的渠道配置或授权已改变")
            return self.runtime.guard_approval_notice(notice)

        for name in NAMES:
            data[name] = ChannelProfile(**data[name])
        manager = ChannelManager(
            self.runtime.store,
            self.runtime.bus,
            ChannelsConfig(**data),
            submit,
            self.attachments,
            delivery_guard=delivery_guard,
            approval_guard=approval_guard,
            admission=self.runtime.maintenance.admission,
            control_submit=control_submit,
            ownership_guard=ownership_guard,
            on_receive_fault=on_receive_fault,
        )
        adapters = dict(manager.adapters)
        manager.recover()
        return manager

    def status(self) -> dict[str, Any]:
        settings = self.runtime.config.settings
        selected = FrozenRouting.independent(settings, self.runtime.store.db)
        _, reason = check_preference(settings, self.runtime.store.db)
        return {
            **self.manager.status(),
            "proactive": {
                **settings.channels.proactive.model_dump(),
                "next_destination": selected.dump()["destination"],
                "attention_enabled": settings.attention.enabled,
                "model_configured": settings.models.main.configured,
                "independent_sources": [
                    "startup_update_check",
                    "extensions_watch",
                    "mcp_connection",
                    "voice_listener",
                    "channel_receiver",
                    "chiliarch_message",
                    "file_inbox",
                ],
                "memory_status": {"valid": reason == "valid", "reason": reason},
            },
        }

    async def start(self) -> None:
        await self.manager.start()

    async def close(self) -> None:
        self._generation += 1
        await self.manager.close()

    async def reconfigure(self) -> None:
        self.runtime.store.revalidate_format()
        self.runtime.channel_ownership.reconcile()
        await self.close()
        self.manager = self._manager()
        await self.start()

    async def _reply_files(self, args: dict[str, Any]) -> dict[str, Any]:
        runtime = self.runtime
        owner = runtime._tool_execution.get()
        caller = runtime.tool_session_id
        request = runtime.request_context(caller) if caller is not None else None
        task = asyncio.current_task()
        if (
            runtime._closing
            or owner is None
            or caller is None
            or caller != runtime.store.existing_main_session_id()
            or owner[0] != caller
            or runtime.run_ids.get(caller) != owner[1]
            or request is None
            or task is None
            or runtime.tasks.get(caller) is not task
            or caller in runtime._stopping
            or "channel_reply_files" in runtime.config.settings.disabled_tools
        ):
            raise PermissionError("文件回复需要当前主助手的真实工具执行")
        runtime.maintenance.admission.require()
        budget = runtime.config.settings.limits.max_output_chars * 4
        session = runtime.store.bounded_object("session", caller, max_bytes=budget)
        run = runtime.store.bounded_object("run", owner[1], max_bytes=budget)
        if (
            session is None
            or session.get("id") != caller
            or session.get("kind") != "main"
            or run is None
            or run.get("id") != owner[1]
            or run.get("session_id") != caller
            or run.get("status") != "running"
        ):
            raise PermissionError("文件回复的主助手执行已结束或被替换")
        origin = runtime.current_reply_origin()
        routes = origin.delivery_routes()
        if (
            origin.kind != "channel"
            or not origin.input_id
            or not routes
            or routes[0].get("kind") != "private"
        ):
            raise PermissionError("文件回复仅支持本次真实渠道私聊，不选择默认收件人")
        route = routes[0]
        manager = self.manager
        name = route["name"]
        manager.config.profile(name)  # Reject unknown channel names before the live config lookup.
        profile = getattr(runtime.config.settings.channels, name)
        if not profile.enabled:
            raise PermissionError("原回复渠道未启用")
        authorize_route(
            name,
            route,
            account_id=channel_account(name, profile),
            owner_ids=profile.owner_ids,
            allowed_groups=profile.allowed_groups,
            outbound=True,
        )
        metadata = FrozenRouting.bound(origin).dump()
        manager._automatic_guard(name, route, metadata)
        key = args.get("idempotency_key")
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 200
            or any(ord(character) < 32 or ord(character) == 127 for character in key)
        ):
            raise ValueError("idempotency_key 需要 1–200 字符的非控制字符串")
        references = self.attachments.validate_references(args.get("attachment_ids"))
        if not references:
            raise ValueError("文件回复至少需要一个本地附件")
        event_id = (
            "reply-files:"
            + hashlib.sha256(
                json.dumps([origin.input_id, key], ensure_ascii=True, separators=(",", ":")).encode(
                    "ascii"
                )
            ).hexdigest()
        )
        # File mode has no await before enqueue commits. Preserve the tool's
        # external/epoch fence; automatic only retains dispatch-time routing checks.
        runtime._guard_request(request, "external")
        if runtime.store.pending_inputs(caller):
            raise PermissionError("收到新的输入，文件尚未投递，请重新确认本次来源")
        return await manager.enqueue(
            name,
            route["target"],
            attachment_ids=references,
            route=route,
            media_mode="file",
            automatic=True,
            attention_routing=metadata,
            event_id=event_id,
        )

    def tools(self, runtime: Runtime) -> list[Tool]:
        async def status(args: dict[str, Any]) -> Any:
            return {
                **self.status(),
                "deliveries": self.manager.deliveries(),
                "ownership": runtime.channel_ownership.status(),
            }

        async def delegate(args: dict[str, Any]) -> dict[str, Any]:
            caller = runtime.tool_session_id
            execution = runtime._tool_execution.get()
            request = runtime.request_context(caller) if caller is not None else None
            task = asyncio.current_task()
            if (
                caller is None
                or execution != (caller, runtime.run_ids.get(caller))
                or request is None
                or task is None
                or task.cancelling()
                or runtime.tasks.get(caller) is not task
                or runtime._closing
                or caller in runtime._stopping
                or request.attention_routing is not None
                or "channel_delegate" in runtime.config.settings.disabled_tools
            ):
                raise PermissionError("渠道移交需要当前主人委派的真实执行")
            runtime.maintenance.admission.require()
            runtime._guard_request(request, "write")
            if runtime.store.pending_inputs(caller):
                raise PermissionError("收到新的输入，请重新确认渠道归属")
            session = runtime.store.get("session", caller)
            if session is None or session.get("principal") != "owner":
                raise PermissionError("外部资料或系统事项不能移交渠道")
            trigger = runtime._latest_main_trigger(caller)
            if caller == runtime.store.existing_main_session_id() and (
                trigger is None or trigger.get("principal") != "owner"
            ):
                raise PermissionError("渠道移交只能依据主人当前要求")
            result = runtime.channel_ownership.control(caller, args)
            if args["action"] == "request_return":
                runtime.team.start()
            if (
                args["action"] == "reclaim"
                and result["agent_id"] != caller
                and runtime.channel_ownership.affects_running(result)
            ):
                await runtime.team.stop(result["agent_id"], cancel_pending=False)
                if runtime.store.pending_inputs(result["agent_id"]):
                    agent = runtime.team.get(result["agent_id"])
                    runtime.store.put("agent", {**agent, "status": "queued"})
                    runtime.team.start()
            await runtime.bus.notify()
            return result

        async def send(args: dict[str, Any]) -> Any:
            caller = runtime.tool_session_id
            execution = runtime._tool_execution.get()
            request = runtime.request_context(caller) if caller is not None else None
            task = asyncio.current_task()
            if (
                runtime._closing
                or caller is None
                or execution != (caller, runtime.run_ids.get(caller))
                or request is None
                or task is None
                or task.cancelling()
                or runtime.tasks.get(caller) is not task
                or caller in runtime._stopping
                or "channel_send" in runtime.config.settings.disabled_tools
            ):
                raise PermissionError("渠道发送需要当前助手的真实工具执行")
            session = runtime.store.bounded_object(
                "session", caller, max_bytes=runtime.config.settings.limits.max_output_chars * 4
            )
            if session is None or session.get("principal") != "owner":
                raise PermissionError("渠道发送仅供主助手及主人委派的助手使用")
            key = args.get("idempotency_key")
            if key is not None and (
                not isinstance(key, str)
                or not 1 <= len(key) <= 200
                or any(ord(character) < 32 or ord(character) == 127 for character in key)
            ):
                raise ValueError("idempotency_key 需要 1–200 字符的非控制字符串")
            manager, generation = self.manager, self._generation
            expected = runtime.config.settings.channels.model_dump()
            routing = request.attention_routing
            trigger = runtime._latest_main_trigger(caller)
            if routing is None and trigger and trigger.get("principal") == "system:attention":
                raise PermissionError("后台事项缺少可核验的通知目的地，保留在 WebUI")
            route = args.get("route")
            if request.channel_ownership is not None and route is None:
                routes = request.reply_origin.delivery_routes()
                if not routes:
                    raise PermissionError("受托队友外发缺少本批真实私聊来源")
                route = routes[0]
            event_id = "tool:" + key if key is not None else None
            if routing is not None:
                routes = routing.origin.delivery_routes()
                if (
                    not routes
                    or args["channel"] != routes[0]["name"]
                    or args["target"] != routes[0]["target"]
                    or (
                        route is not None
                        and manager._route(args["channel"], route, outbound=True) != routes[0]
                    )
                ):
                    raise PermissionError("后台通知只能显式发送到本事项已冻结的通知目的地")
                route = routes[0]
                if event_id is None:
                    raise ValueError("后台通知发送需要稳定的 idempotency_key")

            def guard() -> None:
                runtime.maintenance.admission.require()
                runtime._guard_request(request, "external")
                if (
                    task is not asyncio.current_task()
                    or task.cancelling()
                    or runtime.tasks.get(caller) is not task
                    or runtime.run_ids.get(caller) != execution[1]
                    or runtime._closing
                    or caller in runtime._stopping
                    or self.manager is not manager
                    or self._generation != generation
                    or runtime.config.settings.channels.model_dump() != expected
                    or "channel_send" in runtime.config.settings.disabled_tools
                    or runtime.store.pending_inputs(caller)
                ):
                    raise PermissionError("发送前执行、渠道配置或新输入已改变，请重新核对")
                if routing is not None:
                    manager._automatic_guard(args["channel"], route or {}, routing.dump())

            return await manager.enqueue(
                str(args["channel"]),
                str(args["target"]),
                str(args.get("text", "")),
                args.get("attachment_ids", []),
                route=route,
                media_mode=args.get("media_mode", "file"),
                event_id=event_id,
                automatic=routing is not None,
                attention_routing=routing.dump() if routing is not None else None,
                admission_guard=guard,
                ownership=request.channel_ownership,
            )

        return [
            Tool(
                "channel_delegate",
                "将已注册渠道的已绑定主人私聊长期交给已完成且空闲的直属队友。"
                "account覆盖该接入的已授权私聊，private需owner_id且优先；不扩大群或陌生人权限。"
                "request_return标记请归还，reclaim立即撤销旧资格并停止旧轮；队友return只归还自己的范围。"
                "普通回答仅在Web，外发仍需显式channel_send；取消的旧输入不会自动重演",
                parameters(
                    {
                        "action": {
                            "type": "string",
                            "enum": ["assign", "request_return", "reclaim", "return"],
                        },
                        "channel": {"type": "string", "enum": list(NAMES)},
                        "scope": {"type": "string", "enum": ["account", "private"]},
                        "owner_id": string("private范围必须选择当前已绑定主人"),
                        "agent_id": string("assign选择已有直属执行队友准确ID"),
                    },
                    ("action", "channel", "scope"),
                ),
                delegate,
                "write",
            ),
            Tool(
                "channel_history",
                "只读查询 MuLi 已保存的四渠道主人私聊；默认列入站和真实出站投递目录。"
                "不追补接入前的平台历史，普通Web文字不算外发。"
                "省略渠道及主人时沿本轮真实私聊；网页可明确选择渠道，多个主人须明确 owner_id。"
                "按各流方向和 next_cursor 继续，空 items 不代表查完；入站正文用 conversation_read。"
                "出站正文改传 delivery_id、part，按revision/next_offset_bytes续读；不能混目录参数。"
                "附件另行读取，pending不代表sent，unknown不要重发",
                parameters(
                    {
                        "channel": {"type": "string", "enum": list(NAMES)},
                        "owner_id": string("当前渠道已绑定的主人 ID；不猜联系人"),
                        "direction": {"type": "string", "enum": ["both", "incoming", "outgoing"]},
                        "cursor": string(),
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": CHANNEL_HISTORY_PAGE_LIMIT,
                        },
                        "delivery_id": string("已有出站投递ID；改为读取该条某个分段正文"),
                        "part": {"type": "integer", "minimum": 0},
                        "offset_bytes": {"type": "integer", "minimum": 0},
                        "revision": string(),
                    }
                ),
                self._history,
                preserve_output=True,
            ),
            Tool(
                "channel_status",
                "查看可选渠道与持久投递记录；不把排队当作已送达",
                parameters({}),
                status,
            ),
            Tool(
                "channel_reply_files",
                "主助手将已核对的本地附件作为文件回复本轮真实私聊；不填写收件人，同一请求重试保持稳定key，排队不表示送达，结果未知不自动重发；Web、无来源和群聊不可用",
                parameters(
                    {
                        "attachment_ids": {
                            **attachment_ids_schema(),
                            "minItems": 1,
                            "description": "显式回复本次私聊的已有本地附件ID；不推断任务归属或验收结论",
                        },
                        "idempotency_key": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": "本次请求内同一逻辑投递保持稳定；换附件冲突，unknown不换键重发",
                        },
                    },
                    ("attachment_ids", "idempotency_key"),
                ),
                self._reply_files,
                "external",
            ),
            Tool(
                "channel_send",
                "向已绑定渠道发送文字或附件。受托队友仅能回复本批真实私聊，省略route自动沿用原消息凭据。"
                "media_mode=voice显式发送单个语音附件，默认保持文件路径；"
                "重试同一逻辑发送保持idempotency_key，结果未知不自动重发，返回排队ID不代表送达",
                parameters(
                    {
                        "channel": {
                            "type": "string",
                            "enum": list(NAMES),
                        },
                        "target": string(),
                        "text": string(),
                        "media_mode": {
                            "type": "string",
                            "enum": ["file", "voice"],
                            "description": "默认file保留既有附件行为；voice仅Telegram/OneBot，需一个兼容音频附件，不自动转码或降级",
                        },
                        "idempotency_key": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": "同一逻辑发送保持稳定；同键内容或模式改变会拒绝。省略表示新发送，不能用换键规避未知结果",
                        },
                        "attachment_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 32,
                        },
                        "route": {
                            "type": "object",
                            "description": "受托队友可省略，宿主沿用本批真实私聊来源；显式指定须保留准确收件人、渠道及reply_to，不猜测回复凭据",
                        },
                    },
                    ("channel", "target"),
                ),
                send,
                "external",
            ),
        ]
