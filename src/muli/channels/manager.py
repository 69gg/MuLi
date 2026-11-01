"""签名渠道输入、明确路由回复及持久 outbox。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import asdict
from typing import Any

import httpx

from muli.attachments import Attachments, file_io
from muli.channel_health import POLLING_FAILURES
from muli.events import EventBus
from muli.evidence import decode_utf8
from muli.maintenance import Admission
from muli.management_transaction import _object
from muli.permissions import ApprovalNotice
from muli.proactive import FrozenRouting
from muli.reply_origin import (
    PRIVATE_OWNER_CHANNELS,
    ReplyOrigin,
    RouteError,
    authorize_route,
    channel_account,
)
from muli.store import SQLITE_MAX_INTEGER, Store, new_id, now

from .adapters import QQ, Adapter, OneBot, Telegram
from .types import (
    MAX_OUTGOING_TEXT_CHARS,
    NAMES,
    ChannelError,
    ChannelsConfig,
    FileContent,
    IncomingMessage,
    Submit,
    is_mobile_control_text,
    normalize_channel_reference,
)
from .weixin import Weixin


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def continuity_reference(value: Mapping[str, Any] | None) -> dict[str, str] | None:
    """只保存宿主给出的有界摘要引用，不接受任意 outbox 元数据。"""
    if value is None:
        return None
    keys = ("id", "main_id", "agent_id", "run_id")
    if set(value) != set(keys) or any(
        not isinstance(value.get(key), str)
        or not value[key]
        or len(value[key]) > 256
        for key in keys
    ):
        raise ChannelError("渠道连续摘要引用无效")
    return {key: value[key] for key in keys}


class _BlockedDelivery(ChannelError):
    pass


class ChannelManager:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        config: ChannelsConfig,
        submit: Submit,
        attachments: Attachments | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        delivery_guard: Callable[[Mapping[str, Any], object], None] | None = None,
        approval_guard: Callable[[ApprovalNotice], dict[str, Any]] | None = None,
        admission: Admission | None = None,
        control_submit: Submit | None = None,
        ownership_guard: Callable[[object, Mapping[str, Any]], None] | None = None,
        on_receive_fault: Callable[[str, str], None] | None = None,
    ) -> None:
        self.store, self.bus, self.config, self.submit = store, bus, config, submit
        self.attachments = attachments
        self.delivery_guard = delivery_guard
        self.approval_guard = approval_guard
        self.admission = admission or Admission()
        self.control_submit = control_submit
        self.ownership_guard = ownership_guard
        self.on_receive_fault = on_receive_fault
        # Validate before constructing an owned client, including on reconfigure.
        self.store.prepare_channels()
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self.adapters: dict[str, Adapter] = {
            "onebot": OneBot(config.onebot, self.client),
            "telegram": Telegram(config.telegram, self.client),
            "qq": QQ(config.qq, self.client),
            "weixin": Weixin(config.weixin, self.client),
        }
        self._tasks: list[asyncio.Task[None]] = []
        self._incoming: set[asyncio.Task[Any]] = set()
        self._recovered = False
        self._wake = asyncio.Event()
        self.changed = asyncio.Condition()
        self._inbound_lock = asyncio.Lock()
        self._errors: dict[str, str] = {}
        self._receive_health: dict[str, dict[str, str | None]] = {
            name: {"state": "idle", "code": None, "occurrence_id": None}
            for name in POLLING_FAILURES
        }
        self._active: set[str] = set()
        self._closed = False
        self._stopped = False
        self._revocation_after = ""

    def _account(self, name: str) -> str:
        return channel_account(name, self.config.profile(name))

    def _state(self, key: str, default: str = "") -> str:
        row = self.store.db.execute(
            "SELECT value FROM channel_state WHERE name=?", (key,)
        ).fetchone()
        return str(row[0]) if row else default

    def _set_state(self, key: str, value: str) -> None:
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO channel_state VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    async def start(self) -> None:
        if self._tasks or self._closed:
            return
        receive_failed = False
        for name in NAMES:
            if self.config.profile(name).enabled:
                try:
                    self.adapters[name].ready()
                except ChannelError as error:
                    self._errors[name] = str(error)
                    receive_failed = self._receive_fault(name, unavailable=True) or receive_failed
                else:
                    self._active.add(name)
        self.recover()
        self._tasks = [
            asyncio.create_task(self._outbox(), name="muli-channel-outbox"),
            asyncio.create_task(self._events(), name="muli-channel-events"),
        ]
        for poll_name in POLLING_FAILURES:
            if poll_name in self._active:
                self._tasks.append(
                    asyncio.create_task(self._poll(poll_name), name="muli-channel-" + poll_name)
                )
        self._wake.set()
        if receive_failed:
            await self._publish()

    def recover(self) -> None:
        """Pure local crash recovery; probation never needs to start adapters."""
        if self._recovered:
            return
        with self.store.db:
            # A process may have died after remote acceptance but before local commit.
            self.store.db.execute(
                "UPDATE channel_outbox SET state='unknown',last_error='重启前发送结果未确认',updated_at=? "
                "WHERE state='sending'",
                (now(),),
            )
            if not self.store.db.execute(
                "SELECT 1 FROM channel_state WHERE name='events'"
            ).fetchone():
                latest = self.store.db.execute("SELECT COALESCE(max(id),0) FROM events").fetchone()[
                    0
                ]
                self.store.db.execute(
                    "INSERT INTO channel_state VALUES('events',?)", (str(latest),)
                )
        self._recovered = True

    async def close(self) -> None:
        self._closed = True
        tasks, self._tasks = self._tasks, []
        tasks = list(set(tasks).union(self._incoming) - {asyncio.current_task()})
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        if self._owns_client:
            await self.client.aclose()
        self._stopped = True
        async with self.changed:
            self.changed.notify_all()

    @property
    def receiving(self) -> bool:
        return bool(self._tasks) and not self._closed

    def status(self) -> dict[str, Any]:
        counts = {
            row[0]: row[1]
            for row in self.store.db.execute(
                "SELECT state,count(*) FROM channel_outbox GROUP BY state"
            ).fetchall()
        }
        legacy = self.store.db.execute(
            "SELECT count(*),COALESCE(sum(receipt IS NULL),0) FROM channel_inbound WHERE fingerprint IS NULL"
        ).fetchone()
        channels = {}
        for name in NAMES:
            profile = self.config.profile(name)
            channels[name] = {
                "account_id": self._account(name),
                "enabled": profile.enabled,
                "active": name in self._active,
                "owner_count": len(profile.owner_ids),
                "group_count": len(profile.allowed_groups),
                "receive_files": profile.receive_files,
                "voice_supported": bool(self.adapters[name].voice_mime_types),
                "voice_mime_types": sorted(self.adapters[name].voice_mime_types),
                "last_error": self._errors.get(name, ""),
            }
            if name in self._receive_health:
                channels[name]["receive_health"] = dict(self._receive_health[name])
        return {
            "running": bool(self._tasks),
            "channels": channels,
            "outbox": counts,
            "inbound": {"legacy_unverified": legacy[0], "legacy_incomplete": legacy[1]},
            "max_pending_deliveries": self.config.max_pending_deliveries,
        }

    def _route(self, name: str, route: Mapping[str, Any], *, outbound: bool) -> dict[str, Any]:
        profile = self.config.profile(name)
        try:
            return authorize_route(
                name,
                route,
                account_id=self._account(name),
                owner_ids=profile.owner_ids,
                allowed_groups=profile.allowed_groups,
                outbound=outbound,
            )
        except RouteError as error:
            raise ChannelError(str(error), status_code=error.status_code) from None

    def _automatic_guard(self, name: str, route: Mapping[str, Any], metadata: object) -> None:
        try:
            if not self.config.profile(name).enabled:
                raise ChannelError("原回复渠道未启用", status_code=404)
            self._route(name, route, outbound=True)
            if name not in self._active:
                raise ChannelError("原回复渠道尚未就绪")
            if self.delivery_guard is not None:
                self.delivery_guard(route, metadata)
        except ValueError as error:
            raise _BlockedDelivery(str(error), status_code=403) from None

    def _outbox_guard(
        self, name: str, route: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> None:
        if "channel_ownership" in payload:
            try:
                if self.ownership_guard is None:
                    raise PermissionError("渠道归属投递没有当前验证者")
                self.ownership_guard(payload["channel_ownership"], route)
            except (ValueError, PermissionError) as error:
                raise _BlockedDelivery(str(error), status_code=403) from None
        if name in PRIVATE_OWNER_CHANNELS:
            try:
                self._route(name, route, outbound=True)
            except ChannelError as error:
                raise _BlockedDelivery(str(error), status_code=error.status_code) from None
        if payload.get("automatic"):
            self._automatic_guard(name, route, payload.get("attention_routing"))
        if "approval_notice" not in payload:
            return
        try:
            notice = ApprovalNotice.load(payload["approval_notice"])
            if (
                self._closed
                or not self.admission.allowed
                or payload.get("automatic") is not True
                or notice.reply_origin.delivery_routes() != [dict(route)]
                or FrozenRouting.bound(notice.reply_origin).dump()
                != payload.get("attention_routing")
                or self.approval_guard is None
            ):
                raise ValueError("审批通知来源或执行已失效，保留在本地")
            self.approval_guard(notice)
        except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as error:
            raise _BlockedDelivery(str(error), status_code=403) from None

    def _block_revoked(self) -> tuple[bool, bool]:
        # Page through pending work, including queues left above a reduced limit.
        # Sent/unknown rows are never reconsidered; each turn parses at most 100.
        rows = self.store.db.execute(
            "SELECT id,name,route,payload FROM channel_outbox WHERE state IN ('pending','retry') "
            "AND json_extract(payload,'$.automatic')=1 AND id>? ORDER BY id LIMIT 101",
            (self._revocation_after,),
        ).fetchall()
        more = len(rows) > 100
        rows = rows[:100]
        self._revocation_after = rows[-1]["id"] if more else ""
        changed = False
        with self.store.db:
            for row in rows:
                try:
                    self._outbox_guard(
                        row["name"],
                        json.loads(row["route"]),
                        json.loads(row["payload"]),
                    )
                except _BlockedDelivery as error:
                    self.store.db.execute(
                        "UPDATE channel_outbox SET state='blocked',last_error=?,updated_at=? WHERE id=?",
                        (str(error), now(), row["id"]),
                    )
                    changed = True
        return changed, more

    async def webhook(self, name: str, raw: bytes, headers: Mapping[str, str]) -> dict[str, Any]:
        profile = self.config.profile(name)
        if not profile.enabled:
            raise ChannelError("渠道未启用", status_code=404)
        if len(raw) > profile.max_response_bytes:
            raise ChannelError("渠道 webhook 超过大小上限", status_code=413)
        adapter = self.adapters[name]
        adapter.ready()
        normalized = {key.lower(): value for key, value in headers.items()}
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ChannelError("渠道 webhook JSON 无效") from None
        if not isinstance(payload, dict):
            raise ChannelError("渠道 webhook 需要 JSON 对象")
        if name == "qq" and payload.get("op") == 13:
            if normalized.get("x-bot-appid") != profile.app_id:
                raise ChannelError("QQ webhook app_id 不匹配", status_code=401)
            assert isinstance(adapter, QQ)
            # QQ's documented validation handshake is not a signed Dispatch event.
            return adapter.validation(payload) or {}
        adapter.authenticate(raw, normalized)
        for message in adapter.webhook(payload):
            try:
                await self.receive(name, message)
            except ChannelError as error:
                if error.status_code != 403:
                    raise
        return {"op": 12} if name == "qq" else {"ok": True}

    async def receive(self, name: str, message: IncomingMessage) -> dict[str, Any]:
        if self._closed:
            raise ChannelError("渠道正在关闭", status_code=503)
        task = asyncio.current_task()
        assert task is not None
        self._incoming.add(task)
        try:
            return await self._receive(name, message)
        finally:
            self._incoming.discard(task)

    async def _receive(self, name: str, message: IncomingMessage) -> dict[str, Any]:
        profile = self.config.profile(name)
        if not profile.enabled:
            raise ChannelError("渠道未启用", status_code=404)
        try:
            channel_reference = normalize_channel_reference(message.channel_reference)
        except ValueError as error:
            raise ChannelError(str(error)) from None
        if channel_reference is not None and name != "telegram":
            raise ChannelError("原生引用当前仅支持 Telegram 私聊", status_code=403)
        # This shared gate also covers direct receive(), before fingerprints,
        # attachment staging or a durable inbound receipt can be created.
        route = self._route(
            name,
            {
                "sender_id": message.sender_id,
                "target": message.target,
                "kind": message.kind,
                "reply_to": message.reply_to,
            },
            outbound=False,
        )
        if not message.event_id or len(message.event_id) > 512 or len(message.text) > 64000:
            raise ChannelError("渠道消息 ID 或内容长度无效")
        trusted = message.sender_id in profile.owner_ids
        control = is_mobile_control_text(message.text)
        if control:
            self.control_guard(name, message, route)
            if self.control_submit is None:
                raise ChannelError("手机任务控制服务未启用", status_code=503)
        key = hashlib.sha256(
            encoded([name, self._account(name), message.event_id]).encode("utf-8")
        ).hexdigest()
        try:
            envelope = asdict(message)
            # Preserve fingerprints of ordinary messages accepted before control
            # evidence existed. Nonempty evidence remains part of the envelope.
            for field in ("control_text", "control_account_id", "channel_reference"):
                if not envelope[field]:
                    del envelope[field]
            fingerprint = hashlib.sha256(encoded(envelope).encode("utf-8")).hexdigest()
            legacy_fingerprint = ""
            if control:
                old_envelope = dict(envelope)
                old_envelope.pop("control_text", None)
                old_envelope.pop("control_account_id", None)
                legacy_fingerprint = hashlib.sha256(
                    encoded(old_envelope).encode("utf-8")
                ).hexdigest()
            elif channel_reference is not None:
                old_envelope = dict(envelope)
                old_envelope.pop("channel_reference")
                legacy_fingerprint = hashlib.sha256(
                    encoded(old_envelope).encode("utf-8")
                ).hexdigest()
        except (TypeError, ValueError):
            raise ChannelError("渠道消息信封无法规范化") from None
        async with self._inbound_lock:
            if control:
                row = self.store.db.execute(
                    "SELECT fingerprint IS NULL AS unverified,"
                    "CASE WHEN typeof(fingerprint)='text' "
                    "AND length(CAST(fingerprint AS BLOB))=? "
                    "THEN fingerprint=? COLLATE BINARY ELSE 0 END AS matched,"
                    "CASE WHEN typeof(fingerprint)='text' "
                    "AND length(CAST(fingerprint AS BLOB))=? "
                    "THEN fingerprint=? COLLATE BINARY ELSE 0 END AS legacy_matched,"
                    "receipt IS NOT NULL AS has_receipt FROM channel_inbound WHERE id=?",
                    (
                        len(fingerprint),
                        fingerprint,
                        len(legacy_fingerprint),
                        legacy_fingerprint,
                        key,
                    ),
                ).fetchone()
                if row is not None:
                    if row["unverified"] or (row["legacy_matched"] and row["has_receipt"]):
                        # Refuse only: neither the old JSON nor any of its fields
                        # become authority, and immutable history stays untouched.
                        raise ChannelError(
                            "旧入站回执不是手机任务票据，请重新发送 /任务", status_code=403
                        )
                    if not row["matched"]:
                        raise ChannelError("同一渠道消息 ID 的信封内容不一致", status_code=409)
                else:
                    with self.store.db:
                        self.store.db.execute(
                            "INSERT INTO channel_inbound(id,name,created_at,fingerprint) "
                            "VALUES(?,?,?,?)",
                            (key, name, now(), fingerprint),
                        )
                # Only the core service reads and validates a stored control
                # receipt, under its SQL byte gates and configuration lock.
                return await self._receive_control(name, message, route, key, None)
            row = self.store.db.execute(
                "SELECT * FROM channel_inbound WHERE id=?", (key,)
            ).fetchone()
            if row is not None:
                if row["fingerprint"] is None:
                    if row["receipt"] is None:
                        raise ChannelError(
                            "旧入站记录缺少内容指纹，需人工核对后再恢复", status_code=409
                        )
                    self._errors[name] = "旧入站记录缺少内容指纹，重投内容未核验"
                    return {**json.loads(row["receipt"]), "fingerprint_verified": False}
                if row["fingerprint"] != fingerprint:
                    if (
                        row["receipt"] is not None
                        and legacy_fingerprint
                        and row["fingerprint"] == legacy_fingerprint
                    ):
                        # An old completed event has no reference evidence.
                        # Return it unchanged; never upgrade or replay its input.
                        return dict(json.loads(row["receipt"]))
                    raise ChannelError("同一渠道消息 ID 的信封内容不一致", status_code=409)
                if row["receipt"] is not None:
                    return dict(json.loads(row["receipt"]))
            if row is None:
                with self.store.db:
                    self.store.db.execute(
                        "INSERT INTO channel_inbound(id,name,created_at,fingerprint) VALUES(?,?,?,?)",
                        (key, name, now(), fingerprint),
                    )
            staged = json.loads(row["attachment_ids"]) if row else []
            if isinstance(staged, list):
                staged = {"ids": staged, "processed": len(staged), "warnings": [], "attempts": 0}
            files = staged["ids"]

            def checkpoint() -> None:
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE channel_inbound SET attachment_ids=? WHERE id=?",
                        (encoded(staged), key),
                    )

            wanted = (
                message.files[: profile.max_attachments]
                if trusted and profile.receive_files and self.attachments is not None
                else ()
            )
            for index, remote in enumerate(wanted[staged["processed"] :], staged["processed"]):
                await self.admission.wait()
                with self.admission.operation():
                    if staged["attempts"] >= self.config.max_attempts:
                        staged["warnings"].append("附件多次接收超时，未自动继续下载")
                        staged["processed"], staged["attempts"] = index + 1, 0
                        checkpoint()
                        continue
                    staged["attempts"] += 1
                    checkpoint()
                    try:
                        async with asyncio.timeout(profile.timeout):
                            content = await self.adapters[name].download(remote, route)
                            attachment = await self._save_file(content, name)
                    except ChannelError as error:
                        if error.retryable and staged["attempts"] < self.config.max_attempts:
                            raise
                        staged["warnings"].append("附件未接收：" + str(error))
                    except ValueError:
                        staged["warnings"].append("附件未接收：文件名或本地存储配额不允许")
                    else:
                        files.append(attachment["id"])
                    staged["processed"], staged["attempts"] = index + 1, 0
                    checkpoint()
            if trusted and message.context_token:
                if len(message.context_token) > profile.max_response_bytes:
                    raise ChannelError("渠道 context_token 超限")
                self._set_state(
                    "context:" + name + ":" + self._account(name) + ":" + message.target,
                    message.context_token,
                )
            text = message.text
            if not text.strip() and message.files:
                text = "收到附件，请按需查看。" if files else "收到附件；当前未自动下载。"
            if trusted and len(message.files) > profile.max_attachments:
                text += "\n[超过 max_attachments 的附件未接收]"
            if staged["warnings"]:
                text += "\n[" + "；".join(staged["warnings"]) + "]"
            if not text.strip():
                receipt: dict[str, Any] = {"state": "ignored"}
            else:
                try:
                    receipt = await self.submit(
                        content=text,
                        source="channel:" + name + ":" + self._account(name),
                        principal="owner"
                        if trusted
                        else "external:" + name + ":" + message.sender_id,
                        route=route,
                        event_id=key,
                        attachment_ids=files,
                        _owner_text_chars=len(message.text)
                        if trusted and message.text.strip()
                        else None,
                        _channel_reference=channel_reference,
                    )
                except ValueError:
                    if trusted:
                        raise
                    # A full quarantine must not hold the platform cursor in front of owner input.
                    receipt = {"state": "quarantine_full", "quarantined": True}
            with self.store.db:
                self.store.db.execute(
                    "UPDATE channel_inbound SET receipt=? WHERE id=?", (encoded(receipt), key)
                )
            await self.bus.emit(
                "channel.received",
                {
                    "name": name,
                    "event_id": key,
                    "trusted_owner": trusted,
                    "state": receipt.get("state"),
                },
            )
            return receipt

    def control_guard(self, name: str, message: IncomingMessage, route: Mapping[str, Any]) -> None:
        """Synchronous source checks, repeated by the runtime's final guard."""
        if (
            self._closed
            or name not in {"telegram", "onebot"}
            or not self.config.profile(name).enabled
            or message.sender_id not in self.config.profile(name).owner_ids
            or message.kind != "private"
            or message.files
            or message.context_token
            or message.channel_reference is not None
            or not message.control_text
            or not is_mobile_control_text(message.control_text)
            or message.control_text != message.text
            or not message.control_account_id
            or message.control_account_id != self._account(name)
        ):
            raise ChannelError(
                "任务控制仅接受当前已绑定主人原始私聊纯文本与可核验账号", status_code=403
            )
        self._route(name, route, outbound=True)

    async def _receive_control(
        self,
        name: str,
        message: IncomingMessage,
        route: dict[str, Any],
        key: str,
        previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        assert self.control_submit is not None
        receipt = await self.control_submit(
            name=name, message=message, route=route, event_id=key, receipt=previous
        )
        # The core service already committed this receipt while holding the
        # configuration lock. Retrying only repairs this local idempotent queue.
        reply = receipt.get("reply_text")
        if not isinstance(reply, str) or not reply:
            raise ChannelError("手机任务控制回执缺少明确回复")
        await self.enqueue(name, message.target, reply, route=route, event_id="mobile-task:" + key)
        return receipt

    async def _save_file(self, file: FileContent, name: str) -> dict[str, Any]:
        assert self.attachments is not None

        async def chunks() -> AsyncIterator[bytes]:
            yield file.data

        return await self.attachments.save(
            chunks(), file.name, file.mime_type, source="channel:" + name
        )

    def _receive_current(self, name: str) -> bool:
        task = asyncio.current_task()
        return (
            not self._closed
            and self.config.profile(name).enabled
            and task is not None
            and not task.cancelling()
            and task in self._tasks
        )

    def _receive_fault(self, name: str, *, unavailable: bool = False) -> bool:
        task = asyncio.current_task()
        if (
            name not in self._receive_health
            or self._closed
            or not self.config.profile(name).enabled
            or task is None
            or task.cancelling()
            or self._receive_health[name]["state"] == "failed"
        ):
            return False
        code = POLLING_FAILURES[name][0 if unavailable else 1]
        occurrence = new_id()
        self._receive_health[name] = {"state": "failed", "code": code, "occurrence_id": occurrence}
        if self.on_receive_fault is not None:
            try:
                self.on_receive_fault(code, occurrence)
            except Exception:
                # Diagnostics must not change the original retry/cursor behavior.
                pass
        return True

    async def _poll(self, name: str) -> None:
        key = "poll:" + name + ":" + self._account(name)
        while self._receive_current(name):
            try:
                batch = await self.adapters[name].poll(self._state(key))
                if not self._receive_current(name):
                    return
                for message in batch.messages:
                    try:
                        await self.receive(name, message)
                    except ChannelError as error:
                        if error.status_code == 403:
                            continue  # Unbound groups are not a retryable delivery.
                        raise
                if not self._receive_current(name):
                    return
                self._set_state(key, batch.cursor)
                recovered = self._receive_health[name]["state"] != "ready"
                self._receive_health[name] = {"state": "ready", "code": None, "occurrence_id": None}
                had_error = self._errors.pop(name, None) is not None
                if recovered or had_error:
                    await self._publish()
                if not batch.messages:
                    # Real servers long-poll; this also bounds an immediate empty/error response loop.
                    await asyncio.sleep(min(1.0, self.config.retry_backoff_seconds or 0.1))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not self._receive_current(name):
                    return
                detail = str(error) if isinstance(error, ChannelError) else type(error).__name__
                changed = self._errors.get(name) != detail
                self._errors[name] = detail
                failed = self._receive_fault(name)
                if failed or changed:
                    await self._publish()
                delay = max(
                    self.config.retry_backoff_seconds,
                    error.retry_after if isinstance(error, ChannelError) else 0,
                    0.1,
                )
                await asyncio.sleep(delay)

    async def enqueue(
        self,
        name: str,
        target: str,
        text: str = "",
        attachment_ids: Sequence[str] = (),
        *,
        event_id: str | None = None,
        route: Mapping[str, Any] | None = None,
        media_mode: str = "file",
        automatic: bool = False,
        attention_routing: dict[str, Any] | None = None,
        approval_notice: ApprovalNotice | None = None,
        admission_guard: Callable[[], None] | None = None,
        ownership: dict[str, Any] | None = None,
        continuity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if admission_guard is not None:
            admission_guard()
        profile = self.config.profile(name)
        metadata = (
            FrozenRouting.load(attention_routing).dump() if attention_routing is not None else None
        )
        if metadata is not None and not automatic:
            raise ChannelError("主动投递证据只能来自宿主自动回复")
        notice_data = None
        if approval_notice is not None:
            if not automatic or attachment_ids or media_mode != "file":
                raise ChannelError("审批通知仅接纳宿主纯文本自动投递")
            notice_data = ApprovalNotice.load(approval_notice.dump()).dump()
        blocked = ""
        if automatic:
            # Preserve the original account and recipient even when today's
            # binding no longer authorizes sending. Only finite host metadata
            # enters the ledger; no arbitrary event dictionary is persisted.
            candidate = {
                "name": name,
                "account_id": self._account(name),
                "kind": "private",
                **dict(route or {}),
            }
            origin = ReplyOrigin.load({"kind": "channel", "input_id": event_id, "route": candidate})
            routes = origin.delivery_routes()
            if not routes:
                raise ChannelError("自动回复来源无效，已保留在本地")
            selected = routes[0]
            try:
                self._automatic_guard(name, selected, metadata)
            except ChannelError as error:
                blocked = str(error)
        else:
            if not profile.enabled:
                raise ChannelError("渠道未启用", status_code=404)
            selected = self._route(
                name,
                route or {"target": target, "sender_id": target, "kind": "private"},
                outbound=True,
            )
        if selected["target"] != target:
            raise ChannelError("渠道目标与路由不一致")
        if media_mode not in {"file", "voice"}:
            raise ChannelError("渠道 media_mode 仅支持 file 或 voice")
        if event_id is not None and (
            not isinstance(event_id, str)
            or not 1 <= len(event_id) <= 256
            or any(ord(character) < 32 or ord(character) == 127 for character in event_id)
        ):
            raise ChannelError("渠道幂等键需要 1–256 字符的非控制字符串")
        if media_mode == "voice":
            if len(attachment_ids) != 1:
                raise ChannelError("原生语音需要恰好一个本地音频附件")
            if not self.adapters[name].voice_mime_types:
                raise ChannelError("当前渠道适配器不支持原生语音发送")
        if (
            not isinstance(text, str)
            or len(text) > MAX_OUTGOING_TEXT_CHARS
            or len(attachment_ids) > profile.max_attachments
        ):
            raise ChannelError("渠道文本或附件数量超过上限")
        if not text and not attachment_ids:
            raise ChannelError("渠道消息不能为空")
        parts: list[dict[str, Any]] = []
        chunk_size = min(
            profile.max_message_chars, 4096 if name == "telegram" else profile.max_message_chars
        )
        parts.extend(
            {"text": text[offset : offset + chunk_size]}
            for offset in range(0, len(text), chunk_size)
        )
        for attachment_id in attachment_ids:
            if blocked:
                if (
                    not isinstance(attachment_id, str)
                    or not attachment_id
                    or len(attachment_id) > 256
                ):
                    raise ChannelError("渠道附件引用无效")
                parts.append({"attachment_id": attachment_id})
                continue
            if self.attachments is None:
                raise ChannelError("附件服务未启用")
            try:
                item = self.attachments.get(attachment_id)
            except KeyError:
                raise ChannelError("渠道附件不存在或已删除") from None
            if item["size"] > profile.max_file_bytes:
                raise ChannelError("渠道附件超过 max_file_bytes")
            part: dict[str, Any] = {"attachment_id": attachment_id}
            if media_mode == "voice":
                # Validate before committing any text or media part. Dispatch
                # validates the bytes again, including the stored digest.
                await self._file(attachment_id, name, media_mode=media_mode)
                part["media_mode"] = media_mode
            parts.append(part)
        if self._closed:
            raise ChannelError("渠道管理器已关闭，请使用当前配置重新发送")
        if admission_guard is not None:
            admission_guard()
        payload: dict[str, Any] = {"parts": parts}
        if ownership is not None:
            if self.ownership_guard is None:
                raise ChannelError("渠道归属投递缺少宿主验证")
            self.ownership_guard(ownership, selected)
            payload["channel_ownership"] = dict(ownership)
        continuity_data = continuity_reference(continuity)
        if continuity_data is not None:
            payload["channel_continuity"] = continuity_data
        if automatic:
            payload["automatic"] = True
        if metadata is not None:
            payload["attention_routing"] = metadata
        if notice_data is not None:
            payload["approval_notice"] = notice_data
        identifier = (
            hashlib.sha256(encoded([event_id, name, selected]).encode("utf-8")).hexdigest()
            if event_id
            else new_id()
        )
        existing = self.delivery(identifier)
        if existing is not None:
            plain = {
                "parts": [
                    {key: value for key, value in part.items() if key != "sequence"}
                    for part in existing["payload"]["parts"]
                ]
            }
            if (
                plain["parts"] != parts
                or existing["route"] != selected
                or existing["payload"].get("attention_routing") != metadata
                or existing["payload"].get("approval_notice") != notice_data
                or (
                    existing["payload"].get("channel_continuity") is not None
                    and existing["payload"].get("channel_continuity") != continuity_data
                )
            ):
                raise ChannelError("相同投递 ID 的内容或发送模式不一致", status_code=409)
            return existing
        count = self.store.db.execute(
            "SELECT count(*) FROM channel_outbox WHERE state IN ('pending','retry','sending')"
        ).fetchone()[0]
        if automatic and not blocked:
            try:
                self._outbox_guard(name, selected, payload)
            except _BlockedDelivery as error:
                blocked = str(error)
        if not blocked and count >= self.config.max_pending_deliveries:
            raise ChannelError("渠道投递队列已满", retryable=True, status_code=429)
        timestamp = now()
        with self.store.db:
            if automatic and not blocked:
                try:
                    self._outbox_guard(name, selected, payload)
                except _BlockedDelivery as error:
                    blocked = str(error)
            if not blocked:
                try:
                    self._reserve_reply_parts(name, selected, parts)
                except ChannelError as error:
                    if not automatic:
                        raise
                    blocked = str(error)
            self.store.db.execute(
                "INSERT INTO channel_outbox(id,name,route,payload,state,last_error,next_attempt,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    name,
                    encoded(selected),
                    encoded(payload),
                    "blocked" if blocked else "pending",
                    blocked,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        self._wake.set()
        await self._publish()
        result = self.delivery(identifier)
        assert result is not None
        return result

    def _reserve_reply_parts(
        self, name: str, route: Mapping[str, Any], parts: list[dict[str, Any]]
    ) -> None:
        """Reserve QQ's passive reply slots inside the caller's existing transaction."""
        if name != "qq" or not route.get("reply_to"):
            return
        if parts and all("sequence" in part for part in parts):
            return  # A blocked later segment keeps the original reservation.
        state_key = "qq_sequence:" + encoded(
            [route["account_id"], route["target"], route["reply_to"]]
        )
        used = int(self._state(state_key, "0"))
        if used + len(parts) > 5:
            raise ChannelError("QQ 同一被动消息的五次回复预算不足")
        for index, part in enumerate(parts):
            part["sequence"] = used + index
        self.store.db.execute(
            "INSERT INTO channel_state VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (state_key, str(used + len(parts))),
        )

    def delivery(self, identifier: str) -> dict[str, Any] | None:
        row = self.store.db.execute(
            "SELECT * FROM channel_outbox WHERE id=?", (identifier,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        for key in ("route", "payload", "receipts"):
            item[key] = json.loads(item[key])
        return item

    def deliveries(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.store.db.execute(
            "SELECT id FROM channel_outbox ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [item for row in rows if (item := self.delivery(row[0])) is not None]

    def _cancellation_record(
        self, identifier: str, *, name: str, owner_id: str, max_bytes: int
    ) -> tuple[dict[str, Any], tuple[bytes, bytes, bytes]]:
        # The primary-key lookup admits the complete evidence before exporting
        # any JSON. A valid-looking prefix is never enough to cancel a record.
        bounded = (
            "typeof(name)='text' AND typeof(state)='text' AND typeof(route)='text' "
            "AND typeof(payload)='text' AND typeof(receipts)='text' "
            "AND length(CAST(name AS BLOB))+length(CAST(state AS BLOB))"
            "+length(CAST(route AS BLOB))+length(CAST(payload AS BLOB))"
            "+length(CAST(receipts AS BLOB))<=:budget"
        )
        valid = "json_valid(route) AND json_valid(payload) AND json_valid(receipts)"
        columns = ",".join(
            f"CASE WHEN {bounded} THEN CASE WHEN {valid} THEN CAST({field} AS BLOB) END END AS {field}"
            for field in ("route", "payload", "receipts")
        )
        row = self.store.db.execute(
            f"SELECT CASE WHEN {bounded} THEN name END AS name,"
            f"CASE WHEN {bounded} THEN state END AS state,"
            "CASE WHEN typeof(next_part)='integer' THEN next_part END AS next_part,"
            + columns
            + " FROM channel_outbox WHERE id=:id",
            {"id": identifier, "budget": max_bytes},
        ).fetchone()
        if row is None:
            raise ChannelError("投递不存在或不属于所选主人私聊", status_code=404)
        if row["name"] is None:
            raise ChannelError("投递证据损坏或超过核查预算，未取消", status_code=409)
        if row["name"] != name:
            raise ChannelError("投递不存在或不属于所选主人私聊", status_code=404)
        raw = tuple(row[field] for field in ("route", "payload", "receipts"))
        if any(not isinstance(value, bytes) for value in raw):
            raise ChannelError("投递证据损坏或超过核查预算，未取消", status_code=409)
        route_raw, payload_raw, receipts_raw = raw
        try:
            route, payload = _object(route_raw), _object(payload_raw)
            receipts = json.loads(decode_utf8(receipts_raw, allow_incomplete_tail=False))
            # Reject duplicate object keys, nonfinite metadata and escaped lone
            # surrogates rather than manufacturing a current source identity.
            json.dumps([route, payload, receipts], ensure_ascii=False, allow_nan=False).encode(
                "utf-8", errors="strict"
            )
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise ChannelError("投递证据不能完整核验，未取消", status_code=409) from None
        if (
            route.get("name") != name
            or route.get("account_id") != self._account(name)
            or route.get("kind") != "private"
            or route.get("sender_id") != owner_id
            or route.get("target") != owner_id
        ):
            raise ChannelError("投递不存在或不属于当前账号与主人私聊", status_code=404)
        self._route(name, route, outbound=True)
        parts = payload.get("parts")
        confirmed, state = row["next_part"], row["state"]
        if (
            not isinstance(parts, list)
            or not parts
            or any(
                not isinstance(part, dict)
                or not (
                    (isinstance(part.get("text"), str) and "attachment_id" not in part)
                    or (
                        isinstance(part.get("attachment_id"), str)
                        and bool(part["attachment_id"])
                        and "text" not in part
                    )
                )
                for part in parts
            )
            or type(confirmed) is not int
            or not 0 <= confirmed <= len(parts)
            or not isinstance(receipts, list)
            or any(not isinstance(receipt, str) or not receipt for receipt in receipts)
            or len(receipts) != confirmed
            or state
            not in {
                "pending",
                "retry",
                "sending",
                "sent",
                "failed",
                "blocked",
                "unknown",
                "cancelled",
            }
            or (state == "cancelled" and confirmed != 0)
        ):
            raise ChannelError("投递进度或回执不能完整核验，未取消", status_code=409)
        return (
            {
                "id": identifier,
                "channel": name,
                "owner_id": owner_id,
                "state": state,
                "cancelled": state == "cancelled",
                "confirmed_parts": confirmed,
                "total_parts": len(parts),
            },
            (route_raw, payload_raw, receipts_raw),
        )

    async def cancel(
        self,
        identifier: str,
        *,
        name: str,
        owner_id: str,
        max_bytes: int,
        admission_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Commit no future dispatch; this cannot recall past platform effects."""
        self.admission.require()
        if (
            self._closed
            or not isinstance(identifier, str)
            or not identifier
            or "\x00" in identifier
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= SQLITE_MAX_INTEGER
        ):
            raise ChannelError("取消需要当前管理器、准确投递 ID 与有效核查预算")
        try:
            if len(identifier.encode("utf-8", errors="strict")) > max_bytes:
                raise ValueError
        except (ValueError, UnicodeError):
            raise ChannelError("投递 ID 超过预算或包含无效字符") from None
        profile = self.config.profile(name)
        if (
            not profile.enabled
            or not isinstance(owner_id, str)
            or owner_id not in profile.owner_ids
        ):
            raise ChannelError("只能取消当前已启用渠道的已绑定主人私聊", status_code=403)
        if admission_guard is not None:
            admission_guard()
        changed = False
        with self.store.db:
            result, raw = self._cancellation_record(
                identifier, name=name, owner_id=owner_id, max_bytes=max_bytes
            )
            if result["state"] in {"pending", "retry"} and result["confirmed_parts"] == 0:
                if admission_guard is not None:
                    admission_guard()
                changed = (
                    self.store.db.execute(
                        "UPDATE channel_outbox SET state='cancelled',updated_at=? "
                        "WHERE id=? AND name=? AND state IN ('pending','retry') "
                        "AND typeof(next_part)='integer' AND next_part=0 "
                        "AND CAST(route AS BLOB)=? AND CAST(payload AS BLOB)=? AND CAST(receipts AS BLOB)=?",
                        (now(), identifier, name, *raw),
                    ).rowcount
                    == 1
                )
                if changed:
                    result.update(state="cancelled", cancelled=True)
                else:
                    result, _ = self._cancellation_record(
                        identifier, name=name, owner_id=owner_id, max_bytes=max_bytes
                    )
        if changed:
            self._wake.set()
            try:
                await self._publish()
            except Exception:
                # The durable decision stands even when event publication is
                # unavailable. Existing waiters must still observe its terminal.
                async with self.changed:
                    self.changed.notify_all()
        return result

    async def retry(self, identifier: str, *, allow_unknown: bool = False) -> dict[str, Any]:
        item = self.delivery(identifier)
        if item is None:
            raise KeyError(identifier)
        if item["state"] == "unknown" and not allow_unknown:
            raise ChannelError("发送结果未知；必须明确允许可能重复的人工重试")
        if item["state"] not in {"failed", "unknown", "blocked"}:
            return item
        if not self.config.profile(item["name"]).enabled:
            raise ChannelError("原回复渠道未启用", status_code=404)
        self._route(item["name"], item["route"], outbound=True)
        self._outbox_guard(item["name"], item["route"], item["payload"])
        if item["state"] == "blocked" and item["name"] not in self._active:
            raise ChannelError("原回复渠道尚未就绪")
        if (
            self.store.db.execute(
                "SELECT count(*) FROM channel_outbox WHERE state IN ('pending','retry','sending')"
            ).fetchone()[0]
            >= self.config.max_pending_deliveries
        ):
            raise ChannelError("渠道投递队列已满", status_code=429)
        with self.store.db:
            if item["state"] == "blocked":
                self._reserve_reply_parts(item["name"], item["route"], item["payload"]["parts"])
            self.store.db.execute(
                "UPDATE channel_outbox SET state='pending',payload=?,attempts=0,next_attempt=?,last_error='',updated_at=? WHERE id=?",
                (encoded(item["payload"]), now(), now(), identifier),
            )
        self._wake.set()
        await self._publish()
        result = self.delivery(identifier)
        assert result is not None
        return result

    async def wait(self, identifier: str) -> dict[str, Any] | None:
        def terminal() -> bool:
            item = self.delivery(identifier)
            return (
                self._stopped
                or item is None
                or item["state"] in {"sent", "failed", "unknown", "blocked", "cancelled"}
            )

        async with self.changed:
            await self.changed.wait_for(terminal)
        return self.delivery(identifier)

    async def _publish(self) -> None:
        async with self.changed:
            self.changed.notify_all()
        await self.bus.emit("channel.updated", self.status())

    async def _file(self, identifier: str, name: str, *, media_mode: str = "file") -> FileContent:
        if self.attachments is None:
            raise ChannelError("附件服务未启用")
        item = self.attachments.get(identifier)
        maximum = self.config.profile(name).max_file_bytes
        if item["size"] > maximum:
            raise ChannelError("渠道附件超过上限")
        path = self.attachments.path(identifier)

        def read() -> bytes:
            with path.open("rb") as handle:
                return handle.read(maximum + 1)

        data = await file_io(read)
        if len(data) > maximum or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ChannelError("附件内容已变化或超过上限")
        file = FileContent(item["name"], item["mime_type"], data, media_mode=media_mode)
        self.adapters[name].validate_media(file)
        return file

    async def _outbox(self) -> None:
        while True:
            await self.admission.wait()
            self._wake.clear()
            changed, more = self._block_revoked()
            if changed or more:
                if changed:
                    await self._publish()
                await asyncio.sleep(0)
                continue
            names = sorted(self._active)
            if not names:
                await self._wake.wait()
                continue
            marks = ",".join("?" for _ in names)
            row = self.store.db.execute(
                f"SELECT id FROM channel_outbox WHERE state IN ('pending','retry') AND name IN ({marks}) "
                "AND next_attempt<=? ORDER BY created_at,id LIMIT 1",
                [*names, now()],
            ).fetchone()
            if row is None:
                deadline = self.store.db.execute(
                    f"SELECT min(next_attempt) FROM channel_outbox WHERE state IN ('pending','retry') AND name IN ({marks})",
                    names,
                ).fetchone()[0]
                try:
                    if deadline is None:
                        await self._wake.wait()
                    else:
                        await asyncio.wait_for(self._wake.wait(), max(0.0, deadline - now()))
                except TimeoutError:
                    pass
                continue
            item = self.delivery(row[0])
            assert item is not None
            with self.admission.operation():
                await self._send_item(item)
            await asyncio.sleep(0)

    async def _send_item(self, item: dict[str, Any]) -> None:
        with self.store.db:
            claimed = self.store.db.execute(
                "UPDATE channel_outbox SET state='sending',attempts=attempts+1,updated_at=? "
                "WHERE id=? AND state IN ('pending','retry')",
                (now(), item["id"]),
            ).rowcount
            if not claimed:
                return
            current = self.delivery(item["id"])
            assert current is not None
            item = current
        adapter = self.adapters[item["name"]]
        adapter.delivery_started = False
        try:
            self._outbox_guard(item["name"], item["route"], item["payload"])
            route = self._route(item["name"], item["route"], outbound=True)
            if item["name"] == "weixin":
                route["context_token"] = self._state(
                    "context:weixin:" + self._account("weixin") + ":" + route["target"]
                )
            part = item["payload"]["parts"][item["next_part"]]
            async with asyncio.timeout(self.config.profile(item["name"]).timeout):
                file = (
                    await self._file(
                        part["attachment_id"],
                        item["name"],
                        media_mode=part.get("media_mode", "file"),
                    )
                    if "attachment_id" in part
                    else None
                )
                self._outbox_guard(item["name"], item["route"], item["payload"])
                receipt = await adapter.send(
                    route,
                    part.get("text", ""),
                    file,
                    key=item["id"],
                    sequence=part.get("sequence", item["next_part"]),
                )
            receipts = [*item["receipts"], receipt]
            next_part = item["next_part"] + 1
            with self.store.db:
                self.store.db.execute(
                    "UPDATE channel_outbox SET state=?,next_part=?,attempts=0,next_attempt=?,last_error='',receipts=?,updated_at=? WHERE id=?",
                    (
                        "sent" if next_part == len(item["payload"]["parts"]) else "pending",
                        next_part,
                        now(),
                        encoded(receipts),
                        now(),
                        item["id"],
                    ),
                )
        except asyncio.CancelledError:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE channel_outbox SET state=?,last_error=?,updated_at=? WHERE id=?",
                    (
                        "unknown" if adapter.delivery_started else "pending",
                        "关闭时发送结果未确认" if adapter.delivery_started else "",
                        now(),
                        item["id"],
                    ),
                )
            raise
        except Exception as error:
            safe = str(error) if isinstance(error, ChannelError) else type(error).__name__
            uncertain = (
                error.uncertain if isinstance(error, ChannelError) else adapter.delivery_started
            )
            retryable = isinstance(error, ChannelError) and error.retryable
            attempts = item["attempts"]
            state = (
                "unknown"
                if uncertain
                else "blocked"
                if isinstance(error, _BlockedDelivery)
                else "retry"
                if retryable and attempts < self.config.max_attempts
                else "failed"
            )
            delay = max(
                self.config.retry_backoff_seconds * attempts,
                error.retry_after if isinstance(error, ChannelError) else 0,
            )
            with self.store.db:
                self.store.db.execute(
                    "UPDATE channel_outbox SET state=?,next_attempt=?,last_error=?,updated_at=? WHERE id=?",
                    (state, now() + delay, safe, now(), item["id"]),
                )
        await self._publish()

    async def _events(self) -> None:
        after = int(self._state("events", "0"))
        while True:
            revision = self.bus.revision

            def changed(expected: int = revision) -> bool:
                return self.bus.revision != expected

            events = self.store.events(after, limit=100)
            for event in events:
                if event["type"] == "approval.requested" and "approval_notice" in event["data"]:
                    try:
                        data = event["data"]
                        notice = ApprovalNotice.load(data["approval_notice"])
                        if (
                            data.get("id") != notice.approval_id
                            or data.get("session_id") != notice.session_id
                            or data.get("run_id") != notice.run_id
                            or event["session_id"] != notice.session_id
                            or not all(
                                isinstance(data.get(key), str) and data[key]
                                for key in ("tool", "risk")
                            )
                        ):
                            raise ValueError("审批通知与原请求不一致")
                        route = notice.reply_origin.delivery_routes()[0]
                        name = route["name"]
                        text = (
                            f"有工具调用等待主人批准：\n{notice.approval_id}\n"
                            f"工具：{data['tool']}；风险：{data['risk']}\n"
                            f"会话：{notice.session_id}\n运行：{notice.run_id}\n"
                            f"详情：/审批 {notice.approval_id}\n"
                            f"批准：/批准 {notice.approval_id}\n拒绝：/拒绝 {notice.approval_id}\n"
                            "批准仅登记决定，不表示操作已执行或成功。"
                        )
                        limit = min(
                            self.config.profile(name).max_message_chars,
                            4096
                            if name == "telegram"
                            else self.config.profile(name).max_message_chars,
                        )
                        if len(text) > limit:
                            raise ChannelError("完整审批通知超过原渠道单条文本预算，保留在 Web")
                        text.encode("utf-8")
                        await self.enqueue(
                            name,
                            route["target"],
                            text,
                            event_id="approval-notice:" + notice.approval_id,
                            route=route,
                            automatic=True,
                            attention_routing=FrozenRouting.bound(notice.reply_origin).dump(),
                            approval_notice=notice,
                        )
                    except ChannelError as error:
                        if error.retryable:
                            # Persist first; retain this event while the existing queue drains.
                            async with self.bus.changed:
                                await self.bus.changed.wait_for(changed)
                            break
                        self._errors["approval_notice"] = str(error)
                    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                        self._errors["approval_notice"] = "审批通知来源或摘要无法核验，保留在 Web"
                # Assistant text/reasoning and tool activity stay in WebUI.
                # External deliveries require an explicit enqueue; the approval
                # protocol above retains its own host-authorized notification.
                after = event["id"]
                self._set_state("events", str(after))
            else:
                if events:
                    async with self.changed:
                        self.changed.notify_all()
                    await asyncio.sleep(0)
                    continue
                async with self.bus.changed:
                    await self.bus.changed.wait_for(changed)
