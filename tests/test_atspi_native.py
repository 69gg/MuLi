"""Real AT-SPI journeys only in the hosted CI's explicitly private GTK session."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from helpers import atspi_native_window as fixture
from helpers.atspi_native_session import NativeWindow, owned_window

from muli.config import Settings
from muli.tools.computer import Computer, ComputerError

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("MULI_ATSPI_NATIVE") != "1",
    reason="Native AT-SPI requires the explicitly enabled hosted private session",
)


def settings() -> Settings:
    return Settings.model_validate(
        {"computer": {"enabled": True, "backend": "auto", "python_path": "/usr/bin/python3"}}
    )


@pytest_asyncio.fixture
async def native_window() -> AsyncIterator[NativeWindow]:
    fixture.require_fixture_environment(os.environ, sys.platform)
    async with owned_window(settings().computer.python_path, "muli-atspi-" + uuid4().hex) as window:
        yield window


async def discover(computer: Computer, window: NativeWindow) -> str:
    deadline = asyncio.get_running_loop().time() + computer.config.operation_timeout_seconds
    async with asyncio.timeout_at(deadline):
        while True:
            applications = await computer.targets({})
            own = [
                row
                for row in applications["targets"]
                if computer._targets[row["target_id"]].node["pid"] == window.process.pid
            ]
            if own:
                assert len(own) == 1, "The native fixture must have one exact process identity"
                break
            # Registration is asynchronous; only discovery is retried, never effects.
            await asyncio.sleep(0.05)
        app = computer._targets[own[0]["target_id"]].node
        windows = await computer.targets({"application_id": own[0]["application_id"]})
        matches = [
            row
            for row in windows["targets"]
            if row["kind"] == "window" and row["name"] == window.title
        ]
        assert len(matches) == 1, "The exact GTK window must be discoverable"
        identifier = str(matches[0]["target_id"])
        cached = computer._targets[identifier].node
        assert cached["pid"] == window.process.pid
        assert cached["identity"]["bus_name"] == app["identity"]["bus_name"]
        assert cached["identity"]["object_path"] != app["identity"]["object_path"]
        return identifier


async def observe(computer: Computer, target: str, name: str) -> dict[str, Any]:
    snapshot = await computer.snapshot({"target_id": target})
    assert snapshot["complete"] is True and snapshot["truncated"] is False
    nodes = [node for node in snapshot["nodes"] if node["name"] == name]
    assert len(nodes) == 1, "The requested owned control must occur exactly once"
    return {"snapshot_id": snapshot["snapshot_id"], "node": nodes[0]}


async def test_real_linux_button_text_password_and_release(native_window: NativeWindow) -> None:
    computer = Computer(settings(), owner=lambda: ("native-ci", "button-text"))
    try:
        target = await discover(computer, native_window)
        observed = await observe(computer, target, fixture.BUTTON_NAME)
        assert "click" in observed["node"]["actions"]
        clicked = {
            "snapshot_id": observed["snapshot_id"],
            "node_id": observed["node"]["id"],
            "action": "click",
        }
        assert await computer.action(clicked) == {
            "ok": True,
            "outcome": "accepted",
            "effect_verified": False,
        }
        event = await native_window.state()
        assert event["kind"] == "button" and event["clicks"] == 1
        assert event["changes"] == 0 and event["password_unchanged"] is True
        with pytest.raises(ComputerError, match="快照") as consumed:
            await computer.action(clicked)
        assert consumed.value.code == "stale_snapshot"

        observed = await observe(computer, target, fixture.ENTRY_NAME)
        assert observed["node"]["editable"] is True
        await computer.action(
            {
                "snapshot_id": observed["snapshot_id"],
                "node_id": observed["node"]["id"],
                "text": fixture.UNICODE_TEXT,
            }
        )
        for _ in range(4):
            event = await native_window.state()
            assert event["kind"] == "text" and event["clicks"] == 1
            assert event["password_unchanged"] is True
            if event["text"] == fixture.UNICODE_TEXT:
                break
        else:
            pytest.fail("GTK did not independently confirm complete Unicode text")

        observed = await observe(computer, target, fixture.PASSWORD_NAME)
        assert observed["node"]["editable"] is False
        with pytest.raises(ComputerError) as protected:
            await computer.action(
                {
                    "snapshot_id": observed["snapshot_id"],
                    "node_id": observed["node"]["id"],
                    "text": "must not be written",
                }
            )
        assert protected.value.outcome == "not_started"
        assert protected.value.code == "unsupported_operation"
        await computer.release({})
        with pytest.raises(ComputerError) as released:
            await computer.snapshot({"target_id": target})
        assert released.value.outcome == "not_started"
        assert released.value.code == "target_invalid"
    finally:
        await computer.aclose()


async def test_real_linux_departed_target_is_not_replaced(native_window: NativeWindow) -> None:
    computer = Computer(settings(), owner=lambda: ("native-ci", "departed"))
    try:
        target = await discover(computer, native_window)
        observed = await observe(computer, target, fixture.BUTTON_NAME)
        native_window.process.terminate()
        await asyncio.wait_for(
            native_window.process.wait(), computer.config.operation_timeout_seconds
        )
        request = {
            "snapshot_id": observed["snapshot_id"],
            "node_id": observed["node"]["id"],
            "action": "click",
        }
        with pytest.raises(ComputerError) as departed:
            await computer.action(request)
        assert departed.value.outcome == "not_started"
        assert departed.value.code in {"target_invalid", "target_changed", "element_unavailable"}
        with pytest.raises(ComputerError) as replay:
            await computer.action(request)
        assert replay.value.code == "stale_snapshot"
        targets = await computer.targets({})
        assert all(
            computer._targets[item["target_id"]].node["pid"] != native_window.process.pid
            for item in targets["targets"]
        )
    finally:
        await computer.aclose()
