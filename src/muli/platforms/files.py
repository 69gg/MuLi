"""Private, bounded local artifacts shared by capture and voice adapters."""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

ProcessRunner = Callable[..., Awaitable[dict[str, Any]]]


class LocalFiles:
    def __init__(self, root: Path, max_file_bytes: int, max_total_bytes: int) -> None:
        self.root = root
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.lock = asyncio.Lock()

    def used_bytes(self) -> int:
        if self.root.is_symlink():
            raise ValueError("采集目录不能是符号链接")
        if not self.root.exists():
            return 0
        return sum(
            path.stat().st_size
            for path in self.root.rglob("*")
            if not path.is_symlink() and path.is_file()
        )

    def allocate(self, suffix: str) -> Path:
        if self.used_bytes() >= self.max_total_bytes:
            raise ValueError("本地采集目录已达到容量限制，请先清理文件")
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = self.root / (uuid4().hex + suffix)
        path.touch(mode=0o600, exist_ok=False)
        return path

    def validate(self, path: Path) -> int:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("采集结果必须是普通文件")
        if details.st_size <= 0:
            raise ValueError("采集没有生成有效文件")
        if details.st_size > self.max_file_bytes or self.used_bytes() > self.max_total_bytes:
            raise ValueError("采集文件超过容量限制")
        path.chmod(0o600)
        return details.st_size

    async def run_output(
        self,
        runner: ProcessRunner,
        arguments: list[str],
        path: Path,
        *,
        timeout: float,
        expected_codes: tuple[int, ...] = (0,),
    ) -> None:
        task: asyncio.Future[dict[str, Any]] = asyncio.ensure_future(
            runner(arguments, timeout=timeout, limit=24000)
        )
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.1)
                if path.is_symlink():
                    raise ValueError("采集结果不能是符号链接")
                if path.exists() and (
                    path.stat().st_size > self.max_file_bytes
                    or self.used_bytes() > self.max_total_bytes
                ):
                    raise ValueError("采集文件超过容量限制，已停止")
            response = await task
            if response["exit_code"] not in expected_codes:
                raise ValueError("系统采集失败，请检查桌面权限、输入设备和后端配置")
            self.validate(path)
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            path.unlink(missing_ok=True)
            raise

    def save(self, data: bytes, suffix: str) -> Path:
        if not data or len(data) > self.max_file_bytes:
            raise ValueError("媒体文件为空或超过容量限制")
        if self.used_bytes() + len(data) > self.max_total_bytes:
            raise ValueError("本地采集目录已达到容量限制")
        path = self.allocate(suffix)
        try:
            with path.open("wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self.validate(path)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path


def artifact(path: Path, mime_type: str, **metadata: Any) -> dict[str, Any]:
    return {
        "id": path.stem,
        "path": str(path),
        "name": path.name,
        "size": path.stat().st_size,
        "mime_type": mime_type,
        **metadata,
    }
