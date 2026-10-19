"""Real helper.serve/Native with fake CF, real lease pipes, real Computer facade."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

import muli.tools.ax_computer as ax_module
from muli.config import Settings
from muli.tools.ax_lease import AXHelperLease
from muli.tools.computer import Computer, ComputerError

CHILD = r"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from typing import Any
sys.path[:0] = sys.argv[3:5]
from test_ax_helper import Element, World
from muli.tools import ax_helper as helper

mode = sys.argv[1]
audit = Path(sys.argv[2])
button = Element(name="确认𐐀", mutation_code=-25204 if mode == "unknown" else 0)
text = Element("AXTextField", "输入框", attrs={"AXSubrole": "AXSearchField"})
secret = Element(name="SECRET_PROTECTED_DESCENDANT")
secure = Element("AXTextField", "SECRET_TITLE", attrs={"AXSubrole": helper.SECURE_SUBROLE, "AXChildren": [secret]})
children = ([text] if mode == "text" else [secure] if mode == "secure" else
            [Element(name=str(index) + "𐐀" * 1000) for index in range(80)] if mode == "budget" else [button])
window = Element("AXWindow", "测试窗口", attrs={"AXChildren": children})
application = Element("AXApplication", "测试应用", attrs={"AXWindows": [window], "AXChildren": [window]})
world = World(application=application)
if mode == "permission":
    world.ax.trusted = False

def record(value: dict[str, Any]) -> None:
    with audit.open("a", encoding="utf-8") as target:
        target.write(json.dumps(value, ensure_ascii=False) + "\n")

class Input:
    def readline(self, size: int = -1) -> bytes:
        value = sys.stdin.buffer.readline(size)
        if value:
            record({"command": json.loads(value)["command"]})
        return value

class Output:
    def write(self, value: bytes) -> int:
        record({"wire_bytes": len(value), "wire_chars": len(value.decode("utf-8")),
                "button_mutations": button.mutations, "text_mutations": text.mutations,
                "protected_reads": secure.reads, "descendant_reads": secret.reads})
        return sys.stdout.buffer.write(value)
    def flush(self) -> None:
        sys.stdout.buffer.flush()

result = helper.serve("--session", Input(), Output(), sys.stderr.buffer, factory=world.native)
world.close()
record({"cf_balanced": True})
raise SystemExit(result)
"""


def install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, *, output_chars: int = 24000
) -> tuple[Computer, list[AXHelperLease], Path]:
    monkeypatch.setattr(ax_module.sys, "platform", "darwin")
    child, audit = tmp_path / "native_fake_cf.py", tmp_path / "audit.jsonl"
    child.write_text(CHILD, encoding="utf-8")
    computer = Computer(
        Settings.model_validate(
            {
                "computer": {"backend": "ax", "enabled": True, "max_nodes": 100},
                "limits": {"max_output_chars": output_chars},
            }
        ),
        owner=lambda: ("owner", "run"),
    )
    leases: list[AXHelperLease] = []

    def create(**kwargs: Any) -> AXHelperLease:
        lease = AXHelperLease(
            [
                sys.executable,
                str(child),
                mode,
                str(audit),
                str(Path(__file__).resolve().parent),
                str(Path(__file__).resolve().parents[1] / "src"),
            ],
            timeout=3,
            ttl=15,
            max_nodes=100,
            once=kwargs.get("once", False),
        )
        leases.append(lease)
        return lease

    monkeypatch.setattr(computer._ax, "_new_lease", create)
    return computer, leases, audit


def rows(audit: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]


async def observe_window(computer: Computer) -> dict[str, Any]:
    apps = await computer.targets({})
    assert apps["scope"] == "foreground_application" and apps["complete"]
    windows = await computer.targets({"application_id": apps["targets"][0]["application_id"]})
    assert windows["scope"] == "application_windows" and windows["complete"]
    window = next(row for row in windows["targets"] if row["kind"] == "window")
    result = await computer.snapshot({"target_id": window["target_id"]})
    assert result["scope"] == "window"
    assert all("ref_id" not in row and "pid" not in row for row in result["nodes"])
    return result


async def test_native_serve_app_window_action_same_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    computer, leases, audit = install(tmp_path, monkeypatch, "button")
    try:
        snapshot = await observe_window(computer)
        assert snapshot["complete"]
        root, button = snapshot["nodes"]
        assert button["parent_id"] == root["id"] and button["depth"] == 1
        result = await computer.action(
            {"snapshot_id": snapshot["snapshot_id"], "node_id": button["id"], "action": "AXPress"}
        )
        assert result == {"ok": True, "outcome": "accepted", "effect_verified": False}
        assert len(leases) == 1 and leases[0].closed
        log = rows(audit)
        assert [row["command"] for row in log if "command" in row] == [
            "targets",
            "targets",
            "snapshot",
            "action",
        ]
        assert [row for row in log if "wire_bytes" in row][-1]["button_mutations"] == [
            ["action", "AXPress"]
        ]
    finally:
        await computer.aclose()


async def test_native_serve_explicit_nonsecure_type_preserves_nul(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    computer, leases, audit = install(tmp_path, monkeypatch, "text")
    try:
        snapshot = await observe_window(computer)
        node = next(row for row in snapshot["nodes"] if row["editable"])
        assert node["protected"] is False
        result = await computer.action(
            {"snapshot_id": snapshot["snapshot_id"], "node_id": node["id"], "text": "甲\x00乙𐐀"}
        )
        assert result["outcome"] == "accepted" and leases[0].closed
        last = [row for row in rows(audit) if "wire_bytes" in row][-1]
        assert last["text_mutations"] == [["type", "甲\x00乙𐐀"]]
    finally:
        await computer.aclose()


async def test_native_serve_unknown_receipt_remains_unknown_and_single_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    computer, leases, audit = install(tmp_path, monkeypatch, "unknown")
    try:
        snapshot = await observe_window(computer)
        button = next(row for row in snapshot["nodes"] if row["role"] == "AXButton")
        with pytest.raises(ComputerError) as caught:
            await computer.action(
                {
                    "snapshot_id": snapshot["snapshot_id"],
                    "node_id": button["id"],
                    "action": "AXPress",
                }
            )
        assert (caught.value.code, caught.value.outcome) == ("action_unknown_outcome", "unknown")
        assert leases[0].closed and not computer._ax._snapshot_id
        last = [row for row in rows(audit) if "wire_bytes" in row][-1]
        assert last["button_mutations"] == [["action", "AXPress"]]
    finally:
        await computer.aclose()


async def test_native_serve_protected_subtree_unread_and_public_action_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    computer, leases, audit = install(tmp_path, monkeypatch, "secure")
    try:
        snapshot = await observe_window(computer)
        assert not snapshot["complete"] and "SECRET" not in json.dumps(snapshot)
        secure = next(row for row in snapshot["nodes"] if row["protected"] is True)
        assert not secure["actions"] and not secure["editable"]
        with pytest.raises(ComputerError) as caught:
            await computer.action(
                {
                    "snapshot_id": snapshot["snapshot_id"],
                    "node_id": secure["id"],
                    "text": "do not type",
                }
            )
        assert caught.value.outcome == "not_started" and leases[0].closed
        log = rows(audit)
        assert not any(row.get("command") == "type" for row in log)
        last = [row for row in log if "wire_bytes" in row][-1]
        assert last["descendant_reads"] == []
        assert "AXChildren" not in last["protected_reads"]
    finally:
        await computer.aclose()


async def test_native_serve_unicode_wire_and_public_budget_keep_parent_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    computer, leases, audit = install(tmp_path, monkeypatch, "budget", output_chars=4000)
    try:
        snapshot = await observe_window(computer)
        assert len(json.dumps(snapshot, ensure_ascii=False)) <= 4000
        assert not snapshot["complete"] and "output_budget" in snapshot["omission_reasons"]
        observed: set[str] = set()
        for row in snapshot["nodes"]:
            assert row["parent_id"] is None or row["parent_id"] in observed
            observed.add(row["id"])
        assert set(computer._ax._nodes) == observed
        assert len(snapshot["nodes"]) >= 2
        frames = [row for row in rows(audit) if "wire_bytes" in row]
        assert max(row["wire_bytes"] for row in frames) <= 250000
        assert max(row["wire_chars"] for row in frames) <= 60000
    finally:
        await computer.aclose()
    assert leases[0].closed


async def test_native_serve_permission_error_invalidates_refs_without_device_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    computer, leases, audit = install(tmp_path, monkeypatch, "permission")
    try:
        with pytest.raises(ComputerError) as caught:
            await computer.snapshot({})
        assert (caught.value.code, caught.value.outcome) == ("permission_required", "not_started")
        assert leases[0].closed and not computer._ax._snapshot_id and not computer._ax._nodes
        assert [row["command"] for row in rows(audit) if "command" in row] == ["snapshot"]
    finally:
        await computer.aclose()
