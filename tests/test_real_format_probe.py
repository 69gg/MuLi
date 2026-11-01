from __future__ import annotations

from pathlib import Path

import pytest

from muli.real_format_probe import FormatProbeError, probe_format


def runtime_at(path: Path):
    from muli.config import ConfigFile
    from muli.runtime import Runtime

    config = ConfigFile(path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    return Runtime(config, path / "data")


async def test_probe_reads_identity_and_format_without_writing(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path)
    database = runtime.data_dir / "muli.sqlite3"
    try:
        before = database.stat().st_mtime_ns
        probe = probe_format(database)
        assert probe.store_instance == runtime.store.instance_id
        assert probe.main_session == runtime.main_session()["id"]
        assert probe.state == runtime.store.format_state
        probe.verify(
            path=database,
            store_instance=runtime.store.instance_id,
            main_session=runtime.main_session()["id"],
            state=runtime.store.format_state,
        )
        assert database.stat().st_mtime_ns == before
    finally:
        await runtime.close()


async def test_probe_rejects_missing_symlink_and_wrong_identity(tmp_path: Path) -> None:
    runtime = runtime_at(tmp_path)
    database = runtime.data_dir / "muli.sqlite3"
    try:
        with pytest.raises(FormatProbeError, match="format_probe_unavailable"):
            probe_format(tmp_path / "missing.sqlite3")
        link = tmp_path / "linked.sqlite3"
        link.symlink_to(database)
        with pytest.raises(FormatProbeError, match="format_probe_not_regular_file"):
            probe_format(link)
        probe = probe_format(database)
        with pytest.raises(FormatProbeError, match="format_probe_store_changed"):
            probe.verify(
                path=database,
                store_instance="other",
                main_session=runtime.main_session()["id"],
                state=runtime.store.format_state,
            )
    finally:
        await runtime.close()
