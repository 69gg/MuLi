"""Real three-file reconciliation, probe parser and lease; synthetic manager only."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from test_linux_transaction import FakeBus
from test_managed_boot import Host
from test_managed_boot import host as boot_host
from test_managed_controller import FakeClock
from test_project_controller_transfer_host import _pending_record, _publish, _restore_origin
from test_project_controller_transfer_runtime import _boot_environment, _publish_terminal_transfer

from muli.managed_journal import read_snapshots
from muli.managed_transaction import ProjectControllerTransferRecord
from muli.platforms.linux_transaction import ObservationError
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_deployment import DeploymentStore, SelectorStore, SelectorValue
from muli.platforms.managed_probe import ManagedSystemdProbe, ManagedUserService

__all__ = ["boot_host"]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux file CAS and writer lease")


@pytest.mark.parametrize(
    "prefix,reverse",
    [("O", False), ("X", False), ("O", True), ("X", True), ("Y", True), ("T", True)],
)
async def test_transfer_backend_reconciles_only_frozen_forward_or_reverse_prefix(
    boot_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prefix: str,
    reverse: bool,
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    record = ProjectControllerTransferRecord.model_validate(
        {
            **_pending_record(source.record).model_dump(),
            "phase": "rolling_back" if reverse else "activating",
            "intent": "restore_base" if reverse else "install_target",
            "rollback_attempted": reverse,
            "rollback_challenge": "independent-rollback" if reverse else None,
        }
    )
    _restore_origin(boot_host, record)
    if prefix in {"X", "T"}:
        with SelectorStore(boot_host.journal.directory.path) as selected:
            selected.replace(
                selected.read(),
                SelectorValue(
                    deployment=source.anchor.digest, installation=boot_host.target.digest
                ),
                guard=lambda: None,
            )
    if prefix in {"Y", "T"}:
        Path(source.anchor.unit_path).write_bytes(source.service.render().encode())
    _publish(boot_host, record)
    with DeploymentStore(boot_host.journal.directory.path) as deployments:
        origin = deployments.read_anchor(record.origin.deployment)
        old_controller = deployments.read_slot(origin.controller_slot)
    old_service = ManagedUserService(
        Path(origin.config_path),
        Path(origin.data_dir),
        Path(origin.unit_path).parent,
        controller=old_controller,
        launcher=Path(origin.launcher.path),
        journal_root=boot_host.journal.directory.path,
    )
    bus = FakeBus(old_service)
    bus.rows.update(MainPID=("u", 0), ActiveState=("s", "inactive"), SubState=("s", "dead"))
    commands: list[str] = []

    async def runner(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        if argv[0] != "systemctl":
            return await bus(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)
        assert argv[:2] == ["systemctl", "--user"]
        commands.append(argv[-1] if "daemon-reload" in argv else argv[-2])
        if "daemon-reload" in argv:
            bus.rows["ExecStart"] = (
                "a(sasbttttuii)",
                [
                    [
                        source.service.executable,
                        source.service._arguments(),
                        False,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                    ]
                ],
            )
            bus.rows["NeedDaemonReload"] = ("b", False)
        return {"exit_code": 0, "output": "", "stderr": ""}

    cgroup_root = tmp_path / "cgroup"
    cgroup = cgroup_root / "user.slice/example.service"
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    probe = ManagedSystemdProbe(runner=runner, proc_root=tmp_path / "unused-proc")
    role = "base" if reverse else "target"
    with boot_host.journal.locked() as control:
        async with ManagedLinuxBackend(
            source.anchor,
            boot_host.target,
            {"base": boot_host.target, "target": boot_host.target},
            boot_host.journal,
            control=control,
            clock=FakeClock(),
            probe=probe,
            runner=runner,
            cgroup_root=cgroup_root,
        ) as backend:
            await backend.install(record, role, read_snapshots(boot_host.journal, record, role))
            assert backend._disk().digests == (
                record.base.configuration.digest,
                record.base.selector.digest if record.base.selector else None,
                record.base.unit.digest,
            )
            assert commands == ["daemon-reload"]
            current = await backend.observe(record)
            assert current.confirmation is not None and current.confirmation.role == role
            assert boot_host.journal.read_current() == record


async def test_transfer_backend_forward_rejects_reverse_only_prefix_without_mutation(
    boot_host: Host, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    record = ProjectControllerTransferRecord.model_validate(
        {
            **_pending_record(source.record).model_dump(),
            "phase": "activating",
            "intent": "install_target",
        }
    )
    _restore_origin(boot_host, record)
    Path(source.anchor.unit_path).write_bytes(
        source.service.render().encode()
    )  # Y: new unit before selector.
    _publish(boot_host, record)
    bus = FakeBus(source.service)
    bus.rows.update(MainPID=("u", 0), ActiveState=("s", "inactive"), SubState=("s", "dead"))
    before = (
        Path(source.anchor.config_path).read_bytes(),
        Path(source.anchor.unit_path).read_bytes(),
    )
    with SelectorStore(boot_host.journal.directory.path) as selected:
        selector = selected.read()
    cgroup_root = tmp_path / "cgroup"
    cgroup = cgroup_root / "user.slice/example.service"
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    with boot_host.journal.locked() as control:
        async with ManagedLinuxBackend(
            source.anchor,
            boot_host.target,
            {"base": boot_host.target, "target": boot_host.target},
            boot_host.journal,
            control=control,
            clock=FakeClock(),
            probe=ManagedSystemdProbe(runner=bus),
            runner=bus,
            cgroup_root=cgroup_root,
        ) as backend:
            with pytest.raises(ObservationError, match="foreign_managed_binding"):
                await backend.install(
                    record, "target", read_snapshots(boot_host.journal, record, "target")
                )
    with SelectorStore(boot_host.journal.directory.path) as selected:
        assert selected.read() == selector
    assert (
        Path(source.anchor.config_path).read_bytes(),
        Path(source.anchor.unit_path).read_bytes(),
    ) == before
    assert not any("daemon-reload" in call or "start" in call for call in bus.calls)
