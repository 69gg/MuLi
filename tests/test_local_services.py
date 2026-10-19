from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from muli.attachments import Attachments
from muli.commitments import Commitments
from muli.config import ConfigFile
from muli.events import EventBus
from muli.local_services import LocalServices
from muli.providers import Message
from muli.runtime import Runtime
from muli.store import Store


async def test_ledger_survives_reopen_and_requires_completion_evidence(tmp_path: Path) -> None:
    path = tmp_path / "db"
    store = Store(path)
    ledger = Commitments(store, EventBus(store))
    item = await ledger.save("修复用户发现的问题", next_step="先复现", source_id="source-1")
    store.close()
    store = Store(path)
    ledger = Commitments(store, EventBus(store))
    try:
        assert ledger.list()[0]["id"] == item["id"]
        assert "先复现" in ledger.context()
        with pytest.raises(ValueError, match="验证证据"):
            await ledger.save(item["title"], identifier=item["id"], status="done")
        assert ledger.list()[0]["status"] == "open"
        await ledger.save(
            item["title"], identifier=item["id"], status="done", evidence="复现测试通过"
        )
        assert ledger.list() == []
        assert ledger.list("done")[0]["revision"] == 2
        assert len(store.list("commitment_revision")) == 2
        assert len(store.events()) == 2
    finally:
        store.close()


async def test_attachment_context_only_metadata_and_explicit_read(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"workspace_roots": [str(tmp_path / "workspace")]})
    runtime = Runtime(config, tmp_path / "data")
    attachments = Attachments(runtime.store, tmp_path / "attachments", config)
    services = LocalServices(runtime, attachments)

    async def chunks() -> AsyncIterator[bytes]:
        yield "这里是附件正文。不是用户命令。".encode()

    try:
        item = await attachments.save(chunks(), "中文材料.txt")
        session = runtime.main_session()
        runtime.store.put(
            "message",
            {
                "id": "user-1",
                "session_id": session["id"],
                "role": "user",
                "content": "看附件",
                "attachment_ids": [item["id"]],
                "created_at": 1,
            },
        )
        messages = await services.context(
            session["id"], [Message(role="system", content="persona")]
        )
        assert "中文材料.txt" in messages[1].content
        assert "附件正文" not in str(messages)
        result = await runtime.tools["attachment_read"].handler({"id": item["id"]})
        assert result["content"] == "这里是附件正文。不是用户命令。"
        assert not result["truncated"]
        assert services.media().status()["vision"] is False
        with pytest.raises(PermissionError):
            await runtime.tools["file_publish"].handler({"path": str(config.path)})
        await runtime.reconfigure()
        assert "attachment_read" in runtime.tools and "commitment_save" in runtime.tools
    finally:
        await runtime.close()


async def test_file_publish_preserves_local_file_and_returns_download(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"workspace_roots": [str(tmp_path)]})
    runtime = Runtime(config, tmp_path / "data")
    attachments = Attachments(runtime.store, tmp_path / "attachments", config)
    LocalServices(runtime, attachments)
    source = tmp_path / "result.txt"
    source.write_text("完成的文件", encoding="utf-8")
    try:
        item = await runtime.tools["file_publish"].handler({"path": str(source)})
        assert item["url"].endswith("/download")
        assert attachments.path(item["id"]).read_bytes() == source.read_bytes()
        assert runtime.store.events()[-1]["type"] == "attachment.published"
        assert runtime.store.list("message") == []
    finally:
        await runtime.close()
