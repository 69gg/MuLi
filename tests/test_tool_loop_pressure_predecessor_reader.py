"""objects4 准入必须阻止真实旧投影遗漏同一 run 的原始输入。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sys
from dataclasses import replace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_compaction_objects_format import frozen_e141_reader
from test_outbox_cancellation_format import frozen_e144_module
from test_tool_loop_pressure import add_batch, configured, seeded

from muli.context import context_size
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE,
    PRE_OUTBOX_CANCELLATION_FORMAT_STATE,
)
from muli.format_evidence import read_format_evidence
from muli.store import Store


def frozen_e141_compaction(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """执行 Good 2.141 候选的完整冻结源码，不伪造旧包或构造旧 Runtime。"""
    path = Path(__file__).parent / "fixtures" / "previous_compaction_v2_141.py.txt"
    raw = path.read_bytes()
    assert len(raw) == 17101
    assert hashlib.sha256(raw).hexdigest() == (
        "d12889bb347e072ae552c686714b22f5ea68581623a3050f5958ca9fcf4a7904"
    )
    module_name = "muli_frozen_compaction_v2_141_projection_diagnostic"
    loader = SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # 冻结模块有 dataclass 与延迟注解，执行前必须注册其真实模块别名。
    monkeypatch.setitem(sys.modules, module_name, module)
    loader.exec_module(module)
    return module


class ReadOnlyProjectionStore:
    """只委托真实新版 Store 的读取，不给旧方法任何写入或工具入口。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    def get(self, kind: str, identifier: str) -> dict[str, Any] | None:
        return self._store.get(kind, identifier)

    def replay_messages(self, session_id: str, *, after: int) -> list[dict[str, Any]]:
        return self._store.replay_messages(session_id, after=after)

    def message_prefix(self, session_id: str, *, after: int, limit: int) -> list[dict[str, Any]]:
        return self._store.message_prefix(session_id, after=after, limit=limit)


class ProjectionDiagnostic:
    """仅供明确绕过准入后的旧 project 调用；不运行旧 adopt。"""

    def __init__(self, store: Store) -> None:
        self.store = ReadOnlyProjectionStore(store)
        self.adopt_calls: list[str] = []

    def adopt(self, session_id: str) -> bool:
        self.adopt_calls.append(session_id)
        return False


@pytest.mark.parametrize(
    "suffix_has_user",
    [True, False],
    ids=["prefix-u1-suffix-u2", "prefix-u1-u2-no-suffix-user"],
)
async def test_actual_predecessor_projection_loses_input_only_after_bypassing_objects4_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix_has_user: bool
) -> None:
    runtime = await configured(tmp_path)
    try:
        session_id, first = seeded(runtime, batches=4 if suffix_has_user else 1)
        if not suffix_has_user:
            second = runtime.store.add_message(
                session_id,
                "user",
                "同轮中段补充：核完失败回执再继续。",
                source="web",
                principal="owner",
                run_id="same-run",
            )
            for index in range(1, 5):
                add_batch(runtime.store, session_id, index)

        session = runtime.store.get("session", session_id)
        assert session is not None
        runtime.store.put("session", {**session, "status": "running"})
        runtime.config.settings.compaction.trigger_ratio = 0.5
        runtime.compaction.observe(session_id)
        await asyncio.gather(*list(runtime.compaction._tasks.values()))
        assert runtime.compaction.status(session_id)["ready"]
        assert runtime.store.get("compaction", session_id) is None
        ready = runtime.compaction._ready[session_id]
        assert not ready.requires_preview

        if suffix_has_user:
            second = runtime.store.add_message(
                session_id,
                "user",
                "同轮补充：不得覆盖最初的禁止部署要求。",
                source="web",
                principal="owner",
                run_id="same-run",
            )
        originals = runtime.store.messages(session_id)
        request = await runtime._prepare_request(session_id)
        active = runtime.store.get("compaction", session_id)
        assert active is not None and active["generation"] == 1
        assert active["snapshot_id"] == ready.snapshot_id
        assert first["seq"] <= active["covered_seq"]
        assert first in active["anchors"]
        suffix = runtime.store.replay_messages(session_id, after=active["covered_seq"])
        assert any(item["role"] == "user" for item in suffix) is suffix_has_user
        if suffix_has_user:
            assert second["seq"] > active["covered_seq"]
            assert active["anchor"] == first
            assert second in suffix
        else:
            assert second["seq"] <= active["covered_seq"]
            assert active["anchors"] == [first, second]
            assert active["anchor"] == second

        for item in (first, second):
            matches = [
                message for message in request.messages if message.source_message_id == item["id"]
            ]
            assert len(matches) == 1 and matches[0].content == item["content"]
            assert matches[0].role == "user"
        assert context_size(request.messages) <= runtime.config.settings.limits.max_context_chars
        assert runtime.run_ids[session_id] == "same-run"
        assert runtime.store.messages(session_id) == originals

        # 真实旧准入先拒绝当前 marker；正常旧启动不会走到下面的诊断。
        evidence = read_format_evidence(runtime.store.db)
        old_gate = frozen_e141_reader()
        assert evidence.state == CURRENT_FORMAT_STATE
        assert old_gate.CURRENT_FORMAT_STATE == PRE_MULTI_ANCHOR_COMPACTION_FORMAT_STATE
        historical_gate = frozen_e144_module("format_admission_v2_144.py")
        assert historical_gate.CURRENT_FORMAT_STATE == PRE_OUTBOX_CANCELLATION_FORMAT_STATE
        assert historical_gate.CURRENT_FORMAT_STATE.formats.model_dump() == {
            **old_gate.CURRENT_FORMAT_STATE.formats.model_dump(),
            "objects": 4,
        }
        assert evidence.state.formats.model_dump() == {
            **historical_gate.CURRENT_FORMAT_STATE.formats.model_dump(),
            "outbox": 4,
        }
        changes_before_gate = runtime.store.db.total_changes
        historical_evidence = replace(evidence, state=historical_gate.CURRENT_FORMAT_STATE)
        historical_gate.require_current_format(historical_evidence)
        with pytest.raises(old_gate.FormatAdmissionError, match="^store_format_unsupported$"):
            old_gate.require_current_format(historical_evidence)
        with pytest.raises(old_gate.FormatAdmissionError, match="^store_format_unsupported$"):
            old_gate.require_current_format(evidence)
        assert runtime.store.db.total_changes == changes_before_gate

        # 以下刻意绕过已拒绝的准入，只将旧原方法绑定到只读适配层。
        # 依赖当前 Store 的读取支撑，不能称为完整旧安装或旧模型实测。
        previous = frozen_e141_compaction(monkeypatch)
        diagnostic = ProjectionDiagnostic(runtime.store)
        snapshots = runtime.store.list("compaction_snapshot")
        assert len(snapshots) == 1 and snapshots[0]["id"] == active["snapshot_id"]
        changes_before_projection = runtime.store.db.total_changes
        old_projection = previous.Compactor.project(diagnostic, session_id)
        assert diagnostic.adopt_calls == [session_id]
        assert old_projection[0]["id"] == active["snapshot_id"]
        assert old_projection[0]["compaction_summary"] is True
        assert not any(item["id"] == first["id"] for item in old_projection)
        assert [item for item in old_projection if item["id"] == second["id"]] == [second]
        assert runtime.store.db.total_changes == changes_before_projection
        assert runtime.store.messages(session_id) == originals
        assert runtime.store.get("compaction", session_id) == active
        assert runtime.store.list("compaction_snapshot") == snapshots
    finally:
        await runtime.close()
