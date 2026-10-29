"""Complete injected readers preserve cancellation before source admission."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

import pytest
from test_attachment_memory_policy import actual_app
from test_attachment_memory_policy import offline as offline
from test_document_memory import start_tool
from test_document_reading import docx

from muli.document_reading import DocumentView


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_kind", ["text", "docx"])
@pytest.mark.parametrize("cancel_mode", ["stop", "task"])
async def test_injected_complete_reader_cannot_finish_or_collect_after_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader_kind: Literal["text", "docx"],
    cancel_mode: Literal["stop", "task"],
) -> None:
    entered, swallowed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancelling_counts: list[int] = []
    text = "完整真实资料已经核验，但取消中的收藏不能迟到提交或误记完成。"
    async with actual_app(tmp_path) as rig:
        item = await rig.upload(text.encode("utf-8") if reader_kind == "text" else docx(text))
        identifier = item["id"]
        raw_item = rig.attachments.get(identifier)
        assert item == rig.attachments.public(raw_item)
        session_id = rig.runtime.main_session()["id"]
        arguments: dict[str, Any] = {"id": identifier}

        async def hold_verified_read() -> None:
            task = asyncio.current_task()
            assert task is not None and rig.runtime.tasks[session_id] is task
            active = rig.runtime._active_tool.get()
            assert active is not None and active.name == "memory_import_attachment"
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelling_counts.append(task.cancelling())
                swallowed.set()
                await release.wait()

        if reader_kind == "text":
            original_text = rig.memory.attachment_reader
            assert original_text is not None

            async def text_reader(candidate: str) -> tuple[dict[str, Any], str]:
                verified, content = await original_text(candidate)
                assert candidate == identifier and verified == raw_item and content == text
                await hold_verified_read()
                return verified, content

            monkeypatch.setattr(rig.memory, "attachment_reader", text_reader)
        else:
            prepared, call_id = await start_tool(rig, "document_read", id=identifier)
            await asyncio.wait_for(rig.runtime.wait(prepared["run_id"]), timeout=4)
            page = rig.result(call_id)
            assert "error" not in page
            arguments["revision"] = page["revision"]
            original_document = rig.memory.document_reader
            assert original_document is not None

            async def document_reader(
                candidate: str, revision: str
            ) -> tuple[dict[str, Any], DocumentView]:
                verified, view = await original_document(candidate, revision)
                assert candidate == identifier and verified == raw_item
                assert view.revision == revision and text in view.content.decode("utf-8")
                await hold_verified_read()
                return verified, view

            monkeypatch.setattr(rig.memory, "document_reader", document_reader)

        receipt, call_id = await start_tool(rig, "memory_import_attachment", **arguments)
        task = rig.runtime.tasks[session_id]
        entered_waiter = asyncio.create_task(entered.wait())
        stopping: asyncio.Task[None] | None = None
        try:
            await asyncio.wait(
                (entered_waiter, task), timeout=4, return_when=asyncio.FIRST_COMPLETED
            )
            assert entered.is_set(), [
                message["content"]
                for message in rig.runtime.store.messages(session_id)
                if message.get("tool_call_id") == call_id
            ]
            if cancel_mode == "stop":
                stopping = asyncio.create_task(rig.runtime.stop(session_id))
            else:
                assert task.cancel()
            await asyncio.wait_for(swallowed.wait(), timeout=4)
            assert cancelling_counts == [task.cancelling()] and cancelling_counts[0] > 0
            assert (session_id in rig.runtime._stopping) is (cancel_mode == "stop")
            rig.absent(identifier)
        finally:
            release.set()
            entered_waiter.cancel()
            pending: list[asyncio.Task[Any]] = [task, entered_waiter]
            if stopping is not None:
                pending.append(stopping)
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=4)

        rig.absent(identifier)
        run = rig.runtime.store.get("run", receipt["run_id"])
        assert run is not None and run["status"] == "cancelled", run
        assert task.cancelled()
        events = [
            event
            for event in rig.runtime.store.events(limit=1000)
            if event["data"].get("run_id") == receipt["run_id"]
        ]
        assert any(event["type"] == "run.cancelled" for event in events)
        assert not any(event["type"] == "run.completed" for event in events)
