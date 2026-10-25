"""Real Case/probe parsing across collection of an owned transient helper."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from helpers import managed_native_worker as worker
from test_linux_helper_probe import NAME, HelperBus, setup
from test_managed_native_ci import forbid_real_effects as forbid_real_effects

from muli import failure_diagnostics
from muli.platforms.linux_transaction import ObservationError, SystemdProbe

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux transient helper fixture")


class CollectionBus:
    def __init__(self, original: HelperBus) -> None:
        self.original = original
        self.window = ""
        self.recheck = "empty"
        self.listings = 0
        self.lookups = 0
        self.failed = False
        self.calls: list[list[str]] = []

    def arm(self, window: str, recheck: str = "empty") -> None:
        self.window, self.recheck = window, recheck
        self.listings = self.lookups = 0
        self.failed = False
        self.calls.clear()

    async def __call__(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        self.calls.append(argv)
        if "ListUnitsByPatterns" in argv:
            assert argv[-4:] == ["asas", "0", "1", NAME]
            self.listings += 1
            if self.failed:
                if self.recheck == "cancel":
                    raise asyncio.CancelledError
                if self.recheck == "failed":
                    return {"exit_code": 1, "output": "", "stderr": "not exported"}
                if self.recheck == "malformed":
                    return {"exit_code": 0, "output": "not typed JSON"}
                if self.recheck == "empty":
                    return {
                        "exit_code": 0,
                        "output": json.dumps({"type": "a(ssssssouso)", "data": [[]]}),
                    }
                if self.recheck == "wrong_name":
                    listing = copy.deepcopy(self.original.listing)
                    assert isinstance(listing, list) and isinstance(listing[0], list)
                    listing[0][0][0] = "muli-transaction-" + "b" * 64 + ".service"
                    return {
                        "exit_code": 0,
                        "output": json.dumps({"type": "a(ssssssouso)", "data": listing}),
                    }
        if "GetUnit" in argv:
            self.lookups += 1
        at_window = (
            self.window == "lookup"
            and "GetUnit" in argv
            and self.lookups == 1
            or self.window == "snapshot_service"
            and "get-property" in argv
            and "MainPID" in argv
            or self.window == "details"
            and "get-property" in argv
            and "ExecMainStatus" in argv
        )
        if self.window and not self.failed and at_window:
            self.failed = True
            return {
                "exit_code": 1,
                "output": "",
                "stderr": f"Call failed: Unit {NAME} not loaded.\n",
                "truncated": False,
            }
        return await self.original(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)


@dataclass(slots=True)
class CollectionCase:
    case: worker.Case
    probe: SystemdProbe
    bus: CollectionBus
    argv: tuple[str, ...]
    source: dict[str, Any]


@pytest.fixture
async def collection(tmp_path: Path) -> CollectionCase:
    probe, original, argv, _, _ = setup(tmp_path)
    original.rows.update(
        ControlGroup=("s", f"/user.slice/user@{os.geteuid()}.service/{NAME}"),
        Result=("s", "success"),
        ExecMainStatus=("i", 0),
    )
    original.listing = [
        [
            [
                NAME,
                "",
                "loaded",
                "active",
                "running",
                "",
                "/org/freedesktop/systemd1/unit/helper",
                0,
                "",
                "/",
            ]
        ]
    ]
    bus = CollectionBus(original)
    probe.runner = bus
    root = tmp_path / "case"
    root.mkdir()
    case = worker.Case(root)
    case.result["generations"] = [
        {
            "deployment": "d" * 64,
            "generation": "a" * 32,
            "helper": NAME,
        }
    ]
    # This positive control uses the actual Case and typed SystemdProbe parser
    # to bind source identity before any collection race is injected.
    assert not await case.helper_empty(probe, NAME, argv)
    source = copy.deepcopy(case.result["resources"]["helper_source"])
    assert source["helper"] == NAME and source["pid"] == 123
    assert source["uid"] == os.geteuid()
    return CollectionCase(case, probe, bus, argv, source)


@pytest.mark.parametrize("window", ["lookup", "snapshot_service", "details"])
async def test_actual_helper_collection_requires_a_fresh_exact_empty_listing(
    collection: CollectionCase, window: str
) -> None:
    collection.bus.arm(window)
    assert await collection.case.helper_empty(collection.probe, NAME, collection.argv)
    assert collection.bus.failed and collection.bus.listings == 2
    assert collection.case.result["resources"]["helper_source"] == collection.source
    final = collection.case.result["timeline"][-1]
    assert final["stage"] == "helper_exit" and final["state"] == "collected"
    assert final["helper"] == NAME


@pytest.mark.parametrize("recheck", ["exists", "failed", "malformed", "wrong_name", "cancel"])
async def test_failed_helper_observation_never_treats_an_unproved_listing_as_absence(
    collection: CollectionCase, recheck: str
) -> None:
    collection.bus.arm("lookup", recheck)
    expected = asyncio.CancelledError if recheck == "cancel" else ObservationError
    with pytest.raises(expected) as caught:
        await collection.case.helper_empty(collection.probe, NAME, collection.argv)
    if recheck != "cancel":
        assert caught.value.args == ("systemd_observation_unavailable",)
    assert collection.bus.listings == 2
    assert collection.case.result["resources"]["helper_source"] == collection.source
    assert collection.case.result["timeline"][-1]["state"] == "active"


async def test_malformed_snapshot_cannot_be_reclassified_as_collected(
    collection: CollectionCase,
) -> None:
    collection.bus.arm("disabled")
    collection.bus.original.rows["InvocationID"] = ("ay", [0])
    with pytest.raises(ObservationError, match="^invalid_invocation$"):
        await collection.case.helper_empty(collection.probe, NAME, collection.argv)
    assert collection.bus.listings == 1
    assert collection.case.result["resources"]["helper_source"] == collection.source


async def test_helper_argv_drift_keeps_original_source_revocation_and_error(
    collection: CollectionCase,
) -> None:
    collection.bus.arm("disabled")
    entries = copy.deepcopy(collection.bus.original.rows["ExecStart"][1])
    entries[0][1] = ["foreign-controller"]
    collection.bus.original.rows["ExecStart"] = ("a(sasbttttuii)", entries)
    with pytest.raises(worker.NativeError, match="^native_helper_identity$"):
        await collection.case.helper_empty(collection.probe, NAME, collection.argv)
    assert collection.bus.listings == 1
    assert "helper_source" not in collection.case.result["resources"]


async def test_collection_after_later_rpc_failure_never_restores_a_revoked_source(
    collection: CollectionCase,
) -> None:
    collection.bus.arm("details")
    collection.bus.original.rows["InvocationID"] = ("ay", [2] * 16)
    assert await collection.case.helper_empty(collection.probe, NAME, collection.argv)
    assert collection.bus.failed and collection.bus.listings == 2
    assert collection.source["generation"] in collection.case._invalid_helper_sources
    assert "helper_source" not in collection.case.result["resources"]
    assert collection.case.result["timeline"][-1]["state"] == "collected"


@pytest.mark.parametrize("extractor_fails", [False, True], ids=["safe-frames", "extractor-failure"])
async def test_worker_reports_owned_probe_frames_without_exporting_private_failure_data(
    collection: CollectionCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extractor_fails: bool,
) -> None:
    secret = "private-password-not-for-export"
    collection.bus.original.rows["InvocationID"] = ("ay", [0])

    async def drive(case: worker.Case) -> None:
        case.note("helper_exit")
        try:
            raise RuntimeError(secret + str(tmp_path))
        except RuntimeError:
            # Keep a private chained exception while the real typed probe
            # rejects the malformed snapshot inside a package-owned frame.
            await case.helper_empty(collection.probe, NAME, collection.argv)

    if extractor_fails:

        def fail_capture(error: BaseException) -> failure_diagnostics.FailureDiagnostic:
            raise RuntimeError(secret + str(tmp_path))

        monkeypatch.setattr(failure_diagnostics, "capture_failure", fail_capture)
    monkeypatch.setattr(worker.Case, "drive", drive)
    monkeypatch.setenv(
        "MULI_NATIVE_EXECUTE_DEADLINE", str(worker.time.monotonic() + worker.HARNESS_SECONDS)
    )
    assert await worker.run("drive", tmp_path) == 1
    persisted = (tmp_path / "result.json").read_text()
    result = json.loads(persisted)
    assert result["code"] == "ObservationError" and result["completed"] is False
    assert "failure_diagnostic" not in result
    stderr = capsys.readouterr().err
    assert "native_drive_failed:helper_exit:ObservationError" in stderr
    assert secret not in stderr + persisted and str(tmp_path) not in stderr + persisted
    prefix = "native_failure_diagnostic:"
    lines = [line.removeprefix(prefix) for line in stderr.splitlines() if line.startswith(prefix)]
    assert len(lines) == (0 if extractor_fails else 1)
    if lines:
        assert len(lines[0].encode("utf-8")) <= failure_diagnostics.MAX_FAILURE_BYTES
        diagnostic = failure_diagnostics.validate_failure_diagnostic(json.loads(lines[0]))
        assert diagnostic is not None
        assert any(
            frame["module"] == "muli.platforms.linux_transaction"
            and frame["function"] == "SystemdProbe._snapshot"
            and type(frame["line"]) is int
            for frame in diagnostic["frames"]
        )
