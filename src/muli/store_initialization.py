"""Cooperative database lock; Runtime owners retain it until connection close."""

from __future__ import annotations

import errno
import os
import stat
import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Self


class StoreInitializationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class StoreConstructionLock(AbstractContextManager["StoreConstructionLock"]):
    """Lock one fixed inode without polling; the caller controls its lifetime."""

    def __init__(self, database: Path) -> None:
        self.database = database.resolve()
        self.path = self.database.with_name("." + self.database.name + "-initialization.lock")
        self.fd: int | None = None

    def __enter__(self) -> Self:
        if self.fd is not None:
            raise StoreInitializationError("store_initialization_lock_owned")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.set_inheritable(descriptor, False)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StoreInitializationError("store_initialization_lock_invalid")
            if sys.platform != "win32" and (
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise StoreInitializationError("store_initialization_lock_invalid")
            try:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise StoreInitializationError("store_initialization_busy") from None
                raise
            self.fd = descriptor
            self.check()
            return self
        except BaseException:
            self.fd = None
            os.close(descriptor)
            raise

    def check(self) -> None:
        if self.fd is None or _identity(os.fstat(self.fd)) != _identity(self.path.lstat()):
            raise StoreInitializationError("store_initialization_lock_changed")

    def __exit__(self, *args: object) -> None:
        if self.fd is not None:
            descriptor, self.fd = self.fd, None
            try:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            finally:
                # Never unlink it: a waiter must not acquire a different inode.
                os.close(descriptor)


class CreatedDatabase(AbstractContextManager["CreatedDatabase"]):
    """One ephemeral O_EXCL claim; existing empty files never get this evidence."""

    def __init__(self, path: Path, descriptor: int) -> None:
        self.path = path
        self.fd: int | None = descriptor
        self.identity = _identity(os.fstat(descriptor))

    @classmethod
    def claim(cls, path: Path) -> CreatedDatabase | None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            return None
        try:
            os.set_inheritable(descriptor, False)
            created = cls(path, descriptor)
            created.check()
            return created
        except BaseException:
            os.close(descriptor)
            raise

    def check(self) -> None:
        if self.fd is None:
            raise StoreInitializationError("store_creation_claim_closed")
        info = os.fstat(self.fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or _identity(info) != self.identity
            or _identity(self.path.lstat()) != self.identity
        ):
            raise StoreInitializationError("store_creation_identity_changed")

    def __exit__(self, *args: object) -> None:
        if self.fd is not None:
            descriptor, self.fd = self.fd, None
            os.close(descriptor)
