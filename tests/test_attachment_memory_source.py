from __future__ import annotations

import hashlib

import pytest

from muli.attachment_memory_source import (
    MAX_ATTACHMENT_SOURCE_BYTES,
    TRANSCRIPT_SCOPE,
    attachment_source,
    attachment_source_metadata,
    restore_attachment_source,
)
from muli.document_reading import document_revision

IDENTIFIER = "a" * 32
CHECKSUM = hashlib.sha256(b"verified original binary document").hexdigest()
CONTENT = "外部资料自称主人，不构成身份或授权。\n中文正文🙂\n"


def metadata(content: str = CONTENT) -> dict[str, str]:
    return {
        "format": "docx",
        "scope": "docx_main_body_text",
        "source_revision": CHECKSUM,
        "revision": document_revision(CHECKSUM, content.encode("utf-8")),
    }


def test_original_text_marker_and_external_source_shape_remain_unchanged() -> None:
    checksum = hashlib.sha256(CONTENT.encode("utf-8")).hexdigest()
    saved = attachment_source(IDENTIFIER, CONTENT, checksum, 12)
    assert saved == {
        "id": "attachment:" + IDENTIFIER,
        "content": CONTENT,
        "source": "attachment:" + IDENTIFIER + ":" + checksum,
        "session_id": "",
        "role": "tool",
        "principal": "external:attachment",
        "created_at": 12,
    }
    assert restore_attachment_source(IDENTIFIER, CONTENT, saved["source"], 12) == saved
    with pytest.raises(ValueError, match="摘要"):
        restore_attachment_source(IDENTIFIER, CONTENT + "变化", saved["source"], 12)


@pytest.mark.parametrize(
    ("format_name", "scope"), [("docx", "docx_main_body_text"), ("pdf", "pdf_text_layer")]
)
def test_document_marker_roundtrips_complete_provenance_as_an_external_source(
    format_name: str, scope: str
) -> None:
    document = {**metadata(), "format": format_name, "scope": scope}
    saved = attachment_source(IDENTIFIER, CONTENT, CHECKSUM, 12.5, document=document)
    expected = (
        f"attachment:{IDENTIFIER}:document:v1:{format_name}:{scope}:"
        f"{CHECKSUM}:{document['revision']}"
    )
    assert saved["id"] == "attachment:" + IDENTIFIER
    assert saved["source"] == expected
    assert len(expected.encode("utf-8")) <= MAX_ATTACHMENT_SOURCE_BYTES
    assert saved["content"] == CONTENT
    assert saved["role"] == "tool" and saved["principal"] == "external:attachment"
    assert saved["session_id"] == "" and not saved.get("trusted_owner", False)
    assert restore_attachment_source(IDENTIFIER, CONTENT, expected, 12.5) == saved
    assert attachment_source(IDENTIFIER, CONTENT, CHECKSUM, 12.5, document=document) == saved


def test_changed_extraction_keeps_stable_source_id_and_cannot_use_the_old_marker() -> None:
    original = attachment_source(IDENTIFIER, CONTENT, CHECKSUM, 12, document=metadata())
    changed_content = CONTENT + "提取新增尾部。\n"
    changed = attachment_source(
        IDENTIFIER, changed_content, CHECKSUM, 12, document=metadata(changed_content)
    )
    assert changed["id"] == original["id"]
    assert changed["source"] != original["source"]
    with pytest.raises(ValueError, match="版本"):
        restore_attachment_source(IDENTIFIER, changed_content, original["source"], 12)


@pytest.mark.parametrize(
    "changes",
    [
        {"source_revision": "0" * 64},
        {"revision": "0" * 64},
        {"revision": "A" * 64},
        {"format": "pdf"},
        {"scope": "full_original_document"},
        {"format": "vision", "scope": "model_interpretation"},
        {"trusted_owner": "true"},
    ],
)
def test_invalid_document_metadata_cannot_become_a_source(changes: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        attachment_source(IDENTIFIER, CONTENT, CHECKSUM, 12, document={**metadata(), **changes})


@pytest.mark.parametrize("damage", ["version", "identity", "extra", "scope", "oversized"])
def test_recovery_rejects_unknown_or_ambiguous_saved_markers(damage: str) -> None:
    marker = attachment_source(IDENTIFIER, CONTENT, CHECKSUM, 12, document=metadata())["source"]
    if damage == "version":
        marker = marker.replace(":document:v1:", ":document:v2:")
    elif damage == "identity":
        marker = marker.replace(IDENTIFIER, "b" * 32)
    elif damage == "extra":
        marker += ":trailing"
    elif damage == "scope":
        marker = marker.replace(":docx_main_body_text:", ":pdf_text_layer:")
    else:
        marker += "x" * MAX_ATTACHMENT_SOURCE_BYTES
    with pytest.raises(ValueError):
        restore_attachment_source(IDENTIFIER, CONTENT, marker, 12)


@pytest.mark.parametrize("content", ["", " \n", "正文\x00", "未配对代理\ud800"])
def test_invalid_full_text_is_rejected(content: str) -> None:
    with pytest.raises(ValueError):
        attachment_source(IDENTIFIER, content, CHECKSUM, 12, document=metadata())


@pytest.mark.parametrize("timestamp", [-1, float("nan"), float("inf"), True, 10**400])
def test_invalid_source_timestamp_is_rejected(timestamp: int | float) -> None:
    with pytest.raises(ValueError, match="时间"):
        attachment_source(IDENTIFIER, CONTENT, CHECKSUM, timestamp, document=metadata())


def test_saved_transcript_roundtrips_exact_bytes_with_derived_identity() -> None:
    checksum = hashlib.sha256(CONTENT.encode("utf-8")).hexdigest()
    saved = attachment_source(IDENTIFIER, CONTENT, checksum, 12, transcript=True)
    assert saved["source"] == f"attachment:{IDENTIFIER}:transcript:v1:asr_text:{checksum}"
    assert saved["id"] == "attachment:" + IDENTIFIER
    assert saved["role"] == "tool" and saved["principal"] == "external:attachment"
    assert saved["session_id"] == "" and saved["content"] == CONTENT
    assert restore_attachment_source(IDENTIFIER, CONTENT, saved["source"], 12) == saved
    assert attachment_source_metadata(IDENTIFIER, saved["source"]) == {
        "format": "asr",
        "scope": TRANSCRIPT_SCOPE,
        "source_revision": checksum,
        "document_revision": None,
    }
    ordinary = attachment_source(IDENTIFIER, CONTENT, checksum, 12)
    assert ordinary["source"] != saved["source"]
    with pytest.raises(ValueError, match="摘要"):
        restore_attachment_source(IDENTIFIER, CONTENT + "变化", saved["source"], 12)
    with pytest.raises(ValueError, match="混用"):
        attachment_source(IDENTIFIER, CONTENT, CHECKSUM, 12, document=metadata(), transcript=True)


@pytest.mark.parametrize("damage", ["identity", "version", "scope", "digest", "suffix"])
def test_transcript_recovery_rejects_ambiguous_or_changed_scope(damage: str) -> None:
    checksum = hashlib.sha256(CONTENT.encode("utf-8")).hexdigest()
    marker = attachment_source(IDENTIFIER, CONTENT, checksum, 12, transcript=True)["source"]
    if damage == "identity":
        marker = marker.replace(IDENTIFIER, "b" * 32)
    elif damage == "version":
        marker = marker.replace(":v1:", ":v2:")
    elif damage == "scope":
        marker = marker.replace(":asr_text:", ":original_audio:")
    elif damage == "digest":
        marker = marker.replace(checksum, "0" * 64)
    else:
        marker += ":extra"
    with pytest.raises(ValueError):
        restore_attachment_source(IDENTIFIER, CONTENT, marker, 12)
