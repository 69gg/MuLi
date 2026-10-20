from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_muli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default extension discovery and configuration must never read the host's MuLi home."""
    home = tmp_path / "muli-home"
    home.mkdir()
    monkeypatch.setenv("MULI_HOME", str(home))
    monkeypatch.delenv("MULI_CONFIG", raising=False)
    monkeypatch.delenv("MULI_DATA_DIR", raising=False)
