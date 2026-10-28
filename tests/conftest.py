from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--muli-ci-shard",
        type=int,
        choices=(1, 2, 3),
        default=None,
        help="Run one disjoint file group after normal pytest collection (one-based index).",
    )
    parser.addoption(
        "--muli-ci-shards",
        type=int,
        choices=(2, 3),
        default=2,
        help="Total file groups when --muli-ci-shard is enabled (default: 2).",
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = config.getoption("--muli-ci-shard")
    if shard is None:
        return
    shards = config.getoption("--muli-ci-shards")
    if not 1 <= shard <= shards:
        raise pytest.UsageError("--muli-ci-shard must be between 1 and --muli-ci-shards")
    # Keep a file's parameterized cases and fixture lifetime together. All jobs
    # collect normally, so pytest's own discovery and ignore rules remain authoritative.
    membership: dict[Path, int] = {}
    for path in {item.path for item in items}:
        try:
            name = path.relative_to(config.rootpath).as_posix()
        except ValueError as error:
            raise pytest.UsageError(
                "MuLi CI shards require test files inside pytest rootdir"
            ) from error
        # Relative POSIX names stay stable across runners and added test files.
        digest = hashlib.sha256(name.encode("utf-8")).digest()
        membership[path] = int.from_bytes(digest, "big") % shards + 1
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
