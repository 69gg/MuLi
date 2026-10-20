"""Private, bounded identity for a same-version Linux service transaction.

This module observes files only. It neither controls a service nor proves that
its dependencies, cgroup, writer lease or database contents are healthy.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

import muli
from muli.management_transaction import (
    Digest,
    JournalControl,
    JournalLimits,
    ProjectionReceipt,
    StrictRecord,
    TransactionError,
    _Directory,
    _object,
)
from muli.platforms.transaction_files import _open_directory
from muli.service import UNIT, UserService

_MANIFEST_BYTES = 131_072
_LINKS = 40
_PATH_BYTES = 4096
AbsolutePath = Annotated[str, Field(min_length=1, max_length=_PATH_BYTES)]


class InstallationError(ValueError):
    """A stable reason code, without configuration or environment contents."""


class HelperLimits(StrictRecord):
    seconds: float = Field(default=180.0, gt=0, le=900, allow_inf_nan=False)
    rollback_reserve: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    operation_timeout: float = Field(default=20.0, gt=0, le=30, allow_inf_nan=False)
    poll_interval: float = Field(default=0.25, gt=0, le=2, allow_inf_nan=False)
    memory_bytes: int = Field(default=128 * 1024 * 1024, ge=64 * 1024 * 1024, le=256 * 1024 * 1024)
    cpu_percent: int = Field(default=25, ge=1, le=100)
    tasks: int = Field(default=32, ge=8, le=64)
    stop_seconds: float = Field(default=2.0, gt=0, le=5, allow_inf_nan=False)
    restart_burst: int = Field(default=3, ge=1, le=3)
    restart_delay: float = Field(default=0.5, gt=0, le=5, allow_inf_nan=False)

    @model_validator(mode="after")
    def reserve_fits(self) -> Self:
        if self.rollback_reserve >= self.seconds:
            raise ValueError("invalid_reserve")
        return self


class PackageLimits(StrictRecord):
    entries: int = Field(default=512, ge=1, le=2048)
    total_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=32 * 1024 * 1024)
    file_bytes: int = Field(default=4 * 1024 * 1024, ge=1, le=8 * 1024 * 1024)
    depth: int = Field(default=16, ge=1, le=32)


class FileIdentity(StrictRecord):
    device: int
    inode: int
    size: int = Field(ge=0)
    modified_ns: int
    changed_ns: int
    mode: int
    uid: int

    @classmethod
    def of(cls, value: os.stat_result) -> Self:
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            size=value.st_size,
            modified_ns=value.st_mtime_ns,
            changed_ns=value.st_ctime_ns,
            mode=value.st_mode,
            uid=value.st_uid,
        )


class FileFingerprint(StrictRecord):
    path: AbsolutePath
    identity: FileIdentity
    digest: Digest

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _absolute(value)


class InterpreterIdentity(StrictRecord):
    executable: AbsolutePath
    real_executable: AbsolutePath
    identity: FileIdentity
    prefix: AbsolutePath
    base_prefix: AbsolutePath
    pyvenv: FileFingerprint | None

    @field_validator("executable", "real_executable", "prefix", "base_prefix")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _absolute(value)


class PackageFingerprint(StrictRecord):
    root: AbsolutePath
    digest: Digest
    files: int = Field(ge=1)
    size: int = Field(ge=0)

    @field_validator("root")
    @classmethod
    def valid_root(cls, value: str) -> str:
        return _absolute(value)


def _absolute(value: str) -> str:
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or len(os.fsencode(value)) > _PATH_BYTES
        or any(character in value for character in "\x00\r\n")
    ):
        raise ValueError("installation_path")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def _service_digest(service: UserService) -> str:
    value = {"unit": service.render(), "argv": service._arguments()}
    return hashlib.sha256(_canonical(value)).hexdigest()


def _same_directory(path: Path, descriptor: int) -> None:
    current = _open_directory(path)
    try:
        before, after = os.fstat(descriptor), os.fstat(current)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise InstallationError("installation_directory_changed")
    finally:
        os.close(current)


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    descriptor = _open_directory(path)
    try:
        yield descriptor
        _same_directory(path, descriptor)
    finally:
        os.close(descriptor)


def _read_at(
    directory: int, name: str, limit: int, *, contents: bool = True, private_owner: bool = False
) -> tuple[bytes, FileIdentity]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory
    )
    with os.fdopen(descriptor, "rb") as source:
        before = FileIdentity.of(os.fstat(source.fileno()))
        if not stat.S_ISREG(before.mode) or (private_owner and before.uid != os.geteuid()):
            raise InstallationError("installation_not_regular")
        if contents and before.size > limit:
            raise InstallationError("installation_file_budget")
        data = source.read(limit + 1) if contents else b""
        after = FileIdentity.of(os.fstat(source.fileno()))
    current = FileIdentity.of(os.stat(name, dir_fd=directory, follow_symlinks=False))
    if len(data) > limit or before != after or before != current:
        raise InstallationError("installation_file_changed")
    return data, before


def _read_file(
    path: Path, limit: int, *, contents: bool = True, private_owner: bool = False
) -> tuple[bytes, FileIdentity]:
    with _directory(path.parent) as descriptor:
        return _read_at(
            descriptor, path.name, limit, contents=contents, private_owner=private_owner
        )


def _interpreter(executable: str, limits: PackageLimits) -> InterpreterIdentity:
    entry = Path(_absolute(executable))
    # A second venv can share the same real binary while importing other code.
    # Capture is supported only for the interpreter actually running this host.
    if str(entry) != str(Path(sys.executable).absolute()):
        raise InstallationError("foreign_interpreter_entry")
    target = entry
    links: list[tuple[Path, FileIdentity, str]] = []
    for _ in range(_LINKS):
        with _directory(target.parent) as descriptor:
            before = FileIdentity.of(os.stat(target.name, dir_fd=descriptor, follow_symlinks=False))
            if stat.S_ISLNK(before.mode):
                value = os.readlink(target.name, dir_fd=descriptor)
                if before != FileIdentity.of(
                    os.stat(target.name, dir_fd=descriptor, follow_symlinks=False)
                ):
                    raise InstallationError("interpreter_changed")
                links.append((target, before, value))
                target = Path(os.path.normpath(target.parent / value))
                _absolute(str(target))
                continue
            _, identity = _read_at(descriptor, target.name, 0, contents=False)
            if not identity.mode & 0o111:
                raise InstallationError("interpreter_not_executable")
            break
    else:
        raise InstallationError("interpreter_link_budget")
    for path, expected, value in links:
        with _directory(path.parent) as descriptor:
            if (
                FileIdentity.of(os.stat(path.name, dir_fd=descriptor, follow_symlinks=False))
                != expected
                or os.readlink(path.name, dir_fd=descriptor) != value
            ):
                raise InstallationError("interpreter_changed")
    prefix = str(Path(sys.prefix).resolve(strict=True))
    base_prefix = str(Path(sys.base_prefix).resolve(strict=True))
    with _directory(Path(prefix)), _directory(Path(base_prefix)):
        pass
    pyvenv: FileFingerprint | None = None
    for parent in (entry.parent, entry.parent.parent):
        path = parent / "pyvenv.cfg"
        try:
            data, fingerprint = _read_file(path, limits.file_bytes)
        except FileNotFoundError:
            continue
        pyvenv = FileFingerprint(
            path=str(path), identity=fingerprint, digest=hashlib.sha256(data).hexdigest()
        )
        if str(parent) != prefix:
            raise InstallationError("interpreter_prefix_mismatch")
        break
    if (prefix != base_prefix) != (pyvenv is not None):
        raise InstallationError("interpreter_prefix_mismatch")
    return InterpreterIdentity(
        executable=str(entry),
        real_executable=str(target),
        identity=identity,
        prefix=prefix,
        base_prefix=base_prefix,
        pyvenv=pyvenv,
    )


def _package_root() -> Path:
    if not muli.__file__:
        raise InstallationError("package_location_unknown")
    return Path(muli.__file__).resolve(strict=True).parent


def _package(limits: PackageLimits) -> PackageFingerprint:
    root = _package_root()
    digest = hashlib.sha256()
    visited, files, size = 0, 0, 0

    def walk(descriptor: int, prefix: str, depth: int) -> None:
        nonlocal visited, files, size
        before = FileIdentity.of(os.fstat(descriptor))
        names: list[str] = []
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                visited += 1
                if visited > limits.entries:
                    raise InstallationError("package_entry_budget")
                if entry.name == "__pycache__" or entry.name.endswith((".pyc", ".pyo")):
                    continue
                names.append(entry.name)
        for name in sorted(names):
            relative = prefix + name
            _absolute(str(root / relative))
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(current.st_mode):
                if depth >= limits.depth:
                    raise InstallationError("package_depth_budget")
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                try:
                    if FileIdentity.of(current) != FileIdentity.of(os.fstat(child)):
                        raise InstallationError("installation_file_changed")
                    digest.update(_canonical([relative, "directory"]) + b"\n")
                    walk(child, relative + "/", depth + 1)
                    if FileIdentity.of(current) != FileIdentity.of(
                        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    ):
                        raise InstallationError("installation_file_changed")
                finally:
                    os.close(child)
            else:
                data, identity = _read_at(
                    descriptor, name, min(limits.file_bytes, limits.total_bytes - size)
                )
                size += identity.size
                files += 1
                digest.update(
                    _canonical([relative, identity.size, hashlib.sha256(data).hexdigest()]) + b"\n"
                )
        if before != FileIdentity.of(os.fstat(descriptor)):
            raise InstallationError("installation_directory_changed")

    with _directory(root) as descriptor:
        walk(descriptor, "", 0)
    if files == 0:
        raise InstallationError("package_empty")
    return PackageFingerprint(root=str(root), digest=digest.hexdigest(), files=files, size=size)


class InstallationManifest(StrictRecord):
    protocol: Literal[1] = 1
    config_path: AbsolutePath
    data_dir: AbsolutePath
    database_path: AbsolutePath
    unit_path: AbsolutePath
    service_digest: Digest
    interpreter: InterpreterIdentity
    package: PackageFingerprint
    helper: HelperLimits = Field(default_factory=HelperLimits)
    journal: JournalLimits = Field(default_factory=JournalLimits)
    package_limits: PackageLimits = Field(default_factory=PackageLimits)

    @field_validator("config_path", "data_dir", "database_path", "unit_path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _absolute(value)

    @model_validator(mode="after")
    def standard_unit(self) -> Self:
        if Path(self.unit_path).name != UNIT:
            raise ValueError("unsupported_unit_name")
        return self

    def canonical(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    def user_service(self) -> UserService:
        return UserService(
            Path(self.config_path),
            Path(self.data_dir),
            Path(self.unit_path).parent,
            platform="linux",
            executable=self.interpreter.executable,
        )

    def _paths_and_unit(self, *, require_database: bool = True) -> None:
        if sys.platform != "linux":
            raise InstallationError("platform_unsupported")
        _read_file(Path(self.config_path), 0, contents=False, private_owner=True)
        database = Path(self.database_path)
        with _directory(database.parent) as descriptor:
            try:
                _read_at(descriptor, database.name, 0, contents=False, private_owner=True)
            except FileNotFoundError:
                if require_database:
                    raise
        with _directory(Path(self.data_dir)):
            pass
        expected = self.user_service().render().encode("utf-8")
        content, _ = _read_file(Path(self.unit_path), len(expected), private_owner=True)
        if content != expected:
            raise InstallationError("unsupported_unit_file")
        if self.service_digest != _service_digest(self.user_service()):
            raise InstallationError("service_identity_changed")

    @classmethod
    def capture(
        cls,
        service: UserService,
        database: Path,
        *,
        helper: HelperLimits | None = None,
        journal: JournalLimits | None = None,
        package_limits: PackageLimits | None = None,
        require_database: bool = True,
    ) -> Self:
        if sys.platform != "linux" or service.platform != "linux":
            raise InstallationError("platform_unsupported")
        limits = package_limits or PackageLimits()
        config_path = _absolute(str(service.config_path.absolute()))
        data_dir = _absolute(str(service.data_dir.absolute()))
        unit_path = _absolute(str(service.path.absolute()))
        standard = UserService(
            Path(config_path),
            Path(data_dir),
            Path(unit_path).parent,
            platform="linux",
            executable=service.executable,
        )
        result = cls(
            config_path=config_path,
            data_dir=data_dir,
            database_path=str(database.absolute()),
            unit_path=unit_path,
            service_digest=_service_digest(standard),
            interpreter=_interpreter(service.executable, limits),
            package=_package(limits),
            helper=helper or HelperLimits(),
            journal=journal or JournalLimits(),
            package_limits=limits,
        )
        result._paths_and_unit(require_database=require_database)
        return result

    @classmethod
    def load(cls, root: Path) -> Self:
        path = Path(_absolute(str(root.absolute())))
        with _Directory(path, create=False) as directory:
            with _directory(path):
                data = directory.read("installation.json", _MANIFEST_BYTES)
        try:
            result = cls.model_validate(_object(data))
        except ValueError:
            raise InstallationError("invalid_installation") from None
        if len(data) > result.journal.max_record_bytes:
            raise InstallationError("installation_budget")
        return result

    def verify(self, *, require_database: bool = True) -> None:
        self._paths_and_unit(require_database=require_database)
        if self.interpreter != _interpreter(self.interpreter.executable, self.package_limits):
            raise InstallationError("interpreter_changed")
        if str(_package_root()) != self.package.root or self.package != _package(
            self.package_limits
        ):
            raise InstallationError("package_changed")

    def publish(
        self, control: JournalControl, *, projected: ProjectionReceipt | None = None
    ) -> None:
        control.check()
        if (
            str(control.journal.database) != self.database_path
            or control.journal.limits != self.journal
        ):
            raise InstallationError("foreign_installation_journal")
        self.verify()
        data = self.canonical()
        if len(data) > self.journal.max_record_bytes:
            raise InstallationError("installation_budget")
        record = control.read()
        try:
            previous = self.load(control.directory.path)
        except FileNotFoundError:
            previous = None
        if record is not None:
            if previous is None:
                raise InstallationError("foreign_installation_record")
            matches = (
                record.identity.installation == previous.digest
                and record.identity.service == previous.service_digest
            )
            if previous != self or not matches:
                if not record.resolved:
                    if not matches:
                        raise InstallationError("foreign_installation_record")
                    raise TransactionError("transaction_in_progress")
                expected = ProjectionReceipt(
                    **record.position.model_dump(), store_instance=record.identity.store_instance
                )
                if projected != expected:
                    raise TransactionError("projection_required")
        # The existing poisoned write primitive confirms both file and parent,
        # including adoption of identical visible bytes after an interrupted fsync.
        control._replace("installation.json", data)
