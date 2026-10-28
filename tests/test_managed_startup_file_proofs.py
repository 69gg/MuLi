"""Startup content proofs belong to one controller/selected synchronous pair."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
import test_managed_boot as boot_fixtures
import test_managed_launcher as launcher_fixtures
from test_managed_backend_python_proof import CountedReader, PythonReads
from test_managed_boot import host as host
from test_managed_launcher import deployment as deployment

from muli import managed_boot
from muli.managed_transaction import TransactionRecordV2, raw_snapshot
from muli.platforms import managed_deployment, managed_launcher
from muli.platforms.managed_deployment import ManagedDeploymentError, SlotIdentity
from muli.platforms.managed_launcher import LaunchError
from muli.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux retained startup identities")

_PAYLOAD = Path("proof_payload/nested.bin")
_CONTENT = b"retained startup content\n" * 11


@dataclass
class Startup:
    kind: Literal["launcher", "boot"]
    controller: SlotIdentity
    selected: SlotIdentity
    call: Callable[[], object]
    open_store: Callable[[], Store] | None = None


@pytest.fixture
def variant(request: pytest.FixtureRequest) -> str:
    value: str = getattr(request, "param", "shared")
    return value


@pytest.fixture(params=("launcher", "boot"))
def startup(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> Startup:
    binary = tmp_path / "shared-startup-python"
    binary.write_bytes(b"\x7fELF" + bytes(range(256)) * 257)
    binary.chmod(0o700)

    def register(prefix: Path, python: Path, package: Path) -> SlotIdentity:
        target = binary
        if prefix.name == "target" and variant == "different-binary":
            target = tmp_path / "other-startup-python"
            target.write_bytes(binary.read_bytes())
            target.chmod(0o700)
        python.unlink()
        python.symlink_to(target)
        payload = package / _PAYLOAD
        payload.parent.mkdir()
        payload.write_bytes(_CONTENT)
        limits = binary.stat().st_size + (1 if prefix.name == "target" else 17)
        return managed_deployment.register_local_slot(prefix, python, package, python_bytes=limits)

    kind: Literal["launcher", "boot"] = "launcher" if request.param == "launcher" else "boot"
    module = launcher_fixtures if kind == "launcher" else boot_fixtures
    monkeypatch.setattr(module, "register_local_slot", register)
    if kind == "launcher":
        source: launcher_fixtures.Deployment = request.getfixturevalue("deployment")
        if variant == "same-slot":
            state = source.record().model_dump(mode="json")
            state["action"] = "adopt"
            state["target"] = state["base"]
            state["base"] = {
                **state["base"],
                "entry": "legacy",
                "selector": None,
                "unit": raw_snapshot(
                    boot_fixtures._LEGACY_UNIT, source.anchor.journal.max_snapshot_bytes
                ).model_dump(mode="json"),
            }
            adopted = TransactionRecordV2.model_validate(state)
            source.target = source.base
            source.target_selector = source.base_selector
            source.select(source.target_selector)
            launcher_fixtures._write_private(
                source.root / "state.json", adopted.model_dump_json().encode()
            )
        return Startup(
            kind,
            source.base,
            source.target,
            lambda: managed_launcher.resolve_entry(source.root, purpose="service"),
        )
    boot_source: boot_fixtures.Host = request.getfixturevalue("host")
    if variant == "same-slot":
        boot_source.record(legacy=True)
    permit = boot_source.acquire()
    return Startup(
        kind,
        boot_source.base,
        boot_source.base if variant == "same-slot" else boot_source.target,
        permit._observe,
        permit.open_store,
    )


def observe_reads(
    startup: Startup, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[Path, PythonReads], dict[Path, int]]:
    slots = (startup.controller, startup.selected)
    paths = {Path(slot.resolved_python.path) for slot in slots}
    paths.update(Path(slot.package.root) / _PAYLOAD for slot in slots)
    reads = {path: PythonReads() for path in paths}
    identities = {
        (info.st_dev, info.st_ino): reads[path] for path in paths for info in (path.stat(),)
    }
    roots = {Path(slot.package.root) for slot in slots}
    scans = {path: 0 for root in roots for path in (root, root / _PAYLOAD.parent)}
    directories = {(info.st_dev, info.st_ino): path for path in scans for info in (path.stat(),)}
    original_open, original_fdopen = os.open, os.fdopen
    original_scandir = os.scandir

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        info = os.fstat(descriptor)
        observed = identities.get((info.st_dev, info.st_ino))
        if observed is not None:
            observed.opened.append(flags)
        return descriptor

    def stream(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        source = original_fdopen(descriptor, *args, **kwargs)
        info = os.fstat(source.fileno())
        observed = identities.get((info.st_dev, info.st_ino))
        return source if observed is None else CountedReader(source, observed)

    def scandir(path: Any = ".") -> Any:
        if isinstance(path, int):
            info = os.fstat(path)
            current = directories.get((info.st_dev, info.st_ino))
        else:
            current = Path(os.fsdecode(path))
        if current in scans:
            scans[current] += 1
        return original_scandir(path)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fdopen", stream)
    monkeypatch.setattr(os, "scandir", scandir)
    return reads, scans


@pytest.mark.parametrize("variant", ("shared", "different-binary", "same-slot"), indirect=True)
def test_each_startup_pair_reopens_every_file_and_the_next_pair_reads_again(
    startup: Startup, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads, scans = observe_reads(startup, monkeypatch)
    slots = (startup.controller, startup.selected)
    for attempt in (1, 2):
        startup.call()
        for path, observed in reads.items():
            assert observed.bytes == path.stat().st_size * attempt
            expected_opens = sum(
                path in {Path(slot.resolved_python.path), Path(slot.package.root) / _PAYLOAD}
                for slot in slots
            )
            assert len(observed.opened) == expected_opens * attempt
            assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)
        for root, count in scans.items():
            assert count == sum(root.is_relative_to(slot.package.root) for slot in slots) * attempt


@pytest.mark.parametrize("startup", ("boot",), indirect=True)
@pytest.mark.parametrize("variant", ("shared", "same-slot"), indirect=True)
def test_open_store_uses_two_fresh_pairs_after_acquire(
    startup: Startup, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads, scans = observe_reads(startup, monkeypatch)
    assert startup.open_store is not None
    with closing(startup.open_store()) as store:
        assert store.db.total_changes == 0
    for path, observed in reads.items():
        assert observed.bytes == 2 * path.stat().st_size
    for root, count in scans.items():
        assert count == 2 * sum(
            root.is_relative_to(slot.package.root)
            for slot in (startup.controller, startup.selected)
        )


def at_selected(
    startup: Startup, monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]
) -> list[bool]:
    module = managed_launcher if startup.kind == "launcher" else managed_boot
    original = module.verify_slot_files
    calls: list[bool] = []

    def verify(slot: Any, **kwargs: Any) -> None:
        calls.append(True)
        if len(calls) == 2:
            change()
        original(slot, **kwargs)

    monkeypatch.setattr(module, "verify_slot_files", verify)
    return calls


@pytest.mark.parametrize("variant", ("shared", "same-slot"), indirect=True)
@pytest.mark.parametrize(
    "fault",
    (
        "python-rewrite",
        "python-replace",
        "python-link",
        "package-rewrite",
        "package-new",
        "package-delete",
        "package-link",
    ),
)
def test_selected_pair_rejects_changes_after_the_controller_check(
    startup: Startup, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    def change() -> None:
        binary = Path(startup.selected.resolved_python.path)
        payload = Path(startup.selected.package.root) / _PAYLOAD
        if fault == "python-rewrite":
            before = binary.stat()
            with binary.open("r+b") as output:
                output.write(b"BAD!")
            os.utime(binary, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert binary.stat().st_ctime_ns != before.st_ctime_ns
        elif fault == "python-replace":
            replacement = binary.with_name("replacement-python")
            replacement.write_bytes(binary.read_bytes())
            replacement.chmod(0o700)
            replacement.replace(binary)
        elif fault == "python-link":
            replacement = binary.with_name("different-python")
            replacement.write_bytes(binary.read_bytes())
            replacement.chmod(0o700)
            lexical = Path(startup.selected.lexical_python)
            lexical.unlink()
            lexical.symlink_to(replacement)
        elif fault == "package-rewrite":
            before = payload.stat()
            payload.write_bytes(b"x" * before.st_size)
            os.utime(payload, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert payload.stat().st_ctime_ns != before.st_ctime_ns
        elif fault == "package-new":
            payload.with_name("new.bin").write_bytes(b"unexpected leaf")
        elif fault == "package-delete":
            payload.unlink()
        else:
            payload.unlink()
            payload.symlink_to("../compatibility.json")

    calls = at_selected(startup, monkeypatch, change)
    with pytest.raises((LaunchError, ManagedDeploymentError, OSError)):
        startup.call()
    assert calls == [True, True]


def test_shared_python_proof_keeps_the_selected_byte_budget(
    startup: Startup, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = managed_launcher if startup.kind == "launcher" else managed_boot
    original = module.verify_slot_files
    calls: list[bool] = []

    def verify(slot: Any, **kwargs: Any) -> None:
        calls.append(True)
        if len(calls) == 2:
            budget = startup.selected.resolved_python.identity.size - 1
            slot = (
                {**slot, "python_bytes": budget}
                if startup.kind == "launcher"
                else slot.model_copy(update={"python_bytes": budget})
            )
        original(slot, **kwargs)

    monkeypatch.setattr(module, "verify_slot_files", verify)
    with pytest.raises((LaunchError, ManagedDeploymentError)):
        startup.call()
    assert calls == [True, True]
