from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import conftest as ci_sharding
import pytest


@dataclass(frozen=True)
class CollectedItem:
    path: Path
    nodeid: str


@dataclass
class DeselectionHook:
    calls: list[list[pytest.Item]] = field(default_factory=list)

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        self.calls.append(list(items))


@dataclass
class CollectionConfig:
    rootpath: Path
    option: argparse.Namespace
    hook: DeselectionHook = field(default_factory=DeselectionHook)

    def getoption(self, name: str) -> int | None:
        return cast(int | None, getattr(self.option, name.removeprefix("--").replace("-", "_")))


def options(*arguments: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    ci_sharding.pytest_addoption(
        cast(pytest.Parser, SimpleNamespace(addoption=parser.add_argument))
    )
    return parser.parse_args(list(arguments))


def sample_items(root: Path) -> list[CollectedItem]:
    names = [f"tests/test_case_{index:02d}.py" for index in range(24)]
    names.extend(["tests/嵌套/test_原文.py", "tests/other/test_case_00.py"])
    return [
        CollectedItem(root / name, f"{name}::{test}")
        for name in names
        for test in ("test_value[one]", "test_value[two]", "test_shared_fixture")
    ]


def select(config: CollectionConfig, items: list[CollectedItem]) -> list[CollectedItem]:
    selected = list(items)
    ci_sharding.pytest_collection_modifyitems(
        cast(pytest.Config, config), cast(list[pytest.Item], selected)
    )
    return selected


def assignment(root: Path, items: list[CollectedItem], shards: int) -> dict[str, int]:
    groups: dict[str, int] = {}
    for index in range(1, shards + 1):
        config = CollectionConfig(
            root, options(f"--muli-ci-shard={index}", f"--muli-ci-shards={shards}")
        )
        for item in select(config, items):
            assert item.nodeid not in groups
            groups[item.nodeid] = index
    return groups


@pytest.mark.parametrize("shards", [2, 3])
def test_groups_cover_the_collected_items_once_without_splitting_files(
    tmp_path: Path, shards: int
) -> None:
    items = sample_items(tmp_path)
    groups: list[set[str]] = []
    file_groups: dict[Path, set[int]] = {}
    for index in range(1, shards + 1):
        config = CollectionConfig(
            tmp_path, options(f"--muli-ci-shard={index}", f"--muli-ci-shards={shards}")
        )
        selected = select(config, items)
        ids = {item.nodeid for item in selected}
        assert ids and len(ids) == len(selected)
        assert selected == [item for item in items if item.nodeid in ids]
        assert config.hook.calls == [[item for item in items if item.nodeid not in ids]]
        for item in selected:
            file_groups.setdefault(item.path, set()).add(index)
        groups.append(ids)
    assert set.union(*groups) == {item.nodeid for item in items}
    assert sum(map(len, groups)) == len(items)
    assert all(len(indices) == 1 for indices in file_groups.values())


@pytest.mark.parametrize("shards", [2, 3])
def test_adding_a_file_does_not_move_existing_cases(tmp_path: Path, shards: int) -> None:
    items = sample_items(tmp_path)
    before = assignment(tmp_path, items, shards)
    added = CollectedItem(tmp_path / "tests/test_000_new.py", "tests/test_000_new.py::test_new")
    after = assignment(tmp_path, [added, *items], shards)
    assert {name: after[name] for name in before} == before
    assert set(after) == {*before, added.nodeid}


@pytest.mark.parametrize("shards", [2, 3])
def test_checkout_location_does_not_change_groups(tmp_path: Path, shards: int) -> None:
    first, second = tmp_path / "first-checkout", tmp_path / "different-checkout"
    assert assignment(first, sample_items(first), shards) == assignment(
        second, sample_items(second), shards
    )


@pytest.mark.parametrize("arguments", [(), ("--muli-ci-shards=3",)])
def test_without_a_shard_collection_and_hooks_are_untouched(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    items = sample_items(tmp_path)
    config = CollectionConfig(tmp_path, options(*arguments))
    selected = select(config, items)
    assert config.option.muli_ci_shard is None
    assert [id(item) for item in selected] == [id(item) for item in items]
    assert config.hook.calls == []


def test_default_group_count_matches_explicit_two(tmp_path: Path) -> None:
    items = sample_items(tmp_path)
    implicit = CollectionConfig(tmp_path, options("--muli-ci-shard=1"))
    explicit = CollectionConfig(tmp_path, options("--muli-ci-shard=1", "--muli-ci-shards=2"))
    assert implicit.option.muli_ci_shards == 2
    assert select(implicit, items) == select(explicit, items)


@pytest.mark.parametrize(
    "argument",
    [
        "--muli-ci-shard=0",
        "--muli-ci-shard=-1",
        "--muli-ci-shard=4",
        "--muli-ci-shard=invalid",
        "--muli-ci-shards=1",
        "--muli-ci-shards=4",
        "--muli-ci-shards=invalid",
    ],
)
def test_invalid_options_are_rejected(argument: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        options(argument)
    assert error.value.code == 2  # The receiving argparse parser rejects the option.
    assert argument.split("=", 1)[0] in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments", [("--muli-ci-shard=3",), ("--muli-ci-shard=3", "--muli-ci-shards=2")]
)
def test_index_outside_the_total_is_rejected_before_selection(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    items = sample_items(tmp_path)
    config = CollectionConfig(tmp_path, options(*arguments))
    with pytest.raises(pytest.UsageError, match="--muli-ci-shard must be between"):
        select(config, items)
    assert config.hook.calls == []


@pytest.mark.parametrize("shards", [2, 3])
def test_empty_groups_are_errors_instead_of_green_coverage_gaps(
    tmp_path: Path, shards: int
) -> None:
    only = [CollectedItem(tmp_path / "tests/test_only.py", "tests/test_only.py::test_value")]
    successes = 0
    failures = 0
    for index in range(1, shards + 1):
        config = CollectionConfig(
            tmp_path, options(f"--muli-ci-shard={index}", f"--muli-ci-shards={shards}")
        )
        try:
            selected = select(config, only)
        except pytest.UsageError as error:
            assert str(error) == "MuLi CI shard selected no tests"
            assert config.hook.calls == []
            failures += 1
        else:
            assert selected == only and config.hook.calls == [[]]
            successes += 1
    assert successes == 1 and failures == shards - 1


def test_files_outside_the_root_cannot_use_runner_specific_absolute_names(tmp_path: Path) -> None:
    config = CollectionConfig(tmp_path / "checkout", options("--muli-ci-shard=1"))
    outside = [CollectedItem(tmp_path / "outside.py", "outside.py::test_value")]
    with pytest.raises(pytest.UsageError, match="inside pytest rootdir"):
        select(config, outside)
    assert config.hook.calls == []
