from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest
from test_managed_launcher import Deployment
from test_managed_launcher import deployment as _launcher_deployment

import muli
from muli import managed_cli, management_cli
from muli.management_transaction import TransactionError, TransactionJournal
from muli.platforms import managed_deployment, managed_launcher
from muli.platforms.managed_deployment import DeploymentAnchor, DeploymentStore, SelectorValue
from muli.platforms.managed_launcher import LaunchPlan
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed CLI routing")
launcher_deployment = _launcher_deployment


class Executed(Exception):
    pass


@pytest.fixture
def deployment(launcher_deployment: Deployment) -> Deployment:
    original = launcher_deployment
    data = Path(original.anchor.data_dir)
    data.mkdir()
    anchor = DeploymentAnchor.model_validate(
        {**original.anchor.model_dump(), "database_path": str(data / "muli.sqlite3")}
    )
    with DeploymentStore(original.root) as source:
        source.publish_anchor(anchor, guard=lambda: None)
    current = replace(
        original,
        anchor=anchor,
        base_selector=SelectorValue(deployment=anchor.digest, installation=original.base.digest),
        target_selector=SelectorValue(
            deployment=anchor.digest, installation=original.target.digest
        ),
    )
    current.select(current.target_selector)
    current.record()
    with TransactionJournal(current.root, data / "muli.sqlite3"):
        pass
    return current


def capture_exec(monkeypatch: pytest.MonkeyPatch) -> list[LaunchPlan]:
    plans: list[LaunchPlan] = []

    def execute(plan: LaunchPlan) -> NoReturn:
        plans.append(plan)
        raise Executed

    monkeypatch.setattr(managed_cli, "execute", execute)
    monkeypatch.setattr(managed_launcher, "execute", execute)
    return plans


def paths(deployment: Deployment) -> tuple[Path, Path]:
    return Path(deployment.anchor.config_path), Path(deployment.anchor.data_dir)


def test_missing_journal_is_read_only_and_explicit_root_cannot_fall_back(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    assert managed_cli.managed_record(root) is None
    assert not root.exists()
    with pytest.raises(TransactionError, match="managed_transaction_required"):
        managed_cli.managed_record(root, required=True)
    assert not root.exists()


def test_missing_state_with_selector_cannot_delegate_to_ordinary_cli(tmp_path: Path) -> None:
    root = tmp_path / "management" / "transaction-v1"
    root.mkdir(mode=0o700, parents=True)
    (root / "selector.json").symlink_to("absent")
    with pytest.raises(ValueError, match="managed_state_missing"):
        managed_cli.route_command(tmp_path / "config.json", tmp_path, ("init", "--defaults"))


@pytest.mark.parametrize("arguments", [("doctor",), ("init", "--defaults"), ("service", "stop")])
def test_unresolved_blocks_ordinary_commands_before_exec(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch, arguments: tuple[str, ...]
) -> None:
    plans = capture_exec(monkeypatch)
    with pytest.raises(ValueError, match="transaction_in_progress"):
        managed_cli.route_command(*paths(deployment), arguments, managed_root=deployment.root)
    assert not plans


def test_resolved_child_verifies_current_slot_without_exec_recursion(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment.record("committed", "none", activation="released")
    plans = capture_exec(monkeypatch)
    # A child with explicit root must already run the selected package.
    with pytest.raises(ValueError):
        managed_cli.route_command(*paths(deployment), ("doctor",), managed_root=deployment.root)
    assert not plans
    monkeypatch.setattr(
        managed_deployment,
        "sys",
        SimpleNamespace(
            platform="linux",
            executable=deployment.target.lexical_python,
            prefix=deployment.target.prefix,
            base_prefix="/different-base",
        ),
    )
    monkeypatch.setattr(muli, "__file__", str(Path(deployment.target.package.root) / "__init__.py"))
    managed_cli.route_command(*paths(deployment), ("doctor",), managed_root=deployment.root)
    assert not plans


def test_cli_cannot_override_frozen_config_root(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment.record("committed", "none", activation="released")
    plans = capture_exec(monkeypatch)
    with pytest.raises(TransactionError, match="managed_paths_changed"):
        managed_cli.route_command(
            deployment.root / "other.json",
            Path(deployment.anchor.data_dir),
            ("doctor",),
            managed_root=deployment.root,
        )
    assert not plans


async def test_pending_status_is_available_without_opening_application_store(
    deployment: Deployment,
) -> None:
    service = UserService(*paths(deployment))
    before = (deployment.root / "state.json").read_bytes()
    managed_cli.route_command(
        *paths(deployment), ("management", "status"), managed_root=deployment.root
    )
    result = await management_cli.management_action(service, "status", managed_root=deployment.root)
    assert result["transaction"]["protocol"] == 2
    assert not result["transaction"]["resolved"]
    assert (deployment.root / "state.json").read_bytes() == before


async def test_recover_routes_fixed_controller_even_with_broken_selector(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    plans = capture_exec(monkeypatch)
    (deployment.root / "selector.json").write_bytes(b"broken")
    with pytest.raises(Executed):
        await management_cli.management_action(
            UserService(*paths(deployment)), "recover", managed_root=deployment.root
        )
    assert len(plans) == 1
    assert plans[0].role == "controller"
    assert plans[0].installation == deployment.base.digest
    assert plans[0].argv[-1] == "recover"


@pytest.mark.parametrize("resolved", [True, False])
async def test_ordinary_install_cannot_replace_managed_unit(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch, resolved: bool
) -> None:
    if resolved:
        deployment.record("committed", "none", activation="released")
    called: list[str] = []

    async def action(service: UserService, action: str) -> dict[str, bool]:
        called.append(action)
        return {"accepted": True}

    monkeypatch.setattr(UserService, "action", action)
    with pytest.raises(
        TransactionError,
        match="managed_unit_replacement_forbidden" if resolved else "transaction_in_progress",
    ):
        await management_cli.guarded_service_action(
            UserService(*paths(deployment)), "install", managed_root=deployment.root
        )
    assert not called


async def test_service_action_confirms_known_unit_and_rejects_manual_change(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = deployment.record("committed", "none", activation="released")
    unit = Path(deployment.anchor.unit_path)
    unit.parent.mkdir(exist_ok=True)
    from test_managed_launcher import _UNIT

    unit.write_bytes(_UNIT)
    unit.chmod(0o600)
    called: list[str] = []

    async def action(service: UserService, action: str) -> dict[str, bool]:
        called.append(action)
        return {"accepted": True}

    monkeypatch.setattr(UserService, "action", action)
    service = UserService(*paths(deployment), unit.parent)
    assert (
        await management_cli.guarded_service_action(service, "stop", managed_root=deployment.root)
    )["accepted"]
    assert called == ["stop"]
    unit.write_bytes(_UNIT + b"# manual change\n")
    with pytest.raises(ValueError):
        await management_cli.guarded_service_action(service, "start", managed_root=deployment.root)
    assert called == ["stop"]
    assert managed_cli.managed_record(deployment.root) == record
