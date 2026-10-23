"""Prompt parameters and text drafts are values, never conversation messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

from muli.tools.mcp_catalog import Catalog, ContentError, json_size

JsonObject = dict[str, Any]

# Finite protocol boundaries, in addition to configured catalog/wire/output
# budgets. No independent settings or lifecycle is needed for prompt drafts.
MAX_PROMPT_ARGUMENTS = 32
MAX_PROMPT_ARGUMENT_BYTES = 16384
MAX_PROMPT_MESSAGES = 16


class PromptClient(Protocol):
    async def list_prompts_page(self, cursor: str | None) -> JsonObject: ...

    async def get_prompt(self, name: str, arguments: dict[str, str] | None) -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class PromptItem:
    name: str
    metadata: JsonObject


PromptCatalog = Catalog[PromptItem]


def _optional_text(source: JsonObject, target: JsonObject, key: str) -> None:
    value = source.get(key)
    if value is not None:
        if type(value) is not str:
            raise ContentError("prompt_invalid")
        target[key] = value


def prompt_item(value: object) -> PromptItem:
    if type(value) is not dict or type(value.get("name")) is not str:
        raise ContentError("prompt_invalid")
    metadata: JsonObject = {"name": value["name"]}
    for key in ("title", "description"):
        _optional_text(value, metadata, key)
    arguments = value.get("arguments")
    if arguments is None:
        arguments = []
    if type(arguments) is not list:
        raise ContentError("prompt_invalid")
    if len(arguments) > MAX_PROMPT_ARGUMENTS:
        raise ContentError("prompt_catalog_budget_exceeded")
    parameters: list[JsonObject] = []
    names: set[str] = set()
    for argument in arguments:
        if (
            type(argument) is not dict
            or type(argument.get("name")) is not str
            or argument["name"] in names
        ):
            raise ContentError("prompt_invalid")
        names.add(argument["name"])
        parameter: JsonObject = {"name": argument["name"]}
        _optional_text(argument, parameter, "description")
        required = argument.get("required")
        if required is not None:
            if type(required) is not bool:
                raise ContentError("prompt_invalid")
            parameter["required"] = required
        parameters.append(parameter)
    metadata["arguments"] = parameters
    return PromptItem(value["name"], metadata)


def prompt_arguments(item: PromptItem, request: JsonObject) -> dict[str, str] | None:
    arguments = request.get("arguments", {})
    if type(arguments) is not dict or len(arguments) > MAX_PROMPT_ARGUMENTS:
        raise ContentError("prompt_arguments_invalid")
    declared = {parameter["name"]: parameter for parameter in item.metadata["arguments"]}
    if any(
        type(key) is not str or type(value) is not str or key not in declared
        for key, value in arguments.items()
    ) or any(
        parameter.get("required") is True and name not in arguments
        for name, parameter in declared.items()
    ):
        raise ContentError("prompt_arguments_invalid")
    if any(
        len(key) > MAX_PROMPT_ARGUMENT_BYTES or len(value) > MAX_PROMPT_ARGUMENT_BYTES
        for key, value in arguments.items()
    ):
        raise ContentError("prompt_arguments_budget_exceeded")
    try:
        json_size(arguments, MAX_PROMPT_ARGUMENT_BYTES, kind="prompt")
    except ContentError as error:
        code = (
            "prompt_arguments_budget_exceeded"
            if error.code == "prompt_catalog_budget_exceeded"
            else "prompt_arguments_invalid"
        )
        raise ContentError(code) from None
    return cast(dict[str, str], arguments) if "arguments" in request else None


def prompt_content(value: object) -> JsonObject:
    if type(value) is not dict or type(value.get("messages")) is not list:
        raise ContentError("prompt_invalid")
    messages = value["messages"]
    if len(messages) > MAX_PROMPT_MESSAGES:
        raise ContentError("prompt_content_budget_exceeded")
    result: JsonObject = {}
    _optional_text(value, result, "description")
    projected: list[JsonObject] = []
    for message in messages:
        if (
            type(message) is not dict
            or type(message.get("role")) is not str
            or message["role"] not in {"user", "assistant"}
            or type(message.get("content")) is not dict
        ):
            raise ContentError("prompt_invalid")
        content = message["content"]
        if content.get("type") != "text":
            raise ContentError("prompt_content_unsupported")
        if type(content.get("text")) is not str:
            raise ContentError("prompt_invalid")
        projected.append({"role": message["role"], "text": content["text"]})
    result["messages"] = projected
    return result
