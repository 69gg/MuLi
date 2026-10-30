"""Find host-recovered main runs through the original, owner-driven reader.

The persisted running boundary is a fixture, not an OS-kill experiment.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_history_scope_runtime import configured, run_tool, settled
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, tool_result

from muli.config import ConfigFile
from muli.evidence import json_size
from muli.history_evidence import HistoryEvidence
from muli.history_find import HistoryFind
from muli.providers import Message, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.store import Store, new_id


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        raise AssertionError(f"主运行恢复证据不得请求外部 HTTP：{request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_http)


def recovered_input(
    runtime: Runtime, *, content: str = "原文甲", principal: str = "owner", source: str = "web"
) -> tuple[str, dict[str, Any], int]:
    """Use the original claimer and recovery, before a new actual owner run."""
    main_id, run_id = runtime.main_session()["id"], new_id()
    runtime.store.put("run", {"id": run_id, "session_id": main_id, "status": "running"})
    runtime.store.enqueue_input(
        {
            "id": new_id(),
            "session_id": main_id,
            "source": source,
            "principal": principal,
            "content": content,
            "attachment_ids": [],
        },
        max_pending=4,
    )
    claimed = runtime.store.claim_inputs(main_id, run_id, limit=4)
    assert len(claimed) == 1 and claimed[0]["run_id"] == run_id
    runtime._recover()
    event_id = runtime.store.event_position()
    event = runtime.store.events(after=event_id - 1, session_id=main_id)
    assert len(event) == 1 and event[0]["data"]["run_id"] == run_id
    assert runtime.store.get("run", run_id)["status"] == "interrupted"
    return run_id, claimed[0], event_id


async def read_recovery(runtime: Runtime, **arguments: Any) -> dict[str, Any]:
    return await run_tool(
        runtime,
        "conversation_read",
        {"session_id": runtime.main_session()["id"], "section": "main_recovery", **arguments},
    )


def recovery_items(page: dict[str, Any]) -> list[dict[str, Any]]:
    assert "error" not in page, page
    assert set(page) == {
        "items",
        "omitted",
        "has_more",
        "next_cursor",
        "consistency",
        "side_effect_status",
    }
    assert page["consistency"] == "live" and page["side_effect_status"] == "unknown"
    assert type(page["omitted"]) is int and page["omitted"] >= 0
    assert type(page["has_more"]) is bool and json_size(page) <= 6000
    items = page["items"]
    assert isinstance(items, list)
    for item in items:
        assert set(item) == {"run_id", "status", "recovery_event_id", "recovered_at", "first_input"}
        assert item["status"] == "interrupted"
        assert item["first_input"]["scope"] == "start_only"
    return items


async def test_owner_discovers_claimed_main_run_after_real_host_recovery(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "workspace_roots": [str(tmp_path)],
            "persona": {"prompt": "只核查已保存资料，不继续旧执行。"},
            "compaction": {"enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000},
            "updates": {"check_on_start": False},
        }
    )
    store = Store.open_runtime(tmp_path / "data" / "muli.sqlite3", default_mode="yolo")
    try:
        session = store.main_session("yolo")
        main_id, instance = session["id"], store.instance_id
        old_run, old_input = new_id(), new_id()
        original = "旧主运行的合成原请求：先核查快照，不发布，也不要重跑未知操作。"
        store.put("session", {**session, "status": "running"})
        store.put("run", {"id": old_run, "session_id": main_id, "status": "running"})
        accepted, inserted = store.enqueue_input(
            {
                "id": old_input,
                "session_id": main_id,
                "source": "web",
                "principal": "owner",
                "content": original,
                "attachment_ids": [],
            },
            max_pending=4,
        )
        assert inserted and accepted["state"] == "pending"
        claimed = store.claim_inputs(main_id, old_run, limit=4)
        assert len(claimed) == 1
        archived = claimed[0]
        assert archived["id"] == old_input and archived["run_id"] == old_run
        assert archived["input_seq"] == accepted["seq"]
        assert archived["content"] == original
        child_id, child_run = new_id(), new_id()
        store.put("run", {"id": child_run, "session_id": child_id, "status": "cancelled"})
        store.put(
            "agent",
            {
                "id": child_id,
                "session_id": child_id,
                "parent_id": main_id,
                "status": "interrupted",
                "run_id": child_run,
            },
        )
        before_recovery = store.event_position()
    finally:
        store.close()

    runtime, _ = make_runtime(tmp_path, [])
    catalog: dict[str, Any] = {}
    positives: dict[str, dict[str, Any]] = {}
    rounds = 0
    current_runs: list[str] = []

    class Reader(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            assert rounds <= 3
            if rounds == 1:
                current = runtime.run_ids[main_id]
                current_runs.append(current)
                assert current != old_run
                assert runtime.tasks[main_id] is asyncio.current_task()
                assert runtime.store.get("run", current)["status"] == "running"
                owner_input = runtime.store.db.execute(
                    "SELECT id,payload,delivered_seq FROM ingress "
                    "WHERE session_id=? AND run_id=? AND state='delivered' ORDER BY seq",
                    (main_id, current),
                ).fetchall()
                assert len(owner_input) == 1
                payload = json.loads(owner_input[0]["payload"])
                assert payload["principal"] == "owner" and payload["source"] == "web"
                assert (
                    runtime.store.db.execute(
                        "SELECT 1 FROM message_order WHERE message_id=? AND session_id=? AND seq=?",
                        (owner_input[0]["id"], main_id, owner_input[0]["delivered_seq"]),
                    ).fetchone()
                    is not None
                )
                functions = {item["function"]["name"] for item in tools}
                assert {"conversation_read", "history_find"} <= functions
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "old-continuations-positive",
                            "conversation_read",
                            section="continuations",
                            session_id=main_id,
                            limit=4,
                        ),
                        call("current-owner-positive", "history_find", query=original, limit=4),
                    ]
                )
            elif rounds == 2:
                positives["continuations"] = tool_result(messages, "old-continuations-positive")
                positives["owner"] = tool_result(messages, "current-owner-positive")
                assert "error" not in positives["continuations"]
                assert [item["id"] for item in positives["continuations"]["items"]] == [child_id]
                assert "error" not in positives["owner"], positives["owner"]
                assert any(item["message_id"] == old_input for item in positives["owner"]["items"])
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "discover-old-main",
                            "conversation_read",
                            section="main_recovery",
                            session_id=main_id,
                            limit=20,
                        )
                    ]
                )
            else:
                catalog.update(tool_result(messages, "discover-old-main"))
                turn = ModelTurn(content="本次只核查已保存的中断证据。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: Reader([], [])  # type: ignore[assignment,return-value]
    try:
        assert runtime.main_session()["id"] == main_id
        assert runtime.store.instance_id == instance
        assert runtime.store.get("run", old_run)["status"] == "interrupted"
        recovered = [
            event
            for event in runtime.store.events(after=before_recovery, session_id=main_id)
            if event["type"] == "session.recovered" and event["data"]["kind"] == "run"
        ]
        assert len(recovered) == 1
        event = recovered[0]
        assert event["data"]["object_id"] == event["data"]["run_id"] == old_run
        ingress = runtime.store.db.execute(
            "SELECT state,run_id,delivered_seq FROM ingress WHERE id=?", (old_input,)
        ).fetchone()
        assert tuple(ingress) == ("delivered", old_run, archived["seq"])
        assert not runtime.tasks
        receipt = await runtime.submit("上次你自己做到哪了？先查证据，不要继续。", mode="yolo")
        assert (
            await asyncio.wait_for(runtime.wait(receipt["run_id"]), 5)
            == "本次只核查已保存的中断证据。"
        )
        assert rounds == 3 and current_runs == [receipt["run_id"]]
        assert set(positives) == {"continuations", "owner"}
        assert "error" not in catalog, catalog
        assert len(catalog["items"]) == 1
        item = catalog["items"][0]
        assert item["run_id"] == old_run and item["status"] == "interrupted"
        assert item["recovery_event_id"] == event["id"]
        assert item["recovered_at"] == event["created_at"]
        first_input = item["first_input"]
        assert first_input["availability"] == "available"
        assert first_input["scope"] == "start_only"
        assert first_input["message_id"] == old_input
        assert first_input["seq"] == archived["seq"]
        assert first_input["revision"] == archived["evidence_revision"]
        assert first_input["source"] == "web" and first_input["principal"] == "owner"
        assert first_input["read_arguments"] == {
            "session_id": main_id,
            "message_id": old_input,
            "revision": archived["evidence_revision"],
            "offset_bytes": 0,
        }
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("content", "原文乙"),
        ("principal", "guest"),
        ("source", "api"),
        ("attachment_ids", ["changed-reference"]),
        ("route", {"kind": "private", "target": "different"}),
        ("input_seq", True),
        ("run_id", "wrong-run"),
    ],
)
async def test_first_discovery_rejects_archive_rewrites_without_an_old_revision_token(
    tmp_path: Path, field: str, replacement: Any
) -> None:
    runtime = await configured(tmp_path)
    try:
        run_id, archived, _ = recovered_input(runtime)
        original_ingress = runtime.store.db.execute(
            "SELECT payload FROM ingress WHERE id=?", (archived["id"],)
        ).fetchone()[0]
        if field in {"content", "source", "principal"}:
            assert len(archived[field].encode("utf-8")) == len(replacement.encode("utf-8"))
        rewritten = runtime.store.put("message", {**archived, field: replacement})
        assert rewritten["evidence_revision"] != archived["evidence_revision"]
        # This is the first catalog read. Its new revision cannot prove that
        # the rewritten archive still represents what this run claimed.
        page = await read_recovery(runtime)
        items = recovery_items(page)
        assert len(items) == 1 and items[0]["run_id"] == run_id
        first = items[0]["first_input"]
        assert first["availability"] == "unavailable"
        assert "read_arguments" not in first and "message_id" not in first
        assert (
            runtime.store.db.execute(
                "SELECT payload FROM ingress WHERE id=?", (archived["id"],)
            ).fetchone()[0]
            == original_ingress
        )
        assert (
            runtime.store.get("message", archived["id"])["evidence_revision"]
            == rewritten["evidence_revision"]
        )
    finally:
        await runtime.close()


async def test_historical_system_input_keeps_its_real_source_and_principal(tmp_path: Path) -> None:
    runtime = await configured(tmp_path)
    try:
        run_id, archived, _ = recovered_input(
            runtime, principal="system:attention", source="system:attention"
        )
        items = recovery_items(await read_recovery(runtime))
        assert len(items) == 1 and items[0]["run_id"] == run_id
        first = items[0]["first_input"]
        assert first["availability"] == "available"
        assert first["source"] == first["principal"] == "system:attention"
        original = await run_tool(runtime, "conversation_read", first["read_arguments"])
        assert original["content"] == archived["content"]
        assert original["principal"] == "system:attention"
    finally:
        await runtime.close()


@pytest.mark.parametrize("missing", ["ingress", "archive"])
async def test_missing_first_input_never_substitutes_a_later_claim(
    tmp_path: Path, missing: str
) -> None:
    runtime = await configured(tmp_path)
    try:
        run_id, archived, _ = recovered_input(runtime)
        main_id = runtime.main_session()["id"]
        if missing == "archive":
            runtime.store.enqueue_input(
                {
                    "id": new_id(),
                    "session_id": main_id,
                    "content": "后来的补充",
                    "source": "web",
                    "principal": "owner",
                    "attachment_ids": [],
                },
                max_pending=4,
            )
            later = runtime.store.claim_inputs(main_id, run_id, limit=1)
            assert len(later) == 1 and later[0]["seq"] > archived["seq"]
            with runtime.store.db:
                runtime.store.db.execute(
                    "DELETE FROM objects WHERE kind='message' AND id=?", (archived["id"],)
                )
        else:
            with runtime.store.db:
                runtime.store.db.execute("DELETE FROM ingress WHERE id=?", (archived["id"],))
        items = recovery_items(await read_recovery(runtime))
        assert len(items) == 1 and items[0]["run_id"] == run_id
        assert items[0]["first_input"] == {
            "availability": "unavailable",
            "scope": "start_only",
            "reason": "missing",
        }
    finally:
        await runtime.close()


@pytest.mark.parametrize("broken", ["event-object", "run-session", "run-id", "run-status"])
async def test_recovery_event_and_actual_main_run_must_still_agree(
    tmp_path: Path, broken: str
) -> None:
    runtime = await configured(tmp_path)
    try:
        run_id, _archived, event_id = recovered_input(runtime)
        if broken == "event-object":
            with runtime.store.db:
                runtime.store.db.execute(
                    "UPDATE events SET data=? WHERE id=?",
                    (json.dumps({"kind": "run", "object_id": "wrong", "run_id": run_id}), event_id),
                )
        else:
            run = runtime.store.get("run", run_id)
            assert run is not None
            changes = {
                "run-session": {"session_id": "unrelated-session"},
                "run-id": {"id": "not-the-stored-key"},
                "run-status": {"status": "failed"},
            }[broken]
            # A bad stored-key fixture cannot be made with Store.put: that
            # would create a different object instead of corrupting this row.
            with runtime.store.db:
                runtime.store.db.execute(
                    "UPDATE objects SET data=? WHERE kind='run' AND id=?",
                    (json.dumps({**run, **changes}), run_id),
                )
        page = await read_recovery(runtime)
        assert recovery_items(page) == [] and page["omitted"] >= 1
    finally:
        await runtime.close()


async def test_sparse_raw_event_pages_advance_and_cursors_reject_other_sections_and_stores(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path / "first")
    other = await configured(tmp_path / "second")
    try:
        run_id, _archived, _ = recovered_input(runtime)
        main_id = runtime.main_session()["id"]
        for index in range(3):
            runtime.store.event("unrelated.fixture", {"number": index}, main_id)
        first = await read_recovery(runtime, limit=1)
        assert recovery_items(first) == [] and first["has_more"]
        token = first["next_cursor"]
        assert isinstance(token, str)
        seen: list[int] = []
        found: list[str] = []
        current = first
        for _ in range(12):
            cursor = current["next_cursor"]
            assert isinstance(cursor, str)
            position = json.loads(base64.urlsafe_b64decode(cursor))[-1]
            assert type(position) is int
            if seen:
                assert position < seen[-1]
            seen.append(position)
            current = await read_recovery(runtime, limit=1, cursor=cursor)
            found.extend(item["run_id"] for item in recovery_items(current))
            if not current["has_more"]:
                break
        assert found == [run_id] and current["has_more"] is False
        cross_section = await run_tool(
            runtime,
            "conversation_read",
            {"session_id": main_id, "section": "continuations", "cursor": token},
        )
        assert cross_section.get("code") == "record_cursor_invalid"
        old_section = HistoryEvidence(runtime.store)._encode_continuation_cursor(main_id, 1)
        assert (await read_recovery(runtime, cursor=old_section)).get(
            "code"
        ) == "record_cursor_invalid"
        assert (await read_recovery(other, cursor=token)).get("code") == "record_cursor_invalid"
        for position in (True, 0, (1 << 63)):
            payload = json.loads(base64.urlsafe_b64decode(token))
            payload[-1] = position
            malformed = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
            assert (await read_recovery(runtime, cursor=malformed)).get(
                "code"
            ) == "record_cursor_invalid"
    finally:
        await other.close()
        await runtime.close()


async def test_oversized_run_and_event_never_load_their_json_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await configured(tmp_path)
    try:
        run_id, _archived, _ = recovered_input(runtime)
        main_id = runtime.main_session()["id"]
        raw = runtime.config.settings.limits.max_context_chars * 4
        run = runtime.store.get("run", run_id)
        assert run is not None
        runtime.store.put("run", {**run, "result": "private-result" * raw})
        large_event = runtime.store.event("session.recovered", {"padding": "x" * raw}, main_id)
        projected_run = runtime.store.main_recovery_run(run_id, max_bytes=raw)
        projected_event = runtime.store.main_recovery_event(
            main_id, large_event["id"], max_bytes=raw
        )
        assert projected_run is not None and projected_run["size"] > raw
        assert projected_run["metadata"] is None
        assert projected_event is not None and projected_event["size"] > raw
        assert projected_event["data"] is None and projected_event["type"] is None
        original = runtime.store.bounded_object

        def bounded(kind: str, identifier: str, **arguments: Any) -> dict[str, Any] | None:
            assert not (kind == "run" and identifier == run_id)
            return original(kind, identifier, **arguments)

        monkeypatch.setattr(runtime.store, "bounded_object", bounded)
        page = await read_recovery(runtime)
        assert recovery_items(page) == [] and page["omitted"] >= 2
        assert "private-result" not in json.dumps(page)
    finally:
        await runtime.close()


async def test_owner_metadata_and_historical_input_share_the_same_raw_budget(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    try:
        # Use the existing legal context minimum, keeping both source objects
        # below the main identity reader's independent 16 KiB ceiling.
        runtime.config.update(
            {
                "persona": {"prompt": "只核查本次已存目录。"},
                "limits": {"max_context_chars": 4000},
                "disabled_tools": sorted(
                    name for name in runtime.tools if name != "conversation_read"
                ),
            }
        )
        await runtime.reconfigure()
        run_id, archived, _ = recovered_input(runtime)
        main_id = runtime.main_session()["id"]
        raw = runtime.config.settings.limits.max_context_chars * 4
        session = runtime.main_session()
        runtime.store.put("session", {**session, "authority_padding": "x" * (raw * 5 // 8)})
        with runtime.store.db:
            row = runtime.store.db.execute(
                "SELECT payload FROM ingress WHERE id=?", (archived["id"],)
            ).fetchone()
            payload = json.loads(row[0])
            payload["unprojected_fixture_padding"] = "x" * (raw // 2)
            runtime.store.db.execute(
                "UPDATE ingress SET payload=? WHERE id=?", (json.dumps(payload), archived["id"])
            )
        authority_size = runtime.store.db.execute(
            "SELECT length(CAST(data AS BLOB)) FROM objects WHERE kind='session' AND id=?",
            (main_id,),
        ).fetchone()[0]
        input_size = runtime.store.db.execute(
            "SELECT length(CAST(payload AS BLOB)) FROM ingress WHERE id=?", (archived["id"],)
        ).fetchone()[0]
        assert authority_size < raw and input_size < raw and authority_size + input_size > raw
        assert runtime.main_session()["id"] == main_id
        items = recovery_items(await read_recovery(runtime))
        assert len(items) == 1 and items[0]["run_id"] == run_id
        assert items[0]["first_input"] == {
            "availability": "unavailable",
            "scope": "start_only",
            "reason": "raw_budget",
        }
    finally:
        await runtime.close()


@pytest.mark.parametrize("scalar", ["created_at", "received", "delivered_seq"])
async def test_bad_blob_scalars_are_not_loaded_as_recovery_metadata(
    tmp_path: Path, scalar: str
) -> None:
    runtime = await configured(tmp_path)
    try:
        run_id, archived, event_id = recovered_input(runtime)
        main_id = runtime.main_session()["id"]
        raw = runtime.config.settings.limits.max_context_chars * 4
        with runtime.store.db:
            if scalar == "created_at":
                runtime.store.db.execute(
                    "UPDATE events SET created_at=? WHERE id=?", (b"x" * (raw + 1), event_id)
                )
            else:
                runtime.store.db.execute(
                    "UPDATE ingress SET " + scalar + "=? WHERE id=?",
                    (b"x" * (raw + 1), archived["id"]),
                )
        projected = (
            runtime.store.main_recovery_event(main_id, event_id, max_bytes=raw)
            if scalar == "created_at"
            else runtime.store.main_recovery_first_input(run_id, max_bytes=raw)
        )
        assert projected is not None and projected[scalar] is None
        items = recovery_items(await read_recovery(runtime))
        if scalar == "created_at":
            assert items == []
        else:
            assert len(items) == 1 and items[0]["run_id"] == run_id
            assert items[0]["first_input"]["availability"] == "unavailable"
    finally:
        await runtime.close()


@pytest.mark.parametrize("name", ["history_find", "conversation_read"])
async def test_current_owner_blob_mapping_is_rejected_before_loading_a_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    runtime = await configured(tmp_path)
    responses: list[dict[str, Any]] = []
    sequences: list[object] = []
    original = HistoryFind._input_mapping

    def mapping(reader: HistoryFind, session_id: str, identifier: str, sequence: object) -> bool:
        assert not isinstance(sequence, bytes)
        sequences.append(sequence)
        return original(reader, session_id, identifier, sequence)

    monkeypatch.setattr(HistoryFind, "_input_mapping", mapping)
    main_id = runtime.main_session()["id"]
    rounds = 0

    class Reader(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                with runtime.store.db:
                    runtime.store.db.execute(
                        "UPDATE ingress SET delivered_seq=? WHERE run_id=? AND state='delivered'",
                        (
                            b"x" * (runtime.config.settings.limits.max_context_chars * 4 + 1),
                            runtime.run_ids[main_id],
                        ),
                    )
                arguments = (
                    {"query": "old"}
                    if name == "history_find"
                    else {"session_id": main_id, "section": "main_recovery"}
                )
                turn = ModelTurn(tool_calls=[call("invalid-current-mapping", name, **arguments)])
            else:
                assert rounds == 2
                responses.append(tool_result(messages, "invalid-current-mapping"))
                turn = ModelTurn(content="本次只核查拒绝结果。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: Reader([], [])  # type: ignore[assignment]
    try:
        receipt = await runtime.submit("核查当前映射。", mode="yolo")
        assert await asyncio.wait_for(runtime.wait(receipt["run_id"]), 5) == "本次只核查拒绝结果。"
        assert rounds == 2 and len(responses) == 1
        assert "error" in responses[0] and "items" not in responses[0]
        assert sequences and all(sequence is None for sequence in sequences)
    finally:
        await runtime.close()


async def test_main_recovery_rejects_independent_and_relayed_handler_calls(tmp_path: Path) -> None:
    from muli.tools import Tool, parameters

    runtime = await configured(tmp_path)
    try:
        recovered_input(runtime)
        arguments = {"session_id": runtime.main_session()["id"], "section": "main_recovery"}
        independent = runtime.new_session(mode="yolo")["id"]
        denied = await run_tool(runtime, "conversation_read", arguments, session_id=independent)
        assert "error" in denied and "items" not in denied

        async def relay(arguments: dict[str, Any]) -> Any:
            return await runtime.tools["conversation_read"].handler(arguments)

        runtime.add_tool_provider(
            lambda _: [
                Tool(
                    "relay_recovery",
                    "真实转发反例",
                    parameters(
                        {"session_id": {"type": "string"}, "section": {"type": "string"}},
                        ("session_id", "section"),
                    ),
                    relay,
                )
            ]
        )
        forwarded = await run_tool(runtime, "relay_recovery", arguments)
        assert "error" in forwarded and "items" not in forwarded
        with pytest.raises(PermissionError):
            await runtime.tools["conversation_read"].handler(arguments)
    finally:
        await runtime.close()


@pytest.mark.parametrize("choice", ["default", "disabled", "persona"])
async def test_main_recovery_hint_tracks_the_actual_read_tool_and_custom_persona(
    tmp_path: Path, choice: str
) -> None:
    runtime = await configured(tmp_path)
    try:
        if choice == "disabled":
            runtime.config.update({"disabled_tools": ["conversation_read"]})
        elif choice == "persona":
            runtime.config.update({"persona": {"prompt": "用户定制人格正文"}})
        await runtime.reconfigure()
        main_id = runtime.main_session()["id"]
        prompt = runtime._history_context(main_id).messages[0].content
        assert ("section=main_recovery" in prompt) is (choice == "default")
        if choice == "persona":
            assert prompt.startswith("用户定制人格正文")
    finally:
        await runtime.close()


async def test_real_teammate_and_its_main_consult_cannot_read_main_recovery(tmp_path: Path) -> None:
    runtime = await configured(tmp_path)
    main_id = runtime.main_session()["id"]
    denials: dict[str, dict[str, Any]] = {}
    rounds = {"worker": 0, "main": 0}
    worker_runs: list[str] = []
    consult_runs: list[str] = []

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if self.role == "worker":
                rounds["worker"] += 1
                child_id = next(
                    identifier
                    for identifier, task in runtime.tasks.items()
                    if task is asyncio.current_task()
                )
                assert child_id != main_id
                child = runtime.team.get(child_id)
                assert child["parent_id"] == main_id
                current = runtime.run_ids[child_id]
                if rounds["worker"] == 1:
                    worker_runs.append(current)
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "teammate-recovery-denied",
                                "conversation_read",
                                session_id=main_id,
                                section="main_recovery",
                            )
                        ]
                    )
                elif rounds["worker"] == 2:
                    assert worker_runs == [current]
                    denials["team"] = tool_result(messages, "teammate-recovery-denied")
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "real-recovery-consult",
                                "team_consult",
                                question="请只给出核查资料的建议，不扩大权限。",
                                wait_seconds=2,
                            )
                        ]
                    )
                else:
                    assert rounds["worker"] == 3 and worker_runs == [current]
                    consult = tool_result(messages, "real-recovery-consult")
                    assert "error" not in consult
                    assert consult["answer"] == "咨询只提供建议，历史发现仍需当前主人请求。"
                    assert consult["child_run_id"] == current
                    turn = ModelTurn(content="原队友只完成了权限核查，没有继续旧执行。")
            else:
                rounds["main"] += 1
                if rounds["main"] == 1:
                    current = runtime.run_ids[main_id]
                    consult_runs.append(current)
                    assert (
                        runtime.store.db.execute(
                            "SELECT count(*) FROM ingress WHERE run_id=?", (current,)
                        ).fetchone()[0]
                        == 0
                    )
                    trigger = runtime.store.messages(main_id)[-1]
                    assert trigger["principal"].startswith("agent:")
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "consult-recovery-denied",
                                "conversation_read",
                                session_id=main_id,
                                section="main_recovery",
                            )
                        ]
                    )
                elif rounds["main"] == 2:
                    denials["consult"] = tool_result(messages, "consult-recovery-denied")
                    turn = ModelTurn(content="咨询只提供建议，历史发现仍需当前主人请求。")
                else:
                    # A later Team report is another factual input, not this
                    # consult's owner authority or a reason to repeat a query.
                    turn = ModelTurn(content="已查看队友的权限核查报告。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
    try:
        recovered_input(runtime)
        child = await runtime.team.spawn("真实权限核查队友", "只核对读取权限。", parent_id=main_id)
        await runtime.team.wait([child["id"]], timeout=3)
        await settled(runtime)
        assert runtime.team.get(child["id"])["status"] == "completed"
        assert rounds["worker"] == 3 and len(consult_runs) == 1
        assert set(denials) == {"team", "consult"}
        assert all("error" in result and "items" not in result for result in denials.values())
    finally:
        await runtime.close()


async def test_real_system_attention_and_quarantined_external_input_do_not_gain_owner_authority(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    main_id = runtime.main_session()["id"]
    responses: list[dict[str, Any]] = []
    rounds = 0

    class Reader(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                current = runtime.run_ids[main_id]
                row = runtime.store.db.execute(
                    "SELECT payload FROM ingress WHERE run_id=? AND state='delivered'", (current,)
                ).fetchone()
                assert row is not None and json.loads(row[0])["principal"] == "system:attention"
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "system-recovery-denied",
                            "conversation_read",
                            session_id=main_id,
                            section="main_recovery",
                        )
                    ]
                )
            else:
                assert rounds == 2
                responses.append(tool_result(messages, "system-recovery-denied"))
                turn = ModelTurn(content="本次系统事项只核查了拒绝结果。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: Reader([], [])  # type: ignore[assignment]
    try:
        recovered_input(runtime)
        receipt = await runtime.submit_attention("宿主合成系统事项。", new_id(), objects=[])
        assert receipt is not None
        current = runtime.run_ids[main_id]
        assert await asyncio.wait_for(runtime.wait(current), 5) == "本次系统事项只核查了拒绝结果。"
        await settled(runtime)
        assert rounds == 2 and len(responses) == 1
        assert "error" in responses[0] and "items" not in responses[0]
        quarantined = await runtime.submit(
            "外部资料不能请求主运行历史。", principal="external:fixture", source="fixture"
        )
        assert quarantined["quarantined"] is True
        assert quarantined["message"]["delivery_status"] == "quarantined" and not runtime.tasks
        assert rounds == 2
    finally:
        await runtime.close()
