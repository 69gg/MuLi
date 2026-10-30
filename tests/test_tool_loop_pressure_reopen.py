"""Committed layers survive reopen and remain eligible for the next checkpoint."""

from __future__ import annotations

from pathlib import Path

from test_tool_loop_pressure import add_batch, configured, fill_required_context, seeded

from muli.context import context_size
from muli.runtime import Runtime


async def test_pressure_layers_survive_close_reopen_and_later_pressure(tmp_path: Path) -> None:
    runtime = await configured(tmp_path)
    reopened: Runtime | None = None
    try:
        session_id, user = seeded(runtime)
        fill_required_context(runtime, session_id)
        await runtime._prepare_request(session_id)
        first = runtime.store.get("compaction", session_id)
        assert first is not None
        for index in range(4, 8):
            add_batch(runtime.store, session_id, index)
        await runtime._prepare_request(session_id)
        second = runtime.store.get("compaction", session_id)
        assert second is not None
        assert second["covered_seq"] > first["covered_seq"]
        assert second["generation"] == first["generation"] + 1
        assert second["parent_snapshot_id"] == first["snapshot_id"]
        records = runtime.store.messages(session_id)
        snapshots = runtime.store.list("compaction_snapshot")
        await runtime.close()

        reopened = Runtime(runtime.config, runtime.data_dir)
        assert reopened.store.messages(session_id) == records
        assert reopened.store.list("compaction_snapshot") == snapshots
        assert reopened.store.get("compaction", session_id) == second
        assert reopened.store.get("message", user["id"]) == user
        # No replay/model/tool operation is needed to reload the durable layer.
        request = await reopened._prepare_request(session_id)
        assert context_size(request.messages) <= reopened.config.settings.limits.max_context_chars
        assert reopened.store.messages(session_id) == records
        for index in range(8, 12):
            add_batch(reopened.store, session_id, index)
        fill_required_context(reopened, session_id)
        request = await reopened._prepare_request(session_id)
        third = reopened.store.get("compaction", session_id)
        assert third is not None
        assert third["covered_seq"] > second["covered_seq"]
        assert third["parent_snapshot_id"] == second["snapshot_id"]
        assert third["generation"] == second["generation"] + 1
        assert context_size(request.messages) <= reopened.config.settings.limits.max_context_chars
        assert reopened.store.get("message", user["id"]) == user
        assert reopened.store.messages(session_id)[: len(records)] == records
    finally:
        if reopened is not None:
            await reopened.close()
        await runtime.close()
