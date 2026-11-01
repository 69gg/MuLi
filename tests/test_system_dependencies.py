"""Read-only install hints never execute a package manager."""

from __future__ import annotations

from collections.abc import Callable

import pytest

import muli.system_dependencies as dependencies


def fake_which(installed: set[str]) -> Callable[[str], str | None]:
    return lambda name: f"/usr/bin/{name}" if name in installed else None


def test_manager_preference_follows_platform_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies.shutil, "which", fake_which({"pacman", "apt", "dnf"}))
    assert dependencies.detect_manager("linux") == "pacman"


def test_manager_detection_is_absent_without_any_known_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dependencies.shutil, "which", fake_which(set()))
    assert dependencies.detect_manager("linux") is None


def test_install_hint_recommends_one_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies.shutil, "which", fake_which({"pacman", "sudo"}))
    posix = hasattr(dependencies.os, "geteuid")
    if posix:
        monkeypatch.setattr(dependencies.os, "geteuid", lambda: 1000)
    hint = dependencies.install_hint(["wf-recorder", "timeout"], platform="linux")
    assert hint is not None
    assert hint["manager"] == "pacman"
    assert hint["packages"] == ["wf-recorder"]
    assert hint["command"] == "pacman -S --needed --noconfirm wf-recorder"
    assert hint["elevated_command"] == (
        "sudo -n pacman -S --needed --noconfirm wf-recorder" if posix else None
    )
    assert hint["missing_tools"] == ["wf-recorder", "timeout"]


def test_elevation_is_omitted_without_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies.shutil, "which", fake_which({"pacman"}))
    hint = dependencies.install_hint(["wf-recorder"], platform="linux")
    assert hint is not None
    assert hint["command"] is not None
    assert hint["elevated_command"] is None


def test_present_tool_returns_no_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies.shutil, "which", fake_which({"ffmpeg"}))
    assert dependencies.install_hint(["ffmpeg"], platform="linux") is None


def test_unknown_tool_keeps_manager_but_has_no_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dependencies.shutil, "which", fake_which({"pacman"}))
    hint = dependencies.install_hint(["custom-tool"], platform="linux")
    assert hint is not None
    assert hint["manager"] == "pacman"
    assert hint["command"] is None
    assert hint["missing_tools"] == ["custom-tool"]


def test_no_manager_reports_manual_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies.shutil, "which", fake_which(set()))
    hint = dependencies.install_hint(["wf-recorder"], platform="linux")
    assert hint is not None
    assert hint["manager"] is None
    assert hint["command"] is None
    assert "手动安装" in hint["note"]


def test_root_does_not_get_an_elevation_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies.shutil, "which", fake_which({"pacman", "sudo"}))
    monkeypatch.setattr(dependencies.os, "geteuid", lambda: 0, raising=False)
    hint = dependencies.install_hint(["wf-recorder"], platform="linux")
    assert hint is not None
    assert hint["command"] is not None
    assert hint["elevated_command"] is None


def test_windows_manager_has_no_elevation_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies.shutil, "which", fake_which({"winget"}))
    hint = dependencies.install_hint(["ffmpeg"], platform="win32")
    assert hint is not None
    assert hint["manager"] == "winget"
    assert hint["command"] is not None and "Gyan.FFmpeg" in hint["command"]
    assert hint["elevated_command"] is None
