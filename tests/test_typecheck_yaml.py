"""Keep the real YAML import policy consistent across incremental type checks."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]
B_OLD: bytes = (
    b"from __future__ import annotations\n\n"
    b"from typing import Literal, TypeAlias\n\nfrom a import to_text\n\n"
    b'Token: TypeAlias = Literal["old"]\n\n\n'
    b"def forward(value: Token) -> str:\n    return to_text(value)\n"
)


def yaml_imports() -> bytes:
    source: str = (ROOT / "src/muli/skills.py").read_text(encoding="utf-8")
    lines: list[str] = source.splitlines(keepends=True)
    imports: list[str] = []
    for node in ast.parse(source).body:
        if (isinstance(node, ast.Import) and any(alias.name == "yaml" for alias in node.names)) or (
            isinstance(node, ast.ImportFrom) and node.module in {"yaml.events", "yaml.nodes"}
        ):
            assert node.end_lineno is not None
            imports.append("".join(lines[node.lineno - 1 : node.end_lineno]))
    assert len(imports) == 3
    return "".join(imports).encode("utf-8")


def typecheck(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            "-m",
            "mypy",
            "--incremental",
            "--config-file",
            str(ROOT / "pyproject.toml"),
            "--cache-dir",
            str(directory / ".mypy_cache"),
            "a.py",
            "b.py",
        ),
        cwd=directory,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )


def test_yaml_policy_preserves_cold_warm_and_typed_errors(tmp_path: Path) -> None:
    a_source: bytes = (
        b"from __future__ import annotations\n\nfrom typing import TYPE_CHECKING\n\n"
        + yaml_imports()
        + b"\nif TYPE_CHECKING:\n    from b import Token\n\n\n"
        + b"def to_text(value: Token) -> str:\n    return value\n"
    )
    negative_source: bytes = a_source.replace(b"    return value\n", b"    return 7\n") + (
        b"\n\ntyped_business_value: int = 1  # type: ignore[assignment]\n"
    )
    positive: Path = tmp_path / "positive"
    negative: Path = tmp_path / "negative"
    for directory, source in ((positive, a_source), (negative, negative_source)):
        directory.mkdir()
        (directory / "a.py").write_bytes(source)
        (directory / "b.py").write_bytes(B_OLD)

    cold: subprocess.CompletedProcess[str] = typecheck(positive)
    # A different byte length invalidates mypy's second-resolution stat cache.
    (positive / "b.py").write_bytes(B_OLD.replace(b'Literal["old"]', b'Literal["new-public"]'))
    warm: subprocess.CompletedProcess[str] = typecheck(positive)
    control: subprocess.CompletedProcess[str] = typecheck(negative)

    for result in (cold, warm):
        assert result.returncode == 0, (result.stdout + result.stderr)[-10000:]
    output: str = control.stdout + control.stderr
    assert control.returncode == 1, output[-10000:]
    diagnostics: list[tuple[str, str]] = re.findall(
        r"^a\.py:(\d+): error: .*\s+\[([a-z-]+)\]$", output, flags=re.MULTILINE
    )
    return_line: int = next(
        index
        for index, line in enumerate(negative_source.splitlines(), 1)
        if line == b"    return 7"
    )
    assert sorted(diagnostics) == sorted(
        [
            (str(return_line), "return-value"),
            (str(len(negative_source.splitlines())), "unused-ignore"),
        ]
    ), output[-10000:]
