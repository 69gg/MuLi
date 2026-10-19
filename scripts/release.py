"""Validate a release without importing application code or installing build tools."""

from __future__ import annotations

import argparse
import ast
import email.parser
import re
import tarfile
import tomllib
import zipfile
from pathlib import Path


def validate(root: Path, tag: str, dist: Path) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    if tag != f"v{version}" or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("发布标签必须与正式版本元数据一致")
    tree = ast.parse((root / "src/muli/__init__.py").read_text(encoding="utf-8"))
    versions = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    ]
    if versions != [version]:
        raise ValueError("运行时版本与发行版本不一致")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)", changelog, re.M | re.S
    )
    if not match or not match.group(1).strip():
        raise ValueError("CHANGELOG 缺少该正式版本的发布说明")
    wheels, sources = list(dist.glob("*.whl")), list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("发布目录必须恰好包含一个 wheel 和一个 sdist")
    with zipfile.ZipFile(wheels[0]) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError("wheel 元数据不唯一")
        metadata = archive.read(metadata_names[0]).decode("utf-8")
    with tarfile.open(sources[0], "r:gz") as archive:
        candidates = [
            member
            for member in archive.getmembers()
            if member.name.count("/") == 1 and member.name.endswith("/PKG-INFO")
        ]
        if len(candidates) != 1:
            raise ValueError("sdist 元数据不唯一")
        stream = archive.extractfile(candidates[0])
        if stream is None:
            raise ValueError("sdist 元数据不可读取")
        source_metadata = stream.read().decode("utf-8")
    for content in (metadata, source_metadata):
        parsed = email.parser.Parser().parsestr(content)
        if parsed["Version"] != version or parsed["Name"] != project["name"]:
            raise ValueError("测试产物的发行名称或版本不匹配")
    return match.group(1).strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    notes = validate(Path.cwd(), args.tag, args.dist)
    Path("release-notes.md").write_text(notes, encoding="utf-8")
    print(f"Validated {args.tag}: source version, changelog, wheel and sdist agree.")


if __name__ == "__main__":
    main()
