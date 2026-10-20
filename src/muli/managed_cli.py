"""Delegate ordinary managed CLI commands before opening application data.

The stable launcher remains the independent repair/recovery entry when a
selected package cannot import. This module only bridges an importable CLI.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from muli.managed_transaction import TransactionRecordV2, parse_record_v2
from muli.management_transaction import JournalLimits, TransactionError
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    require_unselected_without_record,
    verify_current_slot,
)
from muli.platforms.managed_launcher import execute, object_bytes, read_file, resolve_entry


def managed_record(root: Path, *, required: bool = False) -> TransactionRecordV2 | None:
    """Bounded, read-only protocol discovery; never create a journal or database."""
    if sys.platform != "linux":
        if required:
            raise TransactionError("platform_unsupported")
        return None
    try:
        data, _ = read_file(
            root.absolute() / "state.json", JournalLimits().max_record_bytes, private=True
        )
    except FileNotFoundError:
        require_unselected_without_record(root)
        if required:
            raise TransactionError("managed_transaction_required") from None
        return None
    protocol = object_bytes(data).get("protocol")
    if type(protocol) is int and protocol == 1 and not required:
        return None
    return parse_record_v2(data)


def check_cli_paths(
    root: Path, record: TransactionRecordV2, config_path: Path, data_dir: Path
) -> DeploymentAnchor:
    with DeploymentStore(root.absolute()) as deployments:
        anchor = deployments.read_anchor(record.identity.deployment)
        if (
            anchor.journal_root != str(root.absolute())
            or anchor.config_path != str(config_path.absolute())
            or anchor.data_dir != str(data_dir.absolute())
            or anchor.database_path != str(data_dir.absolute() / "muli.sqlite3")
        ):
            raise TransactionError("managed_paths_changed")
        return anchor


def route_command(
    config_path: Path,
    data_dir: Path,
    arguments: Sequence[str],
    *,
    managed_root: Path | None = None,
) -> None:
    """Exec the selected slot once; its child verifies its actual installation."""
    root = managed_root or data_dir.absolute() / "management" / "transaction-v1"
    record = managed_record(root, required=managed_root is not None)
    if record is None:
        return
    check_cli_paths(root, record, config_path, data_dir)
    # Diagnostic/recovery dispatch has its own stable-controller route and must
    # remain available while ordinary commands are forbidden or the slot is bad.
    if tuple(arguments[:1]) == ("management",):
        return
    plan = resolve_entry(root.absolute(), purpose="cli", arguments=arguments)
    if managed_root is None:
        execute(plan)
    with DeploymentStore(root.absolute()) as deployments:
        slot = deployments.read_slot(plan.installation)
        verify_current_slot(slot)
    # resolve_entry checked frozen routing twice; child commands cannot substitute
    # the config/data roots or silently run under a different Python installation.
    fresh = managed_record(root, required=True)
    if fresh != record:
        raise TransactionError("managed_record_changed")
