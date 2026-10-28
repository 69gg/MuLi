"""Private file integrity checks shared by independently authorized readers."""

from __future__ import annotations

import codecs
import hashlib
import os
import stat
from pathlib import Path

from muli.evidence import EvidenceError


def _version(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_verified_window(
    path: Path,
    *,
    expected: str,
    size: int,
    offset_bytes: int,
    window: int,
    prefix: str,
    label: str,
    validate_text: bool = False,
) -> bytes:
    """Read a bounded window only after checking the complete regular file."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise EvidenceError(f"{prefix}_changed", f"{label}工件已变化")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size != size
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise EvidenceError(f"{prefix}_changed", f"{label}工件已变化")
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")("strict") if validate_text else None
        remaining = size
        while remaining:
            chunk = handle.read(min(65536, remaining))
            if not chunk:
                raise EvidenceError(f"{prefix}_changed", f"{label}工件已变化")
            digest.update(chunk)
            remaining -= len(chunk)
            if decoder is not None:
                if b"\x00" in chunk:
                    raise EvidenceError("attachment_binary_data", "附件包含二进制数据")
                try:
                    decoder.decode(chunk)
                except UnicodeDecodeError as exc:
                    raise EvidenceError(
                        "attachment_encoding_error", "附件不是UTF-8文本，请使用对应的图像或音频工具"
                    ) from exc
        if handle.read(1) or digest.hexdigest() != expected:
            raise EvidenceError(f"{prefix}_changed", f"{label}工件摘要不匹配")
        if decoder is not None:
            try:
                decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise EvidenceError(
                    "attachment_encoding_error", "附件不是UTF-8文本，请使用对应的图像或音频工具"
                ) from exc
        handle.seek(offset_bytes)
        raw = handle.read(min(window, size - offset_bytes))
        if _version(opened) != _version(os.fstat(handle.fileno())):
            raise EvidenceError(f"{prefix}_changed", f"{label}读取期间已变化")
    if _version(before) != _version(path.lstat()):
        raise EvidenceError(f"{prefix}_changed", f"{label}路径已变化")
    return raw
