"""Signed M reader bytes define the explicit memory2 -> memory3 boundary."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_channel_format_upgrade import snapshot as stored_rows
from test_channel_format_upgrade import upgrade
from test_fresh_format_admission import database as database
from test_fresh_format_admission import deny_construction_writes, no_model, snapshot
from test_project_inbox_format import frozen_e136_bytes, frozen_e136_reader

from muli.attachment_memory_source import attachment_source_metadata, restore_attachment_source
from muli.compatibility import CompatibilityManifest, assess_compatibility
from muli.config import ConfigFile, MemoryWatchSettings
from muli.document_reading import document_revision
from muli.events import EventBus
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    PRE_PROJECT_INBOX_FORMAT_STATE,
    PRE_WEBPAGE_MEMORY_FORMAT_STATE,
    FormatAdmissionError,
)
from muli.format_evidence import FORMAT_STATE_KEY, read_optional_format_evidence
from muli.maintenance import Admission
from muli.memory import Memory
from muli.memory_pipeline import MemoryPipeline, MemoryPipelineConfig
from muli.memory_watches import MATCH_EVENT, MemoryWatches
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import Store
from muli.store_schema import ExistingStoreError
from muli.webpage_snapshot import compose_webpage_snapshot

_FIXTURES = Path(__file__).parent / "fixtures"
# Frozen by root from signed M 4c069a12cfe2bcbab062db44105c10de669861f3.
# These execute the original readers with shared models, not an old wheel.
_M_BYTES: dict[str, str] = {
    "attachment_memory_source_v2_118.py": (
        "3d26e657d6119bd3d0fa0b4720bbab41a4109d02715c6f1b11816218b5ccf482"
    ),
    "format_admission_v2_118.py": (
        "592e817a9d4c564cfd01f92f450cf809a61bdb636c24c26117544ea9427e1fab"
    ),
    "compatibility_v2_118.json": (
        "ae552b4177c4cb2c26090b21d8ec81b6213f25089fdc721e3ed58883971b2be0"
    ),
}


def frozen_m_bytes(name: str) -> bytes:
    raw = (_FIXTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _M_BYTES[name]
    return raw


def frozen_m_reader(name: str) -> ModuleType:
    frozen_m_bytes(name)
    path = _FIXTURES / name
    spec = importlib.util.spec_from_file_location("muli_frozen_" + path.stem, path)
    assert spec is not None and spec.loader is not None
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    return reader


def legacy_sources() -> list[dict[str, Any]]:
    old = frozen_m_reader("attachment_memory_source_v2_118.py")
    content = "保留原文：完整旧来源与外部身份🙂。"
    checksum = hashlib.sha256(content.encode()).hexdigest()
    document_checksum = hashlib.sha256(b"original-document").hexdigest()
    document = {
        "format": "docx",
        "scope": "docx_main_body_text",
        "source_revision": document_checksum,
        "revision": document_revision(document_checksum, content.encode()),
    }
    return [
        old.attachment_source("a" * 32, content, checksum, 12),
        old.attachment_source("b" * 32, content, document_checksum, 13, document=document),
        old.attachment_source("c" * 32, content, checksum, 14, transcript=True),
    ]


def test_m_reader_and_manifest_refuse_webpage_memory_contract(database: Path) -> None:
    old = frozen_m_reader("format_admission_v2_118.py")
    manifest = CompatibilityManifest.from_json(frozen_m_bytes("compatibility_v2_118.json"))
    memory4 = frozen_e136_reader()
    current = CompatibilityManifest.from_json(frozen_e136_bytes("compatibility_v2_136.json"))
    assert manifest.release == "2.118.0"
    assert manifest.formats.memory.reads == (2,) and manifest.formats.memory.writes == 2
    assert current.formats.memory.reads == (4,) and current.formats.memory.writes == 4
    assert old.CURRENT_FORMAT_STATE == PRE_WEBPAGE_MEMORY_FORMAT_STATE
    # Preserve the memory-only counterexample independently of ingress5.
    assert memory4.CURRENT_FORMAT_STATE == PRE_PROJECT_INBOX_FORMAT_STATE
    assert memory4.CURRENT_FORMAT_STATE.formats.model_dump() == {
        **old.CURRENT_FORMAT_STATE.formats.model_dump(),
        "memory": 4,
    }
    before = snapshot(database)
    with closing(sqlite3.connect(database)) as db:
        evidence = read_optional_format_evidence(db)
        assert evidence is not None and evidence.state == CURRENT_FORMAT_STATE
        old.require_current_format(replace(evidence, state=PRE_WEBPAGE_MEMORY_FORMAT_STATE))
        with pytest.raises(old.FormatAdmissionError, match="store_format_unsupported"):
            old.require_current_format(evidence)
        with pytest.raises(old.FormatAdmissionError, match="store_format_unsupported"):
            old.require_current_format(replace(evidence, state=PRE_PROJECT_INBOX_FORMAT_STATE))
        assert db.total_changes == 0
    for state in (PRE_WEBPAGE_MEMORY_FORMAT_STATE, PRE_PROJECT_INBOX_FORMAT_STATE):
        assessment = assess_compatibility(manifest, current, state, handoff_protocol=2)
        assert not assessment.format_compatible and assessment.domain == "memory"
    assert snapshot(database) == before


@pytest.mark.parametrize("scope", ["http_utf8_text_v1", "html_static_text_v1"])
def test_m_source_reader_refuses_webpage_identity_that_new_reader_restores(scope: str) -> None:
    old = frozen_m_reader("attachment_memory_source_v2_118.py")
    identifier = "d" * 32
    content = compose_webpage_snapshot(
        "完整网页正文🙂\n正文中也可能写着来源字段。",
        requested_url="https://example.invalid/requested",
        final_url="https://example.invalid/final",
        captured_at="2026-10-29T08:00:00+00:00",
        scope=scope,
    )
    checksum = hashlib.sha256(content.encode()).hexdigest()
    marker = f"attachment:{identifier}:webpage:v1:{scope}:{checksum}"
    metadata = attachment_source_metadata(identifier, marker)
    assert metadata == {
        "format": "webpage",
        "scope": scope,
        "source_revision": checksum,
        "document_revision": None,
    }
    restored = restore_attachment_source(identifier, content, marker, 12)
    assert restored == {
        "id": "attachment:" + identifier,
        "content": content,
        "source": marker,
        "session_id": "",
        "role": "tool",
        "principal": "external:attachment",
        "created_at": 12,
    }
    with pytest.raises(ValueError):
        old.attachment_source_metadata(identifier, marker)
    with pytest.raises(ValueError):
        old.restore_attachment_source(identifier, content, marker, 12)


def test_new_reader_preserves_original_plain_document_and_asr_sources() -> None:
    old = frozen_m_reader("attachment_memory_source_v2_118.py")
    for source in legacy_sources():
        identifier = source["id"].removeprefix("attachment:")
        arguments = (identifier, source["content"], source["source"], source["created_at"])
        assert old.restore_attachment_source(*arguments) == source
        assert restore_attachment_source(*arguments) == source
        assert attachment_source_metadata(identifier, source["source"]) == (
            old.attachment_source_metadata(identifier, source["source"])
        )


@pytest.mark.parametrize("opener", ["ordinary", "existing", "runtime_store", "runtime", "server"])
def test_memory2_is_refused_before_construction_writes_or_business_recovery(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "UPDATE runtime_state SET value=? WHERE name=?",
            (PRE_WEBPAGE_MEMORY_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
        )
        db.commit()
    before = snapshot(database)

    def forbidden_recovery(runtime: Runtime) -> None:
        raise AssertionError("The M memory2 tuple must fail before business recovery")

    monkeypatch.setattr(Runtime, "_recover", forbidden_recovery)
    connections, mutations = deny_construction_writes(monkeypatch)
    with pytest.raises(
        (FormatAdmissionError, ExistingStoreError), match="store_format_upgrade_required"
    ):
        if opener == "ordinary":
            Store(database)
        elif opener == "existing":
            Store.open_existing(database)
        elif opener == "runtime_store":
            Store.open_runtime(database, default_mode="auto")
        elif opener == "runtime":
            Runtime(ConfigFile(tmp_path / "config.json"), database.parent, no_model)
        else:
            create_app(ConfigFile(tmp_path / "config.json"), database.parent)
    assert mutations == [] and snapshot(database) == before
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX stopped fixed upgrade")
async def test_memory2_upgrade_preserves_sources_checkpoints_tombstones_and_watch_proofs(
    tmp_path: Path,
) -> None:
    path = tmp_path.resolve() / "data" / "muli.sqlite3"
    store = Store.open_runtime(path, default_mode="auto")
    memory, bus = Memory(store), EventBus(store)
    pipeline = MemoryPipeline(memory, bus, config=MemoryPipelineConfig(auto_extract=False))
    settings = MemoryWatchSettings()
    watches = MemoryWatches(
        store,
        bus,
        settings=lambda: settings,
        enabled=lambda: True,
        max_source_chars=lambda: pipeline.config.max_source_chars,
        max_output_chars=lambda: 6000,
        admission=Admission(),
    )
    originals = legacy_sources()
    forgotten = frozen_m_reader("attachment_memory_source_v2_118.py").attachment_source(
        "f" * 32,
        originals[0]["content"],
        hashlib.sha256(originals[0]["content"].encode()).hexdigest(),
        15,
    )
    try:
        watches.create(
            store.main_session()["id"],
            "historical-watch-input",
            "收藏资料中是否出现保留原文？",
            ["保留原文"],
            key="memory2-upgrade",
        )
        await pipeline.start()
        for source in originals:
            pipeline.ingest(source, trusted_owner=False)
            async with asyncio.timeout(3):
                job = await pipeline.wait(source["id"])
            assert job is not None and job["state"] == "done" and job["checkpoint"]
        more = True
        while more:
            more, _ = watches._consume_page()
        assert any(event["type"] == MATCH_EVENT for event in store.events(limit=1000))
        saved = {source["id"]: memory.source(source["id"]) for source in originals}
        assert all(source is not None for source in saved.values())
        assert memory.register_source(forgotten)[1]
        assert memory.forget_source(forgotten["id"])
    finally:
        await watches.close()
        await pipeline.close()
        store.close()
    with closing(sqlite3.connect(path)) as db:
        evidence = read_optional_format_evidence(db)
        assert evidence is not None
        identity = evidence.store_instance, evidence.main_session
        db.execute(
            "UPDATE runtime_state SET value=? WHERE name=?",
            (PRE_WEBPAGE_MEMORY_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
        )
        db.execute(
            "INSERT INTO channel_outbox(id,name,route,payload,state,next_attempt,created_at,updated_at) "
            "VALUES('unknown-webpage-upgrade','onebot','{}','{}','unknown',0,1,1)"
        )
        db.commit()
    before = stored_rows(path, omit_marker=True)
    assert upgrade(path.parent)
    assert stored_rows(path, omit_marker=True) == before
    assert not upgrade(path.parent)
    reopened = Store.open_runtime(path, default_mode="auto")
    try:
        assert reopened.format_state == CURRENT_FORMAT_STATE
        evidence = read_optional_format_evidence(reopened.db)
        assert evidence is not None
        assert (evidence.store_instance, evidence.main_session) == identity
        current = Memory(reopened)
        for source in originals:
            assert current.source(source["id"]) == saved[source["id"]]
            assert (
                restore_attachment_source(
                    source["id"].removeprefix("attachment:"),
                    source["content"],
                    source["source"],
                    source["created_at"],
                )
                == source
            )
        assert current.register_source(forgotten) == (None, False)
    finally:
        reopened.close()
    assert stored_rows(path, omit_marker=True) == before
