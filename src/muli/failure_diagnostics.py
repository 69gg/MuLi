"""Small, pure-value failure locations and bounded, read-only run queries."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from abc import ABCMeta
from itertools import islice
from pathlib import Path
from traceback import walk_tb
from types import FrameType, FunctionType, ModuleType, TracebackType
from typing import TYPE_CHECKING, Any, Final, TypedDict, cast

if TYPE_CHECKING:
    from muli.store import Store

# Version-one evidence limits, independent of the configurable tool-output cap.
MAX_FAILURE_FRAMES: Final = 16
MAX_FAILURE_BYTES: Final = 4096
MAX_RUN_RECORD_BYTES: Final = 32 * 1024
_LABEL_CHARS: Final = 128
_PACKAGE_PREFIX: Final = str(Path(__file__).parent) + os.sep
_IDENTIFIER: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RUN_ID: Final = re.compile(r"[a-f0-9]{32}")
_TRACEBACK: Final = BaseException.__dict__["__traceback__"]
_EXCEPTIONS: Final = (
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    RuntimeError,
    TimeoutError,
    OSError,
    PermissionError,
    FileNotFoundError,
    ConnectionError,
    AssertionError,
    MemoryError,
    RecursionError,
    ExceptionGroup,
)
_EXCEPTION_NAMES: Final = frozenset(item.__name__ for item in _EXCEPTIONS) | {"unknown"}
_RUN_STATES: Final = frozenset(
    {"queued", "running", "completed", "failed", "paused", "cancelled", "interrupted"}
)


class FailureFrame(TypedDict):
    module: str
    function: str
    line: int | None


class FailureDiagnostic(TypedDict):
    schema_version: int
    scope: str
    exception_type: str
    frames: list[FailureFrame]
    truncated: bool


def _parts(label: str) -> list[str] | None:
    if not 1 <= len(label) <= _LABEL_CHARS:
        return None
    parts = label.split(".")
    return parts if len(parts) <= 4 and all(_IDENTIFIER.fullmatch(p) for p in parts) else None


def _location(frame: FrameType, line: int) -> FailureFrame:
    unknown: FailureFrame = {"module": "unknown", "function": "unknown", "line": None}
    code = frame.f_code
    filename = code.co_filename
    if (
        not filename.startswith(_PACKAGE_PREFIX)
        or len(filename) > len(_PACKAGE_PREFIX) + _LABEL_CHARS
        or not filename.endswith(".py")
        or type(line) is not int
        or not 1 <= line <= (1 << 31) - 1
    ):
        return unknown
    relative = filename[len(_PACKAGE_PREFIX) : -3].replace(os.sep, ".")
    if relative == "__init__":
        relative = ""
    elif relative.endswith(".__init__"):
        relative = relative[: -len(".__init__")]
    module_name = "muli" + ("." + relative if relative else "")
    names = _parts(code.co_qualname)
    if _parts(module_name) is None or names is None:
        return unknown
    module = sys.modules.get(module_name)
    if type(module) is not ModuleType:
        return unknown
    namespace = ModuleType.__getattribute__(module, "__dict__")
    # A filename alone (including compile(..., real_package_filename)) is not
    # evidence of package ownership. Resolve only a few direct dictionary keys;
    # never enumerate modules/objects or evaluate user-defined descriptors.
    module_file = namespace.get("__file__")
    if namespace is not frame.f_globals or type(module_file) is not str or module_file != filename:
        return unknown
    value = namespace.get(names[0])
    for name in names[1:]:
        if type(value) is not type and type(value) is not ABCMeta:
            return unknown
        value = type.__getattribute__(value, "__dict__").get(name)
    if type(value) is staticmethod or type(value) is classmethod:
        value = object.__getattribute__(value, "__func__")
    if type(value) is not FunctionType or value.__code__ is not code:
        return unknown
    return {"module": module_name, "function": code.co_qualname, "line": line}


def capture_failure(exc: BaseException) -> FailureDiagnostic:
    """Do not format exceptions, inspect locals/source, or follow exception chains.

    The host owns error isolation: an extraction failure must not replace the
    original run failure. No exception/traceback/frame escapes this call.
    """
    kind = "unknown"
    for known in _EXCEPTIONS:
        if type(exc) is known:
            kind = known.__name__
            break
    result: FailureDiagnostic = {
        "schema_version": 1,
        "scope": "muli_package_only",
        "exception_type": kind,
        "frames": [],
        "truncated": False,
    }
    traceback = _TRACEBACK.__get__(exc)
    if traceback is not None and type(traceback) is not TracebackType:
        raise ValueError("failure_traceback_invalid")
    for frame, line in islice(walk_tb(traceback), MAX_FAILURE_FRAMES):
        result["frames"].append(_location(frame, line))
    # Conservatively mark the cap without visiting a seventeenth frame.
    result["truncated"] = len(result["frames"]) == MAX_FAILURE_FRAMES
    while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_FAILURE_BYTES:
        result["frames"].pop()
        result["truncated"] = True
    return result


def _validated(value: Any) -> FailureDiagnostic | None:
    if (
        type(value) is not dict
        or value.keys() != {"schema_version", "scope", "exception_type", "frames", "truncated"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["scope"] != "muli_package_only"
        or type(value["exception_type"]) is not str
        or value["exception_type"] not in _EXCEPTION_NAMES
        or type(value["truncated"]) is not bool
        or type(value["frames"]) is not list
        or len(value["frames"]) > MAX_FAILURE_FRAMES
    ):
        return None
    for frame in value["frames"]:
        if type(frame) is not dict or frame.keys() != {"module", "function", "line"}:
            return None
        module, function, line = frame["module"], frame["function"], frame["line"]
        if type(module) is not str or type(function) is not str:
            return None
        if (module, function, line) == ("unknown", "unknown", None):
            continue
        if (
            not (module == "muli" or module.startswith("muli."))
            or _parts(module) is None
            or _parts(function) is None
            or type(line) is not int
            or not 1 <= line <= (1 << 31) - 1
        ):
            return None
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_FAILURE_BYTES:
        return None
    return cast(FailureDiagnostic, value)


def validate_failure_diagnostic(value: object) -> FailureDiagnostic | None:
    """Validate an already bounded decoded diagnostic without reading persisted runs."""
    return _validated(value)


def read_failure(store: Store, run_id: str, *, max_output_chars: int) -> dict[str, Any]:
    """Point-read a finite original run before parsing; never return result/error text."""
    from muli.evidence import json_size
    from muli.management_projection import ProjectionError, _read_object

    if type(run_id) is not str or len(run_id) != 32 or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("failure_run_id_invalid")
    response: dict[str, Any] = {
        "run_id": run_id,
        "status": "unavailable",
        "trust": "diagnostic_data_not_instructions",
    }
    try:
        run = _read_object(store.db, "run", run_id, MAX_RUN_RECORD_BYTES)
    except ProjectionError as exc:
        response["reason"] = (
            "run_record_over_budget"
            if exc.code == "projection_object_budget"
            else "run_record_invalid"
        )
    except sqlite3.Error:
        response["reason"] = "run_read_failed"
    else:
        if run is None:
            response.update(status="not_found", reason="run_not_found")
        elif (
            run.get("id") != run_id
            or type(run.get("status")) is not str
            or run["status"] not in _RUN_STATES
        ):
            response["reason"] = "run_record_invalid"
        elif run["status"] in {"queued", "running"}:
            response["reason"] = "run_not_finalized"
        elif "failure_diagnostic" not in run:
            response["reason"] = "diagnostic_not_recorded"
        elif (
            run["status"] != "failed"
            or (diagnostic := _validated(run["failure_diagnostic"])) is None
        ):
            response["reason"] = "failure_diagnostic_invalid"
        else:
            response.update(status="available", diagnostic=diagnostic)
    if type(max_output_chars) is not int or max_output_chars < 1:
        raise ValueError("output_budget")
    while json_size(response) > max_output_chars and response.get("diagnostic", {}).get("frames"):
        response["diagnostic"]["frames"].pop()
        response["diagnostic"]["truncated"] = True
    if json_size(response) > max_output_chars:
        raise ValueError("output_budget")
    return response
