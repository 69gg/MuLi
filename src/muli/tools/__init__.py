"""Explicit, inspectable tool contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

Handler = Callable[[dict[str, Any]], Awaitable[Any]]
Recovery = Literal["replayable", "reconcile", "never"]
FailureSemantics = Literal["unknown", "not_executed"]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    risk: str = "read"
    requires_approval: bool = False
    preserve_output: bool = False
    # How a crash after dispatch may be treated. ``never`` is the safe default
    # for writes, external effects and anything the host cannot prove.
    recovery: Recovery = "never"
    # Only pure read-only handlers may claim that an exception means no effect.
    failure_semantics: FailureSemantics = "unknown"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def parameters(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def string(description: str = "") -> dict[str, str]:
    return {"type": "string", "description": description}
