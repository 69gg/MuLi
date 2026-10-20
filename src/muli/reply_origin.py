"""Host-owned reply destinations, independent of identity and execution authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

MAX_ROUTE_CHARS = 4096
MAX_ORIGIN_ID_CHARS = 512
_ROUTE_FIELDS = ("name", "account_id", "target", "sender_id", "kind", "reply_to")
_ROUTE_FIELD_CHARS = 200


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
