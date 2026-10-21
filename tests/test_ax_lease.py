"""AX transport tests use only Python children; no accessibility API is loaded."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from muli.process import _settle_process, run_process
from muli.tools import ax_lease as module
from muli.tools.ax_lease import AXHelperLease, AXLeaseError

_HELPER = r"""
import json
import os
import sys
import time
from typing import Any

mode: str = sys.argv[1]

def send(data: dict[str, Any]) -> None:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n"
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()

for raw in sys.stdin.buffer:
    request: dict[str, Any] = json.loads(raw)
    command: str = request["command"]
    response: dict[str, Any] = {
        "v": 1,
        "request_id": request["request_id"],
        "result": {
            "pid": os.getpid(),
            "command": command,
            "payload": request["payload"],
            "limits": request["limits"],
            "request_id": request["request_id"],
        },
    }
    if command in {"action", "type"}:
        response["result"].update(ok=True, outcome="accepted", effect_verified=False)
    if mode == "hang":
        time.sleep(30)
    elif mode == "wrong_id":
        response["request_id"] = "wrong"
    elif mode == "bad_version":
        response["v"] = True
    elif mode == "both":
        response["error"] = {"code": "bad", "message": "bad", "outcome": "not_started"}
    elif mode == "warning":
        sys.stdout.buffer.write(b"debug warning\n")
        sys.stdout.buffer.flush()
        time.sleep(30)
    elif mode == "utf8":
        sys.stdout.buffer.write(b"\xff\n")
        sys.stdout.buffer.flush()
        time.sleep(30)
    elif mode == "partial":
        sys.stdout.buffer.write(b'{"v":1')
        sys.stdout.buffer.flush()
        break
    elif mode == "eof":
        break
    elif mode == "frame_bytes":
        sys.stdout.buffer.write(b"x" * 250001)
        sys.stdout.buffer.flush()
        time.sleep(30)
    elif mode == "frame_chars":
        response["result"]["text"] = "中" * 1000
    elif mode == "stderr":
        sys.stderr.buffer.write(b"s" * 4096)
        sys.stderr.buffer.flush()
    elif mode == "stderr_flood":
        sys.stderr.buffer.write(b"s" * 1000001)
        sys.stderr.buffer.flush()
        time.sleep(30)
    elif mode == "idle_flood":
        send(response)
        sys.stderr.buffer.write(b"s" * 1000001)
        sys.stderr.buffer.flush()
        time.sleep(30)
    elif mode == "native_error":
        response.pop("result")
        response["error"] = {
            "code": "element_disabled", "message": "节点不可编辑",
            "outcome": "not_started", "native_code": -25205,
        }
    elif mode == "bad_receipt":
        response["result"]["effect_verified"] = True
    elif mode.startswith("error:"):
        _, code, outcome = mode.split(":", 2)
        response.pop("result")
        response["error"] = {"code": code, "message": "有界诊断", "outcome": outcome}
    send(response)
    if mode == "duplicate":
        send(response)
    if mode == "once":
        break
"""


def lease(mode: str = "normal", **kwargs: Any) -> AXHelperLease:
    options: dict[str, Any] = {"timeout": 2, "ttl": 10, "max_nodes": 30}
    options.update(kwargs)
    return AXHelperLease([sys.executable, "-u", "-c", _HELPER, mode], **options)


def assert_reaped(item: AXHelperLease) -> None:
    assert item.closed and item.state == "closed"
    if item._process is not None:
        assert item._process.returncode is not None
    assert all(task.done() for task in item._readers)
    assert item._cleanup is not None and item._cleanup.done()
    assert not item._in_request


async def test_serial_requests_keep_one_process_and_return_only_result() -> None:
    item = lease()
    assert item.state == "new" and item._starting is None
    expires = item.expires_at
    try:
        first = await item.request("targets", {})
        second = await item.request("snapshot", {"target_ref_id": "r1"})
        assert first["pid"] == second["pid"]
        assert first["request_id"] != second["request_id"]
        assert "v" not in first and second["payload"] == {"target_ref_id": "r1"}
        assert second["limits"]["max_nodes"] == 30
        assert second["limits"]["max_response_chars"] == 60000
        assert 0 < second["limits"]["timeout_seconds"] <= 2
        assert item.expires_at == expires and item.state == "ready"
        await item.request("release", {})
    finally:
        await item.aclose()
    await item.aclose()
    assert_reaped(item)


@pytest.mark.parametrize("command", ["action", "type"])
async def test_mutation_acceptance_consumes_the_whole_session(command: str) -> None:
    item = lease()
    payload = (
        {"ref_id": "r1", "action": "AXPress"}
        if command == "action"
        else {"ref_id": "r1", "text": "你好\n世界"}
    )
    result = await item.request(command, payload)
    assert result["ok"] is True and result["outcome"] == "accepted"
    assert result["effect_verified"] is False
    assert_reaped(item)
    with pytest.raises(AXLeaseError):
        await item.request("snapshot", {})


async def test_once_probe_closes_without_persisting_a_reference_session() -> None:
    item = lease("once", once=True)
    result = await item.request("probe", {})
    assert result["command"] == "probe"
    assert_reaped(item)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"timeout": 0}, "期限"),
        ({"timeout": float("nan")}, "期限"),
        ({"ttl": float("inf")}, "期限"),
        ({"ttl": True}, "期限"),
        ({"max_nodes": True}, "节点"),
        ({"max_nodes": 1001}, "节点"),
        ({"max_response_chars": 511}, "响应"),
        ({"max_response_chars": 60001}, "响应"),
        ({"once": "yes"}, "单次"),
    ],
)
async def test_constructor_rejects_invalid_budgets(options: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        lease(**options)


@pytest.mark.parametrize(
    ("command", "payload"),
    [
        ("unknown", {}),
        ("probe", {"ref_id": "r1"}),
        ("snapshot", {"target_ref_id": ""}),
        ("targets", {"application_ref_id": 123}),
        ("type", {"ref_id": "r1"}),
        ("action", {"ref_id": "r1", "action": "click", "owner": "forged"}),
    ],
)
async def test_invalid_requests_do_not_start_helper(command: str, payload: dict[str, Any]) -> None:
    item = lease()
    try:
        with pytest.raises(AXLeaseError):
            await item.request(command, payload)
        assert item._starting is None
    finally:
        await item.aclose()


async def test_once_mode_rejects_native_commands_without_starting() -> None:
    item = lease(once=True)
    try:
        with pytest.raises(AXLeaseError) as caught:
            await item.request("snapshot", {})
        assert caught.value.code == "unsupported_operation"
        assert item._starting is None
    finally:
        await item.aclose()


@pytest.mark.parametrize("command", ["snapshot", "action"])
@pytest.mark.parametrize("mode", ["wrong_id", "bad_version", "both", "warning", "utf8", "partial"])
async def test_invalid_wire_retires_without_resynchronizing(mode: str, command: str) -> None:
    item = lease(mode)
    payload = {} if command == "snapshot" else {"ref_id": "r1", "action": "AXPress"}
    with pytest.raises(AXLeaseError) as caught:
        await item.request(command, payload)
    if command == "action":
        assert caught.value.code == "action_unknown_outcome" and caught.value.outcome == "unknown"
    else:
        assert caught.value.code == "protocol_error"
    assert item._count == 1
    assert_reaped(item)


async def test_native_pre_dispatch_error_and_code_are_preserved() -> None:
    item = lease("native_error")
    with pytest.raises(AXLeaseError) as caught:
        await item.request("type", {"ref_id": "r1", "text": "私有文本"})
    assert caught.value.code == "element_disabled"
    assert caught.value.outcome == "not_started" and caught.value.native_code == -25205
    assert "私有文本" not in str(caught.value)
    assert_reaped(item)


@pytest.mark.parametrize("mode", ["bad_receipt", "eof"])
async def test_lost_or_unverifiable_mutation_receipt_is_unknown(mode: str) -> None:
    item = lease(mode)
    with pytest.raises(AXLeaseError) as caught:
        await item.request("action", {"ref_id": "r1", "action": "AXPress"})
    assert caught.value.code == "action_unknown_outcome" and caught.value.outcome == "unknown"
    assert item._count == 1
    assert_reaped(item)


@pytest.mark.parametrize("mode", ["frame_bytes", "frame_chars", "stderr_flood"])
async def test_frame_and_session_output_are_bounded(mode: str) -> None:
    item = lease(mode, max_response_chars=512)
    with pytest.raises(AXLeaseError) as caught:
        await item.request("snapshot", {})
    assert caught.value.code == "limit_exceeded"
    assert len(item._stderr) <= module.MAX_STDERR_BYTES
    assert item._budget.used <= module.MAX_SESSION_BYTES + 4096
    assert_reaped(item)


async def test_stderr_is_separate_and_only_a_small_prefix_is_saved() -> None:
    item = lease("stderr")
    try:
        assert (await item.request("snapshot", {}))["command"] == "snapshot"
    finally:
        await item.aclose()
    assert 0 < len(item._stderr) <= module.MAX_STDERR_BYTES
    assert item._budget.used > len(item._stderr)
    assert_reaped(item)


async def test_idle_output_is_still_counted_and_retires_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = lease("idle_flood")
    retired = asyncio.Event()
    original = item._retire

    def retire(error: AXLeaseError | None = None) -> None:
        original(error)
        retired.set()

    monkeypatch.setattr(item, "_retire", retire)
    try:
        try:
            await item.request("snapshot", {})
        except AXLeaseError as exc:
            assert exc.code == "limit_exceeded"
        await asyncio.wait_for(retired.wait(), 2)
        await item.aclose()
        assert item._failure is not None and item._failure.code == "limit_exceeded"
        assert_reaped(item)
    finally:
        await item.aclose()


async def test_lifetime_request_limit_does_not_restart_the_helper() -> None:
    item = lease()
    try:
        for _ in range(module.MAX_REQUESTS):
            await item.request("snapshot", {})
        starting = item._starting
        with pytest.raises(AXLeaseError) as caught:
            await item.request("snapshot", {})
        assert caught.value.code == "limit_exceeded"
        assert item._starting is starting and item._count == module.MAX_REQUESTS
    finally:
        await item.aclose()
    assert_reaped(item)


async def test_utf8_request_budget_is_checked_before_dispatch() -> None:
    item = lease()
    with pytest.raises(AXLeaseError) as caught:
        await item.request("type", {"ref_id": "r1", "text": "中" * 30000})
    assert caught.value.code == "limit_exceeded" and caught.value.outcome == "not_started"
    assert item._count == 0
    assert_reaped(item)


async def test_host_fence_exception_is_not_wrapped_or_dispatched() -> None:
    class HostPaused(Exception):
        pass

    expected = HostPaused("host epoch changed")
    item = lease()

    def before_send() -> None:
        assert item._starting is not None and item._process is not None
        assert item._count == 0
        raise expected

    with pytest.raises(HostPaused) as caught:
        await item.request("action", {"ref_id": "r1", "action": "AXPress"}, before_send=before_send)
    assert caught.value is expected and item._count == 0
    assert_reaped(item)


async def test_busy_observer_cannot_cancel_another_inflight_request() -> None:
    item = lease("hang")
    sent = asyncio.Event()
    first = asyncio.create_task(item.request("snapshot", {}, before_send=sent.set))
    try:
        await asyncio.wait_for(sent.wait(), 2)
        with pytest.raises(AXLeaseError) as caught:
            await item.request("snapshot", {})
        assert caught.value.code == "desktop_busy"
        assert item.state == "in_flight" and not first.done()
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert_reaped(item)


async def test_request_timeout_includes_response_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = lease("hang")
    try:
        # This case starts at the response phase. Cold startup has its own
        # unchanged 150ms deadline below; it must not decide this phase's result.
        async with asyncio.timeout(item._timeout):
            await item._start()
        process = item._process
        assert process is not None and process.stdin is not None
        drained = asyncio.Event()
        original_drain = process.stdin.drain

        async def drain() -> None:
            await original_drain()
            drained.set()

        monkeypatch.setattr(process.stdin, "drain", drain)
        current = asyncio.get_running_loop().time()
        loop = Mock(spec=asyncio.AbstractEventLoop, wraps=asyncio.get_running_loop())
        # No await separates the completed startup from dispatch. Keep only
        # this lease's synchronous checks stable; the real timeout still runs.
        loop.time.return_value = current
        monkeypatch.setattr(item, "_loop", loop)
        with pytest.raises(AXLeaseError) as caught:
            await item.request(
                "action",
                {"ref_id": "r1", "action": "AXPress"},
                deadline=current + 0.15,
            )
        assert drained.is_set() and item._count == 1 and item._dispatched
        assert caught.value.code == "action_unknown_outcome" and caught.value.outcome == "unknown"
        assert isinstance(caught.value.__cause__, TimeoutError)
    finally:
        await item.aclose()
    assert_reaped(item)


async def test_request_timeout_includes_cold_startup_before_dispatch_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LaunchGate(monkeypatch)
    item = lease("hang")
    original_finish = item._finish
    cleaning = asyncio.Event()

    async def finish() -> None:
        cleaning.set()
        await original_finish()

    def forbidden_send() -> None:
        pytest.fail("Startup timeout must not reach the dispatch fence")

    monkeypatch.setattr(item, "_finish", finish)
    request = asyncio.create_task(
        item.request(
            "action",
            {"ref_id": "r1", "action": "AXPress"},
            deadline=asyncio.get_running_loop().time() + 0.15,
            before_send=forbidden_send,
        )
    )
    try:
        # The same deadline must retire even before a process handle returns.
        await asyncio.wait_for(cleaning.wait(), item._timeout)
        assert item.state == "closing" and not request.done()
        assert item._starting is not None and not item._starting.done()
        gate.release.set()
        with pytest.raises(AXLeaseError) as caught:
            await request
        assert caught.value.code == "read_timeout" and caught.value.outcome == "not_started"
        assert item._count == 0 and not item._dispatched
        assert item._starting.done() and item._process is gate.process
        assert gate.process is not None and gate.process.returncode is not None
        assert isinstance(caught.value.__cause__, TimeoutError)
    finally:
        gate.release.set()
        await item.aclose()
        await asyncio.gather(request, return_exceptions=True)
    assert_reaped(item)


async def test_absolute_ttl_early_callback_rechecks_and_does_not_renew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ttl = 0.2
    item = lease(ttl=ttl)
    expiry: float = item.expires_at
    item._timer.cancel()
    current: float = expiry - ttl
    scheduled: list[tuple[float, Callable[[], None], Mock]] = []
    budgets: list[float] = []

    def clock() -> float:
        return current

    def call_at(when: float, callback: Callable[[], None]) -> asyncio.TimerHandle:
        handle = Mock(spec=asyncio.TimerHandle)
        scheduled.append((when, callback, handle))
        return handle

    # Only this lease sees virtual time/timers. Futures and cleanup tasks still
    # belong to the real loop; unrelated test/transport deadlines stay real.
    loop = Mock(spec=asyncio.AbstractEventLoop, wraps=asyncio.get_running_loop())
    loop.time.side_effect = clock
    loop.call_at.side_effect = call_at
    monkeypatch.setattr(item, "_loop", loop)
    process = Mock(spec=asyncio.subprocess.Process)
    process.returncode = 0
    process.stdin = Mock(spec=asyncio.StreamWriter)
    process.stdin.drain = AsyncMock(return_value=None)

    def respond(raw: bytes) -> None:
        request = json.loads(raw)
        budgets.append(request["limits"]["timeout_seconds"])
        item._response(
            json.dumps(
                {"v": 1, "request_id": request["request_id"], "result": {"command": "snapshot"}}
            ).encode("utf-8")
        )

    async def start() -> None:
        nonlocal current
        item._check_alive()
        current += ttl / 4
        item._process, item._state = process, "in_flight"

    # This request seam cannot suspend: readiness and the matching protocol
    # frame are controlled here, not by whether an OS spawn fits inside 200ms.
    process.stdin.write.side_effect = respond
    monkeypatch.setattr(item, "_start", start)
    try:
        item._expire()
        assert item.state == "new" and item.expires_at == expiry
        assert len(scheduled) == 1 and scheduled[-1][0] == expiry
        current += ttl / 4
        scheduled[-1][1]()  # A second early callback must recheck/rearm too.
        assert item.state == "new" and item.expires_at == expiry
        assert len(scheduled) == 2 and scheduled[-1][0] == expiry
        for _ in range(2):
            assert (await item.request("snapshot", {}))["command"] == "snapshot"
            assert item.state == "ready" and item.expires_at == expiry
            assert item._cleanup is None and len(scheduled) == 2
        assert budgets == pytest.approx([ttl / 2, ttl / 4])
        assert item._count == 2 and item._starting is None
        current = expiry
        scheduled[-1][1]()  # Reach the original deadline without rewriting it.
        assert item.state == "closing" and not item.closed
        scheduled[-1][2].cancel.assert_called_once()
        with pytest.raises(AXLeaseError) as caught:
            await item.request("snapshot", {})
        assert caught.value.code == "lease_expired" and item._count == 2
    finally:
        await item.aclose()
    assert_reaped(item)


class LaunchGate:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ready, self.release = asyncio.Event(), asyncio.Event()
        self.process: asyncio.subprocess.Process | None = None
        original = asyncio.create_subprocess_exec

        async def start(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
            self.process = await original(*args, **kwargs)
            self.ready.set()
            await self.release.wait()
            return self.process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", start)


@pytest.mark.parametrize("operation", ["request_cancel", "close"])
async def test_launch_handle_is_adopted_after_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    gate = LaunchGate(monkeypatch)
    item = lease()
    request = asyncio.create_task(item.request("snapshot", {}))
    await asyncio.wait_for(gate.ready.wait(), 2)
    closer: asyncio.Task[Any]
    if operation == "request_cancel":
        request.cancel()
        closer = request
    else:
        closer = asyncio.create_task(item.aclose())
    await asyncio.sleep(0)
    closer.cancel()
    await asyncio.sleep(0)
    closer.cancel()
    assert not item.closed
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closer, 2)
    if request is not closer:
        with pytest.raises(AXLeaseError):
            await request
    assert gate.process is not None and gate.process.returncode is not None
    assert_reaped(item)


async def test_run_process_also_adopts_launch_before_returning_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LaunchGate(monkeypatch)
    task = asyncio.create_task(run_process([sys.executable, "-c", "import time; time.sleep(30)"]))
    await asyncio.wait_for(gate.ready.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert gate.process is not None and gate.process.returncode is not None


async def test_close_does_not_wait_for_an_inflight_drain_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = lease()
    draining = asyncio.Event()
    blocker = asyncio.Event()
    original = item._start

    async def start() -> None:
        await original()
        assert item._process is not None and item._process.stdin is not None

        async def drain() -> None:
            draining.set()
            await blocker.wait()

        monkeypatch.setattr(item._process.stdin, "drain", drain)

    monkeypatch.setattr(item, "_start", start)
    task = asyncio.create_task(item.request("action", {"ref_id": "r1", "action": "AXPress"}))
    await asyncio.wait_for(draining.wait(), 2)
    await asyncio.wait_for(asyncio.gather(item.aclose(), item.aclose()), 2)
    assert item.closed
    blocker.set()
    # An explicitly valid receipt wins over a concurrent close after dispatch.
    try:
        result = await task
        assert result["outcome"] == "accepted"
    except AXLeaseError as exc:
        assert exc.outcome == "unknown"
    assert_reaped(item)


async def test_cancel_during_success_cleanup_still_clears_request_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = lease()
    entered, release = asyncio.Event(), asyncio.Event()
    original = _settle_process

    async def settle(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await release.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(module, "_settle_process", settle)
    request = asyncio.create_task(item.request("action", {"ref_id": "r1", "action": "AXPress"}))
    await asyncio.wait_for(entered.wait(), 2)
    request.cancel()
    await asyncio.sleep(0)
    request.cancel()
    assert not request.done() and not item.closed
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, 2)
    assert_reaped(item)


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda data: data.update(v=True),
        lambda data: data.update(result=[]),
        lambda data: data.update(extra=True),
        lambda data: data.update(error={"code": "bad", "message": "bad", "outcome": []}),
        lambda data: data.update(
            error={"code": "bad", "message": "bad", "outcome": "not_started", "native_code": True}
        ),
    ],
)
async def test_protocol_parser_rejects_malformed_typed_fields(
    corrupt: Callable[[dict[str, Any]], None],
) -> None:
    item = lease()
    item._pending = asyncio.get_running_loop().create_future()
    item._request_id, item._dispatched = "r1", True
    data: dict[str, Any] = {"v": 1, "request_id": "r1", "result": {}}
    corrupt(data)
    if "error" in data:
        data.pop("result")
    item._response(json.dumps(data).encode("utf-8"))
    with pytest.raises(AXLeaseError) as caught:
        await item._pending
    assert caught.value.code == "protocol_error"
    await item.aclose()


async def test_missing_executable_is_a_safe_bounded_error(tmp_path: Path) -> None:
    item = AXHelperLease([str(tmp_path / "missing-python")], timeout=1, ttl=2, max_nodes=1)
    with pytest.raises(AXLeaseError) as caught:
        await item.request("snapshot", {})
    assert caught.value.code == "dependency_missing"
    assert str(tmp_path) not in str(caught.value)
    assert_reaped(item)


async def test_small_valid_responses_still_share_a_lifetime_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "MAX_SESSION_BYTES", 1024)
    item = lease()
    successes = 0
    try:
        with pytest.raises(AXLeaseError) as caught:
            for _ in range(module.MAX_REQUESTS):
                await item.request("snapshot", {})
                successes += 1
        assert successes > 0 and caught.value.code == "limit_exceeded"
        assert item._count < module.MAX_REQUESTS
    finally:
        await item.aclose()
    assert_reaped(item)


async def test_extra_response_cannot_become_the_next_commands_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = lease("duplicate")
    retired = asyncio.Event()
    original = item._retire

    def retire(error: AXLeaseError | None = None) -> None:
        original(error)
        retired.set()

    monkeypatch.setattr(item, "_retire", retire)
    try:
        try:
            await item.request("snapshot", {})
        except AXLeaseError as exc:
            assert exc.code == "protocol_error"
        await asyncio.wait_for(retired.wait(), 2)
        with pytest.raises(AXLeaseError) as caught:
            await item.request("targets", {})
        assert caught.value.code == "protocol_error" and item._count == 1
    finally:
        await item.aclose()
    assert_reaped(item)


async def test_concurrent_close_observers_share_cleanup_even_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = lease()
    await item.request("snapshot", {})
    entered, release = asyncio.Event(), asyncio.Event()
    original = _settle_process
    calls = 0

    async def settle(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(module, "_settle_process", settle)
    first = asyncio.create_task(item.aclose())
    second = asyncio.create_task(item.aclose())
    await asyncio.wait_for(entered.wait(), 2)
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    refused = asyncio.create_task(item.request("snapshot", {}))
    assert item.state == "closing" and not item.closed
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    with pytest.raises(AXLeaseError) as caught:
        await refused
    assert caught.value.code == "lease_closed" and calls == 1 and item._count == 1
    assert_reaped(item)


@pytest.mark.parametrize("reject_in_host", [False, True])
async def test_cleanup_failure_does_not_override_known_receipt_or_host_fence(
    monkeypatch: pytest.MonkeyPatch,
    reject_in_host: bool,
) -> None:
    class HostPaused(Exception):
        pass

    item = lease()
    expected = HostPaused("current task paused")
    original = _settle_process

    async def settle(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        raise OSError("sensitive native diagnostic")

    def guard() -> None:
        if reject_in_host:
            raise expected

    monkeypatch.setattr(module, "_settle_process", settle)
    if reject_in_host:
        with pytest.raises(HostPaused) as caught:
            await item.request("action", {"ref_id": "r1", "action": "AXPress"}, before_send=guard)
        assert caught.value is expected and item._count == 0
    else:
        result = await item.request("action", {"ref_id": "r1", "action": "AXPress"})
        assert result["outcome"] == "accepted" and result["effect_verified"] is False
    assert not item._in_request and item._pending is None
    assert item.state == "closing" and not item.closed
    assert item._process is not None and item._process.returncode is not None
    assert item._cleanup is not None and item._cleanup.done()
    with pytest.raises(AXLeaseError) as cleanup_error:
        await item.aclose()
    assert cleanup_error.value.code == "backend_unavailable"
    assert "sensitive" not in str(cleanup_error.value)


async def test_idle_ttl_retires_even_a_lazy_unstarted_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = lease(ttl=0.02)
    retired = asyncio.Event()
    original = item._retire

    def retire(error: AXLeaseError | None = None) -> None:
        original(error)
        retired.set()

    monkeypatch.setattr(item, "_retire", retire)
    await asyncio.wait_for(retired.wait(), 2)
    assert asyncio.get_running_loop().time() >= item.expires_at
    await item.aclose()
    assert item._starting is None
    assert_reaped(item)


@pytest.mark.parametrize("stage", ["write", "drain"])
async def test_transport_failure_after_write_attempt_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    item = lease()
    original = item._start

    async def start() -> None:
        await original()
        assert item._process is not None and item._process.stdin is not None

        def write(_: bytes) -> None:
            raise RuntimeError("sensitive closed transport detail")

        async def drain() -> None:
            raise ConnectionAbortedError("sensitive closed pipe detail")

        monkeypatch.setattr(item._process.stdin, stage, write if stage == "write" else drain)

    monkeypatch.setattr(item, "_start", start)
    with pytest.raises(AXLeaseError) as caught:
        await item.request("action", {"ref_id": "r1", "action": "AXPress"})
    assert caught.value.outcome == "unknown" and caught.value.code == "action_unknown_outcome"
    assert "sensitive" not in str(caught.value)
    assert item._count == 1
    assert_reaped(item)


@pytest.mark.parametrize(
    ("command", "code", "outcome", "expected_code", "expected_outcome"),
    [
        ("snapshot", "backend_unavailable", "not_started", "backend_unavailable", "not_started"),
        ("snapshot", "node_budget", "not_started", "node_budget", "not_started"),
        ("action", "output_budget", "not_started", "action_unknown_outcome", "unknown"),
        ("action", "action_unknown_outcome", "unknown", "action_unknown_outcome", "unknown"),
        ("snapshot", "action_unknown_outcome", "unknown", "protocol_error", "not_started"),
        ("snapshot", "action_unknown_outcome", "not_started", "protocol_error", "not_started"),
        ("action", "action_unknown_outcome", "not_started", "action_unknown_outcome", "unknown"),
        ("snapshot", "element_disabled", "unknown", "protocol_error", "not_started"),
        ("action", "element_disabled", "unknown", "action_unknown_outcome", "unknown"),
        ("snapshot", "future_unknown_code", "not_started", "protocol_error", "not_started"),
        ("action", "future_unknown_code", "not_started", "action_unknown_outcome", "unknown"),
    ],
)
async def test_native_error_codes_cannot_misrepresent_dispatch_outcome(
    command: str,
    code: str,
    outcome: str,
    expected_code: str,
    expected_outcome: str,
) -> None:
    item = lease(f"error:{code}:{outcome}")
    payload = {} if command == "snapshot" else {"ref_id": "r1", "action": "AXPress"}
    with pytest.raises(AXLeaseError) as caught:
        await item.request(command, payload)
    assert caught.value.code == expected_code and caught.value.outcome == expected_outcome
    assert item._count == 1
    assert_reaped(item)
