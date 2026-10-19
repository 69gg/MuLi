"""One bounded, serial JSON-lines session for an AX reference-holding helper."""

from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import subprocess
from collections.abc import Callable
from typing import Any

from muli.process import _OutputBudget, _settle_process, _wait_cleanup

MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 250000
MAX_RESPONSE_CHARS = 60000
MAX_SESSION_BYTES = 1000000
MAX_REQUESTS = 16
MAX_STDERR_BYTES = 2048
_CLOSE_GRACE = 0.2
_COMMANDS = frozenset({"probe", "targets", "snapshot", "action", "type", "release"})


class AXLeaseError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        outcome: str = "not_started",
        *,
        native_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code, self.message, self.outcome = code, message, outcome
        self.native_code = native_code


class AXHelperLease:
    """Create inside the owning event loop; construction never starts a process.

    Caller/credential ownership belongs to Computer. This class owns creation,
    protocol state and reaping until close settles, including late spawn returns.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        timeout: float,
        ttl: float,
        max_nodes: int,
        max_response_chars: int = MAX_RESPONSE_CHARS,
        once: bool = False,
    ) -> None:
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
        ):
            raise ValueError("AX helper 命令参数无效")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in (timeout, ttl)
        ):
            raise ValueError("AX helper 期限须为有限正数")
        if type(max_nodes) is not int or not 1 <= max_nodes <= 1000:
            raise ValueError("AX helper 节点预算无效")
        if (
            type(max_response_chars) is not int
            or not 512 <= max_response_chars <= MAX_RESPONSE_CHARS
        ):
            raise ValueError("AX helper 响应预算无效")
        if type(once) is not bool:
            raise ValueError("AX helper 单次模式无效")
        self._loop = asyncio.get_running_loop()
        self.expires_at = self._loop.time() + ttl
        self._argv = tuple(argv)
        self._timeout, self._max_nodes = timeout, max_nodes
        self._max_response_chars, self._once = max_response_chars, once
        self._state = "new"
        self._starting: asyncio.Task[asyncio.subprocess.Process] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._readers: list[asyncio.Task[None]] = []
        self._cleanup: asyncio.Task[None] | None = None
        self._pending: asyncio.Future[dict[str, Any]] | None = None
        self._request_id: str | None = None
        self._in_request = False
        self._mutation = self._dispatched = False
        self._count = 0
        self._failure: AXLeaseError | None = None
        self._budget = _OutputBudget(MAX_SESSION_BYTES)
        self._stderr = bytearray()
        self._timer = self._loop.call_at(self.expires_at, self._expire)

    @property
    def state(self) -> str:
        return self._state

    @property
    def closed(self) -> bool:
        return self._state == "closed"

    def _expire(self) -> None:
        if self._state in {"closing", "closed"}:
            return
        if self._loop.time() < self.expires_at:
            self._timer = self._loop.call_at(self.expires_at, self._expire)
            return
        self._retire(AXLeaseError("lease_expired", "AX 引用已过期，请重新观察"))

    def _check_alive(self) -> None:
        if self._state in {"closing", "closed"}:
            raise self._failure or AXLeaseError("lease_closed", "AX helper 会话已关闭")
        if self._loop.time() >= self.expires_at:
            raise AXLeaseError("lease_expired", "AX 引用已过期，请重新观察")

    def _uncertain(self, error: AXLeaseError) -> AXLeaseError:
        if self._mutation and self._dispatched:
            return AXLeaseError(
                "action_unknown_outcome",
                "动作请求已发送，结果未知；请重新观察，勿自动重试",
                "unknown",
            )
        return error

    def _retire(self, error: AXLeaseError | None = None) -> None:
        if self._state in {"closing", "closed"}:
            return
        self._state = "closing"
        self._timer.cancel()
        self._failure = error
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(
                self._uncertain(error or AXLeaseError("lease_closed", "AX helper 会话已关闭"))
            )
        self._cleanup = self._loop.create_task(self._finish())
        self._cleanup.add_done_callback(self._cleanup_done)

    def _cleanup_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._failure = AXLeaseError("backend_unavailable", "AX helper 清理未能完成")

    async def _finish(self) -> None:
        if self._starting is not None:
            try:
                # A cancelled observer must not abandon a late process handle.
                process = await asyncio.shield(self._starting)
            except Exception:
                self._state = "closed"
                return
            self._process = process
            await _settle_process(
                process, self._readers, eof_grace=_CLOSE_GRACE, terminate_grace=_CLOSE_GRACE
            )
        self._state = "closed"

    async def aclose(self) -> None:
        self._retire()
        try:
            if self._cleanup is not None and await _wait_cleanup(self._cleanup):
                raise asyncio.CancelledError
        except Exception as exc:
            # Keep the slot retired on cleanup failure and never expose native
            # diagnostics. In particular this is not evidence to retry an action.
            raise AXLeaseError("backend_unavailable", "AX helper 清理未能完成") from exc

    async def _close_preserving_result(self) -> None:
        try:
            await self.aclose()
        except AXLeaseError:
            # A known receipt or the original host exception remains definitive.
            # _cleanup_done records failure; closed remains False to prevent reuse.
            pass

    async def _start(self) -> None:
        self._check_alive()
        if self._starting is None:
            self._state = "starting"
            self._starting = self._loop.create_task(
                asyncio.create_subprocess_exec(
                    *self._argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name == "posix",
                    creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if os.name == "nt"
                    else 0,
                )
            )
        try:
            self._process = await asyncio.shield(self._starting)
        except FileNotFoundError as exc:
            raise AXLeaseError("dependency_missing", "无法启动 AX helper Python") from exc
        except OSError as exc:
            raise AXLeaseError("backend_unavailable", "无法启动 AX helper") from exc
        self._check_alive()
        if not self._readers:
            assert self._process.stdout is not None and self._process.stderr is not None
            self._readers = [
                self._loop.create_task(self._stdout(self._process.stdout)),
                self._loop.create_task(self._read_stderr(self._process.stderr)),
            ]
        self._state = "in_flight"

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        try:
            while chunk := await stream.read(4096):
                self._budget.charge(chunk)
                self._stderr.extend(chunk[: max(0, MAX_STDERR_BYTES - len(self._stderr))])
        except asyncio.CancelledError:
            raise
        except ValueError:
            self._retire(AXLeaseError("limit_exceeded", "AX helper 累计输出超过限制"))
        except Exception:
            self._retire(AXLeaseError("protocol_error", "无法读取 AX helper 诊断管道"))

    async def _stdout(self, stream: asyncio.StreamReader) -> None:
        buffer = bytearray()
        try:
            while chunk := await stream.read(4096):
                self._budget.charge(chunk)
                buffer.extend(chunk)
                while b"\n" in buffer:
                    index = buffer.index(b"\n")
                    if index + 1 > MAX_RESPONSE_BYTES:
                        raise ValueError("frame_limit")
                    frame = bytes(buffer[:index])
                    del buffer[: index + 1]
                    self._response(frame)
                    if self._state in {"closing", "closed"}:
                        return
                if len(buffer) >= MAX_RESPONSE_BYTES:
                    raise ValueError("frame_limit")
            if buffer:
                self._retire(AXLeaseError("protocol_error", "AX helper 返回了未终结的 JSON 帧"))
            elif not (self._once and self._pending is not None and self._pending.done()):
                self._retire(AXLeaseError("backend_unavailable", "AX helper 意外退出"))
        except asyncio.CancelledError:
            raise
        except ValueError:
            self._retire(AXLeaseError("limit_exceeded", "AX helper 输出超过限制"))
        except Exception:
            self._retire(AXLeaseError("protocol_error", "无法读取 AX helper 协议管道"))

    @staticmethod
    def _invalid_constant(value: str) -> None:
        raise ValueError("nonfinite_json")

    def _response(self, frame: bytes) -> None:
        try:
            text = frame.decode("utf-8", errors="strict")
            if len(text) + 1 > self._max_response_chars:
                self._retire(AXLeaseError("limit_exceeded", "AX helper 响应超过字符限制"))
                return
            if not text:
                raise ValueError("response_empty")
            data = json.loads(text, parse_constant=self._invalid_constant)
            if (
                not isinstance(data, dict)
                or type(data.get("v")) is not int
                or data["v"] != 1
                or data.get("request_id") != self._request_id
                or self._pending is None
                or self._pending.done()
                or not self._dispatched
                or set(data)
                not in (
                    {"v", "request_id", "result"},
                    {"v", "request_id", "error"},
                )
            ):
                raise ValueError("envelope")
            if "error" in data:
                error = data["error"]
                if (
                    not isinstance(error, dict)
                    or not isinstance(error.get("code"), str)
                    or not 1 <= len(error["code"]) <= 64
                    or not isinstance(error.get("message"), str)
                    or len(error["message"]) > 1000
                    or error.get("outcome") not in {"not_started", "unknown"}
                    or ("native_code" in error and type(error["native_code"]) is not int)
                    or set(error) - {"code", "message", "outcome", "native_code"}
                ):
                    raise ValueError("error")
                self._pending.set_exception(
                    AXLeaseError(
                        error["code"],
                        error["message"],
                        error["outcome"],
                        native_code=error.get("native_code"),
                    )
                )
            else:
                result = data["result"]
                if not isinstance(result, dict):
                    raise ValueError("result")
                if self._mutation and not (
                    result.get("ok") is True
                    and result.get("outcome") == "accepted"
                    and result.get("effect_verified") is False
                ):
                    raise ValueError("action_receipt")
                self._pending.set_result(result)
        except (ValueError, TypeError, RecursionError):
            self._retire(AXLeaseError("protocol_error", "AX helper JSON 帧格式或请求 ID 无效"))

    @staticmethod
    def _validate_payload(command: str, payload: dict[str, Any]) -> None:
        fields = {
            "probe": (set(), set()),
            "release": (set(), set()),
            "targets": ({"application_ref_id"}, set()),
            "snapshot": ({"target_ref_id"}, set()),
            "action": ({"ref_id", "action"}, {"ref_id", "action"}),
            "type": ({"ref_id", "text"}, {"ref_id", "text"}),
        }
        allowed, required = fields[command]
        if (
            not isinstance(payload, dict)
            or not required <= set(payload) <= allowed
            or any(
                not isinstance(value, str) or len(value) > MAX_REQUEST_BYTES
                for value in payload.values()
            )
            or any(not value for key, value in payload.items() if key != "text")
        ):
            raise AXLeaseError("protocol_error", "AX helper 请求载荷无效")

    async def request(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        deadline: float | None = None,
        before_send: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(command, str)
            or command not in _COMMANDS
            or (self._once and command != "probe")
        ):
            raise AXLeaseError("unsupported_operation", "AX helper 不支持该命令")
        self._validate_payload(command, payload)
        if deadline is not None and (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise AXLeaseError("protocol_error", "AX helper 请求期限无效")
        if self._in_request:
            raise AXLeaseError("desktop_busy", "AX helper 正在处理另一条请求")
        self._in_request = True
        self._mutation, self._dispatched = command in {"action", "type"}, False
        result: dict[str, Any] | None = None
        try:
            self._check_alive()
            if self._count >= MAX_REQUESTS:
                raise AXLeaseError("limit_exceeded", "AX helper 会话请求数达到限制")
            operation_deadline = min(self._loop.time() + self._timeout, self.expires_at)
            if deadline is not None:
                operation_deadline = min(operation_deadline, deadline)
            async with asyncio.timeout_at(operation_deadline):
                await self._start()
                self._request_id = secrets.token_hex(16)
                frame = (
                    json.dumps(
                        {
                            "v": 1,
                            "request_id": self._request_id,
                            "command": command,
                            "payload": payload,
                            "limits": {
                                "timeout_seconds": max(0, operation_deadline - self._loop.time()),
                                "max_nodes": self._max_nodes,
                                "max_response_chars": self._max_response_chars,
                            },
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                    + b"\n"
                )
                if len(frame) > MAX_REQUEST_BYTES:
                    raise AXLeaseError("limit_exceeded", "AX helper 请求超过字节限制")
                self._pending = self._loop.create_future()
                if before_send is not None:
                    before_send()  # Preserve host TaskPaused/ComputerError unchanged.
                self._check_alive()
                if self._loop.time() >= operation_deadline:
                    raise AXLeaseError("read_timeout", "AX helper 请求期限已过")
                assert self._process is not None and self._process.stdin is not None
                # Final host fence and write are adjacent, with no await in between.
                self._dispatched = True
                self._count += 1
                try:
                    self._process.stdin.write(frame)
                    await self._process.stdin.drain()
                except (ConnectionError, OSError, RuntimeError) as exc:
                    raise self._uncertain(
                        AXLeaseError("backend_unavailable", "AX helper 管道已关闭")
                    ) from exc
                result = await asyncio.shield(self._pending)
                if self._failure is not None and not self._mutation:
                    raise self._failure
                return result
        except asyncio.CancelledError:
            self._retire(AXLeaseError("lease_closed", "AX helper 请求已取消"))
            await self._close_preserving_result()
            raise
        except TimeoutError as exc:
            error = self._uncertain(AXLeaseError("read_timeout", "AX helper 请求超时"))
            self._retire(error)
            await self._close_preserving_result()
            raise error from exc
        except AXLeaseError as exc:
            self._retire(exc)
            await self._close_preserving_result()
            raise
        except (BrokenPipeError, ConnectionResetError) as exc:
            error = self._uncertain(AXLeaseError("backend_unavailable", "AX helper 管道已关闭"))
            self._retire(error)
            await self._close_preserving_result()
            raise error from exc
        except BaseException:
            self._retire(AXLeaseError("lease_closed", "AX helper 请求已终止"))
            await self._close_preserving_result()
            raise
        finally:
            try:
                if result is not None and self._mutation:
                    await self._close_preserving_result()
                elif result is not None and self._once:
                    await self.aclose()
            finally:
                if (
                    self._pending is not None
                    and self._pending.done()
                    and not self._pending.cancelled()
                ):
                    self._pending.exception()
                self._request_id, self._pending = None, None
                self._in_request = False
                if self._state not in {"closing", "closed"}:
                    self._state = "ready"
