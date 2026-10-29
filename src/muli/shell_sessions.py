"""Lazy, session-owned native shells with a separate completion channel.

Pipes are intentionally not a PTY. Commands have no input stream and cannot
consume the host's next dispatch. This is ownership and framing, not a sandbox
against malicious programs running as the same operating-system user.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import hmac
import json
import math
import os
import secrets
import subprocess
import tempfile
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict

from .evidence import json_size
from .platforms.windows_shell import WindowsShellJob, cmd_console_options
from .process import _settle_process, _wait_cleanup
from .shell_protocol import (
    CONTROL_BYTES,
    CONTROL_TIMEOUT,
    ShellSpec,
    cmd_bootstrap,
    cmd_environment,
    command_file,
    dispatch,
    marker,
    powershell_bootstrap,
    select_shell,
    write_command,
)

StreamName = Literal["output", "stderr"]
Guard = Callable[[], None]
_STREAMS: tuple[StreamName, StreamName] = ("output", "stderr")


@dataclass(frozen=True, slots=True)
class ShellFinished:
    session_id: str
    command_id: str
    os_epoch: str
    state: str
    exit_code: int | None


FinishCallback = Callable[[ShellFinished], Awaitable[None]]


class OutputCursor(TypedDict):
    output: int
    stderr: int


def _empty_cursor() -> OutputCursor:
    return {"output": 0, "stderr": 0}


class ShellSessionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class _Output:
    chunks: deque[tuple[int, StreamName, bytes]] = field(default_factory=deque)
    observed: int = 0
    retained: int = 0
    discarded: int = 0
    observed_by_stream: OutputCursor = field(default_factory=_empty_cursor)
    discarded_by_stream: OutputCursor = field(default_factory=_empty_cursor)

    def append(self, stream: StreamName, data: bytes, limit: int) -> None:
        if not data:
            return
        self.chunks.append((self.observed_by_stream[stream], stream, data))
        self.observed_by_stream[stream] += len(data)
        self.observed += len(data)
        self.retained += len(data)
        self.trim(limit)

    def trim(self, limit: int) -> None:
        while self.retained > limit and self.chunks:
            offset, name, first = self.chunks.popleft()
            count = min(len(first), self.retained - limit)
            self.retained -= count
            self.discarded += count
            self.discarded_by_stream[name] += count
            if count < len(first):
                self.chunks.appendleft((offset + count, name, first[count:]))

    def read_stream(
        self, stream: StreamName, cursor: int, *, final: bool = False, max_bytes: int | None = None
    ) -> tuple[str, int]:
        start = max(cursor, self.discarded_by_stream[stream])
        boundary = self.observed_by_stream[stream]
        if max_bytes is not None:
            boundary = min(boundary, start + max_bytes)
        raw = bytearray()
        for offset, name, data in self.chunks:
            if name != stream:
                continue
            if offset >= boundary:
                break
            if offset + len(data) > start:
                raw.extend(data[max(0, start - offset) : boundary - offset])
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(
            bytes(raw), final=final and boundary == self.observed_by_stream[stream]
        )
        return text, start + len(raw) - len(decoder.getstate()[0])

    def read(self, cursor: OutputCursor, *, final: bool = False) -> tuple[str, str, OutputCursor]:
        output, next_output = self.read_stream("output", cursor["output"], final=final)
        error, next_error = self.read_stream("stderr", cursor["stderr"], final=final)
        return output, error, {"output": next_output, "stderr": next_error}


def _read_cursor(cursor: OutputCursor | None, output: _Output) -> OutputCursor:
    if cursor is None:
        return _empty_cursor()
    if (
        not isinstance(cursor, dict)
        or set(cursor) != set(_STREAMS)
        or any(
            type(cursor[stream]) is not int
            or cursor[stream] < 0
            or cursor[stream] > output.observed_by_stream[stream]
            for stream in _STREAMS
        )
    ):
        raise ShellSessionError("invalid_cursor", "shell 输出游标无效")
    return cursor.copy()


def _receipt_budget(
    receipt: dict[str, Any], maximum: int | None, *, before_dispatch: bool = False
) -> int | None:
    if maximum is None:
        return None
    if type(maximum) is not int or maximum < 1:
        raise ShellSessionError("invalid_argument", "shell 回执预算必须是正整数")
    if json_size(receipt) > maximum:
        raise ShellSessionError(
            "output_budget",
            "shell 完整回执元数据超过预算，本次命令尚未派发"
            if before_dispatch
            else "shell 完整回执元数据超过预算；请核查原命令，不要重新执行",
        )
    return maximum


def _receipt_page(
    output: _Output,
    result: dict[str, Any],
    cursor: OutputCursor,
    *,
    maximum: int | None,
    final: bool,
) -> dict[str, Any]:
    """One byte-cursor pager for live pipes and validated, process-free evidence."""
    start: OutputCursor = {
        "output": max(cursor["output"], output.discarded_by_stream["output"]),
        "stderr": max(cursor["stderr"], output.discarded_by_stream["stderr"]),
    }
    maximum = _receipt_budget(result, maximum)

    def select(stream: StreamName, count: int) -> int:
        text, next_cursor = output.read_stream(stream, cursor[stream], final=final, max_bytes=count)
        result[stream] = text
        result["next_cursor"][stream] = next_cursor
        return next_cursor

    remaining = {stream: output.observed_by_stream[stream] - start[stream] for stream in _STREAMS}
    full_cursor = {stream: select(stream, remaining[stream]) for stream in _STREAMS}
    if maximum is None or json_size(result) <= maximum:
        return result
    for stream in _STREAMS:
        select(stream, 0)
    base_size = json_size(result)
    available = maximum - base_size
    minimum: dict[StreamName, int] = {}
    for stream in _STREAMS:
        if full_cursor[stream] == start[stream]:
            continue
        for count in range(1, min(4, remaining[stream]) + 1):
            if select(stream, count) > start[stream]:
                minimum[stream] = json_size(result) - base_size
                break
        select(stream, 0)
    order: list[StreamName] = []
    for offset, stream, data in output.chunks:
        if stream in minimum and stream not in order and offset + len(data) > start[stream]:
            order.append(stream)
    shares: dict[StreamName, int] = {"output": 0, "stderr": 0}
    if len(minimum) == 2 and sum(minimum.values()) <= available:
        shares["output"] = min(
            max(available // 2, minimum["output"]), available - minimum["stderr"]
        )
        shares["stderr"] = available - shares["output"]
    else:
        for stream in order:
            if minimum[stream] <= available:
                shares[stream] = available
                break

    def fit(stream: StreamName, limit: int) -> None:
        low, high = 0, remaining[stream]
        while low < high:
            middle = (low + high + 1) // 2
            select(stream, middle)
            if json_size(result) <= limit:
                low = middle
            else:
                high = middle - 1
        select(stream, low)

    for stream in _STREAMS:
        fit(stream, json_size(result) + shares[stream])
    for stream in order:
        fit(stream, maximum)
    if all(result["next_cursor"][stream] == start[stream] for stream in _STREAMS):
        raise ShellSessionError(
            "output_budget", "shell 预算不足以返回完整字符及续读元数据，请勿重新执行命令"
        )
    return result


@dataclass(slots=True)
class _Command:
    id: str
    shell: _Shell
    deadline: float
    state: str = "running"
    exit_code: int | None = None
    cwd: str = ""
    output: _Output = field(default_factory=_Output)
    proof: asyncio.Event = field(default_factory=asyncio.Event)
    stdout_fence: asyncio.Event = field(default_factory=asyncio.Event)
    stderr_fence: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    monitor: asyncio.Task[None] | None = None
    notify_on_finish: bool = False
    dispatched: bool = False
    finish_attempted: bool = False


@dataclass(slots=True)
class _Shell:
    session_id: str
    spec: ShellSpec
    directory: tempfile.TemporaryDirectory[str]
    cwd: str
    epoch: str = field(default_factory=lambda: secrets.token_hex(16))
    secret: str = field(default_factory=lambda: secrets.token_hex(32))
    state: str = "starting"
    process: asyncio.subprocess.Process | None = None
    spawn: asyncio.Task[asyncio.subprocess.Process] | None = None
    boot: asyncio.Task[None] | None = None
    readers: list[asyncio.Task[None]] = field(default_factory=list)
    watcher: asyncio.Task[None] | None = None
    cleanup: asyncio.Task[None] | None = None
    server: asyncio.Server | None = None
    controls: set[asyncio.Task[None]] = field(default_factory=set)
    writers: set[asyncio.StreamWriter] = field(default_factory=set)
    control_writer: asyncio.StreamWriter | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    job: WindowsShellJob | None = None
    current: _Command | None = None
    cleanup_failed: bool = False

    @property
    def root(self) -> Path:
        return Path(self.directory.name)


class ShellSessions:
    def __init__(
        self,
        *,
        default_cwd: Path,
        executable: str = "",
        max_sessions: Callable[[], int],
        max_output_bytes: Callable[[], int],
        max_command_bytes: Callable[[], int],
        max_run_seconds: Callable[[], float],
        max_receipt_chars: Callable[[], int] | None = None,
        on_finish: FinishCallback | None = None,
    ) -> None:
        self.default_cwd = default_cwd
        self.executable = executable
        self._max_sessions = max_sessions
        self._max_output_bytes = max_output_bytes
        self._max_command_bytes = max_command_bytes
        self._max_run_seconds = max_run_seconds
        self._max_receipt_chars = max_receipt_chars
        self._on_finish = on_finish
        self._sessions: dict[str, _Shell] = {}
        self._commands: OrderedDict[str, _Command] = OrderedDict()
        self._closed = False

    def live_sessions(self) -> tuple[str, ...]:
        return tuple(self._sessions)

    def is_running(self, session_id: str) -> bool:
        shell = self._sessions.get(session_id)
        return shell is not None and shell.current is not None and shell.current.state == "running"

    def current_cwd(self, session_id: str) -> str | None:
        shell = self._sessions.get(session_id)
        return shell.cwd if shell is not None else None

    @staticmethod
    def _number(value: float, *, maximum: float, zero: bool) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            or (not zero and value == 0)
            or value > maximum
        ):
            raise ShellSessionError("invalid_argument", "shell 等待或执行期限无效")
        return float(value)

    def _output_limit(self) -> int:
        return max(1, self._max_output_bytes())

    def _retain_command(self, command: _Command) -> None:
        limit = max(1, self._max_sessions())
        while len(self._commands) >= limit:
            removable = next(
                (
                    key
                    for key, item in self._commands.items()
                    if item.state != "running"
                    and (not item.notify_on_finish or item.monitor is None or item.monitor.done())
                ),
                None,
            )
            if removable is None:
                raise ShellSessionError("capacity", "shell 命令记录容量已满")
            del self._commands[removable]
        self._commands[command.id] = command

    async def execute(
        self,
        session_id: str,
        command: str,
        *,
        cwd: Path | None = None,
        wait_seconds: float = 1,
        timeout: float | None = None,
        notify_on_finish: bool = False,
        admission_guard: Guard,
    ) -> dict[str, Any]:
        if type(notify_on_finish) is not bool:
            raise ShellSessionError("invalid_argument", "shell 完成通知选择必须是布尔值")
        wait = self._number(wait_seconds, maximum=10, zero=True)
        maximum = self._max_run_seconds()
        duration = self._number(
            maximum if timeout is None else timeout, maximum=maximum, zero=False
        )
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(command, str)
            or not command.strip()
            or "\x00" in command
            or len(command.encode("utf-8")) > self._max_command_bytes()
        ):
            raise ShellSessionError("invalid_argument", "shell 命令或会话无效")
        if self._closed:
            raise ShellSessionError("closed", "shell 管理器已关闭")
        admission_guard()
        shell = self._sessions.get(session_id)
        fresh = shell is None
        if shell is not None and shell.state != "idle":
            code = "cleanup_unknown" if shell.state == "blocked" else "busy"
            raise ShellSessionError(code, "该会话的 shell 正忙或尚未完成收口")
        if shell is None:
            if len(self._sessions) >= self._max_sessions():
                raise ShellSessionError("capacity", "活动 shell 数量已达上限")
            spec = select_shell(self.executable)
            initial = cwd if cwd is not None else self.default_cwd
            directory = tempfile.TemporaryDirectory(prefix="muli-shell-")
            shell = _Shell(session_id, spec, directory, str(initial))
            self._sessions[session_id] = shell
        item = _Command(
            secrets.token_hex(16),
            shell,
            asyncio.get_running_loop().time() + duration,
            notify_on_finish=notify_on_finish,
        )
        try:
            metadata = self._receipt_metadata(item, _empty_cursor())
            # Immutable identity is a conservative lower bound; keep the full
            # receipt check after startup and adjacent to command dispatch.
            self._receipt_budget(
                {
                    key: metadata[key]
                    for key in (
                        "session_id",
                        "command_id",
                        "os_epoch",
                        "notify_on_finish",
                        "notification_destination",
                    )
                    if key in metadata
                },
                before_dispatch=True,
            )
            self._retain_command(item)
        except Exception:
            if fresh:
                del self._sessions[session_id]
                shell.directory.cleanup()
            raise
        shell.current = item
        shell.state = "starting" if fresh else "busy"
        try:
            if fresh:
                shell.boot = asyncio.create_task(self._start(shell))
                await asyncio.wait_for(asyncio.shield(shell.boot), min(CONTROL_TIMEOUT, duration))
            if (
                self._closed
                or self._sessions.get(session_id) is not shell
                or shell.state not in {"starting", "busy"}
                or shell.lost.is_set()
            ):
                raise ShellSessionError("invalidated", "shell 执行资格已撤销")
            admission_guard()
            metadata = self._receipt_metadata(item, _empty_cursor())
            if cwd is not None and not fresh:
                metadata["cwd"] = str(Path(shell.cwd) / cwd)
            self._receipt_budget(metadata, before_dispatch=True)
            path = command_file(shell.root, item.id, shell.spec.flavor)
            write_command(path, command, shell.spec.flavor, None if fresh else cwd)
            # No suspension between the final host guard and dispatch. Startup
            # and the optional command file are owned by this exact OS epoch.
            shell.state = "busy"
            item.monitor = asyncio.create_task(self._monitor(item))
            if shell.spec.flavor == "pwsh":
                writer = shell.control_writer
                if writer is None:
                    raise ShellSessionError("protocol", "shell 控制连接不可用")
                writer.write((item.id + "\n").encode("ascii"))
            else:
                process = shell.process
                if process is None or process.stdin is None:
                    raise ShellSessionError("protocol", "shell 输入连接不可用")
                writer = process.stdin
                writer.write(dispatch(shell.root, item.id, shell.spec.flavor))
            item.dispatched = True
            await asyncio.wait_for(writer.drain(), CONTROL_TIMEOUT)
            if wait:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(item.done.wait(), wait)
        except asyncio.CancelledError:
            await _wait_cleanup(self._retire(shell, "unknown"))
            raise
        except Exception:
            if await _wait_cleanup(self._retire(shell, "unknown")):
                raise asyncio.CancelledError from None
            raise
        # Publication budgets cannot change the actual command outcome or
        # destroy the OS session. Its retained output remains readable.
        return self._receipt(item, _empty_cursor())

    async def _start(self, shell: _Shell) -> None:
        def accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if shell.state == "stopping" or len(shell.controls) >= 2:
                writer.close()
                return
            task = asyncio.create_task(self._control(shell, reader, writer))
            shell.controls.add(task)
            task.add_done_callback(shell.controls.discard)

        shell.server = await asyncio.start_server(accepted, "127.0.0.1", 0, limit=CONTROL_BYTES + 1)
        sockets = shell.server.sockets
        if not sockets:
            raise ShellSessionError("protocol", "shell 控制监听未建立")
        config = shell.root / "control.json"
        with config.open("x", encoding="utf-8") as stream:
            json.dump(
                {"port": sockets[0].getsockname()[1], "secret": shell.secret, "epoch": shell.epoch},
                stream,
            )
        config.chmod(0o600)
        bootstrap = shell.root / "bootstrap.ps1"
        if shell.spec.flavor == "pwsh":
            bootstrap.write_text(powershell_bootstrap(shell.root), encoding="utf-8")
            bootstrap.chmod(0o600)
        if shell.state != "starting" or self._closed:
            raise ShellSessionError("invalidated", "shell 启动已撤销")
        environment = cmd_environment(shell.root) if shell.spec.flavor == "cmd" else None
        if os.name == "nt":
            shell.job = WindowsShellJob()
        console = (
            cmd_console_options()
            if os.name == "nt" and shell.spec.flavor == "cmd"
            else {
                "creationflags": (
                    int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
                )
            }
        )
        # Own creation before suspension, including a handle returned after the
        # observer is cancelled. Cleanup adopts and reaps that same handle.
        shell.spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *shell.spec.arguments(bootstrap),
                cwd=shell.cwd,
                stdin=(
                    asyncio.subprocess.DEVNULL
                    if shell.spec.flavor == "pwsh"
                    else asyncio.subprocess.PIPE
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                start_new_session=os.name == "posix",
                **console,
            )
        )
        shell.process = await asyncio.shield(shell.spawn)
        if shell.job is not None:
            shell.job.attach(shell.process.pid)
        if shell.process.stdout is None or shell.process.stderr is None:
            raise ShellSessionError("protocol", "shell 输出连接不可用")
        shell.readers = [
            asyncio.create_task(self._pipe(shell, shell.process.stdout, "output")),
            asyncio.create_task(self._pipe(shell, shell.process.stderr, "stderr")),
        ]
        shell.watcher = asyncio.create_task(self._watch(shell))
        if shell.spec.flavor == "cmd":
            if shell.process.stdin is None:
                raise ShellSessionError("protocol", "shell 输入连接不可用")
            shell.process.stdin.write(cmd_bootstrap())
            await asyncio.wait_for(shell.process.stdin.drain(), CONTROL_TIMEOUT)
        if shell.spec.flavor in {"pwsh", "cmd"}:
            await asyncio.wait_for(shell.ready.wait(), CONTROL_TIMEOUT)

    def _append(self, shell: _Shell, stream: StreamName, data: bytes) -> None:
        item = shell.current
        if item is not None and data:
            item.output.append(stream, data, self._output_limit())
            item.changed.set()

    async def _pipe(self, shell: _Shell, stream: asyncio.StreamReader, name: StreamName) -> None:
        pending = b""
        try:
            while chunk := await stream.read(4096):
                pending += chunk
                item = shell.current
                fence_seen = (
                    None
                    if item is None
                    else item.stdout_fence
                    if name == "output"
                    else item.stderr_fence
                )
                if (
                    item is None
                    or item.state != "running"
                    or fence_seen is None
                    or fence_seen.is_set()
                ):
                    self._append(shell, name, pending)
                    pending = b""
                    continue
                fence = marker(shell.secret, item.id)
                position = pending.find(fence)
                if position >= 0:
                    self._append(shell, name, pending[:position])
                    pending = pending[position + len(fence) :]
                    fence_seen.set()
                    self._append(shell, name, pending)
                    pending = b""
                else:
                    # Only retain an actual partial fence, not an arbitrary
                    # full fence-width suffix that would hide small live output.
                    start = pending.rfind(b"\x00")
                    keep = (
                        len(pending) - start
                        if start >= 0 and fence.startswith(pending[start:])
                        else 0
                    )
                    count = len(pending) - keep
                    self._append(shell, name, pending[:count])
                    pending = pending[count:]
        except (ConnectionError, OSError):
            pass
        finally:
            self._append(shell, name, pending)
            shell.lost.set()

    async def _control(
        self, shell: _Shell, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        shell.writers.add(writer)
        persistent = False
        try:
            raw = await asyncio.wait_for(reader.readline(), CONTROL_TIMEOUT)
            while raw:
                if len(raw) > CONTROL_BYTES or not raw.endswith(b"\n"):
                    return
                data = json.loads(raw)
                if (
                    not isinstance(data, dict)
                    or not isinstance(data.get("secret"), str)
                    or not hmac.compare_digest(
                        data["secret"].encode("utf-8"), shell.secret.encode("ascii")
                    )
                    or data.get("epoch") != shell.epoch
                    or shell.state not in {"starting", "busy", "idle"}
                ):
                    return
                if data.get("op") == "ready":
                    if (
                        set(data) != {"op", "secret", "epoch"}
                        or shell.spec.flavor not in {"pwsh", "cmd"}
                        or shell.ready.is_set()
                    ):
                        return
                    persistent = shell.spec.flavor == "pwsh"
                    if persistent:
                        shell.control_writer = writer
                    shell.ready.set()
                    if not persistent:
                        writer.write(b"1")
                        await asyncio.wait_for(writer.drain(), CONTROL_TIMEOUT)
                        return
                elif data.get("op") == "complete":
                    item = shell.current
                    if (
                        set(data) != {"op", "secret", "epoch", "command_id", "exit_code", "cwd"}
                        or item is None
                        or item.state != "running"
                        or data.get("command_id") != item.id
                        or type(data.get("exit_code")) is not int
                        or not -(2**31) <= data["exit_code"] < 2**32
                        or not isinstance(data.get("cwd"), str)
                        or "\x00" in data["cwd"]
                        or item.proof.is_set()
                    ):
                        return
                    data["cwd"].encode("utf-8", errors="strict")
                    item.exit_code = data["exit_code"]
                    item.cwd = data["cwd"]
                    shell.cwd = item.cwd
                    item.proof.set()
                    if not persistent:
                        writer.write(b"1")
                        await asyncio.wait_for(writer.drain(), CONTROL_TIMEOUT)
                        return
                else:
                    return
                raw = await reader.readline()
        except (ValueError, ConnectionError, OSError, TimeoutError):
            pass
        finally:
            shell.writers.discard(writer)
            writer.close()
            with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), CONTROL_TIMEOUT)
            if persistent and shell.state not in {"stopping", "closed", "blocked"}:
                shell.lost.set()
                self._retire(shell, "unknown")

    async def _monitor(self, item: _Command) -> None:
        shell = item.shell

        async def completed() -> None:
            await item.proof.wait()
            await item.stdout_fence.wait()
            await item.stderr_fence.wait()

        proof = asyncio.create_task(completed())
        lost = asyncio.create_task(shell.lost.wait())
        try:
            remaining = max(0, item.deadline - asyncio.get_running_loop().time())
            ready, _ = await asyncio.wait(
                {proof, lost}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if proof in ready and shell.state == "busy" and shell.current is item:
                item.state = "completed"
                shell.state = "idle"
                item.done.set()
                item.changed.set()
                with contextlib.suppress(OSError):
                    command_file(shell.root, item.id, shell.spec.flavor).unlink(missing_ok=True)
                await self._finish(item)
            else:
                await asyncio.shield(self._retire(shell, "unknown" if ready else "timed_out"))
        finally:
            proof.cancel()
            lost.cancel()
            await asyncio.gather(proof, lost, return_exceptions=True)

    async def _finish(self, item: _Command) -> None:
        if (
            not item.notify_on_finish
            or not item.dispatched
            or not item.done.is_set()
            or item.finish_attempted
            or self._on_finish is None
        ):
            return
        # Claim before suspension: a concurrent retire must neither publish
        # twice nor detach a callback from its original monitor/cleanup owner.
        item.finish_attempted = True
        try:
            await self._on_finish(
                ShellFinished(
                    item.shell.session_id,
                    item.id,
                    item.shell.epoch,
                    item.state,
                    item.exit_code if item.state == "completed" else None,
                )
            )
        except (Exception, asyncio.CancelledError):
            # This optional local notice never rewrites the observed command
            # outcome or prevents process cleanup. Recovery is an explicit read,
            # not an automatic command replay or detached notification retry.
            pass

    async def _watch(self, shell: _Shell) -> None:
        process = shell.process
        if process is None:
            return
        await process.wait()
        shell.lost.set()
        if shell.state not in {"stopping", "closed", "blocked"}:
            await asyncio.shield(self._retire(shell, "unknown"))

    def _retire(self, shell: _Shell, outcome: str) -> asyncio.Task[None]:
        if shell.cleanup is not None:
            return shell.cleanup
        # This synchronous fence is also used by configuration/mode changes.
        shell.state = "stopping"
        if shell.current is not None and shell.current.state == "running":
            shell.current.state = outcome
            shell.current.exit_code = None
        shell.cleanup = asyncio.create_task(self._cleanup(shell))
        return shell.cleanup

    async def _cleanup(self, shell: _Shell) -> None:
        failures = False
        if shell.boot is not None and not shell.boot.done():
            shell.boot.cancel()
        if shell.boot is not None:
            await asyncio.gather(shell.boot, return_exceptions=True)
        if shell.spawn is not None:
            try:
                shell.process = await asyncio.shield(shell.spawn)
            except Exception:
                pass
        if shell.server is not None:
            shell.server.close()
        # Closing the listener leaves existing control connections alive.
        # Release them before waiting for the server to finish closing.
        for writer in tuple(shell.writers):
            writer.close()
        controls = tuple(shell.controls)
        for task in controls:
            task.cancel()
        if shell.job is not None:
            try:
                shell.job.terminate()
            except OSError:
                failures = True
        tasks = [*shell.readers]
        if shell.watcher is not None:
            tasks.append(shell.watcher)
        tasks.extend(
            item.monitor
            for item in self._commands.values()
            if item.shell is shell and item.monitor is not None
        )
        if shell.process is not None:
            try:
                await _settle_process(shell.process, tasks)
            except (OSError, TimeoutError):
                failures = True
        else:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*controls, return_exceptions=True)
        if shell.server is not None:
            try:
                await asyncio.wait_for(shell.server.wait_closed(), CONTROL_TIMEOUT)
            except (OSError, TimeoutError):
                failures = True
        if shell.job is not None:
            try:
                deadline = asyncio.get_running_loop().time() + CONTROL_TIMEOUT
                while shell.job.active_processes() != 0:
                    if asyncio.get_running_loop().time() >= deadline:
                        failures = True
                        break
                    await asyncio.sleep(0.01)
                if not failures:
                    shell.job.close()
            except OSError:
                failures = True
        shell.cleanup_failed = failures
        shell.state = "blocked" if failures else "closed"
        if shell.current is not None:
            if failures and shell.current.state != "completed":
                shell.current.state = "unknown"
            shell.current.done.set()
            shell.current.changed.set()
        if shell.current is not None:
            await self._finish(shell.current)
        if not failures:
            if self._sessions.get(shell.session_id) is shell:
                del self._sessions[shell.session_id]
            with contextlib.suppress(OSError):
                shell.directory.cleanup()

    def invalidate(self, session_id: str) -> None:
        shell = self._sessions.get(session_id)
        if shell is not None:
            self._retire(shell, "stopped")

    async def stop(self, session_id: str) -> dict[str, Any]:
        # Capture the OS generation before the first await. A later authorized
        # execute can never be killed by this older stop's continuation.
        shell = self._sessions.get(session_id)
        if shell is None:
            return self.status(session_id)
        cancelled = await _wait_cleanup(self._retire(shell, "stopped"))
        if cancelled:
            raise asyncio.CancelledError
        if shell.cleanup_failed:
            raise ShellSessionError("cleanup_unknown", "未能确认 shell 子树已收口")
        if shell.current is not None:
            return self._receipt(shell.current, shell.current.output.observed_by_stream.copy())
        return self._empty(shell.session_id)

    async def read(
        self,
        session_id: str,
        command_id: str,
        *,
        cursor: OutputCursor | None = None,
        wait_seconds: float = 0,
    ) -> dict[str, Any]:
        wait = self._number(wait_seconds, maximum=10, zero=True)
        item = self._commands.get(command_id)
        if item is None or item.shell.session_id != session_id:
            raise ShellSessionError("not_found", "该会话没有可读的 shell 命令记录")
        cursor = _read_cursor(cursor, item.output)
        if wait and item.state == "running" and item.output.read(cursor)[2] == cursor:
            item.changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(item.changed.wait(), wait)
        return self._receipt(item, cursor)

    def _receipt_budget(
        self, receipt: dict[str, Any], *, before_dispatch: bool = False
    ) -> int | None:
        maximum = self._max_receipt_chars() if self._max_receipt_chars is not None else None
        return _receipt_budget(receipt, maximum, before_dispatch=before_dispatch)

    def _empty(self, session_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "session_id": session_id,
            "command_id": None,
            "os_epoch": None,
            "shell_pid": None,
            "executable": None,
            "flavor": None,
            "state": "not_started",
            "shell_state": "not_started",
            "exit_code": None,
            "status_basis": None,
            "cwd": None,
            "output": "",
            "stderr": "",
            "cursor": _empty_cursor(),
            "next_cursor": _empty_cursor(),
            "observed_bytes": 0,
            "discarded_bytes": 0,
            "truncated": False,
            "output_scope": "shell",
        }
        self._receipt_budget(result)
        return result

    def status(self, session_id: str) -> dict[str, Any]:
        shell = self._sessions.get(session_id)
        item = (
            shell.current
            if shell is not None
            else next(
                (
                    value
                    for value in reversed(self._commands.values())
                    if value.shell.session_id == session_id
                ),
                None,
            )
        )
        if item is None:
            return self._empty(session_id)
        return self._receipt(item, item.output.observed_by_stream.copy())

    @staticmethod
    def _receipt_metadata(item: _Command, cursor: OutputCursor) -> dict[str, Any]:
        shell = item.shell
        return {
            "session_id": shell.session_id,
            "command_id": item.id,
            "os_epoch": shell.epoch,
            "shell_pid": shell.process.pid if shell.process is not None else None,
            "executable": shell.spec.executable,
            "flavor": shell.spec.flavor,
            "state": item.state,
            "shell_state": shell.state,
            "exit_code": item.exit_code if item.state == "completed" else None,
            "status_basis": (
                "powershell_success" if shell.spec.flavor == "pwsh" else "shell_status"
            ),
            "cwd": item.cwd or shell.cwd,
            "output": "",
            "stderr": "",
            "cursor": cursor.copy(),
            "next_cursor": {
                stream: max(cursor[stream], item.output.discarded_by_stream[stream])
                for stream in _STREAMS
            },
            "observed_bytes": item.output.observed,
            "discarded_bytes": item.output.discarded,
            "truncated": item.output.discarded > 0,
            "output_scope": "shell",
            **(
                {"notify_on_finish": True, "notification_destination": "web"}
                if item.notify_on_finish
                else {}
            ),
        }

    def _receipt(self, item: _Command, cursor: OutputCursor) -> dict[str, Any]:
        item.output.trim(self._output_limit())
        result = self._receipt_metadata(item, cursor)
        maximum = self._max_receipt_chars() if self._max_receipt_chars is not None else None
        return _receipt_page(item.output, result, cursor, maximum=maximum, final=item.done.is_set())

    async def aclose(self) -> None:
        self._closed = True
        cleanups = [self._retire(shell, "stopped") for shell in tuple(self._sessions.values())]

        async def settle() -> None:
            await asyncio.gather(*cleanups)

        cancelled = await _wait_cleanup(asyncio.create_task(settle()))
        if cancelled:
            raise asyncio.CancelledError
        if any(shell.cleanup_failed for shell in self._sessions.values()):
            raise ShellSessionError("cleanup_unknown", "未能确认所有 shell 子树已收口")
