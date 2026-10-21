"""Host-configured, offline candidate limits and the bounded execution contract.

ExpectedPolicy is an If-Match condition shown in the real tool approval. It is
never a grant to select a path, source or larger budget supplied by the model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from muli.management_transaction import Digest, StrictRecord

DiskMode = Literal["threshold", "strict"]
SourceName = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")]


class CandidateError(ValueError):
    """Fixed codes only: paths, requirements and process output stay private."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CandidateLimits(StrictRecord):
    seconds: float = Field(default=180.0, gt=0, le=180, allow_inf_nan=False)
    preflight_seconds: float = Field(default=30.0, gt=0, le=30, allow_inf_nan=False)
    stop_seconds: float = Field(default=2.0, gt=0, le=5, allow_inf_nan=False)
    poll_interval: float = Field(default=0.25, ge=0.05, le=2, allow_inf_nan=False)
    memory_bytes: int = Field(default=512 * 1024 * 1024, ge=32 * 1024 * 1024, le=1024**3)
    cpu_percent: int = Field(default=100, ge=1, le=100)
    tasks: int = Field(default=64, ge=8, le=128)
    output_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    wheels: int = Field(default=256, ge=1, le=512)
    wheel_bytes: int = Field(default=128 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024)
    compressed_bytes: int = Field(default=512 * 1024 * 1024, ge=1024, le=1024**3)
    expanded_bytes: int = Field(default=512 * 1024 * 1024, ge=1024, le=1024**3)
    archive_entries: int = Field(default=20_000, ge=1, le=100_000)
    central_directory_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)
    candidate_bytes: int = Field(default=512 * 1024 * 1024, ge=1024, le=1024**3)
    cache_bytes: int = Field(default=256 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024)
    spare_bytes: int = Field(default=256 * 1024 * 1024, ge=1024, le=1024**3)
    scan_entries: int = Field(default=30_000, ge=1, le=100_000)
    scan_seconds: float = Field(default=2.0, gt=0, le=5, allow_inf_nan=False)
    executable_bytes: int = Field(default=64 * 1024 * 1024, ge=1024, le=128 * 1024 * 1024)
    metadata_bytes: int = Field(default=262_144, ge=1024, le=1_048_576)

    @model_validator(mode="after")
    def valid_sizes(self) -> Self:
        if self.wheel_bytes > self.compressed_bytes or self.preflight_seconds > self.seconds:
            raise ValueError("candidate_budget")
        return self


class CandidateSettings(StrictRecord):
    enabled: bool = False
    source: SourceName = "local-wheelhouse"
    wheelhouse: str = Field(default="", repr=False)
    retained_root: str = Field(default="", repr=False)
    uv_executable: str = Field(default="", repr=False)
    disk_mode: DiskMode = "threshold"
    limits: CandidateLimits = Field(default_factory=CandidateLimits)

    @field_validator("wheelhouse", "retained_root", "uv_executable")
    @classmethod
    def explicit_path(cls, value: str) -> str:
        if value and (
            not Path(value).is_absolute()
            or ".." in Path(value).parts
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("candidate_path")
        return value

    @model_validator(mode="after")
    def configured_source(self) -> Self:
        if self.enabled and not all((self.wheelhouse, self.retained_root, self.uv_executable)):
            raise ValueError("candidate_source_required")
        return self


class ExpectedPolicy(StrictRecord):
    source: SourceName
    limits: CandidateLimits
    disk_mode: DiskMode
    revision: Digest


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    """One owned process operation inside the same candidate deadline.

    The backend verifies kernel limits before allowing argv to execute, keeps
    ownership through cancellation, and returns only after the tree is empty.
    observe() is a bounded host filesystem check, not a kernel quota. The first
    backend rejects strict mode because it cannot verify an existing quota.
    """

    argv: tuple[str, ...] = field(repr=False)
    env: tuple[tuple[str, str], ...] = field(repr=False)
    cwd: Path = field(repr=False)
    private_root: Path = field(repr=False)
    deadline: float = field(repr=False)
    limits: CandidateLimits
    disk_mode: DiskMode
    observe: Callable[[], None] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    """Private bounded output after the backend confirmed complete cleanup."""

    returncode: int
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)
