"""Bounded cooperative config CAS, anchored to an existing Linux directory.

The caller owns the journal and writer leases. This is not a kernel content-CAS
primitive or protection from an administrator ignoring those cooperative locks.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path


class ConfigurationError(ValueError):
    pass


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _open_directory(path: Path) -> int:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@dataclass(frozen=True, slots=True)
class ConfigurationSnapshot:
    data: bytes = field(repr=False)
    identity: tuple[int, int, int, int, int]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class ConfigurationFile(AbstractContextManager["ConfigurationFile"]):
    """No mkdir, chmod of existing paths, link following, or unbounded reads."""

    def __init__(self, path: Path, limit: int) -> None:
        if sys.platform != "linux":
            raise ConfigurationError("platform_unsupported")
        if type(limit) is not int or not 1 <= limit <= 4_194_304:
            raise ConfigurationError("configuration_budget")
        self.path = path.absolute()
        if ".." in self.path.parts or self.path == Path("/"):
            raise ConfigurationError("configuration_path")
        self.limit = limit
        self.fd = _open_directory(self.path.parent)
        self._unconfirmed = False

    def __exit__(self, *args: object) -> None:
        os.close(self.fd)

    def _check(self) -> None:
        if self._unconfirmed:
            raise ConfigurationError("configuration_unconfirmed")
        current = _open_directory(self.path.parent)
        try:
            before, after = os.fstat(self.fd), os.fstat(current)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ConfigurationError("configuration_changed")
        finally:
            os.close(current)

    def read(self) -> ConfigurationSnapshot:
        self._check()
        descriptor = os.open(
            self.path.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=self.fd,
        )
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
                raise ConfigurationError("configuration_not_regular")
            if before.st_size > self.limit:
                raise ConfigurationError("configuration_budget")
            data = source.read(self.limit + 1)
            after = os.fstat(source.fileno())
        current = os.stat(self.path.name, dir_fd=self.fd, follow_symlinks=False)
        if (
            len(data) > self.limit
            or _identity(before) != _identity(after)
            or _identity(before) != _identity(current)
        ):
            raise ConfigurationError("configuration_changed")
        self._check()
        return ConfigurationSnapshot(data, _identity(before))

    def replace(
        self, expected_digest: str, data: bytes, *, guard: Callable[[], None]
    ) -> ConfigurationSnapshot:
        """Install exact bytes only while the caller's synchronous guard holds.

        Revalidate the journal lock, writer lease and empty cgroup in ``guard``.
        It must not start asynchronous work. A directory-sync failure poisons
        this handle: visible new bytes alone are never a success receipt.
        """
        if not data or len(data) > self.limit:
            raise ConfigurationError("configuration_budget")
        guard()
        before = self.read()
        if before.digest != expected_digest:
            raise ConfigurationError("configuration_changed")
        temporary = ".muli-config-" + secrets.token_hex(16)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.fd,
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            guard()
            if self.read() != before:
                raise ConfigurationError("configuration_changed")
            self._unconfirmed = True
            os.replace(temporary, self.path.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
            self._unconfirmed = False
            installed = self.read()
            if installed.data != data:
                raise ConfigurationError("configuration_changed")
            return installed
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def confirm(self, expected_digest: str, *, guard: Callable[[], None]) -> ConfigurationSnapshot:
        """Adopt visible bytes after a prior writer may have died before fsync.

        Required even when a reopened file already has the desired digest.
        A plain read does not prove that the containing rename was durable.
        """
        guard()
        before = self.read()
        if before.digest != expected_digest:
            raise ConfigurationError("configuration_changed")
        guard()
        self._check()
        descriptor = os.open(
            self.path.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=self.fd,
        )
        try:
            if _identity(os.fstat(descriptor)) != before.identity:
                raise ConfigurationError("configuration_changed")
            self._unconfirmed = True
            os.fsync(descriptor)
            os.fsync(self.fd)
            self._unconfirmed = False
        finally:
            os.close(descriptor)
        after = self.read()
        if after != before:
            raise ConfigurationError("configuration_changed")
        return after
