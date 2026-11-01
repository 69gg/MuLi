"""Host-owned reply destinations, independent of identity and execution authority."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

MAX_ROUTE_CHARS = 4096
MAX_ORIGIN_ID_CHARS = 512
PRIVATE_OWNER_CHANNELS: tuple[str, ...] = ("onebot", "telegram", "qq")
OWNER_CHANNELS: tuple[str, ...] = (*PRIVATE_OWNER_CHANNELS, "weixin")
_ROUTE_FIELDS = ("name", "account_id", "target", "sender_id", "kind", "reply_to")
_ROUTE_FIELD_CHARS = 200


class ChannelBinding(Protocol):
    @property
    def account_id(self) -> str: ...

    @property
    def app_id(self) -> str: ...

    @property
    def user_id(self) -> str: ...

    def secret(self, name: str) -> str: ...


class RouteError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def channel_account(name: str, profile: ChannelBinding) -> str:
    if profile.account_id or profile.app_id or profile.user_id:
        return profile.account_id or profile.app_id or profile.user_id
    if name == "telegram":
        prefix = profile.secret("token").partition(":")[0]
        if prefix.isdigit():
            return prefix
    return "bot"


def validate_onebot_owners(owner_ids: Collection[str], *, enabled: bool) -> None:
    if enabled and not owner_ids:
        raise ValueError("启用 OneBot 必须填写主人 QQ 号（owner_ids）")
    if any(
        not isinstance(value, str)
        or len(value) > _ROUTE_FIELD_CHARS
        or not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
        for value in owner_ids
    ):
        raise ValueError("OneBot 主人 QQ 号（owner_ids）必须是无空格的正整数")


def validate_channel_owners(name: str, owner_ids: Collection[str], *, enabled: bool) -> None:
    if name == "onebot":
        validate_onebot_owners(owner_ids, enabled=enabled)
    elif name in OWNER_CHANNELS:
        if enabled and not owner_ids:
            raise ValueError(f"启用 {name} 必须填写已绑定主人 ID（owner_ids）")
        if any(
            not isinstance(value, str) or not value or len(value) > _ROUTE_FIELD_CHARS
            for value in owner_ids
        ):
            raise ValueError("渠道 owner_ids 需要有界的非空用户 ID")


def authorize_route(
    name: str,
    route: Mapping[str, Any],
    *,
    account_id: str,
    owner_ids: Collection[str],
    allowed_groups: Collection[str],
    outbound: bool,
) -> dict[str, Any]:
    """Shared binding checks for a complete candidate config and the live transport."""
    sender, target, kind = route.get("sender_id"), route.get("target"), route.get("kind", "private")
    if (
        any(
            not isinstance(value, str) or not value or len(value) > _ROUTE_FIELD_CHARS
            for value in (sender, target)
        )
        or not isinstance(kind, str)
        or kind not in {"private", "group"}
    ):
        raise RouteError("渠道路由需要真实 sender_id/target/kind")
    if route.get("name", name) != name or route.get("account_id", account_id) != account_id:
        raise RouteError("渠道路由账号不匹配")
    if name in PRIVATE_OWNER_CHANNELS and (kind != "private" or sender not in owner_ids):
        label = "OneBot" if name == "onebot" else name
        raise RouteError(f"{label} 仅接受 owner_ids 中已绑定主人的点对点私聊", status_code=403)
    if kind == "group" and target not in allowed_groups:
        raise RouteError("此群未在 allowed_groups 中绑定", status_code=403)
    if kind == "private" and target != sender:
        raise RouteError("私聊目标与真实 sender_id 不一致", status_code=403)
    if outbound and sender not in owner_ids:
        raise RouteError("渠道收件人未绑定 owner_ids", status_code=403)
    result = {
        "name": name,
        "account_id": account_id,
        "target": target,
        "sender_id": sender,
        "kind": kind,
    }
    reply = route.get("reply_to")
    if reply:
        if not isinstance(reply, str) or len(reply) > _ROUTE_FIELD_CHARS:
            raise RouteError("渠道 reply_to 无效")
        result["reply_to"] = reply
    return result


def _route_json(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    route = {key: value[key] for key in _ROUTE_FIELDS if key in value}
    if any(
        not isinstance(item, str) or not item or len(item) > _ROUTE_FIELD_CHARS
        for item in route.values()
    ) or any(key not in route for key in ("name", "account_id", "target", "sender_id")):
        return ""
    if route.get("kind", "private") not in {"private", "group"}:
        return ""
    serialized = json.dumps(route, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return serialized if len(serialized) <= MAX_ROUTE_CHARS else ""


@dataclass(frozen=True, slots=True)
class ReplyOrigin:
    kind: Literal["channel", "web", "unattributed"] = "unattributed"
    input_id: str | None = None
    route_json: str = ""

    @classmethod
    def load(cls, value: object) -> ReplyOrigin:
        """Invalid or absent historical metadata cannot select an external recipient."""
        if not isinstance(value, dict):
            return cls()
        kind, identifier = value.get("kind"), value.get("input_id")
        if (
            not isinstance(kind, str)
            or kind not in {"channel", "web", "unattributed"}
            or (
                identifier is not None
                and (
                    not isinstance(identifier, str)
                    or not identifier
                    or len(identifier) > MAX_ORIGIN_ID_CHARS
                )
            )
        ):
            return cls()
        if kind == "channel":
            route = _route_json(value.get("route"))
            return cls("channel", identifier, route) if route and identifier else cls()
        return cls("web" if kind == "web" else "unattributed", identifier)

    @classmethod
    def from_input(cls, payload: dict[str, Any]) -> ReplyOrigin:
        stored = payload.get("reply_origin")
        if stored is not None:
            return cls.load(stored)
        if payload.get("principal") != "owner":
            return cls()
        identifier, source = payload.get("id"), payload.get("source", "")
        if not isinstance(identifier, str) or not isinstance(source, str):
            return cls()
        if source.startswith("channel:"):
            route = payload.get("route")
            parts = source.split(":", 2)
            if (
                len(parts) == 3
                and parts[2]
                and isinstance(route, dict)
                and route.get("name") == parts[1]
                and route.get("account_id", parts[2]) == parts[2]
            ):
                return cls.load(
                    {
                        "kind": "channel",
                        "input_id": identifier,
                        "route": {**route, "account_id": parts[2]},
                    }
                )
            return cls()
        return cls.load(
            {"kind": "web" if source == "web" else "unattributed", "input_id": identifier}
        )

    @property
    def destination_key(self) -> tuple[str, str]:
        """Same complete route may share a request; input IDs retain their provenance."""
        return self.kind, self.route_json

    def dump(self) -> dict[str, Any]:
        value: dict[str, Any] = {"kind": self.kind, "input_id": self.input_id}
        if self.route_json:
            value["route"] = json.loads(self.route_json)
        return value

    def delivery_routes(self) -> list[dict[str, Any]]:
        # Decode a fresh dictionary: no caller can mutate the frozen snapshot.
        return [json.loads(self.route_json)] if self.kind == "channel" and self.route_json else []
