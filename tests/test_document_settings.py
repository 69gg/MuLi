from __future__ import annotations

import pytest
from test_vision_archives_policy import Host
from test_vision_archives_policy import host as host

from muli.document_settings import DocumentSettings, DocumentSettingsStore
from muli.evidence import EvidenceError


async def test_main_configures_hot_policy_without_rewriting_the_legacy_config(host: Host) -> None:
    config = host.runtime.config.path.read_bytes()
    default = await host.drive("document_settings_read")
    assert default["settings"]["max_xml_depth"] == 128
    changed = await host.drive("document_configure", settings={"max_xml_depth": 64})
    assert changed["settings"]["max_xml_depth"] == 64
    assert DocumentSettingsStore(host.runtime.store).read().max_xml_depth == 64
    assert await host.drive("document_settings_read") == changed
    assert host.runtime.config.path.read_bytes() == config
    assert "documents" not in host.runtime.config.settings.model_dump()


async def test_configure_requires_a_real_main_tool_context(host: Host) -> None:
    policy = DocumentSettingsStore(host.runtime.store)
    before = policy.read()
    with pytest.raises(PermissionError, match="主沐璃"):
        await host.call("document_configure", settings={"max_xml_depth": 64})
    assert policy.read() == before


@pytest.mark.parametrize("malformed", ["future", "oversized"])
async def test_unknown_or_unreadable_policy_is_not_overwritten(host: Host, malformed: str) -> None:
    item = {
        "id": "current",
        "schema_version": 2 if malformed == "future" else 1,
        "settings": {"pdf_command": ["x" * 20000]} if malformed == "oversized" else {},
    }
    host.runtime.store.put("document_settings", item)
    before = host.runtime.store.get("document_settings", "current")
    with pytest.raises(EvidenceError) as caught:
        DocumentSettingsStore(host.runtime.store).replace(DocumentSettings())
    assert caught.value.code == "document_settings_unavailable"
    assert host.runtime.store.get("document_settings", "current") == before


async def test_output_budget_failure_precedes_policy_commit(host: Host) -> None:
    policy = DocumentSettingsStore(host.runtime.store)
    before = policy.read()
    result = await host.drive("document_configure", settings={"pdf_command": ["x" * 2000]})
    assert "output_budget" in str(result) or "输出预算" in str(result)
    assert policy.read() == before
