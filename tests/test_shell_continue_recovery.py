"""Reopen real continuation crash snapshots with this build and a frozen old build."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_shell_continue_runtime import (
    RegisteredShellWait,
    registered_shell_wait,
    wait_team_state,
)

from muli.providers import Message, ModelTurn
from muli.team import ShellContinuation

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="真实受控命令崩溃快照首片限 POSIX；旧消费者不依赖本机源码路径"
)

# -I removes the calling checkout and PYTHONPATH. The selected source tree is
# inserted before importing *any* MuLi module; every loaded MuLi module is checked.
# No test helpers or candidate classes cross this subprocess boundary.
_CONSUMER = r"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, NoReturn

source = Path(sys.argv[1]).resolve(strict=True)
config_path = Path(sys.argv[2]).resolve(strict=True)
data_dir = Path(sys.argv[3]).resolve(strict=True)
target, fresh_id, consumer_kind = sys.argv[4:7]
assert Path.cwd().resolve().is_relative_to(config_path.parent)
assert Path(os.environ["MULI_HOME"]).resolve().is_relative_to(config_path.parent)
sys.path.insert(0, str(source / "src"))

import httpx
import muli.config as config_module
import muli.runtime as runtime_module
import muli.shell_sessions as shell_module
import muli.store as store_module
import muli.team as team_module
from muli.attachments import Attachments
from muli.config import ConfigFile
from muli.local_services import LocalServices
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime

attempts: dict[str, int] = {
    "model_factory": 0, "model_stream": 0, "model_complete": 0,
    "shell": 0, "process": 0, "network": 0,
}

def refuse_http(*args: Any, **kwargs: Any) -> NoReturn:
    attempts["network"] += 1
    raise AssertionError("recovery consumer must not access HTTP")

async def refuse_async_http(*args: Any, **kwargs: Any) -> NoReturn:
    refuse_http(*args, **kwargs)

def refuse_connect(*args: Any, **kwargs: Any) -> NoReturn:
    attempts["network"] += 1
    raise AssertionError("recovery consumer must not connect a socket")

async def refuse_shell(*args: Any, **kwargs: Any) -> NoReturn:
    attempts["shell"] += 1
    raise AssertionError("recovery must not replay a command")

async def refuse_process(*args: Any, **kwargs: Any) -> NoReturn:
    attempts["process"] += 1
    raise AssertionError("recovery consumer must not start a process")

httpx.HTTPTransport.handle_request = refuse_http
httpx.AsyncHTTPTransport.handle_async_request = refuse_async_http
shell_module.ShellSessions.execute = refuse_shell
asyncio.create_subprocess_exec = refuse_process
asyncio.create_subprocess_shell = refuse_process

def module_paths() -> dict[str, str]:
    paths: dict[str, str] = {}
    for name, module in tuple(sys.modules.items()):
        if name != "muli" and not name.startswith("muli."):
            continue
        filename = getattr(module, "__file__", None)
        if filename is None:
            spec = getattr(module, "__spec__", None)
            locations = getattr(spec, "submodule_search_locations", None)
            assert spec is not None and spec.origin is None and locations, (
                "missing module source", name,
            )
            for location in locations:
                path = Path(location).resolve(strict=True)
                assert path.is_dir() and path.is_relative_to(source / "src"), (
                    "mixed MuLi namespace source", name, str(path),
                )
            continue
        path = Path(filename).resolve(strict=True)
        assert path.is_relative_to(source / "src"), ("mixed MuLi source", name, str(path))
        if name in {"muli.config", "muli.runtime", "muli.team", "muli.store", "muli.shell_sessions"}:
            paths[name] = str(path)
    assert len(paths) == 5, paths
    return paths

module_paths()
has_continuation = hasattr(team_module.TeamManager, "request_shell_continuation")
assert has_continuation == (consumer_kind == "candidate"), "previous source must really predate continuation"
assert runtime_module.Runtime is Runtime
assert store_module.Store.__module__ == "muli.store"
assert config_module.default_home() == Path(os.environ["MULI_HOME"])

runtime: Runtime
fresh_payload: dict[str, Any] | None = None

class ExactInputClient:
    async def __aenter__(self) -> ExactInputClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        attempts["model_stream"] += 1
        assert attempts["model_stream"] == 1 and fresh_id and fresh_payload is not None
        archived = runtime.store.get("message", fresh_id)
        assert archived is not None and archived["delivery_status"] == "delivered"
        assert archived["content"] == fresh_payload["content"]
        assert archived["source"] == fresh_payload["source"]
        assert archived["principal"] == fresh_payload["principal"]
        assert archived["source"].startswith("team:")
        assert any(message.role == "user" and message.content == fresh_payload["content"] for message in messages)
        yield StreamEvent("done", turn=ModelTurn(content="EXACT_FRESH_INPUT_HANDLED_ONCE"))

    async def complete(
        self, messages: list[Message], tools: Any = (),
        *, attempt_guard: Callable[[], None] | None = None,
    ) -> ModelTurn:
        attempts["model_complete"] += 1
        raise AssertionError("recovery must not request another model path")

def client_factory(profile: ModelConfig) -> ExactInputClient:
    attempts["model_factory"] += 1
    assert fresh_id and profile.model == "worker", "recovery must not call a model without the exact new input"
    assert attempts["model_factory"] == 1
    return ExactInputClient()

def ingress(session_id: str) -> list[dict[str, Any]]:
    return [
        {**dict(row), "payload": json.loads(row["payload"])}
        for row in runtime.store.db.execute(
            "SELECT seq,id,state,delivered_seq,run_id,payload FROM ingress WHERE session_id=? ORDER BY seq",
            (session_id,),
        )
    ]

async def main() -> dict[str, Any]:
    global runtime, fresh_payload
    # The real event loop and its socketpair already exist. Leave socket
    # construction intact; only attempted business connections are rejected.
    socket.socket.connect = refuse_connect
    socket.socket.connect_ex = refuse_connect
    runtime = Runtime(ConfigFile(config_path), data_dir, client_factory=client_factory)
    LocalServices(runtime, Attachments(runtime.store, config_path.parent / "attachments", runtime.config))
    assert runtime.config.path.resolve() == config_path
    assert runtime.data_dir.resolve() == data_dir
    if fresh_id:
        rows = [row for row in ingress(target) if row["id"] == fresh_id]
        assert len(rows) == 1 and rows[0]["state"] == "pending"
        fresh_payload = rows[0]["payload"]
        # Keep the normal final report pending for its parent. The exact worker
        # input still completes through the real claim/prepare/report path.
        runtime._main_wake_deferred = True
    try:
        async with asyncio.timeout(8):
            await runtime.startup()
            while runtime.team._tasks or runtime.tasks:
                tasks = {*runtime.team._tasks, *runtime.tasks.values()}
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
        assert not runtime.tasks and not runtime.team._active and not runtime.team._tasks
        assert attempts["shell"] == attempts["process"] == attempts["network"] == 0, attempts
        assert attempts["model_complete"] == 0, attempts
        expected_models = int(bool(fresh_id))
        assert attempts["model_factory"] == attempts["model_stream"] == expected_models, attempts
        agent = runtime.team.get(target)
        messages = runtime.store.messages(target)
        reports = [
            json.loads(row[0])
            for row in runtime.store.db.execute(
                "SELECT data FROM objects WHERE kind='team_report' AND json_extract(data,'$.agent_id')=? ORDER BY rowid",
                (target,),
            )
        ]
        return {
            "modules": module_paths(), "continuation_api": has_continuation,
            "agent": agent,
            "session": runtime.store.get("session", target),
            "runs": [
                json.loads(row[0])
                for row in runtime.store.db.execute(
                    "SELECT data FROM objects WHERE kind='run' AND json_extract(data,'$.session_id')=? ORDER BY rowid",
                    (target,),
                )
            ],
            "ingress": ingress(target),
            "facts": [message for message in messages if message.get("source") == "shell-continuation"],
            "message_ids": [message["id"] for message in messages],
            "fresh_messages": [message for message in messages if message["id"] == fresh_id],
            "reports": reports,
            "parent_ingress": ingress(agent["parent_id"]),
            "pending": runtime.store.pending_inputs(target),
            "live_shells": len(runtime.shells.live_sessions()), "attempts": attempts,
        }
    finally:
        await runtime.close()

print(json.dumps(asyncio.run(main()), ensure_ascii=False, separators=(",", ":")))
"""


@dataclass(frozen=True)
class CrashSnapshot:
    name: str
    config: Path
    data: Path
    agent: dict[str, Any]
    session: dict[str, Any]
    runs: list[dict[str, Any]]
    ingress: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    message_ids: list[str]
    counter: Path
    fresh_input_id: str = ""


@pytest.fixture(params=["candidate", "previous"])
def consumer_source_root(request: pytest.FixtureRequest) -> tuple[str, Path]:
    candidate = Path(__file__).resolve().parents[1]
    if request.param == "candidate":
        return "candidate", candidate
    value = os.environ.get("MULI_PREVIOUS_SOURCE_ROOT")
    if not value:
        pytest.fail(
            "真正旧消费者验证需要显式 MULI_PREVIOUS_SOURCE_ROOT 指向精确冻结 U checkout；不能跳过",
            pytrace=False,
        )
    previous = Path(value).resolve(strict=True)
    assert previous != candidate, "旧消费者不能使用候选源码树"
    for module in ("runtime", "team", "store", "shell_sessions", "config"):
        assert (previous / "src" / "muli" / f"{module}.py").is_file(), previous
    return "previous", previous


def capture_snapshot(
    state: RegisteredShellWait, directory: Path, name: str, *, fresh_input_id: str = ""
) -> CrashSnapshot:
    runtime = state.runtime
    destination = directory / name
    data = destination / "data"
    data.mkdir(parents=True)
    config = destination / "config.json"
    shutil.copyfile(runtime.config.path, config)
    # All callers are synchronous, after the relevant production commit and
    # before returning to the event loop. Async cleanup cannot hide this window.
    assert not runtime.store.db.in_transaction
    with closing(sqlite3.connect(data / "muli.sqlite3")) as copy:
        runtime.store.db.backup(copy)
    agent = runtime.team.get(state.target)
    session = runtime.store.get("session", state.target)
    assert session is not None
    messages = runtime.store.messages(state.target)
    return CrashSnapshot(
        name=name,
        config=config,
        data=data,
        agent=agent,
        session=session,
        runs=[
            json.loads(row[0])
            for row in runtime.store.db.execute(
                "SELECT data FROM objects WHERE kind='run' "
                "AND json_extract(data,'$.session_id')=? ORDER BY rowid",
                (state.target,),
            )
        ],
        ingress=[
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in runtime.store.db.execute(
                "SELECT seq,id,state,delivered_seq,run_id,payload FROM ingress "
                "WHERE session_id=? ORDER BY seq",
                (state.target,),
            )
        ],
        facts=[message for message in messages if message.get("source") == "shell-continuation"],
        message_ids=[message["id"] for message in messages],
        counter=state.counter,
        fresh_input_id=fresh_input_id,
    )


async def consume_snapshot(
    snapshot: CrashSnapshot, consumer: tuple[str, Path], directory: Path
) -> dict[str, Any]:
    kind, source = consumer
    root = directory / f"{kind}-{snapshot.name}"
    data = root / "data"
    data.mkdir(parents=True)
    config = root / "config.json"
    shutil.copyfile(snapshot.config, config)
    shutil.copyfile(snapshot.data / "muli.sqlite3", data / "muli.sqlite3")
    cwd, home = root / "cwd", root / "home"
    cwd.mkdir()
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("MULI_")}
    env.pop("PYTHONPATH", None)
    env["MULI_HOME"] = str(home)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        _CONSUMER,
        str(source),
        str(config),
        str(data),
        snapshot.agent["id"],
        snapshot.fresh_input_id,
        kind,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(25):
            stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    assert process.returncode == 0, (
        kind,
        snapshot.name,
        stderr.decode("utf-8", errors="replace")[-4000:],
        stdout.decode("utf-8", errors="replace")[-1000:],
    )
    result: dict[str, Any] = json.loads(stdout)
    assert result["continuation_api"] is (kind == "candidate")
    for module, filename in result["modules"].items():
        assert Path(filename).resolve().is_relative_to(source / "src"), (module, filename)
    assert result["live_shells"] == 0 and result["pending"] == 0
    assert snapshot.counter.read_text(encoding="utf-8").splitlines() == ["once"]
    return result


@pytest.fixture
async def crash_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[CrashSnapshot]:
    snapshots: dict[str, CrashSnapshot] = {}
    snapshot_root = tmp_path / "snapshots"

    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("快照前置只允许临时本机 shell 控制通道")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)

    def configure(state: RegisteredShellWait) -> None:
        runtime = state.runtime
        original_schedule = runtime.team._schedule_shell_continuations
        original_put = runtime.store.put
        original_launch = runtime.team._launch_agent

        def schedule() -> None:
            record = runtime.team._shell_continuations.get(state.target)
            ready = bool(
                state.original
                and record is not None
                and record.finished is not None
                and record.finished.state == "completed"
                and not record.reserved
                and state.target not in runtime.team._active
                and state.target not in runtime.tasks
            )
            if ready and "ready" not in snapshots:
                snapshots["ready"] = capture_snapshot(state, snapshot_root, "ready")
            original_schedule()
            if ready and record is not None and record.reserved and "reserved" not in snapshots:
                snapshots["reserved"] = capture_snapshot(state, snapshot_root, "reserved")

        def put(kind: str, item: dict[str, Any]) -> dict[str, Any]:
            result = original_put(kind, item)
            if state.original and item.get("id") == state.target and kind == "session":
                if item.get("status") == "running" and "session-written" not in snapshots:
                    snapshots["session-written"] = capture_snapshot(
                        state, snapshot_root, "session-written"
                    )
            if state.original and kind == "run" and item.get("session_id") == state.target:
                if item.get("status") == "queued" and "run-queued" not in snapshots:
                    snapshots["run-queued"] = capture_snapshot(state, snapshot_root, "run-queued")
            return result

        def launch(
            agent: dict[str, Any], shell_continuation: ShellContinuation | None = None
        ) -> str:
            run_id = original_launch(agent, shell_continuation)
            if shell_continuation is not None and "fact-committed" not in snapshots:
                snapshots["fact-committed"] = capture_snapshot(
                    state, snapshot_root, "fact-committed"
                )
            return run_id

        monkeypatch.setattr(runtime.team, "_schedule_shell_continuations", schedule)
        monkeypatch.setattr(runtime.store, "put", put)
        monkeypatch.setattr(runtime.team, "_launch_agent", launch)

    async def requested(state: RegisteredShellWait, messages: list[Message]) -> ModelTurn:
        assert messages and state.runtime.team.get(state.target)["generation"] == 2
        assert "model-requested" not in snapshots
        snapshots["model-requested"] = capture_snapshot(state, snapshot_root, "model-requested")
        return ModelTurn(content="已收到原命令完成事实，未重新执行。")

    async with registered_shell_wait(
        tmp_path / "producer", configure=configure, resumed=requested
    ) as state:
        snapshots["waiting"] = capture_snapshot(state, snapshot_root, "waiting")
        state.release.write_text("finish once", encoding="utf-8")
        await wait_team_state(
            state.runtime,
            state.target,
            lambda agent: (
                agent["status"] == "completed"
                and agent["generation"] == 2
                and state.target not in state.runtime.tasks
                and state.target not in state.runtime.team._active
            ),
        )
        assert state.counter.read_text(encoding="utf-8").splitlines() == ["once"]
    order = (
        "waiting",
        "ready",
        "reserved",
        "session-written",
        "run-queued",
        "fact-committed",
        "model-requested",
    )
    assert set(snapshots) == set(order), tuple(snapshots)
    for name in order:
        snapshot = snapshots[name]
        before_launch = name in {"waiting", "ready", "reserved", "session-written", "run-queued"}
        assert snapshot.agent["status"] == ("completed" if before_launch else "running"), name
        assert snapshot.agent["generation"] == (1 if before_launch else 2), name
        assert snapshot.session["status"] == (
            "idle" if name in {"waiting", "ready", "reserved"} else "running"
        ), name
        assert not any(row["state"] == "pending" for row in snapshot.ingress), name
        assert not any(row["payload"]["source"] == "shell-continuation" for row in snapshot.ingress)
        assert len(snapshot.facts) == (0 if before_launch else 1), name
        if snapshot.facts:
            fact = snapshot.facts[0]
            assert fact["delivery_status"] == "delivered"
            assert fact["run_id"] == snapshot.agent["run_id"]
            assert fact["principal"] != "owner" and fact["route"] is None
        assert len(snapshot.runs) == (1 if name in order[:4] else 2), name
        assert snapshot.runs[0]["status"] == "completed", name
        if len(snapshot.runs) == 2:
            assert snapshot.runs[-1]["status"] == (
                "running" if name == "model-requested" else "queued"
            ), name
    return [snapshots[name] for name in order]


async def test_real_crash_snapshots_reopen_without_shell_replay(
    consumer_source_root: tuple[str, Path], crash_snapshots: list[CrashSnapshot], tmp_path: Path
) -> None:
    for snapshot in crash_snapshots:
        result = await consume_snapshot(snapshot, consumer_source_root, tmp_path / "consumers")
        agent = result["agent"]
        was_running = snapshot.agent["status"] == "running"
        assert agent["status"] == ("interrupted" if was_running else "completed"), snapshot.name
        assert agent["generation"] == snapshot.agent["generation"], snapshot.name
        expected_revision = snapshot.agent["completion_revision"] + int(was_running)
        assert agent["completion_revision"] == expected_revision, snapshot.name
        assert agent["run_id"] == snapshot.agent["run_id"], snapshot.name
        assert result["session"]["status"] == "idle", snapshot.name
        assert result["ingress"] == snapshot.ingress, snapshot.name
        assert result["facts"] == snapshot.facts, snapshot.name
        assert result["message_ids"] == snapshot.message_ids, snapshot.name
        assert len(result["runs"]) == len(snapshot.runs), snapshot.name
        for before, after in zip(snapshot.runs, result["runs"], strict=True):
            expected = {**before}
            if expected["status"] in {"running", "queued", "pending"}:
                expected["status"] = "interrupted"
            assert after == expected, (snapshot.name, after)
        assert all(count == 0 for count in result["attempts"].values()), snapshot.name


async def test_old_and_candidate_consume_only_a_real_independent_queued_input(
    consumer_source_root: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("真实新输入例外前置不允许外部 HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    async with registered_shell_wait(tmp_path / "producer") as state:
        async with state.runtime.configuration_lock:
            sent = await state.runtime.team.send(
                state.target, "EXPLICIT_FRESH_INPUT：只处理这条真实补充，不得重做原命令。"
            )
            snapshot = capture_snapshot(
                state,
                tmp_path / "snapshots",
                "real-input-queued",
                fresh_input_id=sent["receipt_id"],
            )
            assert snapshot.agent["status"] == "queued" and snapshot.agent["generation"] == 1
            pending = [row for row in snapshot.ingress if row["state"] == "pending"]
            assert len(pending) == 1 and pending[0]["id"] == sent["receipt_id"]
            assert pending[0]["payload"]["source"].startswith("team:") and not snapshot.facts
    result = await consume_snapshot(snapshot, consumer_source_root, tmp_path / "consumer")
    assert result["agent"]["status"] == "completed" and result["agent"]["generation"] == 2
    assert result["agent"]["completion_revision"] == snapshot.agent["completion_revision"] + 1
    assert result["agent"]["result"] == "EXACT_FRESH_INPUT_HANDLED_ONCE"
    assert len(result["runs"]) == len(snapshot.runs) + 1
    assert result["runs"][-1]["status"] == "completed"
    assert result["facts"] == []
    assert len(result["fresh_messages"]) == 1
    archived = result["fresh_messages"][0]
    original = pending[0]
    assert archived["delivery_status"] == "delivered"
    assert archived["content"] == original["payload"]["content"]
    assert archived["source"] == original["payload"]["source"]
    assert archived["principal"] == original["payload"]["principal"]
    assert archived["input_seq"] == original["seq"]
    assert archived["run_id"] == result["agent"]["run_id"]
    assert len(result["ingress"]) == len(snapshot.ingress)
    for before, after in zip(snapshot.ingress, result["ingress"], strict=True):
        if before["id"] != snapshot.fresh_input_id:
            assert before == after
        else:
            assert after == {
                **before,
                "state": "delivered",
                "delivered_seq": archived["seq"],
                "run_id": archived["run_id"],
            }
    assert result["attempts"] == {
        "model_factory": 1,
        "model_stream": 1,
        "model_complete": 0,
        "shell": 0,
        "process": 0,
        "network": 0,
    }
    # The ordinary result survives; deferring the parent's model never converts
    # this authorized input/report into another shell continuation trigger.
    reports = [report for report in result["reports"] if report["id"] == result["agent"]["run_id"]]
    assert len(reports) == 1 and reports[0]["status"] == "delivered"
    assert reports[0]["execution_status"] == "completed"
    returned = [
        row
        for row in result["parent_ingress"]
        if row["payload"]["source"] == f"team:{snapshot.agent['id']}"
        and row["payload"]["content"] == reports[0]["content"]
    ]
    assert len(returned) == 1 and returned[0]["state"] == "pending"
    assert returned[0]["payload"]["source"] == f"team:{snapshot.agent['id']}"
    assert returned[0]["payload"]["principal"] == f"agent:{snapshot.agent['id']}"
    assert returned[0]["payload"]["content"] == reports[0]["content"]
    assert "EXACT_FRESH_INPUT_HANDLED_ONCE" in reports[0]["content"]
