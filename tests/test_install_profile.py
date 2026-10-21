from __future__ import annotations

import builtins
import json
import os
import socket
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from muli.install_profile import (
    UV_RECEIPT_COMMIT,
    UV_RECEIPT_VERSION,
    InstallProfileError,
    IntentLimits,
    ParsedInstallIntent,
    parse_install_intent,
)

# Synthetic data in the pinned upstream Tool/Requirement/ToolOptions wire shape;
# no receipt, credential or installation file is read from the machine.
PINNED_RECEIPT = b"""[tool]
requirements = [
  { name = "muli-agent", extras = ["mcp", "channels"], specifier = ">=2.18,<3" },
  { name = "httpx", specifier = ">=0.28,<0.29", marker = "python_version >= '3.12'" },
  { name = "idna", specifier = "==3.10" },
]
constraints = [{ name = "httpx", specifier = "<0.29" }]
overrides = [{ name = "idna", specifier = "==3.10" }]
python = "3.12"
entrypoints = [
  { name = "muli", install-path = "/fixture/bin/muli", from = "muli-agent" },
  { name = "MuLi", install-path = "/fixture/bin/MuLi" },
]

[tool.options]
index-url = "https://registry.example.invalid/simple"
extra-index-url = ["https://z.example.invalid/simple", "https://a.example.invalid/simple"]
no-index = false
index-strategy = "first-index"
"""

# Transcribed from the isolated uv 0.13.1 tiny-wheel experiment. Tool/entrypoint
# names and private paths are substituted; these are not original receipt bytes
# or evidence that a MuLi installation was performed.
OBSERVED_UV_0131_RECEIPT = b"""[tool]
requirements = [{name = "muli-agent", specifier = "==0.0.1"}]
python = "/fixture/source/bin/python"
entrypoints = [{name = "muli", install-path = "/fixture/private-bin/muli", from = "muli-agent"}]

[tool.options]
no-index = true
find-links = ["file:///fixture/wheelhouse"]
no-build = true
"""


def tool_data() -> dict[str, Any]:
    return {
        "requirements": [{"name": "muli-agent", "extras": ["mcp", "channels"]}],
        "entrypoints": [{"name": "muli", "install-path": "/fixture/bin/muli"}],
    }


def toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(f"{json.dumps(key)} = {toml_value(item)}" for key, item in value.items())
            + "}"
        )
    raise AssertionError("unsupported synthetic fixture value")


def receipt(tool: dict[str, Any]) -> bytes:
    return ("tool = " + toml_value(tool)).encode("utf-8")


def parse(data: bytes, limits: IntentLimits | None = None) -> ParsedInstallIntent:
    return parse_install_intent(data, uv_version=UV_RECEIPT_VERSION, limits=limits)


def test_pinned_wire_preserves_requirements_options_and_entrypoint_evidence() -> None:
    result = parse(PINNED_RECEIPT)
    assert result.status == "intent_parsed"
    assert result.context_status == "context_required"
    assert result.adapter == "uv-tool-0.13.0"
    assert UV_RECEIPT_COMMIT == "4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17"
    intent = result.intent
    assert intent.main.name == "muli-agent"
    assert intent.main.extras == ("mcp", "channels")
    assert intent.main.specifier == ">=2.18,<3"
    assert [item.name for item in intent.with_requirements] == ["httpx", "idna"]
    assert intent.with_requirements[0].specifier == ">=0.28,<0.29"
    assert intent.with_requirements[0].marker == "python_version >= '3.12'"
    assert intent.constraints[0].name == "httpx" and intent.constraints[0].specifier == "<0.29"
    assert intent.overrides[0].name == "idna" and intent.overrides[0].specifier == "==3.10"
    assert intent.python == "3.12"
    assert [entry.name for entry in intent.entrypoints] == ["muli", "MuLi"]
    assert intent.entrypoints[0].install_path == "/fixture/bin/muli"
    assert intent.entrypoints[0].from_requirement == "muli-agent"
    assert intent.entrypoints[1].from_requirement is None
    assert intent.options is not None
    assert intent.options.index_url == "https://registry.example.invalid/simple"
    assert intent.options.extra_index_urls == (
        "https://z.example.invalid/simple",
        "https://a.example.invalid/simple",
    )
    assert intent.options.no_index is False
    assert intent.options.index_strategy == "first-index"


@pytest.mark.parametrize("version", ["0.13.0", "0.13.1"])
def test_observed_0131_offline_wire_has_only_parsed_intent(version: str) -> None:
    result = parse_install_intent(OBSERVED_UV_0131_RECEIPT, uv_version=version)
    assert result.adapter == f"uv-tool-{version}"
    assert result.context_status == "context_required"
    assert result.intent.main.specifier == "==0.0.1"
    assert result.intent.python == "/fixture/source/bin/python"
    options = result.intent.options
    assert options is not None
    assert options.no_index is True and options.no_build is True
    assert options.find_links == ("file:///fixture/wheelhouse",)
    assert options.index_url is options.extra_index_urls is options.index_strategy is None
    assert "offline" not in options.model_dump() and "profile_complete" not in repr(result)


def test_local_links_preserve_order_and_explicit_empty_or_false() -> None:
    data = tool_data()
    data["options"] = {"find-links": [], "no-build": False}
    options = parse(receipt(data)).intent.options
    assert options is not None
    assert options.find_links == () and options.no_build is False
    data["options"] = {}
    options = parse(receipt(data)).intent.options
    assert options is not None and options.find_links is None and options.no_build is None
    links = ["file:///fixture/z", "file:///fixture/a%20b", "file:///fixture/z"]
    data["options"] = {"find-links": links}
    options = parse(receipt(data)).intent.options
    assert options is not None and options.find_links == tuple(links)


def test_constraints_overrides_and_extras_are_never_sorted_or_deduplicated() -> None:
    data = tool_data()
    data["requirements"][0]["extras"] = ["z", "a", "z"]
    requirements = [
        {"name": "z", "specifier": "==2"},
        {"name": "a", "specifier": "<4"},
        {"name": "z", "specifier": ">=1"},
    ]
    data["constraints"] = deepcopy(requirements)
    data["overrides"] = deepcopy(requirements)
    intent = parse(receipt(data)).intent
    assert intent.main.extras == ("z", "a", "z")
    for items in (intent.constraints, intent.overrides):
        assert [(item.name, item.specifier) for item in items] == [
            ("z", "==2"),
            ("a", "<4"),
            ("z", ">=1"),
        ]


def test_missing_and_explicit_empty_fields_do_not_inherit_external_defaults() -> None:
    missing = parse(receipt(tool_data())).intent
    assert missing.main.specifier is None and missing.main.marker is None
    assert missing.python is None and missing.options is None
    assert missing.with_requirements == missing.constraints == missing.overrides == ()
    data = tool_data()
    data["requirements"][0].update(specifier="", marker="")
    data["options"] = {"extra-index-url": [], "no-index": False}
    explicit = parse(receipt(data)).intent
    assert explicit.main.specifier == explicit.main.marker == ""
    assert explicit.options is not None
    assert explicit.options.extra_index_urls == ()
    assert explicit.options.no_index is False and explicit.options.index_url is None
    assert parse(receipt({**tool_data(), "options": {}})).intent.options is not None


@pytest.mark.parametrize("strategy", ["first-index", "unsafe-first-match", "unsafe-best-match"])
def test_supported_strategies_are_preserved_without_a_safety_upgrade_claim(strategy: str) -> None:
    data = tool_data()
    data["options"] = {"index-strategy": strategy}
    result = parse(receipt(data))
    assert result.intent.options is not None
    assert result.intent.options.index_strategy == strategy
    assert result.context_status == "context_required"


def test_registry_index_and_windows_entrypoint_paths_are_opaque_evidence() -> None:
    data = tool_data()
    data["requirements"][0]["index"] = "https://corp.example.invalid/simple"
    data["entrypoints"][0]["install-path"] = "C:/fixture/bin/muli.exe"
    data["python"] = "C:/fixture/python.exe"
    intent = parse(receipt(data)).intent
    assert intent.main.index == "https://corp.example.invalid/simple"
    assert intent.entrypoints[0].install_path == "C:/fixture/bin/muli.exe"
    assert intent.python == "C:/fixture/python.exe"


def test_no_pep_evaluation_or_candidate_pin_is_invented() -> None:
    data = tool_data()
    specifier, marker = "opaque specifier, not evaluated", "opaque marker; not evaluated"
    data["requirements"][0].update(specifier=specifier, marker=marker)
    parsed = parse(receipt(data))
    assert parsed.intent.main.specifier == specifier
    assert parsed.intent.main.marker == marker
    assert parsed.context_status == "context_required"
    assert "target" not in parsed.model_dump() and "profile_complete" not in repr(parsed)


@pytest.mark.parametrize("version", ["", "0.12.22", "0.13.2", "main", "uv 0.13.0", True, 13])
def test_other_versions_are_not_inferred_from_receipt_shape(version: Any) -> None:
    with pytest.raises(InstallProfileError, match="^unsupported_uv_version$"):
        parse_install_intent(PINNED_RECEIPT, uv_version=version)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("excludes", [], "unsupported_tool_field"),
        ("build-constraint-dependencies", [], "unsupported_tool_field"),
        ("future-option", 1, "unsupported_tool_field"),
        ("options", {"index": []}, "unsupported_option"),
        ("options", {"keyring-provider": "subprocess"}, "unsupported_option"),
        ("options", {"config-settings": {"secret-key": "synthetic-secret"}}, "unsupported_option"),
        ("options", {"no-binary": False}, "unsupported_option"),
        ("options", {"future-setting": {"version": 2}}, "unsupported_option"),
        ("options", {"index-strategy": "new-strategy"}, "unsupported_index_strategy"),
    ],
)
def test_unsupported_options_are_not_dropped(field: str, value: Any, code: str) -> None:
    data = tool_data()
    data[field] = value
    with pytest.raises(InstallProfileError) as error:
        parse(receipt(data))
    assert error.value.code == code
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("location", ["main", "with", "constraints", "overrides"])
@pytest.mark.parametrize("source", ["url", "git", "path", "directory", "editable", "virtual"])
def test_non_registry_sources_are_rejected_in_every_requirement_position(
    location: str, source: str
) -> None:
    data = tool_data()
    item = {"name": "muli-agent" if location == "main" else "other", source: "/fixture/source"}
    if location == "main":
        data["requirements"][0] = item
    elif location == "with":
        data["requirements"].append(item)
    else:
        data[location] = [item]
    with pytest.raises(InstallProfileError, match="^unsupported_requirement_source$"):
        parse(receipt(data))


@pytest.mark.parametrize("location", ["main", "with", "constraints", "overrides"])
def test_legacy_requirement_strings_require_a_separate_adapter(location: str) -> None:
    data = tool_data()
    if location == "main":
        data["requirements"][0] = "muli-agent[mcp]>=2"
    elif location == "with":
        data["requirements"].append("other==1")
    else:
        data[location] = ["other==1"]
    with pytest.raises(InstallProfileError, match="^unsupported_requirement_format$"):
        parse(receipt(data))


@pytest.mark.parametrize("field", ["version", "source", "groups", "conflict", "future-key"])
def test_unknown_requirement_fields_are_not_mistaken_for_lock_format(field: str) -> None:
    data = tool_data()
    data["requirements"][0][field] = "synthetic-secret"
    with pytest.raises(InstallProfileError, match="^unsupported_requirement_field$"):
        parse(receipt(data))


@pytest.mark.parametrize(
    "url",
    [
        "http://registry.example.invalid/simple",
        "https://user:synthetic-secret@registry.example.invalid/simple",
        "https://user@registry.example.invalid/simple",
        "https://registry.example.invalid/simple?token=synthetic-secret",
        "https://registry.example.invalid/simple#synthetic-secret",
        "https://registry.example.invalid/simple?",
        "https://registry.example.invalid/simple#",
        " https://registry.example.invalid/simple",
        "https://registry.example.invalid/white space",
        "https://registry.example.invalid:invalid/simple",
        "https://registry.example.invalid:70000/simple",
        "https://[invalid/simple",
        "https:///simple",
        "https://registry.example.invalid\\other/simple",
    ],
)
def test_credential_bearing_or_unsupported_index_urls_are_not_echoed(url: str) -> None:
    data = tool_data()
    data["options"] = {"index-url": url}
    with pytest.raises(InstallProfileError, match="^unsupported_index_url$") as error:
        parse(receipt(data))
    assert str(error.value) == error.value.code
    assert "synthetic-secret" not in repr(error.value)


@pytest.mark.parametrize("location", ["requirement", "extra-index"])
def test_url_policy_also_covers_bound_requirement_and_extra_index(location: str) -> None:
    data = tool_data()
    url = "https://user:synthetic-secret@registry.example.invalid/simple"
    if location == "requirement":
        data["requirements"][0]["index"] = url
    else:
        data["options"] = {"extra-index-url": [url]}
    with pytest.raises(InstallProfileError, match="^unsupported_index_url$"):
        parse(receipt(data))


@pytest.mark.parametrize(
    "url",
    [
        "/fixture/wheelhouse",
        "file:relative",
        "file:/fixture/wheelhouse",
        "file://localhost/fixture/wheelhouse",
        "file://remote/fixture/wheelhouse",
        "file:////remote/wheelhouse",
        "https://example.invalid/wheelhouse",
        "file:///fixture/wheelhouse?token=synthetic-secret",
        "file:///fixture/wheelhouse#synthetic-secret",
        "file:///fixture/wheelhouse?",
        "file:///fixture/wheelhouse#",
        "file:///fixture/white space",
        "file:///fixture/%00",
        "file:///fixture/%0a",
        "file:///fixture/%5cother",
        "file:///fixture/%2e%2e/wheelhouse",
        "file:///fixture/../wheelhouse",
        "file:///fixture/%ff",
        "file:///fixture/%",
        "file:///fixture/%zz",
    ],
)
def test_nonlocal_or_ambiguous_find_links_are_not_authorized(url: str) -> None:
    data = tool_data()
    data["options"] = {"find-links": [url]}
    with pytest.raises(InstallProfileError, match="^unsupported_find_links$") as error:
        parse(receipt(data))
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"tool = []",
        b"[tool]\nentrypoints = []",
        b"[tool]\nrequirements = []\nentrypoints = []",
        b"tool = {requirements = [{name = 'other'}], entrypoints = []}",
        b"tool = {requirements = [{name = 'muli-agent'}]}",
        b"[tool]\nrequirements = [\nsecret",
        b"[tool]\npython = '3.12'\npython = '3.13'",
        b"[tool]\nrequirements = [{name='muli-agent', name='other'}]",
        b"\xff",
    ],
)
def test_missing_malformed_and_duplicate_receipt_inputs_fail_closed(data: bytes) -> None:
    with pytest.raises(InstallProfileError) as error:
        parse(data)
    assert error.value.code in {"invalid_receipt", "unsupported_tool"}
    assert "secret" not in str(error.value)


def test_oversized_toml_integer_has_fixed_error_without_changing_interpreter_limits() -> None:
    digits = sys.get_int_max_str_digits()
    if not 0 < digits <= 8192:
        pytest.skip("integer conversion limit is disabled or exceeds the bounded fixture")
    data = b"[tool]\npython = " + b"1" * (digits + 1)
    with pytest.raises(InstallProfileError, match="^invalid_receipt$") as error:
        parse(data)
    assert str(error.value) == error.value.code


def test_root_and_entrypoint_unknown_fields_are_rejected() -> None:
    with pytest.raises(InstallProfileError, match="^unsupported_receipt_field$"):
        parse(receipt(tool_data()) + b"\nproducer-version = 1")
    data = tool_data()
    data["entrypoints"][0]["command"] = "synthetic-secret"
    with pytest.raises(InstallProfileError, match="^unsupported_entrypoint_field$"):
        parse(receipt(data))


def test_multiple_primary_requirements_cannot_ambiguously_replace_installation_intent() -> None:
    data = tool_data()
    data["requirements"].append({"name": "muli-agent", "specifier": "==99"})
    with pytest.raises(InstallProfileError, match="^ambiguous_tool_requirement$"):
        parse(receipt(data))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requirements", False),
        ("constraints", "other==1"),
        ("overrides", False),
        ("python", 312),
        ("entrypoints", False),
        ("options", {"no-index": 0}),
        ("options", {"no-index": "false"}),
        ("options", {"no-build": 0}),
        ("options", {"no-build": "false"}),
        ("options", {"find-links": "file:///fixture/wheelhouse"}),
        ("options", {"find-links": [True]}),
        ("options", {"extra-index-url": "https://example.invalid/simple"}),
    ],
)
def test_wrong_types_are_not_coerced(field: str, value: Any) -> None:
    data = tool_data()
    data[field] = value
    with pytest.raises(InstallProfileError, match="^invalid_receipt$"):
        parse(receipt(data))


@pytest.mark.parametrize("field", ["name", "extras", "specifier", "marker", "index"])
def test_requirement_field_types_are_strict(field: str) -> None:
    data = tool_data()
    data["requirements"][0][field] = 1
    with pytest.raises(InstallProfileError, match="^invalid_receipt$"):
        parse(receipt(data))


@pytest.mark.parametrize("name", ["Muli_Agent", "muli.agent", "muli--agent", "muli/agent", "沐璃"])
def test_non_normalized_names_are_not_silently_rewritten(name: str) -> None:
    data = tool_data()
    data["requirements"][0]["name"] = name
    with pytest.raises(InstallProfileError, match="^unsupported_requirement_name$"):
        parse(receipt(data))


@pytest.mark.parametrize("field", ["specifier", "marker"])
def test_control_characters_in_opaque_requirements_are_rejected(field: str) -> None:
    data = tool_data()
    data["requirements"][0][field] = "synthetic\nsecret"
    with pytest.raises(InstallProfileError, match="^invalid_receipt$"):
        parse(receipt(data))


def test_exact_byte_limit_is_checked_before_toml_parsing() -> None:
    data = receipt(tool_data())
    assert parse(data, IntentLimits(max_receipt_bytes=len(data))).status == "intent_parsed"
    with pytest.raises(InstallProfileError, match="^receipt_budget$"):
        parse(data, IntentLimits(max_receipt_bytes=len(data) - 1))
    with pytest.raises(InstallProfileError, match="^receipt_budget$"):
        parse(b"secret" * 50_000)


@pytest.mark.parametrize(
    "limits",
    [IntentLimits(max_nodes=16), IntentLimits(max_depth=2), IntentLimits(max_extras=1)],
)
def test_tree_and_collection_budgets_reject_instead_of_truncating(limits: IntentLimits) -> None:
    with pytest.raises(InstallProfileError, match="^receipt_budget$"):
        parse(receipt(tool_data()), limits)


def test_requirement_budget_covers_with_constraints_and_overrides_together() -> None:
    data = tool_data()
    data["constraints"] = [{"name": "other"}]
    data["overrides"] = [{"name": "other"}]
    assert len(parse(receipt(data), IntentLimits(max_requirements=3)).intent.constraints) == 1
    with pytest.raises(InstallProfileError, match="^receipt_budget$"):
        parse(receipt(data), IntentLimits(max_requirements=2))


def test_index_entrypoint_and_utf8_string_budgets() -> None:
    data = tool_data()
    data["options"] = {
        "index-url": "https://a.example.invalid/simple",
        "extra-index-url": ["https://b.example.invalid/simple"],
    }
    with pytest.raises(InstallProfileError, match="^receipt_budget$"):
        parse(receipt(data), IntentLimits(max_indexes=1))
    data = tool_data()
    data["entrypoints"] *= 2
    with pytest.raises(InstallProfileError, match="^receipt_budget$"):
        parse(receipt(data), IntentLimits(max_entrypoints=1))
    data = tool_data()
    data["requirements"][0]["marker"] = "字" * 11
    with pytest.raises(InstallProfileError, match="^receipt_budget$"):
        parse(receipt(data), IntentLimits(max_string_bytes=32))


def test_find_links_share_the_finite_source_budget() -> None:
    data = tool_data()
    data["options"] = {
        "index-url": "https://a.example.invalid/simple",
        "extra-index-url": ["https://b.example.invalid/simple"],
        "find-links": ["file:///fixture/wheelhouse"],
    }
    assert parse(receipt(data), IntentLimits(max_indexes=3)).intent.options is not None
    with pytest.raises(InstallProfileError, match="^receipt_budget$"):
        parse(receipt(data), IntentLimits(max_indexes=2))


def test_output_and_nested_collection_values_are_immutable() -> None:
    parsed = parse(PINNED_RECEIPT)
    with pytest.raises(ValidationError):
        parsed.context_status = "context_required"
    with pytest.raises(ValidationError):
        parsed.intent.main.specifier = "==99"
    with pytest.raises(AttributeError):
        parsed.intent.main.extras.append("other")  # type: ignore[attr-defined]
    assert isinstance(parsed.intent.with_requirements, tuple)
    assert isinstance(parsed.intent.entrypoints, tuple)
    assert parsed.intent.options is not None
    assert isinstance(parsed.intent.options.extra_index_urls, tuple)


def test_pure_entrypoint_does_not_discover_files_environment_credentials_or_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("unexpected I/O during receipt parsing")

    with monkeypatch.context() as blocked:
        for target, name in (
            (builtins, "open"),
            (Path, "open"),
            (Path, "read_bytes"),
            (Path, "read_text"),
            (os, "getenv"),
            (sqlite3, "connect"),
            (socket, "socket"),
            (subprocess, "Popen"),
        ):
            blocked.setattr(target, name, forbidden)
        result = parse(PINNED_RECEIPT)
        local = parse_install_intent(OBSERVED_UV_0131_RECEIPT, uv_version="0.13.1")
    assert result.status == "intent_parsed" and result.context_status == "context_required"
    assert local.intent.options is not None
    assert local.intent.options.find_links == ("file:///fixture/wheelhouse",)
