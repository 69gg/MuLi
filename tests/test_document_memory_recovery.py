"""Recover an authenticated document source after an actual episodic rollback."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, NoReturn

import pytest
from test_attachment_memory_policy import actual_app
from test_attachment_memory_policy import offline as offline
from test_document_memory import tool
from test_document_reading import docx


async def test_document_source_gap_recovers_without_original_once_and_stays_forgotten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_entered, owner_release = asyncio.Event(), asyncio.Event()
    extracted: list[dict[str, Any]] = []
    text = "DOCX-EPISODE-GAP:完整文档正文🙂。"
    content = text + "\n"

    async def no_process(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("document source recovery must not run a converter")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_process)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", no_process)

    async def slow_owner(source: dict[str, Any]) -> dict[str, Any]:
        if source["id"].startswith("attachment:"):
            extracted.append(dict(source))
        else:
            owner_entered.set()
            await owner_release.wait()
        return {"memories": [], "aliases": {}}

    async with actual_app(tmp_path, extractor=slow_owner) as rig:
        item = await rig.upload(docx(text))
        source_id = "attachment:" + item["id"]
        page = await tool(rig, "document_read", id=item["id"])
        assert page["content"] == content and page["has_more"] is False
        await asyncio.wait_for(owner_entered.wait(), timeout=3)
        errors: list[sqlite3.IntegrityError] = []
        original = rig.runtime.memory.save

        def observe_save(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return original(*args, **kwargs)
            except sqlite3.IntegrityError as error:
                if kwargs.get("source_id") == source_id:
                    errors.append(error)
                raise

        # The episode normalizes surrounding whitespace; its source retains
        # the extraction verbatim, including the final paragraph newline.
        literal = "'" + content.strip().replace("'", "''") + "'"
        with rig.runtime.store.db:
            rig.runtime.store.db.execute(
                "CREATE TRIGGER reject_document_episode BEFORE INSERT ON memories "
                f"WHEN NEW.content={literal} "
                "BEGIN SELECT RAISE(ABORT,'controlled document episode interruption'); END"
            )
        try:
            with monkeypatch.context() as patch:
                patch.setattr(rig.runtime.memory, "save", observe_save)
                result = await tool(
                    rig, "memory_import_attachment", id=item["id"], revision=page["revision"]
                )
            assert "error" in result and len(errors) == 1
            assert not owner_release.is_set() and extracted == []
            source = rig.runtime.memory.source(source_id)
            assert source is not None and source["content"] == content
            assert source["role"] == "tool" and source["principal"] == "external:attachment"
            assert source["trusted_owner"] is False and source["session_id"] == ""
            marker = ":".join(
                (
                    source_id,
                    "document",
                    "v1",
                    page["format"],
                    page["scope"],
                    page["source_revision"],
                    page["revision"],
                )
            )
            assert source["source"] == marker
            assert page["format"] == "docx" and page["scope"] == "docx_main_body_text"
            assert page["source_revision"] == item["sha256"]
            assert rig.runtime.memory.automatic_episode_id(source_id) is None
            assert rig.memory.pipeline.job(source_id) is None
            path = rig.attachments.path(item["id"])
            path.unlink()
            assert not path.exists()
        finally:
            with rig.runtime.store.db:
                rig.runtime.store.db.execute("DROP TRIGGER reject_document_episode")
            # Keep the real background worker at its owner-source await. Normal
            # application close cancels that work before the next lifecycle.

    async def record(source: dict[str, Any]) -> dict[str, Any]:
        if source["id"].startswith("attachment:"):
            assert source["id"] == source_id and source["content"] == content
            assert source["source"] == marker
            assert source["role"] == "tool" and source["principal"] == "external:attachment"
            assert source["trusted_owner"] is False and source["session_id"] == ""
            extracted.append(dict(source))
        return {"memories": [], "aliases": {}}

    async with actual_app(tmp_path, extractor=record) as reopened:
        assert not path.exists()
        assert reopened.runtime.memory.source(source_id) == source
        done = await asyncio.wait_for(reopened.memory.pipeline.wait(source_id), timeout=3)
        assert done is not None and done["state"] == "done"
        episode_id = reopened.runtime.memory.automatic_episode_id(source_id)
        assert episode_id is not None
        assert extracted == [source]
        evidence = await tool(reopened, "memory_source", id=source_id)
        assert evidence["content"] == content and evidence["has_more"] is False
        assert evidence["source"] == marker and evidence["trusted_owner"] is False
        assert (
            reopened.runtime.store.db.execute(
                "SELECT count(*) FROM memory_meta WHERE source_id=? AND dedup_key=?",
                (source_id, "episodic:" + source_id),
            ).fetchone()[0]
            == 1
        )
        assert (
            reopened.runtime.store.db.execute(
                "SELECT count(*) FROM memory_jobs WHERE source_id=?", (source_id,)
            ).fetchone()[0]
            == 1
        )

    async with actual_app(tmp_path, extractor=record) as again:
        assert not path.exists()
        assert again.runtime.memory.source(source_id) == source
        assert again.runtime.memory.automatic_episode_id(source_id) == episode_id
        assert again.memory.pipeline.job(source_id) == done
        assert extracted == [source]
        assert again.runtime.memory.delete(episode_id)
        again.absent(item["id"])

    async with actual_app(tmp_path, extractor=record) as forgotten:
        assert not path.exists()
        forgotten.absent(item["id"])
        assert extracted == [source]
        assert (
            forgotten.runtime.store.db.execute(
                "SELECT count(*) FROM memory_forgotten_sources WHERE id=?", (source_id,)
            ).fetchone()[0]
            == 1
        )
