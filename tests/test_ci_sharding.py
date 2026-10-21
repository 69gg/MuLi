from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def sample_suite(tmp_path: Path) -> Path:
    (tmp_path / "conftest.py").write_text(
        Path(__file__).with_name("conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    source = (
        "import pytest\n"
        "@pytest.mark.parametrize('value', [1, 2, 3])\n"
        "def test_value(value: int) -> None:\n"
        "    assert value > 0\n"
    )
    for name in ("alpha", "middle", "zeta"):
        (tmp_path / f"test_{name}.py").write_text(source, encoding="utf-8")
    return tmp_path


def run_sample(directory: Path, shard: str | None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST"):
        environment.pop(name, None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-vv",
            "--color=no",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "--confcutdir",
            str(directory),
            "--basetemp",
            str(directory / f"temp-{shard or 'all'}"),
            *([f"--muli-ci-shard={shard}"] if shard is not None else []),
        ],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )


def passed_ids(result: subprocess.CompletedProcess[str]) -> set[str]:
    assert result.returncode == 0, result.stdout + result.stderr
    return set(re.findall(r"^(test_\S+) PASSED", result.stdout, re.MULTILINE))


def test_real_pytest_shards_cover_every_parameter_once_and_preserve_file_groups(
    sample_suite: Path,
) -> None:
    unsharded = run_sample(sample_suite, None)
    full = passed_ids(unsharded)
    assert re.findall(r"^(test_\S+) PASSED", unsharded.stdout, re.MULTILINE) == [
        f"test_{name}.py::test_value[{value}]"
        for name in ("alpha", "middle", "zeta")
        for value in (1, 2, 3)
    ]
    first = passed_ids(run_sample(sample_suite, "1"))
    second = passed_ids(run_sample(sample_suite, "2"))
    assert len(full) == 9 and first and second
    assert first | second == full
    assert not first & second
    assert not {item.split("::")[0] for item in first} & {item.split("::")[0] for item in second}


@pytest.mark.parametrize("shard", ["0", "3", "invalid"])
def test_invalid_shard_fails_before_running_tests(sample_suite: Path, shard: str) -> None:
    result = run_sample(sample_suite, shard)
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "--muli-ci-shard" in result.stderr and "PASSED" not in result.stdout


def test_empty_shard_is_an_error_instead_of_a_green_coverage_gap(sample_suite: Path) -> None:
    (sample_suite / "test_middle.py").unlink()
    (sample_suite / "test_zeta.py").unlink()
    result = run_sample(sample_suite, "2")
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "MuLi CI shard selected no tests" in result.stderr
    assert "PASSED" not in result.stdout
