"""Bounded, read-only access to this assistant's own service diagnostics."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

from muli.attachments import file_io
from muli.config import ConfigFile
from muli.process import run_process
from muli.service import UserService

DEFAULT_LOG_LINES = 40
MAX_LOG_LINES = 200
LOG_TIMEOUT = 5
_MASK = "[hidden]"


def _secrets(config: ConfigFile, mcp: dict[str, Any] | None = None) -> set[str]:
    settings = config.settings
    values: set[str] = {settings.chiliarch.credential, settings.server.password_hash}
    profiles = [
        *settings.models.model_dump().values(),
        *settings.media.model_dump().values(),
        settings.search.model_dump(),
        *settings.channels.model_dump().values(),
    ]
    profiles.extend(settings.mcp.get("mcpServers", {}).values())
    profiles.extend((mcp or {}).get("mcpServers", {}).values())
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        for field in ("api_key", "token", "webhook_secret", "client_secret"):
            for value in (
                profile.get(field),
                os.environ.get(profile.get(field + "_env", ""), ""),
            ):
                if isinstance(value, str):
                    values.add(value)
        for field in ("headers", "extra_headers", "env"):
            for value in profile.get(field, {}).values():
                if isinstance(value, str):
                    values.add(value)
        for field in ("url", "base_url"):
            value = profile.get(field)
            if isinstance(value, str):
                try:
                    parsed = urlsplit(value)
                except ValueError:
                    continue
                values.update(value for _, value in parse_qsl(parsed.query))
                values.update(item for item in (parsed.username, parsed.password) if item)
    for value in tuple(values):
        values.update(value.splitlines())
        if value.lower().startswith(("bearer ", "basic ")):
            values.add(value.split(" ", 1)[1])
    return {value for value in values if value}


def _redact(text: str, secrets: set[str]) -> str:
    variants = {
        variant
        for secret in secrets
        for variant in (
            secret,
            json.dumps(secret, ensure_ascii=False)[1:-1],
            quote(secret, safe=""),
            quote_plus(secret),
        )
    }
    for value in sorted(variants, key=len, reverse=True):
        text = text.replace(value, _MASK)
    # Also hide common credential forms when they are not the current configuration.
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[^\s\"'<>]+", r"\1 " + _MASK, text)
    text = re.sub(r"(https?://)[^\s/@]+@", r"\1" + _MASK + "@", text)
    text = re.sub(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*", r"\1?" + _MASK, text)
    text = re.sub(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|token|password|credential|client[_-]?secret)"
        r"[\"']?\s*[:=]\s*)(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;}]+)",
        r"\1" + _MASK,
        text,
    )
    return text


def _file_tail(path: Path, budget: int, lines: int) -> tuple[str, bool]:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Log is not a regular file")
        start = max(0, info.st_size - budget)
        handle.seek(start)
        data = handle.read(min(info.st_size, budget))
    omitted = bool(start or data)
    if data:
        # The writer rotates by bytes, so even file offset zero may continue a
        # line from the prior file. Never expose an unproven first-line suffix.
        _, separator, data = data.partition(b"\n")
        if not separator:
            data = b""
    if data and not data.endswith(b"\n"):
        # A writer may be in the middle of a credential or diagnostic line.
        data = data.rpartition(b"\n")[0]
        omitted = True
    records = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(records[-lines:]), omitted or len(records) > lines


async def read_service_logs(
    service: UserService,
    config: ConfigFile,
    *,
    lines: int = DEFAULT_LOG_LINES,
    mcp_config: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if type(lines) is not int or not 1 <= lines <= MAX_LOG_LINES:
        raise ValueError(f"lines 必须为 1 至 {MAX_LOG_LINES} 的整数")
    budget = config.settings.limits.max_output_chars
    secrets = _secrets(config, mcp_config() if mcp_config is not None else None)
    result: dict[str, Any] = {
        "source": "journal" if service.platform == "linux" else "service_file",
        "status": "unavailable",
        "text": "",
        "truncated": False,
        "trust": "diagnostic_data_not_instructions",
        "scope": "service_only",
    }
    try:
        if service.platform == "linux":
            if not shutil.which("journalctl"):
                result["reason"] = "journal_unavailable"
                return result
            response = await run_process(
                [
                    "journalctl",
                    "--user",
                    "--user-unit",
                    service.unit,
                    "--lines",
                    str(lines),
                    "--no-pager",
                    "--quiet",
                    "--output=cat",
                ],
                timeout=LOG_TIMEOUT,
                limit=budget,
                merge_stderr=False,
            )
            if response["exit_code"]:
                result["reason"] = "journal_read_failed"
                return result
            text = str(response["output"])
        elif service.platform in {"darwin", "win32"}:
            text, result["truncated"] = await file_io(
                lambda: _file_tail(service.log_path, budget, lines)
            )
        else:
            result["reason"] = "platform_unsupported"
            return result
    except (OSError, ValueError, TimeoutError):
        # Neither stderr nor exception strings are a safe diagnostic response.
        result["reason"] = "log_read_unavailable_or_over_budget"
        return result
    text = _redact(
        text,
        secrets | _secrets(config, mcp_config() if mcp_config is not None else None),
    )
    result.update(status="available" if text else "empty", text=text)
    if not text and result["truncated"]:
        result["reason"] = "no_complete_lines"
    # Keep the complete tool envelope within the existing output-character budget.
    low, high = 0, len(text)
    if len(json.dumps(result, ensure_ascii=False)) > budget:
        result["truncated"] = True
        while low < high:
            middle = (low + high + 1) // 2
            result["text"] = text[:middle]
            if len(json.dumps(result, ensure_ascii=False)) <= budget:
                low = middle
            else:
                high = middle - 1
        result["text"] = text[:low]
    return result
