from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from test_install_profile import receipt
from test_managed_launcher import _slot

import muli
import muli.candidate_preparation as candidate
from muli.candidate_policy import (
    CandidateError,
    CandidateLimits,
    CandidateSettings,
    ProcessOutput,
)
from muli.candidate_preparation import (
    CandidateAttempt,
    CandidateEvidence,
    CandidateSource,
    FrozenPolicy,
    ReadyCandidate,
    create_attempt,
    inspect_policy,
    inspect_wheelhouse,
)
from muli.format_admission import CURRENT_FORMAT_STATE
from muli.platforms.install_receipt import read_managed_install_receipt

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux candidate file ownership")


@pytest.fixture(autouse=True)
def no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """These file/plan tests must never fall through to a real installer."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("candidate core tests must not start subprocesses")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


def wheel(path: Path, *, member: str = "fixture.py", content: bytes = b"fixture = True\n") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, content)


@pytest.fixture
def policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FrozenPolicy:
    slot = _slot(tmp_path, "current")
    monkeypatch.setattr(sys, "executable", slot.lexical_python)
    monkeypatch.setattr(sys, "prefix", slot.prefix)
    monkeypatch.setattr(muli, "__file__", str(Path(slot.package.root) / "__init__.py"))
    path = Path(slot.prefix) / "uv-receipt.toml"
    path.write_bytes(
        receipt(
            {
                "requirements": [
                    {"name": "muli-agent", "extras": ["mcp"], "specifier": ">=2,<3"},
                    {"name": "httpx", "specifier": ">=0.28", "marker": "python_version >= '3.12'"},
                ],
                "constraints": [{"name": "httpx", "specifier": "<0.29"}],
                "overrides": [{"name": "idna", "specifier": "==3.10"}],
                "entrypoints": [
                    {"name": "muli", "install-path": str(tmp_path / "current-bin/muli")}
                ],
            }
        )
    )
    observed = read_managed_install_receipt(slot, uv_version="0.13.1")
    materials, retained = tmp_path / "wheelhouse", tmp_path / "retained"
    materials.mkdir(mode=0o700)
    retained.mkdir(mode=0o700)
    wheel(materials / "muli_agent-2.23.0-py3-none-any.whl")
    uv = tmp_path / "uv"
    uv.write_bytes(b"unexecuted uv fixture")
    uv.chmod(0o700)
    limits = CandidateLimits(
        wheel_bytes=1024**2,
        compressed_bytes=1024**2,
        expanded_bytes=1024**2,
        candidate_bytes=1024**2,
        cache_bytes=1024**2,
        spare_bytes=1024,
    )
    settings = CandidateSettings(
        enabled=True,
        wheelhouse=str(materials),
        retained_root=str(retained),
        uv_executable=str(uv),
        limits=limits,
    )
    # Only live host authority is a fixture: all receipt/slot/file validation is real.
    return inspect_policy(settings, CandidateSource(slot, observed, "a" * 64, lambda: None))


def attempt(policy: FrozenPolicy, request_id: str = "request-one") -> CandidateAttempt:
    return create_attempt(
        policy,
        expected=policy.expected,
        request_id=request_id,
        version="2.23.0",
        configuration=b"{}",
    )


def install_fixture(value: CandidateAttempt) -> ReadyCandidate:
    """Supply fake process receipts, actual retained files; never execute uv/Python."""
    value.uv_version_spec()
    value.accept_uv_version(ProcessOutput(0, "uv 0.13.1 (fixture 2026-10-13)\n"))
    value.install_spec()
    slot = _slot(value.root / "tools", "muli-agent")
    declaration = Path(slot.declaration.path)
    declaration.write_bytes(declaration.read_bytes().replace(b"2.22.0", b"2.23.0"))
    intent = value.policy.original_intent
    tool = {
        "requirements": [
            item.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            for item in (intent.main, *intent.with_requirements)
        ],
        "constraints": [
            item.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            for item in intent.constraints
        ]
        + [{"name": "muli-agent", "specifier": "==2.23.0"}],
        "overrides": [
            item.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            for item in intent.overrides
        ],
        "python": value.policy.source.slot.lexical_python,
        "entrypoints": [{"name": "muli", "install-path": str(value.root / "entrypoints/muli")}],
        "options": {
            "no-index": True,
            "find-links": [(value.root / "wheelhouse").as_uri()],
            "no-build": True,
        },
    }
    (value.prefix / "uv-receipt.toml").write_bytes(receipt(tool))
    value.accept_install(ProcessOutput(0))
    value.dependency_spec()
    value.accept_dependencies(ProcessOutput(0))
    value.probe_spec()
    return value.finish(
        ProcessOutput(
            0,
            json.dumps(
                {
                    "python": str(value.python),
                    "prefix": str(value.prefix),
                    "base_prefix": "/fixture-base-python",
                    "package": slot.package.root,
                    "version": value.version,
                    "module_version": value.version,
                    "main_session": "fixture-main-session",
                    "format": CURRENT_FORMAT_STATE.model_dump(mode="json"),
                }
            ),
        )
    )


def test_disabled_source_never_opens_any_directory(
    policy: FrozenPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("disabled policy must not scan")

    monkeypatch.setattr(candidate, "inspect_wheelhouse", forbidden)
    with pytest.raises(CandidateError, match="^candidate_source_not_configured$"):
        inspect_policy(CandidateSettings(), policy.source)


@pytest.mark.parametrize("change", ["materials", "uv", "policy", "binding", "receipt"])
def test_expected_policy_covers_actual_inputs(policy: FrozenPolicy, change: str) -> None:
    source, settings = policy.source, policy.settings
    if change == "materials":
        wheel(Path(settings.wheelhouse) / policy.materials.wheels[0].name, content=b"changed")
    elif change == "uv":
        Path(settings.uv_executable).write_bytes(b"changed uv")
    elif change == "policy":
        settings = settings.model_copy(update={"source": "another-source"})
    elif change == "binding":
        source = replace(source, binding_revision="b" * 64)
    else:
        path = Path(source.receipt.receipt.path)
        path.write_bytes(path.read_bytes() + b"\n# same semantics, different evidence\n")
    with pytest.raises(CandidateError):
        repeated = inspect_policy(settings, source)
        repeated.verify(policy.expected)


@pytest.mark.parametrize(
    "kind", ["symlink", "fifo", "other-file", "unsafe-member", "invalid-wheel", "writable"]
)
def test_materials_require_bounded_owned_regular_wheels(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "materials"
    root.mkdir(mode=0o700)
    path = root / "demo-1.0-py3-none-any.whl"
    if kind == "symlink":
        target = tmp_path / "unfollowed"
        target.write_bytes(b"private")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "other-file":
        (root / "readme.txt").write_bytes(b"not a wheel")
    elif kind == "unsafe-member":
        wheel(path, member="../escape.py")
    elif kind == "invalid-wheel":
        path.write_bytes(b"not zip")
    else:
        wheel(path)
        path.chmod(0o666)
    with pytest.raises(CandidateError):
        inspect_wheelhouse(root, CandidateLimits())


@pytest.mark.parametrize("budget", ["compressed", "expanded", "entries", "central"])
def test_material_budget_is_total_and_precedes_zip_expansion(tmp_path: Path, budget: str) -> None:
    root = tmp_path / "materials"
    root.mkdir(mode=0o700)
    for index in range(2):
        path = root / f"demo{index}-1.0-py3-none-any.whl"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member in range(2 if budget != "central" else 32):
                archive.writestr(f"file-{member}.py", b"x" * 800)
    limits = CandidateLimits()
    if budget == "compressed":
        # Stored extra data is counted across files rather than per wheel.
        for path in root.iterdir():
            wheel(path, content=b"x" * 800)
        limits = limits.model_copy(update={"compressed_bytes": 1500})
    elif budget == "expanded":
        limits = limits.model_copy(update={"expanded_bytes": 2500})
    elif budget == "entries":
        limits = limits.model_copy(update={"archive_entries": 3})
    else:
        limits = limits.model_copy(update={"central_directory_bytes": 1024})
    with pytest.raises(CandidateError):
        inspect_wheelhouse(root, limits)


def test_materials_share_one_total_scan_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "materials"
    root.mkdir(mode=0o700)
    for index in range(2):
        wheel(root / f"demo{index}-1.0-py3-none-any.whl")
    original, ends = candidate._wheel, []
    clock = 1.0

    def observed(
        directory: Path, descriptor: int, name: str, limits: CandidateLimits, deadline: float
    ) -> candidate.WheelEvidence:
        nonlocal clock
        ends.append(deadline)
        result = original(directory, descriptor, name, limits, deadline)
        clock += 16
        return result

    monkeypatch.setattr(candidate.time, "monotonic", lambda: clock)
    monkeypatch.setattr(candidate, "_wheel", observed)
    with pytest.raises(CandidateError, match="^candidate_deadline_exceeded$"):
        inspect_wheelhouse(root, CandidateLimits())
    assert ends == [31.0, 31.0]


def test_final_path_is_exclusive_and_cannot_overwrite_current(policy: FrozenPolicy) -> None:
    first = attempt(policy)
    assert first.root.name == "candidate-request-one"
    with pytest.raises(CandidateError, match="^candidate_attempt_unavailable$"):
        attempt(policy)
    assert first.root.is_dir()
    protected = replace(policy.source, protected_roots=(Path(policy.settings.retained_root),))
    frozen = inspect_policy(policy.settings, protected)
    with pytest.raises(CandidateError, match="^candidate_path_conflict$"):
        attempt(frozen, "protected")


def test_install_requires_actual_supported_uv_version_and_cannot_repeat(
    policy: FrozenPolicy,
) -> None:
    value = attempt(policy)
    with pytest.raises(CandidateError, match="^candidate_stage_invalid$"):
        value.install_spec()
    spec = value.uv_version_spec()
    assert spec.argv[-1] == "--version"
    value.accept_uv_version(ProcessOutput(0, "uv 0.13.1 (fixture)\n"))
    installed = value.install_spec()
    assert installed.deadline == value.deadline
    with pytest.raises(CandidateError, match="^candidate_stage_invalid$"):
        value.install_spec()


@pytest.mark.parametrize("output", ["uv 0.14.0\n", "uv 0.13.2\n", "private arbitrary output"])
def test_unknown_actual_uv_never_produces_an_install_spec(
    policy: FrozenPolicy, output: str
) -> None:
    value = attempt(policy)
    value.uv_version_spec()
    with pytest.raises(CandidateError, match="^candidate_uv_unsupported$"):
        value.accept_uv_version(ProcessOutput(0, output))
    with pytest.raises(CandidateError, match="^candidate_stage_invalid$"):
        value.install_spec()


def test_argv_preserves_intent_and_environment_does_not_inherit_shell(
    policy: FrozenPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_INDEX", "https://secret.invalid")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://secret.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid")
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    value = attempt(policy)
    value.uv_version_spec()
    value.accept_uv_version(ProcessOutput(0, "uv 0.13.1\n"))
    spec = value.install_spec()
    assert spec.argv[-1] == "muli-agent[mcp]<3,>=2"
    assert "--offline" in spec.argv and "--no-build" in spec.argv and "--force" not in spec.argv
    assert 'httpx>=0.28; python_version >= "3.12"' in spec.argv
    assert (value.root / "constraints.txt").read_text(
        encoding="utf-8"
    ) == "httpx<0.29\nmuli-agent==2.23.0\n"
    assert (value.root / "overrides.txt").read_text(encoding="utf-8") == "idna==3.10\n"
    env = dict(spec.env)
    assert (
        not {"UV_INDEX", "PIP_EXTRA_INDEX_URL", "HTTPS_PROXY", "PYTHONPATH", "HOME", "CODEX_HOME"}
        & env.keys()
    )
    assert Path(env["NETRC"]).read_bytes() == b""
    assert list(Path(env["UV_CREDENTIALS_DIR"]).iterdir()) == []
    assert env["UV_KEYRING_PROVIDER"] == "disabled" and env["UV_LINK_MODE"] == "copy"


def test_copied_material_change_blocks_effect_without_cleaning_attempt(
    policy: FrozenPolicy,
) -> None:
    value = attempt(policy)
    copied = value.root / "wheelhouse" / value.copied.wheels[0].name
    wheel(copied, content=b"changed copy")
    with pytest.raises(CandidateError, match="^candidate_material_changed$"):
        value.uv_version_spec()
    assert copied.exists() and value.root.exists()


@pytest.mark.parametrize("change", ["specifier-url", "marker", "index"])
def test_requirement_fields_are_not_an_arbitrary_argv_escape(
    policy: FrozenPolicy, change: str
) -> None:
    original = policy.original_intent
    if change == "specifier-url":
        main = original.main.model_copy(update={"specifier": " @ file:///unapproved.whl"})
    elif change == "marker":
        main = original.main.model_copy(update={"marker": "invalid marker"})
    else:
        main = original.main.model_copy(update={"index": "https://custom.invalid/simple"})
    with pytest.raises(CandidateError):
        candidate._supported_intent(original.model_copy(update={"main": main}), policy.settings)


def test_custom_index_intent_is_not_silently_replaced_by_pypi(policy: FrozenPolicy) -> None:
    from muli.install_profile import ReceiptOptions

    intent = policy.original_intent.model_copy(
        update={"options": ReceiptOptions(index_url="https://private.invalid/simple")}
    )
    with pytest.raises(CandidateError, match="^candidate_context_unproven$"):
        candidate._supported_intent(intent, policy.settings)


def test_host_policy_guard_is_rechecked_after_a_process_wait(policy: FrozenPolicy) -> None:
    changed = False

    def verify() -> None:
        policy.source.verify()
        if changed:
            raise CandidateError("candidate_policy_changed")

    guarded = inspect_policy(policy.settings, replace(policy.source, verify=verify))
    value = attempt(guarded)
    value.uv_version_spec()
    changed = True
    with pytest.raises(CandidateError, match="^candidate_policy_changed$"):
        value.accept_uv_version(ProcessOutput(0, "uv 0.13.1\n"))


def test_failed_install_is_single_use_and_keeps_ownership(policy: FrozenPolicy) -> None:
    value = attempt(policy)
    value.uv_version_spec()
    value.accept_uv_version(ProcessOutput(0, "uv 0.13.1\n"))
    value.install_spec()
    with pytest.raises(CandidateError, match="^candidate_process_failed$"):
        value.accept_install(ProcessOutput(1, stderr="private solver detail"))
    with pytest.raises(CandidateError, match="^candidate_stage_invalid$"):
        value.install_spec()
    assert value.root.exists()


def test_disk_threshold_is_observed_without_claiming_quota(policy: FrozenPolicy) -> None:
    value = attempt(policy)
    (value.root / "tmp/oversized").write_bytes(b"x" * 2048)
    value.policy = replace(
        policy,
        settings=policy.settings.model_copy(
            update={"limits": policy.settings.limits.model_copy(update={"cache_bytes": 1024})}
        ),
    )
    with pytest.raises(CandidateError, match="^candidate_disk_threshold$"):
        value.observe()


def test_ready_uses_real_slot_and_receipt_and_revalidates(policy: FrozenPolicy) -> None:
    value = attempt(policy)
    ready = install_fixture(value)
    value.verify_ready(ready)
    assert ready.slot.prefix == str(value.prefix)
    assert ready.paths.python == value.python
    assert (
        CandidateEvidence.from_bytes(
            Path(ready.evidence_file.path).read_bytes(), policy.settings.limits.metadata_bytes
        )
        == ready.evidence
    )
    assert ready.evidence.original_intent == policy.original_intent
    assert ready.evidence.deployed_intent.constraints[-1].specifier == "==2.23.0"
    assert "retained" not in repr(ready)
    receipt_path = Path(ready.evidence.deployed_receipt.path)
    receipt_path.write_bytes(receipt_path.read_bytes() + b"\n# external change\n")
    with pytest.raises(ValueError):
        value.verify_ready(ready)


@pytest.mark.parametrize("fail_after_construction", [False, True])
def test_probe_constructs_and_discards_a_fresh_runtime_without_starting_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail_after_construction: bool,
) -> None:
    """Run the exact probe in-process; subprocess/installer launch stays forbidden."""
    import importlib.metadata

    from muli.runtime import Runtime

    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    config = tmp_path / "configuration.json"
    config.write_bytes(b"{}")
    monkeypatch.setenv("MULI_HOME", str(scratch / "home"))
    monkeypatch.setattr(sys, "argv", ["probe", str(config), str(scratch)])
    closed: list[bool] = []
    discard = Runtime.discard_unstarted
    version = importlib.metadata.version

    def forbidden(self: Runtime) -> None:
        raise AssertionError("probe must not start components")

    def record_close(self: Runtime) -> None:
        discard(self)
        closed.append(True)

    def metadata_version(distribution: str) -> str:
        if distribution == "muli-agent" and fail_after_construction:
            raise RuntimeError("probe result failure")
        return version(distribution)

    monkeypatch.setattr(Runtime, "startup", forbidden)
    monkeypatch.setattr(Runtime, "activate_components", forbidden)
    monkeypatch.setattr(Runtime, "discard_unstarted", record_close)
    monkeypatch.setattr(importlib.metadata, "version", metadata_version)
    if fail_after_construction:
        with pytest.raises(RuntimeError, match="^probe result failure$"):
            exec(compile(candidate._PROBE, "<candidate-probe>", "exec"), {})
    else:
        exec(compile(candidate._PROBE, "<candidate-probe>", "exec"), {})
        result = json.loads(capsys.readouterr().out)
        assert result["main_session"]
        assert result["format"] == CURRENT_FORMAT_STATE.model_dump(mode="json")
    assert closed == [True]
    assert (scratch / "data/muli.sqlite3").is_file()


def test_second_update_preserves_original_intent_without_pinning_old_deployment(
    policy: FrozenPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = install_fixture(attempt(policy))
    slot = ready.slot
    monkeypatch.setattr(sys, "executable", slot.lexical_python)
    monkeypatch.setattr(sys, "prefix", slot.prefix)
    monkeypatch.setattr(muli, "__file__", str(Path(slot.package.root) / "__init__.py"))
    current = read_managed_install_receipt(slot, uv_version="0.13.1")
    next_source = CandidateSource(slot, current, "b" * 64, lambda: None, ready.evidence)
    next_policy = inspect_policy(policy.settings, next_source)
    assert next_policy.original_intent == policy.original_intent
    assert next_policy.original_intent.main.specifier == ">=2,<3"
    assert next_policy.original_intent.constraints[-1].name == "httpx"
    wrong = replace(
        next_source, previous=ready.evidence.model_copy(update={"deployed_installation": "f" * 64})
    )
    with pytest.raises(CandidateError, match="^candidate_intent_chain_changed$"):
        inspect_policy(policy.settings, wrong)
