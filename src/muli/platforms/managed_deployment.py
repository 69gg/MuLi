"""Private identities and selector files for an explicitly retained Linux slot.

These files describe installed bytes. They do not attest a running interpreter,
authorize a database write, or implement a protocol-2 service transaction.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from muli.compatibility import CompatibilityManifest
from muli.managed_transaction import RawSnapshotReference, compatibility_digest
from muli.management_transaction import (
    Digest,
    JournalLimits,
    SnapshotReference,
    StrictRecord,
    _Directory,
    _snapshot,
)
from muli.platforms.installation import (
    AbsolutePath,
    FileFingerprint,
    HelperLimits,
    PackageFingerprint,
    PackageLimits,
    _absolute,
    _canonical,
)
from muli.platforms.managed_launcher import (
    LaunchPlan as EntryPlan,
)
from muli.platforms.managed_launcher import (
    PackageProofs,
    absolute,
    directory,
    fingerprint,
    identity,
    object_bytes,
    package_fingerprint,
    python_fingerprint,
    read_at,
    read_file,
)
from muli.platforms.managed_launcher import resolve_entry as resolve_entry
from muli.platforms.managed_launcher import (
    verify_slot_files as _verify_slot_files,
)
from muli.platforms.transaction_files import ConfigurationFile
from muli.service import UNIT

__all__ = [
    "DeploymentAnchor",
    "DeploymentStore",
    "EntryPlan",
    "ManagedDeploymentError",
    "SelectorSnapshot",
    "SelectorStore",
    "SelectorValue",
    "SlotIdentity",
    "register_local_slot",
    "require_unselected_without_record",
    "resolve_entry",
    "verify_current_slot",
    "verify_slot_files",
]
DECLARATION_NAME = "compatibility.json"
SELECTOR_NAME = "selector.json"
LAUNCHER_NAME = "launcher.py"
_PRIVATE_BYTES = 131_072
_SELECTOR_BYTES = 16_384
_LAUNCHER_BYTES = 262_144
_PYTHON_BYTES = 33_554_432


class ManagedDeploymentError(ValueError):
    """Only fixed reason codes; supplied paths and file contents stay private."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _AuthenticationMismatch(ManagedDeploymentError):
    """An actual parsed record differs from the backend's frozen expectation."""


def require_unselected_without_record(root: Path) -> None:
    """Missing state cannot turn a selected deployment into an ordinary one."""
    try:
        (root / SELECTOR_NAME).lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise ManagedDeploymentError("managed_state_unavailable") from None
    # The entry itself is evidence. Never open a FIFO or follow a dangling link.
    raise ManagedDeploymentError("managed_state_missing")


class _ManagedRecord(StrictRecord):
    schema_version: int = Field(default=1, ge=1, le=1)

    def canonical(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()


class SlotIdentity(_ManagedRecord):
    prefix: AbsolutePath = Field(repr=False)
    lexical_python: AbsolutePath = Field(repr=False)
    resolved_python: FileFingerprint = Field(repr=False)
    pyvenv: FileFingerprint = Field(repr=False)
    package: PackageFingerprint = Field(repr=False)
    declaration: FileFingerprint = Field(repr=False)
    compatibility: CompatibilityManifest = Field(repr=False)
    package_limits: PackageLimits = Field(default_factory=PackageLimits)
    python_bytes: int = Field(default=_PYTHON_BYTES, ge=1, le=67_108_864)

    @field_validator("prefix", "lexical_python")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _absolute(value)

    @model_validator(mode="after")
    def retained_venv(self) -> Self:
        prefix = Path(self.prefix)
        package = Path(self.package.root)
        if (
            prefix == Path("/")
            or not Path(self.lexical_python).is_relative_to(prefix)
            or not package.is_relative_to(prefix)
            or Path(self.pyvenv.path) != prefix / "pyvenv.cfg"
            or Path(self.declaration.path) != package / DECLARATION_NAME
            or self.compatibility.handoff_protocol != 2
            or self.compatibility.requires_migration
        ):
            raise ValueError("unsupported_slot")
        return self

    @property
    def compatibility_digest(self) -> str:
        return compatibility_digest(self.compatibility)


class DeploymentAnchor(_ManagedRecord):
    config_path: AbsolutePath = Field(repr=False)
    data_dir: AbsolutePath = Field(repr=False)
    database_path: AbsolutePath = Field(repr=False)
    unit_path: AbsolutePath = Field(repr=False)
    journal_root: AbsolutePath = Field(repr=False)
    controller_slot: Digest = Field(repr=False)
    launcher: FileFingerprint = Field(repr=False)
    managed_unit: RawSnapshotReference = Field(repr=False)
    helper: HelperLimits = Field(default_factory=HelperLimits)
    journal: JournalLimits = Field(default_factory=JournalLimits)
    package_limits: PackageLimits = Field(default_factory=PackageLimits)

    @field_validator("config_path", "data_dir", "database_path", "unit_path", "journal_root")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _absolute(value)

    @model_validator(mode="after")
    def fixed_entry(self) -> Self:
        if (
            Path(self.unit_path).name != UNIT
            or Path(self.launcher.path) != Path(self.journal_root) / LAUNCHER_NAME
        ):
            raise ValueError("unsupported_deployment")
        return self


class SelectorValue(_ManagedRecord):
    deployment: Digest = Field(repr=False)
    installation: Digest = Field(repr=False)


@dataclass(frozen=True, slots=True)
class SelectorSnapshot:
    value: SelectorValue = field(repr=False)
    data: bytes = field(repr=False)
    identity: tuple[int, int, int, int, int] = field(repr=False)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def reference(self) -> SnapshotReference:
        return _snapshot(self.data, _SELECTOR_BYTES)


EntryPurpose = Literal["service", "cli", "recover", "repair"]
EntryRole = Literal["base", "target", "controller"]


def _platform() -> None:
    if sys.platform != "linux":
        raise ManagedDeploymentError("platform_unsupported")


def register_local_slot(
    prefix: Path,
    python: Path,
    package_root: Path,
    *,
    limits: PackageLimits | None = None,
    python_bytes: int = _PYTHON_BYTES,
) -> SlotIdentity:
    """Observe an explicit retained venv without executing its interpreter.

    The prefix and package location are expectations, not a claim about a live
    import. ``verify_current_slot`` must independently check them at boot.
    """
    _platform()
    if type(python_bytes) is not int or not 1 <= python_bytes <= 67_108_864:
        raise ManagedDeploymentError("managed_python_budget")
    limits = limits or PackageLimits()
    try:
        prefix, python, package_root = (
            absolute(str(path)) for path in (prefix, python, package_root)
        )
        declaration_path = package_root / DECLARATION_NAME
        declaration, _ = read_file(declaration_path, _SELECTOR_BYTES)
        result = SlotIdentity(
            prefix=str(prefix),
            lexical_python=str(python),
            resolved_python=FileFingerprint.model_validate(
                python_fingerprint(python, python_bytes)
            ),
            pyvenv=FileFingerprint.model_validate(
                fingerprint(prefix / "pyvenv.cfg", limits.file_bytes)
            ),
            package=PackageFingerprint.model_validate(
                package_fingerprint(package_root, limits.model_dump())
            ),
            declaration=FileFingerprint.model_validate(
                fingerprint(declaration_path, _SELECTOR_BYTES)
            ),
            compatibility=CompatibilityManifest.from_json(declaration),
            package_limits=limits,
            python_bytes=python_bytes,
        )
        verify_slot_files(result)
        return result
    except (OSError, ValueError):
        raise ManagedDeploymentError("managed_slot_unsupported") from None


def verify_slot_files(
    slot: SlotIdentity,
    *,
    verified_python: FileFingerprint | None = None,
    verified_packages: PackageProofs | None = None,
    observed_packages: PackageProofs | None = None,
) -> None:
    _platform()
    try:
        if verified_packages is not None or observed_packages is not None:
            _verify_slot_files(
                slot.model_dump(mode="json"),
                verified_python=(
                    None if verified_python is None else verified_python.model_dump(mode="json")
                ),
                verified_packages=verified_packages,
                observed_packages=observed_packages,
            )
        elif verified_python is None:
            _verify_slot_files(slot.model_dump(mode="json"))
        else:
            _verify_slot_files(
                slot.model_dump(mode="json"),
                verified_python=verified_python.model_dump(mode="json"),
            )
    except (OSError, ValueError):
        raise ManagedDeploymentError("managed_slot_changed") from None


def verify_current_slot(slot: SlotIdentity) -> None:
    """Verify this process; sharing the same resolved Python is insufficient."""
    verify_slot_files(slot)
    verify_current_process(slot)


def verify_current_process(slot: SlotIdentity) -> None:
    """Check process identity after the caller's synchronous full slot check."""
    import muli

    if (
        str(Path(sys.executable).absolute()) != slot.lexical_python
        or str(Path(sys.prefix).absolute()) != slot.prefix
        or Path(sys.prefix).absolute() == Path(sys.base_prefix).absolute()
        or not muli.__file__
        or str(Path(muli.__file__).absolute().parent) != slot.package.root
    ):
        raise ManagedDeploymentError("managed_process_mismatch")


class DeploymentStore(AbstractContextManager["DeploymentStore"]):
    """Content-addressed private files, published under the existing journal lock.

    No directories, locks, units or processes are created. An unreferenced file
    is preparation material, never evidence that a deployment was adopted.
    """

    def __init__(self, root: Path, limits: JournalLimits | None = None) -> None:
        _platform()
        self.root = absolute(str(root))
        self.directory = _Directory(self.root, create=False)
        self.limits = limits or JournalLimits()
        self._unconfirmed = False

    def __exit__(self, *args: object) -> None:
        self.directory.__exit__(*args)

    def _check(self) -> None:
        if self._unconfirmed:
            raise ManagedDeploymentError("managed_write_unconfirmed")
        self.directory.check()
        with directory(self.root, private=True) as descriptor:
            before, after = os.fstat(self.directory.fd), os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ManagedDeploymentError("managed_directory_changed")

    def _read(self, prefix: str, expected: str) -> bytes:
        self._check()
        if len(expected) != 64 or any(value not in "0123456789abcdef" for value in expected):
            raise ManagedDeploymentError("managed_digest_invalid")
        data = self.directory.read(f"{prefix}-{expected}.json", _PRIVATE_BYTES)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ManagedDeploymentError("managed_record_changed")
        self._check()
        return data

    def read_slot(self, expected: str) -> SlotIdentity:
        try:
            data = self._read("slot", expected)
            object_bytes(data)
            return SlotIdentity.model_validate_json(data)
        except (OSError, ValueError):
            raise ManagedDeploymentError("managed_slot_unavailable") from None

    def read_anchor(self, expected: str) -> DeploymentAnchor:
        try:
            result = DeploymentAnchor.model_validate(object_bytes(self._read("anchor", expected)))
            if result.journal_root != str(self.root):
                raise ValueError("foreign_root")
            return result
        except (OSError, ValueError):
            raise ManagedDeploymentError("anchor_unavailable") from None

    def _authenticate(
        self,
        anchor: DeploymentAnchor,
        slots: Mapping[str, SlotIdentity],
        *,
        selected: str | None,
        verify_selected: Callable[[SlotIdentity], None],
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        """Authenticate one synchronous backend guard from actual private bytes.

        ``verify_selected`` is the backend's trusted synchronous read-only slot
        verifier, not an extension hook. All evidence remains on this stack;
        the final leaf/root fences also follow the selected package scan.
        """
        if self._unconfirmed:
            raise ManagedDeploymentError("managed_write_unconfirmed")
        self.directory.check()
        if anchor.controller_slot not in slots or (selected is not None and selected not in slots):
            raise _AuthenticationMismatch("managed_deployment_changed")
        with directory(self.root, private=True) as descriptor:
            retained, current = os.fstat(self.directory.fd), os.fstat(descriptor)
            if (retained.st_dev, retained.st_ino) != (current.st_dev, current.st_ino):
                raise ManagedDeploymentError("managed_directory_changed")
            leaves: dict[str, tuple[dict[str, int], str]] = {}

            def read_record(prefix: str, expected: str, code: str) -> bytes:
                try:
                    if len(expected) != 64 or any(
                        value not in "0123456789abcdef" for value in expected
                    ):
                        raise ValueError("managed_digest_invalid")
                    name = f"{prefix}-{expected}.json"
                    data, observed = read_at(descriptor, name, _PRIVATE_BYTES, private=True)
                    if hashlib.sha256(data).hexdigest() != expected:
                        raise ValueError("managed_record_changed")
                except (OSError, ValueError):
                    raise ManagedDeploymentError(code) from None
                leaves[name] = observed, code
                return data

            try:
                actual_anchor = DeploymentAnchor.model_validate(
                    object_bytes(read_record("anchor", anchor.digest, "anchor_unavailable"))
                )
                if actual_anchor.journal_root != str(self.root):
                    raise ValueError("foreign_root")
            except (OSError, ValueError):
                raise ManagedDeploymentError("anchor_unavailable") from None
            if actual_anchor != anchor:
                raise _AuthenticationMismatch("managed_deployment_changed")

            def read_slot(expected: str) -> SlotIdentity:
                try:
                    data = read_record("slot", expected, "managed_slot_unavailable")
                    object_bytes(data)
                    actual = SlotIdentity.model_validate_json(data)
                except (OSError, ValueError):
                    raise ManagedDeploymentError("managed_slot_unavailable") from None
                if actual != slots[expected]:
                    raise _AuthenticationMismatch(
                        "managed_deployment_changed"
                        if expected == anchor.controller_slot
                        else "managed_slot_changed"
                    )
                return actual

            controller = read_slot(anchor.controller_slot)
            try:
                data, observed = read_at(descriptor, LAUNCHER_NAME, _LAUNCHER_BYTES, private=True)
                if {
                    "path": str(self.root / LAUNCHER_NAME),
                    "identity": observed,
                    "digest": hashlib.sha256(data).hexdigest(),
                } != anchor.launcher.model_dump(mode="json"):
                    raise ValueError("foreign_launcher")
                leaves[LAUNCHER_NAME] = observed, "anchor_unavailable"
                if verified_packages is None and observed_packages is None:
                    verify_slot_files(controller, verified_python=verified_python)
                else:
                    verify_slot_files(
                        controller,
                        verified_python=verified_python,
                        verified_packages=verified_packages,
                        observed_packages=observed_packages,
                    )
            except (OSError, ValueError):
                raise ManagedDeploymentError("anchor_unavailable") from None
            actual_slots = {anchor.controller_slot: controller}
            for expected in slots:
                if expected not in actual_slots:
                    actual_slots[expected] = read_slot(expected)
            if selected is not None and selected != anchor.controller_slot:
                verify_selected(actual_slots[selected])

            # Comparing all seven fields also rejects permission changes and a
            # same-inode rewrite whose caller restored only the old mtime.
            for name, (observed, code) in leaves.items():
                try:
                    if (
                        identity(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
                        != observed
                    ):
                        raise ValueError("managed_file_changed")
                except (OSError, ValueError):
                    raise ManagedDeploymentError(code) from None
            current = os.fstat(descriptor)
            if current.st_uid != os.geteuid() or stat.S_IMODE(current.st_mode) != 0o700:
                raise ManagedDeploymentError("managed_directory_not_private")
            # directory() performs its final full ancestor walk on normal exit.
            # On an exception it only closes the fd, preserving that first error.

    def _publish(self, name: str, data: bytes, *, guard: Callable[[], None]) -> None:
        if len(data) > _PRIVATE_BYTES:
            raise ManagedDeploymentError("managed_record_budget")
        guard()
        self._check()
        try:
            previous = self.directory.read(name, _PRIVATE_BYTES)
        except FileNotFoundError:
            previous = None
        if previous is not None and previous != data:
            raise ManagedDeploymentError("managed_record_changed")
        if previous is None:
            with os.scandir(self.directory.fd) as entries:
                for count, _ in enumerate(entries, 1):
                    if count >= self.limits.max_entries:
                        raise ManagedDeploymentError("managed_entry_budget")
            self._unconfirmed = True
            self.directory.replace(name, data)
            self._unconfirmed = False
        try:
            with ConfigurationFile(self.root / name, _PRIVATE_BYTES) as published:
                published.confirm(hashlib.sha256(data).hexdigest(), guard=guard)
        except BaseException:
            self._unconfirmed = True
            raise
        self._check()

    def publish_slot(self, slot: SlotIdentity, *, guard: Callable[[], None]) -> None:
        verify_slot_files(slot)
        self._publish(f"slot-{slot.digest}.json", slot.canonical(), guard=guard)
        verify_slot_files(slot)

    def publish_anchor(self, anchor: DeploymentAnchor, *, guard: Callable[[], None]) -> None:
        if anchor.journal_root != str(self.root):
            raise ManagedDeploymentError("managed_paths_changed")
        self.read_slot(anchor.controller_slot)
        self.verify_anchor(anchor)
        self._publish(f"anchor-{anchor.digest}.json", anchor.canonical(), guard=guard)

    def publish_launcher(self, *, guard: Callable[[], None]) -> FileFingerprint:
        """Copy the shipped standalone entry; it imports no installed package."""
        from muli.platforms import managed_launcher

        if not managed_launcher.__file__:
            raise ManagedDeploymentError("launcher_unavailable")
        data, _ = read_file(Path(managed_launcher.__file__).absolute(), _LAUNCHER_BYTES)
        self._publish(LAUNCHER_NAME, data, guard=guard)
        try:
            return FileFingerprint.model_validate(
                fingerprint(self.root / LAUNCHER_NAME, _LAUNCHER_BYTES)
            )
        except (OSError, ValueError):
            raise ManagedDeploymentError("launcher_unavailable") from None

    def verify_anchor(
        self,
        anchor: DeploymentAnchor,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        self._check()
        try:
            if anchor.journal_root != str(self.root) or fingerprint(
                self.root / LAUNCHER_NAME, _LAUNCHER_BYTES
            ) != anchor.launcher.model_dump(mode="json"):
                raise ValueError("foreign_launcher")
            if verified_packages is None and observed_packages is None:
                verify_slot_files(
                    self.read_slot(anchor.controller_slot), verified_python=verified_python
                )
            else:
                verify_slot_files(
                    self.read_slot(anchor.controller_slot),
                    verified_python=verified_python,
                    verified_packages=verified_packages,
                    observed_packages=observed_packages,
                )
        except (OSError, ValueError):
            raise ManagedDeploymentError("anchor_unavailable") from None


class SelectorStore(AbstractContextManager["SelectorStore"]):
    """One selector; no extra confirmed/mode state or implicit legacy fallback."""

    def __init__(self, root: Path) -> None:
        _platform()
        self.root = absolute(str(root))
        self.file = ConfigurationFile(self.root / SELECTOR_NAME, _SELECTOR_BYTES)
        self._unconfirmed = False
        try:
            self._check()
        except BaseException:
            self.file.__exit__()
            raise

    def __exit__(self, *args: object) -> None:
        self.file.__exit__(*args)

    def _check(self) -> None:
        if self._unconfirmed:
            raise ManagedDeploymentError("managed_write_unconfirmed")
        self.file._check()
        info = os.fstat(self.file.fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ManagedDeploymentError("managed_directory_not_private")

    def read(self) -> SelectorSnapshot | None:
        self._check()
        try:
            snapshot = self.file.read()
        except FileNotFoundError:
            return None
        try:
            info = os.stat(SELECTOR_NAME, dir_fd=self.file.fd, follow_symlinks=False)
            if (
                stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                != snapshot.identity
            ):
                raise ValueError("not_private")
            value = SelectorValue.model_validate(object_bytes(snapshot.data))
        except ValueError:
            raise ManagedDeploymentError("managed_selector_invalid") from None
        return SelectorSnapshot(value, snapshot.data, snapshot.identity)

    def _expect(self, expected: SelectorSnapshot | None) -> None:
        if self.read() != expected:
            raise ManagedDeploymentError("managed_selector_changed")

    def confirm(
        self, expected: SelectorSnapshot | None, *, guard: Callable[[], None]
    ) -> SelectorSnapshot | None:
        if expected is None:
            guard()
        self._expect(expected)
        if expected is None:
            self._unconfirmed = True
            os.fsync(self.file.fd)
            self._unconfirmed = False
        else:
            self.file.confirm(expected.digest, guard=guard)
        self._expect(expected)
        return self.read()

    def replace(
        self,
        expected: SelectorSnapshot | None,
        desired: SelectorValue | None,
        *,
        guard: Callable[[], None],
    ) -> SelectorSnapshot | None:
        guard()
        self._expect(expected)
        if expected is not None and expected.value == desired:
            return self.confirm(expected, guard=guard)
        if expected is not None and desired is not None:
            self.file.replace(expected.digest, desired.canonical(), guard=guard)
        elif desired is None:
            if expected is not None:
                guard()
                self._expect(expected)
                guard()
                self._unconfirmed = True
                os.unlink(SELECTOR_NAME, dir_fd=self.file.fd)
                os.fsync(self.file.fd)
                self._unconfirmed = False
        else:
            # The existing controller and writer leases serialize the absence
            # check and this initial publication. There is no new lock inode.
            guard()
            self._expect(None)
            self._create(desired.canonical(), guard=guard)
        installed = self.read()
        if (installed.value if installed else None) != desired:
            raise ManagedDeploymentError("managed_selector_changed")
        return self.confirm(installed, guard=guard)

    def _create(self, data: bytes, *, guard: Callable[[], None]) -> None:
        temporary = ".tmp-" + secrets.token_hex(16)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.file.fd,
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self._expect(None)
            # A synchronous fsync may consume the remaining effect budget.
            # Recheck the real leases and deadline immediately before rename.
            guard()
            self._unconfirmed = True
            os.replace(temporary, SELECTOR_NAME, src_dir_fd=self.file.fd, dst_dir_fd=self.file.fd)
            os.fsync(self.file.fd)
            self._unconfirmed = False
        finally:
            try:
                os.unlink(temporary, dir_fd=self.file.fd)
            except FileNotFoundError:
                pass
