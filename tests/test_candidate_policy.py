from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from muli.candidate_policy import (
    CandidateError,
    CandidateLimits,
    CandidateSettings,
    ExpectedPolicy,
    ProcessOutput,
    ProcessSpec,
)


def test_default_policy_does_not_enable_or_select_a_directory() -> None:
    settings = CandidateSettings()
    assert not settings.enabled
    assert (settings.wheelhouse, settings.retained_root, settings.uv_executable) == ("", "", "")
    assert settings.disk_mode == "threshold"


def test_configured_policy_keeps_paths_for_explicit_management(tmp_path: Path) -> None:
    settings = CandidateSettings(
        enabled=True,
        wheelhouse=str(tmp_path / "private-material"),
        retained_root=str(tmp_path / "retained"),
        uv_executable=str(tmp_path / "uv"),
    )
    assert CandidateSettings.model_validate_json(settings.model_dump_json()) == settings
    assert settings.model_dump()["wheelhouse"] == str(tmp_path / "private-material")
    assert "private-material" not in repr(settings)
    expected = ExpectedPolicy(
        source=settings.source,
        limits=settings.limits,
        disk_mode=settings.disk_mode,
        revision="a" * 64,
    )
    assert "wheelhouse" not in expected.model_dump()


@pytest.mark.parametrize("path", ["relative", "/one/../two", "/one\nsecret"])
def test_policy_paths_are_explicit(path: str) -> None:
    with pytest.raises(ValidationError):
        CandidateSettings(wheelhouse=path)


def test_enabling_requires_all_paths() -> None:
    with pytest.raises(ValidationError):
        CandidateSettings(enabled=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"seconds": 181.0},
        {"memory_bytes": 1024**3 + 1},
        {"cpu_percent": 101},
        {"tasks": 129},
        {"preflight_seconds": 30.0, "seconds": 29.0},
        {"wheel_bytes": 2048, "compressed_bytes": 1024},
    ],
)
def test_resource_contract_cannot_silently_expand(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CandidateLimits.model_validate(changes)


def test_process_contract_reuses_the_exact_limits_and_hides_private_output(tmp_path: Path) -> None:
    limits = CandidateLimits()
    spec = ProcessSpec(
        argv=(str(tmp_path / "uv"), "--version"),
        env=(("NETRC", str(tmp_path / "empty-netrc")),),
        cwd=tmp_path,
        private_root=tmp_path,
        deadline=123.0,
        limits=limits,
        disk_mode="threshold",
        observe=lambda: None,
    )
    assert spec.limits is limits
    assert str(tmp_path) not in repr(spec)
    output = ProcessOutput(1, "private-requirement", "private-path")
    assert repr(output) == "ProcessOutput(returncode=1)"
    assert str(CandidateError("candidate_failed")) == "candidate_failed"
