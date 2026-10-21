"""Parse a bounded, pinned uv receipt into immutable installation intent.

No environment or configuration discovery is performed. Parsed intent still
requires verified resolver context and does not authorize an installation.
"""

from __future__ import annotations

import re
import tomllib
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field

from muli.management_transaction import StrictRecord

UV_RECEIPT_VERSION = "0.13.0"
UV_RECEIPT_COMMIT = "4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17"
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)
_INVALID_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})", re.ASCII)
ReceiptAdapter = Literal["uv-tool-0.13.0", "uv-tool-0.13.1"]
IndexStrategy = Literal["first-index", "unsafe-first-match", "unsafe-best-match"]
_STRATEGIES: tuple[IndexStrategy, ...] = (
    "first-index",
    "unsafe-first-match",
    "unsafe-best-match",
)


class InstallProfileError(ValueError):
    """A fixed reason without supplied requirement, URL or TOML contents."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class IntentLimits(StrictRecord):
    max_receipt_bytes: int = Field(default=262_144, ge=128, le=1_048_576)
    max_requirements: int = Field(default=256, ge=1, le=512)
    max_indexes: int = Field(default=8, ge=1, le=32)
    max_entrypoints: int = Field(default=32, ge=1, le=128)
    max_extras: int = Field(default=32, ge=1, le=128)
    max_string_bytes: int = Field(default=2048, ge=32, le=8192)
    max_nodes: int = Field(default=4096, ge=16, le=16_384)
    max_depth: int = Field(default=16, ge=2, le=32)


class RegistryRequirement(StrictRecord):
    name: str
    extras: tuple[str, ...] = ()
    specifier: str | None = Field(default=None, repr=False)
    marker: str | None = Field(default=None, repr=False)
    index: str | None = Field(default=None, repr=False)


class EntrypointEvidence(StrictRecord):
    name: str
    install_path: str = Field(repr=False)
    from_requirement: str | None = Field(default=None, repr=False)


class ReceiptOptions(StrictRecord):
    # None preserves omission; False and an explicit empty array stay distinct.
    index_url: str | None = Field(default=None, repr=False)
    extra_index_urls: tuple[str, ...] | None = Field(default=None, repr=False)
    no_index: bool | None = None
    index_strategy: IndexStrategy | None = None
    find_links: tuple[str, ...] | None = Field(default=None, repr=False)
    no_build: bool | None = None


class InstallIntent(StrictRecord):
    main: RegistryRequirement
    with_requirements: tuple[RegistryRequirement, ...]
    constraints: tuple[RegistryRequirement, ...]
    overrides: tuple[RegistryRequirement, ...]
    python: str | None = Field(repr=False)
    entrypoints: tuple[EntrypointEvidence, ...]
    options: ReceiptOptions | None


class ParsedInstallIntent(StrictRecord):
    status: Literal["intent_parsed"] = "intent_parsed"
    context_status: Literal["context_required"] = "context_required"
    adapter: ReceiptAdapter = "uv-tool-0.13.0"
    intent: InstallIntent


def receipt_adapter(uv_version: str) -> ReceiptAdapter:
    """Select only explicitly reviewed formats, without observing a uv binary."""
    if type(uv_version) is str:
        if uv_version == "0.13.0":
            return "uv-tool-0.13.0"
        if uv_version == "0.13.1":
            return "uv-tool-0.13.1"
    raise InstallProfileError("unsupported_uv_version")


def _table(value: object, allowed: set[str], code: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InstallProfileError("invalid_receipt")
    if any(type(key) is not str or key not in allowed for key in value):
        raise InstallProfileError(code)
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise InstallProfileError("invalid_receipt")
    return value


def _text(value: object, *, empty: bool = False) -> str:
    if type(value) is not str or (not value and not empty):
        raise InstallProfileError("invalid_receipt")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InstallProfileError("invalid_receipt")
    return value


def _name(value: object) -> str:
    name = _text(value)
    # uv's PackageName/ExtraName serializer emits normalized names. This is not
    # a PEP requirement parser and deliberately does not normalize user input.
    if _NAME.fullmatch(name) is None:
        raise InstallProfileError("unsupported_requirement_name")
    return name


def _https(value: object) -> str:
    url = _text(value)
    if any(character.isspace() for character in url) or "\\" in url:
        raise InstallProfileError("unsupported_index_url")
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or "?" in url
            or "#" in url
        ):
            raise ValueError
        # Access validates the port too; urlsplit alone is not a URL validator.
        _ = parts.port
    except ValueError:
        raise InstallProfileError("unsupported_index_url") from None
    return url


def _local_links(value: object) -> str:
    """Keep a local URI as evidence; never discover or authorize its contents."""
    url = _text(value)
    try:
        parts = urlsplit(url)
        path = unquote(parts.path, encoding="utf-8", errors="strict")
        if (
            not url.startswith("file:///")
            or parts.netloc
            or "?" in url
            or "#" in url
            or _INVALID_ESCAPE.search(url)
            or any(character.isspace() for character in url)
            or not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            raise ValueError
    except ValueError:
        raise InstallProfileError("unsupported_find_links") from None
    return url


def _bounded_tree(value: object, limits: IntentLimits) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if depth > limits.max_depth or nodes > limits.max_nodes:
            raise InstallProfileError("receipt_budget")
        if isinstance(current, dict):
            children: list[object] = []
            for key, item in current.items():
                children.extend((key, item))
        elif isinstance(current, list):
            children = current
        elif isinstance(current, str):
            if len(current.encode("utf-8")) > limits.max_string_bytes:
                raise InstallProfileError("receipt_budget")
            continue
        else:
            # TOML scalar types are checked at their known field below. Keep
            # unknown fields unsupported even when their value is numeric/date.
            continue
        if nodes + len(pending) + len(children) > limits.max_nodes:
            raise InstallProfileError("receipt_budget")
        pending.extend((child, depth + 1) for child in children)


def _requirement(value: object, limits: IntentLimits) -> RegistryRequirement:
    if isinstance(value, str):
        raise InstallProfileError("unsupported_requirement_format")
    if isinstance(value, dict) and any(
        key in value for key in ("url", "git", "path", "directory", "editable", "virtual")
    ):
        raise InstallProfileError("unsupported_requirement_source")
    table = _table(
        value, {"name", "extras", "specifier", "marker", "index"}, "unsupported_requirement_field"
    )
    extras = _array(table.get("extras", []))
    if len(extras) > limits.max_extras:
        raise InstallProfileError("receipt_budget")
    return RegistryRequirement(
        name=_name(table.get("name")),
        extras=tuple(_name(extra) for extra in extras),
        specifier=_text(table["specifier"], empty=True) if "specifier" in table else None,
        marker=_text(table["marker"], empty=True) if "marker" in table else None,
        index=_https(table["index"]) if "index" in table else None,
    )


def _options(value: object, limits: IntentLimits) -> ReceiptOptions:
    table = _table(
        value,
        {"index-url", "extra-index-url", "no-index", "index-strategy", "find-links", "no-build"},
        "unsupported_option",
    )
    extra = _array(table["extra-index-url"]) if "extra-index-url" in table else None
    links = _array(table["find-links"]) if "find-links" in table else None
    if (len(extra) if extra is not None else 0) + (len(links) if links is not None else 0) + int(
        "index-url" in table
    ) > limits.max_indexes:
        raise InstallProfileError("receipt_budget")
    booleans: dict[str, bool | None] = {"no-index": None, "no-build": None}
    for name in booleans:
        if name in table:
            value = table[name]
            if type(value) is not bool:
                raise InstallProfileError("invalid_receipt")
            booleans[name] = value
    strategy: IndexStrategy | None = None
    if "index-strategy" in table:
        value = table["index-strategy"]
        for supported in _STRATEGIES:
            if type(value) is str and value == supported:
                strategy = supported
                break
        else:
            raise InstallProfileError("unsupported_index_strategy")
    return ReceiptOptions(
        index_url=_https(table["index-url"]) if "index-url" in table else None,
        extra_index_urls=tuple(_https(item) for item in extra) if extra is not None else None,
        no_index=booleans["no-index"],
        index_strategy=strategy,
        find_links=tuple(_local_links(item) for item in links) if links is not None else None,
        no_build=booleans["no-build"],
    )


def _entrypoint(value: object) -> EntrypointEvidence:
    table = _table(value, {"name", "install-path", "from"}, "unsupported_entrypoint_field")
    return EntrypointEvidence(
        name=_text(table.get("name")),
        install_path=_text(table.get("install-path")),
        from_requirement=_text(table["from"]) if "from" in table else None,
    )


def parse_install_intent(
    data: bytes, *, uv_version: str, limits: IntentLimits | None = None
) -> ParsedInstallIntent:
    """Parse supplied bytes; uv_version is a caller assertion, not attestation.

    This format adapter never resolves PEP specifiers/markers, applies defaults
    from outside the receipt, discovers credentials or selects a target release.
    """
    adapter = receipt_adapter(uv_version)
    if type(data) is not bytes:
        raise InstallProfileError("invalid_receipt")
    budget = limits if limits is not None else IntentLimits()
    if len(data) > budget.max_receipt_bytes:
        raise InstallProfileError("receipt_budget")
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except ValueError:
        raise InstallProfileError("invalid_receipt") from None
    except RecursionError:
        raise InstallProfileError("receipt_budget") from None
    _bounded_tree(document, budget)
    root = _table(document, {"tool"}, "unsupported_receipt_field")
    tool = _table(
        root.get("tool"),
        {"requirements", "constraints", "overrides", "python", "entrypoints", "options"},
        "unsupported_tool_field",
    )
    requirements = _array(tool.get("requirements", []))
    constraints = _array(tool.get("constraints", []))
    overrides = _array(tool.get("overrides", []))
    if len(requirements) + len(constraints) + len(overrides) > budget.max_requirements:
        raise InstallProfileError("receipt_budget")
    parsed = tuple(_requirement(item, budget) for item in requirements)
    if not parsed or parsed[0].name != "muli-agent":
        raise InstallProfileError("unsupported_tool")
    if any(item.name == "muli-agent" for item in parsed[1:]):
        raise InstallProfileError("ambiguous_tool_requirement")
    entrypoints = _array(tool.get("entrypoints"))
    if len(entrypoints) > budget.max_entrypoints:
        raise InstallProfileError("receipt_budget")
    return ParsedInstallIntent(
        adapter=adapter,
        intent=InstallIntent(
            main=parsed[0],
            with_requirements=parsed[1:],
            constraints=tuple(_requirement(item, budget) for item in constraints),
            overrides=tuple(_requirement(item, budget) for item in overrides),
            python=_text(tool["python"]) if "python" in tool else None,
            entrypoints=tuple(_entrypoint(item) for item in entrypoints),
            options=_options(tool["options"], budget) if "options" in tool else None,
        ),
    )
