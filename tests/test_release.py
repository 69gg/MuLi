from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Literal

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release.py"
Artifact = Literal["source", "wheel", "sdist"]
PackageFile = Literal["compatibility.json", "__init__.py"]
RUNTIME = b'__version__ = "2.0.0"\n'
DECLARATION = json.dumps(
    {
        "metadata_version": 1,
        "distribution": "muli-agent",
        "release": "2.0.0",
        "handoff_protocol": 2,
        "requires_migration": False,
        "formats": {
            name: {"reads": [1], "writes": 1}
            for name in (
                "database",
                "objects",
                "configuration",
                "ingress",
                "outbox",
                "memory",
                "install_profile",
            )
        },
    }
).encode("utf-8")


def release_project(
    root: Path,
    *,
    wheel_name: str = "muli-agent",
    wheel_version: str = "2.0.0",
    declaration: bytes = DECLARATION,
    change: tuple[Artifact, PackageFile, bytes | None] | None = None,
    duplicate: Literal["wheel", "sdist"] | None = None,
) -> None:
    def files(artifact: Artifact) -> dict[str, bytes]:
        result = {"compatibility.json": declaration, "__init__.py": RUNTIME}
        if change is not None and change[0] == artifact:
            _, name, content = change
            if content is None:
                result.pop(name)
            else:
                result[name] = content
        return result

    (root / "src/muli").mkdir(parents=True)
    (root / "dist").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname="muli-agent"\nversion="2.0.0"\n', encoding="utf-8"
    )
    for name, content in files("source").items():
        (root / "src/muli" / name).write_bytes(content)
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [2.0.0] — today\n\n已验证功能。\n\n## [1.0.0]\n旧版\n", encoding="utf-8"
    )
    with zipfile.ZipFile(root / "dist/muli_agent-2.0.0-py3-none-any.whl", "w") as archive:
        archive.writestr(
            "muli_agent-2.0.0.dist-info/METADATA", f"Name: {wheel_name}\nVersion: {wheel_version}\n"
        )
        for name, content in files("wheel").items():
            archive.writestr("muli/" + name, content)
        if duplicate == "wheel":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("muli/compatibility.json", declaration)
    metadata = b"Name: muli-agent\nVersion: 2.0.0\n"
    with tarfile.open(root / "dist/muli_agent-2.0.0.tar.gz", "w:gz") as source_archive:
        member = tarfile.TarInfo("muli_agent-2.0.0/PKG-INFO")
        member.size = len(metadata)
        source_archive.addfile(member, io.BytesIO(metadata))
        entries = list(files("sdist").items())
        if duplicate == "sdist":
            entries.append(("compatibility.json", declaration))
        for name, content in entries:
            member = tarfile.TarInfo("muli_agent-2.0.0/src/muli/" + name)
            member.size = len(content)
            source_archive.addfile(member, io.BytesIO(content))


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
        timeout=5,
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
        timeout=5,
    )
    assert result.returncode != 0
    assert not (tmp_path / "release-notes.md").exists()


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "entry,content",
    [
        ("compatibility.json", None),
        ("compatibility.json", DECLARATION + b"\n"),
        ("compatibility.json", b" " * (16_384 + 1)),
        ("__init__.py", None),
        ("__init__.py", b'__version__ = "1.0.0"\n'),
    ],
    ids=["missing-declaration", "different-bytes", "oversized", "missing-runtime", "stale-runtime"],
)
def test_release_rejects_package_files_even_when_distribution_metadata_matches(
    tmp_path: Path, artifact: Artifact, entry: PackageFile, content: bytes | None
) -> None:
    release_project(tmp_path, change=(artifact, entry, content))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", "v2.0.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    assert result.returncode != 0
    assert not (tmp_path / "release-notes.md").exists()


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_release_rejects_duplicate_declaration_entries(
    tmp_path: Path, artifact: Literal["wheel", "sdist"]
) -> None:
    release_project(tmp_path, duplicate=artifact)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", "v2.0.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    assert result.returncode != 0
    assert not (tmp_path / "release-notes.md").exists()


def test_release_rejects_missing_source_declaration(tmp_path: Path) -> None:
    release_project(tmp_path, change=("source", "compatibility.json", None))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", "v2.0.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    assert result.returncode != 0
    assert not (tmp_path / "release-notes.md").exists()


@pytest.mark.parametrize(
    "declaration",
    [
        DECLARATION + b" " * (16_384 + 1 - len(DECLARATION)),
        DECLARATION.replace(b'"2.0.0"', b'"1.0.0"'),
    ],
    ids=["oversized-valid-json", "stale-release"],
)
def test_release_rejects_bad_declaration_even_when_all_three_copies_match(
    tmp_path: Path, declaration: bytes
) -> None:
    release_project(tmp_path, declaration=declaration)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", "v2.0.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    assert result.returncode != 0
    assert not (tmp_path / "release-notes.md").exists()


def test_release_accepts_declaration_at_the_exact_metadata_budget(tmp_path: Path) -> None:
    release_project(tmp_path, declaration=DECLARATION + b" " * (16_384 - len(DECLARATION)))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", "v2.0.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
