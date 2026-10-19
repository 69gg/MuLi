"""Bounded local attachment storage; upload never extracts or injects file contents."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from muli.config import ConfigFile
from muli.store import Store, new_id, now

SAFE_INLINE = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "audio/wav",
    "audio/mpeg",
    "audio/ogg",
    "audio/flac",
    "audio/mp4",
    "audio/webm",
    "video/mp4",
    "video/webm",
    "video/ogg",
}


async def file_io[T](operation: Callable[[], T]) -> T:
    """Do not close a file handle while its cancelled to_thread call is still writing."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await task
        raise


def detected_media(header: bytes, claimed: str) -> str:
    """Only known passive media gets an inline preview; claimed HTML/SVG stays a file."""
    claimed = claimed.lower().split(";", 1)[0].strip()
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF":
        if header[8:12] == b"WEBP":
            return "image/webp"
        if header[8:12] == b"WAVE":
            return "audio/wav"
    if header.startswith(b"fLaC"):
        return "audio/flac"
    if header.startswith(b"ID3") or (
        len(header) > 1 and header[0] == 255 and header[1] & 224 == 224
    ):
        return "audio/mpeg"
    if header.startswith(b"OggS"):
        return "video/ogg" if claimed == "video/ogg" else "audio/ogg"
    if header[4:8] == b"ftyp":
        return "audio/mp4" if claimed == "audio/mp4" else "video/mp4"
    if header.startswith(b"\x1a\x45\xdf\xa3") and claimed in {"audio/webm", "video/webm"}:
        return claimed
    return "application/octet-stream"


class Attachments:
    def __init__(self, store: Store, directory: Path, config: ConfigFile) -> None:
        self.store, self.directory, self.config = store, directory, config
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.capacity = asyncio.Semaphore(config.settings.uploads.max_concurrent)
        self.reserved = 0

    def get(self, identifier: str) -> dict[str, Any]:
        item = self.store.get("attachment", identifier)
        if item is None:
            raise KeyError(identifier)
        return item

    def path(self, identifier: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise KeyError(identifier)
        self.get(identifier)
        path = self.directory / identifier
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("附件文件不存在")
        return path

    def public(self, item: dict[str, Any]) -> dict[str, Any]:
        identifier = item["id"]
        inline = item["mime_type"] in SAFE_INLINE
        return {
            **item,
            "url": f"/api/uploads/{identifier}/download",
            "preview_url": f"/api/uploads/{identifier}/content" if inline else None,
            "kind": item["mime_type"].split("/", 1)[0] if inline else "file",
        }

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        return [self.public(item) for item in self.store.list("attachment", limit)]

    def used_bytes(self) -> int:
        row = self.store.db.execute(
            "SELECT COALESCE(SUM(json_extract(data,'$.size')),0) FROM objects WHERE kind='attachment'"
        ).fetchone()
        return int(row[0])

    async def save(
        self, chunks: AsyncIterator[bytes], name: str, mime_type: str = "", *, source: str = "web"
    ) -> dict[str, Any]:
        name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or len(name.encode("utf-8")) > 240 or any(ord(c) < 32 for c in name):
            raise ValueError("附件文件名无效")
        async with self.capacity:
            identifier = new_id()
            fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=self.directory)
            target = self.directory / identifier
            size = 0
            header = bytearray()
            digest = hashlib.sha256()
            try:
                with os.fdopen(fd, "wb") as handle:
                    async for chunk in chunks:
                        limit = self.config.settings.uploads
                        if size + len(chunk) > limit.max_file_bytes:
                            raise ValueError("附件超过单文件大小限制")
                        if self.used_bytes() + self.reserved + len(chunk) > limit.max_total_bytes:
                            raise ValueError("附件存储配额不足")
                        size += len(chunk)
                        self.reserved += len(chunk)
                        header.extend(chunk[: max(0, 512 - len(header))])
                        digest.update(chunk)

                        def write(data: bytes = chunk) -> int:
                            return handle.write(data)

                        await file_io(write)
                    handle.flush()
                    await file_io(lambda: os.fsync(handle.fileno()))
                item = {
                    "id": identifier,
                    "name": name,
                    "mime_type": detected_media(bytes(header), mime_type),
                    "size": size,
                    "sha256": digest.hexdigest(),
                    "source": source,
                    "created_at": now(),
                }
                os.replace(temporary, target)
                self.store.put("attachment", item)
                return self.public(item)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            finally:
                self.reserved -= size
                with contextlib.suppress(FileNotFoundError):
                    Path(temporary).unlink()

    async def import_file(self, path: Path, *, source: str = "local") -> dict[str, Any]:
        import mimetypes

        async def chunks() -> AsyncIterator[bytes]:
            with path.open("rb") as handle:
                while chunk := await file_io(lambda: handle.read(65536)):
                    yield chunk

        return await self.save(
            chunks(), path.name, mimetypes.guess_type(path.name)[0] or "", source=source
        )
