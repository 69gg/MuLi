"""Durable input admission, separate from model replay and transport delivery."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from muli.reply_origin import MAX_ROUTE_CHARS, ReplyOrigin
from muli.store import Store, new_id

MAX_EVENT_ID_CHARS = 512


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
    ) -> tuple[dict[str, Any], bool]:
        payload = self.payload(
            session_id,
            content,
            source=source,
            principal=principal,
            mode=mode,
            attachment_ids=attachment_ids,
            route=route,
            event_id=event_id,
            reply_origin=reply_origin,
        )
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
    ) -> dict[str, Any]:
        """Validate and build input without committing; host transactions may add trusted refs."""
        if not content.strip() or len(content) > 64000:
            raise ValueError("消息需为1–64000字符")
        if not source or len(source) > 120 or not principal or len(principal) > 256:
            raise ValueError("消息来源或身份无效")
        attachments = list(attachment_ids or [])
        if len(attachments) > 32 or any(
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
        return payload
