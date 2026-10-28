"""Small host-owned evidence for the original text prefix of an admitted input."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypedDict

OWNER_TEXT_VERSION = 1
OwnerTextKind = Literal["web", "channel"]


class OwnerText(TypedDict):
    version: int
    kind: OwnerTextKind
    chars: int


def owner_text_prefix(payload: Mapping[str, Any]) -> str | None:
    """Read only a validated original prefix; this evidence grants no authority."""
    marker = payload.get("owner_text")
    if (
        not isinstance(marker, dict)
        or set(marker) != {"version", "kind", "chars"}
        or type(marker.get("version")) is not int
        or marker["version"] != OWNER_TEXT_VERSION
        or type(marker.get("kind")) is not str
        or marker["kind"] not in {"web", "channel"}
    ):
        return None
    content, source, chars = payload.get("content"), payload.get("source"), marker["chars"]
    if not isinstance(content, str) or type(chars) is not int or not 0 < chars <= len(content):
        return None
    if marker["kind"] == "web":
        if source != "web" or payload.get("route") is not None:
            return None
    else:
        route = payload.get("route")
        if not isinstance(route, dict) or any(
            not isinstance(route.get(key), str) or not route[key] for key in ("name", "account_id")
        ):
            return None
        if source != f"channel:{route['name']}:{route['account_id']}":
            return None
    prefix = content[:chars]
    return prefix if prefix.strip() else None


def owner_text_marker(
    payload: Mapping[str, Any], chars: object, *, kind: OwnerTextKind
) -> OwnerText | None:
    """Build the same bounded descriptor accepted by the durable reader."""
    if type(chars) is not int:
        return None
    marker: OwnerText = {"version": OWNER_TEXT_VERSION, "kind": kind, "chars": chars}
    return marker if owner_text_prefix({**payload, "owner_text": marker}) is not None else None
