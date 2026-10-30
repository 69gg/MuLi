"""Preparation retains lineage's two real source guards without a third one."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_managed_preparation import Host
from test_managed_preparation import host as host

import muli.managed_preparation as preparation_module
import muli.platforms.managed_launcher as launcher_module
from muli.managed_preparation import CurrentManagedInstallation, ManagedPreparation
from muli.management_host import RecoveryHost
from muli.runtime import Runtime
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed preparation")


async def test_managed_prepare_source_guard_uses_lineage_two_real_authentications(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    adopted = host.complete(await host.run(host.plan("adopt"), candidate=host.slot))
    item = host.plan("restart", name="next-preparation")
    position = host.runtime.store.event_position()
    source_checks = 0
    python_checks = 0
    verified: list[tuple[int, int]] = []
    original_python = launcher_module.python_fingerprint

    def python(
        path: Path, limit: int, *, verified: dict[str, Any] | None = None
    ) -> dict[str, object]:
        nonlocal python_checks
        result = original_python(path, limit, verified=verified)
        python_checks += 1
        return result

    class ObservedPreparation(ManagedPreparation):
        def __init__(self, runtime: Runtime, service: UserService) -> None:
            super().__init__(runtime, service, probe=host.probe, clock=host.prepare.clock)

        def _source(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal source_checks
            result = super()._source(*args, **kwargs)
            source_checks += 1
            return result

        async def observe_current(self) -> CurrentManagedInstallation:
            current = await super().observe_current()

            def verify() -> None:
                before = source_checks, python_checks
                current.verify()
                # Each counted guard really rereads source evidence and hashes
                # its retained Python; it is never an empty counting callback.
                delta = source_checks - before[0], python_checks - before[1]
                assert delta[0] == 1 and delta[1] > 0
                verified.append(delta)

            return replace(current, verify=verify)

    class CandidateGuardRejected(Exception):
        pass

    def candidate_guard() -> None:
        # read_lineage runs once at admission and again inside source_guard.
        # Both calls retain their entry and return-side full authentication.
        assert len(verified) == 4, (
            "source_guard duplicated read_lineage's complete entry authentication",
            len(verified),
        )
        raise CandidateGuardRejected

    monkeypatch.setattr(launcher_module, "python_fingerprint", python)
    monkeypatch.setattr(preparation_module, "ManagedPreparation", ObservedPreparation)
    recovery = RecoveryHost(host.runtime, host.service)

    async with host.runtime.configuration_lock:
        with pytest.raises(CandidateGuardRejected):
            await recovery._prepare_managed(
                item, host.runtime.config.settings, candidate_guard=candidate_guard
            )

    assert host.journal.read_current() == adopted
    assert host.runtime.store.get("management", item["id"]) == item
    assert host.runtime.store.event_position() == position
