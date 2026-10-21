"""Anonymous, bounded version metadata; no artifact download or installation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Self
from urllib.parse import unquote, urlsplit

import httpx
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import Version
from pydantic import Field, field_validator, model_validator

from muli.candidate_policy import CandidateError, SourceName
from muli.compatibility import ReleaseVersion
from muli.management_projection import ProjectionError, _read_object
from muli.management_transaction import Digest, StrictRecord, _object
from muli.platforms.installation import _canonical
from muli.process import _wait_cleanup
from muli.store import Store

if TYPE_CHECKING:
    from muli.candidate_preparation import FrozenPolicy

_KIND, _ID, _POLICY_BYTES = "update_policy", "online-v1", 16_384
_CHUNK = 65_536


def anonymous_url(value: str) -> str:
    """Deliberately exclude credential, signed-query and redirect protocols."""
    parsed = urlsplit(value)
    if (
        not value
        or any(ord(c) < 33 or ord(c) == 127 for c in value)
        or "\\" in value
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or parsed.port not in {None, 443}
        or str(httpx.URL(value)) != value
    ):
        raise ValueError("candidate_online_url_invalid")
    return value


def origin(value: str) -> str:
    parsed = urlsplit(anonymous_url(value))
    return f"https://{parsed.netloc}"


class OnlineSettings(StrictRecord):
    enabled: bool = False
    source: SourceName = "pypi"
    metadata_base: str = Field(default="https://pypi.org/pypi", max_length=2048)
    artifact_origins: list[str] = Field(
        default_factory=lambda: ["https://files.pythonhosted.org"], min_length=1, max_length=8
    )
    metadata_bytes: int = Field(default=262_144, ge=1024, le=1_048_576)
    max_files: int = Field(default=64, ge=1, le=512)
    download_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    request_seconds: float = Field(default=10.0, gt=0, le=30, allow_inf_nan=False)

    @field_validator("metadata_base")
    @classmethod
    def metadata_url(cls, value: str) -> str:
        anonymous_url(value)
        if value.endswith("/"):
            raise ValueError("candidate_online_url_invalid")
        return value

    @field_validator("artifact_origins")
    @classmethod
    def allowed_origins(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(origin(v) != v for v in values):
            raise ValueError("candidate_online_origin_invalid")
        return values

    @property
    def revision(self) -> str:
        return hashlib.sha256(_canonical(self.model_dump(mode="json"))).hexdigest()


class ArtifactApproval(StrictRecord):
    """An approval's complete If-Match facts, not permission supplied by a model."""

    schema_version: int = Field(default=1, ge=1, le=1)
    source: SourceName
    version: ReleaseVersion
    url: str = Field(max_length=4096)
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=1, le=32 * 1024 * 1024)
    sha256: Digest
    yanked: Literal[False]
    requires_python: str | None = Field(default=None, max_length=1024)
    online_revision: Digest
    candidate_revision: Digest
    installation: Digest
    binding_revision: Digest
    intent: Digest
    local_materials: Digest
    format: Digest

    @field_validator("yanked", mode="before")
    @classmethod
    def not_yanked(cls, value: object) -> object:
        if type(value) is not bool or value:
            raise ValueError("candidate_online_yanked")
        return value

    @model_validator(mode="after")
    def exact_wheel(self) -> Self:
        anonymous_url(self.url)
        try:
            name, version, build, tags = parse_wheel_filename(self.filename)
            compatible = {str(tag) for tag in tags} == {"py3-none-any"}
            python = SpecifierSet(self.requires_python or "")
        except (InvalidWheelFilename, InvalidSpecifier):
            raise ValueError("candidate_online_artifact_invalid") from None
        if (
            name != "muli-agent"
            or version != Version(self.version)
            or build
            or not compatible
            or unquote(urlsplit(self.url).path.rsplit("/", 1)[-1]) != self.filename
            or not python.contains(platform.python_version(), prereleases=True)
        ):
            raise ValueError("candidate_online_artifact_unsupported")
        return self


def check_approval(
    settings: OnlineSettings, approval: ArtifactApproval, policy: FrozenPolicy, version: str
) -> None:
    # Revalidate frozen models too: model_copy(update=...) bypasses validation.
    checked = ArtifactApproval.model_validate(approval.model_dump(mode="python"))
    expected = policy.expected
    if (
        not settings.enabled
        or checked.version != version
        or checked.source != settings.source
        or checked.online_revision != settings.revision
        or checked.candidate_revision != expected.revision
        or _binding(policy)
        != {
            "installation": checked.installation,
            "binding_revision": checked.binding_revision,
            "intent": checked.intent,
            "local_materials": checked.local_materials,
            "format": checked.format,
        }
        or origin(checked.url) not in settings.artifact_origins
        or checked.size > min(settings.download_bytes, expected.limits.wheel_bytes)
    ):
        raise CandidateError("candidate_online_changed")


def _binding(policy: FrozenPolicy) -> dict[str, str]:
    state = policy.source.observed_format
    if state is None:
        raise CandidateError("candidate_online_format_required")
    return {
        "installation": policy.source.slot.digest,
        "binding_revision": policy.source.binding_revision,
        "intent": hashlib.sha256(
            _canonical(policy.original_intent.model_dump(mode="json"))
        ).hexdigest(),
        "local_materials": policy.materials.digest,
        "format": hashlib.sha256(_canonical(state.model_dump(mode="json"))).hexdigest(),
    }


@dataclass(frozen=True, slots=True)
class OnlinePolicyStore:
    store: Store

    def read(self) -> OnlineSettings:
        try:
            item = _read_object(self.store.db, _KIND, _ID, _POLICY_BYTES)
        except (ProjectionError, sqlite3.Error):
            raise CandidateError("candidate_online_policy_invalid") from None
        if item is None:
            return OnlineSettings()
        if (
            set(item) != {"id", "schema_version", "policy"}
            or item["id"] != _ID
            or type(item["schema_version"]) is not int
            or item["schema_version"] != 1
        ):
            raise CandidateError("candidate_online_policy_invalid")
        try:
            return OnlineSettings.model_validate(item["policy"])
        except (ValueError, TypeError):
            raise CandidateError("candidate_online_policy_invalid") from None

    def replace(self, policy: OnlineSettings) -> OnlineSettings:
        db = self.store.db
        if (
            db.in_transaction
            or db.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL
            or db.isolation_level is None
        ):
            raise CandidateError("candidate_online_transaction_unsupported")
        self.read()  # Corrupt or unknown existing data cannot be reset by replacement.
        try:
            checked = OnlineSettings.model_validate(policy.model_dump(mode="python"))
            item = {"id": _ID, "schema_version": 1, "policy": checked.model_dump(mode="json")}
            if len(json.dumps(item, ensure_ascii=False).encode()) > _POLICY_BYTES:
                raise CandidateError("candidate_online_policy_budget")
            self.store.event(
                "update.online_policy.changed",
                {"id": _ID, "enabled": checked.enabled, "revision": checked.revision},
                objects=[(_KIND, item)],
            )
        except sqlite3.Error:
            raise CandidateError("candidate_online_policy_write_failed") from None
        return checked


async def receive(
    url: str,
    *,
    settings: OnlineSettings,
    limit: int,
    deadline: float,
    close_seconds: float,
    media_types: frozenset[str],
    consume: Callable[[bytes], None],
    verify: Callable[[], None],
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """One direct request, counting raw HTTP body bytes, including error bodies.

    The caller owns the task. Close remains strongly owned through repeated
    cancellation and shares one separate stop budget; it cannot renew the
    request/attempt deadline. Production uses HTTPX's cancellation-cooperative
    transport, not an arbitrary user-supplied close callback.
    """
    anonymous_url(url)
    verify()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CandidateError("candidate_deadline_exceeded")
    client = httpx.AsyncClient(
        trust_env=False,
        verify=True,
        follow_redirects=False,
        timeout=min(settings.request_seconds, remaining),
        transport=transport,
        headers={"Accept-Encoding": "identity"},
    )
    response: httpx.Response | None = None
    try:
        # Client/TLS construction is charged too; it cannot restart the clock.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CandidateError("candidate_deadline_exceeded")
        async with asyncio.timeout(remaining):
            response = await client.send(client.build_request("GET", url), stream=True)
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise CandidateError("candidate_online_encoding_unsupported")
            declared = response.headers.get("content-length")
            if declared is not None and (
                len(declared) > 20
                or not declared.isascii()
                or not declared.isdecimal()
                or int(declared) > limit
            ):
                raise CandidateError("candidate_online_body_budget")
            consumed = 0
            async for chunk in response.aiter_raw(chunk_size=min(_CHUNK, limit + 1)):
                consumed += len(chunk)
                if consumed > limit:
                    raise CandidateError("candidate_online_body_budget")
                verify()
                if response.status_code == 200:
                    consume(chunk)
                if time.monotonic() >= deadline:
                    raise CandidateError("candidate_deadline_exceeded")
            if (
                response.status_code != 200
                or response.headers.get("content-type", "").split(";", 1)[0].lower()
                not in media_types
            ):
                raise CandidateError("candidate_online_response_invalid")
            if declared is not None and consumed != int(declared):
                raise CandidateError("candidate_online_response_invalid")
            verify()
    except (httpx.HTTPError, TimeoutError):
        raise CandidateError("candidate_online_unavailable") from None
    finally:

        async def close() -> None:
            end = asyncio.get_running_loop().time() + close_seconds
            failed = False
            closers = [response.aclose, client.aclose] if response is not None else [client.aclose]
            for closer in closers:
                try:
                    async with asyncio.timeout_at(end):
                        await closer()
                except Exception:
                    # Still attempt client close after a response close error,
                    # within the same deadline. Never treat timeout as success.
                    failed = True
            if failed:
                raise CandidateError("candidate_online_close_failed")

        if await _wait_cleanup(asyncio.create_task(close())):
            raise asyncio.CancelledError


async def preview_artifact(
    settings: OnlineSettings,
    policy: FrozenPolicy,
    version: str,
    *,
    verify: Callable[[], None],
    transport: httpx.AsyncBaseTransport | None = None,
) -> ArtifactApproval:
    from muli.updates import release_version

    release_version(version)
    if not settings.enabled:
        raise CandidateError("candidate_online_disabled")
    expected = policy.expected
    binding = _binding(policy)
    end = time.monotonic() + expected.limits.preflight_seconds
    data = bytearray()
    await receive(
        f"{settings.metadata_base}/muli-agent/{version}/json",
        settings=settings,
        limit=min(settings.metadata_bytes, expected.limits.metadata_bytes),
        deadline=end,
        close_seconds=expected.limits.stop_seconds,
        media_types=frozenset({"application/json"}),
        consume=data.extend,
        verify=verify,
        transport=transport,
    )
    try:
        payload = _object(bytes(data))
        info, files = payload["info"], payload["urls"]
        if (
            not isinstance(info, dict)
            or info.get("name") not in {"muli-agent", "muli_agent"}
            or info.get("version") != version
            or not isinstance(files, list)
            or len(files) > settings.max_files
        ):
            raise ValueError
        candidates: list[ArtifactApproval] = []
        for item in files:
            if not isinstance(item, dict):
                raise ValueError
            if item.get("packagetype") != "bdist_wheel":
                continue
            filename = item.get("filename")
            if not isinstance(filename, str) or len(filename) > 255:
                raise ValueError
            name, parsed, build, tags = parse_wheel_filename(filename)
            if name != "muli-agent" or parsed != Version(version):
                raise ValueError
            if build or {str(tag) for tag in tags} != {"py3-none-any"}:
                continue
            candidate = ArtifactApproval(
                source=settings.source,
                version=version,
                url=item["url"],
                filename=filename,
                size=item["size"],
                sha256=item["digests"]["sha256"],
                yanked=item["yanked"],
                requires_python=item.get("requires_python"),
                online_revision=settings.revision,
                candidate_revision=expected.revision,
                installation=binding["installation"],
                binding_revision=binding["binding_revision"],
                intent=binding["intent"],
                local_materials=binding["local_materials"],
                format=binding["format"],
            )
            check_approval(settings, candidate, policy, version)
            candidates.append(candidate)
        if len(candidates) != 1:
            raise ValueError
        verify()
        if time.monotonic() >= end:
            raise CandidateError("candidate_deadline_exceeded")
        return candidates[0]
    except CandidateError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError):
        raise CandidateError("candidate_online_metadata_invalid") from None
