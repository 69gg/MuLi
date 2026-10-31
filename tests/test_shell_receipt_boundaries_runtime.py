"""Real call authorization, close ownership and session deletion around evidence."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import FakeClient
from test_shell_receipts import closed_commands
from test_shell_session_runtime import ShellClient, ShellDialogue, ShellScript, shell_runtime
from test_task_briefs import attach_services
from test_teammate_attachment_runtime import call, tool_result

from muli.config import ConfigFile
from muli.permissions import Mode
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app
from muli.shell_receipts import RECEIPT_KIND, receipt_key
from muli.shell_sessions import ShellSessionError, ShellSessions
from muli.task_control import OwnerControlRequest
from muli.tools import Tool, parameters


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_http(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("本文件只使用本机 Runtime 与显式 ASGITransport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_http)


def save_fixture(runtime: Runtime, tmp_path: Path, session_id: str) -> str:
    # Authorization/lifecycle tests use a small storage fixture. The separate
    # first journey runs the command, control proof, pipes and reopen for real.
    with closed_commands(tmp_path, [(b"retained evidence", b"")], session_id=session_id) as manager:
        runtime.shell_receipts.archive(manager, max_records=3, output_limit=6000)
        return next(iter(manager._commands))


def result(runtime: Runtime, session_id: str, identifier: str) -> dict[str, Any]:
    message = next(
        message
        for message in runtime.store.messages(session_id)
        if message.get("tool_call_id") == identifier
    )
    return json.loads(message["content"])


async def test_current_owner_reads_old_evidence_but_real_delegated_session_cannot(
    tmp_path: Path,
) -> None:
    runtime = await shell_runtime(tmp_path)
    session_id = runtime.main_session()["id"]
    command_id = save_fixture(runtime, tmp_path, session_id)
    worker_id = ""
    worker_command = ""

    def worker_script() -> ShellScript:
        nonlocal worker_command
        worker_session = runtime.team.get("receipt-reader")["session_id"]
        worker_command = save_fixture(runtime, tmp_path, worker_session)
        yield call("worker-history", "shell_history")
        yield call("foreign-receipt", "shell_read", command_id=command_id)
        return "已按本会话权限核查。"

    def main_script() -> ShellScript:
        nonlocal worker_id
        spawned = yield call(
            "spawn-receipt-reader", "spawn_agent", task_name="receipt-reader", prompt="只读原编号"
        )
        worker_id = spawned["id"]
        yield call("wait-receipt-reader", "wait_agents", targets=[worker_id], timeout=2)
        yield call("owner-history", "shell_history")
        yield call("foreign-history", "shell_history", session_id=worker_id)
        yield call("owner-receipt", "shell_read", command_id=command_id)
        return "权限核查结束。"

    main, worker = ShellDialogue(main_script()), ShellDialogue(worker_script())

    def factory(config: ModelConfig) -> FakeClient:
        return ShellClient(worker if config.model == "worker" else main)

    runtime.client_factory = factory
    try:
        started = await runtime.submit("委派队友按原编号核查，再由原会话读取。", mode="yolo")
        async with asyncio.timeout(4):
            await runtime.wait(started["run_id"])
        agent = runtime.team.get(worker_id)
        assert agent["status"] == "completed"
        assert runtime.store.get("session", worker_id)["principal"] == "owner"
        worker_history = result(runtime, worker_id, "worker-history")
        owner_history = result(runtime, session_id, "owner-history")
        assert worker_history["session_id"] == worker_id
        assert [item["command_id"] for item in worker_history["items"]] == [worker_command]
        assert [item["command_id"] for item in owner_history["items"]] == [command_id]
        assert "error" in result(runtime, session_id, "foreign-history")
        assert result(runtime, worker_id, "foreign-receipt")["code"] == "not_found"
        owner = result(runtime, session_id, "owner-receipt")
        assert owner["receipt_source"] == "stored" and owner["output"] == "retained evidence"
        assert runtime.shells.live_sessions() == ()
    finally:
        await runtime.close()


@pytest.mark.parametrize("denial", ["non_owner", "disabled"])
@pytest.mark.parametrize("name", ["shell_read", "shell_history"])
async def test_stored_evidence_does_not_bypass_current_principal_or_tool_enablement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denial: str, name: str
) -> None:
    runtime = await shell_runtime(tmp_path)
    session_id = runtime.main_session()["id"]
    if denial == "non_owner":
        session_id = runtime.new_session(mode="yolo")["id"]
    command_id = save_fixture(runtime, tmp_path, session_id)
    lookups = 0

    def forbidden_lookup(*args: Any, **kwargs: Any) -> NoReturn:
        nonlocal lookups
        lookups += 1
        raise AssertionError("当前调用拒绝时不得查询历史回执")

    if denial == "disabled":
        runtime.config.update({"disabled_tools": [name]})
        await runtime.reconfigure()
    monkeypatch.setattr(
        runtime.shell_receipts, "read" if name == "shell_read" else "history", forbidden_lookup
    )
    rounds = 0

    class Reader(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                if denial == "disabled":
                    assert name not in {item["function"]["name"] for item in tools}
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "denied-receipt",
                            name,
                            **({"command_id": command_id} if name == "shell_read" else {}),
                        )
                    ]
                )
            else:
                tool_result(messages, "denied-receipt")
                turn = ModelTurn(content="权限核查结束。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _config: Reader([], [])
    try:
        started = (
            await runtime.start(session_id, "只读保存的回执。", mode="yolo")
            if denial == "non_owner"
            else await runtime.submit("只读保存的回执。", mode="yolo")
        )
        async with asyncio.timeout(4):
            await runtime.wait(started["run_id"])
        denied = result(runtime, session_id, "denied-receipt")
        assert "error" in denied and denied.get("receipt_source") is None
        assert lookups == 0 and runtime.shells.live_sessions() == ()
    finally:
        await runtime.close()


@pytest.mark.parametrize("mode", ["ask", "auto", "yolo"])
async def test_shell_history_uses_current_permission_mode_and_returns_only_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Mode
) -> None:
    runtime = await shell_runtime(tmp_path)
    session_id = runtime.main_session()["id"]
    command_id = save_fixture(runtime, tmp_path, session_id)
    pending = asyncio.Event()
    lookups = 0
    reviews: list[tuple[str, str]] = []
    original_notify = runtime.bus.notify
    original_history = runtime.shell_receipts.history

    async def observe_pending() -> None:
        if any(
            runtime.permissions.pending_for(session_id, run_id)
            for run_id in runtime.run_ids.values()
        ):
            pending.set()
        await original_notify()

    async def review(name: str, arguments: dict[str, Any], risk: str) -> tuple[bool, str]:
        reviews.append((name, risk))
        return True, "允许当前会话显式查询回执目录"

    def lookup(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal lookups
        lookups += 1
        assert kwargs["limit"] == runtime.config.settings.limits.max_search_results
        return original_history(*args, **kwargs)

    monkeypatch.setattr(runtime.bus, "notify", observe_pending)
    monkeypatch.setattr(runtime.permissions, "review", review)
    monkeypatch.setattr(runtime.shell_receipts, "history", lookup)

    def script() -> ShellScript:
        yield call(
            "current-history",
            "shell_history",
            limit=runtime.config.settings.limits.max_search_results + 1,
        )
        return "目录核查完成。"

    dialogue = ShellDialogue(script())
    runtime.client_factory = lambda _config: ShellClient(dialogue)
    try:
        started = await runtime.submit("仅列本会话正常关闭后保留的回执。", mode=mode)
        if mode == "ask":
            await asyncio.wait_for(pending.wait(), 2)
            assert lookups == 0
            (approval,) = runtime.permissions.pending_for(session_id, started["run_id"])
            stored = runtime.store.get("approval", approval)
            assert stored["tool"] == "shell_history" and stored["risk"] == "read"
            await runtime.permissions.resolve(approval, "approve")
        async with asyncio.timeout(4):
            await runtime.wait(started["run_id"])
        page = result(runtime, session_id, "current-history")
        assert [item["command_id"] for item in page["items"]] == [command_id]
        assert "retained evidence" not in json.dumps(page)
        assert lookups == 1 and runtime.shells.live_sessions() == ()
        assert reviews == ([("shell_history", "read")] if mode == "auto" else [])
    finally:
        await runtime.close()


@pytest.mark.parametrize("error", ["invalid_cursor", "output_budget", "unknown", "invalidated"])
async def test_only_exact_live_not_found_can_fall_back_to_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    runtime = await shell_runtime(tmp_path)
    command_id = save_fixture(runtime, tmp_path, runtime.main_session()["id"])
    lookups = 0

    async def live_error(*args: Any, **kwargs: Any) -> NoReturn:
        raise ShellSessionError(error, "injected live read error")

    def forbidden_lookup(*args: Any, **kwargs: Any) -> NoReturn:
        nonlocal lookups
        lookups += 1
        raise AssertionError("非 not_found 不能回退")

    monkeypatch.setattr(runtime.shells, "read", live_error)
    monkeypatch.setattr(runtime.shell_receipts, "read", forbidden_lookup)

    def script() -> ShellScript:
        yield call("live-read-error", "shell_read", command_id=command_id)
        return "已收到读取结果。"

    dialogue = ShellDialogue(script())
    runtime.client_factory = lambda _config: ShellClient(dialogue)
    try:
        started = await runtime.submit("只读取原命令，不重试其它入口。", mode="yolo")
        async with asyncio.timeout(4):
            await runtime.wait(started["run_id"])
        assert result(runtime, started["session_id"], "live-read-error")["code"] == error
        assert lookups == 0 and runtime.shells.live_sessions() == ()
    finally:
        await runtime.close()


@pytest.mark.parametrize("stale", ["retired_run", "old_configuration"])
async def test_retired_execution_and_old_closure_cannot_read_but_current_call_can(
    tmp_path: Path, stale: str
) -> None:
    runtime = await shell_runtime(tmp_path)
    session_id = runtime.main_session()["id"]
    command_id = save_fixture(runtime, tmp_path, session_id)
    runtime.client_factory = lambda _config: FakeClient([ModelTurn(content="原回合结束。")], [])
    original = await runtime.submit("完成一个普通回合。", mode="yolo")
    async with asyncio.timeout(4):
        await runtime.wait(original["run_id"])
    old_handler = runtime.tools["shell_read"].handler
    if stale == "old_configuration":
        await runtime.reconfigure()

    async def probe(arguments: dict[str, Any]) -> Any:
        token = None
        if stale == "retired_run":
            token = runtime._tool_execution.set((session_id, original["run_id"]))
        try:
            return await old_handler({"command_id": command_id})
        finally:
            if token is not None:
                runtime._tool_execution.reset(token)

    runtime.add_tool_provider(
        lambda _: [Tool("old_shell_probe", "核对退休调用拒绝", parameters({}), probe, "read")]
    )

    def script() -> ShellScript:
        yield call("stale-receipt", "old_shell_probe")
        yield call("current-receipt", "shell_read", command_id=command_id)
        return "已核对旧调用与当前调用。"

    dialogue = ShellDialogue(script())
    runtime.client_factory = lambda _config: ShellClient(dialogue)
    try:
        started = await runtime.submit("只核查当前调用资格。", mode="yolo")
        async with asyncio.timeout(4):
            await runtime.wait(started["run_id"])
        assert "error" in result(runtime, session_id, "stale-receipt")
        current = result(runtime, session_id, "current-receipt")
        assert current["receipt_source"] == "stored" and current["output"] == "retained evidence"
        assert runtime.shells.live_sessions() == ()
    finally:
        await runtime.close()


@pytest.mark.parametrize("name", ["shell_read", "shell_history"])
async def test_stored_read_rechecks_current_qualification_before_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    runtime = await shell_runtime(tmp_path)
    session_id = runtime.main_session()["id"]
    command_id = save_fixture(runtime, tmp_path, session_id)
    method = "read" if name == "shell_read" else "history"
    original = getattr(runtime.shell_receipts, method)

    def revoke_after_lookup(*args: Any, **kwargs: Any) -> dict[str, Any]:
        page = original(*args, **kwargs)
        runtime.config.update({"disabled_tools": [name]})
        return page

    monkeypatch.setattr(runtime.shell_receipts, method, revoke_after_lookup)

    def script() -> ShellScript:
        yield call(
            "revoked-receipt", name, **({"command_id": command_id} if name == "shell_read" else {})
        )
        return "当前资格核查结束。"

    dialogue = ShellDialogue(script())
    runtime.client_factory = lambda _config: ShellClient(dialogue)
    try:
        started = await runtime.submit("只按当前资格核查已保留回执。", mode="yolo")
        async with asyncio.timeout(4):
            await runtime.wait(started["run_id"])
        denied = result(runtime, session_id, "revoked-receipt")
        assert "error" in denied and denied.get("receipt_source") is None
        assert "retained evidence" not in json.dumps(denied)
        assert runtime.shells.live_sessions() == ()
    finally:
        await runtime.close()


async def test_task_pause_does_not_revoke_the_existing_read_boundary_for_stored_evidence(
    tmp_path: Path,
) -> None:
    runtime = await shell_runtime(tmp_path)
    services = attach_services(runtime, tmp_path)
    item = await services.commitments.save(
        "只读核查旧回执", brief={"goal": "只读核查", "constraints": []}
    )
    entered, release = asyncio.Event(), asyncio.Event()
    command_id = ""

    class Reader(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            entered.set()
            await release.wait()
            yield StreamEvent(
                "done",
                turn=ModelTurn(
                    tool_calls=[call("paused-read", "shell_read", command_id=command_id)]
                ),
            )

    runtime.client_factory = lambda _config: Reader([], [])
    try:
        child = await runtime.team.spawn("暂停时只读回执", "只读原记录", commitment_id=item["id"])
        await asyncio.wait_for(entered.wait(), 2)
        worker_id = child["id"]
        assert runtime.store.get("session", worker_id)["principal"] == "owner"
        command_id = save_fixture(runtime, tmp_path, worker_id)
        task = runtime.team._active[worker_id]
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause-read"))
        assert not task.done() and task.cancelling() == 0
        release.set()
        await asyncio.wait_for(asyncio.shield(task), 4)
        page = result(runtime, worker_id, "paused-read")
        assert page["receipt_source"] == "stored" and page["output"] == "retained evidence"
        assert page["state"] == "completed" and page["shell_pid"] is None
        assert runtime.team.get(worker_id)["status"] == "paused"
        assert runtime.shells.live_sessions() == ()
    finally:
        release.set()
        await runtime.close()


async def test_archive_failure_still_closes_settled_store_and_releases_runtime_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await shell_runtime(tmp_path)
    released = False
    original_release = runtime._release_writer

    def observe_release() -> None:
        nonlocal released
        original_release()
        released = True

    def fail_archive(shells: ShellSessions, *, max_records: int, output_limit: int) -> NoReturn:
        assert shells._closed and shells.live_sessions() == ()
        raise RuntimeError("injected archive failure after shell settlement")

    monkeypatch.setattr(runtime, "_release_writer", observe_release)
    monkeypatch.setattr(runtime.shell_receipts, "archive", fail_archive)
    with pytest.raises(RuntimeError, match="injected archive failure"):
        await runtime.close()
    assert released
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        runtime.store.db.execute("SELECT 1")
    reopened = Runtime(ConfigFile(runtime.config.path), runtime.data_dir)
    try:
        assert reopened.main_session()["id"]
        assert reopened.shells.live_sessions() == ()
    finally:
        await reopened.close()


async def test_unsettled_shell_failure_keeps_store_and_writer_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await shell_runtime(tmp_path)
    original_close = runtime.shells.aclose
    original_release = runtime._release_writer
    released = False

    def observe_release() -> None:
        nonlocal released
        released = True

    async def fail_close() -> NoReturn:
        raise ShellSessionError("cleanup_unknown", "injected shell settlement failure")

    monkeypatch.setattr(runtime, "_release_writer", observe_release)
    monkeypatch.setattr(runtime.shells, "aclose", fail_close)
    try:
        with pytest.raises(ShellSessionError) as caught:
            await runtime.close()
        assert caught.value.code == "cleanup_unknown"
        assert not released
        assert runtime.store.db.execute("SELECT 1").fetchone()[0] == 1
    finally:
        await original_close()
        runtime.store.close()
        original_release()


async def test_api_session_delete_is_atomic_and_normal_reopen_does_not_revive_receipts(
    tmp_path: Path,
) -> None:
    runtime = await shell_runtime(tmp_path)
    session_id = runtime.new_session()["id"]
    command_id = save_fixture(runtime, tmp_path, session_id)
    runtime.store.add_message(session_id, "user", "删除失败时应保留的历史")
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                base_url="http://localhost:8765",
            ) as web,
        ):
            with runtime.store.db:
                runtime.store.db.execute(
                    "CREATE TEMP TRIGGER fail_session_delete BEFORE DELETE ON objects "
                    "WHEN OLD.kind='session' BEGIN SELECT RAISE(ABORT,'injected delete failure'); END"
                )
            failed = await web.delete(f"/api/sessions/{session_id}")
            assert failed.status_code == 500
            assert runtime.store.get("session", session_id) is not None
            assert runtime.store.get(RECEIPT_KIND, receipt_key(session_id, command_id)) is not None
            assert runtime.store.messages(session_id)[0]["content"] == "删除失败时应保留的历史"
            with runtime.store.db:
                runtime.store.db.execute("DROP TRIGGER fail_session_delete")
            deleted = await web.delete(f"/api/sessions/{session_id}")
            assert deleted.status_code == 200 and deleted.json()["deleted"] is True
            assert runtime.store.get("session", session_id) is None
            assert runtime.store.get(RECEIPT_KIND, receipt_key(session_id, command_id)) is None
            assert (
                await web.delete(f"/api/sessions/{runtime.main_session()['id']}")
            ).status_code == 409
    finally:
        await runtime.close()
    reopened = Runtime(ConfigFile(runtime.config.path), runtime.data_dir)
    try:
        assert reopened.store.get("session", session_id) is None
        assert reopened.store.get(RECEIPT_KIND, receipt_key(session_id, command_id)) is None
        with pytest.raises(ShellSessionError) as missing_directory:
            reopened.shell_receipts.history(
                session_id, limit=20, output_limit=6000, receipt_limit=6000
            )
        assert missing_directory.value.code == "not_found"
        assert reopened.shells.live_sessions() == ()
    finally:
        await reopened.close()
