from __future__ import annotations

import io
import stat
import time
import zipfile
from xml.sax.saxutils import escape, quoteattr

import pytest

import muli.document_xml as document_xml
from muli.candidate_policy import CandidateError, CandidateLimits
from muli.candidate_preparation import _archive
from muli.document_xml import extract_docx
from muli.evidence import EvidenceError

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
STRICT_WORD = "http://purl.oclc.org/ooxml/wordprocessingml/main"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
STRICT_OFFICE = "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"
RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


def package(
    body: str,
    *,
    namespace: str = WORD,
    relationship: str = OFFICE,
    target: str = "custom/body.xml",
    mode: str = "Internal",
    content_type: str = MAIN_TYPE,
    extra: tuple[tuple[str | zipfile.ZipInfo, bytes], ...] = (),
    raw_main: bytes | None = None,
) -> bytes:
    """Build local package bytes with a deliberately nondefault main part."""
    root = (
        f'<Relationships xmlns="{RELATIONSHIPS}">'
        f'<Relationship Id="main" Type={quoteattr(relationship)} '
        f"Target={quoteattr(target)} TargetMode={quoteattr(mode)}/>"
        '<Relationship Id="ignored" Type="urn:fixture:hyperlink" '
        'Target="https://invalid.example/never-follow" TargetMode="External"/>'
        "</Relationships>"
    )
    types = (
        f'<Types xmlns="{CONTENT_TYPES}">'
        f'<Override PartName="/custom/body.xml" ContentType={quoteattr(content_type)}/>'
        "</Types>"
    )
    main = f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'.encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("_rels/.rels", root.encode())
        archive.writestr("[Content_Types].xml", types.encode())
        archive.writestr("custom/body.xml", main if raw_main is None else raw_main)
        for name, data in extra:
            archive.writestr(name, data)
    return output.getvalue()


def extract(data: bytes, *, maximum: int = 16_384, entries: int = 128, depth: int = 128) -> bytes:
    return extract_docx(
        data,
        max_bytes=maximum,
        max_entries=entries,
        max_depth=depth,
        deadline=time.monotonic() + 30,
    )


def rejected(data: bytes, code: str, *, maximum: int = 16_384, depth: int = 128) -> None:
    with pytest.raises(EvidenceError) as raised:
        extract(data, maximum=maximum, depth=depth)
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("namespace", "relationship", "target"),
    [(WORD, OFFICE, "custom/./body.xml"), (STRICT_WORD, STRICT_OFFICE, "/custom/body.xml")],
)
def test_main_part_relation_namespaces_and_plain_paragraph(
    namespace: str, relationship: str, target: str
) -> None:
    text = "中文与 emoji 🐈，转义 <>& 保留"
    data = package(
        f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>",
        namespace=namespace,
        relationship=relationship,
        target=target,
    )
    assert extract(data) == (text + "\n").encode()


def test_paragraph_table_separators_and_deleted_moved_nonbody_text() -> None:
    data = package(
        "<w:p><w:r><w:t>开场</w:t><w:tab/><w:t>后</w:t><w:br/><w:t>行</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc>"
        "<w:p><w:r><w:t>甲</w:t></w:r></w:p><w:p><w:r><w:t>乙</w:t></w:r></w:p>"
        "</w:tc><w:tc><w:p><w:r><w:t>丙</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        '<w:p><w:r><w:rPr><style:effect xmlns:style="urn:fixture:format"/>'
        "</w:rPr><w:t>前</w:t></w:r>"
        "<w:del><w:r><w:delText>删除</w:delText></w:r></w:del>"
        "<w:moveFrom><w:r><w:t>移出</w:t></w:r></w:moveFrom>"
        '<w:moveFromRangeStart w:id="7"/><w:r><w:t>旧位置</w:t></w:r>'
        '<w:moveFromRangeEnd w:id="7"/>'
        "<w:r><w:drawing><w:txbxContent><w:p><w:r><w:t>文本框</w:t>"
        "</w:r></w:p></w:txbxContent></w:drawing><w:t>后</w:t></w:r></w:p>"
    )
    assert extract(data) == "开场\t后\n行\n甲\n乙\t丙\n前后\n".encode()


def test_unselected_large_media_does_not_consume_xml_budget() -> None:
    data = package(
        "<w:p><w:r><w:t>正文</w:t></w:r></w:p>", extra=(("media/image.bin", b"x" * 50_000),)
    )
    assert extract(data, maximum=2048) == "正文\n".encode()


def test_directory_entry_budget_is_checked_before_zipfile_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = package("<w:p/>")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("oversized directory must not construct ZipFile")

    monkeypatch.setattr(document_xml.zipfile, "ZipFile", forbidden)
    with pytest.raises(EvidenceError) as raised:
        extract(data, entries=2)
    assert raised.value.code == "document_budget"


def test_xml_total_depth_and_deadline_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    data = package("<w:p><w:r><w:t>正文</w:t></w:r></w:p>")
    rejected(data, "document_budget", maximum=10)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        largest_xml = max(entry.file_size for entry in archive.infolist())
    rejected(data, "document_budget", maximum=largest_xml)
    rejected(data, "document_budget", depth=2)
    with pytest.raises(EvidenceError) as raised:
        extract_docx(data, max_bytes=16_384, max_entries=128, max_depth=128, deadline=0)
    assert raised.value.code == "document_timeout"
    calls = 0

    def expires() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls < 12 else 2.0

    monkeypatch.setattr(document_xml.time, "monotonic", expires)
    with pytest.raises(EvidenceError) as raised:
        extract_docx(data, max_bytes=16_384, max_entries=128, max_depth=128, deadline=1)
    assert raised.value.code == "document_timeout"
    assert calls == 12


@pytest.mark.parametrize(
    "declaration",
    ['<!DOCTYPE document [<!ENTITY e "expanded">]>', '<!DOCTYPE document SYSTEM "file:///never">'],
)
def test_dtd_and_entities_are_rejected(declaration: str) -> None:
    raw = (
        declaration + f'<w:document xmlns:w="{WORD}"><w:body><w:p><w:r><w:t>&e;</w:t>'
        "</w:r></w:p></w:body></w:document>"
    ).encode()
    rejected(package("", raw_main=raw), "document_unsupported")


@pytest.mark.parametrize(
    ("target", "mode", "content_type", "code"),
    [
        ("https://invalid.example/body.xml", "External", MAIN_TYPE, "document_unsupported"),
        ("../custom/body.xml", "Internal", MAIN_TYPE, "document_unsupported"),
        ("missing.xml", "Internal", MAIN_TYPE, "document_invalid"),
        ("custom/body.xml", "Internal", "application/xml", "document_unsupported"),
    ],
)
def test_main_relationship_and_type_are_required(
    target: str, mode: str, content_type: str, code: str
) -> None:
    rejected(package("<w:p/>", target=target, mode=mode, content_type=content_type), code)


def test_malformed_unknown_body_and_crc_never_return_partial_text() -> None:
    rejected(b"not a ZIP package", "document_invalid")
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w"):
        pass
    rejected(empty.getvalue(), "document_invalid")
    rejected(package("<w:p>"), "document_invalid")
    rejected(
        package('<other:body xmlns:other="urn:unknown"><other:text>遗漏</other:text></other:body>'),
        "document_unsupported",
    )
    data = package("<w:p><w:r><w:t>CRC-ORIGINAL</w:t></w:r></w:p>")
    assert data.count(b"CRC-ORIGINAL") == 1
    rejected(data.replace(b"CRC-ORIGINAL", b"CRC-MODIFIED"), "document_invalid")


def test_duplicate_confusing_and_symlink_members_are_rejected() -> None:
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicate = package("<w:p/>", extra=(("custom/body.xml", b"duplicate"),))
    rejected(duplicate, "document_unsupported")
    rejected(package("<w:p/>", extra=(("CUSTOM/BODY.XML", b"alias"),)), "document_unsupported")
    rejected(package("<w:p/>", extra=(("media/../alias", b"confusing"),)), "document_unsupported")
    link = zipfile.ZipInfo("media/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    rejected(package("<w:p/>", extra=((link, b"target"),)), "document_unsupported")


def test_wheel_preflight_error_mapping_and_remaining_validation_are_preserved() -> None:
    limits = CandidateLimits()
    content = b"fixture = True\n"
    payload = b"x" * 2048
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fixture.py", content)
        archive.writestr("payload", payload)
    data = output.getvalue()
    deadline = time.monotonic() + 30
    assert _archive(io.BytesIO(data), len(data), limits, deadline) == (
        len(content) + len(payload),
        2,
    )
    for raw, bounded, code in (
        (b"invalid", limits, "candidate_wheel_invalid"),
        (data, limits.model_copy(update={"archive_entries": 1}), "candidate_archive_budget"),
        (data, limits.model_copy(update={"expanded_bytes": 1024}), "candidate_archive_budget"),
    ):
        with pytest.raises(CandidateError) as raised:
            _archive(io.BytesIO(raw), len(raw), bounded, deadline)
        assert raised.value.code == code
