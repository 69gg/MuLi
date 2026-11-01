"""Read-only install hints for required system tools; never runs a package manager.

The hints only describe what is missing and how the current platform's default
package manager would install it. Running the command stays with the caller,
under the normal permission mode and approval flow.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from typing import Any

# Argument vector for each supported package manager. Package names are appended.
_MANAGER_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "pacman": ("-S", "--needed", "--noconfirm"),
    "apt": ("install", "-y"),
    "dnf": ("install", "-y"),
    "zypper": ("install", "-y"),
    "apk": ("add",),
    "brew": ("install",),
    "winget": ("install", "--accept-source-agreements", "--accept-package-agreements"),
    "choco": ("install", "-y"),
    "scoop": ("install",),
}

# Preference order per platform; only an installed manager is reported.
_PLATFORM_MANAGERS: dict[str, tuple[str, ...]] = {
    "linux": ("pacman", "apt", "dnf", "zypper", "apk"),
    "darwin": ("brew",),
    "win32": ("winget", "choco", "scoop"),
}

# System-wide package managers that normally need root; brew/winget stay per-user.
_ELEVATED_MANAGERS = frozenset({"pacman", "apt", "dnf", "zypper", "apk"})

# Tool -> package name per manager. A tool absent here has no reliable package.
_TOOL_PACKAGES: dict[str, dict[str, str]] = {
    "wf-recorder": {
        "pacman": "wf-recorder",
        "apt": "wf-recorder",
        "dnf": "wf-recorder",
        "zypper": "wf-recorder",
        "apk": "wf-recorder",
    },
    "grim": {
        "pacman": "grim",
        "apt": "grim",
        "dnf": "grim",
        "zypper": "grim",
        "apk": "grim",
    },
    "gnome-screenshot": {
        "pacman": "gnome-screenshot",
        "apt": "gnome-screenshot",
        "dnf": "gnome-screenshot",
        "zypper": "gnome-screenshot",
        "apk": "gnome-screenshot",
    },
    "scrot": {
        "pacman": "scrot",
        "apt": "scrot",
        "dnf": "scrot",
        "zypper": "scrot",
        "apk": "scrot",
    },
    "ffmpeg": {
        "pacman": "ffmpeg",
        "apt": "ffmpeg",
        "dnf": "ffmpeg",
        "zypper": "ffmpeg",
        "apk": "ffmpeg",
        "brew": "ffmpeg",
        "winget": "Gyan.FFmpeg",
        "choco": "ffmpeg",
        "scoop": "ffmpeg",
    },
    "ffplay": {
        "pacman": "ffmpeg",
        "apt": "ffmpeg",
        "dnf": "ffmpeg",
        "zypper": "ffmpeg",
        "apk": "ffmpeg",
        "brew": "ffmpeg",
        "winget": "Gyan.FFmpeg",
        "choco": "ffmpeg",
        "scoop": "ffmpeg",
    },
    "notify-send": {
        "pacman": "libnotify",
        "apt": "libnotify-bin",
        "dnf": "libnotify",
        "zypper": "libnotify-tools",
        "apk": "libnotify",
    },
    "arecord": {
        "pacman": "alsa-utils",
        "apt": "alsa-utils",
        "dnf": "alsa-utils",
        "zypper": "alsa-utils",
        "apk": "alsa-utils",
    },
    "aplay": {
        "pacman": "alsa-utils",
        "apt": "alsa-utils",
        "dnf": "alsa-utils",
        "zypper": "alsa-utils",
        "apk": "alsa-utils",
    },
    "pw-play": {
        "pacman": "pipewire-audio",
        "apt": "pipewire-bin",
        "dnf": "pipewire-utils",
        "zypper": "pipewire-tools",
        "apk": "pipewire-tools",
    },
    "pdftotext": {
        "pacman": "poppler",
        "apt": "poppler-utils",
        "dnf": "poppler-utils",
        "zypper": "poppler-tools",
        "apk": "poppler-utils",
        "brew": "poppler",
    },
    "timeout": {
        "pacman": "coreutils",
        "apt": "coreutils",
        "dnf": "coreutils",
        "zypper": "coreutils",
        "apk": "coreutils",
    },
}


def detect_manager(platform: str | None = None) -> str | None:
    """Return the first available package manager for this platform."""
    target = platform or sys.platform
    for name in _PLATFORM_MANAGERS.get(target, ()):
        if shutil.which(name) is not None:
            return name
    return None


def missing_tools(tools: Sequence[str]) -> list[str]:
    """Return the requested tools that are not currently on PATH."""
    return [tool for tool in tools if shutil.which(tool) is None]


def install_hint(tools: Sequence[str], *, platform: str | None = None) -> dict[str, Any] | None:
    """Describe a non-executed install command, or ``None`` when nothing is missing.

    One package is recommended: callers re-check capability after installing and
    only ask for the next candidate if the feature is still unavailable.
    """
    missing = missing_tools(tools)
    if not missing:
        return None
    manager = detect_manager(platform)
    packages = _recommended_packages(missing, manager)
    if manager is None or not packages:
        return {
            "manager": manager,
            "packages": [],
            "command": None,
            "elevated_command": None,
            "missing_tools": missing,
            "note": "未检测到可用的系统包管理器；请手动安装后重新检查能力。"
            if manager is None
            else "该工具没有已知的包名；请手动安装后重新检查能力。",
        }
    command = shlex.join([manager, *_MANAGER_ARGUMENTS[manager], *packages])
    elevated = _elevated_command(command, manager)
    return {
        "manager": manager,
        "packages": packages,
        "command": command,
        "elevated_command": elevated,
        "missing_tools": missing,
        "note": "主助手可在主人请求下先尝试非交互提权；需要密码、无权限或安装失败时停止并告知用户。安装后重新检查能力。"
        if elevated
        else "安装后重新检查能力；失败、需要提权或没有权限时告知用户手动处理。",
    }


def _elevated_command(command: str, manager: str) -> str | None:
    """Only offer non-interactive elevation when sudo exists and root is not current."""
    if manager not in _ELEVATED_MANAGERS:
        return None
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and geteuid() == 0:
        return None
    if shutil.which("sudo") is None:
        return None
    return f"sudo -n {command}"


def _recommended_packages(missing: Sequence[str], manager: str | None) -> list[str]:
    if manager is None:
        return []
    for tool in missing:
        package = _TOOL_PACKAGES.get(tool, {}).get(manager)
        if package:
            return [package]
    return []
