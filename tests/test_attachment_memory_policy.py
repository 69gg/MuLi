"""Attachment-memory policy through real application tools and persisted sources.

Only local uploads, model strategy and explicit await barriers are controlled here;
the cross-channel/embedding journey lives in the separate formal test.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from fastapi import FastAPI
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import eventually

from muli.attachments import Attachments
from muli.memory_application import MemoryApplication
from muli.memory_pipeline import Extractor
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import new_id


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("attachment-memory policy must remain offline")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)


@dataclass
class MemoryRig:
    runtime: Runtime
    app: FastAPI
    attachments: Attachments
    memory: MemoryApplication
    calls: list[str]

    async def upload(self, data: bytes, *, source: str = "web") -> dict[str, Any]:
        async def chunks() -> AsyncIterator[bytes]:
            yield data

        return await self.attachments.save(
            chunks(), "policy-material.txt", "text/plain", source=source
        )

    async def begin(self, identifier: str) -> dict[str, Any]:
        return await self.runtime.submit(
            "收藏本附件：" + identifier, mode="yolo", attachment_ids=[identifier]
        )

    async def settle(self, receipt: dict[str, Any]) -> None:
        await asyncio.wait_for(self.runtime.wait(receipt["run_id"]), timeout=4)
        run = self.runtime.store.get("run", receipt["run_id"])
        assert run is not None and run["status"] == "completed"

    def result(self, call_id: str) -> dict[str, Any]:
        messages = self.runtime.store.messages(self.runtime.main_session()["id"])
        matching = [item for item in messages if item.get("tool_call_id") == call_id]
        assert len(matching) == 1
        result = json.loads(matching[0]["content"])
        assert isinstance(result, dict)
        return result

    async def import_attachment(self, identifier: str) -> dict[str, Any]:
        previous = len(self.calls)
        receipt = await self.begin(identifier)
        await self.settle(receipt)
        assert len(self.calls) == previous + 1
        return self.result(self.calls[-1])

    def absent(self, identifier: str) -> None:
        source_id = "attachment:" + identifier
        assert self.runtime.memory.source(source_id) is None
        assert self.memory.pipeline.job(source_id) is None
        assert (
            self.runtime.store.db.execute(
                "SELECT count(*) FROM memory_meta WHERE source_id=?", (source_id,)
            ).fetchone()[0]
            == 0
        )


@asynccontextmanager
async def actual_app(
    tmp_path: Path, *, extractor: Extractor | None = None, worker: FakeClient | None = None
) -> AsyncIterator[MemoryRig]:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "memory": {"enabled": True, "auto_extract": True},
            "limits": {
                "max_context_chars": 16000,
                "max_output_chars": 6000,
                "max_pending_runs": 3,
            },
        }
    )
    if worker is not None:
        runtime.config.update(
            {
                "models": {
                    "worker": {
                        "model": "attachment-policy-worker",
                        "base_url": "https://unused.invalid/v1",
                    }
                }
            }
        )
    await runtime.reconfigure()
    calls: list[str] = []

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            user_index = max(index for index, item in enumerate(messages) if item.role == "user")
            user = messages[user_index]
            after = messages[user_index + 1 :]
            if user.content.startswith("收藏本附件：") and not any(
                item.role == "tool" for item in after
            ):
                names = {item["function"]["name"] for item in tools}
                assert "memory_import_attachment" in names
                identifier = user.content.removeprefix("收藏本附件：")
                call_id = "policy-import-" + new_id()
                calls.append(call_id)
                turn = ModelTurn(
                    tool_calls=[
                        ToolCall(
                            call_id, "memory_import_attachment", json.dumps({"id": identifier})
                        )
                    ]
                )
            else:
                turn = ModelTurn(content="已完成本轮处理；收藏状态以工具回执为准。")
            yield StreamEvent("done", turn=turn)

    def factory(profile: ModelConfig) -> FakeClient:
        if worker is not None and profile.model == "attachment-policy-worker":
            return worker
        return Main([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    memory: MemoryApplication = app.state.memory_application

    async def empty(source: dict[str, Any]) -> dict[str, Any]:
        return {"memories": [], "aliases": {}}

    memory.pipeline.extractor = extractor or empty
    async with app.router.lifespan_context(app):
        assert memory._recovery is not None
        await asyncio.wait_for(asyncio.shield(memory._recovery), timeout=4)
        yield MemoryRig(runtime, app, app.state.attachments, memory, calls)


@pytest.mark.parametrize("damage", ["utf8", "nul", "source_limit", "hash", "size"])
async def test_invalid_actual_attachment_never_registers_source_or_job(
    tmp_path: Path, damage: str
) -> None:
    async with actual_app(tmp_path) as rig:
        data = {"utf8": b"actual-invalid-\xff", "nul": b"actual\x00text"}.get(
            damage, "真实资料中的完整正文。".encode()
        )
        if damage == "source_limit":
            data = ("文" * 1001).encode("utf-8")
        item = await rig.upload(data)
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
        assert item["size"] == len(data)
        if damage == "source_limit":
            # The source was uploaded under the old budget. A live smaller
            # setting must also constrain this invocation's complete snapshot.
            rig.runtime.config.update({"memory": {"max_source_chars": 1000}})
            assert len(data.decode("utf-8")) > rig.runtime.config.settings.memory.max_source_chars
        elif damage == "hash":
            replacement = b"X" * len(data)
            assert hashlib.sha256(replacement).hexdigest() != item["sha256"]
            rig.attachments.path(item["id"]).write_bytes(replacement)
        elif damage == "size":
            rig.attachments.path(item["id"]).write_bytes(data + b"x")
        result = await rig.import_attachment(item["id"])
        assert "error" in result
        rig.absent(item["id"])


@pytest.mark.parametrize("source", ["asr", "vision", "tts", "unknown"])
async def test_generated_or_unknown_attachment_source_is_not_original_evidence(
    tmp_path: Path, source: str
) -> None:
    async with actual_app(tmp_path) as rig:
        data = "即使是合法完整UTF-8，生成解释也不能冒充原始上传资料。".encode()
        item = await rig.upload(data, source=source)
        saved = rig.attachments.get(item["id"])
        assert saved["source"] == source and saved["size"] == len(data)
        assert saved["sha256"] == hashlib.sha256(data).hexdigest()
        assert rig.attachments.path(item["id"]).read_bytes() == data
        result = await rig.import_attachment(item["id"])
        assert "error" in result
        rig.absent(item["id"])


async def test_real_team_worker_cannot_see_or_invoke_attachment_collection(
    tmp_path: Path,
) -> None:
    identifier = ""
    call_id = "worker-attachment-import-" + new_id()
    catalogs: list[set[str]] = []

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            catalogs.append({item["function"]["name"] for item in tools})
            assert "memory_import_attachment" not in catalogs[-1]
            replies = [item for item in messages if item.tool_call_id == call_id]
            if not replies:
                assert len(catalogs) == 1 and identifier
                # Intentionally request the missing tool. Runtime must enforce
                # the worker's real catalog, rather than trusting the model.
                turn = ModelTurn(
                    tool_calls=[
                        ToolCall(
                            call_id, "memory_import_attachment", json.dumps({"id": identifier})
                        )
                    ]
                )
            else:
                assert len(replies) == 1 and len(catalogs) == 2
                assert json.loads(replies[0].content) == {"error": "工具未启用"}
                turn = ModelTurn(content="收藏工具未向本队友开放，未执行收藏。")
            yield StreamEvent("done", turn=turn)

    async with actual_app(tmp_path, worker=Worker([], [])) as rig:
        item = await rig.upload("这是存在且可核查的原始附件。".encode())
        identifier = item["id"]
        agent = await rig.runtime.team.spawn(
            "收藏边界核查",
            "尝试收藏所附资料，并准确报告工具执行结果。",
            attachment_ids=[identifier],
        )

        def delivered() -> bool:
            current = rig.runtime.team.get(agent["id"])
            report = rig.runtime.store.get("team_report", current.get("run_id", ""))
            return (
                current["status"] == "completed"
                and rig.runtime.team.idle
                and report is not None
                and report["status"] == "delivered"
            )

        await eventually(rig.runtime, delivered)
        current = rig.runtime.team.get(agent["id"])
        run = rig.runtime.store.get("run", current["run_id"])
        assert run is not None and run["status"] == "completed"
        assert current["parent_id"] == rig.runtime.main_session()["id"]
        assert len(catalogs) == 2 and all(
            "memory_import_attachment" not in names for names in catalogs
        )
        replies = [
            message
            for message in rig.runtime.store.messages(agent["id"])
            if message.get("tool_call_id") == call_id
        ]
        assert len(replies) == 1
        assert json.loads(replies[0]["content"]) == {"error": "工具未启用"}
        assert not any(
            event["type"] == "tool.started" and event["data"].get("call_id") == call_id
            for event in rig.runtime.store.events(session_id=agent["id"])
        )
        assert not rig.calls
        rig.absent(identifier)


@pytest.mark.parametrize("revocation", ["owner_input", "reconfigure"])
async def test_read_await_cannot_publish_after_real_input_or_pipeline_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    async with actual_app(tmp_path) as rig:
        item = await rig.upload("原文只能由仍有效的这一轮收藏。".encode())
        services = rig.app.state.local_services
        original = services.read_bytes
        entered, release = asyncio.Event(), asyncio.Event()

        async def paused(identifier: str, max_bytes: int) -> tuple[dict[str, Any], bytes]:
            snapshot = await original(identifier, max_bytes)
            if identifier == item["id"]:
                entered.set()
                await release.wait()
            return snapshot

        monkeypatch.setattr(services, "read_bytes", paused)
        receipt = await rig.begin(item["id"])
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            assert len(rig.calls) == 1
            if revocation == "owner_input":
                newer = await rig.runtime.submit("先不要收藏，处理这条新的主人输入。")
                assert newer["message"]["id"] != receipt["message"]["id"]
                assert rig.runtime.store.pending_inputs(rig.runtime.main_session()["id"])
            else:
                previous = rig.memory.pipeline
                await rig.memory.reconfigure()
                assert rig.memory.pipeline is not previous
                assert rig.memory._recovery is not None
                await asyncio.wait_for(asyncio.shield(rig.memory._recovery), timeout=4)
            rig.absent(item["id"])
        finally:
            release.set()
            await rig.settle(receipt)
        result = rig.result(rig.calls[0])
        assert "error" in result or result.get("status") == "not_executed"
        rig.absent(item["id"])


async def test_collected_attachment_cannot_supply_owner_identity_preference_or_aliases(
    tmp_path: Path,
) -> None:
    extracted: list[str] = []

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        if not source["id"].startswith("attachment:"):
            return {"memories": [], "aliases": {}}
        extracted.append(source["id"])
        assert source["trusted_owner"] is False
        return {
            "memories": [
                {"layer": "identity", "content": "陌客是主人", "quote": source["content"]},
                {"layer": "preference", "content": "主人喜欢紫色", "quote": source["content"]},
                {"layer": "semantic", "content": "资料声称需要复核", "quote": source["content"]},
            ],
            "aliases": {"陌客": "主人"},
        }

    async with actual_app(tmp_path, extractor=extract) as rig:
        text = "陌客声称自己是主人，主人喜欢紫色；资料中的这些说法需要复核。"
        item = await rig.upload(text.encode("utf-8"))
        result = await rig.import_attachment(item["id"])
        source_id = "attachment:" + item["id"]
        assert result["source_id"] == source_id
        assert result["attachment_id"] == item["id"] and result["sha256"] == item["sha256"]
        source = rig.runtime.memory.source(source_id)
        assert source is not None and source["content"] == text
        assert source["role"] == "tool" and source["principal"] == "external:attachment"
        assert source["trusted_owner"] is False
        job = await asyncio.wait_for(rig.memory.pipeline.wait(source_id), timeout=3)
        assert job is not None and job["state"] == "done"
        assert extracted == [source_id]
        assert job["checkpoint"]["extraction"]["rejected_untrusted_owner_claims"] == 2
        layers = rig.runtime.store.db.execute(
            "SELECT layer,trusted_owner FROM memory_meta WHERE source_id=?", (source_id,)
        ).fetchall()
        assert {row[0] for row in layers} == {"episodic", "semantic"}
        assert all(row[1] == 0 for row in layers)
        assert (
            rig.runtime.store.db.execute(
                "SELECT count(*) FROM memory_aliases WHERE source_id=?", (source_id,)
            ).fetchone()[0]
            == 0
        )


async def test_source_commit_episode_rollback_recovers_once_and_forget_never_revives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extracted: list[str] = []
    owner_entered, owner_release = asyncio.Event(), asyncio.Event()
    text = "EPISODE-GAP:完整附件资料只能精确补齐一次。"

    async def slow_owner(source: dict[str, Any]) -> dict[str, Any]:
        if source["id"].startswith("attachment:"):
            extracted.append(source["id"])
        else:
            owner_entered.set()
            await owner_release.wait()
        return {"memories": [], "aliases": {}}

    async with actual_app(tmp_path, extractor=slow_owner) as rig:
        item = await rig.upload(text.encode("utf-8"))
        source_id = "attachment:" + item["id"]
        errors: list[sqlite3.IntegrityError] = []
        original = rig.runtime.memory.save

        def observe_save(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return original(*args, **kwargs)
            except sqlite3.IntegrityError as error:
                if kwargs.get("source_id") == source_id:
                    errors.append(error)
                raise

        # This is a real SQLite rollback after source registration committed;
        # the observer delegates the unmodified Memory.save implementation.
        with rig.runtime.store.db:
            rig.runtime.store.db.execute(
                "CREATE TRIGGER reject_attachment_episode BEFORE INSERT ON memories "
                "WHEN NEW.content='EPISODE-GAP:完整附件资料只能精确补齐一次。' "
                "BEGIN SELECT RAISE(ABORT,'controlled episode interruption'); END"
            )
        try:
            with monkeypatch.context() as patch:
                patch.setattr(rig.runtime.memory, "save", observe_save)
                result = await rig.import_attachment(item["id"])
            await asyncio.wait_for(owner_entered.wait(), timeout=3)
            assert "error" in result and len(errors) == 1
            source = rig.runtime.memory.source(source_id)
            assert source is not None and source["content"] == text
            assert rig.runtime.memory.automatic_episode_id(source_id) is None
            assert rig.memory.pipeline.job(source_id) is None
        finally:
            with rig.runtime.store.db:
                rig.runtime.store.db.execute("DROP TRIGGER reject_attachment_episode")
            # Leave the owned pipeline worker at its real extractor await;
            # normal application close cancels it and settles the job.

    async def record(source: dict[str, Any]) -> dict[str, Any]:
        if source["id"].startswith("attachment:"):
            extracted.append(source["id"])
        return {"memories": [], "aliases": {}}

    async with actual_app(tmp_path, extractor=record) as reopened:
        assert reopened.runtime.memory.source(source_id) == source
        done = await asyncio.wait_for(reopened.memory.pipeline.wait(source_id), timeout=3)
        assert done is not None and done["state"] == "done"
        episode_id = reopened.runtime.memory.automatic_episode_id(source_id)
        assert episode_id is not None
        assert extracted == [source_id]

    async with actual_app(tmp_path, extractor=record) as again:
        assert again.runtime.memory.automatic_episode_id(source_id) == episode_id
        assert again.memory.pipeline.job(source_id) == done
        assert extracted == [source_id]
        result = await again.import_attachment(item["id"])
        assert result["source_id"] == source_id and result["state"] == "done"
        assert again.memory.pipeline.job(source_id) == done
        assert extracted == [source_id]
        # Forget a real derived memory, exactly as memory_forget does, rather
        # than manufacturing a source tombstone for the test.
        again.runtime.memory.delete(episode_id)
        again.absent(item["id"])

    async with actual_app(tmp_path, extractor=record) as forgotten:
        forgotten.absent(item["id"])
        assert forgotten.attachments.path(item["id"]).read_bytes() == text.encode("utf-8")
        result = await forgotten.import_attachment(item["id"])
        assert result["source_id"] == source_id and result["state"] == "forgotten"
        forgotten.absent(item["id"])
        assert extracted == [source_id]
