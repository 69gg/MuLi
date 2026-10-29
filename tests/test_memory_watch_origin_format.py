"""Signed P source readers define the memory3 -> memory4 admission boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from test_channel_format_upgrade import legacy_database, snapshot, upgrade
from test_fresh_format_admission import database as database
from test_fresh_format_admission import deny_construction_writes, no_model
from test_memory_watch_attention import components
from test_memory_watch_origin_policy import bound_match

from muli.compatibility import CompatibilityManifest, assess_compatibility
from muli.config import ConfigFile
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    PRE_MEMORY_WATCH_ORIGINS_FORMAT_STATE,
    FormatAdmissionError,
)
from muli.format_evidence import FORMAT_STATE_KEY, read_format_evidence
from muli.memory_watches import match_routing
from muli.proactive import FrozenRouting
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import Store
from muli.store_schema import ExistingStoreError

_FIXTURES = Path(__file__).parent / "fixtures"
# Expected bytes are from root's signed P source-freeze manifest, not a recreated reader.
_P_BYTES = {
    "format_admission_v2_120.py": "181da0926874af03ba2caf89e460a88b64c1a135d85ea3a07dacf9af7a37ac41",
    "compatibility_v2_120.json": "0184a7be0e1047d5c11c1a811af59e09dda21e62f321c1d93a5b4d62dcf2db24",
}


def frozen_p_bytes(name: str) -> bytes:
    raw = (_FIXTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _P_BYTES[name]
    return raw


def frozen_p_reader() -> ModuleType:
    frozen_p_bytes("format_admission_v2_120.py")
    path = _FIXTURES / "format_admission_v2_120.py"
    spec = importlib.util.spec_from_file_location("muli_frozen_format_admission_v2_120", path)
    assert spec is not None and spec.loader is not None
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    return reader


def test_signed_p_reader_and_manifest_refuse_memory4_without_writes(database: Path) -> None:
    old = frozen_p_reader()
    manifest = CompatibilityManifest.from_json(frozen_p_bytes("compatibility_v2_120.json"))
    current = CompatibilityManifest.from_json(
        (Path(__file__).parents[1] / "src" / "muli" / "compatibility.json").read_bytes()
    )
    assert manifest.release == "2.120.0"
    assert manifest.formats.memory.reads == (3,) and manifest.formats.memory.writes == 3
    assert current.formats.memory.reads == (4,) and current.formats.memory.writes == 4
    assert old.CURRENT_FORMAT_STATE == PRE_MEMORY_WATCH_ORIGINS_FORMAT_STATE
    assert CURRENT_FORMAT_STATE.formats.model_dump() == {
        **old.CURRENT_FORMAT_STATE.formats.model_dump(),
        "memory": 4,
    }
    before = snapshot(database)
    with closing(sqlite3.connect(database)) as db:
        evidence = read_format_evidence(db)
        old.require_current_format(replace(evidence, state=PRE_MEMORY_WATCH_ORIGINS_FORMAT_STATE))
        with pytest.raises(old.FormatAdmissionError, match="store_format_unsupported"):
            old.require_current_format(evidence)
        assert db.total_changes == 0
    assessment = assess_compatibility(manifest, current, CURRENT_FORMAT_STATE, handoff_protocol=2)
    assert not assessment.format_compatible and assessment.domain == "memory"
    assert snapshot(database) == before


@pytest.mark.parametrize("opener", ["ordinary", "existing", "runtime_store", "runtime", "server"])
def test_memory3_is_rejected_before_recovery_or_construction_writes(
    database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opener: str,
) -> None:
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "UPDATE runtime_state SET value=? WHERE name=?",
            (PRE_MEMORY_WATCH_ORIGINS_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
        )
        db.commit()
    before = snapshot(database)

    def forbidden_recovery(runtime: Runtime) -> None:
        raise AssertionError("memory3 must fail before workers or business recovery")

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


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX stopped upgrade")
@pytest.mark.parametrize("platform", ["linux", "darwin"])
async def test_exact_memory3_upgrade_preserves_old_watch_proof_and_all_business_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    if platform == "linux" and sys.platform != "linux":
        pytest.skip("Linux lock implementation is exercised on Linux")
    rig = components(tmp_path)
    path = rig.runtime.data_dir / "muli.sqlite3"
    try:
        proof, _, _ = await bound_match(rig)
        saved = rig.runtime.store.get("memory_watch", proof.watch_id)
        assert saved is not None
        # This is the exact old v1 record shape: its real private ingress must
        # never become an origin preference just because the new reader exists.
        old_watch = {key: value for key, value in saved.items() if key != "routing"}
        old_watch["version"] = 1
        rig.runtime.store.put("memory_watch", old_watch)
        assert match_routing(rig.runtime.store, proof) == FrozenRouting()
        source = rig.runtime.memory.source(proof.source_id)
        assert source is not None
        job = rig.pipeline.job(proof.source_id)
        assert job is not None and job["state"] == "done" and job["checkpoint"]
        with rig.runtime.store.db:
            rig.runtime.store.db.execute(
                "INSERT INTO channel_outbox(id,name,route,payload,state,next_attempt,created_at,updated_at) "
                "VALUES('unknown-memory4-upgrade','onebot','{}','{}','unknown',0,1,1)"
            )
        identity = read_format_evidence(rig.runtime.store.db)
    finally:
        await rig.close()
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "UPDATE runtime_state SET value=? WHERE name=?",
            (PRE_MEMORY_WATCH_ORIGINS_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
        )
        db.commit()
    before = snapshot(path, omit_marker=True)
    # On Linux the Darwin parameter is branch fault injection, not native evidence.
    if platform == "darwin" and sys.platform != "darwin":
        monkeypatch.setattr(sys, "platform", "darwin")
    assert upgrade(path.parent)
    assert snapshot(path, omit_marker=True) == before
    assert not upgrade(path.parent)
    reopened = Store.open_runtime(path, default_mode="auto")
    try:
        current = read_format_evidence(reopened.db)
        assert current.state == CURRENT_FORMAT_STATE
        assert (current.store_instance, current.main_session) == (
            identity.store_instance,
            identity.main_session,
        )
        assert reopened.get("memory_watch", proof.watch_id) == old_watch
        assert match_routing(reopened, proof) == FrozenRouting()
        assert reopened.db.execute("SELECT state FROM channel_outbox").fetchone()[0] == "unknown"
    finally:
        reopened.close()
    assert snapshot(path, omit_marker=True) == before


@pytest.mark.parametrize("platform", ["win32", "unsupported"])
def test_memory3_upgrade_does_not_expand_unsupported_platforms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    path = legacy_database(tmp_path / "data", state=PRE_MEMORY_WATCH_ORIGINS_FORMAT_STATE)
    before = snapshot(path)
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(FormatAdmissionError, match="store_format_upgrade_platform_unsupported"):
        upgrade(path.parent)
    assert snapshot(path) == before
