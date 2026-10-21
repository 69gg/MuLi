"""Bounded resource values, without transport, filesystem or lifecycle effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import AnyUrl

from muli.tools.mcp_catalog import MAX_CATALOG_ITEMS, Catalog
from muli.tools.mcp_catalog import ContentError as ResourceError

JsonObject = dict[str, Any]

# Host protocol limits; the configured page, catalog, wire and output budgets
# can reject an operation earlier. These do not add a second settings policy.
MAX_RESOURCE_ITEMS = MAX_CATALOG_ITEMS
MAX_RESOURCE_URI_BYTES = 4096
MAX_RESOURCE_CONTENT_BLOCKS = 16


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
