"""Bounded, local DOCX body text extraction without a filesystem unpack step."""

from __future__ import annotations

import io
import math
import re
import stat
import struct
import time
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import NoReturn
from urllib.parse import urlsplit
from xml.parsers import expat

from muli.evidence import EvidenceError
from muli.zip_directory import ZipDirectoryError, check_zip_directory

_CHUNK = 16_384
_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_WORD_NAMESPACES = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
}
_IGNORED_NAMESPACES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "http://purl.oclc.org/ooxml/officeDocument/math",
    "urn:schemas-microsoft-com:vml",
}
_MAIN_RELATIONSHIPS = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument",
}
_MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)


def _invalid() -> NoReturn:
    raise EvidenceError("document_invalid", "DOCX 包或 XML 无效，未返回部分正文")


def _unsupported(*_values: object) -> NoReturn:
    raise EvidenceError("document_unsupported", "DOCX 包含不支持的关系或 XML 结构")


def _part(target: str) -> str:
    """Resolve only an unambiguous package-root URI, never an external resource."""
    if not target or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in target
    ):
        _unsupported()
    if "\\" in target or "%" in target or target.startswith("//"):
        _unsupported()
    try:
        parsed = urlsplit(target)
    except ValueError:
        _unsupported()
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        _unsupported()
    path = parsed.path.removeprefix("/")
    parts: list[str] = []
    for component in path.split("/"):
        if component == ".":
            continue
        if component == "..":
            if not parts:
                _unsupported()
            parts.pop()
        elif component:
            parts.append(component)
        else:
            _unsupported()
    if not parts:
        _unsupported()
    return "/".join(parts)


def _members(
    archive: zipfile.ZipFile, count: int, before: Callable[[], None]
) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) != count:
        _invalid()
    names: set[str] = set()
    members: dict[str, zipfile.ZipInfo] = {}
    for entry in entries:
        before()
        name = entry.filename[:-1] if entry.is_dir() else entry.filename
        path = PurePosixPath(name)
        kind = stat.S_IFMT(entry.external_attr >> 16)
        if (
            not name
            or entry.orig_filename != entry.filename
            or "\\" in name
            or "%" in name
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != name
            or name.casefold() in names
            or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
            or (kind == stat.S_IFDIR and not entry.is_dir())
            or entry.flag_bits & 1
            or entry.extract_version > 20
            or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        ):
            _unsupported()
        names.add(name.casefold())
        if not entry.is_dir():
            members[name] = entry
    return members


@dataclass(slots=True)
class _XMLBudget:
    maximum: int
    depth: int
    deadline: float
    expanded: int = 0

    def before(self) -> None:
        if time.monotonic() >= self.deadline:
            raise EvidenceError("document_timeout", "DOCX 提取超过期限")

    def parse(
        self,
        archive: zipfile.ZipFile,
        entry: zipfile.ZipInfo,
        start: Callable[[str, dict[str, str], int], None],
        *,
        end: Callable[[str, int], None] | None = None,
        text: Callable[[str], None] | None = None,
    ) -> None:
        self.before()
        if entry.file_size > self.maximum - self.expanded:
            raise EvidenceError("document_budget", "DOCX 相关 XML 解压总量超过上限")
        parser = expat.ParserCreate(namespace_separator=" ")
        depth = 0

        def opened(name: str, attributes: dict[str, str]) -> None:
            nonlocal depth
            self.before()
            depth += 1
            if depth > self.depth:
                raise EvidenceError("document_budget", "DOCX XML 深度超过上限")
            start(name, attributes, depth)

        def closed(name: str) -> None:
            nonlocal depth
            self.before()
            if end is not None:
                end(name, depth)
            depth -= 1

        def characters(value: str) -> None:
            self.before()
            if text is not None:
                text(value)

        parser.StartElementHandler = opened
        parser.EndElementHandler = closed
        parser.CharacterDataHandler = characters
        parser.StartDoctypeDeclHandler = _unsupported
        parser.EntityDeclHandler = _unsupported
        parser.ExternalEntityRefHandler = _unsupported
        parser.SkippedEntityHandler = _unsupported
        read_bytes = 0
        with archive.open(entry, "r") as source:
            while True:
                self.before()
                chunk = source.read(min(_CHUNK, self.maximum - self.expanded + 1))
                if not chunk:
                    break
                self.expanded += len(chunk)
                read_bytes += len(chunk)
                if self.expanded > self.maximum:
                    raise EvidenceError("document_budget", "DOCX 相关 XML 解压总量超过上限")
                parser.Parse(chunk, False)
            self.before()
            parser.Parse(b"", True)
        if read_bytes != entry.file_size:
            _invalid()
        self.before()


def _main_part(archive: zipfile.ZipFile, entry: zipfile.ZipInfo, budget: _XMLBudget) -> str:
    found: list[str] = []
    identifiers: set[str] = set()

    def start(name: str, attributes: dict[str, str], depth: int) -> None:
        if depth == 1:
            if name != f"{_RELATIONSHIPS} Relationships":
                _unsupported()
            return
        if depth != 2 or name != f"{_RELATIONSHIPS} Relationship":
            _unsupported()
        identifier = attributes.get("Id", "")
        target, relation = attributes.get("Target", ""), attributes.get("Type", "")
        mode = attributes.get("TargetMode", "Internal")
        if not identifier or identifier in identifiers or not target or not relation:
            _invalid()
        identifiers.add(identifier)
        if mode not in {"Internal", "External"}:
            _unsupported()
        if relation in _MAIN_RELATIONSHIPS:
            if mode != "Internal":
                _unsupported()
            found.append(_part(target))

    budget.parse(archive, entry, start)
    if not found:
        _invalid()
    if len(found) != 1:
        _unsupported()
    return found[0]


def _check_content_type(
    archive: zipfile.ZipFile, entry: zipfile.ZipInfo, selected: str, budget: _XMLBudget
) -> None:
    overrides: dict[str, str] = {}
    defaults: dict[str, str] = {}

    def start(name: str, attributes: dict[str, str], depth: int) -> None:
        if depth == 1:
            if name != f"{_CONTENT_TYPES} Types":
                _unsupported()
            return
        content_type = attributes.get("ContentType", "")
        if depth != 2 or not content_type:
            _unsupported()
        if name == f"{_CONTENT_TYPES} Override":
            part = attributes.get("PartName", "")
            if not part.startswith("/"):
                _unsupported()
            part = _part(part)
            if part.casefold() in overrides:
                _invalid()
            overrides[part.casefold()] = content_type
        elif name == f"{_CONTENT_TYPES} Default":
            extension = attributes.get("Extension", "")
            if not re.fullmatch(r"[a-zA-Z0-9]+", extension):
                _unsupported()
            extension = extension.casefold()
            if extension in defaults:
                _invalid()
            defaults[extension] = content_type
        else:
            _unsupported()

    budget.parse(archive, entry, start)
    content_type = overrides.get(
        selected.casefold(), defaults.get(PurePosixPath(selected).suffix[1:].casefold())
    )
    if content_type != _MAIN_CONTENT_TYPE:
        _unsupported()


@dataclass(slots=True)
class _BodyText:
    maximum: int
    output: bytearray = field(default_factory=bytearray)
    path: list[str] = field(default_factory=list)
    cells: list[int] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)
    moved: set[str] = field(default_factory=set)
    namespace: str = ""
    body: bool = False
    seen_body: bool = False
    skip_depth: int = 0
    paragraph_depth: int = 0

    def tag(self, name: str) -> str:
        return f"{self.namespace} {name}"

    def emit(self, value: str) -> None:
        if self.moved:
            return
        raw = value.encode("utf-8")
        if len(raw) > self.maximum - len(self.output):
            raise EvidenceError("document_budget", "DOCX 提取正文超过上限")
        self.output.extend(raw)

    def start(self, name: str, attributes: dict[str, str], depth: int) -> None:
        self.path.append(name)
        if depth == 1:
            namespace, _, local = name.rpartition(" ")
            if namespace not in _WORD_NAMESPACES or local != "document":
                _unsupported()
            self.namespace = namespace
            return
        if name == self.tag("body"):
            if depth != 2 or self.seen_body:
                _invalid()
            self.body, self.seen_body = True, True
            return
        if not self.body or self.skip_depth:
            return
        namespace, _, local = name.rpartition(" ")
        if namespace != self.namespace:
            if namespace not in _IGNORED_NAMESPACES:
                _unsupported()
            self.skip_depth = depth
            return
        if local in {
            "del",
            "moveFrom",
            "drawing",
            "pict",
            "object",
            "txbxContent",
            "pPr",
            "rPr",
            "tblPr",
            "tblGrid",
            "tcPr",
            "trPr",
            "sectPr",
            "sdtPr",
            "sdtEndPr",
            "customXmlPr",
        }:
            self.skip_depth = depth
            return
        if local == "altChunk":
            _unsupported()
        if local in {"moveFromRangeStart", "moveFromRangeEnd"}:
            identifier = attributes.get(self.tag("id"), "")
            if not identifier:
                _invalid()
            if local == "moveFromRangeStart":
                if identifier in self.moved:
                    _invalid()
                self.moved.add(identifier)
            else:
                if identifier not in self.moved:
                    _invalid()
                self.moved.remove(identifier)
        elif local == "p":
            if self.paragraph_depth:
                _unsupported()
            self.paragraph_depth = depth
        elif local == "tr":
            if self.path[-2] != self.tag("tbl"):
                _unsupported()
            self.rows.append(len(self.output))
        elif local == "tc":
            if self.path[-2] != self.tag("tr"):
                _unsupported()
            self.cells.append(len(self.output))
        elif local in {"tab", "br", "cr", "t"}:
            if not self.paragraph_depth:
                _unsupported()
            if local != "t":
                self.emit("\t" if local == "tab" else "\n")

    def end(self, name: str, depth: int) -> None:
        if self.skip_depth:
            if depth == self.skip_depth:
                self.skip_depth = 0
        elif self.body:
            if name == self.tag("body"):
                self.body = False
            elif name == self.tag("p"):
                self.emit("\n")
                self.paragraph_depth = 0
            elif name == self.tag("tc"):
                self.trim(self.cells.pop(), b"\n")
                self.emit("\t")
            elif name == self.tag("tr"):
                self.trim(self.rows.pop(), b"\t")
                self.emit("\n")
        self.path.pop()

    def trim(self, start: int, suffix: bytes) -> None:
        if len(self.output) > start and self.output.endswith(suffix):
            del self.output[-len(suffix) :]

    def text(self, value: str) -> None:
        if self.body and not self.skip_depth and self.path[-1] == self.tag("t"):
            self.emit(value)


def extract_docx(
    data: bytes, *, max_bytes: int, max_entries: int, max_depth: int, deadline: float
) -> bytes:
    """Extract the main body; paragraphs/rows end in LF and cells use TAB."""
    if any(type(value) is not int or value < 1 for value in (max_bytes, max_entries, max_depth)):
        raise EvidenceError("document_budget", "DOCX 读取预算无效")
    if not math.isfinite(deadline):
        raise EvidenceError("document_budget", "DOCX 读取期限无效")
    budget = _XMLBudget(max_bytes, max_depth, deadline)
    budget.before()
    if type(data) is not bytes:
        _invalid()
    try:
        source = io.BytesIO(data)
        # Bound directory bytes independently of the selected XML's cumulative
        # expanded-byte counter, before ZipFile constructs member objects.
        count = check_zip_directory(
            source, len(data), max_entries=max_entries, max_directory_bytes=max_bytes
        )
        budget.before()
        with zipfile.ZipFile(source, "r") as archive:
            members = _members(archive, count, budget.before)
            budget.before()
            if "_rels/.rels" not in members or "[Content_Types].xml" not in members:
                _invalid()
            selected = _main_part(archive, members["_rels/.rels"], budget)
            if selected not in members:
                _invalid()
            _check_content_type(archive, members["[Content_Types].xml"], selected, budget)
            body = _BodyText(max_bytes)
            budget.parse(archive, members[selected], body.start, end=body.end, text=body.text)
            if not body.seen_body or body.moved:
                _invalid()
            budget.before()
            return bytes(body.output)
    except ZipDirectoryError as exc:
        if exc.limit_exceeded:
            raise EvidenceError("document_budget", "DOCX ZIP 目录超过上限") from None
        _invalid()
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        expat.ExpatError,
        OSError,
        UnicodeError,
        struct.error,
        zlib.error,
        NotImplementedError,
    ):
        _invalid()
