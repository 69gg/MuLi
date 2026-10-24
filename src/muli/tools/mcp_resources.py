"""Bounded resource values, without transport, filesystem or lifecycle effects."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import AnyUrl

from muli.tools.mcp_catalog import MAX_CATALOG_ITEMS, Catalog, json_size
from muli.tools.mcp_catalog import ContentError as ResourceError

JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResourceSaveTarget:
    """Host-owned preflight returns the sole save operation, without starting it."""

    max_file_bytes: int
    prepare: Callable[[bytes, str, int], Callable[[], Awaitable[JsonObject]]]


ResourceSaveFactory = Callable[[str], ResourceSaveTarget]

# Host protocol limits; the configured page, catalog, wire and output budgets
# can reject an operation earlier. These do not add a second settings policy.
MAX_RESOURCE_ITEMS = MAX_CATALOG_ITEMS
MAX_RESOURCE_URI_BYTES = 4096
MAX_RESOURCE_CONTENT_BLOCKS = 16
TOOL_RESULT_PREFIX = "tool_result:"


@dataclass(frozen=True, slots=True)
class Capabilities:
    tools: bool
    resources: bool
    prompts: bool = False


class ResourceClient(Protocol):
    def server_capabilities(self) -> Capabilities: ...

    async def list_resources_page(self, cursor: str | None) -> JsonObject: ...

    async def read_resource(self, uri: AnyUrl) -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class ResourceItem:
    uri: AnyUrl
    metadata: JsonObject


ResourceCatalog = Catalog[ResourceItem]


class ToolResultCatalog(Catalog[ResourceItem]):
    """Keep result refs distinguishable after their sole catalog has expired."""

    __slots__ = ()

    def token(self, kind: str, position: int) -> str:
        return TOOL_RESULT_PREFIX + super().token(kind, position)


def resource_uri(value: object) -> AnyUrl:
    if type(value) is not str or len(value) > MAX_RESOURCE_URI_BYTES:
        raise ResourceError("resource_invalid")
    try:
        if len(value.encode("utf-8")) > MAX_RESOURCE_URI_BYTES or any(
            character in value for character in "\x00\r\n"
        ):
            raise ResourceError("resource_invalid")
        uri = AnyUrl(value)
        if len(str(uri).encode("utf-8")) > MAX_RESOURCE_URI_BYTES:
            raise ResourceError("resource_invalid")
    except ValueError:
        raise ResourceError("resource_invalid") from None
    return uri


def resource_item(value: object) -> ResourceItem:
    if type(value) is not dict or type(value.get("name")) is not str:
        raise ResourceError("resource_invalid")
    uri = resource_uri(value.get("uri"))
    metadata: JsonObject = {"uri": str(uri), "name": value["name"]}
    for source, target in (
        ("title", "title"),
        ("description", "description"),
        ("mimeType", "mime_type"),
    ):
        item = value.get(source)
        if item is not None:
            if type(item) is not str:
                raise ResourceError("resource_invalid")
            metadata[target] = item
    size = value.get("size")
    if size is not None:
        if type(size) is not int or size < 0:
            raise ResourceError("resource_invalid")
        metadata["size"] = size
    return ResourceItem(uri, metadata)


def tool_result_resources(content: object, maximum: int) -> tuple[ResourceItem, ...]:
    """Validate one whole original typed-link group before projecting metadata."""
    if type(content) is not list:
        raise ResourceError("resource_invalid")
    blocks: list[Any] = []
    for block in content:
        if getattr(block, "type", None) == "resource_link":
            if len(blocks) >= MAX_RESOURCE_ITEMS:
                raise ResourceError("resource_catalog_budget_exceeded")
            blocks.append(block)
    values: list[JsonObject] = []
    try:
        for block in blocks:
            dump = getattr(block, "model_dump", None)
            value = dump(mode="json", by_alias=True) if callable(dump) else vars(block)
            if type(value) is not dict or value.get("type") != "resource_link":
                raise ResourceError("resource_invalid")
            values.append(value)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ResourceError("resource_invalid") from None
    # Include annotations, extra fields and all links before any whitelist projection.
    json_size(values, maximum)
    items: list[ResourceItem] = []
    seen: set[AnyUrl] = set()
    for value in values:
        item = resource_item(value)
        if item.uri in seen:
            raise ResourceError("resource_duplicate_uri")
        seen.add(item.uri)
        items.append(item)
    return tuple(items)


def resource_content(value: object) -> list[JsonObject]:
    if type(value) is not dict or type(value.get("contents")) is not list:
        raise ResourceError("resource_invalid")
    blocks = value["contents"]
    if len(blocks) > MAX_RESOURCE_CONTENT_BLOCKS:
        raise ResourceError("resource_content_budget_exceeded")
    result: list[JsonObject] = []
    for block in blocks:
        if type(block) is not dict:
            raise ResourceError("resource_invalid")
        if "blob" in block or "text" not in block:
            raise ResourceError("resource_content_unsupported")
        if type(block["text"]) is not str:
            raise ResourceError("resource_invalid")
        uri = resource_uri(block.get("uri"))
        item: JsonObject = {"uri": str(uri), "text": block["text"]}
        mime = block.get("mimeType")
        if mime is not None:
            if type(mime) is not str:
                raise ResourceError("resource_invalid")
            item["mime_type"] = mime
        result.append(item)
    return result


def resource_blob(value: object, maximum: int) -> tuple[bytes, str]:
    """Validate one complete blob before allocating any attachment reservation."""
    if type(value) is not dict or type(value.get("contents")) is not list:
        raise ResourceError("resource_invalid")
    blocks = value["contents"]
    if len(blocks) != 1:
        raise ResourceError("resource_content_unsupported")
    block = blocks[0]
    if type(block) is not dict or "text" in block or "blob" not in block:
        raise ResourceError("resource_content_unsupported")
    resource_uri(block.get("uri"))
    encoded, mime = block["blob"], block.get("mimeType", "")
    if type(encoded) is not str or type(mime) is not str:
        raise ResourceError("resource_invalid")
    if len(encoded) > ((maximum + 2) // 3) * 4:
        raise ResourceError("resource_file_budget_exceeded")
    try:
        data = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, binascii.Error, ValueError):
        raise ResourceError("resource_blob_invalid") from None
    if len(data) > maximum:
        raise ResourceError("resource_file_budget_exceeded")
    return data, mime
