from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from test_managed_launcher import _slot
from test_managed_transaction import deadline, proof

import muli
from muli.config import Settings
from muli.legacy_transaction_bridge import prepare_legacy_after_managed
from muli.managed_journal import RoleSnapshots, advance_managed, prepare_managed, read_snapshots
from muli.managed_transaction import (
    RoleBinding,
    ServiceIdentityV2,
    TransactionRecordV2,
    TransitionV2,
    raw_snapshot,
)
from muli.management_transaction import (
    FixedLock,
    ProjectionReceipt,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
    _snapshot,
)
from muli.platforms.installation import InstallationManifest
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    SelectorStore,
    SelectorValue,
)
from muli.service import UserService
from muli.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux adoption recovery bridge")


@dataclass(slots=True)
class Recovered:
    journal: TransactionJournal
    writer: FixedLock
    store: Store
    manifest: InstallationManifest
    previous: TransactionRecordV2
    target: bytes
    selector: SelectorValue

    def project(self) -> None:
        receipt = ProjectionReceipt(
            **self.previous.position.model_dump(), store_instance=self.store.instance_id
        )
        self.store.event(
            "test.projection",
            {},
            objects=[("management_projection", {"id": "transaction-v1", **receipt.model_dump()})],
        )

    def prepare(
        self,
        *,
        action: Literal["configure", "restart"] = "configure",
        target: bytes | None = None,
    ) -> TransactionRecord:
        if target is None:
            target = (
                self.target
                if action == "configure"
                else Path(self.manifest.config_path).read_bytes()
            )
        with self.journal.locked() as control:
            return prepare_legacy_after_managed(
                control,
                store=self.store,
                writer=self.writer,
                manifest=self.manifest,
                origin_boot=self.previous.health.boot if self.previous.health else "running-legacy",
                request_id="next-" + action,
                action=action,
                target=target,
                deadline=deadline(),
            )


@pytest.fixture
def recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[Recovered]:
    slot = _slot(tmp_path, "retained")
    monkeypatch.setattr(sys, "executable", slot.lexical_python)
    monkeypatch.setattr(sys, "prefix", slot.prefix)
    monkeypatch.setattr(muli, "__file__", str(Path(slot.package.root) / "__init__.py"))
    config = tmp_path / "config.json"
    original = Settings().model_dump_json(indent=2).encode()
    config.write_bytes(original)
    config.chmod(0o600)
    data = tmp_path / "data"
    store = Store.open_runtime(data / "muli.sqlite3", default_mode="auto", durable=True)
    service = UserService(
        config, data, tmp_path / "units", platform="linux", executable=slot.lexical_python
    )
    service.path.parent.mkdir()
    unit = service.render().encode()
    service.path.write_bytes(unit)
    root = data / "management/transaction-v1"
    try:
        with (
            TransactionJournal(root, data / "muli.sqlite3") as journal,
            journal.writer_lease() as writer,
        ):
            with journal.locked() as control, DeploymentStore(root) as deployments:
                deployments.publish_slot(slot, guard=control.check)
                launcher = deployments.publish_launcher(guard=control.check)
                managed_unit = b"[Service]\nExecStart=/fixture/stable-launcher\n"
                anchor = DeploymentAnchor(
                    config_path=str(config),
                    data_dir=str(data),
                    database_path=str(journal.database),
                    unit_path=str(service.path),
                    journal_root=str(root),
                    controller_slot=slot.digest,
                    launcher=launcher,
                    managed_unit=raw_snapshot(managed_unit, 4096),
                )
                deployments.publish_anchor(anchor, guard=control.check)
                selector = SelectorValue(deployment=anchor.digest, installation=slot.digest)
                base = RoleBinding(
                    entry="legacy",
                    installation=slot.digest,
                    compatibility=slot.compatibility_digest,
                    configuration=_snapshot(original, journal.limits.max_snapshot_bytes),
                    selector=None,
                    unit=raw_snapshot(unit, journal.limits.max_snapshot_bytes),
                )
                target = RoleBinding(
                    entry="managed",
                    installation=slot.digest,
                    compatibility=slot.compatibility_digest,
                    configuration=base.configuration,
                    selector=_snapshot(selector.canonical(), 4096),
                    unit=anchor.managed_unit,
                )
                assert store.format_state is not None
                record = prepare_managed(
                    control,
                    request_id="old-adopt",
                    action="adopt",
                    identity=ServiceIdentityV2(
                        deployment=anchor.digest,
                        store_instance=store.instance_id,
                        main_session=store.main_session()["id"],
                        origin_boot="running-legacy",
                    ),
                    base=base,
                    target=target,
                    base_data=RoleSnapshots(original, None, unit),
                    target_data=RoleSnapshots(original, selector.canonical(), managed_unit),
                    observed_format=store.format_state,
                    base_manifest=slot.compatibility,
                    target_manifest=slot.compatibility,
                    deadline=deadline(),
                    writer=writer,
                )
                if getattr(request, "param", "aborted") == "rolled_back":
                    for transition in (
                        TransitionV2(phase="draining"),
                        TransitionV2(phase="activating", intent="stop_base"),
                        TransitionV2(
                            phase="rolling_back", intent="stop_target", code="health_failed"
                        ),
                        TransitionV2(phase="rolling_back", intent="restore_base"),
                        TransitionV2(phase="rolling_back", intent="start_base"),
                        TransitionV2(phase="rolling_back", intent="verify_base"),
                    ):
                        record = advance_managed(control, record.position, transition)
                    record = advance_managed(
                        control,
                        record.position,
                        TransitionV2(
                            phase="rolled_back",
                            proof=proof(record, role="base", boot="recovered-base"),
                        ),
                    )
                    record = advance_managed(
                        control,
                        record.position,
                        TransitionV2(
                            phase="rolled_back",
                            activation="released",
                            proof=proof(
                                record,
                                role="base",
                                boot="recovered-base",
                                admission="released",
                            ),
                        ),
                    )
                else:
                    record = advance_managed(
                        control, record.position, TransitionV2(phase="aborted", code="cancelled")
                    )
            manifest = InstallationManifest.capture(
                service, journal.database, journal=journal.limits
            )
            value = Recovered(
                journal,
                writer,
                store,
                manifest,
                record,
                Settings(default_mode="ask").model_dump_json().encode(),
                selector,
            )
            value.project()
            try:
                yield value
            finally:
                store.close()
    finally:
        store.close()


@pytest.mark.parametrize("recovered", ["aborted", "rolled_back"], indirect=True)
def test_failed_adoption_can_prepare_a_fresh_legacy_generation(recovered: Recovered) -> None:
    previous = recovered.previous
    record = recovered.prepare()
    assert record.protocol == 1 and record.generation != previous.generation
    assert recovered.journal.read() == record
    assert recovered.journal.read_current() == record
    assert record.identity.installation == recovered.manifest.digest
    assert record.identity.installation != previous.base.installation
    assert record.identity.store_instance == previous.identity.store_instance
    assert recovered.journal.snapshot(record, "target") == recovered.target
    assert read_snapshots(recovered.journal, previous, "base").selector is None
    assert InstallationManifest.load(recovered.journal.directory.path) == recovered.manifest


@pytest.mark.parametrize("recovered", ["aborted", "rolled_back"], indirect=True)
@pytest.mark.parametrize("action", ["configure", "restart"])
def test_projected_hot_configuration_becomes_the_next_legacy_base(
    recovered: Recovered, action: Literal["configure", "restart"]
) -> None:
    historical = read_snapshots(recovered.journal, recovered.previous, "base").configuration
    current = Settings(default_mode="yolo").model_dump_json(indent=2).encode()
    Path(recovered.manifest.config_path).write_bytes(current)
    record = recovered.prepare(action=action)
    assert record.base == _snapshot(current, recovered.journal.limits.max_snapshot_bytes)
    assert recovered.journal.snapshot(record, "base") == current
    assert recovered.journal.snapshot(record, "target") == (
        recovered.target if action == "configure" else current
    )
    assert read_snapshots(recovered.journal, recovered.previous, "base").configuration == historical
    assert record.base != recovered.previous.base.configuration


@pytest.mark.parametrize("recovered", ["aborted", "rolled_back"], indirect=True)
def test_hot_configuration_does_not_bypass_exact_projection(recovered: Recovered) -> None:
    Path(recovered.manifest.config_path).write_bytes(
        Settings(default_mode="yolo").model_dump_json().encode()
    )
    recovered.store.db.execute("DELETE FROM objects WHERE kind='management_projection'")
    recovered.store.db.commit()
    with pytest.raises(TransactionError, match="projection_required"):
        recovered.prepare(action="restart")
    assert recovered.journal.read_current() == recovered.previous


def test_restart_cannot_replace_hot_configuration_with_the_historical_base(
    recovered: Recovered,
) -> None:
    historical = read_snapshots(recovered.journal, recovered.previous, "base").configuration
    current = Settings(default_mode="yolo").model_dump_json().encode()
    Path(recovered.manifest.config_path).write_bytes(current)
    with pytest.raises(TransactionError, match="restart_changes_configuration"):
        recovered.prepare(action="restart", target=historical)
    assert recovered.journal.read_current() == recovered.previous
    assert Path(recovered.manifest.config_path).read_bytes() == current


def test_configuration_change_during_preparation_preserves_the_previous_generation(
    recovered: Recovered, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = Settings(default_mode="yolo").model_dump_json().encode()
    config_path = Path(recovered.manifest.config_path)
    config_path.write_bytes(current)
    replace = recovered.journal.directory.replace

    def changed(name: str, data: bytes) -> None:
        replace(name, data)
        if name.endswith("-target.json"):
            config_path.write_bytes(recovered.target)

    monkeypatch.setattr(recovered.journal.directory, "replace", changed)
    with pytest.raises(ValueError, match="configuration_changed"):
        recovered.prepare()
    assert recovered.journal.read_current() == recovered.previous
    assert config_path.read_bytes() == recovered.target


def test_target_budget_precedes_settings_parsing(
    recovered: Recovered,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("oversized target reached Settings parsing")

    monkeypatch.setattr(Settings, "model_validate_json", forbidden)
    recovered.target = b" " * (recovered.journal.limits.max_snapshot_bytes + 1)
    with pytest.raises(TransactionError, match="snapshot_budget"):
        recovered.prepare()
    assert recovered.journal.read_current() == recovered.previous


@pytest.mark.parametrize(
    "mutation", ["projection", "config", "unit", "selector", "snapshot", "format"]
)
def test_bridge_refuses_changed_evidence_without_replacing_state(
    recovered: Recovered,
    mutation: str,
) -> None:
    root = recovered.journal.directory.path
    before = (root / "state.json").read_bytes()
    if mutation == "projection":
        recovered.store.db.execute("DELETE FROM objects WHERE kind='management_projection'")
        recovered.store.db.commit()
    elif mutation == "config":
        Path(recovered.manifest.config_path).write_bytes(b"invalid configuration")
    elif mutation == "unit":
        Path(recovered.manifest.unit_path).write_bytes(b"[Service]\nExecStart=/foreign\n")
    elif mutation == "selector":
        with SelectorStore(root) as selector:
            selector.replace(None, recovered.selector, guard=recovered.writer.check)
    elif mutation == "snapshot":
        (root / f"{recovered.previous.generation}-base-unit.bin").write_bytes(b"changed")
    else:
        recovered.store.db.execute("DELETE FROM runtime_state WHERE name='format_state_v1'")
        recovered.store.db.commit()
    with pytest.raises((ValueError, OSError)):
        recovered.prepare()
    assert (root / "state.json").read_bytes() == before


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize(
    "suffix", ["-base.json", "-target.json", "installation.json", "state.json"]
)
def test_interrupted_bridge_preserves_recoverable_old_or_new_evidence(
    recovered: Recovered,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    suffix: str,
) -> None:
    class Crash(BaseException):
        pass

    replace = recovered.journal.directory.replace

    def interrupted(name: str, data: bytes) -> None:
        if name.endswith(suffix) and when == "before":
            raise Crash
        replace(name, data)
        if name.endswith(suffix) and when == "after":
            raise Crash

    monkeypatch.setattr(recovered.journal.directory, "replace", interrupted)
    with pytest.raises(Crash):
        recovered.prepare()
    monkeypatch.undo()
    current = recovered.journal.read_current()
    assert current is not None
    if isinstance(current, TransactionRecordV2):
        assert current == recovered.previous
        assert read_snapshots(recovered.journal, current, "base").selector is None
    else:
        assert recovered.journal.snapshot(current, "target") == recovered.target
        assert InstallationManifest.load(recovered.journal.directory.path) == recovered.manifest
    assert read_snapshots(recovered.journal, recovered.previous, "target").selector is not None
