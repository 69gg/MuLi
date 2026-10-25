"""Explicit private visual interpretations, not owner facts or new model calls."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from muli.attachments import Attachments
from muli.evidence import EvidenceError, content_page, json_size
from muli.media import ImageDescription
from muli.transcripts import _owned, _read_verified_artifact

VISION_TRUST = "model_interpretation_not_owner_fact"


def bounded_preview(
    metadata: dict[str, Any], text: str, field: str, *, max_output_chars: int
) -> dict[str, Any]:
    """Keep short legacy values; bound the complete long-result envelope before Runtime."""
    complete = {**metadata, field: text}
    if json_size(complete) <= max_output_chars:
        return complete
    data = text.encode("utf-8")
    page = content_page(
        {**metadata, "truncated": False},
        data,
        offset_bytes=0,
        total_bytes=len(data),
        max_output_chars=max_output_chars,
    )
    return {**metadata, field: page["content"], "truncated": page["has_more"]}


class VisionArchives:
    def __init__(self, attachments: Attachments) -> None:
        self.attachments = attachments

    def _byte_limit(self) -> int:
        return self.attachments.config.settings.uploads.max_file_bytes

    async def save(
        self, source: dict[str, Any], prompt: str, result: ImageDescription
    ) -> dict[str, Any]:
        """Vision has finished; any local failure is unknown and never retries vision."""
        receipt: dict[str, Any] = {
            "id": source["id"],
            "vision_completed": True,
            "archive_status": "unknown",
        }
        try:
            payload = {
                "version": 1,
                "kind": "vision_interpretation",
                "source": {key: source[key] for key in ("id", "sha256", "size", "mime_type")},
                "model": {
                    "profile": result.profile,
                    "protocol": result.protocol,
                    "model": result.model,
                },
                "prompt": prompt,
                "description": result.description,
                "trust": VISION_TRUST,
            }
            text = json.dumps(payload, ensure_ascii=False)
            limit = min(result.max_response_bytes, self._byte_limit())
            if len(text) > limit:
                raise EvidenceError("vision_archive_limit", "完整视觉解释超过留存上限")
            data = text.encode("utf-8")
            if len(data) > limit:
                raise EvidenceError("vision_archive_limit", "完整视觉解释超过留存上限")

            async def chunks() -> AsyncIterator[bytes]:
                yield data

            item = await _owned(
                self.attachments.save(
                    chunks(),
                    f"视觉解释-{source['id']}.json",
                    source="vision",
                )
            )
            receipt.update(archive_id=item["id"], revision=item["sha256"], url=item["url"])
            if len(data) > self._byte_limit():
                raise EvidenceError("vision_archive_limit", "留存期间附件上限已缩小")
            return bounded_preview(
                {**receipt, "archive_status": "saved", "trust": VISION_TRUST},
                result.description,
                "description",
                max_output_chars=self.attachments.config.settings.limits.max_output_chars,
            )
        except Exception as exc:  # noqa: BLE001 - never disguise a completed visual call as unexecuted
            return {
                **receipt,
                "error": "看图已完成，但存档或预览结果不确定；请搜索附件确认，不要自动重复看图。",
                "code": exc.code if isinstance(exc, EvidenceError) else "vision_archive_failed",
            }

    async def read(
        self, identifier: str, *, revision: str | None = None, offset_bytes: int = 0
    ) -> dict[str, Any]:
        item, raw = await _read_verified_artifact(
            self.attachments,
            identifier,
            source="vision",
            byte_limit=self._byte_limit,
            revision=revision,
            offset_bytes=offset_bytes,
        )
        return content_page(
            {
                "id": identifier,
                "revision": item["sha256"],
                "url": self.attachments.public(item)["url"],
                "trust": VISION_TRUST,
                "truncated": False,
            },
            raw,
            offset_bytes=offset_bytes,
            total_bytes=item["size"],
            max_output_chars=self.attachments.config.settings.limits.max_output_chars,
        )
