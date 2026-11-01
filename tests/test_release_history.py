from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_history", ROOT / "scripts/release_history.py"
)
assert SPEC is not None and SPEC.loader is not None
history_script = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = history_script
SPEC.loader.exec_module(history_script)

CHANGELOG = (
    "## [2.2.0] — 2026-01-03\n\n- 二点二\n\n"
    "## [2.1.0] — 2026-01-02\n\n- 二点一\n\n"
    "## [2.0.0] — 2026-01-01\n\n- 二点零\n"
)


def history(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"schema": 1, "baseline": "2.0.0", "releases": list(entries)}


def entry(version: str, status: str, tag: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"version": version, "status": status, **({"tag": tag} if tag else {}), **extra}


def test_repository_history_matches_the_current_release() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    data = json.loads((ROOT / "docs/release-history.json").read_text(encoding="utf-8"))
    result = history_script.validate(changelog, data, tag=f"v{version}")
    assert result["highest"] == version
    assert result["released"] >= 1 and result["changelog"] >= 1


def test_valid_history_accepts_untagged_and_unpublished_gaps() -> None:
    data = history(
        entry("2.3.0", "released", "v2.3.0"),
        entry("2.2.0", "untagged"),
        entry("2.1.0", "unpublished"),
        entry("2.0.0", "released", "v2.0.0"),
    )
    changelog = CHANGELOG.replace("## [2.2.0]", "## [2.3.0]").replace(
        "## [2.1.0] — 2026-01-02\n\n- 二点一\n\n", ""
    )
    result = history_script.validate(changelog, data, tag="v2.3.0")
    assert result["released"] == 2 and result["unpublished"] == 1 and result["untagged"] == 1


def test_missing_changelog_entry_is_rejected() -> None:
    data = history(
        entry("2.2.0", "released", "v2.2.0"),
        entry("2.1.0", "untagged"),
    )
    with pytest.raises(history_script.ReleaseHistoryError, match="未登记"):
        history_script.validate(CHANGELOG, data)


def test_unmarked_gap_is_rejected() -> None:
    data = history(
        entry("2.2.0", "released", "v2.2.0"),
        entry("2.0.0", "released", "v2.0.0"),
    )
    changelog = "## [2.2.0]\n\n- 二点二\n\n## [2.0.0]\n\n- 二点零\n"
    with pytest.raises(history_script.ReleaseHistoryError, match="2.1.0"):
        history_script.validate(changelog, data)


def test_minor_gaps_are_rejected_for_every_major() -> None:
    data = history(
        entry("3.2.0", "released", "v3.2.0"),
        entry("3.0.0", "released", "v3.0.0"),
        entry("2.0.0", "released", "v2.0.0"),
    )
    changelog = "## [3.2.0]\n\n- 三\n\n## [3.0.0]\n\n- 三零\n\n## [2.0.0]\n\n- 二\n"
    with pytest.raises(history_script.ReleaseHistoryError, match="3.1.0"):
        history_script.validate(changelog, data)


def test_released_entries_require_a_changelog_description() -> None:
    data = history(
        entry("2.2.0", "released", "v2.2.0"),
        entry("2.1.0", "untagged"),
        entry("2.0.0", "released", "v2.0.0"),
    )
    changelog = "## [2.2.0]\n\n## [2.1.0]\n\n- 二点一\n\n## [2.0.0]\n\n- 二点零\n"
    with pytest.raises(history_script.ReleaseHistoryError, match="说明为空"):
        history_script.validate(changelog, data)


def test_tagged_entries_are_counted_separately_from_releases() -> None:
    data = history(
        entry("2.2.0", "tagged", "v2.2.0"),
        entry("2.1.0", "untagged"),
        entry("2.0.0", "released", "v2.0.0"),
    )
    result = history_script.validate(CHANGELOG, data)
    assert result["released"] == 1 and result["tagged"] == 1


def test_released_entries_require_matching_tags() -> None:
    data = history(
        entry("2.2.0", "released"),
        entry("2.1.0", "untagged"),
        entry("2.0.0", "released", "v2.0.0"),
    )
    with pytest.raises(history_script.ReleaseHistoryError, match="匹配 tag"):
        history_script.validate(CHANGELOG, data)
    with pytest.raises(history_script.ReleaseHistoryError, match="不能带 tag"):
        history_script.validate(
            CHANGELOG,
            history(
                entry("2.2.0", "released", "v2.2.0"),
                entry("2.1.0", "untagged", "v2.1.0"),
                entry("2.0.0", "released", "v2.0.0"),
            ),
        )


def test_current_tag_must_be_the_highest_released_version() -> None:
    data = history(
        entry("2.2.0", "released", "v2.2.0"),
        entry("2.1.0", "untagged"),
        entry("2.0.0", "released", "v2.0.0"),
    )
    with pytest.raises(history_script.ReleaseHistoryError, match="最高已发布"):
        history_script.validate(CHANGELOG, data, tag="v2.1.0")


def test_order_duplicates_and_schema_are_rejected() -> None:
    with pytest.raises(history_script.ReleaseHistoryError, match="严格降序"):
        history_script.validate(
            CHANGELOG,
            history(
                entry("2.1.0", "released", "v2.1.0"),
                entry("2.2.0", "released", "v2.2.0"),
                entry("2.0.0", "released", "v2.0.0"),
            ),
        )
    with pytest.raises(history_script.ReleaseHistoryError, match="重复"):
        history_script.validate(
            CHANGELOG,
            history(
                entry("2.2.0", "released", "v2.2.0"),
                entry("2.2.0", "untagged"),
                entry("2.1.0", "untagged"),
                entry("2.0.0", "released", "v2.0.0"),
            ),
        )
    with pytest.raises(history_script.ReleaseHistoryError, match="schema"):
        history_script.validate(CHANGELOG, {"schema": 2, "releases": []})
