from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import muli
from muli.compatibility import (
    CompatibilityError,
    CompatibilityManifest,
    FormatDomain,
    FormatState,
    FormatSupport,
    assess_compatibility,
)
from muli.format_admission import CURRENT_FORMAT_STATE

DOMAINS: tuple[FormatDomain, ...] = (
    "database",
    "objects",
    "configuration",
    "ingress",
    "outbox",
    "memory",
    "install_profile",
)


def manifest_data(release: str = "2.18.0") -> dict[str, Any]:
    # These are test-only contract IDs, not declarations for a released MuLi.
    return {
        "metadata_version": 1,
        "distribution": "muli-agent",
        "release": release,
        "handoff_protocol": 2,
        "requires_migration": False,
        "formats": {domain: {"reads": [1], "writes": 1} for domain in DOMAINS},
    }


def state_data() -> dict[str, Any]:
    return {"metadata_version": 1, "formats": dict.fromkeys(DOMAINS, 1)}


def encode(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def manifest(data: dict[str, Any] | None = None) -> CompatibilityManifest:
    return CompatibilityManifest.from_json(encode(manifest_data() if data is None else data))


def state(data: dict[str, Any] | None = None) -> FormatState:
    return FormatState.from_json(encode(state_data() if data is None else data))


def test_shipped_declaration_matches_current_release_and_persisted_contracts() -> None:
    assert muli.__file__ is not None
    with (Path(muli.__file__).parent / "compatibility.json").open("rb") as stream:
        shipped = CompatibilityManifest.from_json(stream.read(16_384 + 1))
    assert shipped.release == muli.__version__
    assert assess_compatibility(
        shipped, shipped, CURRENT_FORMAT_STATE, handoff_protocol=2
    ).format_compatible
    for domain in DOMAINS:
        version = getattr(CURRENT_FORMAT_STATE.formats, domain)
        support = getattr(shipped.formats, domain)
        assert support.reads == (version,) and support.writes == version


@pytest.mark.parametrize("release", ["2.18.0", "2.19.0", "2.9.0"])
def test_release_order_never_replaces_declared_formats(release: str) -> None:
    result = assess_compatibility(
        manifest(), manifest(manifest_data(release)), state(), handoff_protocol=2
    )
    assert result.format_compatible
    assert result.code == "declared_format_compatible"
    assert result.domain is None and result.side is None


def test_domains_keep_separate_format_namespaces() -> None:
    data, evidence = manifest_data(), state_data()
    for version, domain in enumerate(DOMAINS, start=1):
        data["formats"][domain] = {"reads": [version], "writes": version}
        evidence["formats"][domain] = version
    result = assess_compatibility(
        manifest(data), manifest(data), state(evidence), handoff_protocol=2
    )
    assert result.format_compatible


@pytest.mark.parametrize("missing", ["base", "candidate", "current"])
def test_missing_evidence_is_not_inferred(missing: str) -> None:
    result = assess_compatibility(
        None if missing == "base" else manifest(),
        None if missing == "candidate" else manifest(),
        None if missing == "current" else state(),
        handoff_protocol=2,
    )
    assert not result.format_compatible
    assert (
        result.code
        == {
            "base": "missing_base_manifest",
            "candidate": "missing_candidate_manifest",
            "current": "missing_format_state",
        }[missing]
    )


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("side", ["base", "candidate"])
def test_every_domain_requires_explicit_read_support(domain: FormatDomain, side: str) -> None:
    data = manifest_data()
    data["formats"][domain] = {"reads": [3], "writes": 3}
    changed = manifest(data)
    result = assess_compatibility(
        changed if side == "base" else manifest(),
        changed if side == "candidate" else manifest(),
        state(),
        handoff_protocol=2,
    )
    assert not result.format_compatible
    assert result.code == "current_format_unreadable"
    assert result.domain == domain and result.side == side


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("side", ["base", "candidate"])
def test_mutual_read_support_does_not_authorize_new_write_format(
    domain: FormatDomain, side: str
) -> None:
    data = manifest_data()
    data["formats"][domain] = {"reads": [1, 2], "writes": 2}
    changed = manifest(data)
    other_data = manifest_data()
    other_data["formats"][domain] = {"reads": [1, 2], "writes": 1}
    other = manifest(other_data)
    result = assess_compatibility(
        changed if side == "base" else other,
        changed if side == "candidate" else other,
        state(),
        handoff_protocol=2,
    )
    assert not result.format_compatible
    assert result.code == "write_format_change"
    assert result.domain == domain and result.side == side


def test_matching_sql_schema_does_not_hide_unknown_delivery_semantics() -> None:
    data = manifest_data()
    data["formats"]["outbox"] = {"reads": [2], "writes": 2}
    result = assess_compatibility(manifest(), manifest(data), state(), handoff_protocol=2)
    assert not result.format_compatible
    assert result.code == "current_format_unreadable" and result.domain == "outbox"


def test_format_ids_have_no_implicit_order_or_contiguous_range() -> None:
    data, evidence = manifest_data(), state_data()
    for domain in DOMAINS:
        data["formats"][domain] = {"reads": [1, 3], "writes": 3}
        evidence["formats"][domain] = 2
    result = assess_compatibility(
        manifest(data), manifest(data), state(evidence), handoff_protocol=2
    )
    assert not result.format_compatible
    assert result.code == "current_format_unreadable"


@pytest.mark.parametrize("side", ["base", "candidate"])
@pytest.mark.parametrize("field", ["requires_migration", "handoff_protocol"])
def test_migration_or_handoff_disagreement_is_rejected(side: str, field: str) -> None:
    data = manifest_data()
    data[field] = True if field == "requires_migration" else 3
    changed = manifest(data)
    result = assess_compatibility(
        changed if side == "base" else manifest(),
        changed if side == "candidate" else manifest(),
        state(),
        handoff_protocol=2,
    )
    assert not result.format_compatible
    assert result.code == (
        "migration_required" if field == "requires_migration" else "handoff_protocol_unsupported"
    )
    assert result.side == side


@pytest.mark.parametrize("protocol", [True, 0, -1, 2_147_483_648, "2", 2.0])
def test_caller_handoff_protocol_is_not_coerced(protocol: Any) -> None:
    result = assess_compatibility(manifest(), manifest(), state(), handoff_protocol=protocol)
    assert not result.format_compatible and result.code == "handoff_protocol_unsupported"


@pytest.mark.parametrize("field", ["metadata_version", "handoff_protocol"])
@pytest.mark.parametrize("invalid", [None, "1", 1.0, True])
def test_manifest_field_types_are_strict(field: str, invalid: Any) -> None:
    data = manifest_data()
    data[field] = invalid
    with pytest.raises(CompatibilityError, match="^invalid_metadata$"):
        manifest(data)


@pytest.mark.parametrize("invalid", [None, "false", 0, 1, 1.0])
def test_migration_declaration_requires_a_real_boolean(invalid: Any) -> None:
    data = manifest_data()
    data["requires_migration"] = invalid
    with pytest.raises(CompatibilityError, match="^invalid_metadata$"):
        manifest(data)


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("invalid", [True, "1", 1.0, 0, -1, 2_147_483_648])
def test_current_domain_versions_are_strict(domain: FormatDomain, invalid: Any) -> None:
    data = state_data()
    data["formats"][domain] = invalid
    with pytest.raises(CompatibilityError, match="^invalid_metadata$"):
        state(data)


@pytest.mark.parametrize("reader", [CompatibilityManifest.from_json, FormatState.from_json])
@pytest.mark.parametrize("version", [0, 2, True, "1", 1.0])
def test_unknown_or_coerced_metadata_versions_are_rejected(
    reader: Callable[[bytes], object], version: Any
) -> None:
    data = manifest_data() if reader == CompatibilityManifest.from_json else state_data()
    data["metadata_version"] = version
    with pytest.raises(CompatibilityError, match="^invalid_metadata$"):
        reader(encode(data))


@pytest.mark.parametrize("domain", DOMAINS)
def test_missing_or_extra_domain_cannot_be_ignored(domain: FormatDomain) -> None:
    for data, reader in (
        (manifest_data(), CompatibilityManifest.from_json),
        (state_data(), FormatState.from_json),
    ):
        previous = data["formats"].pop(domain)
        with pytest.raises(CompatibilityError, match="^invalid_metadata$"):
            reader(encode(data))
        data["formats"][domain] = previous
        data["formats"]["future_domain"] = previous
        with pytest.raises(CompatibilityError, match="^invalid_metadata$"):
            reader(encode(data))


@pytest.mark.parametrize(
    "support",
    [
        {"reads": [], "writes": 1},
        {"reads": [1, 1], "writes": 1},
        {"reads": list(range(1, 10)), "writes": 1},
        {"reads": [1], "writes": 2},
        {"reads": [True], "writes": 1},
        {"reads": [1.0], "writes": 1},
        {"reads": [1], "writes": True},
        {"reads": [1], "writes": "1"},
    ],
)
def test_support_lists_are_bounded_unique_and_explicit(support: dict[str, Any]) -> None:
    data = manifest_data()
    data["formats"]["ingress"] = support
    with pytest.raises(CompatibilityError, match="^invalid_metadata$"):
        manifest(data)


@pytest.mark.parametrize("release", ["", "2.18", "2.18.0rc1", "02.18.0", "2.18.0\n", "9" * 41])
def test_release_identity_is_a_bounded_canonical_stable_triplet(release: str) -> None:
    with pytest.raises(CompatibilityError, match="^invalid_metadata$"):
        manifest(manifest_data(release))


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b"null",
        b"{}",
        b"\xff",
        b'{"extra":NaN}',
        b'{"extra":Infinity}',
        b"[" * 1024 + b"0" + b"]" * 1024,
    ],
)
def test_invalid_json_and_duplicate_keys_are_not_accepted(raw: bytes) -> None:
    for reader in (CompatibilityManifest.from_json, FormatState.from_json):
        with pytest.raises(CompatibilityError, match="^invalid_metadata$"):
            reader(raw)


@pytest.mark.parametrize("nested", [False, True])
def test_duplicates_in_an_otherwise_valid_declaration_are_rejected(nested: bool) -> None:
    for value, reader in (
        (manifest_data(), CompatibilityManifest.from_json),
        (state_data(), FormatState.from_json),
    ):
        raw = encode(value)
        if nested:
            field = b'"outbox": ' + encode(value["formats"]["outbox"])
            assert raw.count(field) == 1
            raw = raw.replace(field, field + b", " + field)
        else:
            raw = b'{"metadata_version": 1, ' + raw[1:]
        with pytest.raises(CompatibilityError, match="^invalid_metadata$"):
            reader(raw)


def test_oversized_input_is_rejected_before_json_parse() -> None:
    for reader in (CompatibilityManifest.from_json, FormatState.from_json):
        with pytest.raises(CompatibilityError, match="^metadata_budget$"):
            reader(b" " * 16_385)


def test_declarations_and_nested_read_sets_are_immutable() -> None:
    data = manifest()
    with pytest.raises(ValidationError):
        data.requires_migration = True
    with pytest.raises(ValidationError):
        data.formats.outbox.writes = 2
    assert data.formats.outbox.reads == (1,)
    assert FormatSupport(reads=(1,), writes=1) == data.formats.outbox


def test_assessment_never_opens_a_database_or_reads_files(monkeypatch: pytest.MonkeyPatch) -> None:
    base, candidate, current = manifest(), manifest(), state()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Compatibility declarations must not perform I/O")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    parsed = CompatibilityManifest.from_json(base.model_dump_json().encode("utf-8"))
    assert parsed == base
    assert assess_compatibility(parsed, candidate, current, handoff_protocol=2).format_compatible
