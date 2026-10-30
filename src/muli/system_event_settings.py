"""Validated system-event policy without loading its notification runtime."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LogLevel = Literal["warning", "error", "critical"]


class SystemEventsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(default=True, strict=True)
    notify_enabled: bool = Field(default=True, strict=True)
    capture_level: LogLevel = "error"
    notify_level: LogLevel = "error"
    queue_capacity: int = Field(default=128, ge=1, le=4096, strict=True)
    max_records: int = Field(default=1000, ge=1, le=10_000, strict=True)
    dedup_seconds: float = Field(default=60, ge=1, le=3600, allow_inf_nan=False, strict=True)
