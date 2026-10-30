"""Offline Case.transaction evidence; journal publication and revisions are real."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

import pytest
from helpers import managed_native_worker as worker
from test_managed_backend import Host
from test_managed_launcher import _slot
from test_managed_native_ci import ci

from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
from muli.managed_journal import advance_managed
from muli.managed_transaction import TransactionRecordV2, TransitionV2
from muli.management_transaction import TransactionJournal
from muli.platforms.managed_deployment import SlotIdentity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux journal/flock fixture")


async def test_actual_transaction_preserves_first_and_final_failure_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject only HTTP/helper observations, not journal records or note payloads."""

    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("offline transaction evidence must not start a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)

    def retained_slot(root: Path, name: str) -> SlotIdentity:
        assert name in {"base", "target"}
        return _slot(root, "a" if name == "base" else "b")

    # Reuse real retained slot/anchor/snapshot publication, with the native
    # fixture's fixed controller location. No process or systemd is involved.
    monkeypatch.setattr("test_managed_backend._slot", retained_slot)
    directory = tmp_path / "data/management/transaction-v1"
    with TransactionJournal(directory, tmp_path / "data/muli.sqlite3") as journal:
        with journal.locked() as control:
            host = Host(tmp_path, journal, control, "adopt", change_config=False)
            try:
                host.release_writer()
                host.move("draining")
                initial = host.move("activating", "stop_base")
                transitions = iter(
                    (
                        None,
                        TransitionV2(phase="activating", intent="install_target"),
                        TransitionV2(
                            phase="rolling_back", intent="stop_target", code="deadline_exceeded"
                        ),
                        TransitionV2(phase="recovery_required", code="backend_failed"),
                    )
                )
                observed: list[TransactionRecordV2] = []
                helper_observations: list[str] = []
                case = worker.Case(tmp_path)

                async def http(
                    client: Any,
                    method: str,
                    path: str,
                    data: dict[str, Any] | None = None,
                ) -> tuple[int, dict[str, Any]]:
                    if method == "POST":
                        assert path == "/api/management/transactions"
                        assert data is not None and data["action"] == "adopt"
                        return 200, {"id": initial.request_id, "routing": None}
                    assert method == "GET" and path == "/api/management/transactions/current"
                    transition = next(transitions)
                    record = host.record()
                    if transition is not None:
                        record = advance_managed(control, record.position, transition)
                    observed.append(record)
                    return 200, {"transaction": record.public()}

                async def helper_empty(probe: Any, name: str, argv: tuple[str, ...]) -> bool:
                    record = host.record()
                    assert name == managed_helper_unit_name(record)
                    assert argv == managed_helper_arguments(host.anchor, host.slots["base"])
                    helper_observations.append(name)
                    return True

                async def pause() -> None:
                    # The finite transport sequence controls the next revision;
                    # it does not start a polling task or advance the deadline.
                    return None

                monkeypatch.setattr(case, "http", http)
                monkeypatch.setattr(case, "helper_empty", helper_empty)
                monkeypatch.setattr(case, "pause", pause)
                with pytest.raises(worker.NativeError, match="^native_transaction_blocked$"):
                    await case.transaction(
                        object(),
                        "offline-password",
                        "adopt",
                        host.slots["base"],
                        initial.identity.origin_boot,
                    )

                assert [record.phase for record in observed] == [
                    "activating",
                    "activating",
                    "rolling_back",
                    "recovery_required",
                ]
                assert len(helper_observations) == len(observed)
                final = journal.read_current()
                assert isinstance(final, TransactionRecordV2)
                assert final == observed[-1]
                assert final.failure_code == "deadline_exceeded"
                assert final.code == "backend_failed" and final.rollback_attempted
                saved_bytes = (tmp_path / "result.json").read_bytes()
                assert len(saved_bytes) <= worker.MAX_BYTES
                saved = json.loads(saved_bytes)
                assert len(saved["timeline"]) <= 64
                rows = [row for row in saved["timeline"] if row["stage"] == "transaction"]
                assert [row["phase"] for row in rows] == [record.phase for record in observed]
                fields = ("revision", "intent", "code", "failure_code", "rollback_attempted")
                assert [{key: row.get(key) for key in fields} for row in rows] == [
                    {key: getattr(record, key) for key in fields} for record in observed
                ]
                assert all("health" not in row and "challenge" not in row for row in rows)
                assert ci.fixture_result(saved_bytes) == saved
            finally:
                host.release_writer()
                await host.backend.__aexit__()


def test_legacy_timeline_keeps_its_original_shape(tmp_path: Path) -> None:
    case = worker.Case(tmp_path)
    case.note("transaction", phase="activating", activation="pending")
    original = json.loads((tmp_path / "result.json").read_bytes())
    assert ci.fixture_result(json.dumps(original).encode()) == original
    assert set(original["timeline"][0]) == {"stage", "elapsed_ms", "phase", "activation"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", True),
        ("revision", 0),
        ("revision", 2**63),
        ("intent", "invoke_unknown"),
        ("code", "native_error_body"),
        ("failure_code", {"raw": "private"}),
        ("rollback_attempted", 1),
    ],
)
def test_revision_facts_reject_non_typed_or_unlisted_values(
    tmp_path: Path, field: str, value: object
) -> None:
    facts: dict[str, Any] = {
        "revision": 1,
        "intent": "none",
        "code": None,
        "failure_code": None,
        "rollback_attempted": False,
    }
    facts[field] = value
    case = worker.Case(tmp_path)
    with pytest.raises(worker.NativeError, match="^native_fact_invalid$"):
        case.note("transaction", **facts)
    assert case.result["timeline"] == []
    case.result["timeline"] = [{"stage": "transaction", "elapsed_ms": 0, **facts}]
    with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
        ci.fixture_result(json.dumps(case.result).encode())


def test_revision_facts_must_be_a_complete_transaction_group(tmp_path: Path) -> None:
    facts: dict[str, Any] = {
        "revision": 1,
        "intent": "none",
        "code": None,
        "failure_code": None,
        "rollback_attempted": False,
    }
    case = worker.Case(tmp_path)
    for stage, partial in (("import", facts), ("transaction", {"revision": 1})):
        with pytest.raises(worker.NativeError, match="^native_fact_invalid$"):
            case.note(stage, **partial)
        case.result["timeline"] = [{"stage": stage, "elapsed_ms": 0, **partial}]
        with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
            ci.fixture_result(json.dumps(case.result).encode())
