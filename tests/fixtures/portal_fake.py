"""A fixture-only Portal on an explicitly supplied private, non-activating bus."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def main() -> None:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    settings = json.loads(Path(sys.argv[1]).read_text())
    address = os.environ["MULI_TEST_PRIVATE_BUS"]
    assert address == os.environ["DBUS_SESSION_BUS_ADDRESS"]
    connection = Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )
    connection.set_exit_on_close(False)
    register_object = getattr(connection, "register_object_with_closures2", None)
    if register_object is None:
        register_object = connection.register_object
    desktop = "/org/freedesktop/portal/desktop"
    service = "org.freedesktop.portal.Desktop"
    request_interface = "org.freedesktop.portal.Request"
    loop = GLib.MainLoop()
    schema = Gio.DBusNodeInfo.new_for_xml("""<node>
      <interface name="org.freedesktop.portal.Screenshot">
        <method name="Screenshot"><arg type="s" direction="in"/>
          <arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
        <property name="version" type="u" access="read"/>
        <property name="AvailableTargets" type="u" access="read"/>
      </interface></node>""")
    request_schema = Gio.DBusNodeInfo.new_for_xml("""<node>
      <interface name="org.freedesktop.portal.Request">
        <method name="Close"/>
        <signal name="Response"><arg type="u"/><arg type="a{sv}"/></signal>
      </interface></node>""")

    def log(value: dict[str, Any]) -> None:
        with Path(settings["log"]).open("a") as output:
            output.write(json.dumps(value) + "\n")
        print(json.dumps(value), flush=True)

    def property_value(connection: Any, sender: str, path: str, interface: str, name: str) -> Any:
        return GLib.Variant("u", settings.get("version", 3) if name == "version" else 15)

    def method(
        connection: Any,
        sender: str,
        path: str,
        interface: str,
        name: str,
        parameters: Any,
        invocation: Any,
    ) -> None:
        parent, options = parameters.unpack()
        token = options["handle_token"]
        suffix = "legacy" if settings.get("legacy") else sender[1:].replace(".", "_")
        handle = desktop + "/request/" + suffix + "/" + token

        def close(
            connection: Any,
            sender: str,
            path: str,
            interface: str,
            name: str,
            parameters: Any,
            call: Any,
        ) -> None:
            log({"event": "close", "handle": path})

            def complete() -> bool:
                call.return_value(GLib.Variant("()", ()))
                log({"event": "closed", "handle": path})
                return False

            if settings.get("close_delay"):
                GLib.timeout_add(settings["close_delay"], complete)
            else:
                complete()

        def register() -> bool:
            register_object(handle, request_schema.interfaces[0], close, None, None)
            log({"event": "registered", "handle": handle})
            return False

        def missing_close(
            connection: Any,
            sender: str,
            path: str,
            interface: str,
            name: str,
            parameters: Any,
            call: Any,
        ) -> None:
            log({"event": "close_missing", "handle": path})
            call.return_dbus_error("org.freedesktop.DBus.Error.UnknownObject", "not registered")

            def activate() -> bool:
                connection.unregister_object(placeholder)
                register()
                return reply()

            GLib.idle_add(activate)

        if settings.get("register_after_close"):
            # Gate registration and the original method reply on the first failed
            # Close, rather than requiring the test parent to run before a timer.
            placeholder = register_object(
                handle, request_schema.interfaces[0], missing_close, None, None
            )
        else:
            register()
        log({"event": "screenshot", "parent": parent, "options": options, "handle": handle})

        if settings.get("spoof"):
            attacker = Gio.DBusConnection.new_for_address_sync(
                address,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None,
                None,
            )
            attacker.emit_signal(
                sender,
                handle,
                request_interface,
                "Response",
                GLib.Variant("(ua{sv})", (0, {"uri": GLib.Variant("s", "https://bad.invalid")})),
            )
            attacker.flush_sync(None)
            attacker.close_sync(None)
            connection.emit_signal(
                sender,
                desktop + "/request/unrelated/token",
                request_interface,
                "Response",
                GLib.Variant("(ua{sv})", (0, {"uri": GLib.Variant("s", "https://bad.invalid")})),
            )

        def respond() -> bool:
            connection.emit_signal(
                sender,
                handle,
                request_interface,
                "Response",
                GLib.Variant(
                    "(ua{sv})",
                    (
                        settings.get("response", 0),
                        {"uri": GLib.Variant("s", settings["uri"])},
                    ),
                ),
            )
            log({"event": "response", "handle": handle})
            return False

        def reply() -> bool:
            if settings.get("method_error"):
                invocation.return_dbus_error("org.muli.TestFailure", "fixture failure")
            else:
                invocation.return_value(GLib.Variant("(o)", (handle,)))
            if not settings.get("early") and not settings.get("never"):
                GLib.timeout_add(20, respond)
            return False

        if settings.get("release_owner"):
            connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "ReleaseName",
                GLib.Variant("(s)", (service,)),
                None,
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
        if settings.get("early"):
            respond()
        if not settings.get("register_after_close"):
            GLib.timeout_add(settings.get("reply_delay", 30), reply)

    register_object(desktop, schema.interfaces[0], method, property_value, None)
    result = connection.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "RequestName",
        GLib.Variant("(su)", (service, 0)),
        None,
        Gio.DBusCallFlags.NONE,
        1000,
        None,
    ).unpack()[0]
    assert result == 1
    log({"event": "ready"})
    GLib.timeout_add_seconds(15, lambda: (loop.quit(), False)[1])
    loop.run()


if __name__ == "__main__":
    main()
