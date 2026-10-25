from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import muli
from muli.format_admission import CURRENT_FORMAT_STATE
from muli.managed_transaction import (
    HealthProofV2,
    RoleBinding,
    ServiceIdentityV2,
    TransactionRecordV2,
    format_digest,
    raw_snapshot,
)
from muli.management_transaction import ClockReading, Deadline, _snapshot
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    ManagedDeploymentError,
    SelectorStore,
    SelectorValue,
    SlotIdentity,
    register_local_slot,
    verify_current_slot,
    verify_slot_files,
)
from muli.platforms.managed_launcher import LaunchError, inspect_repair, resolve_entry

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux retained deployment entry")

_CONFIG = b'{"setting":"same configuration for both slots"}'
_UNIT = b"[Service]\nExecStart=/retained/launcher\n"
_STATE = CURRENT_FORMAT_STATE


def _write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _slot(root: Path, name: str) -> SlotIdentity:
    prefix = root / name
    (prefix / "bin").mkdir(parents=True)
    python = prefix / "bin/python"
    python.symlink_to(Path(sys.executable).resolve())
    home = Path(getattr(sys, "_base_executable", sys.executable)).resolve().parent
    (prefix / "pyvenv.cfg").write_text(
        f"home = {home}\ninclude-system-site-packages = false\n", encoding="utf-8"
    )
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    package = prefix / "lib" / version / "site-packages/muli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    observation = (
        "import json,os,sys\n"
        f"print(json.dumps(dict(slot={name!r},pid=os.getpid(),python=sys.executable,"
        "prefix=sys.prefix,args=sys.argv,package=__file__)))\n"
    )
    (package / "__main__.py").write_text(observation, encoding="utf-8")
    (package / "managed_host.py").write_text(observation, encoding="utf-8")
    formats = {
        name: {"reads": [version], "writes": version}
        for name, version in CURRENT_FORMAT_STATE.formats.model_dump().items()
    }
    (package / "compatibility.json").write_text(
        json.dumps(
            {
                "metadata_version": 1,
                "distribution": "muli-agent",
                "release": "2.22.0",
                "handoff_protocol": 2,
                "requires_migration": False,
                "formats": formats,
            }
        ),
        encoding="utf-8",
    )
    return register_local_slot(prefix, python, package)


@dataclass(slots=True)
class Deployment:
    root: Path
    anchor: DeploymentAnchor
    base: SlotIdentity
    target: SlotIdentity
    base_selector: SelectorValue
    target_selector: SelectorValue

    def record(
        self,
        phase: str = "activating",
        intent: str = "start_target",
        *,
        activation: str = "pending",
        role: str = "target",
    ) -> TransactionRecordV2:
        def binding(slot: SlotIdentity, selector: SelectorValue) -> RoleBinding:
            return RoleBinding(
                entry="managed",
                installation=slot.digest,
                compatibility=slot.compatibility_digest,
                configuration=_snapshot(_CONFIG, 16384),
                selector=_snapshot(selector.canonical(), 16384),
                unit=raw_snapshot(_UNIT, 16384),
            )

        base = binding(self.base, self.base_selector)
        target = binding(self.target, self.target_selector)
        health = None
        if phase in {"committed", "rolled_back"}:
            selected = base if role == "base" else target
            health = HealthProofV2.model_validate(
                {
                    "protocol": 2,
                    "generation": "a" * 32,
                    "challenge": "rollback-nonce" if role == "base" else "target-nonce",
                    "boot": "new-boot",
                    "role": role,
                    "deployment": self.anchor.digest,
                    "installation": selected.installation,
                    "compatibility": selected.compatibility,
                    "configuration": selected.configuration.digest,
                    "selector": selected.selector.digest if selected.selector else None,
                    "unit": selected.unit.digest,
                    "store_instance": "store-one",
                    "main_session": "main-one",
                    "format_digest": format_digest(_STATE),
                    "core_ready": True,
                    "admission": activation if activation != "pending" else "paused",
                }
            )
        rollback = phase in {"rolling_back", "rolled_back"}
        record = TransactionRecordV2.model_validate(
            {
                "protocol": 2,
                "request_id": "request-one",
                "generation": "a" * 32,
                "revision": 1,
                "action": "switch",
                "identity": ServiceIdentityV2(
                    deployment=self.anchor.digest,
                    store_instance="store-one",
                    main_session="main-one",
                    origin_boot="origin-boot",
                ),
                "base": base,
                "target": target,
                "observed_format": _STATE,
                "deadline": Deadline(
                    created=ClockReading(boot_id="kernel-one", wall=1.0, monotonic=1.0),
                    seconds=180.0,
                    rollback_reserve=60.0,
                ),
                "phase": phase,
                "intent": intent,
                "challenge": "target-nonce",
                "rollback_challenge": "rollback-nonce" if rollback else None,
                "rollback_attempted": rollback,
                "health": health,
                "activation": activation,
            }
        )
        _write_private(self.root / "state.json", record.model_dump_json().encode())
        return record

    def select(self, value: SelectorValue) -> None:
        with SelectorStore(self.root) as selector:
            selector.replace(selector.read(), value, guard=lambda: None)


@pytest.fixture
def deployment(tmp_path: Path) -> Deployment:
    base, target = _slot(tmp_path, "base"), _slot(tmp_path, "target")
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    with DeploymentStore(root) as store:
        store.publish_slot(base, guard=lambda: None)
        store.publish_slot(target, guard=lambda: None)
        launcher = store.publish_launcher(guard=lambda: None)
        anchor = DeploymentAnchor(
            config_path=str(tmp_path / "settings.json"),
            data_dir=str(tmp_path / "data"),
            database_path=str(tmp_path / "data/muli.db"),
            unit_path=str(tmp_path / "units/muli.service"),
            journal_root=str(root),
            controller_slot=base.digest,
            launcher=launcher,
            managed_unit=raw_snapshot(_UNIT, 16384),
        )
        store.publish_anchor(anchor, guard=lambda: None)
    result = Deployment(
        root,
        anchor,
        base,
        target,
        SelectorValue(deployment=anchor.digest, installation=base.digest),
        SelectorValue(deployment=anchor.digest, installation=target.digest),
    )
    result.select(result.target_selector)
    result.record()
    return result


def test_slot_identity_preserves_lexical_entry_for_shared_binary(deployment: Deployment) -> None:
    assert deployment.base.resolved_python == deployment.target.resolved_python
    assert deployment.base.lexical_python != deployment.target.lexical_python
    assert deployment.base.digest != deployment.target.digest
    assert deployment.base.compatibility_digest == deployment.target.compatibility_digest
    with DeploymentStore(deployment.root) as store:
        assert store.read_slot(deployment.base.digest) == deployment.base
        assert store.read_anchor(deployment.anchor.digest) == deployment.anchor


def test_slot_requires_installed_protocol2_declaration(deployment: Deployment) -> None:
    Path(deployment.target.declaration.path).unlink()
    with pytest.raises(ManagedDeploymentError, match="managed_slot_unsupported"):
        register_local_slot(
            Path(deployment.target.prefix),
            Path(deployment.target.lexical_python),
            Path(deployment.target.package.root),
        )


@pytest.mark.parametrize("field", ["declaration", "pyvenv", "resolved_python", "package"])
def test_slot_revalidates_installed_bytes(deployment: Deployment, field: str) -> None:
    path = (
        Path(deployment.target.package.root) / "__main__.py"
        if field == "package"
        else Path(getattr(deployment.target, field).path)
    )
    if field == "resolved_python":
        # Do not touch the shared real interpreter. Repoint the retained entry.
        entry = Path(deployment.target.lexical_python)
        entry.unlink()
        entry.write_bytes(b"changed interpreter")
        entry.chmod(0o700)
    else:
        path.write_bytes(b"changed installed bytes")
    with pytest.raises(ManagedDeploymentError, match="managed_slot_changed"):
        verify_slot_files(deployment.target)


def test_current_slot_cannot_be_proved_by_the_same_real_binary(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", deployment.base.lexical_python)
    monkeypatch.setattr(sys, "prefix", deployment.target.prefix)
    monkeypatch.setattr(muli, "__file__", str(Path(deployment.target.package.root) / "__init__.py"))
    with pytest.raises(ManagedDeploymentError, match="managed_process_mismatch"):
        verify_current_slot(deployment.target)


@pytest.mark.parametrize(
    "phase,intent",
    [
        ("prepared", "none"),
        ("draining", "none"),
        ("activating", "install_target"),
        ("committed", "none"),
    ],
)
def test_unresolved_transaction_blocks_every_ordinary_cli(
    deployment: Deployment, phase: str, intent: str
) -> None:
    deployment.record(phase, intent)
    with pytest.raises(LaunchError, match="transaction_in_progress"):
        resolve_entry(deployment.root, purpose="cli", arguments=("init",))


@pytest.mark.parametrize(
    "phase,intent",
    [
        ("prepared", "none"),
        ("draining", "none"),
        ("activating", "stop_base"),
        ("activating", "install_target"),
        ("rolling_back", "restore_base"),
    ],
)
def test_service_entry_only_allows_an_explicit_start_role(
    deployment: Deployment, phase: str, intent: str
) -> None:
    deployment.record(phase, intent)
    with pytest.raises(LaunchError, match="transaction_in_progress"):
        resolve_entry(deployment.root, purpose="service")


def test_same_configuration_still_requires_the_target_selector(deployment: Deployment) -> None:
    plan = resolve_entry(deployment.root, purpose="service")
    assert plan.python == Path(deployment.target.lexical_python)
    deployment.select(deployment.base_selector)
    with pytest.raises(LaunchError, match="managed_selector_changed"):
        resolve_entry(deployment.root, purpose="service")


@pytest.mark.parametrize("phase,role", [("committed", "target"), ("rolled_back", "base")])
def test_resolved_cli_uses_the_one_terminal_role(
    deployment: Deployment, phase: str, role: str
) -> None:
    deployment.record(phase, "none", activation="released", role=role)
    if role == "base":
        deployment.select(deployment.base_selector)
    plan = resolve_entry(deployment.root, purpose="cli", arguments=("doctor",))
    selected = deployment.base if role == "base" else deployment.target
    assert plan.installation == selected.digest
    assert plan.argv[-1] == "doctor"


@pytest.mark.parametrize(
    "arguments,destination",
    [
        (("--conf", "/other/settings.json", "doctor"), "config"),
        (("--data-d=/other", "doctor"), "data_dir"),
        (("--managed-r", "/other", "doctor"), "managed_root"),
    ],
)
def test_cli_rejects_abbreviations_that_argparse_accepts_as_path_overrides(
    deployment: Deployment, arguments: tuple[str, ...], destination: str
) -> None:
    deployment.record("committed", "none", activation="released")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--data-dir")
    parser.add_argument("--managed-root")
    parser.add_subparsers(dest="command", required=True).add_parser("doctor")
    fixed = (
        "--config",
        deployment.anchor.config_path,
        "--data-dir",
        deployment.anchor.data_dir,
        "--managed-root",
        str(deployment.root),
    )
    parsed = parser.parse_args((*fixed, *arguments))
    assert getattr(parsed, destination).startswith("/other")
    with pytest.raises(LaunchError, match="managed_arguments_invalid"):
        resolve_entry(deployment.root, purpose="cli", arguments=arguments)


def _run_entry(deployment: Deployment, purpose: str) -> tuple[int, dict[str, Any]]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(deployment.root / "launcher.py"),
            "--root",
            str(deployment.root),
            purpose,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    try:
        stdout, stderr = process.communicate(timeout=5)
    except BaseException:
        process.kill()
        process.communicate(timeout=5)
        raise
    assert process.returncode == 0, stderr
    return process.pid, json.loads(stdout)


def test_standalone_entry_really_execs_the_slot_with_the_same_pid(deployment: Deployment) -> None:
    pid, result = _run_entry(deployment, "service")
    assert result["pid"] == pid
    assert result["slot"] == "target"
    assert result["python"] == deployment.target.lexical_python
    assert result["prefix"] == deployment.target.prefix
    assert result["package"] == str(Path(deployment.target.package.root) / "__main__.py")
    assert result["args"][-1] == "serve"


def test_recover_uses_the_retained_controller_with_a_broken_selector(
    deployment: Deployment,
) -> None:
    _write_private(deployment.root / "selector.json", b"broken selector")
    plan = resolve_entry(deployment.root, purpose="recover")
    assert plan.role == "controller"
    assert plan.argv == (
        deployment.base.lexical_python,
        "-I",
        "-m",
        "muli.managed_host",
        "--root",
        str(deployment.root),
        "recover",
    )
    pid, result = _run_entry(deployment, "recover")
    assert result["pid"] == pid
    assert result["slot"] == "base"
    assert result["args"][-1] == "recover"
    with pytest.raises(LaunchError, match="managed_arguments_invalid"):
        resolve_entry(deployment.root, purpose="recover", arguments=("init",))


def test_repair_never_imports_or_recaptures_the_damaged_target(deployment: Deployment) -> None:
    (Path(deployment.target.package.root) / "__init__.py").write_text(
        "raise RuntimeError('selected-slot-was-imported')\n", encoding="utf-8"
    )
    before = {path.name: path.read_bytes() for path in deployment.root.iterdir()}
    status = inspect_repair(deployment.root)
    assert status.code == "recovery_requires_anchor_verification"
    assert status.resolved is False
    _, result = _run_entry(deployment, "repair")
    assert result["code"] == status.code
    assert {path.name: path.read_bytes() for path in deployment.root.iterdir()} == before


def test_bad_anchor_is_not_recaptured_as_an_unmanaged_installation(deployment: Deployment) -> None:
    path = deployment.root / f"anchor-{deployment.anchor.digest}.json"
    _write_private(path, b"{}")
    assert inspect_repair(deployment.root).code == "anchor_unavailable"
    with pytest.raises(LaunchError, match="managed_record_changed"):
        resolve_entry(deployment.root, purpose="recover")


def test_selector_cas_keeps_foreign_bytes_and_checks_its_guard(deployment: Deployment) -> None:
    with SelectorStore(deployment.root) as selector:
        previous = selector.read()
        deployment.select(deployment.base_selector)
        with pytest.raises(ManagedDeploymentError, match="managed_selector_changed"):
            selector.replace(previous, deployment.target_selector, guard=lambda: None)
        assert selector.read() is not None

        def refuse() -> None:
            raise RuntimeError("guard-refused")

        with pytest.raises(RuntimeError, match="guard-refused"):
            selector.replace(selector.read(), None, guard=refuse)
        assert selector.read() is not None


def test_selector_delete_and_absence_confirmation_are_durable(deployment: Deployment) -> None:
    with SelectorStore(deployment.root) as selector:
        assert selector.replace(selector.read(), None, guard=lambda: None) is None
        assert selector.confirm(None, guard=lambda: None) is None
    assert not (deployment.root / "selector.json").exists()


def test_initial_selector_rechecks_effect_guard_after_file_fsync(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SelectorStore(deployment.root) as selector:
        selector.replace(selector.read(), None, guard=lambda: None)
        expired = False
        fsync = os.fsync

        def consume_budget(descriptor: int) -> None:
            nonlocal expired
            fsync(descriptor)
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                expired = True

        def guard() -> None:
            if expired:
                raise RuntimeError("deadline-exhausted")

        monkeypatch.setattr(os, "fsync", consume_budget)
        with pytest.raises(RuntimeError, match="deadline-exhausted"):
            selector.replace(None, deployment.target_selector, guard=guard)
        assert selector.read() is None
        assert not tuple(deployment.root.glob(".tmp-*"))


def test_visible_selector_after_fsync_failure_is_not_a_success_receipt(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsync = os.fsync

    def fail_directory(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected-directory-fsync")
        fsync(descriptor)

    with SelectorStore(deployment.root) as selector:
        before = selector.read()
        with monkeypatch.context() as scope:
            scope.setattr(os, "fsync", fail_directory)
            with pytest.raises(OSError, match="injected-directory-fsync"):
                selector.replace(before, deployment.base_selector, guard=lambda: None)
            with pytest.raises(ValueError, match="unconfirmed"):
                selector.read()
    with SelectorStore(deployment.root) as reopened:
        visible = reopened.read()
        assert visible is not None
        assert visible.value == deployment.base_selector
        with monkeypatch.context() as scope:
            scope.setattr(os, "fsync", fail_directory)
            with pytest.raises(OSError, match="injected-directory-fsync"):
                reopened.confirm(visible, guard=lambda: None)


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_selector_refuses_special_files_without_blocking(deployment: Deployment, kind: str) -> None:
    path = deployment.root / "selector.json"
    path.unlink()
    if kind == "symlink":
        path.symlink_to(deployment.root / "state.json")
    else:
        os.mkfifo(path)
    with SelectorStore(deployment.root) as selector, pytest.raises((OSError, ValueError)):
        selector.read()


def test_metadata_never_contains_configuration_database_or_nonce(deployment: Deployment) -> None:
    assert b"same configuration" not in deployment.anchor.canonical()
    assert b"target-nonce" not in deployment.anchor.canonical()
    assert "target-nonce" not in repr(resolve_entry(deployment.root, purpose="service"))
    assert deployment.target.prefix not in repr(deployment.target)


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_selector_schema_is_an_exact_supported_integer(version: object) -> None:
    with pytest.raises(ValidationError):
        SelectorValue.model_validate(
            {
                "schema_version": version,
                "deployment": "a" * 64,
                "installation": "b" * 64,
            }
        )
