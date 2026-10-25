"""Fixed offline service/controller roles for the managed process regression.

Only the manager/proc/cgroup observation is synthetic. The service still enters
the retained launcher, real CLI/bootstrap, existing Store, nonce API and Runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import socket
import sys
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, NoReturn, cast, get_args
from unittest.mock import patch

from muli.config import ConfigFile
from muli.format_evidence import read_format_evidence
from muli.managed_controller import ManagedTransactionController
from muli.managed_journal import RoleSnapshots, advance_managed, prepare_managed
from muli.managed_transaction import (
    RoleBinding,
    ServiceIdentityV2,
    TransactionRecordV2,
    TransitionV2,
    raw_snapshot,
)
from muli.management_transaction import Deadline, Intent, Phase, Role, TransactionJournal, _snapshot
from muli.platforms.installation import FileFingerprint
from muli.platforms.linux_transaction import (
    ProbeLimits,
    ProcessIdentity,
    ServiceSnapshot,
    UnitSnapshot,
    clock_reading,
)
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    SelectorStore,
    SelectorValue,
    SlotIdentity,
)
from muli.platforms.managed_probe import (
    ManagedProbeResult,
    ManagedSystemdProbe,
    ManagedUserService,
    managed_process_arguments,
    managed_unit_arguments,
)
from muli.proactive import FrozenRouting
from muli.providers import ModelConfig
from muli.reply_origin import ReplyOrigin
from muli.store import Store

BOOT = {"target": "2" * 32, "base": "3" * 32}
ORIGIN = "1" * 32
IO_SECONDS = 4.0
PREFIX = "/api/internal/management/"
ROLES: tuple[Role, ...] = ("base", "target")
STEP_PHASES: tuple[Phase, ...] = get_args(Phase)
STEP_INTENTS: tuple[Intent, ...] = get_args(Intent)


def read_small(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(4097)
    assert len(data) <= 4096, "fixture observation exceeded its byte budget"
    return data


def checkpoint(value: str) -> None:
    sys.stdout.buffer.write(value.encode("ascii") + b"\n")
    sys.stdout.buffer.flush()


def request_parent(value: str) -> None:
    checkpoint(value)
    assert select.select([sys.stdin.buffer], [], [], IO_SECONDS)[0], "parent reply expired"
    assert sys.stdin.buffer.read(1) == b"G", "invalid parent reply"


def read_record(journal: TransactionJournal) -> TransactionRecordV2:
    record = journal.read_current()
    assert isinstance(record, TransactionRecordV2)
    return record


def deployment(
    journal: TransactionJournal,
) -> tuple[DeploymentAnchor, dict[Role, SlotIdentity]]:
    record = read_record(journal)
    with DeploymentStore(journal.directory.path) as source:
        anchor = source.read_anchor(record.identity.deployment)
        slots: dict[Role, SlotIdentity] = {
            role: source.read_slot(record.binding(role).installation) for role in ROLES
        }
    return anchor, slots


def prepare(root: Path, port: int, base: SlotIdentity, target: SlotIdentity) -> TransactionRecordV2:
    config = ConfigFile(root / "config.json")
    config.update(
        {
            "server": {"host": "127.0.0.1", "port": port},
            "workspace_roots": [str(root)],
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "memory": {"enabled": False, "auto_extract": False},
            "plugins": {"enabled": False},
            "skills": {"enabled": False},
            "extensions": {"watch_enabled": False},
            "attention": {"enabled": False},
            "notifications": {"enabled": False},
            "system_events": {"enabled": False},
            "mcp": {"mcpServers": {}},
        }
    )
    database = root / "data/muli.sqlite3"
    unit_dir = root / "xdg/systemd/user"
    unit_dir.mkdir(parents=True)
    with (
        TransactionJournal(root / "journal", database) as journal,
        journal.writer_lease() as writer,
        closing(Store.open_runtime(database, default_mode="auto", durable=True)) as store,
        journal.locked() as control,
        DeploymentStore(journal.directory.path) as source,
    ):
        evidence = read_format_evidence(store.db)
        for slot in (base, target):
            source.publish_slot(slot, guard=control.check)
        launcher = source.publish_launcher(guard=control.check)
        service = ManagedUserService(
            config.path,
            database.parent,
            unit_dir,
            controller=base,
            launcher=Path(launcher.path),
            journal_root=journal.directory.path,
        )
        unit = service.render().encode()
        service.path.write_bytes(unit)
        service.path.chmod(0o600)
        anchor = DeploymentAnchor(
            config_path=str(config.path),
            data_dir=str(database.parent),
            database_path=str(database),
            unit_path=str(service.path),
            journal_root=str(journal.directory.path),
            controller_slot=base.digest,
            launcher=launcher,
            managed_unit=raw_snapshot(unit, journal.limits.max_snapshot_bytes),
        )
        source.publish_anchor(anchor, guard=control.check)
        data: dict[Role, RoleSnapshots] = {}
        bindings: dict[Role, RoleBinding] = {}
        for role in ROLES:
            slot = base if role == "base" else target
            selector = SelectorValue(deployment=anchor.digest, installation=slot.digest)
            data[role] = RoleSnapshots(config.path.read_bytes(), selector.canonical(), unit)
            bindings[role] = RoleBinding(
                entry="managed",
                installation=slot.digest,
                compatibility=slot.compatibility_digest,
                configuration=_snapshot(
                    data[role].configuration, journal.limits.max_snapshot_bytes
                ),
                selector=_snapshot(selector.canonical(), 16384),
                unit=anchor.managed_unit,
            )
            if role == "base":
                with SelectorStore(journal.directory.path) as selected:
                    selected.replace(None, selector, guard=control.check)
        record = prepare_managed(
            control,
            request_id="managed-process-switch",
            action="switch",
            identity=ServiceIdentityV2(
                deployment=anchor.digest,
                store_instance=evidence.store_instance,
                main_session=evidence.main_session,
                origin_boot=ORIGIN,
            ),
            base=bindings["base"],
            target=bindings["target"],
            base_data=data["base"],
            target_data=data["target"],
            observed_format=evidence.state,
            base_manifest=base.compatibility,
            target_manifest=target.compatibility,
            deadline=Deadline(created=clock_reading(), seconds=180.0, rollback_reserve=60.0),
            writer=writer,
        )
        origin = ReplyOrigin.load(
            {
                "kind": "channel",
                "input_id": "original-input",
                "route": {
                    "name": "telegram",
                    "account_id": "fixture-account",
                    "target": "fixture-owner",
                    "sender_id": "fixture-owner",
                    "kind": "private",
                    "reply_to": "original-message",
                },
            }
        )
        assert origin is not None
        plan = {
            "id": record.request_id,
            "action": record.action,
            "status": "applying",
            "transaction": record.public(),
            "routing": FrozenRouting.bound(origin).dump(),
        }
        store.event("management.prepared", plan, objects=[("management", plan)])
        store.put(
            "message",
            {
                "id": "existing-unknown",
                "session_id": evidence.main_session,
                "role": "tool",
                "content": json.dumps({"status": "unknown"}),
                "tool_call_id": "original-call",
                "created_at": 1.0,
            },
        )
        # Deliberate seam: no fixture pretends to implement the original drain.
        for transition in (
            TransitionV2(phase="draining"),
            TransitionV2(phase="activating", intent="stop_base"),
            TransitionV2(phase="activating", intent="install_target"),
        ):
            record = advance_managed(control, record.position, transition)
    (root / "manager.json").write_text('{"pid":0,"role":"base"}', encoding="utf-8")
    events = root / "cgroup/fixture.slice/muli.service/cgroup.events"
    events.parent.mkdir(parents=True)
    events.write_bytes(b"populated 0\n")
    return record


class Probe(ManagedSystemdProbe):
    """Only systemd/proc evidence is replaced; live argv/exe still come from /proc."""

    def __init__(self, root: Path, *, service_role: Role | None = None) -> None:
        super().__init__(limits=ProbeLimits(seconds=IO_SECONDS))
        self.root, self.service_role = root, service_role

    async def read_managed(
        self,
        journal: TransactionJournal,
        record: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        manager = json.loads(read_small(self.root / "manager.json"))
        role: Role = self.service_role or manager["role"]
        pid = os.getpid() if self.service_role is not None else manager["pid"]
        unit_argv = managed_unit_arguments(anchor, controller)
        digest = raw_snapshot(Path(anchor.unit_path).read_bytes(), 16384).digest
        process: ProcessIdentity | None = None
        matches: tuple[Role, ...] = ()
        if pid:
            proc = Path("/proc") / str(pid)
            argv = tuple(
                os.fsdecode(item) for item in read_small(proc / "cmdline").split(b"\0")[:-1]
            )
            assert argv == managed_process_arguments(anchor, slots[role])
            executable = str((proc / "exe").resolve(strict=True))
            assert executable == slots[role].resolved_python.path
            ticks = int(read_small(proc / "stat").decode().rsplit(")", 1)[1].split()[19])
            process = ProcessIdentity(
                pid, ticks, os.geteuid(), executable, argv, "/fixture.slice/muli.service"
            )
            matches = (role,)
        unit = UnitSnapshot(
            name="muli.service",
            load_state="loaded",
            active_state="active" if pid else "inactive",
            sub_state="running" if pid else "dead",
            fragment=anchor.unit_path,
            source="",
            drop_ins=(),
            needs_reload=False,
            invocation=BOOT[role] if pid else ORIGIN,
            job=(0, "/"),
            service_type="simple",
            pid=pid,
            cgroup="/fixture.slice/muli.service",
            executable=unit_argv[0],
            argv=unit_argv,
            ignore_failure=False,
            kill_mode="control-group",
            send_sigkill=True,
        )
        return ManagedProbeResult(
            ServiceSnapshot(unit, process), matches, frozenset({digest}), digest
        )


class ObservedBackend(ManagedLinuxBackend):
    """Measure the unchanged synchronous guard, without caching its evidence."""

    check_calls: int = 0
    check_seconds: float = 0.0
    controller_calls: int = 0
    controller_seconds: float = 0.0
    selected_calls: int = 0
    selected_seconds: float = 0.0

    def _check(
        self,
        record: TransactionRecordV2,
        role: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
    ) -> None:
        from muli.platforms import managed_backend, managed_deployment

        started = time.monotonic()
        self.check_calls += 1
        controller_verify = managed_deployment.verify_slot_files
        # The backend's existing private import is the selected-role lookup seam.
        selected_verify = cast(Callable[..., None], vars(managed_backend)["verify_slot_files"])

        def controller(
            slot: SlotIdentity, *, verified_python: FileFingerprint | None = None
        ) -> None:
            before = time.monotonic()
            self.controller_calls += 1
            try:
                if verified_python is None:
                    controller_verify(slot)
                else:
                    controller_verify(slot, verified_python=verified_python)
            finally:
                self.controller_seconds += time.monotonic() - before

        def selected(slot: SlotIdentity, *, verified_python: FileFingerprint | None = None) -> None:
            before = time.monotonic()
            self.selected_calls += 1
            try:
                selected_verify(slot, verified_python=verified_python)
            finally:
                self.selected_seconds += time.monotonic() - before

        try:
            # _check is synchronous: restore both lookup seams before it returns.
            # Each wrapper invokes the exact original once, including failures.
            with (
                patch.object(managed_deployment, "verify_slot_files", controller),
                patch.object(managed_backend, "verify_slot_files", selected),
            ):
                if verified_python is None:
                    super()._check(record, role)
                else:
                    super()._check(record, role, verified_python=verified_python)
        finally:
            self.check_seconds += time.monotonic() - started


async def observed_step(
    driver: ManagedTransactionController,
    backend: ObservedBackend,
    index: int,
    previous: TransactionRecordV2,
) -> TransactionRecordV2:
    """At most 32 small fixed records share the original 4 KiB stdout budget."""
    started = time.monotonic()
    calls, seconds = backend.check_calls, backend.check_seconds
    controller_calls, controller_seconds = backend.controller_calls, backend.controller_seconds
    selected_calls, selected_seconds = backend.selected_calls, backend.selected_seconds
    try:
        return await driver.step()
    finally:
        try:
            # Fixed enums and capped scalars only; never serialize record contents.
            values = [
                index,
                min(1_000_000, max(0, round((time.monotonic() - started) * 1000))),
                min(1_000_000, max(0, backend.check_calls - calls)),
                min(1_000_000, max(0, round((backend.check_seconds - seconds) * 1000))),
                min(1_000_000, max(0, backend.controller_calls - controller_calls)),
                min(
                    1_000_000,
                    max(0, round((backend.controller_seconds - controller_seconds) * 1000)),
                ),
                min(1_000_000, max(0, backend.selected_calls - selected_calls)),
                min(1_000_000, max(0, round((backend.selected_seconds - selected_seconds) * 1000))),
                STEP_PHASES.index(previous.phase),
                STEP_INTENTS.index(previous.intent),
            ]
            checkpoint("controller-step=" + json.dumps(values, separators=(",", ":")))
        except Exception:
            pass  # Observation must not replace the step's result or exception.


async def controller(root: Path) -> None:
    import logging

    # This child's merged output is a fixed handshake protocol, not a log sink.
    # Keep optional production diagnostics out of this fixture's control pipe.
    logger = logging.getLogger("muli.management_controller")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    with TransactionJournal(root / "journal", root / "data/muli.sqlite3", create=False) as journal:
        anchor, slots = deployment(journal)
        with journal.locked() as control:

            async def command(
                argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
            ) -> dict[str, Any]:
                assert argv[:2] == ["systemctl", "--user"]
                if "start" in argv:
                    record = read_record(journal)
                    role = "target" if record.intent == "start_target" else "base"
                    await asyncio.to_thread(request_parent, "start-" + role)
                elif "stop" in argv:
                    assert read_record(journal).intent == "stop_target"
                    await asyncio.to_thread(request_parent, "stop-target")
                else:
                    assert argv == ["systemctl", "--user", "daemon-reload"]
                return {"exit_code": 0, "stdout": "", "stderr": ""}

            async with ObservedBackend(
                anchor,
                slots["base"],
                slots,
                journal,
                control=control,
                probe=Probe(root),
                runner=command,
                cgroup_root=root / "cgroup",
            ) as backend:
                driver = ManagedTransactionController(
                    control, backend, clock_reading, operation_timeout=20.0
                )
                for index in range(32):
                    previous = read_record(journal)
                    record = await observed_step(driver, backend, index, previous)
                    assert record.phase != "recovery_required", record.public()
                    assert record.activation != "blocked", record.public()
                    if record.resolved:
                        checkpoint("controller-resolved")
                        return
                    if record.position == previous.position:
                        await asyncio.sleep(0.02)
                raise AssertionError("controller exhausted the fixed step budget")


def write_latest(store: Store, record: TransactionRecordV2) -> None:
    store.put("fixture", {"id": "latest", "after_prepare": record.generation})
    store.enqueue_input(
        {
            "id": "latest-input",
            "session_id": record.identity.main_session,
            "principal": "owner",
            "source": "web",
            "content": "arrived after preparation",
        },
        max_pending=2,
    )
    with store.db:
        store.db.execute(
            "INSERT INTO channel_outbox(id,name,route,payload,state,next_attempt,created_at,updated_at) "
            "VALUES('latest-outbox','telegram',?,?,'unknown',0,0,0)",
            (
                json.dumps(
                    {"account_id": "fixture-account", "target": "fixture-owner", "kind": "private"}
                ),
                json.dumps({"parts": [{"text": "reply prepared after transaction"}]}),
            ),
        )


def forbidden_model(config: ModelConfig) -> NoReturn:
    raise AssertionError("model construction is forbidden in the process fixture")


def install_hooks() -> None:
    """A test-only explicit .pth hook; never installed in a production prefix."""
    import logging
    from unittest.mock import patch

    import uvicorn
    from fastapi import FastAPI, Request
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.responses import Response

    from muli import server
    from muli.managed_boot import ManagedBootPermit
    from muli.platforms import linux_bootstrap

    common_hook = sys.modules.get("sitecustomize")
    assert common_hook is not None and vars(common_hook).get("_muli_fixture_noop") is True
    role_value = os.environ["MULI_RECOVERY_ROLE"]
    assert role_value in {"base", "target"}
    role: Role = "base" if role_value == "base" else "target"
    root = Path(os.environ["MULI_RECOVERY_ROOT"])
    original_boot = linux_bootstrap.open_boot_session
    original_app = server.create_app

    async def boot(*args: Any, **kwargs: Any) -> linux_bootstrap.BootSession:
        kwargs["managed_probe"] = Probe(root, service_role=role)
        try:
            return await original_boot(*args, **kwargs)
        except BaseException as error:
            cause: BaseException | None = error
            for _ in range(3):
                if cause is None:
                    break
                checkpoint(type(cause).__name__ + ":" + str(getattr(cause, "code", "no_code")))
                cause = cause.__context__
            raise

    def application(*args: Any, **kwargs: Any) -> FastAPI:
        app: FastAPI = original_app(*args, **kwargs)
        runtime = app.state.runtime
        permit = runtime.maintenance.permit
        assert isinstance(permit, ManagedBootPermit) and permit.role == role
        assert not runtime.started and not runtime.maintenance.admission.allowed
        assert runtime.store.migration_allowed is False
        assert runtime.store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert runtime.store.instance_id == permit.record.identity.store_instance
        runtime.client_factory = forbidden_model
        if role == "target":
            write_latest(runtime.store, permit.record)
            if os.environ["MULI_RECOVERY_FAILURE"] == "1":
                runtime.maintenance.unsupported("fixture_target_rejected", lambda: True)
        runtime.store.put(
            "fixture",
            {
                "id": "boot-" + role,
                "passive": True,
                "pid": os.getpid(),
                "prefix": sys.prefix,
                "python": sys.executable,
                "sitecustomize_noop": True,
            },
        )
        posts = 0

        @app.middleware("http")
        async def count(request: Request, next_handler: RequestResponseEndpoint) -> Response:
            nonlocal posts
            if request.method == "POST":
                assert request.url.path == PREFIX + "activate"
                posts += 1
            response = await next_handler(request)
            runtime.store.put(
                "fixture",
                {
                    "id": "http-" + role,
                    "posts": posts,
                    "started": runtime.started,
                    "allowed": runtime.maintenance.admission.allowed,
                },
            )
            return response

        return app

    class Server(uvicorn.Server):
        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets)
            assert self.started
            checkpoint("service-ready")

        async def serve(self, sockets: list[socket.socket] | None = None) -> None:
            logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
            self.config.log_config = None
            self.config.log_level = "critical"
            self.config.http = "h11"
            self.config.ws = "none"
            self.config.lifespan = "on"
            self.config.timeout_graceful_shutdown = 1
            with socket.socket(fileno=int(os.environ["MULI_RECOVERY_SOCKET"])) as listener:
                await super().serve(sockets=[listener])

    # The child owns these patches until exit; no patch mutates a product file.
    patch.object(linux_bootstrap, "open_boot_session", boot).start()
    patch.object(server, "create_app", application).start()
    patch.object(uvicorn, "Server", Server).start()


if __name__ == "__main__":
    assert len(sys.argv) == 3 and sys.argv[1] == "controller"
    asyncio.run(controller(Path(sys.argv[2])))
