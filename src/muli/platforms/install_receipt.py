"""Observe a fixed receipt inside a verified Linux installation, without effects.

This does not authenticate uv, reconstruct resolver context or authorize updates.
All returned evidence is private to the host, including paths and digests.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import Field

from muli.install_profile import (
    InstallProfileError,
    IntentLimits,
    ParsedInstallIntent,
    parse_install_intent,
    receipt_adapter,
)
from muli.management_transaction import Digest, StrictRecord
from muli.platforms.installation import FileFingerprint, InstallationManifest, _read_file
from muli.platforms.managed_deployment import SlotIdentity, verify_current_slot

_RECEIPT = "uv-receipt.toml"


class InstallReceiptError(ValueError):
    """A fixed reason without source paths, receipt contents or exception text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ReceiptObservation(StrictRecord):
    """Bounded private evidence; a supplied verifier is not host attestation."""

    status: Literal["receipt_observed"] = "receipt_observed"
    receipt: FileFingerprint = Field(repr=False)
    parsed: ParsedInstallIntent = Field(repr=False)
    limits: IntentLimits = Field(repr=False)


class InstallationReceipt(ReceiptObservation):
    installation: Digest = Field(repr=False)

    def verify(self, manifest: InstallationManifest) -> None:
        """Reobserve both installation and receipt; never reuse stale intent."""
        if manifest.digest != self.installation:
            raise InstallReceiptError("receipt_installation_changed")
        current = read_install_receipt(
            manifest,
            uv_version=self.parsed.adapter.removeprefix("uv-tool-"),
            limits=self.limits,
        )
        if current != self:
            raise InstallReceiptError("receipt_changed")


class ManagedInstallationReceipt(ReceiptObservation):
    installation: Digest = Field(repr=False)

    def verify(self, slot: SlotIdentity) -> None:
        """Revalidate the current process's slot, never an inactive candidate."""
        if slot.digest != self.installation:
            raise InstallReceiptError("receipt_installation_changed")
        current = read_managed_install_receipt(
            slot,
            uv_version=self.parsed.adapter.removeprefix("uv-tool-"),
            limits=self.limits,
        )
        if current != self:
            raise InstallReceiptError("receipt_changed")


def _receipt_path(manifest: InstallationManifest) -> Path:
    if sys.platform != "linux":
        raise InstallReceiptError("receipt_platform_unsupported")
    interpreter = manifest.interpreter
    prefix = Path(interpreter.prefix)
    package = Path(manifest.package.root)
    if (
        prefix.name != "muli-agent"
        or interpreter.prefix == interpreter.base_prefix
        or interpreter.pyvenv is None
        or package == prefix
        or not package.is_relative_to(prefix)
    ):
        raise InstallReceiptError("receipt_installation_unsupported")
    return prefix / _RECEIPT


def _verify_installation(manifest: InstallationManifest) -> None:
    try:
        manifest.verify()
    except (OSError, ValueError):
        raise InstallReceiptError("receipt_installation_changed") from None


def _verify_current_installation(slot: SlotIdentity) -> None:
    try:
        verify_current_slot(slot)
    except (OSError, ValueError):
        raise InstallReceiptError("receipt_installation_changed") from None


def _observe(path: Path, limit: int) -> tuple[bytes, FileFingerprint]:
    try:
        data, identity = _read_file(path, limit, private_owner=True)
    except FileNotFoundError:
        raise InstallReceiptError("receipt_missing") from None
    except (OSError, ValueError):
        raise InstallReceiptError("receipt_unreadable") from None
    return data, FileFingerprint(
        path=str(path), identity=identity, digest=hashlib.sha256(data).hexdigest()
    )


def _adapter(uv_version: str) -> None:
    try:
        receipt_adapter(uv_version)
    except InstallProfileError as exc:
        raise InstallReceiptError(exc.code) from None


def observe_install_receipt(
    path: Path,
    *,
    uv_version: str,
    verify: Callable[[], None],
    limits: IntentLimits | None = None,
) -> ReceiptObservation:
    """Observe supplied fixed evidence under a host-owned verification callback.

    The callback is mandatory, but accepting it does not certify its source or
    authorize an installation. The caller owns path selection, binding proof
    and cooperative exclusion. Local find-links directories are not opened.
    """
    _adapter(uv_version)
    budget = limits if limits is not None else IntentLimits()

    def check() -> None:
        try:
            verify()
        except InstallReceiptError:
            raise
        except (OSError, ValueError):
            raise InstallReceiptError("receipt_binding_changed") from None

    check()
    data, fingerprint = _observe(path, budget.max_receipt_bytes)
    try:
        parsed = parse_install_intent(data, uv_version=uv_version, limits=budget)
    except InstallProfileError as exc:
        raise InstallReceiptError(exc.code) from None
    check()
    _, repeated = _observe(path, budget.max_receipt_bytes)
    if repeated != fingerprint:
        raise InstallReceiptError("receipt_changed")
    return ReceiptObservation(receipt=fingerprint, parsed=parsed, limits=budget)


def read_install_receipt(
    manifest: InstallationManifest, *, uv_version: str, limits: IntentLimits | None = None
) -> InstallationReceipt:
    """Observe the fixed receipt in the original supported V1 installation."""
    _adapter(uv_version)
    observation = observe_install_receipt(
        _receipt_path(manifest),
        uv_version=uv_version,
        verify=lambda: _verify_installation(manifest),
        limits=limits,
    )
    return InstallationReceipt(
        installation=manifest.digest,
        receipt=observation.receipt,
        parsed=observation.parsed,
        limits=observation.limits,
    )


def read_managed_install_receipt(
    slot: SlotIdentity, *, uv_version: str, limits: IntentLimits | None = None
) -> ManagedInstallationReceipt:
    """Observe this process's retained slot, independently of its controller."""
    _adapter(uv_version)
    if sys.platform != "linux":
        raise InstallReceiptError("receipt_platform_unsupported")
    observation = observe_install_receipt(
        Path(slot.prefix) / _RECEIPT,
        uv_version=uv_version,
        verify=lambda: _verify_current_installation(slot),
        limits=limits,
    )
    return ManagedInstallationReceipt(
        installation=slot.digest,
        receipt=observation.receipt,
        parsed=observation.parsed,
        limits=observation.limits,
    )
