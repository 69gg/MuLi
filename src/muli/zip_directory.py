"""Bound a classic ZIP central directory before constructing member objects."""

from __future__ import annotations

import struct
from typing import BinaryIO, Literal

_EOCD = struct.Struct("<4s4H2IH")


class ZipDirectoryError(ValueError):
    def __init__(
        self, reason: Literal["invalid", "budget"], *, limit_exceeded: bool = False
    ) -> None:
        self.reason = reason
        self.limit_exceeded = limit_exceeded
        super().__init__(reason)


def check_zip_directory(
    source: BinaryIO, size: int, *, max_entries: int, max_directory_bytes: int
) -> int:
    """Keep the wheel preflight's exact classic-ZIP validation and ordering."""
    source.seek(max(0, size - (_EOCD.size + 65_535)))
    tail = source.read(_EOCD.size + 65_535)
    position = tail.rfind(b"PK\x05\x06")
    if position < 0 or position + _EOCD.size > len(tail):
        raise ZipDirectoryError("invalid")
    _, disk, central_disk, disk_count, count, central_size, offset, comment = _EOCD.unpack_from(
        tail, position
    )
    if (
        disk != 0
        or central_disk != 0
        or disk_count != count
        or count == 0
        or count == 65_535
        or count > max_entries
        or central_size > max_directory_bytes
        or offset + central_size != size - _EOCD.size - comment
        or position + _EOCD.size + comment != len(tail)
    ):
        raise ZipDirectoryError(
            "budget", limit_exceeded=count > max_entries or central_size > max_directory_bytes
        )
    source.seek(0)
    return int(count)
