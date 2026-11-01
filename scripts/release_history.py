"""Validate the release history against CHANGELOG versions and the current tag."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\](?:\s*—\s*(\d{4}-\d{2}-\d{2}))?", re.M)
STATUSES = {"released", "tagged", "untagged", "unpublished"}
ReleaseStatus = Literal["released", "tagged", "untagged", "unpublished"]


class ReleaseHistoryError(ValueError):
    """A history file that cannot describe the actual release sequence."""


@dataclass(frozen=True, slots=True)
class ReleaseEntry:
    version: str
    status: ReleaseStatus
    tag: str | None
    legacy: bool = False

    @property
    def parts(self) -> tuple[int, int, int]:
        return version_parts(self.version)


def version_parts(value: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(value)
    if match is None:
        raise ReleaseHistoryError(f"版本号无效：{value!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def changelog_versions(text: str) -> list[str]:
    versions = [match.group(1) for match in HEADING.finditer(text)]
    if not versions:
        raise ReleaseHistoryError("CHANGELOG 没有版本标题")
    if len(set(versions)) != len(versions):
        raise ReleaseHistoryError("CHANGELOG 版本重复")
    parsed = [version_parts(version) for version in versions]
    if any(left <= right for left, right in zip(parsed, parsed[1:], strict=False)):
        raise ReleaseHistoryError("CHANGELOG 版本必须严格降序")
    return versions


def changelog_sections(text: str) -> dict[str, str]:
    """Return the description under each version heading."""
    matches = list(HEADING.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.end() : end].strip()
    return sections


def parse_history(value: object) -> tuple[list[ReleaseEntry], str]:
    if not isinstance(value, Mapping) or value.get("schema") != 1:
        raise ReleaseHistoryError("发布历史 schema 必须为 1")
    baseline = value.get("baseline", "2.0.0")
    if not isinstance(baseline, str):
        raise ReleaseHistoryError("发布历史 baseline 必须是版本号")
    version_parts(baseline)
    releases = value.get("releases")
    if not isinstance(releases, list) or not releases:
        raise ReleaseHistoryError("发布历史 releases 必须是非空列表")
    entries: list[ReleaseEntry] = []
    for item in releases:
        if not isinstance(item, Mapping):
            raise ReleaseHistoryError("发布历史条目必须是对象")
        version, status, tag = item.get("version"), item.get("status"), item.get("tag")
        legacy = item.get("legacy", False)
        if not isinstance(version, str):
            raise ReleaseHistoryError("发布历史条目缺少版本号")
        version_parts(version)
        if status not in STATUSES:
            raise ReleaseHistoryError(f"{version} 状态必须是 released/untagged/unpublished")
        if type(legacy) is not bool:
            raise ReleaseHistoryError(f"{version} legacy 必须是布尔值")
        if status in {"released", "tagged"}:
            if tag != f"v{version}":
                raise ReleaseHistoryError(f"{version} 已打 tag 条目必须带匹配 tag")
        elif tag is not None:
            raise ReleaseHistoryError(f"{version} 未发布条目不能带 tag")
        entries.append(ReleaseEntry(version, status, tag, legacy))
    if len({entry.version for entry in entries}) != len(entries):
        raise ReleaseHistoryError("发布历史版本重复")
    if any(left.parts <= right.parts for left, right in zip(entries, entries[1:], strict=False)):
        raise ReleaseHistoryError("发布历史版本必须严格降序")
    return entries, baseline


def _require_minor_continuity(entries: list[ReleaseEntry], baseline: tuple[int, int, int]) -> None:
    scoped = [entry.parts for entry in entries if entry.parts >= baseline]
    if not scoped:
        raise ReleaseHistoryError("发布历史没有 baseline 之后的版本")
    minors: dict[int, set[int]] = {}
    for major, minor, _patch in scoped:
        minors.setdefault(major, set()).add(minor)
    for major, present in minors.items():
        for minor in range(min(present), max(present) + 1):
            if minor not in present:
                raise ReleaseHistoryError(f"{major}.{minor}.0 未在发布历史中标记")


def validate(changelog: str, history: object, *, tag: str | None = None) -> dict[str, Any]:
    versions = changelog_versions(changelog)
    sections = changelog_sections(changelog)
    entries, baseline_text = parse_history(history)
    listed = {entry.version for entry in entries}
    missing = [version for version in versions if version not in listed]
    if missing:
        raise ReleaseHistoryError("CHANGELOG 版本未登记：" + ",".join(missing))
    changelog_set = set(versions)
    for entry in entries:
        if entry.version not in changelog_set and entry.status == "released":
            raise ReleaseHistoryError(f"{entry.version} 已发布但缺少 CHANGELOG 条目")
        if entry.status == "released" and not sections.get(entry.version):
            raise ReleaseHistoryError(f"{entry.version} 已发布但 CHANGELOG 说明为空")
    _require_minor_continuity(entries, version_parts(baseline_text))
    released = [entry for entry in entries if entry.status == "released"]
    if not released:
        raise ReleaseHistoryError("发布历史没有已发布版本")
    highest = released[0]
    if tag is not None and tag != highest.tag:
        raise ReleaseHistoryError(f"当前 tag {tag!r} 不是最高已发布版本 {highest.tag}")
    return {
        "releases": len(entries),
        "released": len(released),
        "tagged": sum(entry.status == "tagged" for entry in entries),
        "untagged": sum(entry.status == "untagged" for entry in entries),
        "unpublished": sum(entry.status == "unpublished" for entry in entries),
        "highest": highest.version,
        "changelog": len(versions),
        "baseline": baseline_text,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="校验 MuLi 发布历史", allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--changelog", default="CHANGELOG.md")
    parser.add_argument("--history", default="docs/release-history.json")
    parser.add_argument("--tag", help="当前发布 tag，例如 v2.152.0")
    args = parser.parse_args()
    root = args.root
    try:
        changelog = (root / args.changelog).read_text(encoding="utf-8")
        history = json.loads((root / args.history).read_text(encoding="utf-8"))
        result = validate(changelog, history, tag=args.tag)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"发布历史校验失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
