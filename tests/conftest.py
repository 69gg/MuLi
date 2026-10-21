from __future__ import annotations

from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--muli-ci-shard",
        type=int,
        choices=(1, 2),
        default=None,
        help="Run one of two disjoint file groups after normal pytest collection.",
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = config.getoption("--muli-ci-shard")
    if shard is None:
        return
    # Keep a file's parameterized cases and fixture lifetime together. Both jobs
    # collect normally, so pytest's own discovery and ignore rules remain authoritative.
    files = sorted({item.path for item in items})
    membership = {name: index % 2 + 1 for index, name in enumerate(files)}
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        target = selected if membership[item.path] == shard else deselected
        target.append(item)
    if not selected:
        raise pytest.UsageError("MuLi CI shard selected no tests")
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected


@pytest.fixture(autouse=True)
def isolated_muli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default extension discovery and configuration must never read the host's MuLi home."""
    home = tmp_path / "muli-home"
    home.mkdir()
    monkeypatch.setenv("MULI_HOME", str(home))
    monkeypatch.delenv("MULI_CONFIG", raising=False)
    monkeypatch.delenv("MULI_DATA_DIR", raising=False)
