"""Durable input admission, separate from model replay and transport delivery."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from muli.attachments import MAX_ATTACHMENT_IDS
from muli.memory_source_failure import MAX_FAILURE_PROOF_BYTES, MemorySourceFailure
from muli.memory_watches import MAX_MATCH_PROOF_BYTES, MemoryWatchMatch, match_routing
from muli.proactive import FrozenRouting
from muli.project_inbox import WEB_PROJECT_ROUTING, ProjectInboxBatch
from muli.reply_origin import MAX_ROUTE_CHARS, ReplyOrigin
from muli.store import Store, new_id

MAX_EVENT_ID_CHARS = 512
MAX_CONTENT_CHARS = 64000


class Conversation:
    def __init__(self, store: Store) -> None:
        self.store = store

    def pending(
        self, session_id: str, *, after: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """UI projection only; pending input is never part of model tool replay."""
        rows = self.store.db.execute(
            "SELECT seq,payload FROM ingress WHERE session_id=? AND state='pending' "
            "AND seq>? ORDER BY seq LIMIT ?",
            (session_id, after, max(0, limit)),
        ).fetchall()
        return [
            {
                **json.loads(row["payload"]),
                "seq": row["seq"],
                "role": "user",
                "state": "pending",
                "delivery_status": "pending",
            }
            for row in rows
        ]

    def enqueue(
        self,
        session_id: str,
        content: str,
        *,
        source: str,
        principal: str,
        mode: str,
        max_pending: int,
        attachment_ids: list[str] | None = None,
        route: dict[str, Any] | None = None,
        event_id: str | None = None,
        internal: bool = False,
        objects: list[tuple[str, dict[str, Any]]] | None = None,
        reply_origin: ReplyOrigin | None = None,
        attention_routing: FrozenRouting | None = None,
        attention_memory_failure: MemorySourceFailure | None = None,
        attention_memory_watch: MemoryWatchMatch | None = None,
        attention_project: ProjectInboxBatch | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if attention_project is not None and not internal:
            raise ValueError("项目资料复核仅允许受信宿主入口")
        watch_routing: FrozenRouting | None = None
        if attention_memory_watch is not None:
            watch_routing = match_routing(self.store, attention_memory_watch)
            if (
                watch_routing is None
                or (attention_routing or FrozenRouting()) != watch_routing
                or (
                    reply_origin is not None
                    and (
                        reply_origin != watch_routing.origin
                        if watch_routing.classification == "bound"
                        else bool(reply_origin.delivery_routes())
                    )
                )
            ):
                raise ValueError("长期问题回访需要当前宿主证明及准确原来源偏好")
        payload = self.payload(
            session_id,
            content,
            source=source,
            principal=principal,
            mode=mode,
            attachment_ids=attachment_ids,
            route=route,
            event_id=event_id,
            reply_origin=None if attention_memory_watch is not None else reply_origin,
            attention_routing=None if attention_memory_watch is not None else attention_routing,
            attention_memory_failure=attention_memory_failure,
            attention_memory_watch=attention_memory_watch,
            attention_project=attention_project,
        )
        if watch_routing is not None:
            # Only this Store-bound admission may replace the static local
            # builder's routing with the already checked canonical preference.
            payload["attention_routing"] = watch_routing.dump()
            payload["reply_origin"] = watch_routing.reply_origin(payload["id"]).dump()
        return self.store.enqueue_input(
            payload,
            max_pending=max_pending,
            quarantine=principal != "owner" and not internal,
            objects=objects,
        )

    @staticmethod
    def payload(
        session_id: str,
        content: str,
        *,
        source: str,
        principal: str,
        mode: str,
        attachment_ids: list[str] | None = None,
        route: dict[str, Any] | None = None,
        event_id: str | None = None,
        reply_origin: ReplyOrigin | None = None,
        attention_routing: FrozenRouting | None = None,
        attention_memory_failure: MemorySourceFailure | None = None,
        attention_memory_watch: MemoryWatchMatch | None = None,
        attention_project: ProjectInboxBatch | None = None,
    ) -> dict[str, Any]:
        """Validate and build input without committing; host transactions may add trusted refs."""
        if not content.strip() or len(content) > MAX_CONTENT_CHARS:
            raise ValueError("消息需为1–64000字符")
        if not source or len(source) > 120 or not principal or len(principal) > 256:
            raise ValueError("消息来源或身份无效")
        attachments = list(attachment_ids or [])
        if len(attachments) > MAX_ATTACHMENT_IDS or any(
            not isinstance(item, str) or not item or len(item) > 256 for item in attachments
        ):
            raise ValueError("附件引用无效或数量过多")
        if route is not None:
            if (
                not isinstance(route, dict)
                or len(json.dumps(route, ensure_ascii=False, allow_nan=False)) > MAX_ROUTE_CHARS
            ):
                raise ValueError("消息路由必须是有限的 JSON 对象")
        if event_id is not None and (not event_id or len(event_id) > MAX_EVENT_ID_CHARS):
            raise ValueError("来源消息 ID 无效")
        identifier = (
            hashlib.sha256(f"{session_id}\0{source}\0{event_id}".encode()).hexdigest()
            if event_id
            else new_id()
        )
        payload = {
            "id": identifier,
            "session_id": session_id,
            "content": content,
            "source": source,
            "principal": principal,
            "mode": mode,
            "attachment_ids": attachments,
            "route": route,
        }
        if reply_origin is not None:
            payload["reply_origin"] = reply_origin.dump()
        if attention_project is not None:
            if (
                source != "system:attention"
                or principal != "system:attention"
                or route is not None
                or attention_memory_failure is not None
                or attention_memory_watch is not None
                or attention_routing != WEB_PROJECT_ROUTING
                or (reply_origin is not None and reply_origin != WEB_PROJECT_ROUTING.origin)
                or not isinstance(attention_project, ProjectInboxBatch)
                or ProjectInboxBatch.load(attention_project.dump()) != attention_project
            ):
                raise ValueError("项目资料复核需要准确宿主证明与 Web 本地来源")
            payload["attention_project"] = attention_project.dump()
        if attention_routing is not None:
            if principal != "system:attention" or source != "system:attention":
                raise ValueError("主动通知选路仅允许系统注意事项入口")
            payload["attention_routing"] = attention_routing.dump()
            payload["reply_origin"] = attention_routing.reply_origin(identifier).dump()
        if attention_memory_failure is not None:
            if (
                principal != "system:attention"
                or source != "system:attention"
                or MemorySourceFailure.load(
                    attention_memory_failure.dump(), max_bytes=MAX_FAILURE_PROOF_BYTES
                )
                != attention_memory_failure
            ):
                raise ValueError("记忆整理提醒仅允许有界宿主证明")
            payload["attention_memory_failure"] = attention_memory_failure.dump()
        if attention_memory_watch is not None:
            if (
                principal != "system:attention"
                or source != "system:attention"
                or attention_memory_failure is not None
                or attention_routing not in (None, FrozenRouting())
                or (reply_origin is not None and bool(reply_origin.delivery_routes()))
                or MemoryWatchMatch.load(
                    attention_memory_watch.dump(), max_bytes=MAX_MATCH_PROOF_BYTES
                )
                != attention_memory_watch
            ):
                raise ValueError("长期问题回访仅允许本机有界宿主证明")
            payload["attention_memory_watch"] = attention_memory_watch.dump()
            payload["attention_routing"] = FrozenRouting().dump()
            payload["reply_origin"] = FrozenRouting().reply_origin(identifier).dump()
        return payload
