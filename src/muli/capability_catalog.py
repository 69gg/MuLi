"""A bounded observation of registered tools, without invoking their contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from muli.store import new_id
from muli.tools import Tool
from muli.tools.mcp_catalog import ContentError, json_size

if TYPE_CHECKING:
    from muli.plugins import PluginManager
    from muli.runtime import Runtime

JsonObject = dict[str, Any]


class CapabilityCatalogError(ValueError):
    def __init__(self, status_code: int, code: str) -> None:
        self.status_code, self.code = status_code, code
        super().__init__(code)


def _json_data(value: object) -> None:
    """Reject Python-only values that the JSON encoder would silently coerce."""
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise CapabilityCatalogError(422, "capability_contract_invalid")
            _json_data(item)
    elif type(value) is list:
        for item in value:
            _json_data(item)
    elif value is not None and type(value) not in {str, bool, int, float}:
        raise CapabilityCatalogError(422, "capability_contract_invalid")


def _bounded(value: object, maximum: int) -> int:
    try:
        size = json_size(value, maximum)
        _json_data(value)
    except ContentError as error:
        if error.code == "resource_catalog_budget_exceeded":
            raise CapabilityCatalogError(413, "capability_payload_too_large") from None
        raise CapabilityCatalogError(422, "capability_contract_invalid") from None
    except RecursionError:
        raise CapabilityCatalogError(422, "capability_contract_invalid") from None
    return size


@dataclass(eq=False, slots=True)
class _ObservedTool:
    key: str
    tool: Tool
    source: tuple[str, ...]
    data: JsonObject
    identifier: str

    def public(self) -> JsonObject:
        return {"id": self.identifier, **self.data}


@dataclass(slots=True)
class _Observation:
    revision: str
    disabled: frozenset[str]
    tools: tuple[_ObservedTool, ...]
    payload: JsonObject


class CapabilityCatalog:
    def __init__(self, runtime: Runtime, plugins: PluginManager) -> None:
        self.runtime, self.plugins = runtime, plugins
        self._observation: _Observation | None = None

    @property
    def maximum(self) -> int:
        return self.runtime.config.settings.limits.max_output_chars * 4

    def _origin(self, tool: Tool) -> tuple[tuple[str, ...], JsonObject]:
        mcp = self.runtime.mcp
        for origin in mcp.tool_origins:
            if origin.tool is not tool:
                continue
            if origin.server is None and origin.remote_name is None:
                return ("mcp_interface",), {"kind": "mcp_interface"}
            if not isinstance(origin.server, str) or not isinstance(origin.remote_name, str):
                raise CapabilityCatalogError(422, "capability_contract_invalid")
            return ("mcp", origin.server, origin.remote_name), {
                "kind": "mcp",
                "server": mcp.catalog_redact_text(origin.server),
                "remote_name": mcp.catalog_redact_text(origin.remote_name),
            }
        for context in self.plugins.contexts:
            if any(candidate is tool for candidate in context.tools) and any(
                candidate is tool for candidate in self.plugins._tools.values()
            ):
                return ("plugin", context.identifier), {
                    "kind": "plugin",
                    "plugin_id": mcp.catalog_redact_text(context.identifier),
                }
        return ("unrecorded",), {"kind": "unrecorded"}

    def _metadata(self, tool: Tool, disabled: frozenset[str]) -> tuple[tuple[str, ...], JsonObject]:
        if (
            type(tool.name) is not str
            or not tool.name
            or type(tool.description) is not str
            or type(tool.risk) is not str
            or type(tool.requires_approval) is not bool
        ):
            raise CapabilityCatalogError(422, "capability_contract_invalid")
        redact = self.runtime.mcp.catalog_redact_text
        if redact(tool.name) != tool.name or redact(tool.risk) != tool.risk:
            # Registered names and risk labels must retain their actual meaning.
            raise CapabilityCatalogError(422, "capability_contract_sensitive")
        source, origin = self._origin(tool)
        return source, {
            "name": tool.name,
            "description": redact(tool.description),
            "risk": tool.risk,
            "requires_approval": tool.requires_approval,
            "enabled": tool.name not in disabled,
            "origin": origin,
        }

    def _observe(self) -> _Observation:
        """Capture synchronously; retain one bounded set of DTOs and real Tool refs."""
        revision = new_id()
        disabled = frozenset(self.runtime.config.settings.disabled_tools)
        payload: JsonObject = {
            "scope": "registered_tools",
            "revision": revision,
            "complete": True,
            "items": [],
        }
        tools: list[_ObservedTool] = []
        try:
            charged = _bounded(payload, self.maximum)
            for key, tool in self.runtime.tools.items():
                source, data = self._metadata(tool, disabled)
                entry = _ObservedTool(key, tool, source, data, new_id())
                separator = 2 if tools else 0  # Match the shared encoder's comma and space.
                charged += separator + _bounded(entry.public(), self.maximum - charged - separator)
                tools.append(entry)
            current = self._observation
            if (
                current is not None
                and current.disabled == disabled
                and len(current.tools) == len(tools)
                and all(
                    before.key == after.key
                    and before.tool is after.tool
                    and before.source == after.source
                    and before.data == after.data
                    for before, after in zip(current.tools, tools, strict=True)
                )
            ):
                return current
            payload["items"] = [entry.public() for entry in tools]
            _bounded(payload, self.maximum)
            observation = _Observation(revision, disabled, tuple(tools), payload)
        except CapabilityCatalogError:
            self._observation = None
            raise
        self._observation = observation
        return observation

    def listing(self) -> JsonObject:
        return self._observe().payload

    def _sensitive(self, value: object) -> bool:
        if isinstance(value, str):
            return self.runtime.mcp.catalog_redact_text(value) != value
        if isinstance(value, dict):
            return any(self._sensitive(key) or self._sensitive(item) for key, item in value.items())
        if isinstance(value, list):
            return any(self._sensitive(item) for item in value)
        return False

    def detail(self, identifier: str, revision: str) -> JsonObject:
        observation = self._observe()
        if observation.revision != revision:
            raise CapabilityCatalogError(409, "capability_catalog_stale")
        entry = next((item for item in observation.tools if item.identifier == identifier), None)
        if entry is None:
            raise CapabilityCatalogError(404, "capability_not_found")
        if self.runtime.tools.get(entry.key) is not entry.tool:
            raise CapabilityCatalogError(409, "capability_catalog_stale")
        parameters = entry.tool.parameters
        if type(parameters) is not dict:
            raise CapabilityCatalogError(422, "capability_contract_invalid")
        payload: JsonObject = {
            "scope": "registered_tools",
            "revision": observation.revision,
            "tool": {**entry.public(), "parameters": parameters},
        }
        _bounded(payload, self.maximum)
        if self._sensitive(parameters):
            raise CapabilityCatalogError(422, "capability_contract_sensitive")
        return payload
