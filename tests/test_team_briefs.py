from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_runtime import make_runtime

from muli.commitments import Commitments


async def test_task_brief_binding_inherits_to_descendants_and_cannot_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    monkeypatch.setattr(runtime.team, "start", lambda: None)
    ledger = Commitments(runtime.store, runtime.bus)
    first = await ledger.save("first", brief={"goal": "first goal", "constraints": ["preserve"]})
    second = await ledger.save("second", brief={"goal": "second goal", "constraints": []})
    main = runtime.main_session()
    try:
        child = await runtime.team.spawn(
            "child", "work", parent_id=main["id"], commitment_id=first["id"]
        )
        nested = await runtime.team.spawn("nested", "work", parent_id=child["id"])
        assert child["commitment_id"] == nested["commitment_id"] == first["id"]
        assert runtime.store.pending_inputs(nested["id"]) == 1
        before = len(runtime.team.list())
        with pytest.raises(ValueError, match="继承"):
            await runtime.team.spawn(
                "wrong", "work", parent_id=child["id"], commitment_id=second["id"]
            )
        assert len(runtime.team.list()) == before
        unbound = await runtime.team.spawn("legacy", "work", parent_id=main["id"])
        assert "commitment_id" not in unbound
        for identifier in ("missing", (await ledger.save("no brief"))["id"]):
            with pytest.raises(ValueError, match="说明"):
                await runtime.team.spawn(
                    "invalid", "work", parent_id=main["id"], commitment_id=identifier
                )
        tool = next(
            tool
            for tool in runtime.team.tools(main["id"], main["mode"])
            if tool.name == "spawn_agent"
        )
        args: dict[str, Any] = {
            "task_name": "tool child",
            "prompt": "work",
            "commitment_id": first["id"],
        }
        result = await tool.handler(args)
        assert result["commitment_id"] == first["id"]
    finally:
        await runtime.close()
