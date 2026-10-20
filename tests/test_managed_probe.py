from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from test_linux_transaction import FakeBus, write_stat
from test_managed_launcher import _CONFIG, Deployment
from test_managed_launcher import deployment as deployment

from muli.managed_journal import RoleSnapshots
from muli.managed_transaction import TransactionRecordV2, raw_snapshot
from muli.management_transaction import TransactionJournal
from muli.platforms.linux_transaction import ObservationError
from muli.platforms.managed_deployment import DeploymentAnchor, SelectorValue
from muli.platforms.managed_probe import (
    ManagedProbeResult,
    ManagedSystemdProbe,
    ManagedUserService,
    managed_process_arguments,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed service observation")


@dataclass(slots=True)
class ProbeHost:
    deployment: Deployment
    journal: TransactionJournal
    record: TransactionRecordV2
    service: ManagedUserService
    bus: FakeBus
    proc: Path
    probe: ManagedSystemdProbe


@pytest.fixture
def host(deployment: Deployment, tmp_path: Path) -> Iterator[ProbeHost]:
    previous = deployment.anchor
    service = ManagedUserService(
        Path(previous.config_path),
        Path(previous.data_dir),
        Path(previous.unit_path).parent,
        controller=deployment.base,
        launcher=Path(previous.launcher.path),
        journal_root=deployment.root,
    )
    unit = service.render().encode()
    anchor = DeploymentAnchor.model_validate(
        {
            **previous.model_dump(),
            "managed_unit": raw_snapshot(unit, 16384).model_dump(),
        }
    )
    configured = replace(
        deployment,
        anchor=anchor,
        base_selector=SelectorValue(deployment=anchor.digest, installation=deployment.base.digest),
        target_selector=SelectorValue(
            deployment=anchor.digest, installation=deployment.target.digest
        ),
    )
    record = configured.record()
    values = record.model_dump()
    for role in ("base", "target"):
        values[role]["unit"] = anchor.managed_unit.model_dump()
    record = TransactionRecordV2.model_validate(values)
    (configured.root / "state.json").write_bytes(record.model_dump_json().encode())
    service.path.parent.mkdir()
    service.path.write_bytes(unit)
    bus = FakeBus(service)
    proc = tmp_path / "proc"
    process = proc / "123"
    process.mkdir(parents=True)
    write_stat(process / "stat")
    (process / "cmdline").write_bytes(
        b"\x00".join(
            os.fsencode(arg) for arg in managed_process_arguments(anchor, configured.target)
        )
        + b"\x00"
    )
    (process / "cgroup").write_text("0::/user.slice/example.service\n")
    (process / "exe").symlink_to(configured.target.resolved_python.path)
    for role, selector in (
        ("base", configured.base_selector),
        ("target", configured.target_selector),
    ):
        for name, data in (
            RoleSnapshots(_CONFIG, selector.canonical(), unit)
            .entries(record.generation, role)
            .items()
        ):
            path = configured.root / name
            path.write_bytes(data)
            path.chmod(0o600)
    with TransactionJournal(configured.root, Path(anchor.database_path)) as journal:
        yield ProbeHost(
            configured,
            journal,
            record,
            service,
            bus,
            proc,
            ManagedSystemdProbe(runner=bus, proc_root=proc),
        )


async def observe(host: ProbeHost) -> ManagedProbeResult:
    return await host.probe.read_managed(
        host.journal,
        host.record,
        host.deployment.anchor,
        host.deployment.base,
        {"base": host.deployment.base, "target": host.deployment.target},
    )


async def test_stable_unit_and_exec_role_have_distinct_verified_arguments(host: ProbeHost) -> None:
    result = await observe(host)
    assert result.unit_confirmed and result.process_roles == ("target",)
    assert result.service.process is not None
    assert result.service.unit.argv == tuple(host.service._arguments())
    assert result.service.process.argv == managed_process_arguments(
        host.deployment.anchor, host.deployment.target
    )
    assert result.service.process.argv != result.service.unit.argv
    assert host.deployment.base.resolved_python == host.deployment.target.resolved_python
    assert all("GetUnit" in call or "get-property" in call for call in host.bus.calls)


async def test_same_binary_cannot_hide_a_foreign_lexical_python(host: ProbeHost) -> None:
    argv = list(managed_process_arguments(host.deployment.anchor, host.deployment.target))
    argv[0] = str(Path(host.deployment.target.prefix) / "bin/another-python")
    (host.proc / "123/cmdline").write_bytes(
        b"\x00".join(os.fsencode(arg) for arg in argv) + b"\x00"
    )
    with pytest.raises(ObservationError, match="foreign_process"):
        await observe(host)


async def test_base_process_is_observed_as_base_not_candidate_health(host: ProbeHost) -> None:
    argv = managed_process_arguments(host.deployment.anchor, host.deployment.base)
    (host.proc / "123/cmdline").write_bytes(
        b"\x00".join(os.fsencode(arg) for arg in argv) + b"\x00"
    )
    result = await observe(host)
    assert result.process_roles == ("base",)


@pytest.mark.parametrize(
    "phase,intent,allowed",
    [
        ("activating", "install_target", True),
        ("rolling_back", "restore_base", True),
        ("rolling_back", "stop_target", True),
        ("activating", "start_target", False),
        ("verifying", "none", False),
    ],
)
async def test_unloaded_unit_only_observable_in_stopped_reconciliation(
    host: ProbeHost,
    phase: str,
    intent: str,
    allowed: bool,
) -> None:
    values = host.record.model_dump()
    values.update(phase=phase, intent=intent)
    if phase == "rolling_back":
        values.update(rollback_attempted=True, rollback_challenge="rollback-challenge")
    host.record = TransactionRecordV2.model_validate(values)
    host.bus.rows.update(
        MainPID=("u", 0),
        ActiveState=("s", "inactive"),
        SubState=("s", "dead"),
        NeedDaemonReload=("b", True),
    )
    if allowed:
        result = await observe(host)
        assert result.process_roles == () and not result.unit_confirmed
    else:
        with pytest.raises(ObservationError, match="managed_unit_not_loaded"):
            await observe(host)


async def test_running_unit_needing_reload_never_becomes_health(host: ProbeHost) -> None:
    host.bus.rows["NeedDaemonReload"] = ("b", True)
    with pytest.raises(ObservationError, match="managed_unit_not_loaded"):
        await observe(host)


@pytest.mark.parametrize("change", ["pid", "process", "unit"])
async def test_changed_second_observation_is_rejected(host: ProbeHost, change: str) -> None:
    def modify() -> None:
        if change == "pid":
            host.bus.rows["MainPID"] = ("u", 124)
        elif change == "process":
            write_stat(host.proc / "123/stat", start=101)
        else:
            host.service.path.write_bytes(host.service.path.read_bytes() + b"# changed\n")

    host.bus.before_second = modify
    with pytest.raises(ObservationError, match="(?:service_observation|process_identity)_changed"):
        await observe(host)
