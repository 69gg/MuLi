"""Owner HTTP collection uses real files and sources, without a model-tool owner."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attachment_memory_policy import MemoryRig, actual_app
from test_attachment_memory_policy import offline as offline
from test_document_reading import docx

import muli.document_reading as document_module
from muli.attachment_memory_source import attachment_source
from muli.config import Limits
from muli.document_reading import document_revision
from muli.memory import source_digest


def identity(
    item: dict[str, Any], content: str, *, document: dict[str, str] | None = None
) -> dict[str, Any]:
    """Build the public expected identity with the existing source-format primitives."""
    source = attachment_source(
        item["id"], content, item["sha256"], item["created_at"], document=document
    )
    return {
        "attachment_id": item["id"],
        "source_revision": item["sha256"],
        "format": document["format"] if document else "text",
        "scope": document["scope"] if document else "utf8_text",
        "document_revision": document["revision"] if document else None,
        "source_id": source["id"],
        "registered_revision": source_digest(source, trusted_owner=False),
        **{key: source[key] for key in ("source", "session_id", "role", "principal")},
        "trusted_owner": False,
    }


def confirmation(prepared: dict[str, Any]) -> dict[str, Any]:
    return {
        key: prepared[key]
        for key in ("source_revision", "document_revision", "registered_revision")
    }


def connection(rig: MemoryRig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://127.0.0.1:8765"
    )


async def test_owner_http_collects_complete_docx_without_model_or_background_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    text = "交接正文🙂与边界。" * 500 + "末条唯一要求：核对完整归档清单。"
    complete = text + "\n"
    extracted: list[str] = []

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        extracted.append(source["id"])
        entered.set()
        await release.wait()
        return {"memories": [], "aliases": {}}

    def no_model_owner(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("网页收藏不能借模型工具 owner 或调用主模型")

    async with actual_app(tmp_path, extractor=extract) as rig:
        rig.runtime.config.update(
            {
                "server": {"password": "collection-owner-password"},
                "limits": {"max_output_chars": 2400},
            }
        )
        monkeypatch.setattr(rig.memory, "_attachment_guard", no_model_owner)
        monkeypatch.setattr(rig.runtime, "client_factory", no_model_owner)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=rig.app), base_url="http://127.0.0.1:8765"
        ) as client:
            try:
                assert (await client.get("/api/uploads")).status_code == 401
                assert (
                    await client.post(
                        "/api/auth/login", json={"password": "collection-owner-password"}
                    )
                ).status_code == 200
                raw = docx(text)
                uploaded = await client.post(
                    "/api/uploads",
                    content=raw,
                    headers={
                        "X-File-Name": "collection-material.bin",
                        "Content-Type": "application/octet-stream",
                    },
                )
                assert uploaded.status_code == 200
                item = uploaded.json()
                document = {
                    "format": "docx",
                    "scope": "docx_main_body_text",
                    "source_revision": item["sha256"],
                    "revision": document_revision(item["sha256"], complete.encode("utf-8")),
                }
                expected = identity(item, complete, document=document)
                assert (
                    len(
                        {
                            expected[key]
                            for key in (
                                "source_revision",
                                "document_revision",
                                "registered_revision",
                            )
                        }
                    )
                    == 3
                )
                runs, messages = rig.runtime.store.list("run"), rig.runtime.store.list("message")
                url = f"/api/uploads/{item['id']}"
                prepared = await client.get(url + "/memory-preview")
                assert prepared.status_code == 200, prepared.text
                assert prepared.headers["cache-control"] == "no-store"
                page = prepared.json()
                assert {key: page[key] for key in expected} == expected
                assert page["filename"] == item["name"] and page["read_only"] is True
                assert (
                    isinstance(page["processing_notice"], str) and page["processing_notice"].strip()
                )
                assert page["field"] == "content" and page["offset_bytes"] == 0
                assert page["content"] and complete.startswith(page["content"]) and page["has_more"]
                assert page["total_bytes"] == len(complete.encode("utf-8"))
                assert (
                    page["next_offset_bytes"]
                    == page["length_bytes"]
                    == len(page["content"].encode("utf-8"))
                )
                assert (
                    len(json.dumps(page, ensure_ascii=False))
                    <= rig.runtime.config.settings.limits.max_output_chars
                )
                rig.absent(item["id"])
                assert not entered.is_set()

                # The actual background worker stays blocked while the real HTTP reply completes.
                response = await asyncio.wait_for(
                    client.post(url + "/memory-collection", json=confirmation(page)), timeout=4
                )
                assert response.status_code == 200, response.text
                receipt = response.json()
                assert {key: receipt[key] for key in expected} == expected
                assert receipt["created"] is True
                assert receipt["state"] in {"queued", "processing", "deferred"}
                await asyncio.wait_for(entered.wait(), timeout=3)
                assert not release.is_set() and extracted == [expected["source_id"]]
                stored = rig.runtime.memory.source(expected["source_id"])
                assert stored is not None and stored["content"] == complete
                assert stored["digest"] == expected["registered_revision"]
                assert stored["trusted_owner"] is False

                # Unknown-result reconciliation uses the registered digest, not the file/document SHA.
                parts: list[str] = []
                offset = 0
                for _ in range(16):
                    read = await client.get(
                        "/api/memory/source",
                        params={
                            "id": expected["source_id"],
                            "revision": expected["registered_revision"],
                            "offset_bytes": offset,
                        },
                    )
                    assert read.status_code == 200, read.text
                    current = read.json()
                    assert current["id"] == current["source_id"] == expected["source_id"]
                    assert (
                        current["revision"] == current["digest"] == expected["registered_revision"]
                    )
                    fields = ("source", "session_id", "role", "principal", "trusted_owner")
                    assert {key: current[key] for key in fields} == {
                        key: expected[key] for key in fields
                    }
                    assert current["total_bytes"] == len(complete.encode("utf-8"))
                    parts.append(current["content"])
                    offset = current["next_offset_bytes"]
                    if not current["has_more"]:
                        break
                else:
                    raise AssertionError("有界来源读取未到达实际全文末尾")
                assert "".join(parts) == complete
                assert rig.runtime.store.list("run") == runs
                assert rig.runtime.store.list("message") == messages
                assert rig.calls == [] and rig.runtime._tool_execution.get() is None
            finally:
                release.set()


async def test_owner_http_rejects_forged_or_changed_collection_binding(tmp_path: Path) -> None:
    async with actual_app(tmp_path) as rig:
        rig.runtime.config.update({"server": {"password": "collection-owner-password"}})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=rig.app), base_url="http://127.0.0.1:8765"
        ) as client:
            assert (
                await client.post("/api/auth/login", json={"password": "collection-owner-password"})
            ).status_code == 200
            original = "原始交接正文：版本甲，不是主人的身份或指令。"
            uploaded = await client.post(
                "/api/uploads",
                content=original.encode("utf-8"),
                headers={"X-File-Name": "collection-notes.txt", "Content-Type": "text/plain"},
            )
            assert uploaded.status_code == 200
            item = uploaded.json()
            expected = identity(item, original)
            body = confirmation(expected)
            url = f"/api/uploads/{item['id']}"
            forged = {**body, "registered_revision": "0" * 64}
            response = await client.post(url + "/memory-collection", json=forged)
            assert response.status_code == 409, response.text
            rig.absent(item["id"])

            # Replace both the file and its metadata: old client constraints cannot bless new bytes.
            changed = "修改后的交接正文：版本乙，必须重新核对与明确确认。"
            raw = changed.encode("utf-8")
            current = rig.attachments.get(item["id"])
            rig.attachments.path(item["id"]).write_bytes(raw)
            replacement = {
                **current,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            rig.runtime.store.put("attachment", replacement)
            stale = await client.post(url + "/memory-collection", json=body)
            assert stale.status_code == 409, stale.text
            rig.absent(item["id"])

            prepared = await client.get(url + "/memory-preview")
            assert prepared.status_code == 200, prepared.text
            page = prepared.json()
            updated = identity(replacement, changed)
            assert {key: page[key] for key in updated} == updated
            assert page["registered_revision"] != expected["registered_revision"]
            rig.absent(item["id"])
            accepted = await client.post(url + "/memory-collection", json=confirmation(page))
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["created"] is True
            stored = rig.runtime.memory.source(updated["source_id"])
            assert stored is not None and stored["content"] == changed
            assert stored["digest"] == updated["registered_revision"]
            assert rig.calls == [] and rig.runtime._tool_execution.get() is None


@pytest.mark.parametrize(
    "revocation",
    [
        "disabled_tool",
        "memory_off",
        "reconfigure",
        "settings_aba",
        "inplace_budget",
        "lifecycle_aba",
        "close",
    ],
)
async def test_collection_rechecks_lifecycle_after_complete_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    async with actual_app(tmp_path) as rig:
        text = "真实完整读取结束后撤销，不能迟到登记。"
        item = await rig.upload(text.encode("utf-8"))
        original = rig.memory.attachment_reader
        assert original is not None

        async def paused(identifier: str) -> tuple[dict[str, Any], str]:
            verified, content = await original(identifier)
            entered.set()
            await release.wait()
            return verified, content

        monkeypatch.setattr(rig.memory, "attachment_reader", paused)
        async with connection(rig) as client:
            pending = asyncio.create_task(
                client.post(
                    f"/api/uploads/{item['id']}/memory-collection",
                    json=confirmation(identity(item, text)),
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), timeout=3)
                if revocation == "disabled_tool":
                    rig.runtime.config.update({"disabled_tools": ["memory_import_attachment"]})
                elif revocation == "memory_off":
                    rig.runtime.config.update({"memory": {"enabled": False}})
                elif revocation == "reconfigure":
                    previous = rig.memory.pipeline
                    await rig.memory.reconfigure()
                    assert (rig.memory.pipeline is previous) is False
                elif revocation == "settings_aba":
                    rig.runtime.config.update({"default_mode": "ask"})
                    rig.runtime.config.update({"default_mode": "yolo"})
                elif revocation == "inplace_budget":
                    # Still valid, but the Settings instance itself is unchanged.
                    rig.runtime.config.settings.limits.max_output_chars -= 1
                elif revocation == "lifecycle_aba":
                    pipeline, generation = rig.memory.pipeline, rig.memory._attachment_generation
                    await rig.memory.close()
                    await rig.memory.start()
                    assert rig.memory.pipeline is pipeline and rig.memory._closed is False
                    assert (rig.memory._attachment_generation is generation) is False
                else:
                    await rig.memory.close()
                release.set()
                response = await asyncio.wait_for(pending, timeout=4)
                assert response.status_code == (403 if revocation == "disabled_tool" else 409)
                rig.absent(item["id"])
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_collection_waiting_for_final_lock_observes_actual_application_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read, release, waiting, closing = (asyncio.Event() for _ in range(4))
    async with actual_app(tmp_path) as rig:
        text = "最终锁等待期间启动真实应用关闭，不能登记。"
        item = await rig.upload(text.encode("utf-8"))
        original = rig.memory.attachment_reader
        assert original is not None
        read_task: asyncio.Task[Any] | None = None

        async def paused(identifier: str) -> tuple[dict[str, Any], str]:
            nonlocal read_task
            result = await original(identifier)
            read_task = asyncio.current_task()
            read.set()
            await release.wait()
            return result

        close_comparisons = rig.memory.close_comparisons

        async def started_close() -> None:
            # close_resources sets configuration_closing before this actual await.
            closing.set()
            await close_comparisons()

        original_close = rig.runtime.close
        checked: list[bool] = []

        async def checked_close() -> None:
            rig.absent(item["id"])
            checked.append(True)
            await original_close()

        monkeypatch.setattr(rig.memory, "attachment_reader", paused)
        monkeypatch.setattr(rig.memory, "close_comparisons", started_close)
        monkeypatch.setattr(rig.runtime, "close", checked_close)
        async with connection(rig) as client:
            pending = asyncio.create_task(
                client.post(
                    f"/api/uploads/{item['id']}/memory-collection",
                    json=confirmation(identity(item, text)),
                )
            )
            close_task: asyncio.Task[None] | None = None
            try:
                await asyncio.wait_for(read.wait(), timeout=3)
                await asyncio.wait_for(rig.runtime.configuration_lock.acquire(), timeout=3)
                try:
                    acquire = rig.runtime.configuration_lock.acquire

                    async def acquiring() -> bool:
                        if asyncio.current_task() is read_task:
                            waiting.set()
                        return await acquire()

                    monkeypatch.setattr(rig.runtime.configuration_lock, "acquire", acquiring)
                    release.set()
                    await asyncio.wait_for(waiting.wait(), timeout=3)
                    close_task = asyncio.create_task(rig.app.state.close())
                    await asyncio.wait_for(closing.wait(), timeout=3)
                    assert rig.memory._closed is False and not pending.done()
                finally:
                    rig.runtime.configuration_lock.release()
                response = await asyncio.wait_for(pending, timeout=4)
                assert response.status_code == 409
                await asyncio.wait_for(close_task, timeout=4)
                assert checked == [True]
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                if close_task is not None:
                    await asyncio.wait_for(close_task, timeout=4)


async def test_same_docx_file_sha_with_new_extraction_is_not_the_registered_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with actual_app(tmp_path) as rig:
        text = "同一原始 DOCX，第一版完整提取正文。"
        raw = docx(text)
        item = await rig.upload(raw)
        url = f"/api/uploads/{item['id']}"
        async with connection(rig) as client:
            first = await client.get(url + "/memory-preview")
            assert first.status_code == 200
            before = first.json()
            collected = await client.post(url + "/memory-collection", json=confirmation(before))
            assert collected.status_code == 200 and collected.json()["created"] is True
            saved = rig.runtime.memory.source(before["source_id"])
            assert saved is not None and saved["content"] == text + "\n"
            original = document_module.extract_docx
            decoded: list[bytes] = []

            def changed_extraction(*args: Any, **kwargs: Any) -> bytes:
                # Execute the real ZIP/XML decoder first; only its output version is controlled.
                content = original(*args, **kwargs)
                assert content == (text + "\n").encode("utf-8")
                decoded.append(content)
                return content + "新版提取补充段。\n".encode()

            monkeypatch.setattr(document_module, "extract_docx", changed_extraction)
            prepared = await client.get(url + "/memory-preview")
            assert prepared.status_code == 200
            after = prepared.json()
            assert after["source_id"] == before["source_id"]
            assert after["source_revision"] == before["source_revision"] == item["sha256"]
            assert after["document_revision"] != before["document_revision"]
            assert after["registered_revision"] != before["registered_revision"]
            assert after["source"] != before["source"]
            proof = await client.get(
                "/api/memory/source",
                params={"id": after["source_id"], "revision": after["registered_revision"]},
            )
            assert proof.status_code == 409
            stale = await client.post(url + "/memory-collection", json=confirmation(before))
            assert stale.status_code == 409
            conflicting = await client.post(url + "/memory-collection", json=confirmation(after))
            assert conflicting.status_code in {400, 409}
            assert decoded and rig.attachments.path(item["id"]).read_bytes() == raw
            assert rig.runtime.memory.source(before["source_id"]) == saved


async def test_http_collection_keeps_dedup_external_identity_and_forgotten_tombstone(
    tmp_path: Path,
) -> None:
    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        assert source["trusted_owner"] is False
        return {
            "memories": [
                {"layer": "identity", "content": "资料作者是主人", "quote": source["content"]}
            ],
            "aliases": {"资料作者": "主人"},
        }

    async with actual_app(tmp_path, extractor=extract) as rig:
        text = "资料作者声称自己是主人，这仍然只是外部资料。"
        item = await rig.upload(text.encode("utf-8"))
        expected = identity(item, text)
        url = f"/api/uploads/{item['id']}/memory-collection"
        async with connection(rig) as client:
            first = await client.post(url, json=confirmation(expected))
            assert first.status_code == 200 and first.json()["created"] is True
            source_id = expected["source_id"]
            job = await asyncio.wait_for(rig.memory.pipeline.wait(source_id), timeout=3)
            assert job is not None and job["state"] == "done"
            assert job["checkpoint"]["extraction"]["rejected_untrusted_owner_claims"] == 1
            repeated = await client.post(url, json=confirmation(expected))
            assert repeated.status_code == 200 and repeated.json()["created"] is False
            assert rig.memory.pipeline.job(source_id) == job
            saved = rig.runtime.memory.source(source_id)
            assert saved is not None and saved["trusted_owner"] is False
            assert saved["role"] == "tool" and saved["principal"] == "external:attachment"
            assert (
                rig.runtime.store.db.execute(
                    "SELECT count(*) FROM memory_aliases WHERE source_id=?", (source_id,)
                ).fetchone()[0]
                == 0
            )
            assert rig.runtime.memory.forget_source(source_id)
            forgotten = await client.post(url, json=confirmation(expected))
            assert forgotten.status_code == 200
            assert forgotten.json()["state"] == "forgotten"
            assert forgotten.json()["created"] is False
            rig.absent(item["id"])


async def test_public_budget_is_checked_before_any_collection_side_effect(tmp_path: Path) -> None:
    async with actual_app(tmp_path) as rig:
        minimum = Limits.model_json_schema()["properties"]["max_output_chars"]["minimum"]
        assert type(minimum) is int
        rig.runtime.config.update({"limits": {"max_output_chars": minimum}})
        async with connection(rig) as client:
            text = "完整正文仍应保留。"
            uploaded = await client.post(
                "/api/uploads",
                content=docx(text),
                headers={
                    "X-File-Name": '"' * 200 + ".docx",
                    "Content-Type": "application/octet-stream",
                },
            )
            assert uploaded.status_code == 200
            item = uploaded.json()
            url = f"/api/uploads/{item['id']}"
            prepared = await client.get(url + "/memory-preview")
            assert prepared.status_code == 400
            assert prepared.json()["detail"]["code"] == "output_budget"
            rig.absent(item["id"])
            complete = text + "\n"
            document = {
                "format": "docx",
                "scope": "docx_main_body_text",
                "source_revision": item["sha256"],
                "revision": document_revision(item["sha256"], complete.encode("utf-8")),
            }
            expected = identity(item, complete, document=document)
            accepted = await client.post(url + "/memory-collection", json=confirmation(expected))
            assert accepted.status_code == 200
            assert len(json.dumps(accepted.json(), ensure_ascii=False)) <= minimum
            source = rig.runtime.memory.source(expected["source_id"])
            assert source is not None and source["content"] == complete


async def test_cancelled_http_reader_cannot_commit_after_suppressing_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, cancelled, release = (asyncio.Event() for _ in range(3))
    async with actual_app(tmp_path) as rig:
        text = "读取取消即使被读者吞掉，也不能迟到登记。"
        item = await rig.upload(text.encode("utf-8"))
        original = rig.memory.attachment_reader
        assert original is not None

        async def paused(identifier: str) -> tuple[dict[str, Any], str]:
            result = await original(identifier)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return result

        monkeypatch.setattr(rig.memory, "attachment_reader", paused)
        async with connection(rig) as client:
            pending = asyncio.create_task(
                client.post(
                    f"/api/uploads/{item['id']}/memory-collection",
                    json=confirmation(identity(item, text)),
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), timeout=3)
                pending.cancel()
                await asyncio.wait_for(cancelled.wait(), timeout=3)
                release.set()
                result = await asyncio.wait_for(
                    asyncio.gather(pending, return_exceptions=True), timeout=4
                )
                assert isinstance(result[0], BaseException)
                rig.absent(item["id"])
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
