"""Validate a release without importing application code or installing build tools."""

from __future__ import annotations

import argparse
import ast
import email.parser
import json
import re
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import IO

_METADATA_BYTES = 16_384


def read_metadata(stream: IO[bytes]) -> bytes:
    data = stream.read(_METADATA_BYTES + 1)
    if len(data) > _METADATA_BYTES:
        raise ValueError("发行声明或版本文件超过 16 KiB")
    return data


def wheel_file(archive: zipfile.ZipFile, name: str) -> bytes:
    entries = [item for item in archive.infolist() if item.filename == name]
    if len(entries) != 1 or entries[0].is_dir():
        raise ValueError("wheel 声明或版本文件缺失或不唯一")
    with archive.open(entries[0]) as stream:
        return read_metadata(stream)


def source_file(archive: tarfile.TarFile, name: str) -> bytes:
    entries = [item for item in archive.getmembers() if item.name == name]
    if len(entries) != 1 or not entries[0].isfile():
        raise ValueError("sdist 声明或版本文件缺失或不唯一")
    stream = archive.extractfile(entries[0])
    if stream is None:
        raise ValueError("sdist 声明或版本文件不可读取")
    with stream:
        return read_metadata(stream)


def validate(root: Path, tag: str, dist: Path) -> str:
    configuration = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = configuration["project"]
    build_system = configuration.get("build-system", {})
    requirements = build_system.get("requires", [])
    backend_pin = (
        re.fullmatch(r"hatchling==([0-9]+(?:\.[0-9]+)*)", requirements[0])
        if isinstance(requirements, list)
        and len(requirements) == 1
        and isinstance(requirements[0], str)
        else None
    )
    if build_system.get("build-backend") != "hatchling.build" or backend_pin is None:
        raise ValueError("发行构建必须声明唯一的 hatchling 精确版本")
    expected_generator = f"hatchling {backend_pin.group(1)}"
    version = project["version"]
    if tag != f"v{version}" or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("发布标签必须与正式版本元数据一致")
    with (root / "src/muli/__init__.py").open("rb") as stream:
        runtime_bytes = read_metadata(stream)
    tree = ast.parse(runtime_bytes.decode("utf-8"))
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
    with (root / "src/muli/compatibility.json").open("rb") as stream:
        declaration = read_metadata(stream)
    value = json.loads(declaration)
    if not isinstance(value, dict) or value.get("release") != version:
        raise ValueError("兼容声明与发行版本不一致")
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
        wheel_metadata = email.parser.Parser().parsestr(
            wheel_file(archive, metadata_names[0].removesuffix("METADATA") + "WHEEL").decode(
                "utf-8"
            )
        )
        if wheel_metadata.get_all("Generator") != [expected_generator]:
            raise ValueError("wheel Generator 与构建后端声明不一致或不唯一")
        if (
            wheel_file(archive, "muli/compatibility.json") != declaration
            or wheel_file(archive, "muli/__init__.py") != runtime_bytes
        ):
            raise ValueError("wheel 声明或版本文件与源码不一致")
    with tarfile.open(sources[0], "r:gz") as archive:
        candidates = [
            member
            for member in archive.getmembers()
            if member.name.count("/") == 1 and member.name.endswith("/PKG-INFO")
        ]
        if len(candidates) != 1:
            raise ValueError("sdist 元数据不唯一")
        metadata_stream = archive.extractfile(candidates[0])
        if metadata_stream is None:
            raise ValueError("sdist 元数据不可读取")
        with metadata_stream:
            source_metadata = metadata_stream.read().decode("utf-8")
        prefix = candidates[0].name.split("/", 1)[0]
        if (
            source_file(archive, prefix + "/src/muli/compatibility.json") != declaration
            or source_file(archive, prefix + "/src/muli/__init__.py") != runtime_bytes
        ):
            raise ValueError("sdist 声明或版本文件与源码不一致")
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
    print(
        f"Validated {args.tag}: source version, compatibility declaration, changelog, "
        "wheel and sdist agree."
    )


if __name__ == "__main__":
    main()
