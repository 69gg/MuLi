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
from muli.events import EventBus
from muli.maintenance import Admission
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin, RouteError, authorize_route, channel_account
from muli.store import Store, new_id, now

from .adapters import QQ, Adapter, OneBot, Telegram
from .types import NAMES, ChannelError, ChannelsConfig, FileContent, IncomingMessage, Submit
from .weixin import Weixin


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


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
        admission: Admission | None = None,
    ) -> None:
        self.store, self.bus, self.config, self.submit = store, bus, config, submit
        self.attachments = attachments
        self.delivery_guard = delivery_guard
        self.admission = admission or Admission()
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
        for name in NAMES:
            if self.config.profile(name).enabled:
                try:
                    self.adapters[name].ready()
                except ChannelError as error:
                    self._errors[name] = str(error)
                else:
                    self._active.add(name)
        self.recover()
        self._tasks = [
            asyncio.create_task(self._outbox(), name="muli-channel-outbox"),
            asyncio.create_task(self._events(), name="muli-channel-events"),
        ]
        for name in ("telegram", "weixin"):
            if name in self._active:
                self._tasks.append(
                    asyncio.create_task(self._poll(name), name="muli-channel-" + name)
                )
        self._wake.set()

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
                    self._automatic_guard(
                        row["name"],
                        json.loads(row["route"]),
                        json.loads(row["payload"]).get("attention_routing"),
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
        key = hashlib.sha256(
            encoded([name, self._account(name), message.event_id]).encode("utf-8")
        ).hexdigest()
        try:
            fingerprint = hashlib.sha256(encoded(asdict(message)).encode("utf-8")).hexdigest()
        except (TypeError, ValueError):
            raise ChannelError("渠道消息信封无法规范化") from None
        async with self._inbound_lock:
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

    async def _save_file(self, file: FileContent, name: str) -> dict[str, Any]:
        assert self.attachments is not None

        async def chunks() -> AsyncIterator[bytes]:
            yield file.data

        return await self.attachments.save(
            chunks(), file.name, file.mime_type, source="channel:" + name
        )

    async def _poll(self, name: str) -> None:
        key = "poll:" + name + ":" + self._account(name)
        while True:
            try:
                batch = await self.adapters[name].poll(self._state(key))
                for message in batch.messages:
                    try:
                        await self.receive(name, message)
                    except ChannelError as error:
                        if error.status_code == 403:
                            continue  # Unbound groups are not a retryable delivery.
                        raise
                self._set_state(key, batch.cursor)
                self._errors.pop(name, None)
                if not batch.messages:
                    # Real servers long-poll; this also bounds an immediate empty/error response loop.
                    await asyncio.sleep(min(1.0, self.config.retry_backoff_seconds or 0.1))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._errors[name] = (
                    str(error) if isinstance(error, ChannelError) else type(error).__name__
                )
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
    ) -> dict[str, Any]:
        profile = self.config.profile(name)
        metadata = (
            FrozenRouting.load(attention_routing).dump() if attention_routing is not None else None
        )
        if metadata is not None and not automatic:
            raise ChannelError("主动投递证据只能来自宿主自动回复")
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
            or len(text) > 100000
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
        payload: dict[str, Any] = {"parts": parts}
        if automatic:
            payload["automatic"] = True
        if metadata is not None:
            payload["attention_routing"] = metadata
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
            ):
                raise ChannelError("相同投递 ID 的内容或发送模式不一致", status_code=409)
            return existing
        count = self.store.db.execute(
            "SELECT count(*) FROM channel_outbox WHERE state IN ('pending','retry','sending')"
        ).fetchone()[0]
        if automatic and not blocked:
            try:
                self._automatic_guard(name, selected, metadata)
            except _BlockedDelivery as error:
                blocked = str(error)
        if not blocked and count >= self.config.max_pending_deliveries:
            raise ChannelError("渠道投递队列已满", retryable=True, status_code=429)
        timestamp = now()
        with self.store.db:
            if automatic and not blocked:
                try:
                    self._automatic_guard(name, selected, metadata)
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
        if item["payload"].get("automatic"):
            self._automatic_guard(
                item["name"], item["route"], item["payload"].get("attention_routing")
            )
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
                or item["state"] in {"sent", "failed", "unknown", "blocked"}
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
        adapter = self.adapters[item["name"]]
        adapter.delivery_started = False
        with self.store.db:
            self.store.db.execute(
                "UPDATE channel_outbox SET state='sending',attempts=attempts+1,updated_at=? WHERE id=?",
                (now(), item["id"]),
            )
        try:
            if item["payload"].get("automatic"):
                self._automatic_guard(
                    item["name"], item["route"], item["payload"].get("attention_routing")
                )
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
                if item["payload"].get("automatic"):
                    self._automatic_guard(
                        item["name"], item["route"], item["payload"].get("attention_routing")
                    )
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
            attempts = item["attempts"] + 1
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
                if event["type"] == "message.completed":
                    message = event["data"].get("message", {})
                    if (
                        isinstance(message, dict)
                        and message.get("role") == "assistant"
                        and not message.get("interrupted")
                    ):
                        routes = message.get("delivery_routes", [])
                        if isinstance(routes, list):
                            for route in routes[:1]:
                                if isinstance(route, dict) and route.get("name") in NAMES:
                                    try:
                                        await self.enqueue(
                                            route["name"],
                                            route.get("target", ""),
                                            str(message.get("content") or ""),
                                            message.get("attachment_ids", []),
                                            event_id=str(message.get("id") or event["id"]),
                                            route=route,
                                            automatic=True,
                                            attention_routing=message.get("attention_routing"),
                                        )
                                    except ChannelError as error:
                                        if error.retryable:
                                            # Keep the cursor; outbox completion emits a wakeup.
                                            async with self.bus.changed:
                                                await self.bus.changed.wait_for(changed)
                                            break
                                        self._errors[str(route["name"])] = str(error)
                                    except Exception as error:
                                        # One invalid/deleted attachment must not stop all later replies.
                                        self._errors[str(route["name"])] = type(error).__name__
                            else:
                                after = event["id"]
                                self._set_state("events", str(after))
                                continue
                            break
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
