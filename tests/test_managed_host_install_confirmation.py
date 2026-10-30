"""The real managed host must retain helper authentication during continuation."""

from __future__ import annotations

import os
import sys

import pytest
from test_managed_host import Backend, Environment, Probe
from test_managed_host import current_controller as current_controller
from test_managed_host import environment as environment
from test_managed_host import filesystems as filesystems

import muli.managed_host as host_module
from muli.managed_controller import BindingConfirmation
from muli.managed_journal import RoleSnapshots
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed helper/real locks")


@pytest.mark.parametrize("fault", ["pid", "helper_kernel_limits"])
async def test_real_host_reauthenticates_helper_after_install_before_start(
    environment: Environment,
    current_controller: list[str],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    probe = Probe()

    class Effects(Backend):
        instances: list[Backend] = []

        async def install(
            self, record: TransactionRecordV2, role: Role, data: RoleSnapshots
        ) -> BindingConfirmation:
            await super().install(record, role, data)
            assert isinstance(self.confirmation, BindingConfirmation)
            # Only the OS-facing helper probe is synthetic. The real host's
            # current slot/record checks and its supplied callback remain intact.
            if fault == "pid":
                probe.helper.pid = os.getpid() + 1
            else:
                probe.helper.error = fault
            return self.confirmation

    monkeypatch.setattr(host_module, "ManagedLinuxBackend", Effects)
    expected = "foreign_helper_process" if fault == "pid" else "helper_kernel_limits"
    with pytest.raises(ValueError, match="^" + expected + "$"):
        await host_module.run_managed_helper(
            environment.journal.directory.path, probe=probe, clock=environment.clock
        )

    assert len(Effects.instances) == 1
    effects = Effects.instances[0]
    assert effects.calls.count("install:target") == 1
    assert "start:target" not in effects.calls and "start:base" not in effects.calls
    current = environment.journal.read_current()
    assert isinstance(current, TransactionRecordV2)
    assert (current.phase, current.intent, current.code) == (
        "rolling_back",
        "stop_target",
        "backend_failed",
    )
    assert current.deadline == environment.record.deadline
    assert effects.closed and len(probe.helper.reads) > 2 and current_controller
