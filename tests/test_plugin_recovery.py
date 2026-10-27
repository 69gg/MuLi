"""Offline plugin recovery uses real locks/config CAS and bounded service evidence."""

from __future__ import annotations

import json
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

import pytest
from fastapi import FastAPI
from test_installation import Installation
from test_installation import installation as installation
from test_linux_transaction import FakeBus
from test_managed_boot import Host, _receipt
from test_managed_boot import host as _managed_host

from muli import plugin_recovery
from muli.config import ConfigFile
from muli.format_evidence import read_format_evidence
from muli.management_transaction import (
    ClockReading,
    Deadline,
    ProjectionReceipt,
    ServiceIdentity,
    TransactionError,
    TransactionJournal,
    Transition,
)
from muli.platforms.installation import InstallationManifest
from muli.platforms.linux_transaction import ServiceSnapshot, SystemdProbe, UnitSnapshot
from muli.platforms.managed_deployment import SelectorStore, SelectorValue
from muli.platforms.managed_probe import ManagedProbeResult, ManagedSystemdProbe
from muli.platforms.transaction_files import ConfigurationFile
from muli.plugins import PluginManager
from muli.runtime import Runtime
from muli.service import UserService
from muli.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux stopped-service recovery")
managed_host = _managed_host


@dataclass
class Offline:
    service: UserService
    root: Path
    database: Path
    bus: FakeBus


@pytest.fixture(autouse=True)
def no_service_control(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected(service: UserService, action: str) -> Never:
        raise AssertionError("plugin configuration must not control a service")

    monkeypatch.setattr(UserService, "action", unexpected)


@pytest.fixture
def offline(installation: Installation, monkeypatch: pytest.MonkeyPatch) -> Offline:
    service = installation.service
    service.config_path.write_text(
        json.dumps(
            {
                "plugins": {"enabled": True, "disabled": ["keep-disabled"]},
                "persona": {"name": "原称呼"},
                "models": {"main": {"api_key": "private-model-secret"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    database = service.data_dir / "muli.sqlite3"
    database.write_bytes(b"ordinary recovery must not open this business database")
    bus = FakeBus(service)
    bus.rows.update(
        {
            "ActiveState": ("s", "inactive"),
            "SubState": ("s", "dead"),
            "MainPID": ("u", 0),
            "ControlGroup": ("s", ""),
        }
    )
    probe = SystemdProbe(runner=bus)
    monkeypatch.setattr(plugin_recovery, "SystemdProbe", lambda: probe)

    return Offline(service, service.data_dir / "management/transaction-v1", database, bus)


async def test_explicit_disable_preserves_config_and_reopened_manager_loads_only_healthy_plugin(
    offline: Offline,
) -> None:
    service = offline.service
    directory = service.config_path.parent / "plugins"
    marker = directory / "bad-plugin-executed"
    for identifier in ("broken", "healthy"):
        entry = directory / identifier
        entry.mkdir(parents=True)
        (entry / "plugin.json").write_text(
            json.dumps({"id": identifier, "api_version": 1}), encoding="utf-8"
        )
    (directory / "broken/main.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "raise RuntimeError('broken plugin must not run')\n",
        encoding="utf-8",
    )
    (directory / "healthy/main.py").write_text(
        "from muli.plugins import PluginContext\n\n"
        "def setup(api: PluginContext) -> None:\n"
        "    api.append_prompt('healthy-plugin-ready')\n",
        encoding="utf-8",
    )
    original = json.loads(service.config_path.read_bytes())
    database_bytes = offline.database.read_bytes()
    result = await plugin_recovery.set_plugin_disabled(service, "broken", True)
    assert result == {
        "id": "broken",
        "disabled": True,
        "changed": True,
        "code": "plugin_state_saved",
        "effective": "next_start",
    }
    expected = {
        **original,
        "plugins": {**original["plugins"], "disabled": ["keep-disabled", "broken"]},
    }
    assert json.loads(service.config_path.read_bytes()) == expected
    assert offline.database.read_bytes() == database_bytes
    assert not marker.exists()
    assert not (offline.root / "installation.json").exists()
    assert not (offline.root / "state.json").exists()
    offline.database.unlink()
    runtime = Runtime(ConfigFile(service.config_path), service.data_dir)
    manager = PluginManager(runtime, FastAPI(), directory)
    try:
        await manager.load()
        assert [(record["id"], record["status"]) for record in manager.inventory()] == [
            ("broken", "disabled"),
            ("healthy", "loaded"),
        ]
        assert [context.identifier for context in manager.contexts] == ["healthy"]
        assert "healthy-plugin-ready" in manager.contexts[0].prompt
        assert not marker.exists()
        assert runtime.config.settings.persona.name == "原称呼"
        assert runtime.config.settings.models.main.api_key == "private-model-secret"
    finally:
        await manager.close()
        await runtime.close()


async def test_enable_removes_only_the_requested_member_and_repeated_state_is_read_only(
    offline: Offline, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = offline.service
    values = json.loads(service.config_path.read_bytes())
    values["plugins"]["disabled"] = ["keep-disabled", "broken", "keep-disabled", "broken"]
    service.config_path.write_text(json.dumps(values), encoding="utf-8")
    result = await plugin_recovery.set_plugin_disabled(service, "broken", False)
    assert result["changed"] and result["disabled"] is False
    values["plugins"]["disabled"] = ["keep-disabled", "keep-disabled"]
    assert json.loads(service.config_path.read_bytes()) == values
    before, identity = service.config_path.read_bytes(), service.config_path.stat()

    def unexpected_replace(*args: Any, **kwargs: Any) -> Never:
        raise AssertionError("an idempotent command must not rewrite config")

    monkeypatch.setattr(ConfigurationFile, "replace", unexpected_replace)
    repeated = await plugin_recovery.set_plugin_disabled(service, "broken", False)
    after = service.config_path.stat()
    assert not repeated["changed"] and repeated["code"] == "plugin_state_unchanged"
    assert service.config_path.read_bytes() == before
    assert (after.st_ino, after.st_mtime_ns) == (identity.st_ino, identity.st_mtime_ns)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ActiveState", "active"),
        ("ActiveState", "activating"),
        ("Job", [1, "/org/freedesktop/systemd1/job/1"]),
        ("NeedDaemonReload", True),
        ("DropInPaths", ["/untrusted/override.conf"]),
    ],
)
async def test_active_or_unverified_service_never_changes_configuration(
    offline: Offline, field: str, value: Any
) -> None:
    signature, _ = offline.bus.rows[field]
    offline.bus.rows[field] = (signature, value)
    before = offline.service.config_path.read_bytes()
    with pytest.raises(TransactionError):
        await plugin_recovery.set_plugin_disabled(offline.service, "broken", True)
    assert offline.service.config_path.read_bytes() == before


async def test_manager_observation_failure_is_not_an_inactive_service(offline: Offline) -> None:
    offline.bus.failure = True
    before = offline.service.config_path.read_bytes()
    with pytest.raises(TransactionError) as raised:
        await plugin_recovery.set_plugin_disabled(offline.service, "broken", True)
    assert "private" not in str(raised.value) and "secret" not in str(raised.value)
    assert offline.service.config_path.read_bytes() == before


@pytest.mark.parametrize("lock", ["control", "writer"])
async def test_existing_authority_owners_reject_changes(offline: Offline, lock: str) -> None:
    before = offline.service.config_path.read_bytes()
    with TransactionJournal(offline.root, offline.database) as journal:
        with journal.locked() if lock == "control" else journal.writer_lease():
            with pytest.raises(TransactionError, match="lock_busy"):
                await plugin_recovery.set_plugin_disabled(offline.service, "broken", True)
    assert offline.service.config_path.read_bytes() == before


async def test_configuration_replace_runs_under_both_real_locks(
    offline: Offline, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ConfigurationFile.replace

    def replace_locked(source: ConfigurationFile, *args: Any, **kwargs: Any) -> Any:
        with TransactionJournal(offline.root, offline.database) as journal:
            for lock in (journal.locked(), journal.writer_lease()):
                with pytest.raises(TransactionError, match="lock_busy"):
                    with lock:
                        pass
        return original(source, *args, **kwargs)

    monkeypatch.setattr(ConfigurationFile, "replace", replace_locked)
    assert (await plugin_recovery.set_plugin_disabled(offline.service, "broken", True))["changed"]


@pytest.mark.parametrize("path", ["config", "database", "unit"])
async def test_symlinked_files_cannot_choose_recovery_authority(
    offline: Offline, path: str
) -> None:
    target = {
        "config": offline.service.config_path,
        "database": offline.database,
        "unit": offline.service.path,
    }[path]
    moved = target.with_name(target.name + ".original")
    target.rename(moved)
    target.symlink_to(moved)
    before = offline.service.config_path.read_bytes()
    with pytest.raises(TransactionError):
        await plugin_recovery.set_plugin_disabled(offline.service, "broken", True)
    assert offline.service.config_path.read_bytes() == before
    assert target.is_symlink()


async def test_competing_configuration_write_is_retained_and_never_overwritten(
    offline: Offline, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ConfigurationFile.replace
    competing = b'{"persona":{"name":"concurrent owner"}}'

    def race(source: ConfigurationFile, *args: Any, **kwargs: Any) -> Any:
        offline.service.config_path.write_bytes(competing)
        return original(source, *args, **kwargs)

    monkeypatch.setattr(ConfigurationFile, "replace", race)
    with pytest.raises(TransactionError):
        await plugin_recovery.set_plugin_disabled(offline.service, "broken", True)
    assert offline.service.config_path.read_bytes() == competing


@pytest.mark.parametrize("late", [False, True])
async def test_populated_cgroup_rejects_even_a_late_change_before_replace(
    offline: Offline, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late: bool
) -> None:
    probe = plugin_recovery.SystemdProbe()
    probe.cgroup_root = tmp_path / "cgroups"
    directory = probe.cgroup_root / "stopped.scope"
    directory.mkdir(parents=True)
    events = directory / "cgroup.events"
    events.write_text("populated 0\n" if late else "populated 1\n", encoding="ascii")
    offline.bus.rows["ControlGroup"] = ("s", "/stopped.scope")
    if late:
        original = ConfigurationFile.replace

        def populated(source: ConfigurationFile, *args: Any, **kwargs: Any) -> Any:
            events.write_text("populated 1\n", encoding="ascii")
            return original(source, *args, **kwargs)

        monkeypatch.setattr(ConfigurationFile, "replace", populated)
    before = offline.service.config_path.read_bytes()
    with pytest.raises(TransactionError, match="plugin_service_not_stopped"):
        await plugin_recovery.set_plugin_disabled(offline.service, "broken", True)
    assert offline.service.config_path.read_bytes() == before


async def test_new_orphan_selector_cannot_appear_between_discovery_and_config_replace(
    offline: Offline, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ConfigurationFile.replace

    def selected(source: ConfigurationFile, *args: Any, **kwargs: Any) -> Any:
        (offline.root / "selector.json").write_bytes(b"untrusted new selector")
        return original(source, *args, **kwargs)

    monkeypatch.setattr(ConfigurationFile, "replace", selected)
    before = offline.service.config_path.read_bytes()
    with pytest.raises(TransactionError):
        await plugin_recovery.set_plugin_disabled(offline.service, "broken", True)
    assert offline.service.config_path.read_bytes() == before


@pytest.mark.parametrize("phase", ["prepared", "recovery_required"])
async def test_unresolved_legacy_journal_cannot_be_bypassed(offline: Offline, phase: str) -> None:
    from test_installation import prepare

    manifest = InstallationManifest.capture(offline.service, offline.database)
    before = offline.service.config_path.read_bytes()
    with TransactionJournal(offline.root, offline.database) as journal:
        with journal.locked() as control:
            manifest.publish(control)
            record = prepare(control, manifest)
            if phase == "recovery_required":
                control.advance(
                    record.position, Transition(phase="recovery_required", code="foreign_state")
                )
        with pytest.raises(TransactionError, match="transaction_in_progress"):
            await plugin_recovery.set_plugin_disabled(offline.service, "broken", True)
    assert offline.service.config_path.read_bytes() == before


@pytest.mark.parametrize("projected", [False, True])
async def test_resolved_legacy_record_requires_real_matching_projection(
    offline: Offline, projected: bool
) -> None:
    offline.database.unlink()
    with closing(Store.open_runtime(offline.database, default_mode="auto")) as store:
        evidence = read_format_evidence(store.db)
        manifest = InstallationManifest.capture(offline.service, offline.database)
        with TransactionJournal(offline.root, offline.database) as journal:
            with journal.locked() as control, journal.writer_lease() as writer:
                manifest.publish(control)
                record = control.prepare(
                    request_id="offline-plugin-recovery",
                    action="restart",
                    identity=ServiceIdentity(
                        installation=manifest.digest,
                        service=manifest.service_digest,
                        store_instance=evidence.store_instance,
                        main_session=evidence.main_session,
                        origin_boot="offline-origin",
                    ),
                    base=offline.service.config_path.read_bytes(),
                    target=offline.service.config_path.read_bytes(),
                    deadline=Deadline(
                        created=ClockReading(boot_id="offline-kernel", wall=1.0, monotonic=1.0),
                        seconds=180.0,
                        rollback_reserve=60.0,
                    ),
                    writer=writer,
                )
                record = control.advance(
                    record.position, Transition(phase="aborted", code="cancelled")
                )
            if projected:
                store.put(
                    "management_projection",
                    {
                        "id": "transaction-v1",
                        **ProjectionReceipt(
                            **record.position.model_dump(),
                            store_instance=record.identity.store_instance,
                        ).model_dump(),
                    },
                )
    before = offline.service.config_path.read_bytes()
    if projected:
        assert (await plugin_recovery.set_plugin_disabled(offline.service, "broken", True))[
            "changed"
        ]
    else:
        with pytest.raises(TransactionError, match="plugin_projection_required"):
            await plugin_recovery.set_plugin_disabled(offline.service, "broken", True)
        assert offline.service.config_path.read_bytes() == before


@pytest.fixture
def managed_service(managed_host: Host, monkeypatch: pytest.MonkeyPatch) -> UserService:
    host = managed_host
    record = host.record("committed", "none", activation="released")
    _receipt(host, record)
    service = UserService(
        Path(host.anchor.config_path),
        Path(host.anchor.data_dir),
        Path(host.anchor.unit_path).parent,
        platform="linux",
        executable=host.target.lexical_python,
    )
    unit = UnitSnapshot(
        name=service.unit,
        load_state="loaded",
        active_state="inactive",
        sub_state="dead",
        fragment=str(service.path),
        source="",
        drop_ins=(),
        needs_reload=False,
        invocation="0" * 32,
        job=(0, "/"),
        service_type="simple",
        pid=0,
        cgroup="",
        executable=service.executable,
        argv=tuple(service._arguments()),
        ignore_failure=False,
        kill_mode="control-group",
        send_sigkill=True,
    )

    async def observe(*args: Any, **kwargs: Any) -> ManagedProbeResult:
        return ManagedProbeResult(
            ServiceSnapshot(unit, None),
            (),
            frozenset({record.target.unit.digest}),
            record.target.unit.digest,
        )

    monkeypatch.setattr(ManagedSystemdProbe, "read_managed", observe)
    return service


async def test_resolved_managed_change_preserves_anchor_selector_journal_and_projection(
    managed_host: Host, managed_service: UserService
) -> None:
    root = managed_host.journal.directory.path
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    database = managed_host.journal.database.read_bytes()
    result = await plugin_recovery.set_plugin_disabled(
        managed_service, "broken", True, managed_root=root
    )
    assert result["changed"]
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == before
    assert managed_host.journal.database.read_bytes() == database
    assert json.loads(managed_service.config_path.read_bytes())["plugins"]["disabled"] == ["broken"]


@pytest.mark.parametrize(
    "mismatch", ["missing_projection", "stale_projection", "config", "selector", "slot"]
)
async def test_managed_recovery_requires_exact_paths_slot_selection_and_projection(
    managed_host: Host,
    managed_service: UserService,
    mismatch: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, service = managed_host.journal.directory.path, managed_service
    if mismatch == "missing_projection":
        managed_host.mutate("DELETE FROM objects WHERE kind='management_projection'")
    elif mismatch == "stale_projection":
        _receipt(managed_host, managed_host.journal.read_current(), revision=99)
    elif mismatch == "config":
        service = UserService(
            service.config_path.with_name("other.json"),
            service.data_dir,
            service.path.parent,
            platform="linux",
            executable=service.executable,
        )
    elif mismatch == "selector":
        with SelectorStore(root) as selector:
            selector.replace(
                selector.read(),
                SelectorValue(
                    deployment=managed_host.anchor.digest, installation=managed_host.base.digest
                ),
                guard=lambda: None,
            )
    else:

        def wrong_slot(*args: Any) -> Never:
            raise ValueError("managed_process_mismatch")

        monkeypatch.setattr(plugin_recovery, "verify_current_slot", wrong_slot)
    before = managed_service.config_path.read_bytes()
    with pytest.raises(TransactionError):
        await plugin_recovery.set_plugin_disabled(service, "broken", True, managed_root=root)
    assert managed_service.config_path.read_bytes() == before


async def test_unresolved_managed_change_does_not_recover_or_touch_configuration(
    managed_host: Host, managed_service: UserService
) -> None:
    managed_host.record("activating", "start_target")
    before = managed_service.config_path.read_bytes()
    with pytest.raises(TransactionError, match="transaction_in_progress"):
        await plugin_recovery.set_plugin_disabled(
            managed_service, "broken", True, managed_root=managed_host.journal.directory.path
        )
    assert managed_service.config_path.read_bytes() == before


@pytest.mark.parametrize("identifier", ["../other", "a/b", "UPPER", "", "a" * 65])
async def test_invalid_identifiers_are_rejected_before_filesystem_work(
    tmp_path: Path, identifier: str
) -> None:
    service = UserService(
        tmp_path / "missing/config.json", tmp_path / "missing/data", platform="linux"
    )
    with pytest.raises(TransactionError, match="plugin_identifier_invalid"):
        await plugin_recovery.set_plugin_disabled(service, identifier, True)
    assert not (tmp_path / "missing").exists()


async def test_other_platforms_do_not_claim_a_stop_proof_or_change_files(tmp_path: Path) -> None:
    service = UserService(tmp_path / "config.json", tmp_path / "data", platform="darwin")
    with pytest.raises(TransactionError, match="platform_unsupported"):
        await plugin_recovery.set_plugin_disabled(service, "broken", True)
    assert not service.config_path.exists() and not service.data_dir.exists()
