"""Independent hot document policy records without changing the main config format."""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from pydantic import ConfigDict, Field, field_validator

from muli.config import Section
from muli.evidence import EvidenceError
from muli.store import Store

_KIND = "document_settings"
_ID = "current"
_MAX_RECORD_BYTES = 16_384


class DocumentSettings(Section):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pdf_command: list[Annotated[str, Field(strict=True, min_length=1, max_length=4096)]] = Field(
        default_factory=lambda: ["pdftotext"], min_length=1, max_length=16
    )
    timeout_seconds: float = Field(default=15.0, ge=0.1, le=60, allow_inf_nan=False)
    max_extracted_bytes: int = Field(default=2_000_000, ge=1024, le=20_000_000, strict=True)
    max_archive_entries: int = Field(default=2048, ge=1, le=20000, strict=True)
    max_xml_depth: int = Field(default=128, ge=4, le=512, strict=True)

    @field_validator("pdf_command")
    @classmethod
    def valid_pdf_command(cls, value: list[str]) -> list[str]:
        if any("\x00" in part or "\n" in part or "\r" in part for part in value):
            raise ValueError("PDF 转换命令不能包含空字节或换行")
        return value


class DocumentSettingsStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def read(self) -> DocumentSettings:
        try:
            item = self.store.bounded_object(_KIND, _ID, max_bytes=_MAX_RECORD_BYTES)
            if item is None:
                return DocumentSettings()
            if (
                set(item) != {"id", "schema_version", "settings"}
                or item["id"] != _ID
                or type(item["schema_version"]) is not int
                or item["schema_version"] != 1
                or not isinstance(item["settings"], dict)
            ):
                raise ValueError("unknown document policy record")
            return DocumentSettings.model_validate(item["settings"])
        except (ValueError, TypeError, RecursionError, sqlite3.Error) as error:
            raise EvidenceError("document_settings_unavailable", "文档配置无法完整核对") from error

    def replace(self, settings: DocumentSettings) -> DocumentSettings:
        db = self.store.db
        if (
            db.in_transaction
            or db.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL
            or db.isolation_level is None
        ):
            raise EvidenceError("document_settings_transaction", "文档配置需要独立事务")
        self.read()  # Do not overwrite malformed records or an unknown future schema.
        checked = DocumentSettings.model_validate(settings.model_dump())
        item: dict[str, Any] = {
            "id": _ID,
            "schema_version": 1,
            "settings": checked.model_dump(mode="json"),
        }
        try:
            size = len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if size > _MAX_RECORD_BYTES:
                raise ValueError("document policy record exceeds byte budget")
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise EvidenceError(
                "document_settings_budget", "文档配置超过记录预算或不是有效文本"
            ) from error
        try:
            self.store.event("document.settings.changed", {"id": _ID}, objects=[(_KIND, item)])
        except sqlite3.Error as error:
            raise EvidenceError("document_settings_write_failed", "文档配置保存失败") from error
        return checked
