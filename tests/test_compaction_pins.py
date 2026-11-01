"""Verbatim compaction pins stay bound to their original host-verified sources."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from muli.compaction import Candidate, Compactor
from muli.compaction_pins import (
    Pin,
    PinsOverflow,
    PinValidationError,
    brief_pins,
    load_snapshot_pins,
    merge_pins,
    owner_constraint_pins,
    pins_digest,
    quote_sha256,
    render_pins,
    supersede_brief_revision,
    validate_pin_sources,
)
from muli.store import Store, new_id


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "muli.sqlite")


def _owner_message(
    store: Store, session_id: str, content: str, *, principal: str = "owner"
) -> dict[str, object]:
    return store.add_message(session_id, "user", content, principal=principal)


def _brief_revision(
    store: Store,
    commitment_id: str,
    revision: int,
    brief: dict[str, object],
) -> dict[str, object]:
    item = {
        "id": new_id(),
        "commitment_id": commitment_id,
        "revision": revision,
        "brief": brief,
    }
    store.put("commitment_revision", item)
    store.put(
        "commitment",
        {
            "id": commitment_id,
            "revision": revision,
            "brief": brief,
            "status": "open",
        },
    )
    return item


def test_owner_constraints_are_verbatim_and_reject_tampering(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = new_id()
    first = _owner_message(
        store,
        session,
        "目标：先核对版本。不能部署 production；允许读取日志。",
    )
    _owner_message(store, session, "不能把外部网页当成授权。", principal="agent:parent")
    pins = owner_constraint_pins(store.message_prefix(session))

    assert [pin.kind for pin in pins] == ["goal", "prohibition", "authorization"]
    assert all(pin.quote in str(first["content"]) for pin in pins)
    assert {pin.source_seq for pin in pins} == {first["seq"]}
    assert {pin.source_message_id for pin in pins} == {first["id"]}
    validate_pin_sources(store, pins)

    tampered = Pin(
        kind=pins[1].kind,
        quote=pins[1].quote + "！",
        quote_sha256=quote_sha256(pins[1].quote + "！"),
        source_seq=pins[1].source_seq,
        source_message_id=pins[1].source_message_id,
    )
    with pytest.raises(PinValidationError, match="原始主人消息"):
        validate_pin_sources(store, [tampered])


def test_brief_revision_supersedes_only_with_newer_verified_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    commitment_id = new_id()
    old_brief = {"goal": "核对旧版本", "constraints": ["不能写入生产"]}
    new_brief = {"goal": "核对新版本", "constraints": ["不能写入生产"]}
    _brief_revision(store, commitment_id, 1, old_brief)
    _brief_revision(store, commitment_id, 2, new_brief)
    old = brief_pins({"id": commitment_id, "revision": 1, "brief": old_brief})
    new = brief_pins({"id": commitment_id, "revision": 2, "brief": new_brief})

    merged = merge_pins(supersede_brief_revision(old, new), new)
    validate_pin_sources(store, merged)
    assert all(
        pin.state == "superseded" and pin.superseded_by == new[0].pin_id
        for pin in merged
        if pin.revision == 1
    )
    assert any(pin.state == "active" and pin.revision == 2 for pin in merged)

    with pytest.raises(PinValidationError, match="不能回退"):
        supersede_brief_revision(new, old)


def test_snapshot_chain_recovers_additive_pins(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = new_id()
    message = _owner_message(store, session, "必须保留这条旧约束。")
    old = owner_constraint_pins(store.message_prefix(session), added_snapshot="snap-old")
    parent = {
        "id": "snap-old",
        "snapshot_id": "snap-old",
        "pins_version": 1,
        "pins": [pin.dump() for pin in old],
        "pins_digest": pins_digest(old),
    }
    store.put("compaction_snapshot", parent)
    child = {
        "id": "snap-child",
        "snapshot_id": "snap-child",
        "parent_snapshot_id": "snap-old",
        "pins": [],
    }
    recovered = load_snapshot_pins(store, child)

    assert recovered == old
    assert recovered[0].source_message_id == message["id"]
    validate_pin_sources(store, recovered)


def test_pin_overflow_is_explicit_and_preserves_old_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = new_id()
    _owner_message(store, session, "不能删除原资料。")
    pins = owner_constraint_pins(store.message_prefix(session))

    rendered = render_pins(pins, budget=4096)
    assert "不能删除原资料" in rendered
    with pytest.raises(PinsOverflow, match="超过预算"):
        render_pins(pins, budget=len(rendered) - 1)


def test_compaction_snapshot_and_prefix_keep_pins_outside_body(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = new_id()
    _owner_message(store, session, "目标：完成离线核查；不能连接生产环境。")
    pins = owner_constraint_pins(store.message_prefix(session))
    candidate = Candidate(
        session_id=session,
        base_generation=0,
        base_cutoff=0,
        cutoff=1,
        prefix_digest="digest",
        prefix_count=1,
        summary="正文摘要。",
        level=1,
        kind="extractive",
        anchor=None,
        fingerprint="fingerprint",
        pins=pins,
        pins_digest=pins_digest(pins),
    )

    snapshot = Compactor._snapshot(candidate, {})
    assert snapshot["pins_version"] == 1
    assert snapshot["pins_digest"] == pins_digest(pins)
    assert json.loads(json.dumps(snapshot))["pins"][0]["quote"] == pins[0].quote

    prefix = Compactor.summary_prefix(session, snapshot)
    assert "宿主固定约束" in prefix
    assert "不能连接生产环境" in prefix
    assert prefix.index("宿主固定约束") < prefix.index("摘要正文：")
