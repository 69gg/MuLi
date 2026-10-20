"""Observe a fixed receipt inside a verified Linux installation, without effects.

This does not authenticate uv, reconstruct resolver context or authorize updates.
All returned evidence is private to the host, including paths and digests.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Literal

from pydantic import Field

from muli.install_profile import (
    UV_RECEIPT_VERSION,
    InstallProfileError,
    IntentLimits,
    ParsedInstallIntent,
    parse_install_intent,
)
from muli.management_transaction import Digest, StrictRecord
from muli.platforms.installation import FileFingerprint, InstallationManifest, _read_file

_RECEIPT = "uv-receipt.toml"


class InstallReceiptError(ValueError):
    """A fixed reason without source paths, receipt contents or exception text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class InstallationReceipt(StrictRecord):
    status: Literal["receipt_observed"] = "receipt_observed"
    installation: Digest = Field(repr=False)
    receipt: FileFingerprint = Field(repr=False)
    parsed: ParsedInstallIntent = Field(repr=False)
    limits: IntentLimits = Field(repr=False)

    def verify(self, manifest: InstallationManifest) -> None:
        """Reobserve both installation and receipt; never reuse stale intent."""
        if manifest.digest != self.installation:
            raise InstallReceiptError("receipt_installation_changed")
        current = read_install_receipt(manifest, uv_version=UV_RECEIPT_VERSION, limits=self.limits)
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


def read_install_receipt(
    manifest: InstallationManifest, *, uv_version: str, limits: IntentLimits | None = None
) -> InstallationReceipt:
    """Read the prefix's fixed receipt, preserving the parser's context_required.

    uv_version selects an adapter; it is not binary or historical producer proof.
    The host owns cooperative installation exclusion and revalidation before
    future effects. Repeated observations are not an atomic filesystem snapshot.
    """
    # Reject unsupported adapters before observing any installation files.
    if type(uv_version) is not str or uv_version != UV_RECEIPT_VERSION:
        raise InstallReceiptError("unsupported_uv_version")
    budget = limits if limits is not None else IntentLimits()
    path = _receipt_path(manifest)
    _verify_installation(manifest)
    data, fingerprint = _observe(path, budget.max_receipt_bytes)
    try:
        parsed = parse_install_intent(data, uv_version=uv_version, limits=budget)
    except InstallProfileError as exc:
        raise InstallReceiptError(exc.code) from None
    _verify_installation(manifest)
    _, repeated = _observe(path, budget.max_receipt_bytes)
    if repeated != fingerprint:
        raise InstallReceiptError("receipt_changed")
    return InstallationReceipt(
        installation=manifest.digest, receipt=fingerprint, parsed=parsed, limits=budget
    )
