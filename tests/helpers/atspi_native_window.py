"""An explicitly opted-in hosted CI process owns this disposable GTK window."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

BUTTON_NAME = "Owned AT-SPI button"
ENTRY_NAME = "Owned AT-SPI text"
PASSWORD_NAME = "Owned AT-SPI password"
PASSWORD_TEXT = "fixture-only-password"
UNICODE_TEXT = '中文🌊 "quote" \\ slash'
WINDOW_SECONDS = 30
EVENT_BYTES = 4096
_TITLE = re.compile(r"muli-atspi-[0-9a-f]{32}\Z")


def require_fixture_environment(environment: Mapping[str, str], platform: str) -> None:
    if (
        platform != "linux"
        or environment.get("GITHUB_ACTIONS") != "true"
        or environment.get("RUNNER_OS") != "Linux"
        or environment.get("MULI_ATSPI_NATIVE") != "1"
        or not environment.get("DBUS_SESSION_BUS_ADDRESS")
        or not environment.get("DISPLAY")
    ):
        raise ValueError("owned_atspi_fixture_not_enabled")


def window_arguments(python: str, title: str) -> tuple[str, ...]:
    if not Path(python).is_absolute() or not _TITLE.fullmatch(title):
        raise ValueError("invalid_owned_atspi_window_arguments")
    return (python, "-I", "-B", str(Path(__file__).absolute()), title)


def event_bytes(value: dict[str, Any]) -> bytes:
    data = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    if len(data) > EVENT_BYTES:
        raise ValueError("owned_atspi_event_too_large")
    return data


def _run_window(gtk: Any, glib: Any, title: str) -> None:
    window = gtk.Window(title=title)
    window.set_default_size(360, 180)
    window.get_accessible().set_name(title)
    box = gtk.Box(orientation=gtk.Orientation.VERTICAL, spacing=8)
    window.add(box)
    button = gtk.Button(label=BUTTON_NAME)
    button.get_accessible().set_name(BUTTON_NAME)
    text = gtk.Entry()
    text.get_accessible().set_name(ENTRY_NAME)
    password = gtk.Entry()
    password.set_visibility(False)
    password.set_text(PASSWORD_TEXT)
    password.get_accessible().set_name(PASSWORD_NAME)
    for widget in (button, text, password):
        box.pack_start(widget, True, True, 0)

    clicks = changes = 0
    ready = failed = False

    def emit(kind: str) -> None:
        nonlocal failed
        try:
            data = event_bytes(
                {
                    "kind": kind,
                    "pid": os.getpid(),
                    "title": title,
                    "clicks": clicks,
                    "changes": changes,
                    "text": text.get_text(),
                    "password_unchanged": password.get_text() == PASSWORD_TEXT,
                }
            )
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except Exception:
            failed = True
            gtk.main_quit()

    def clicked(_button: Any) -> None:
        nonlocal clicks, failed
        clicks += 1
        if clicks > 4:
            failed = True
            gtk.main_quit()
            return
        emit("button")

    def changed(_entry: Any) -> None:
        nonlocal changes, failed
        changes += 1
        if changes > 4:
            failed = True
            gtk.main_quit()
            return
        emit("text")

    def mapped() -> bool:
        nonlocal ready, failed
        if ready or not window.get_mapped():
            failed = True
            gtk.main_quit()
            return False
        ready = True
        emit("ready")
        return False

    def expired() -> bool:
        nonlocal failed
        failed = True
        gtk.main_quit()
        return False

    button.connect("clicked", clicked)
    text.connect("changed", changed)
    window.connect("destroy", lambda _window: gtk.main_quit())
    glib.timeout_add_seconds(WINDOW_SECONDS, expired)
    window.show_all()
    glib.idle_add(mapped)
    try:
        gtk.main()
    finally:
        window.destroy()
    if failed or not ready:
        raise ValueError("owned_atspi_window_failed")


def main(arguments: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if arguments is None else arguments)
    try:
        require_fixture_environment(os.environ, sys.platform)
        if len(values) != 1 or not _TITLE.fullmatch(values[0]):
            raise ValueError("invalid_owned_atspi_window_arguments")
        # Imports happen only after the explicit CI/private-session gate. Pure
        # contract tests can import this file without loading native libraries.
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk

        _run_window(Gtk, GLib, values[0])
    except Exception:
        print("owned_atspi_window_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
