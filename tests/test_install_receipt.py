from __future__ import annotations

import os
import sys

import pytest
from test_installation import Installation
from test_installation import installation as installation

import muli
from muli.install_profile import UV_RECEIPT_VERSION, IntentLimits
from muli.platforms.install_receipt import InstallReceiptError, read_install_receipt
from muli.platforms.installation import InstallationManifest
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
