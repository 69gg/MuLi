from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from muli.compatibility import FormatState, FormatVersions
from muli.format_evidence import FORMAT_STATE_KEY, read_optional_format_evidence
from muli.store import Store

LEGACY = FormatState(
    metadata_version=1,
    formats=FormatVersions(
        database=1,
        objects=1,
        configuration=1,
        ingress=1,
        outbox=1,
        memory=1,
        install_profile=1,
    ),
)
TARGET = FormatState(
    metadata_version=1,
    formats=FormatVersions(
        database=1,
        objects=4,
        configuration=2,
        ingress=7,
        outbox=3,
        memory=5,
        install_profile=1,
    ),
)


def test_fresh_runtime_publishes_channel_ownership_contract(tmp_path: Path) -> None:
    store = Store.open_runtime(tmp_path / "muli.sqlite3", default_mode="auto")
    try:
        assert store.format_state == TARGET
    finally:
        store.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux exclusive fixed upgrade")
def test_cli_upgrades_complete_legacy_format_without_rewriting_business_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from muli.__main__ import main

    data_dir = tmp_path / "旧渠道数据"
    path = data_dir / "muli.sqlite3"
    store = Store.open_runtime(path, default_mode="auto")
    try:
        store.put("fixture", {"id": "kept", "text": "历史内容保留"})
        with store.db:
            store.db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (LEGACY.model_dump_json(), FORMAT_STATE_KEY),
            )
    finally:
        store.close()
    with closing(sqlite3.connect(path)) as db:
        before = db.execute("SELECT kind,id,data,updated FROM objects ORDER BY kind,id").fetchall()
    monkeypatch.setattr(
        sys, "argv", ["muli", "--data-dir", str(data_dir), "upgrade-channel-ownership"]
    )
    main()
    assert json.loads(capsys.readouterr().out) == {"upgraded": True}
    with closing(sqlite3.connect(path)) as db:
        evidence = read_optional_format_evidence(db)
        assert evidence is not None and evidence.state == TARGET
        assert (
            db.execute("SELECT kind,id,data,updated FROM objects ORDER BY kind,id").fetchall()
            == before
        )
