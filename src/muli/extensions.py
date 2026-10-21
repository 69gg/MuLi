"""Bounded extension discovery, idle MCP and request-boundary Skills publication."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from muli.attachments import file_io
from muli.config import Settings, default_home
from muli.tools import Tool
from muli.tools.mcp import MCPCandidate, MCPLimits

if TYPE_CHECKING:
    from muli.runtime import Runtime
    from muli.skills import SkillSnapshot


class ExtensionError(ValueError):
    def __init__(self, code: str, source: str = "") -> None:
        self.code, self.source = code, source
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MCPSourceSnapshot:
    config: dict[str, Any]
    fingerprint: str
    sources: tuple[str, ...]


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (*_file_identity(value), value.st_ctime_ns)


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ExtensionError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ExtensionError("invalid_json")


class MCPDirectory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.limits = settings.extensions
        self.root = Path(self.limits.mcp_directory or default_home() / "mcp.d").expanduser()

    def _entries(self) -> tuple[Path, tuple[int, ...] | None, list[tuple[Path, os.stat_result]]]:
        try:
            root = self.root.resolve()
            root_stat = root.stat()
        except FileNotFoundError:
            return self.root.absolute(), None, []
        except (OSError, ValueError, RuntimeError) as exc:
            raise ExtensionError("source_unavailable", "mcp.d") from exc
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ExtensionError("source_not_directory", "mcp.d")
        files: list[tuple[Path, os.stat_result]] = []
        try:
            with os.scandir(root) as entries:
                for count, entry in enumerate(entries, 1):
                    if count > self.limits.max_entries:
                        raise ExtensionError("directory_budget_exceeded", "mcp.d")
                    if not entry.name.endswith(".json"):
                        continue
                    path = root / entry.name
                    # Windows DirEntry.stat zeroes device/inode; use uncached path identity.
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        raise ExtensionError("source_not_regular", entry.name)
                    files.append((path, info))
                    if len(files) > self.limits.max_mcp_fragments:
                        raise ExtensionError("fragment_budget_exceeded", "mcp.d")
        except OSError as exc:
            raise ExtensionError("source_changed", "mcp.d") from exc
        return root, _identity(root_stat), sorted(files, key=lambda item: item[0].name)

    def fingerprint(self) -> str:
        root, root_id, files = self._entries()
        return _digest([str(root), root_id, [(path.name, _identity(info)) for path, info in files]])

    def prepare(self) -> MCPSourceSnapshot:
        root, root_id, files = self._entries()
        before = _digest(
            [str(root), root_id, [(path.name, _identity(info)) for path, info in files]]
        )
        servers = dict(self.settings.mcp.get("mcpServers", {}))
        origins = {name: "config.mcp" for name in servers}
        total = 0
        sources: list[str] = []
        for path, expected in files:
            source = path.name
            try:
                if expected.st_size > self.limits.max_mcp_fragment_bytes:
                    raise ExtensionError("fragment_budget_exceeded", source)
                descriptor = os.open(
                    path,
                    os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
                with os.fdopen(descriptor, "rb") as handle:
                    opened = os.fstat(handle.fileno())
                    # Windows path ctime can mean creation time while fstat uses change time.
                    if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(
                        expected
                    ):
                        raise ExtensionError("source_changed", source)
                    payload = handle.read(self.limits.max_mcp_fragment_bytes + 1)
                    if _identity(os.fstat(handle.fileno())) != _identity(opened):
                        raise ExtensionError("source_changed", source)
                if _identity(path.lstat()) != _identity(expected):
                    raise ExtensionError("source_changed", source)
                total += len(payload)
                if (
                    len(payload) > self.limits.max_mcp_fragment_bytes
                    or total > self.limits.max_mcp_source_bytes
                ):
                    raise ExtensionError("source_budget_exceeded", source)
                parsed = json.loads(
                    payload,
                    object_pairs_hook=_unique_object,
                    parse_constant=_invalid_constant,
                )
                validated = Settings.model_validate({"mcp": parsed}).mcp
                for name, raw in validated.get("mcpServers", {}).items():
                    if name in servers:
                        raise ExtensionError("duplicate_server", f"{origins[name]} / {source}")
                    servers[name] = {"enabled": True, **raw}
                    origins[name] = source
                sources.append(source)
            except ExtensionError as exc:
                if not exc.source:
                    raise ExtensionError(exc.code, source) from exc
                raise
            except (OSError, UnicodeError) as exc:
                raise ExtensionError("source_changed", source) from exc
            except Exception as exc:
                raise ExtensionError("invalid_fragment", source) from exc
        if self.fingerprint() != before:
            raise ExtensionError("source_changed", "mcp.d")
        try:
            config = Settings.model_validate({"mcp": {"mcpServers": servers}}).mcp
        except ValueError as exc:
            raise ExtensionError("invalid_merged_config", "mcp.d") from exc
        return MCPSourceSnapshot(config, before, tuple(sources))


class Extensions:
    def __init__(self, runtime: Runtime) -> None:
        from muli.skills import Skills

        self.runtime = runtime
        self.skills = Skills(runtime)
        self.generation = 0
        self.phase = "discovered"
        self._error: dict[str, str] | None = None
        self._fingerprint = ""
        self._observed = ""
        self._sources: tuple[str, ...] = ()
        self._skills_base: tuple[str, str] | None = None
        self._dirty = False
        self._full_reload = False
        self._requested = 0
        self._directory_request: int | None = None
        self._watch_failure: tuple[int, str, str, str] | None = None
        self._refresh: set[str] = set()
        self._ready_at = 0.0
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self._last_apply: dict[str, Any] = {
            "configuration": "unchanged",
            "published": False,
            "retirement": "unneeded",
        }

    @staticmethod
    def configuration_only(previous: Settings, target: Settings) -> bool:
        before, after = previous.model_dump(), target.model_dump()
        return all(
            before[key] == after[key]
            for key in before
            if key not in {"mcp", "skills", "extensions"}
        )

    def status(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "phase": self.phase,
            "running": not self._closed and self._worker is not None and not self._worker.done(),
            "fingerprint": self._fingerprint,
            "sources": list(self._sources),
            "queued_reason": "runtime_busy" if self._dirty and self.runtime.busy else None,
            "error": self._error,
            "retirement_errors": list(self.runtime.mcp.retirement_errors),
            "cleanup_pending": self.runtime.mcp.pending_cleanup,
            "skills": self.skills.status(),
            "last_apply": dict(self._last_apply),
        }

    def _failure(self, exc: Exception) -> ExtensionError:
        code = getattr(exc, "code", None)
        source = getattr(exc, "source", "")
        error = ExtensionError(
            code
            if isinstance(code, str)
            else "candidate_timeout"
            if isinstance(exc, TimeoutError)
            else "candidate_failed",
            source if isinstance(source, str) else "",
        )
        self.phase = "restart_required" if error.code == "restart_required" else "failed"
        self._error = {"code": error.code, "source": error.source}
        return error

    def _limits(self, settings: Settings) -> MCPLimits:
        limits = settings.extensions
        return MCPLimits(
            limits.max_mcp_tools,
            limits.max_mcp_pages,
            limits.max_mcp_catalog_bytes,
            settings.limits.max_output_chars,
        )

    async def fingerprint(self, settings: Settings) -> str:
        return self._source_fingerprint(
            settings,
            await file_io(MCPDirectory(settings).fingerprint),
            await self.skills.fingerprint(settings.skills),
        )

    @staticmethod
    def _source_fingerprint(settings: Settings, mcp: str, skills: str) -> str:
        return _digest(
            [
                settings.mcp,
                settings.extensions.model_dump(),
                settings.skills.model_dump(),
                mcp,
                skills,
            ]
        )

    @staticmethod
    def _skills_configuration(settings: Settings) -> str:
        return _digest(
            [
                settings.mcp,
                settings.extensions.model_dump(),
                settings.skills.model_dump(),
                settings.limits.max_output_chars,
            ]
        )

    def _skills_request(self, settings: Settings, requested: int) -> bool:
        return (
            self._skills_base is not None
            and self._skills_base[0] == self._skills_configuration(settings)
            and self._directory_request == self._requested == requested
            and not self._full_reload
            and not self._refresh
            and not self.runtime.mcp.pending_cleanup
            and not self._closed
            and not self.runtime._closing
        )

    def _registry(self, candidate: MCPCandidate) -> dict[str, Tool]:
        old = {tool.name: tool for tool in self.runtime.mcp.tools}
        registry = {
            name: tool for name, tool in self.runtime.tools.items() if old.get(name) is not tool
        }
        for tool in candidate.tools:
            if tool.name in registry:
                raise ExtensionError("tool_name_conflict")
            registry[tool.name] = tool
        return registry

    async def load_initial(self) -> None:
        """Keep core startup available; report failed sources instead of hiding them."""
        if self.runtime._closing:
            raise ExtensionError("runtime_closed")
        settings = self.runtime.config.settings
        self._closed = False
        self._observed = ""
        self._skills_base = None
        configuration = self._skills_configuration(settings)
        self.phase, self._error = "preparing", None
        requested = self._requested
        error: ExtensionError | None = None
        fingerprint: str | None = None
        sources: tuple[str, ...] = ()
        source: MCPSourceSnapshot | None = None
        try:
            fingerprint = await self.fingerprint(settings)
        except Exception as exc:
            error = self._failure(exc)
        try:
            source = await file_io(MCPDirectory(settings).prepare)
            config, sources = source.config, source.sources
        except ExtensionError as exc:
            error, config = exc, settings.mcp
        snapshot: SkillSnapshot | None = None
        candidate: MCPCandidate | None = None
        published = False
        self.runtime.mcp.set_changed_callback(self._catalog_changed)
        try:
            async with asyncio.timeout(settings.extensions.candidate_timeout_seconds):
                candidate = await self.runtime.mcp.prepare(
                    config, strict=False, limits=self._limits(settings)
                )
                registry = self._registry(candidate)
                try:
                    snapshot = await self.skills.prepare(settings.skills)
                except Exception as exc:
                    error = self._failure(exc)
                if fingerprint is not None and await self.fingerprint(settings) != fingerprint:
                    # Do not acknowledge a source that this candidate did not read.
                    raise ExtensionError("source_changed")
                if self._closed or self.runtime._closing:
                    raise ExtensionError("runtime_closed")
                self.runtime.mcp.publish(candidate)
                self.runtime.tools = registry
                if snapshot is not None:
                    self.skills.publish(snapshot)
                published = True
                self._last_apply = {
                    "configuration": "unchanged",
                    "published": True,
                    "retirement": "unneeded",
                }
                self.generation += 1
                self._sources = sources
                # An unavailable scan can still serve inline/core tools, but has
                # no accepted directory identity; recovery must trigger a reload.
                self._observed = self._fingerprint = fingerprint or ""
                if self._requested == requested:
                    self._dirty = False
                    self._full_reload = False
                    self._refresh.clear()
                failed = next(
                    (item for item in candidate.statuses if item["status"] == "error"), None
                )
                if failed is not None and error is None:
                    error = ExtensionError(failed["error_code"], failed["name"])
                if error is not None:
                    self._failure(error)
                else:
                    self.phase, self._error = "loaded", None
                    if source is not None:
                        self._skills_base = configuration, source.fingerprint
        except asyncio.CancelledError:
            if not published:
                self._observed = ""
                if candidate is not None:
                    await self.runtime.mcp.discard(candidate)
            raise
        except Exception as exc:
            if not published:
                # Re-observe even unchanged sources after a failed full rebuild.
                self._observed = ""
                if candidate is not None:
                    await self.runtime.mcp.discard(candidate)
            self._failure(exc)

    def start(self) -> None:
        if self._closed or self.runtime._closing:
            return
        if self._worker is not None:
            if not self._worker.done():
                return
            if not self._worker.cancelled():
                self._worker.exception()
        self._worker = asyncio.create_task(self._watch(), name="muli-extensions")

    def _catalog_changed(self, name: str) -> None:
        if self._closed:
            return
        self._refresh.add(name)
        self.request_reload(refresh_catalogs=False)

    def request_reload(self, *, refresh_catalogs: bool = True) -> dict[str, Any]:
        if not self._closed:
            self._full_reload = True
            if refresh_catalogs:
                self._refresh.update(
                    item["name"]
                    for item in self.runtime.mcp.status()
                    if item["status"] == "connected"
                )
        return self._queue_reload()

    def _queue_reload(self, *, directory: bool = False) -> dict[str, Any]:
        if not self._closed:
            self._dirty = True
            self._requested += 1
            if directory:
                self._directory_request = self._requested
            self.phase = "queued"
            self._ready_at = asyncio.get_running_loop().time() + (
                self.runtime.config.settings.extensions.debounce_seconds
            )
            self._wake.set()
        return self.status()

    async def apply_locked(self, target: Settings, *, persist: bool) -> None:
        """The owner holds configuration_lock; recheck internal wakeups before publishing."""
        self.runtime.maintenance.require_configuration()
        if self._closed or self.runtime._closing:
            raise ExtensionError("runtime_closed")
        if self.runtime.busy:
            raise ExtensionError("runtime_busy")
        candidate: MCPCandidate | None = None
        published = False
        self.phase, self._error = "preparing", None
        self._skills_base = None
        configuration = self._skills_configuration(target)
        self._last_apply = {
            "configuration": "unchanged",
            "published": False,
            "retirement": "unneeded",
        }
        refresh, self._refresh = self._refresh, set()
        requested = self._requested
        try:
            async with asyncio.timeout(target.extensions.candidate_timeout_seconds):
                before = await self.fingerprint(target)
                sources = await file_io(MCPDirectory(target).prepare)
                snapshot = await self.skills.prepare(target.skills)
                candidate = await self.runtime.mcp.prepare(
                    sources.config, refresh=refresh, limits=self._limits(target)
                )
                registry = self._registry(candidate)
                if await self.fingerprint(target) != before:
                    raise ExtensionError("source_changed")
                if self._closed or self.runtime._closing:
                    raise ExtensionError("runtime_closed")
                if self.runtime.busy:
                    raise ExtensionError("runtime_busy")
                self.runtime.maintenance.require_configuration()
                self.runtime.store.revalidate_format()
                # No await between persistence and publication. Everything below is prevalidated.
                if persist:
                    try:
                        self.runtime.config.replace(target)
                    except Exception as exc:
                        self._last_apply["configuration"] = "save_failed"
                        raise ExtensionError("config_save_failed") from exc
                    self._last_apply["configuration"] = "saved"
                retired = self.runtime.mcp.publish(candidate)
                self.runtime.tools = registry
                self.skills.publish(snapshot)
                self.generation += 1
                self._sources = sources.sources
                self._observed = self._fingerprint = before
                self.phase, self._error = "loaded", None
                self._skills_base = configuration, sources.fingerprint
                if self._requested == requested:
                    self._dirty = False
                    self._full_reload = False
                published = True
                self._last_apply.update(
                    published=True, retirement="pending" if retired else "unneeded"
                )
            retirement_complete = await self.runtime.mcp.retire(retired)
            if retired:
                self._last_apply["retirement"] = "complete" if retirement_complete else "failed"
        except asyncio.CancelledError:
            if candidate is not None and not published:
                await self.runtime.mcp.discard(candidate)
            raise
        except Exception as exc:
            if candidate is not None and not published:
                await self.runtime.mcp.discard(candidate)
            if getattr(exc, "code", None) in {"runtime_busy", "transaction_in_progress"}:
                self._refresh.update(refresh)
            raise self._failure(exc) from exc

    async def _apply_skills_locked(self, settings: Settings) -> bool:
        """Publish only an autonomous Skills change; the owner holds configuration_lock."""
        requested, generation, baseline = self._requested, self.generation, self._skills_base
        if not self._skills_request(settings, requested):
            return False
        target = settings.model_copy(deep=True)
        try:
            self.runtime.maintenance.require_configuration()
            self.runtime.store.revalidate_format()
            async with asyncio.timeout(target.extensions.candidate_timeout_seconds):
                mcp = await file_io(MCPDirectory(target).fingerprint)
                if baseline is None or mcp != baseline[1]:
                    return False
                self.phase, self._error = "preparing", None
                self._last_apply = {
                    "scope": "skills",
                    "mcp": "unchanged",
                    "configuration": "unchanged",
                    "published": False,
                    "retirement": "unneeded",
                }
                snapshot = await self.skills.prepare(target.skills)
                final_mcp = await file_io(MCPDirectory(target).fingerprint)
                final_skills = await self.skills.fingerprint(target.skills)
                if (
                    not self._skills_request(self.runtime.config.settings, requested)
                    or self._skills_base != baseline
                    or self.generation != generation
                    or final_mcp != mcp
                ):
                    return False
                if snapshot.source_fingerprint != final_skills:
                    raise ExtensionError("source_changed", "skills")
                self.runtime.maintenance.require_configuration()
                self.runtime.store.revalidate_format()
                # Only an immutable Skills pointer changes; existing requests and MCP stay owned.
                self.skills.publish(snapshot)
                self.generation += 1
                self._observed = self._fingerprint = self._source_fingerprint(
                    target, final_mcp, final_skills
                )
                self._dirty = False
                self.phase, self._error = "loaded", None
                self._last_apply["published"] = True
                return True
        except Exception as exc:
            if self._requested != requested:
                return False
            if getattr(exc, "code", None) not in {"runtime_busy", "transaction_in_progress"}:
                self._dirty = False
            raise self._failure(exc) from exc

    async def _watch(self) -> None:
        def notify_failure(previous: tuple[str, dict[str, str] | None]) -> None:
            if self.phase not in {"failed", "restart_required"} or self._error is None:
                return
            signature = self.generation, self.phase, self._error["code"], self._error["source"]
            if signature == self._watch_failure:
                return
            self._watch_failure = signature
            if previous != (self.phase, self._error):
                try:
                    self.runtime.system_events.extension_watch_failed()
                except Exception:
                    # The sink may have accepted the notification before failing.
                    # Keep its signature; do not recurse or retry unknown delivery.
                    self._failure(ExtensionError("watcher_failed", "extensions"))

        while not self._closed:
            settings = self.runtime.config.settings
            interval = settings.extensions.scan_interval_seconds
            previous = self.phase, self._error
            try:
                if self.phase == "loaded":
                    self._watch_failure = None
                if settings.extensions.watch_enabled:
                    previous = self.phase, self._error
                    try:
                        observed = await self.fingerprint(settings)
                        if observed != self._observed:
                            self._observed = observed
                            self._queue_reload(directory=True)
                    except Exception as exc:
                        self._failure(exc)
                        notify_failure(previous)
                if self._dirty and asyncio.get_running_loop().time() >= self._ready_at:
                    if (
                        self.runtime.busy
                        and not self._skills_request(self.runtime.config.settings, self._requested)
                    ) or self.runtime.maintenance.configuration_blocked:
                        self.phase = "queued"
                    else:
                        async with self.runtime.configuration_lock:
                            if (
                                not self._closed
                                and not self.runtime.maintenance.configuration_blocked
                            ):
                                previous = self.phase, self._error
                                autonomous = self._directory_request == self._requested
                                try:
                                    if self.runtime.busy:
                                        if not await self._apply_skills_locked(
                                            self.runtime.config.settings
                                        ):
                                            self.phase = "queued"
                                    else:
                                        self._dirty = False
                                        await self.apply_locked(
                                            self.runtime.config.settings, persist=False
                                        )
                                except ExtensionError as exc:
                                    if exc.code in {"runtime_busy", "transaction_in_progress"}:
                                        self._dirty = True
                                        self.phase, self._error = "queued", None
                                    elif autonomous:
                                        notify_failure(previous)
                if (
                    self._dirty
                    and not self.runtime.busy
                    and not self.runtime.maintenance.configuration_blocked
                ):
                    interval = min(
                        interval,
                        max(0.01, self._ready_at - asyncio.get_running_loop().time()),
                    )
            except Exception:  # noqa: BLE001 - observation failure must not lose the watcher owner
                if self._closed or self.runtime._closing:
                    return
                self._failure(ExtensionError("watcher_failed", "extensions"))
                notify_failure(previous)
                # Keep the existing dirty state. A consumed candidate request is
                # not an invitation to retry unknown effects; failed observation
                # waits the normal interval without reading its failing gate again.
            if self._closed or self.runtime._closing:
                return
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except TimeoutError:
                pass

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self._closed = True
        self.runtime.mcp.set_changed_callback(None)
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
