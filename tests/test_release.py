from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release.py"


def release_project(
    root: Path, *, wheel_name: str = "muli-agent", wheel_version: str = "2.0.0"
) -> None:
    (root / "src/muli").mkdir(parents=True)
    (root / "dist").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname="muli-agent"\nversion="2.0.0"\n', encoding="utf-8"
    )
    (root / "src/muli/__init__.py").write_text('__version__ = "2.0.0"\n', encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [2.0.0] — today\n\n已验证功能。\n\n## [1.0.0]\n旧版\n", encoding="utf-8"
    )
    with zipfile.ZipFile(root / "dist/muli_agent-2.0.0-py3-none-any.whl", "w") as archive:
        archive.writestr(
            "muli_agent-2.0.0.dist-info/METADATA", f"Name: {wheel_name}\nVersion: {wheel_version}\n"
        )
    metadata = b"Name: muli-agent\nVersion: 2.0.0\n"
    with tarfile.open(root / "dist/muli_agent-2.0.0.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("muli_agent-2.0.0/PKG-INFO")
        member.size = len(metadata)
        archive.addfile(member, io.BytesIO(metadata))


def test_release_checks_exact_tested_artifacts_and_extracts_only_current_notes(
    tmp_path: Path,
) -> None:
    release_project(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", "v2.0.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "release-notes.md").read_text(encoding="utf-8") == "已验证功能。\n"


@pytest.mark.parametrize(
    "tag,name,version",
    [
        ("v1.0.0", "muli-agent", "2.0.0"),
        ("v2.0.0", "another-project", "2.0.0"),
        ("v2.0.0", "muli-agent", "1.0.0"),
    ],
)
def test_release_rejects_wrong_tag_or_foreign_artifacts(
    tmp_path: Path, tag: str, name: str, version: str
) -> None:
    release_project(tmp_path, wheel_name=name, wheel_version=version)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", tag],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode != 0
    assert not (tmp_path / "release-notes.md").exists()
