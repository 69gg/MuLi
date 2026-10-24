"""Optional attribution limits through real Team/Runtime and file_publish handler.

The zero-argument probe preserves the real prepared/tool caller context while
calling the actual local publication handler. It deliberately does not claim
that arbitrary surrogate-containing model arguments are persistable by the
older, general tool.started event path. No report or history is fabricated.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn, cast

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_task_briefs import attach_services
from test_task_control_runtime import wait_agent
from test_teammate_attachment_runtime import tool_result

from muli import local_services as service_module
from muli.local_services import LocalServices
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import ClientFactory, Runtime
from muli.task_file_publication import PublicationSource
from muli.tools import Tool, parameters


@pytest.fixture
async def publication_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Runtime, LocalServices]]:
    async def no_network(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("publication limit regression must remain offline")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "memory": {"enabled": False, "auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "limits": {"max_output_chars": 6000, "max_context_chars": 16000},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
        }
    )
    await runtime.reconfigure()
    services = attach_services(runtime, tmp_path)
    try:
        yield runtime, services
    finally:
        await runtime.close()


async def publish_from_real_worker(
    runtime: Runtime, services: LocalServices, source: Path
) -> dict[str, Any]:
    """One real generation, a prepared host scope and one actual copy handler."""
    task = await services.commitments.save(
        "保留本地资料副本", brief={"goal": "复制临时资料到本地附件", "constraints": ["不外发"]}
    )
    results: list[dict[str, Any]] = []
    rounds = 0

    async def publish(args: dict[str, Any]) -> Any:
        identifier = runtime.tool_session_id
        assert identifier is not None
        prepared = runtime.request_context(identifier)
        assert prepared is not None and prepared.execution_scope is not None
        assert prepared.execution_scope.commitment_id == task["id"]
        assert runtime._tool_execution.get() == (identifier, runtime.run_ids[identifier])
        return await runtime.tools["file_publish"].handler({"path": str(source)})

    runtime.tools["publication_limit_probe"] = Tool(
        "publication_limit_probe", "临时文件发布边界探针", parameters({}), publish, "write"
    )

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                assert any(item["function"]["name"] == "publication_limit_probe" for item in tools)
                turn = ModelTurn(
                    tool_calls=[ToolCall("publication-limit", "publication_limit_probe", "{}")]
                )
            else:
                assert rounds == 2
                results.append(tool_result(messages, "publication-limit"))
                turn = ModelTurn(content="本地复制操作已结束。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return (
            Worker([], [])
            if config.model == "worker"
            else FakeClient([ModelTurn(content="已收到队友记录。")], [])
        )

    runtime.client_factory = cast(ClientFactory, factory)
    runtime.main_session()
    agent = await runtime.team.spawn(
        "本地复制队友", "执行本地复制，不发送文件。", commitment_id=task["id"]
    )
    await wait_agent(runtime, agent["id"], "completed")
    assert rounds == 2 and len(results) == 1
    return results[0]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux surrogateescape filesystem paths")
async def test_surrogate_ancestor_does_not_break_successful_copy_or_create_optional_proof(
    publication_runtime: tuple[Runtime, LocalServices], tmp_path: Path
) -> None:
    runtime, services = publication_runtime
    parent = Path(os.fsdecode(os.fsencode(tmp_path) + b"/ancestor-\xff"))
    parent.mkdir()
    source = parent / "valid-basename.txt"
    content = b"ordinary local publication survives optional attribution"
    source.write_bytes(content)
    assert runtime.files.path(str(source)) == source
    assert os.fsencode(parent).endswith(b"ancestor-\xff")
    with pytest.raises(UnicodeEncodeError):
        str(source).encode("utf-8")
    result = await publish_from_real_worker(runtime, services, source)
    # Check the copy first: the older path succeeds at Attachments.save and
    # only then fails binding the newly added source_path proof to SQLite.
    attachments = services.attachments.list()
    assert len(attachments) == 1
    assert services.attachments.path(attachments[0]["id"]).read_bytes() == content
    assert "error" not in result, result
    assert result["id"] == attachments[0]["id"]
    assert runtime.store.list("task_file_publication") == []
    assert any(item["type"] == "attachment.published" for item in runtime.store.events())


@pytest.mark.parametrize(("kind", "field"), [("agent", "status"), ("commitment", "revision")])
async def test_capture_rejects_oversize_host_fields_before_sqlite_python_text_projection(
    publication_runtime: tuple[Runtime, LocalServices],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    field: str,
) -> None:
    runtime, services = publication_runtime
    source = tmp_path / "bounded-source.txt"
    source.write_bytes(b"publication remains successful when optional proof is unavailable")
    original_capture = service_module.capture_source
    captured = 0
    crossed_lengths: list[int] = []
    budget = runtime.config.settings.limits.max_output_chars * 4
    oversized = "x" * (1024 * 1024)
    assert len(oversized) > budget

    def capture(host: Runtime, path: Path) -> PublicationSource | None:
        nonlocal captured
        captured += 1
        baseline = original_capture(host, path)
        assert baseline is not None, (
            "must reach real current running task attribution before corruption"
        )
        identifier = (
            baseline.snapshot.session_id if kind == "agent" else baseline.snapshot.commitment_id
        )
        db = host.store.db
        original = db.execute(
            "SELECT data FROM objects WHERE kind=? AND id=?", (kind, identifier)
        ).fetchone()[0]
        factory = db.row_factory
        assert factory is sqlite3.Row
        try:
            with db:
                db.execute(
                    "UPDATE objects SET data=json_set(data,?,?) WHERE kind=? AND id=?",
                    ("$." + field, oversized, kind, identifier),
                )

            def observe(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> sqlite3.Row:
                # Observation occurs at the actual SQL-to-Python row boundary;
                # do not replace the production query or fabricate its result.
                crossed_lengths.extend(
                    len(item) for item in row if isinstance(item, str) and len(item) > budget
                )
                return sqlite3.Row(cursor, row)

            db.row_factory = observe
            result = original_capture(host, path)
            assert result is None
            return result
        finally:
            db.row_factory = factory
            with db:
                db.execute(
                    "UPDATE objects SET data=? WHERE kind=? AND id=?", (original, kind, identifier)
                )

    monkeypatch.setattr(service_module, "capture_source", capture)
    result = await publish_from_real_worker(runtime, services, source)
    assert captured == 1
    assert "error" not in result, result
    assert services.attachments.path(result["id"]).read_bytes() == source.read_bytes()
    assert runtime.store.list("task_file_publication") == []
    assert crossed_lengths == [], (
        "oversized host field crossed the original evidence budget into Python"
    )
