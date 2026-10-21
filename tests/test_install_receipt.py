from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from test_install_profile import OBSERVED_UV_0131_RECEIPT
from test_installation import Installation
from test_installation import installation as installation
from test_managed_launcher import _slot

import muli
from muli.install_profile import UV_RECEIPT_VERSION, IntentLimits
from muli.platforms.install_receipt import (
    InstallReceiptError,
    observe_install_receipt,
    read_install_receipt,
    read_managed_install_receipt,
)
from muli.platforms.installation import InstallationManifest
from muli.platforms.managed_deployment import (
    SlotIdentity,
    register_local_slot,
    verify_current_slot,
    verify_slot_files,
)
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux anchored receipt files")

RECEIPT = b"""[tool]
requirements = [{name = "muli-agent", extras = ["mcp", "channels"], specifier = ">=2,<3"}]
entrypoints = [{name = "muli", install-path = "/fixture/bin/muli"}]
"""


@pytest.fixture
def current(installation: Installation, monkeypatch: pytest.MonkeyPatch) -> Installation:
    previous = installation.venv
    installation.venv = previous.with_name("muli-agent")
    previous.rename(installation.venv)
    installation.executable = installation.venv / "bin/python"
    package = installation.venv / "lib/site-packages/muli"
    package.parent.mkdir(parents=True)
    installation.package.rename(package)
    installation.package = package
    monkeypatch.setattr(sys, "executable", str(installation.executable))
    monkeypatch.setattr(sys, "prefix", str(installation.venv))
    monkeypatch.setattr(muli, "__file__", str(package / "__init__.py"))
    service = installation.service
    installation.service = UserService(
        service.config_path,
        service.data_dir,
        service.path.parent,
        platform="linux",
        executable=str(installation.executable),
    )
    installation.service.path.write_text(installation.service.render(), encoding="utf-8")
    (installation.venv / "uv-receipt.toml").write_bytes(RECEIPT)
    return installation


@pytest.fixture
def managed_current(
    current: Installation, monkeypatch: pytest.MonkeyPatch
) -> tuple[Installation, SlotIdentity]:
    # The current retained slot intentionally has a non-uv-default basename.
    previous = current.venv
    current.venv = previous.with_name("retained-current")
    previous.rename(current.venv)
    current.executable = current.venv / "bin/python"
    current.package = current.venv / "lib/site-packages/muli"
    declaration = Path(__file__).resolve().parents[1] / "src/muli/compatibility.json"
    (current.package / "compatibility.json").write_bytes(declaration.read_bytes())
    monkeypatch.setattr(sys, "executable", str(current.executable))
    monkeypatch.setattr(sys, "prefix", str(current.venv))
    monkeypatch.setattr(muli, "__file__", str(current.package / "__init__.py"))
    return current, register_local_slot(current.venv, current.executable, current.package)


def test_actual_manifest_read_parse_and_revalidation(current: Installation) -> None:
    manifest = current.capture()
    receipt = current.venv / "uv-receipt.toml"
    before = receipt.stat()
    snapshot = read_install_receipt(manifest, uv_version=UV_RECEIPT_VERSION)
    assert snapshot.status == "receipt_observed"
    assert snapshot.parsed.context_status == "context_required"
    assert snapshot.parsed.intent.main.extras == ("mcp", "channels")
    assert snapshot.parsed.intent.main.specifier == ">=2,<3"
    assert snapshot.installation == manifest.digest
    assert snapshot.receipt.path == str(receipt)
    assert repr(snapshot) == "InstallationReceipt(status='receipt_observed')"
    snapshot.verify(manifest)
    after = receipt.stat()
    assert (before.st_ino, before.st_mode, before.st_mtime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_mtime_ns,
    )
    assert receipt.read_bytes() == RECEIPT


@pytest.mark.parametrize("change", ["content", "same-content-replacement", "missing"])
def test_stale_receipt_is_not_reused(current: Installation, change: str) -> None:
    manifest = current.capture()
    snapshot = read_install_receipt(manifest, uv_version=UV_RECEIPT_VERSION)
    receipt = current.venv / "uv-receipt.toml"
    if change == "content":
        receipt.write_bytes(RECEIPT.replace(b">=2,<3", b">=2,<4"))
    elif change == "same-content-replacement":
        replacement = receipt.with_suffix(".new")
        replacement.write_bytes(RECEIPT)
        replacement.replace(receipt)
    else:
        receipt.unlink()
    with pytest.raises(InstallReceiptError, match="^receipt_(changed|missing)$"):
        snapshot.verify(manifest)


@pytest.mark.parametrize("change", ["package", "interpreter", "manifest"])
def test_installation_changes_invalidate_receipt(current: Installation, change: str) -> None:
    manifest = current.capture()
    snapshot = read_install_receipt(manifest, uv_version=UV_RECEIPT_VERSION)
    if change == "package":
        (current.package / "main.py").write_bytes(b"changed")
    elif change == "interpreter":
        current.real.write_bytes(b"changed binary, never executed")
    else:
        manifest = manifest.model_copy(update={"service_digest": "f" * 64})
    with pytest.raises(InstallReceiptError, match="^receipt_installation_changed$"):
        snapshot.verify(manifest)


def test_receipt_replaced_during_manifest_revalidation_is_rejected(
    current: Installation, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = current.capture()
    original = InstallationManifest.verify
    calls = 0

    def replacing(self: InstallationManifest, *, require_database: bool = True) -> None:
        nonlocal calls
        original(self, require_database=require_database)
        calls += 1
        if calls == 2:
            replacement = current.venv / "replacement"
            replacement.write_bytes(RECEIPT)
            replacement.replace(current.venv / "uv-receipt.toml")

    monkeypatch.setattr(InstallationManifest, "verify", replacing)
    with pytest.raises(InstallReceiptError, match="^receipt_changed$"):
        read_install_receipt(manifest, uv_version=UV_RECEIPT_VERSION)
    assert calls == 2


@pytest.mark.parametrize("source", ["symlink", "fifo", "directory", "oversize", "invalid"])
def test_receipt_source_is_bounded_and_regular(current: Installation, source: str) -> None:
    manifest = current.capture()
    receipt = current.venv / "uv-receipt.toml"
    receipt.unlink()
    if source == "symlink":
        target = current.venv / "other"
        target.write_bytes(RECEIPT)
        receipt.symlink_to(target)
    elif source == "fifo":
        os.mkfifo(receipt)
    elif source == "directory":
        receipt.mkdir()
    elif source == "oversize":
        receipt.write_bytes(b"x" * 129)
    else:
        receipt.write_bytes(b"private-invalid-receipt")
    with pytest.raises(InstallReceiptError) as caught:
        read_install_receipt(
            manifest, uv_version=UV_RECEIPT_VERSION, limits=IntentLimits(max_receipt_bytes=128)
        )
    assert caught.value.code == ("invalid_receipt" if source == "invalid" else "receipt_unreadable")
    assert "private" not in str(caught.value) and str(current.venv) not in str(caught.value)


@pytest.mark.parametrize("change", ["prefix", "base", "external-package"])
def test_unsupported_installation_does_not_observe_files(
    current: Installation, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    manifest = current.capture()
    if change == "external-package":
        manifest = manifest.model_copy(
            update={"package": manifest.package.model_copy(update={"root": str(current.base)})}
        )
    else:
        key = "prefix" if change == "prefix" else "base_prefix"
        value = str(current.base) if change == "prefix" else str(current.venv)
        manifest = manifest.model_copy(
            update={"interpreter": manifest.interpreter.model_copy(update={key: value})}
        )

    def forbidden(self: InstallationManifest, *, require_database: bool = True) -> None:
        raise AssertionError("unsupported installation must fail before file observation")

    monkeypatch.setattr(InstallationManifest, "verify", forbidden)
    with pytest.raises(InstallReceiptError, match="^receipt_installation_unsupported$"):
        read_install_receipt(manifest, uv_version=UV_RECEIPT_VERSION)


def test_unknown_adapter_fails_before_installation_observation(
    current: Installation, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = current.capture()

    def forbidden(self: InstallationManifest, *, require_database: bool = True) -> None:
        raise AssertionError("unsupported adapter must not observe files")

    monkeypatch.setattr(InstallationManifest, "verify", forbidden)
    with pytest.raises(InstallReceiptError, match="^unsupported_uv_version$"):
        read_install_receipt(manifest, uv_version="0.0.0")


def test_v1_0131_snapshot_verifies_with_its_original_adapter(current: Installation) -> None:
    manifest = current.capture()
    (current.venv / "uv-receipt.toml").write_bytes(OBSERVED_UV_0131_RECEIPT)
    snapshot = read_install_receipt(manifest, uv_version="0.13.1")
    assert snapshot.parsed.adapter == "uv-tool-0.13.1"
    snapshot.verify(manifest)


def test_current_slot_observation_never_uses_controller_or_v1_receipt(
    managed_current: tuple[Installation, SlotIdentity], monkeypatch: pytest.MonkeyPatch
) -> None:
    current, slot = managed_current
    receipt = current.venv / "uv-receipt.toml"
    receipt.write_bytes(OBSERVED_UV_0131_RECEIPT)
    (current.base / "uv-receipt.toml").write_bytes(b"not the current slot receipt")

    def forbidden(self: InstallationManifest, *, require_database: bool = True) -> None:
        raise AssertionError("a managed slot must not be represented by a V1 manifest")

    monkeypatch.setattr(InstallationManifest, "verify", forbidden)
    snapshot = read_managed_install_receipt(slot, uv_version="0.13.1")
    assert snapshot.installation == slot.digest
    assert snapshot.receipt.path == str(receipt)
    assert snapshot.parsed.intent.main.specifier == "==0.0.1"
    assert snapshot.parsed.adapter == "uv-tool-0.13.1"
    assert repr(snapshot) == "ManagedInstallationReceipt(status='receipt_observed')"
    snapshot.verify(slot)


@pytest.mark.parametrize("change", ["content", "same-content-replacement", "package", "current"])
def test_current_slot_or_receipt_changes_reject_stale_intent(
    managed_current: tuple[Installation, SlotIdentity],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    current, slot = managed_current
    snapshot = read_managed_install_receipt(slot, uv_version="0.13.1")
    receipt = current.venv / "uv-receipt.toml"
    if change == "content":
        receipt.write_bytes(RECEIPT.replace(b">=2,<3", b">=2,<4"))
    elif change == "same-content-replacement":
        replacement = receipt.with_suffix(".new")
        replacement.write_bytes(RECEIPT)
        replacement.replace(receipt)
    elif change == "package":
        (current.package / "main.py").write_bytes(b"changed, never imported")
    else:
        monkeypatch.setattr(sys, "prefix", str(current.base))
    code = (
        "receipt_changed"
        if change in {"content", "same-content-replacement"}
        else ("receipt_installation_changed")
    )
    with pytest.raises(InstallReceiptError, match=f"^{code}$"):
        snapshot.verify(slot)


def test_current_slot_replacement_during_second_identity_check_is_rejected(
    managed_current: tuple[Installation, SlotIdentity], monkeypatch: pytest.MonkeyPatch
) -> None:
    current, slot = managed_current
    calls = 0

    def replacing(value: SlotIdentity) -> None:
        nonlocal calls
        verify_current_slot(value)
        calls += 1
        if calls == 2:
            replacement = current.venv / "replacement"
            replacement.write_bytes(RECEIPT)
            replacement.replace(current.venv / "uv-receipt.toml")

    monkeypatch.setattr("muli.platforms.install_receipt.verify_current_slot", replacing)
    with pytest.raises(InstallReceiptError, match="^receipt_changed$"):
        read_managed_install_receipt(slot, uv_version="0.13.1")
    assert calls == 2


def test_inactive_slot_requires_host_guard_not_current_process_attestation(tmp_path: Path) -> None:
    slot = _slot(tmp_path, "inactive-candidate")
    path = Path(slot.prefix) / "uv-receipt.toml"
    path.write_bytes(OBSERVED_UV_0131_RECEIPT)
    with pytest.raises(InstallReceiptError, match="^receipt_installation_changed$"):
        read_managed_install_receipt(slot, uv_version="0.13.1")
    calls = 0

    def host_guard() -> None:
        nonlocal calls
        calls += 1
        # A real candidate host must additionally bind preflight and ownership.
        verify_slot_files(slot)

    observation = observe_install_receipt(path, uv_version="0.13.1", verify=host_guard)
    assert calls == 2
    assert observation.receipt.path == str(path)
    assert observation.parsed.context_status == "context_required"
    assert "installation" not in observation.model_dump()
    assert repr(observation) == "ReceiptObservation(status='receipt_observed')"


@pytest.mark.parametrize("change", ["receipt", "binding"])
def test_host_observation_rejects_changes_and_hides_verifier_error(
    tmp_path: Path, change: str
) -> None:
    path = tmp_path / "uv-receipt.toml"
    path.write_bytes(RECEIPT)
    calls = 0

    def host_guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "binding":
                raise ValueError("private-binding-secret")
            replacement = path.with_suffix(".new")
            replacement.write_bytes(RECEIPT)
            replacement.replace(path)

    code = "receipt_changed" if change == "receipt" else "receipt_binding_changed"
    with pytest.raises(InstallReceiptError, match=f"^{code}$") as caught:
        observe_install_receipt(path, uv_version="0.13.1", verify=host_guard)
    assert calls == 2 and "private" not in repr(caught.value)


def test_unknown_adapter_does_not_invoke_host_guard_or_observe_missing_path(tmp_path: Path) -> None:
    def forbidden() -> None:
        raise AssertionError("unsupported adapter must fail before the callback")

    with pytest.raises(InstallReceiptError, match="^unsupported_uv_version$"):
        observe_install_receipt(tmp_path / "missing", uv_version="0.13.2", verify=forbidden)
