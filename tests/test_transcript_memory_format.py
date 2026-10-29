"""Frozen 2.114 readers and real store admission define the ASR format boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from test_fresh_format_admission import database as database
from test_fresh_format_admission import deny_construction_writes, no_model, snapshot
from test_webpage_memory_format import frozen_m_bytes, frozen_m_reader

from muli.attachment_memory_source import attachment_source, restore_attachment_source
from muli.compatibility import CompatibilityManifest, assess_compatibility
from muli.config import ConfigFile
from muli.document_reading import document_revision
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    PRE_TRANSCRIPT_MEMORY_FORMAT_STATE,
    PRE_WEBPAGE_MEMORY_FORMAT_STATE,
    FormatAdmissionError,
)
from muli.format_evidence import FORMAT_STATE_KEY, read_optional_format_evidence
from muli.runtime import Runtime
from muli.store import Store
from muli.store_schema import ExistingStoreError

_FIXTURES = Path(__file__).parent / "fixtures"
_H_BYTES = {
    "format_admission_v2_114.py": "f97071025ab5547f72a437c86075f684595cd451c5d5e504d966d4823d0bff24",
    "attachment_memory_source_v2_114.py": (
        "116615e7ea720cd7b600935574a5db6d421c9365e40c96f661fd2dc7ddc3fd72"
    ),
    "compatibility_v2_114.json": "8abaa81d42029906be671f5d3dea2ac370fb9031978acbe1abf35d05bebab1a7",
}


def frozen_reader(name: str) -> ModuleType:
    """Load actual frozen source with unchanged shared models, not an old package."""
    path = _FIXTURES / name
    assert hashlib.sha256(path.read_bytes()).hexdigest() == _H_BYTES[name]
    spec = importlib.util.spec_from_file_location("muli_frozen_" + path.stem, path)
    assert spec is not None and spec.loader is not None
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    return reader


def test_h_format_reader_and_manifest_reject_new_source_contract(database: Path) -> None:
    old = frozen_reader("format_admission_v2_114.py")
    raw = (_FIXTURES / "compatibility_v2_114.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _H_BYTES["compatibility_v2_114.json"]
    manifest = CompatibilityManifest.from_json(raw)
    # Keep the historical ASR1 -> ASR2 assertion even as current advances to 3.
    asr = frozen_m_reader("format_admission_v2_118.py")
    current = CompatibilityManifest.from_json(frozen_m_bytes("compatibility_v2_118.json"))
    assert manifest.release == "2.114.0"
    assert manifest.formats.memory.reads == (1,) and manifest.formats.memory.writes == 1
    assert current.formats.memory.reads == (2,) and current.formats.memory.writes == 2
    assert old.CURRENT_FORMAT_STATE == PRE_TRANSCRIPT_MEMORY_FORMAT_STATE
    assert asr.CURRENT_FORMAT_STATE == PRE_WEBPAGE_MEMORY_FORMAT_STATE
    assert asr.CURRENT_FORMAT_STATE.formats.model_dump() == {
        **old.CURRENT_FORMAT_STATE.formats.model_dump(),
        "memory": 2,
    }
    before = snapshot(database)
    with closing(sqlite3.connect(database)) as db:
        evidence = read_optional_format_evidence(db)
        assert evidence is not None and evidence.state == CURRENT_FORMAT_STATE
        old.require_current_format(replace(evidence, state=PRE_TRANSCRIPT_MEMORY_FORMAT_STATE))
        asr.require_current_format(replace(evidence, state=PRE_WEBPAGE_MEMORY_FORMAT_STATE))
        with pytest.raises(old.FormatAdmissionError, match="store_format_unsupported"):
            old.require_current_format(replace(evidence, state=PRE_WEBPAGE_MEMORY_FORMAT_STATE))
        with pytest.raises(old.FormatAdmissionError, match="store_format_unsupported"):
            old.require_current_format(evidence)
        assert db.total_changes == 0
    for state in (PRE_TRANSCRIPT_MEMORY_FORMAT_STATE, PRE_WEBPAGE_MEMORY_FORMAT_STATE):
        assessment = assess_compatibility(manifest, current, state, handoff_protocol=2)
        assert not assessment.format_compatible and assessment.domain == "memory"
    assert snapshot(database) == before


def test_h_source_reader_accepts_legacy_markers_but_refuses_saved_asr_identity() -> None:
    old = frozen_reader("attachment_memory_source_v2_114.py")
    identifier, content = "a" * 32, "外部转写资料🙂\n只证明保存的这些字节。"
    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
    ordinary = attachment_source(identifier, content, checksum, 12)
    assert old.attachment_source(identifier, content, checksum, 12) == ordinary
    assert old.restore_attachment_source(identifier, content, ordinary["source"], 12) == ordinary
    document_checksum = hashlib.sha256(b"original-document").hexdigest()
    document = {
        "format": "docx",
        "scope": "docx_main_body_text",
        "source_revision": document_checksum,
        "revision": document_revision(document_checksum, content.encode("utf-8")),
    }
    retained_document = attachment_source(
        identifier, content, document_checksum, 12, document=document
    )
    assert (
        old.attachment_source(identifier, content, document_checksum, 12, document=document)
        == retained_document
    )
    assert (
        old.restore_attachment_source(identifier, content, retained_document["source"], 12)
        == retained_document
    )
    transcript = attachment_source(identifier, content, checksum, 12, transcript=True)
    assert restore_attachment_source(identifier, content, transcript["source"], 12) == transcript
    with pytest.raises(ValueError):
        old.restore_attachment_source(identifier, content, transcript["source"], 12)


@pytest.mark.parametrize("opener", ["ordinary", "existing", "runtime_store", "runtime"])
def test_h_tuple_is_refused_before_construction_writes_or_business_recovery(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "UPDATE runtime_state SET value=? WHERE name=?",
            (PRE_TRANSCRIPT_MEMORY_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
        )
        db.commit()
    before = snapshot(database)

    def forbidden_recovery(runtime: Runtime) -> None:
        raise AssertionError("The old H tuple must fail before business recovery")

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
        else:
            Runtime(ConfigFile(tmp_path / "config.json"), database.parent, no_model)
    assert mutations == [] and snapshot(database) == before
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")
