"""Standalone, standard-library-only routing for a retained Linux deployment.

The exact file is copied to the private journal root. Running that copy must not
import either installed MuLi slot before deciding which entry may be executed.
The selected program still has to validate its own process and boot admission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NoReturn

_RECORD_BYTES = 131_072
_DECLARATION_BYTES = 16_384
_LAUNCHER_BYTES = 262_144
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}\Z")
_RELEASED = {"released", "released_degraded"}
_PHASES = {
    "prepared",
    "draining",
    "activating",
    "verifying",
    "rolling_back",
    "committed",
    "rolled_back",
    "aborted",
    "recovery_required",
}

type PackageProofs = dict[str, dict[str, dict[str, Any]]]


class LaunchError(ValueError):
    """Never include private paths, raw records or exception text in a result."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    deployment: str = field(repr=False)
    installation: str = field(repr=False)
    python: Path = field(repr=False)
    argv: tuple[str, ...] = field(repr=False)
    generation: str
    revision: int
    state_digest: str = field(repr=False)
    role: Literal["base", "target", "controller"]


@dataclass(frozen=True, slots=True)
class RepairStatus:
    """A diagnosis, never authorization to replace a selector or an anchor."""

    code: str
    protocol: int | None = None
    generation: str | None = None
    revision: int | None = None
    phase: str | None = None
    resolved: bool = False
    selector: Literal["unknown", "present", "absent", "invalid"] = "unknown"


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _constant(value: str) -> NoReturn:
    raise ValueError("invalid_constant")


def object_bytes(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)
        if not isinstance(value, dict):
            raise ValueError("not_object")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise LaunchError("managed_record_invalid") from None


def absolute(value: object) -> Path:
    if not isinstance(value, str) or len(os.fsencode(value)) > 4096:
        raise LaunchError("managed_path_invalid")
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or any(character in value for character in "\x00\r\n")
    ):
        raise LaunchError("managed_path_invalid")
    return path


def identity(info: os.stat_result) -> dict[str, int]:
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "modified_ns": info.st_mtime_ns,
        "changed_ns": info.st_ctime_ns,
        "mode": info.st_mode,
        "uid": info.st_uid,
    }


def _open_directory(path: Path) -> int:
    if sys.platform != "linux":
        raise LaunchError("platform_unsupported")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in absolute(str(path)).parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _check_directory(path: Path, descriptor: int) -> None:
    current = _open_directory(path)
    try:
        before, after = os.fstat(descriptor), os.fstat(current)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise LaunchError("managed_directory_changed")
    finally:
        os.close(current)


@contextmanager
def directory(path: Path, *, private: bool = False) -> Iterator[int]:
    descriptor = _open_directory(path)
    try:
        info = os.fstat(descriptor)
        if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise LaunchError("managed_directory_not_private")
        yield descriptor
        _check_directory(path, descriptor)
    finally:
        os.close(descriptor)


def read_at(
    descriptor: int, name: str, limit: int, *, private: bool = False
) -> tuple[bytes, dict[str, int]]:
    file_descriptor = os.open(
        name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
    )
    with os.fdopen(file_descriptor, "rb") as source:
        before = identity(os.fstat(source.fileno()))
        if (
            not stat.S_ISREG(before["mode"])
            or before["size"] > limit
            or (
                private and (before["uid"] != os.geteuid() or stat.S_IMODE(before["mode"]) != 0o600)
            )
        ):
            raise LaunchError("managed_file_unsupported")
        data = source.read(limit + 1)
        after = identity(os.fstat(source.fileno()))
    current = identity(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
    if len(data) > limit or before != after or before != current:
        raise LaunchError("managed_file_changed")
    return data, before


def read_file(path: Path, limit: int, *, private: bool = False) -> tuple[bytes, dict[str, int]]:
    with directory(path.parent, private=private) as descriptor:
        return read_at(descriptor, path.name, limit, private=private)


def _fingerprint_at(
    parent: int,
    name: str,
    path: Path,
    limit: int,
    *,
    verified: dict[str, Any] | None = None,
) -> dict[str, object]:
    hashed, size = hashlib.sha256(), 0
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
    )
    with os.fdopen(descriptor, "rb") as source:
        before = identity(os.fstat(source.fileno()))
        if not stat.S_ISREG(before["mode"]) or before["size"] > limit:
            raise LaunchError("managed_file_unsupported")
        # Reusing bytes never replaces this file's independent open, budget
        # check, or descriptor and path identity checks.
        if (
            verified is not None
            and verified.get("path") == str(path)
            and verified.get("identity") == before
            and type(verified.get("digest")) is str
            and _DIGEST.fullmatch(verified["digest"]) is not None
        ):
            checksum = verified["digest"]
        else:
            while chunk := source.read(min(65_536, limit - size + 1)):
                size += len(chunk)
                if size > limit:
                    raise LaunchError("managed_file_unsupported")
                hashed.update(chunk)
            checksum = hashed.hexdigest()
        after = identity(os.fstat(source.fileno()))
    if before != after or before != identity(os.stat(name, dir_fd=parent, follow_symlinks=False)):
        raise LaunchError("managed_file_changed")
    return {"path": str(path), "identity": before, "digest": checksum}


def fingerprint(
    path: Path, limit: int, *, verified: dict[str, Any] | None = None
) -> dict[str, object]:
    with directory(path.parent) as parent:
        return _fingerprint_at(parent, path.name, path, limit, verified=verified)


def python_fingerprint(
    entry: Path, limit: int, *, verified: dict[str, Any] | None = None
) -> dict[str, object]:
    """Follow only a bounded chain of final-component interpreter links."""
    target = absolute(str(entry))
    links: list[tuple[Path, dict[str, int], str]] = []
    for _ in range(40):
        with directory(target.parent) as descriptor:
            before = identity(os.stat(target.name, dir_fd=descriptor, follow_symlinks=False))
            if stat.S_ISLNK(before["mode"]):
                value = os.readlink(target.name, dir_fd=descriptor)
                if before != identity(
                    os.stat(target.name, dir_fd=descriptor, follow_symlinks=False)
                ):
                    raise LaunchError("managed_python_changed")
                links.append((target, before, value))
                target = absolute(os.path.normpath(target.parent / value))
                continue
            if not before["mode"] & 0o111:
                raise LaunchError("managed_python_not_executable")
            result = (
                fingerprint(target, limit)
                if verified is None
                else fingerprint(target, limit, verified=verified)
            )
            break
    else:
        raise LaunchError("managed_python_link_budget")
    for path, expected, value in links:
        with directory(path.parent) as descriptor:
            if expected != identity(
                os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            ) or value != os.readlink(path.name, dir_fd=descriptor):
                raise LaunchError("managed_python_changed")
    return result


def package_fingerprint(
    root: Path,
    limits: Mapping[str, int],
    *,
    verified: dict[str, dict[str, Any]] | None = None,
    observed: dict[str, dict[str, Any]] | None = None,
) -> dict[str, object]:
    """Hash a bounded, no-link package without importing it or reading pyc files."""
    hashed = hashlib.sha256()
    visited, files, size = 0, 0, 0

    def walk(descriptor: int, prefix: str, depth: int) -> None:
        nonlocal visited, files, size
        before = identity(os.fstat(descriptor))
        names: list[str] = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                visited += 1
                if visited > limits["entries"]:
                    raise LaunchError("managed_package_entry_budget")
                if entry.name == "__pycache__" or entry.name.endswith((".pyc", ".pyo")):
                    continue
                names.append(entry.name)
        for name in sorted(names):
            relative = prefix + name
            current = identity(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
            if stat.S_ISDIR(current["mode"]):
                if depth >= limits["depth"]:
                    raise LaunchError("managed_package_depth_budget")
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                try:
                    if current != identity(os.fstat(child)):
                        raise LaunchError("managed_file_changed")
                    hashed.update(canonical([relative, "directory"]) + b"\n")
                    walk(child, relative + "/", depth + 1)
                    if current != identity(os.stat(name, dir_fd=descriptor, follow_symlinks=False)):
                        raise LaunchError("managed_file_changed")
                finally:
                    os.close(child)
            else:
                path = root / relative
                proof = _fingerprint_at(
                    descriptor,
                    name,
                    path,
                    min(limits["file_bytes"], limits["total_bytes"] - size),
                    verified=None if verified is None else verified.get(str(path)),
                )
                info = _dict(proof["identity"])
                size += info["size"]
                files += 1
                hashed.update(canonical([relative, info["size"], proof["digest"]]) + b"\n")
                if observed is not None:
                    observed[str(path)] = proof
        if before != identity(os.fstat(descriptor)):
            raise LaunchError("managed_directory_changed")

    with directory(root) as descriptor:
        walk(descriptor, "", 0)
    if files == 0:
        raise LaunchError("managed_package_empty")
    return {"root": str(root), "digest": hashed.hexdigest(), "files": files, "size": size}


def _hash(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise LaunchError("managed_record_invalid")
    return value


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LaunchError("managed_record_invalid")
    return value


def _version(value: dict[str, Any]) -> None:
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise LaunchError("managed_record_invalid")


def _limits(value: object) -> dict[str, int]:
    limits = _dict(value)
    for key, maximum in {
        "entries": 2048,
        "total_bytes": 33_554_432,
        "file_bytes": 8_388_608,
        "depth": 32,
    }.items():
        if type(limits.get(key)) is not int or not 1 <= limits[key] <= maximum:
            raise LaunchError("managed_record_invalid")
    return limits


def verify_slot_files(
    slot: dict[str, Any],
    *,
    verified_python: dict[str, Any] | None = None,
    verified_packages: PackageProofs | None = None,
    observed_packages: PackageProofs | None = None,
) -> None:
    _version(slot)
    prefix = absolute(slot.get("prefix"))
    python = absolute(slot.get("lexical_python"))
    package = _dict(slot.get("package"))
    root = absolute(package.get("root"))
    if prefix == Path("/") or not python.is_relative_to(prefix) or not root.is_relative_to(prefix):
        raise LaunchError("managed_slot_unsupported")
    limits = _limits(slot.get("package_limits"))
    python_bytes = slot.get("python_bytes")
    if type(python_bytes) is not int or not 1 <= python_bytes <= 67_108_864:
        raise LaunchError("managed_record_invalid")
    actual_python = (
        python_fingerprint(python, python_bytes)
        if verified_python is None
        else python_fingerprint(python, python_bytes, verified=verified_python)
    )
    if actual_python != slot.get("resolved_python"):
        raise LaunchError("managed_python_changed")
    if fingerprint(prefix / "pyvenv.cfg", limits["file_bytes"]) != slot.get("pyvenv"):
        raise LaunchError("managed_python_changed")
    declaration_path = root / "compatibility.json"
    if fingerprint(declaration_path, _DECLARATION_BYTES) != slot.get("declaration"):
        raise LaunchError("managed_declaration_changed")
    declaration, _ = read_file(declaration_path, _DECLARATION_BYTES)
    metadata = object_bytes(declaration)
    if (
        type(metadata.get("handoff_protocol")) is not int
        or metadata["handoff_protocol"] != 2
        or metadata.get("requires_migration") is not False
        or metadata != slot.get("compatibility")
    ):
        raise LaunchError("managed_slot_changed")
    if verified_packages is None and observed_packages is None:
        if package_fingerprint(root, limits) != package:
            raise LaunchError("managed_slot_changed")
    else:
        # The complete slot includes its exact package root and all limits.
        # Neither its aggregate package digest nor another role grants leaf
        # evidence for this scope.
        scope = digest(canonical(slot))
        leaves: dict[str, dict[str, Any]] = {}
        if (
            package_fingerprint(
                root,
                limits,
                verified=None if verified_packages is None else verified_packages.get(scope),
                observed=leaves,
            )
            != package
        ):
            raise LaunchError("managed_slot_changed")
        if observed_packages is not None:
            observed_packages[scope] = leaves


def _content(root: Path, prefix: str, expected: str) -> tuple[dict[str, Any], bytes]:
    data, _ = read_file(root / f"{prefix}-{_hash(expected)}.json", _RECORD_BYTES, private=True)
    if digest(data) != expected:
        raise LaunchError("managed_record_changed")
    value = object_bytes(data)
    _version(value)
    return value, data


def _resolved(record: dict[str, Any]) -> bool:
    if (
        not isinstance(record.get("phase"), str)
        or not isinstance(record.get("intent"), str)
        or not isinstance(record.get("activation"), str)
    ):
        raise LaunchError("managed_record_invalid")
    return record.get("phase") == "aborted" or (
        record.get("phase") in {"committed", "rolled_back"}
        and record.get("activation") in _RELEASED
    )


def _role(record: dict[str, Any], purpose: str) -> Literal["base", "target"]:
    if _resolved(record):
        return "target" if record["phase"] == "committed" else "base"
    if purpose != "service":
        raise LaunchError("transaction_in_progress")
    phase, intent = record.get("phase"), record.get("intent")
    if (phase == "activating" and intent == "start_target") or phase in {"verifying", "committed"}:
        return "target"
    if (
        phase == "rolling_back" and intent in {"start_base", "verify_base"}
    ) or phase == "rolled_back":
        return "base"
    raise LaunchError("transaction_in_progress")


def _anchor(root: Path, record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    deployment = _hash(_dict(record.get("identity")).get("deployment"))
    anchor, _ = _content(root, "anchor", deployment)
    if absolute(anchor.get("journal_root")) != root:
        raise LaunchError("managed_paths_changed")
    launcher = _dict(anchor.get("launcher"))
    if (
        absolute(launcher.get("path")) != root / "launcher.py"
        or fingerprint(root / "launcher.py", _LAUNCHER_BYTES) != launcher
    ):
        raise LaunchError("anchor_unavailable")
    return deployment, anchor


def resolve_entry(root: Path, *, purpose: str, arguments: Sequence[str] = ()) -> LaunchPlan:
    """Read routing twice; full protocol/lease/format admission belongs to the child."""
    if purpose not in {"service", "cli", "recover"}:
        raise LaunchError("managed_purpose_unsupported")
    root = absolute(str(root))
    data, record_identity = read_file(root / "state.json", _RECORD_BYTES, private=True)
    record = object_bytes(data)
    if type(record.get("protocol")) is not int or record["protocol"] != 2:
        raise LaunchError("managed_protocol_unsupported")
    generation, revision = record.get("generation"), record.get("revision")
    if (
        not isinstance(generation, str)
        or _IDENTIFIER.fullmatch(generation) is None
        or type(revision) is not int
        or revision < 1
    ):
        raise LaunchError("managed_record_invalid")
    route_role = None if purpose == "recover" else _role(record, purpose)
    deployment, anchor = _anchor(root, record)
    controller, _ = _content(root, "slot", _hash(anchor.get("controller_slot")))
    verify_slot_files(controller)
    if purpose == "recover":
        if arguments:
            raise LaunchError("managed_arguments_invalid")
        python = absolute(controller.get("lexical_python"))
        argv: tuple[str, ...] = (
            str(python),
            "-I",
            "-m",
            "muli.managed_host",
            "--root",
            str(root),
            "recover",
        )
        if read_file(root / "state.json", _RECORD_BYTES, private=True) != (data, record_identity):
            raise LaunchError("managed_record_changed")
        return LaunchPlan(
            deployment,
            _hash(anchor["controller_slot"]),
            python,
            argv,
            generation,
            revision,
            digest(data),
            "controller",
        )
    if route_role is None:
        raise LaunchError("managed_purpose_unsupported")
    role = route_role
    binding = _dict(record.get(role))
    installation = _hash(binding.get("installation"))
    selector_reference = binding.get("selector")
    if binding.get("entry") == "managed":
        selector_data, _ = read_file(root / "selector.json", _DECLARATION_BYTES, private=True)
        selector = object_bytes(selector_data)
        if (
            selector
            != {"schema_version": 1, "deployment": deployment, "installation": installation}
            or type(selector.get("schema_version")) is not int
            or digest(selector_data) != _dict(selector_reference).get("digest")
        ):
            raise LaunchError("managed_selector_changed")
    elif binding.get("entry") == "legacy" and selector_reference is None:
        try:
            read_file(root / "selector.json", _DECLARATION_BYTES, private=True)
        except FileNotFoundError:
            pass
        else:
            raise LaunchError("managed_selector_changed")
    else:
        raise LaunchError("managed_record_invalid")
    slot, _ = _content(root, "slot", installation)
    verify_slot_files(slot)
    python = absolute(slot.get("lexical_python"))
    config = absolute(anchor.get("config_path"))
    data_dir = absolute(anchor.get("data_dir"))
    command = ("serve",) if purpose == "service" else tuple(arguments)
    reserved = ("--config", "--data-dir", "--managed-root")
    if any(
        not isinstance(value, str)
        or "\x00" in value
        or (
            (option := value.split("=", 1)[0]).startswith("--")
            and option != "--"
            and any(flag.startswith(option) for flag in reserved)
        )
        for value in command
    ):
        raise LaunchError("managed_arguments_invalid")
    argv = (
        str(python),
        "-I",
        "-m",
        "muli",
        "--config",
        str(config),
        "--data-dir",
        str(data_dir),
        "--managed-root",
        str(root),
        *command,
    )
    if read_file(root / "state.json", _RECORD_BYTES, private=True) != (data, record_identity):
        raise LaunchError("managed_record_changed")
    # Re-read the selector after bounded package reads as well. The child checks
    # the same state position again after acquiring its actual writer lease.
    if (
        binding["entry"] == "managed"
        and read_file(root / "selector.json", _DECLARATION_BYTES, private=True)[0] != selector_data
    ):
        raise LaunchError("managed_selector_changed")
    return LaunchPlan(
        deployment, installation, python, argv, generation, revision, digest(data), role
    )


def inspect_repair(root: Path) -> RepairStatus:
    """Read fixed private metadata even when a selected slot cannot be imported.

    No candidate package, configuration or database is read. Missing or damaged
    metadata stays an explicit error; it never means an unmanaged deployment.
    """
    try:
        root = absolute(str(root))
        data, _ = read_file(root / "state.json", _RECORD_BYTES, private=True)
        record = object_bytes(data)
        protocol = record.get("protocol")
        if type(protocol) is not int or protocol != 2:
            return RepairStatus("managed_protocol_unsupported")
        generation, revision, phase = (
            record.get("generation"),
            record.get("revision"),
            record.get("phase"),
        )
        if (
            not isinstance(generation, str)
            or _IDENTIFIER.fullmatch(generation) is None
            or type(revision) is not int
            or revision < 1
            or not isinstance(phase, str)
            or phase not in _PHASES
        ):
            raise LaunchError("managed_record_invalid")
        position: dict[str, Any] = dict(
            protocol=protocol, generation=generation, revision=revision, phase=phase
        )
        try:
            deployment, _ = _anchor(root, record)
        except (OSError, ValueError):
            return RepairStatus("anchor_unavailable", **position)
        try:
            selector_data, _ = read_file(root / "selector.json", _DECLARATION_BYTES, private=True)
        except FileNotFoundError:
            selector: Literal["unknown", "present", "absent", "invalid"] = "absent"
        else:
            value = object_bytes(selector_data)
            selector = (
                "present"
                if value.get("deployment") == deployment
                and type(value.get("schema_version")) is int
                and value["schema_version"] == 1
                and isinstance(value.get("installation"), str)
                and _DIGEST.fullmatch(value["installation"]) is not None
                else "invalid"
            )
        return RepairStatus(
            "recovery_requires_anchor_verification",
            **position,
            resolved=_resolved(record),
            selector=selector,
        )
    except (OSError, ValueError):
        return RepairStatus("managed_metadata_unavailable")


def execute(plan: LaunchPlan) -> NoReturn:
    os.execve(plan.python, plan.argv, dict(os.environ))
    raise AssertionError("exec_returned")


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MuLi retained deployment entry")
    parser.add_argument("--root", required=True)
    parser.add_argument("purpose", choices=("service", "cli", "recover", "repair"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(arguments)
    try:
        if options.purpose == "repair":
            status = inspect_repair(Path(options.root))
            print(
                json.dumps(
                    {
                        "code": status.code,
                        "protocol": status.protocol,
                        "generation": status.generation,
                        "revision": status.revision,
                        "phase": status.phase,
                        "resolved": status.resolved,
                        "selector": status.selector,
                    },
                    separators=(",", ":"),
                )
            )
            return 0
        execute(
            resolve_entry(Path(options.root), purpose=options.purpose, arguments=options.arguments)
        )
    except (OSError, ValueError) as error:
        code = error.code if isinstance(error, LaunchError) else "managed_entry_unavailable"
        print(json.dumps({"ok": False, "code": code}, separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
