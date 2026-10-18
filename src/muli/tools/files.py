"""Bounded file management with a recoverable trash instead of permanent delete."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from muli.config import Settings
from muli.store import new_id, now
from muli.tools import Tool, parameters, string


class Files:
    def __init__(self, settings: Settings, data_dir: Path) -> None:
        self.settings = settings
        self.roots = [Path(root).expanduser().resolve() for root in settings.workspace_roots]
        self.trash = data_dir / "trash"
        self._lock = asyncio.Lock()

    def path(self, value: str) -> Path:
        if not value:
            raise ValueError("路径不能为空")
        candidate = Path(value).expanduser().resolve()
        if not any(candidate == root or root in candidate.parents for root in self.roots):
            raise PermissionError("路径不在配置的访问目录内")
        if candidate == self.trash.resolve() or self.trash.resolve() in candidate.parents:
            raise PermissionError("回收站请使用专用恢复工具")
        return candidate

    def editable(self, value: str) -> Path:
        path = self.path(value)
        if path in self.roots or path == Path(path.anchor):
            raise PermissionError("不能修改访问根目录本身")
        return path

    def _read(self, path: Path) -> str:
        if not path.is_file():
            raise ValueError("需要普通文件")
        with path.open("rb") as handle:
            data = handle.read(self.settings.limits.max_file_bytes + 1)
        if len(data) > self.settings.limits.max_file_bytes:
            raise ValueError("文件超过读取上限")
        return data.decode("utf-8")

    def _execute(
        self,
        operation: str,
        args: dict[str, Any],
        cancelled: threading.Event | None = None,
    ) -> Any:
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("文件操作已取消")
        if operation == "restore":
            identifier = str(args["id"])
            if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
                raise ValueError("回收记录无效")
            record = self.trash / f"{identifier}.json"
            metadata = json.loads(record.read_text(encoding="utf-8"))
            destination = self.editable(metadata["path"])
            if destination.exists():
                raise FileExistsError("原路径已存在，未覆盖")
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Never let a changed mount layout trigger an unbounded cross-device copy.
            (self.trash / identifier).rename(destination)
            record.unlink()
            return {"path": str(destination), "restored": True}
        path = self.path(str(args.get("path", "")))
        if operation == "list":
            entries: list[dict[str, Any]] = []
            with os.scandir(path) as iterator:
                for entry in iterator:
                    if len(entries) >= 300:
                        break
                    info = entry.stat(follow_symlinks=False)
                    entries.append(
                        {
                            "name": entry.name,
                            "directory": entry.is_dir(follow_symlinks=False),
                            "symlink": entry.is_symlink(),
                            "size": info.st_size,
                        }
                    )
            return {
                "path": str(path),
                "items": sorted(entries, key=lambda item: (not item["directory"], item["name"])),
                "limit": 300,
            }
        if operation == "read":
            return {"path": str(path), "content": self._read(path)}
        if operation == "info":
            info = path.stat()
            return {
                "path": str(path),
                "size": info.st_size,
                "modified": info.st_mtime,
                "directory": path.is_dir(),
                "mode": oct(info.st_mode & 0o777),
            }
        if operation == "search":
            pattern, query = str(args.get("pattern", "*")), str(args.get("query", ""))
            results: list[dict[str, Any]] = []
            visited, deadline = (
                0,
                time.monotonic() + self.settings.limits.max_search_seconds,
            )
            directories: list[tuple[Path, int]] = [(path, 0)]
            truncated = False
            while directories:
                directory, depth = directories.pop()
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        if cancelled is not None and cancelled.is_set():
                            raise InterruptedError("文件搜索已取消")
                        visited += 1
                        if (
                            visited > self.settings.limits.max_search_entries
                            or len(results) >= self.settings.limits.max_search_results
                            or time.monotonic() >= deadline
                        ):
                            return {"items": results, "truncated": True}
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if not entry.name.startswith(".") and entry.name not in {
                                "node_modules",
                                "__pycache__",
                            }:
                                if depth < self.settings.limits.max_search_depth:
                                    directories.append((Path(entry.path), depth + 1))
                                else:
                                    truncated = True
                            continue
                        if not entry.is_file(follow_symlinks=False) or not fnmatch.fnmatch(
                            entry.name, pattern
                        ):
                            continue
                        file_path = Path(entry.path)
                        if not query:
                            results.append({"path": str(file_path)})
                            continue
                        try:
                            for number, line in enumerate(self._read(file_path).splitlines(), 1):
                                if query.casefold() in line.casefold():
                                    results.append(
                                        {
                                            "path": str(file_path),
                                            "line": number,
                                            "text": line[:300],
                                        }
                                    )
                                    break
                        except (OSError, UnicodeError, ValueError):
                            continue
            return {"items": results, "truncated": truncated}
        path = self.editable(str(path))
        if operation in {"write", "edit"}:
            if operation == "edit":
                original = self._read(path)
                before, after = str(args["old"]), str(args["new"])
                if not before or original.count(before) != 1:
                    raise ValueError("替换文本必须恰好匹配一次")
                content = original.replace(before, after, 1)
            else:
                content = str(args["content"])
                if path.exists() and not args.get("overwrite", False):
                    raise FileExistsError("文件已存在；覆盖需明确 overwrite=true")
            encoded = content.encode()
            if len(encoded) > self.settings.limits.max_file_bytes:
                raise ValueError("写入内容超过上限")
            if not path.parent.is_dir():
                raise ValueError("父目录不存在，请先创建目录")
            temporary = path.parent / f".muli-{new_id()}"
            try:
                with temporary.open("xb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
                if operation == "write" and not args.get("overwrite", False):
                    # Atomic no-clobber publication also handles concurrent writers.
                    os.link(temporary, path)
                else:
                    os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"path": str(path), "bytes": len(encoded)}
        if operation == "mkdir":
            path.mkdir(parents=True, exist_ok=True)
            return {"path": str(path)}
        if operation in {"copy", "move"}:
            destination = self.editable(str(args["destination"]))
            if destination.exists():
                raise FileExistsError("目标已存在，未覆盖")
            if operation == "copy":
                if not path.is_file() or path.stat().st_size > self.settings.limits.max_file_bytes:
                    raise ValueError("复制仅接受上限内普通文件；目录请分批管理")
                temporary = destination.parent / f".muli-{new_id()}"
                try:
                    with path.open("rb") as source, temporary.open("xb") as target:
                        copied_bytes = source.read(self.settings.limits.max_file_bytes + 1)
                        if len(copied_bytes) > self.settings.limits.max_file_bytes:
                            raise ValueError("复制文件超过读取上限")
                        target.write(copied_bytes)
                        target.flush()
                        os.fsync(target.fileno())
                    shutil.copystat(path, temporary)
                    os.link(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                if path.stat().st_dev != destination.parent.stat().st_dev:
                    raise ValueError("跨文件系统移动请使用受限复制后移入回收站")
                path.rename(destination)
            return {"path": str(destination)}
        if operation == "trash":
            self.trash.mkdir(parents=True, exist_ok=True)
            if path.stat().st_dev != self.trash.stat().st_dev:
                raise ValueError("回收站位于不同文件系统，拒绝隐式大规模复制")
            identifier = new_id()
            metadata = {"id": identifier, "path": str(path), "created_at": now()}
            record = self.trash / f"{identifier}.json"
            record.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            try:
                path.rename(self.trash / identifier)
            except OSError:
                record.unlink(missing_ok=True)
                raise
            return metadata
        raise ValueError("未知文件操作")

    def tools(self) -> list[Tool]:
        definitions: list[tuple[str, str, dict[str, Any], tuple[str, ...], str]] = [
            ("list", "列出目录（最多300项）", {}, ("path",), "read"),
            ("read", "读取有限大小的 UTF-8 文件", {}, ("path",), "read"),
            ("info", "查看文件元数据", {}, ("path",), "read"),
            (
                "search",
                "按文件名或文本搜索；最多检查2000个文件",
                {"pattern": string(), "query": string()},
                ("path",),
                "read",
            ),
            (
                "write",
                "写入文件；覆盖需显式声明",
                {"content": string(), "overwrite": {"type": "boolean"}},
                ("path", "content"),
                "write",
            ),
            (
                "edit",
                "精确替换唯一匹配文本",
                {"old": string(), "new": string()},
                ("path", "old", "new"),
                "write",
            ),
            ("mkdir", "创建目录", {}, ("path",), "write"),
            (
                "copy",
                "复制一个有限大小的文件，不覆盖目标",
                {"destination": string()},
                ("path", "destination"),
                "write",
            ),
            (
                "move",
                "移动或重命名文件/目录，不覆盖目标",
                {"destination": string()},
                ("path", "destination"),
                "destructive",
            ),
            ("trash", "将文件/目录移入可恢复回收站", {}, ("path",), "destructive"),
            (
                "restore",
                "按回收记录恢复文件，不覆盖目标",
                {"id": string()},
                ("id",),
                "write",
            ),
        ]
        result = []
        for operation, description, properties, required, risk in definitions:

            async def handle(arguments: dict[str, Any], op: str = operation) -> Any:
                async with self._lock:
                    return await execute(arguments, op)

            async def execute(arguments: dict[str, Any], op: str) -> Any:
                cancelled = threading.Event()
                task = asyncio.create_task(
                    asyncio.to_thread(self._execute, op, arguments, cancelled)
                )
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled.set()
                    # Settle the bounded filesystem operation before reporting cancellation.
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                        except Exception:  # noqa: BLE001 - collect worker outcome before propagating cancellation
                            break
                    if task.done() and not task.cancelled():
                        task.exception()
                    raise

            result.append(
                Tool(
                    f"fs_{operation}",
                    description,
                    parameters({"path": string("绝对路径"), **properties}, required),
                    handle,
                    risk,
                )
            )
        return result
