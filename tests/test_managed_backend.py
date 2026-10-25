from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from test_linux_controller import Clock, reply
from test_managed_launcher import _slot

from muli.format_admission import CURRENT_FORMAT_STATE
from muli.managed_controller import BindingConfirmation, ManagedTransactionController
from muli.managed_journal import RoleSnapshots, advance_managed, prepare_managed
from muli.managed_transaction import (
    HealthProofV2,
    RoleBinding,
    ServiceIdentityV2,
    TransactionRecordV2,
    TransitionV2,
    format_digest,
    raw_snapshot,
)
from muli.management_transaction import (
    Deadline,
    FixedLock,
    JournalControl,
    Role,
    TransactionError,
    TransactionJournal,
    _snapshot,
)
from muli.platforms import managed_launcher
from muli.platforms.linux_transaction import (
    ObservationError,
    ProbeLimits,
    ProcessIdentity,
    ServiceSnapshot,
    UnitSnapshot,
)
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    ManagedDeploymentError,
    SelectorSnapshot,
    SelectorStore,
    SelectorValue,
    SlotIdentity,
)
from muli.platforms.managed_probe import (
    ManagedProbeResult,
    ManagedSystemdProbe,
    ManagedUserService,
    managed_process_arguments,
)
from muli.platforms.transaction_files import ConfigurationFile, ConfigurationSnapshot
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed effects/leases")
CONFIG = b'{"server":{"host":"127.0.0.1","port":8787}}'
CHANGED_CONFIG = b'{"server":{"host":"127.0.0.1","port":8788}}'
ORIGIN, TARGET_BOOT, BASE_BOOT = "1" * 32, "2" * 32, "3" * 32


class Probe(ManagedSystemdProbe):
    """Fake manager/process evidence; disk CAS and flock are real."""

    def __init__(self, host: Host) -> None:
        super().__init__(limits=ProbeLimits(seconds=0.1))
        self.host, self.count = host, 0
        self.before_read: Callable[[int], None] | None = None
        self.loaded_unit = host.data["base"].unit
        self.snapshot = ServiceSnapshot(
            UnitSnapshot(
                name=host.service.unit,
                load_state="loaded",
                active_state="inactive",
                sub_state="dead",
                fragment=str(host.service.path),
                source="",
                drop_ins=(),
                needs_reload=False,
                invocation=ORIGIN,
                job=(0, "/"),
                service_type="simple",
                pid=0,
                cgroup="/user.slice/muli.service",
                executable=host.service.executable,
                argv=tuple(host.service._arguments()),
                ignore_failure=False,
                kill_mode="control-group",
                send_sigkill=True,
            ),
            None,
        )

    async def read_managed(
        self,
        journal: TransactionJournal,
        record: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        assert journal is self.host.journal and anchor == self.host.anchor
        assert controller == self.host.slots["base"] and slots == self.host.slots
        self.count += 1
        if self.before_read is not None:
            self.before_read(self.count)
        unit = Path(anchor.unit_path).read_bytes()
        if unit not in {self.host.data["base"].unit, self.host.data["target"].unit}:
            raise ObservationError("unsupported_unit_file")
        disk = raw_snapshot(unit, journal.limits.max_snapshot_bytes).digest
        loaded = raw_snapshot(self.loaded_unit, journal.limits.max_snapshot_bytes).digest
        snapshot = replace(
            self.snapshot, unit=replace(self.snapshot.unit, needs_reload=unit != self.loaded_unit)
        )
        process = snapshot.process
        matches: tuple[Role, ...] = ()
        if process is not None:
            matches = tuple(
                role
                for role in ("base", "target")
                if (
                    process.argv == self.host.arguments(role)
                    and process.executable == slots[role].resolved_python.path
                    and process.pid == snapshot.unit.pid
                    and process.cgroup == snapshot.unit.cgroup
                    and record.binding(role).unit.digest == disk
                )
            )
            if not matches:
                raise ObservationError("foreign_process")
        return ManagedProbeResult(snapshot, matches, frozenset({loaded}), disk)


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, host: Host) -> None:
        self.host = host
        self.calls: list[str] = []
        self.handler: Callable[[httpx.Request], httpx.Response] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(action)
        record = self.host.record()
        boot = self.host.probe.snapshot.unit.invocation
        role = self.host.backend._role(record, boot)
        expected = record.rollback_challenge if role == "base" else record.challenge
        assert request.headers["X-MuLi-Nonce"] == expected
        assert request.headers["host"] == "localhost"
        assert request.headers["accept-encoding"] == "identity"
        values = (
            dict(request.url.params) if request.method == "GET" else json.loads(request.content)
        )
        assert values == {"generation": record.generation, "boot": boot}
        if self.handler is not None:
            return self.handler(request)
        if action == "drain":
            self.host.admission = "paused"
            return reply({"state": "drained", "boot": boot})
        if action == "resume":
            self.host.admission = "released"
            return reply({"released": True})
        if action == "activate":
            self.host.admission = "released"
        return reply(self.host.proof().model_dump())


class Host:
    def __init__(
        self,
        root: Path,
        journal: TransactionJournal,
        control: JournalControl,
        mode: Literal["switch", "adopt"],
        *,
        change_config: bool,
    ) -> None:
        self.journal, self.control, self.clock = journal, control, Clock()
        base = _slot(root, "base")
        target = _slot(root, "target") if mode == "switch" else base
        self.slots: dict[Role, SlotIdentity] = {"base": base, "target": target}
        config, data_dir, unit_path = (
            root / "config.json",
            root / "data",
            root / "units/muli.service",
        )
        unit_path.parent.mkdir()
        with DeploymentStore(journal.directory.path) as store:
            for slot in self.slots.values():
                store.publish_slot(slot, guard=control.check)
            launcher = store.publish_launcher(guard=control.check)
            self.service = ManagedUserService(
                config,
                data_dir,
                unit_path.parent,
                controller=base,
                launcher=Path(launcher.path),
                journal_root=journal.directory.path,
            )
            unit = self.service.render().encode()
            self.anchor = DeploymentAnchor(
                config_path=str(config),
                data_dir=str(data_dir),
                database_path=str(journal.database),
                unit_path=str(unit_path),
                journal_root=str(journal.directory.path),
                controller_slot=base.digest,
                launcher=launcher,
                managed_unit=raw_snapshot(unit, 16384),
            )
            store.publish_anchor(self.anchor, guard=control.check)
        self.legacy = UserService(
            config, data_dir, unit_path.parent, platform="linux", executable=base.lexical_python
        )
        self.data: dict[Role, RoleSnapshots] = {}
        for role in ("base", "target"):
            legacy = mode == "adopt" and role == "base"
            selector = (
                None
                if legacy
                else SelectorValue(
                    deployment=self.anchor.digest,
                    installation=self.slots[role].digest,
                ).canonical()
            )
            self.data[role] = RoleSnapshots(
                CHANGED_CONFIG if change_config and role == "target" else CONFIG,
                selector,
                self.legacy.render().encode() if legacy else unit,
            )
        config.write_bytes(CONFIG)
        unit_path.write_bytes(self.data["base"].unit)
        with SelectorStore(journal.directory.path) as selector:
            selector.replace(None, self.selector("base"), guard=control.check)
        bindings: dict[Role, RoleBinding] = {}
        for role in ("base", "target"):
            data = self.data[role]
            bindings[role] = RoleBinding(
                entry="legacy" if data.selector is None else "managed",
                installation=self.slots[role].digest,
                compatibility=self.slots[role].compatibility_digest,
                configuration=_snapshot(data.configuration, journal.limits.max_snapshot_bytes),
                selector=None if data.selector is None else _snapshot(data.selector, 16384),
                unit=raw_snapshot(data.unit, journal.limits.max_snapshot_bytes),
            )
        with journal.writer_lease() as writer:
            prepare_managed(
                control,
                request_id="managed",
                action=mode,
                identity=ServiceIdentityV2(
                    deployment=self.anchor.digest,
                    store_instance="store",
                    main_session="main",
                    origin_boot=ORIGIN,
                ),
                base=bindings["base"],
                target=bindings["target"],
                base_data=self.data["base"],
                target_data=self.data["target"],
                observed_format=CURRENT_FORMAT_STATE,
                base_manifest=base.compatibility,
                target_manifest=target.compatibility,
                deadline=Deadline(created=self.clock(), seconds=180.0, rollback_reserve=60.0),
                writer=writer,
            )
        self.writer: FixedLock | None = None
        self.admission: str = "released"
        self.commands: list[str] = []
        self.fail_reload = False
        self.probe = Probe(self)
        self.transport = Transport(self)
        self.events = root / "cgroups/user.slice/muli.service/cgroup.events"
        self.events.parent.mkdir(parents=True)
        self.events.write_bytes(b"populated 0\n")
        self.backend = ManagedLinuxBackend(
            self.anchor,
            base,
            self.slots,
            journal,
            control=control,
            clock=self.clock,
            probe=self.probe,
            runner=self.command,
            transport=self.transport,
            cgroup_root=root / "cgroups",
        )
        self.running("base", origin=True)

    def selector(self, role: Role) -> SelectorValue | None:
        data = self.data[role].selector
        return None if data is None else SelectorValue.model_validate_json(data)

    def arguments(self, role: Role) -> tuple[str, ...]:
        return (
            tuple(self.legacy._arguments())
            if self.data[role].selector is None
            else managed_process_arguments(self.anchor, self.slots[role])
        )

    def record(self) -> TransactionRecordV2:
        record = self.control.read_current()
        assert isinstance(record, TransactionRecordV2)
        return record

    def move(self, phase: str, intent: str = "none", **values: Any) -> TransactionRecordV2:
        return advance_managed(
            self.control,
            self.record().position,
            TransitionV2.model_validate({"phase": phase, "intent": intent, **values}),
        )

    def install_intent(self) -> TransactionRecordV2:
        self.move("draining")
        self.move("activating", "stop_base")
        self.stopped()
        return self.move("activating", "install_target")

    def stopped(self) -> None:
        self.release_writer()
        self.probe.snapshot = ServiceSnapshot(
            replace(
                self.probe.snapshot.unit,
                active_state="inactive",
                sub_state="dead",
                pid=0,
                job=(0, "/"),
            ),
            None,
        )

    def release_writer(self) -> None:
        if self.writer is not None:
            self.writer.__exit__()
            self.writer = None

    def running(self, role: Role, *, origin: bool = False) -> None:
        self.release_writer()
        unit = replace(
            self.probe.snapshot.unit,
            active_state="active",
            sub_state="running",
            pid=123,
            invocation=ORIGIN if origin else TARGET_BOOT if role == "target" else BASE_BOOT,
        )
        self.probe.snapshot = ServiceSnapshot(
            unit,
            ProcessIdentity(
                123,
                100,
                os.geteuid(),
                self.slots[role].resolved_python.path,
                self.arguments(role),
                unit.cgroup,
            ),
        )
        self.admission = "released" if origin else "paused"
        self.writer = self.journal.writer_lease().__enter__()

    def proof(self) -> HealthProofV2:
        record = self.record()
        boot = self.probe.snapshot.unit.invocation
        role = self.backend._role(record, boot)
        binding = record.binding("base" if role is None else role)
        return HealthProofV2.model_validate(
            {
                "protocol": 2,
                "generation": record.generation,
                "challenge": record.rollback_challenge if role == "base" else record.challenge,
                "boot": boot,
                "role": "origin" if role is None else role,
                "deployment": record.identity.deployment,
                "installation": binding.installation,
                "compatibility": binding.compatibility,
                "configuration": binding.configuration.digest,
                "selector": None if binding.selector is None else binding.selector.digest,
                "unit": binding.unit.digest,
                "store_instance": record.identity.store_instance,
                "main_session": record.identity.main_session,
                "format_digest": format_digest(record.observed_format),
                "core_ready": True,
                "admission": self.admission,
            }
        )

    async def command(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert timeout == self.probe.limits.seconds and limit == self.probe.limits.command_bytes
        assert not merge_stderr and argv[:2] == ["systemctl", "--user"]
        action = (
            "daemon-reload" if "daemon-reload" in argv else "stop" if "stop" in argv else "start"
        )
        self.commands.append(action)
        if action == "daemon-reload":
            if not self.fail_reload:
                self.probe.loaded_unit = Path(self.anchor.unit_path).read_bytes()
        elif action == "stop":
            assert "--no-block" in argv and "--job-mode=fail" in argv
            self.stopped()
        else:
            assert "--no-block" in argv and "--job-mode=fail" in argv
            with SelectorStore(self.journal.directory.path) as selector:
                selected = selector.read()
            role: Role = (
                "target" if selected and selected.value == self.selector("target") else "base"
            )
            self.running(role)
        return {"exit_code": 0, "output": ""}


@pytest.fixture
async def host(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Host]:
    mode, change_config = getattr(request, "param", ("switch", False))
    with TransactionJournal(tmp_path / "journal", tmp_path / "data/muli.db") as journal:
        with journal.locked() as control:
            value = Host(tmp_path, journal, control, mode, change_config=change_config)
            try:
                yield value
            finally:
                value.release_writer()
                await value.backend.__aexit__()


def track_slot_scans(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Observe real package hashing after fixture preparation, without replacing it."""
    scans: list[Path] = []
    fingerprint = managed_launcher.package_fingerprint

    def observed(
        root: Path,
        limits: Mapping[str, int],
        *,
        verified: dict[str, dict[str, Any]] | None = None,
        observed: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, object]:
        scans.append(root)
        if verified is None and observed is None:
            return fingerprint(root, limits)
        return fingerprint(root, limits, verified=verified, observed=observed)

    monkeypatch.setattr(managed_launcher, "package_fingerprint", observed)
    return scans


async def test_one_guard_hashes_equal_controller_and_selected_slot_once(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Equality is the complete frozen installation, not Python object identity.
    host.backend.slots["base"] = host.slots["base"].model_copy(deep=True)
    assert host.backend.slots["base"] is not host.backend.controller_slot
    assert host.backend.slots["base"] == host.backend.controller_slot
    scans = track_slot_scans(monkeypatch)

    host.backend._check(host.record(), "base")

    assert scans == [Path(host.slots["base"].package.root)]
    assert host.commands == []


async def test_one_guard_hashes_distinct_slots_even_with_shared_python(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, target = host.slots["base"], host.slots["target"]
    assert base != target and base.resolved_python == target.resolved_python
    scans = track_slot_scans(monkeypatch)
    record = host.record()

    host.backend._check(record, "target")

    assert scans == [Path(base.package.root), Path(target.package.root)]
    # The selected package, not just the common interpreter, was authenticated.
    (Path(target.package.root) / "__main__.py").write_bytes(b"changed selected package")
    with pytest.raises(ManagedDeploymentError, match="^managed_slot_changed$"):
        host.backend._check(record, "target")
    assert host.commands == []


async def test_next_guard_rejects_changed_file_in_shared_controller_slot(host: Host) -> None:
    record = host.record()
    host.backend._check(record, "base")

    # No cache may outlive one synchronous guard, even for the same record/role.
    (Path(host.slots["base"].package.root) / "__main__.py").write_bytes(b"changed between guards")

    with pytest.raises(ManagedDeploymentError, match="^anchor_unavailable$"):
        host.backend._check(record, "base")
    assert host.commands == []


@pytest.mark.parametrize("host", [("switch", False), ("adopt", False)], indirect=True)
async def test_complete_step_chain_uses_actual_slot_even_with_same_config(host: Host) -> None:
    original = host.record()
    assert original.base.configuration == original.target.configuration
    controller = ManagedTransactionController(
        host.control, host.backend, host.clock, operation_timeout=10.0
    )
    for _ in range(16):
        record = await controller.step()
        if record.resolved:
            break
    assert record.phase == "committed" and record.activation == "released"
    assert record.deadline == original.deadline
    assert host.commands.count("stop") == host.commands.count("start") == 1
    assert host.commands.count("daemon-reload") == (1 if original.action == "adopt" else 0)
    assert host.backend._disk().digests == (
        record.target.configuration.digest,
        record.target.selector.digest if record.target.selector else None,
        record.target.unit.digest,
    )
    assert host.transport.calls.count("activate") == 1
    assert host.probe.snapshot.process is not None
    assert host.probe.snapshot.process.argv == host.arguments("target")


async def test_install_confirmation_is_revision_bound_and_repeated_before_start(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = host.install_intent()
    await host.backend.install(record, "target", host.data["target"])
    observed = await host.backend.observe(record)
    assert observed.confirmation == BindingConfirmation(
        record.position,
        "target",
        record.target.configuration.digest,
        record.target.selector.digest if record.target.selector else None,
        record.target.unit.digest,
        record.target.unit.digest,
    )
    calls: list[Path] = []
    confirm = ConfigurationFile.confirm

    def counted(
        file: ConfigurationFile, expected: str, *, guard: Callable[[], None]
    ) -> ConfigurationSnapshot:
        calls.append(file.path)
        return confirm(file, expected, guard=guard)

    monkeypatch.setattr(ConfigurationFile, "confirm", counted)
    started = host.move("activating", "start_target")
    await host.backend.start(started, "target")
    assert calls.count(Path(host.anchor.config_path)) >= 1
    assert calls.count(Path(host.anchor.unit_path)) >= 1
    assert "start" in host.commands


@pytest.mark.parametrize(
    "fault", ["writer", "cgroup", "running", "foreign-config", "foreign-selector", "foreign-unit"]
)
async def test_no_file_effect_without_stopped_owned_prefix(host: Host, fault: str) -> None:
    record = host.install_intent()
    if fault == "writer":
        host.writer = host.journal.writer_lease().__enter__()
    elif fault == "cgroup":
        host.events.write_bytes(b"populated 1\n")
    elif fault == "running":
        host.running("base", origin=True)
    elif fault == "foreign-config":
        Path(host.anchor.config_path).write_bytes(b'{"different":true}')
    elif fault == "foreign-selector":
        with SelectorStore(host.journal.directory.path) as selector:
            selector.replace(
                selector.read(),
                SelectorValue(deployment="f" * 64, installation="f" * 64),
                guard=lambda: None,
            )
    else:
        Path(host.anchor.unit_path).write_bytes(b"[Service]\nExecStart=/foreign\n")
    before = host.backend._disk()
    with pytest.raises((ObservationError, TransactionError)):
        await host.backend.install(record, "target", host.data["target"])
    assert host.backend._disk() == before
    assert host.commands == []


@pytest.mark.parametrize("host", [("switch", True)], indirect=True)
@pytest.mark.parametrize("cancel", [False, True])
async def test_config_prefix_survives_error_or_cancel_and_reconciles(
    host: Host, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    record = host.install_intent()
    replace_selector = SelectorStore.replace

    def interrupted(
        store: SelectorStore,
        expected: SelectorSnapshot | None,
        desired: SelectorValue | None,
        *,
        guard: Callable[[], None],
    ) -> SelectorSnapshot | None:
        raise asyncio.CancelledError if cancel else OSError(errno.EIO, "injected")

    monkeypatch.setattr(SelectorStore, "replace", interrupted)
    with pytest.raises(asyncio.CancelledError if cancel else OSError):
        await host.backend.install(record, "target", host.data["target"])
    disk = host.backend._disk()
    assert disk.configuration.data == CHANGED_CONFIG
    assert disk.selector is not None and disk.selector.value == host.selector("base")
    assert host.record() == record and host.commands == []
    monkeypatch.setattr(SelectorStore, "replace", replace_selector)
    await host.backend.install(record, "target", host.data["target"])
    assert (await host.backend.observe(record)).confirmation is not None


@pytest.mark.parametrize("host", [("adopt", False)], indirect=True)
async def test_unit_prefix_failure_restores_legacy_in_reverse_order(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.install_intent()
    replace_file = ConfigurationFile.replace

    def failed_unit(
        file: ConfigurationFile, expected: str, data: bytes, *, guard: Callable[[], None]
    ) -> ConfigurationSnapshot:
        if file.path == Path(host.anchor.unit_path):
            raise OSError(errno.EIO, "unit write")
        return replace_file(file, expected, data, guard=guard)

    monkeypatch.setattr(ConfigurationFile, "replace", failed_unit)
    controller = ManagedTransactionController(host.control, host.backend, host.clock)
    rolled = await controller.step()
    assert (rolled.phase, rolled.intent) == ("rolling_back", "stop_target")
    assert host.backend._disk().selector is not None
    restored = await controller.step()
    assert (restored.phase, restored.intent) == ("rolling_back", "restore_base")
    assert host.commands == []
    monkeypatch.setattr(ConfigurationFile, "replace", replace_file)
    await host.backend.install(restored, "base", host.data["base"])
    observed = await host.backend.observe(restored)
    assert observed.confirmation is not None and observed.confirmation.role == "base"
    assert host.backend._disk().selector is None
    assert Path(host.anchor.unit_path).read_bytes() == host.legacy.render().encode()


@pytest.mark.parametrize("host", [("adopt", False)], indirect=True)
async def test_accepted_daemon_reload_without_fresh_loaded_evidence_never_starts(
    host: Host,
) -> None:
    record = host.install_intent()
    host.fail_reload = True
    with pytest.raises(ObservationError, match="managed_unit_not_loaded"):
        await host.backend.install(record, "target", host.data["target"])
    assert host.commands == ["daemon-reload"]
    assert (await host.backend.observe(record)).confirmation is None
    started = host.move("activating", "start_target")
    with pytest.raises(ObservationError):
        await host.backend.start(started, "target")
    assert "start" not in host.commands


@pytest.mark.parametrize("host", [("adopt", False)], indirect=True)
@pytest.mark.parametrize("role", ["target", "base"])
async def test_confirmation_order_matches_forward_and_reverse_prefixes(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    role: Role,
) -> None:
    record = host.install_intent()
    if role == "base":
        await host.backend.install(record, "target", host.data["target"])
        host.move("rolling_back", "stop_target", code="backend_failed")
        record = host.move("rolling_back", "restore_base", code="backend_failed")
    order: list[str] = []
    confirm_file, confirm_selector = ConfigurationFile.confirm, SelectorStore.confirm
    replace_file = ConfigurationFile.replace

    def file_confirm(
        file: ConfigurationFile, digest: str, *, guard: Callable[[], None]
    ) -> ConfigurationSnapshot:
        if file.path == Path(host.anchor.config_path):
            order.append("configuration")
        elif file.path == Path(host.anchor.unit_path):
            order.append("unit")
        return confirm_file(file, digest, guard=guard)

    def selector_confirm(
        store: SelectorStore, expected: SelectorSnapshot | None, *, guard: Callable[[], None]
    ) -> SelectorSnapshot | None:
        order.append("selector")
        return confirm_selector(store, expected, guard=guard)

    def file_replace(
        file: ConfigurationFile,
        expected: str,
        data: bytes,
        *,
        guard: Callable[[], None],
    ) -> ConfigurationSnapshot:
        result = replace_file(file, expected, data, guard=guard)
        # replace returns only after both file and directory barriers. Record
        # changed files as well as the already-desired confirm path.
        order.append("configuration" if file.path == Path(host.anchor.config_path) else "unit")
        return result

    monkeypatch.setattr(ConfigurationFile, "confirm", file_confirm)
    monkeypatch.setattr(ConfigurationFile, "replace", file_replace)
    monkeypatch.setattr(SelectorStore, "confirm", selector_confirm)
    await host.backend.install(record, role, host.data[role])
    expected = (
        ["configuration", "selector", "unit"]
        if role == "target"
        else ["unit", "selector", "configuration"]
    )
    assert list(dict.fromkeys(order)) == expected


async def test_broken_target_can_be_stopped_and_base_restored_without_loading_target(
    host: Host,
) -> None:
    record = host.install_intent()
    await host.backend.install(record, "target", host.data["target"])
    host.move("activating", "start_target")
    host.running("target")
    record = host.move("rolling_back", "stop_target", code="backend_failed")
    Path(host.slots["target"].declaration.path).write_bytes(b"broken installed target")
    observation = await host.backend.observe(record)
    assert observation.service == "running" and observation.health is None
    await host.backend.stop(record, "target")
    restored = host.move("rolling_back", "restore_base", code="backend_failed")
    await host.backend.install(restored, "base", host.data["base"])
    assert (await host.backend.observe(restored)).confirmation is not None
    started = host.move("rolling_back", "start_base", code="backend_failed")
    await host.backend.start(started, "base")
    assert host.commands == ["stop", "start"]
    assert host.probe.snapshot.process is not None
    assert host.probe.snapshot.process.argv == host.arguments("base")


@pytest.mark.parametrize("foreign", ["pid", "cgroup", "argv", "boot"])
async def test_stop_never_targets_a_foreign_process(host: Host, foreign: str) -> None:
    record = host.move("draining")
    record = host.move("activating", "stop_base")
    current = host.probe.snapshot
    assert current.process is not None
    if foreign == "boot":
        host.probe.snapshot = replace(current, unit=replace(current.unit, invocation=TARGET_BOOT))
    else:
        changes: dict[str, Any] = (
            {"pid": 999}
            if foreign == "pid"
            else {"cgroup": "/foreign"}
            if foreign == "cgroup"
            else {"argv": ("/foreign/python",)}
        )
        host.probe.snapshot = replace(current, process=replace(current.process, **changes))
    with pytest.raises(ObservationError):
        await host.backend.stop(record, "base")
    assert host.commands == []


@pytest.mark.parametrize("corrupt", ["package", "proof", "origin-boot", "during-readiness"])
async def test_health_requires_actual_role_and_matching_protocol2_proof(
    host: Host, corrupt: str
) -> None:
    record = host.install_intent()
    await host.backend.install(record, "target", host.data["target"])
    host.move("activating", "start_target")
    host.running("target")
    record = host.move("verifying")
    if corrupt == "package":
        (Path(host.slots["target"].package.root) / "__main__.py").write_bytes(b"changed")
    elif corrupt == "origin-boot":
        host.probe.snapshot = replace(
            host.probe.snapshot, unit=replace(host.probe.snapshot.unit, invocation=ORIGIN)
        )
    elif corrupt == "during-readiness":

        def changed_package(request: httpx.Request) -> httpx.Response:
            (Path(host.slots["target"].package.root) / "__main__.py").write_bytes(
                b"changed during HTTP"
            )
            return reply(host.proof().model_dump())

        host.transport.handler = changed_package
    else:
        host.transport.handler = lambda _: reply(
            {**host.proof().model_dump(), "compatibility": "f" * 64}
        )
    with pytest.raises((ObservationError, ManagedDeploymentError)):
        await host.backend.observe(record)
    assert "activate" not in host.transport.calls


@pytest.mark.parametrize("fault", ["deadline", "revision", "anchor", "controller", "target"])
async def test_effect_rechecks_frozen_evidence_and_original_deadline(
    host: Host, fault: str
) -> None:
    record = host.install_intent()
    if fault == "deadline":
        host.clock.elapsed = 130.0
    elif fault == "revision":
        host.move("activating", "start_target")
    elif fault == "anchor":
        (host.journal.directory.path / f"anchor-{host.anchor.digest}.json").write_bytes(b"{}")
    else:
        selected = host.slots["base"] if fault == "controller" else host.slots["target"]
        Path(selected.declaration.path).write_bytes(b"changed installation")
    before = host.backend._disk()
    with pytest.raises((TransactionError, ManagedDeploymentError, TimeoutError)):
        await host.backend.install(record, "target", host.data["target"])
    assert host.backend._disk() == before and host.commands == []


async def test_start_intent_without_process_is_not_repeated_by_new_controller(host: Host) -> None:
    record = host.install_intent()
    await host.backend.install(record, "target", host.data["target"])
    started = host.move("activating", "start_target")
    controller = ManagedTransactionController(host.control, host.backend, host.clock)
    result = await controller.step()
    assert result.phase == "rolling_back" and result.code == "ambiguous_start"
    assert result.deadline == started.deadline and "start" not in host.commands


async def test_activation_timeout_is_only_reobserved_not_resent(host: Host) -> None:
    record = host.install_intent()
    await host.backend.install(record, "target", host.data["target"])
    host.move("activating", "start_target")
    host.running("target")
    host.move("verifying")
    record = host.move("committed", proof=host.proof())

    def lost_reply(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("injected")

    host.transport.handler = lost_reply
    await host.backend.activate(record, "target")
    await host.backend.activate(record, "target")
    assert host.transport.calls.count("activate") == 1
    assert host.record() == record
