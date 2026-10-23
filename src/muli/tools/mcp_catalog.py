"""Finite values shared by the resource and prompt directories."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

JsonObject = dict[str, Any]
CatalogKind = Literal["resource", "prompt"]
MAX_CATALOG_ITEMS = 256


class ContentError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class Catalog[T]:
    identifier: str
    items: tuple[T, ...]
    references: dict[str, int] = field(default_factory=dict)
    cursors: dict[str, int] = field(default_factory=dict)

    def token(self, kind: str, position: int) -> str:
        return hashlib.sha256(f"{kind}:{self.identifier}:{position}".encode()).hexdigest()[:32]


def json_size(value: object, maximum: int, *, kind: CatalogKind = "resource") -> int:
    """Charge the whole logical page, including fields that will not be shown."""
    size = 0
    try:
        for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
            size += len(chunk.encode("utf-8"))
            if size > maximum:
                raise ContentError(f"{kind}_catalog_budget_exceeded")
    except (TypeError, ValueError, RecursionError, UnicodeError) as error:
        if isinstance(error, ContentError):
            raise
        raise ContentError(f"{kind}_invalid") from None
    return size


def fits_output(value: object, maximum: int) -> bool:
    size = 0
    for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
        size += len(chunk)
        if size > maximum:
            return False
    return True


def catalog_page(value: object, remaining: int, kind: CatalogKind) -> tuple[list[Any], str | None]:
    field_name = "resources" if kind == "resource" else "prompts"
    if type(value) is not dict or type(value.get(field_name)) is not list:
        raise ContentError(f"{kind}_invalid")
    items = value[field_name]
    if len(items) > remaining:
        raise ContentError(f"{kind}_catalog_budget_exceeded")
    cursor = value.get("nextCursor")
    if cursor is not None and type(cursor) is not str:
        raise ContentError(f"{kind}_invalid")
    return items, cursor
