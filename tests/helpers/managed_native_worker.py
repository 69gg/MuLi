"""Hosted-CI-only worker. No manager, protocol, probe or backend is replaced."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
import sysconfig
import time
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from muli.managed_transaction import TransactionRecordV2
    from muli.management_transaction import Role, TransactionJournal
    from muli.platforms.linux_transaction import SystemdProbe
    from muli.platforms.managed_deployment import DeploymentAnchor, SlotIdentity
    from muli.store import Store

MAX_BYTES = 32768
CASE_SECONDS = 60
CLEANUP_SECONDS = 5
MEMORY = 1024**3
TASKS = 128
HARNESS_MEMORY = MEMORY // 4
HARNESS_TASKS = 32
HTTP_SECONDS = 2.0
POLL_SECONDS = 0.1


class NativeError(ValueError):
    """A fixed code, never an upstream response or a configuration value."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise NativeError(code)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as source:
        data = source.read(MAX_BYTES + 1)
    require(len(data) <= MAX_BYTES, "native_metadata_budget")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise NativeError("native_metadata_invalid")
    return result


def write_json(path: Path, value: dict[str, Any]) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    require(len(data) <= MAX_BYTES, "native_metadata_budget")
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    require(value > 0, "native_deadline_exceeded")
    return value


def kernel_limits(group: Path, *, memory: int, tasks: int) -> dict[str, str]:
    result = {}
    for name, maximum in (("memory.max", memory), ("memory.swap.max", 0), ("pids.max", tasks)):
        value = (group / name).read_text().strip()
        require(value.isdecimal() and int(value) <= maximum, "native_kernel_limit")
        require(not maximum or int(value) > 0, "native_kernel_limit")
        result[name] = value
    values = (group / "cpu.max").read_text().split()
    require(len(values) == 2 and all(item.isdecimal() for item in values), "native_kernel_cpu")
    quota, period = map(int, values)
    require(0 < quota <= period, "native_kernel_cpu")
    result["cpu.max"] = " ".join(values)
    controllers = (group / "cgroup.controllers").read_text().split()
    require({"cpu", "memory", "pids"}.issubset(controllers), "native_kernel_controllers")
    return result


def membership(unit: str, uid: int) -> Path:
    value = Path("/proc/self/cgroup").read_text().strip()
    require(value.startswith("0::/") and "\n" not in value, "native_cgroup_version")
    group = Path(value.removeprefix("0::"))
    require(group.name == unit and f"user@{uid}.service" in group.parts, "native_membership")
    return Path("/sys/fs/cgroup") / str(group).lstrip("/")


async def service_exists(probe: SystemdProbe) -> bool:
    """Exact standard-unit listing; helper_exists deliberately rejects this name."""
    from muli.platforms.linux_transaction import _typed

    async with asyncio.timeout(probe.limits.seconds):
        reply = _typed(
            await probe._command(
                "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "ListUnitsByPatterns",
                "asas",
                "0",
                "1",
                "muli.service",
            ),
            "a(ssssssouso)",
        )
        if reply == [[]]:
            return False
        require(
            isinstance(reply, list)
            and len(reply) == 1
            and isinstance(reply[0], list)
            and len(reply[0]) == 1
            and isinstance(reply[0][0], list)
            and len(reply[0][0]) == 10
            and reply[0][0][0] == "muli.service",
            "native_service_listing",
        )
        return True


def fixture(root: Path) -> dict[str, Any]:
    require(root.is_absolute() and root.resolve(strict=True) == root, "native_root_invalid")
    value = read_json(root / "fixture.json")
    require(value.get("uid") == os.geteuid() and os.geteuid() != 0, "native_uid_invalid")
    expected = {
        "memory": MEMORY,
        "tasks": TASKS,
        "harness_memory": HARNESS_MEMORY,
        "harness_tasks": HARNESS_TASKS,
        "seconds": CASE_SECONDS,
        "cleanup_seconds": CLEANUP_SECONDS,
        "harness_seconds": 90,
        "http_bytes": MAX_BYTES,
    }
    require(
        all(type(value.get(key)) is int and value[key] == item for key, item in expected.items()),
        "native_fixture_budget",
    )
    require(
        isinstance(value.get("harness_unit"), str)
        and re.fullmatch(r"muli-native-[a-f0-9]{32}\.service", value["harness_unit"]) is not None,
        "native_fixture_unit",
    )
    return value


def describe() -> dict[str, Any]:
    """Read only the installed runtime; this mode never launches any command."""
    import muli
    from muli.management_transaction import JournalLimits
    from muli.platforms.installation import PackageLimits
    from muli.platforms.managed_launcher import package_fingerprint

    limits = PackageLimits()
    package = Path(muli.__file__).resolve(strict=True).parent
    observed = package_fingerprint(package, limits.model_dump())
    runtime = Path(sys.base_prefix).resolve(strict=True)
    executable = Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True)
    require(executable.is_relative_to(runtime), "native_runtime_layout")
    return {
        "runtime": str(runtime),
        "executable": str(executable),
        "dependencies": str(Path(sysconfig.get_paths()["purelib"]).resolve(strict=True)),
        "package": str(package),
        "size": observed["size"],
        "digest": observed["digest"],
        "limits": limits.model_dump(mode="json"),
        "http_bytes": JournalLimits().max_record_bytes,
    }


def forbidden_model(*args: Any, **kwargs: Any) -> NoReturn:
    raise NativeError("native_model_forbidden")


def write_latest(store: Store, record: TransactionRecordV2) -> None:
    """Only called after the real target has acquired its writer and Store."""
    store.put("fixture", {"id": "latest", "after_prepare": record.generation})
    store.enqueue_input(
        {
            "id": "latest-input",
            "session_id": record.identity.main_session,
            "principal": "owner",
            "source": "web",
            "content": "after prepare",
        },
        max_pending=2,
    )
    with store.db:
        store.db.execute(
            "INSERT INTO channel_outbox(id,name,route,payload,state,next_attempt,created_at,updated_at) "
            "VALUES('latest-outbox','telegram',?,?,'unknown',0,0,0)",
            (
                json.dumps({"target": "fixture-owner", "kind": "private"}),
                json.dumps({"parts": [{"text": "after prepare"}]}),
            ),
        )


def install_hook() -> None:
    """Wrap only the app factory; the real bootstrap and health path stay intact."""
    from fastapi import FastAPI

    from muli import server
    from muli.managed_boot import ManagedBootPermit
    from muli.managed_transaction import TransactionRecordV2

    original = server.create_app

    def application(*args: Any, **kwargs: Any) -> FastAPI:
        app: FastAPI = original(*args, **kwargs)
        runtime = app.state.runtime
        runtime.client_factory = forbidden_model
        permit = runtime.maintenance.permit
        if (
            not isinstance(permit, ManagedBootPermit)
            or permit.role != "target"
            or permit.record.action != "switch"
        ):
            return app
        root = Path(permit.config_path).parent
        if Path(sys.prefix) != root / "a":
            return app
        from muli.platforms.managed_deployment import DeploymentStore

        with DeploymentStore(permit.journal.directory.path) as source:
            base = source.read_slot(permit.record.base.installation)
        require(Path(base.prefix) == root / "b", "native_failure_role")
        permit.lease.check()
        permit.check_paths(runtime.config.path, runtime.data_dir / "muli.sqlite3")
        current = permit.journal.read_current()
        if not isinstance(current, TransactionRecordV2) or current != permit.record:
            raise NativeError("native_failure_generation")
        require(
            not runtime.started and not runtime.maintenance.admission.allowed,
            "native_failure_not_passive",
        )
        require(not runtime.store.migration_allowed, "native_failure_migration")
        runtime.store.revalidate_format()
        require(
            runtime.store.instance_id == current.identity.store_instance, "native_failure_store"
        )
        require(
            runtime.store.main_session()["id"] == current.identity.main_session,
            "native_failure_main",
        )
        require(
            runtime.store.db.execute("PRAGMA synchronous").fetchone()[0] == 2,
            "native_failure_durability",
        )
        write_latest(runtime.store, current)
        runtime.maintenance.unsupported("fixture_target_rejected", lambda: True)
        return app

    server.create_app = application


class Case:
    def __init__(self, root: Path, *, seconds: float = CASE_SECONDS) -> None:
        require(0 < seconds <= CASE_SECONDS, "native_case_budget")
        self.root, self.start = root, time.monotonic()
        self.deadline = self.start + seconds
        self.stage = "import"
        self.recording = True
        self._notes: dict[str, dict[str, Any]] = {}
        self.result: dict[str, Any] = {
            "completed": False,
            "stage": self.stage,
            "code": "",
            "timeline": [],
            "generations": [],
            "resources": {},
        }

    def note(self, stage: str, **facts: Any) -> None:
        require(re.fullmatch(r"[a-z_]{1,40}", stage) is not None, "native_stage_invalid")
        allowed = {
            "phase",
            "activation",
            "request_id",
            "generation",
            "helper",
            "state",
            "pid",
            "boot",
            "result",
            "exit_status",
        }
        require(set(facts).issubset(allowed), "native_fact_invalid")
        self.stage = stage
        if not self.recording:
            return
        self.result["stage"] = stage
        row = {"elapsed_ms": int((time.monotonic() - self.start) * 1000), "stage": stage, **facts}
        timeline = self.result["timeline"]
        if self._notes.get(stage) != facts:
            require(len(timeline) < 64, "native_timeline_budget")
            timeline.append(row)
            self._notes[stage] = facts
            write_json(self.root / "result.json", self.result)

    async def command(self, *argv: str) -> str:
        from muli.process import run_process

        result = await run_process(
            list(argv),
            timeout=min(5.0, remaining(self.deadline)),
            limit=MAX_BYTES,
            merge_stderr=False,
        )
        require(result.get("exit_code") == 0, "native_command_failed")
        output = result.get("output")
        if not isinstance(output, str):
            raise NativeError("native_command_output")
        remaining(self.deadline)
        return output

    async def pause(self) -> None:
        await asyncio.sleep(min(POLL_SECONDS, remaining(self.deadline)))

    async def http(
        self, client: Any, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        import httpx

        size = min(HTTP_SECONDS, remaining(self.deadline))
        try:
            async with asyncio.timeout(size):
                async with client.stream(method, path, json=body) as response:
                    require(
                        response.headers.get("content-encoding", "identity") == "identity",
                        "native_http_encoding",
                    )
                    data = bytearray()
                    async for part in response.aiter_raw():
                        data.extend(part)
                        require(len(data) <= MAX_BYTES, "native_http_budget")
                    result = json.loads(data)
                    require(isinstance(result, dict), "native_http_object")
                    return response.status_code, result
        except httpx.RequestError:
            raise NativeError("native_http_unavailable") from None

    def journal(self) -> TransactionJournal:
        from muli.management_transaction import TransactionJournal

        return TransactionJournal(
            self.root / "data/management/transaction-v1",
            self.root / "data/muli.sqlite3",
            create=False,
        )

    def record(self, journal: TransactionJournal) -> TransactionRecordV2:
        from muli.managed_transaction import TransactionRecordV2

        record = journal.read_current()
        if not isinstance(record, TransactionRecordV2):
            raise NativeError("native_managed_record_required")
        return record

    def deployment(
        self, journal: TransactionJournal, record: TransactionRecordV2
    ) -> tuple[DeploymentAnchor, SlotIdentity, dict[Role, SlotIdentity]]:
        from muli.platforms.managed_deployment import DeploymentStore

        with DeploymentStore(journal.directory.path) as source:
            anchor = source.read_anchor(record.identity.deployment)
            controller = source.read_slot(anchor.controller_slot)
            roles: tuple[Role, ...] = ("base", "target")
            slots = {role: source.read_slot(record.binding(role).installation) for role in roles}
        require(Path(controller.prefix) == self.root / "a", "native_controller_changed")
        return anchor, controller, slots

    async def helper_empty(self, probe: SystemdProbe, name: str, argv: tuple[str, ...]) -> bool:
        from muli.platforms.linux_controller import CgroupObservation

        if not await probe.helper_exists(name):
            self.note("helper_exit", helper=name, state="collected")
            return True
        snapshot = await probe._snapshot(await probe._unit_path(name))
        require(snapshot.name == name and snapshot.argv == argv, "native_helper_identity")
        require(
            f"user@{os.geteuid()}.service" in Path(snapshot.cgroup).parts or not snapshot.cgroup,
            "native_helper_membership",
        )
        details = await probe._properties(
            await probe._unit_path(name), "Service", (("Result", "s"), ("ExecMainStatus", "i"))
        )
        self.note(
            "helper_exit",
            helper=name,
            state=snapshot.active_state,
            pid=snapshot.pid,
            result=details["Result"],
            exit_status=details["ExecMainStatus"],
        )
        return (
            snapshot.active_state == "inactive"
            and snapshot.pid == 0
            and snapshot.job == (0, "/")
            and CgroupObservation(Path("/sys/fs/cgroup"), MAX_BYTES).empty(snapshot.cgroup)
        )

    def read_database(self, record: TransactionRecordV2, *, expect_latest: bool) -> dict[str, Any]:
        from muli.format_evidence import read_format_evidence
        from muli.management_projection import read_projection_connection

        database = self.root / "data/muli.sqlite3"
        initial = self.result["resources"].get("initial")
        require(isinstance(initial, dict), "native_initial_identity_missing")
        identity = database.stat()
        require(
            (identity.st_dev, identity.st_ino) == (initial["device"], initial["inode"]),
            "native_database_replaced",
        )
        require(
            (record.identity.store_instance, record.identity.main_session)
            == (initial["store"], initial["main"]),
            "native_initial_identity_changed",
        )
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA query_only=ON")
            receipt = read_projection_connection(db)
            require(
                receipt is not None
                and receipt.generation == record.generation
                and receipt.revision == record.revision,
                "native_exact_projection",
            )
            evidence = read_format_evidence(db)
            require(
                (evidence.store_instance, evidence.main_session)
                == (record.identity.store_instance, record.identity.main_session),
                "native_store_changed",
            )
            row = db.execute(
                "SELECT data FROM objects WHERE kind='management' AND id=?", (record.request_id,)
            ).fetchone()
            require(row is not None, "native_plan_missing")
            plan: dict[str, Any] = json.loads(row[0])
            require(plan["transaction"] == record.public(), "native_plan_projection")
            count = db.execute(
                "SELECT count(*) FROM events WHERE type='management.completed' AND json_extract(data,'$.id')=?",
                (record.request_id,),
            ).fetchone()[0]
            require(count == 1, "native_completion_count")
            if expect_latest:
                latest = db.execute(
                    "SELECT data FROM objects WHERE kind='fixture' AND id='latest'"
                ).fetchone()
                require(
                    latest is not None
                    and json.loads(latest[0])["after_prepare"] == record.generation,
                    "native_latest_missing",
                )
                require(
                    db.execute(
                        "SELECT state FROM channel_outbox WHERE id='latest-outbox'"
                    ).fetchone()
                    == ("unknown",),
                    "native_unknown_changed",
                )
                require(
                    db.execute("SELECT state FROM ingress WHERE id='latest-input'").fetchone()
                    == ("pending",),
                    "native_input_changed",
                )
            return plan

    def freeze_initial(self) -> None:
        from muli.format_evidence import read_format_evidence

        database = self.root / "data/muli.sqlite3"
        before = database.stat()
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.execute("PRAGMA query_only=ON")
            evidence = read_format_evidence(db)
        after = database.stat()
        require(
            (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino),
            "native_database_replaced",
        )
        self.result["resources"]["initial"] = {
            "store": evidence.store_instance,
            "main": evidence.main_session,
            "device": before.st_dev,
            "inode": before.st_ino,
        }
        self.note("identity_frozen")

    async def transaction(
        self, client: Any, password: str, action: str, slot: SlotIdentity, previous_boot: str
    ) -> str:
        from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
        from muli.platforms.linux_transaction import ProbeLimits
        from muli.platforms.managed_probe import ManagedSystemdProbe

        self.note("prepare", state=action)
        status, plan = await self.http(
            client,
            "POST",
            "/api/management/transactions",
            {
                "action": action,
                "candidate": {
                    "prefix": slot.prefix,
                    "python": slot.lexical_python,
                    "package_root": slot.package.root,
                },
            },
        )
        require(status == 200 and isinstance(plan.get("id"), str), "native_prepare_failed")
        request_id, routing = plan["id"], plan.get("routing")
        probe = ManagedSystemdProbe(
            limits=ProbeLimits(seconds=HTTP_SECONDS, command_bytes=MAX_BYTES)
        )
        journal: TransactionJournal | None = None
        position: tuple[str, int] | None = None
        try:
            while True:
                remaining(self.deadline)
                try:
                    status, current = await self.http(
                        client, "GET", "/api/management/transactions/current"
                    )
                    if status == 401:
                        status, _ = await self.http(
                            client, "POST", "/api/auth/login", {"password": password}
                        )
                        require(status == 200, "native_login_failed")
                        await self.pause()
                        continue
                except NativeError as error:
                    if str(error) != "native_http_unavailable":
                        raise
                    status, current = 0, {}
                if (
                    journal is None
                    and (self.root / "data/management/transaction-v1/state.json").exists()
                ):
                    journal = self.journal()
                if journal is not None:
                    record = self.record(journal)
                    if record.request_id == request_id:
                        name = managed_helper_unit_name(record)
                        history = self.result["generations"]
                        if not any(item["generation"] == record.generation for item in history):
                            require(len(history) < 3, "native_generation_budget")
                            history.append(
                                {
                                    "deployment": record.identity.deployment,
                                    "generation": record.generation,
                                    "helper": name,
                                }
                            )
                        fresh_position = (record.generation, record.revision)
                        if fresh_position != position:
                            self.note(
                                "transaction",
                                request_id=request_id,
                                generation=record.generation,
                                phase=record.phase,
                                activation=record.activation,
                            )
                            anchor, controller, _ = self.deployment(journal, record)
                            await self.helper_empty(
                                probe, name, managed_helper_arguments(anchor, controller)
                            )
                            position = fresh_position
                        require(
                            record.phase != "recovery_required" and record.activation != "blocked",
                            "native_transaction_blocked",
                        )
                        if (
                            record.resolved
                            and status == 200
                            and current.get("transaction") == record.public()
                        ):
                            anchor, controller, slots = self.deployment(journal, record)
                            if not await self.helper_empty(
                                probe, name, managed_helper_arguments(anchor, controller)
                            ):
                                await self.pause()
                                continue
                            # A resolved HTTP response can precede helper lock release.
                            with journal.locked():
                                require(self.record(journal) == record, "native_generation_changed")
                            expect_latest = (
                                action == "switch" and Path(slot.prefix) == self.root / "a"
                            )
                            require(
                                (record.phase, record.activation)
                                == ("rolled_back" if expect_latest else "committed", "released"),
                                "native_wrong_decision",
                            )
                            expected_role = "base" if expect_latest else "target"
                            observed = await probe.read_managed(
                                journal, record, anchor, controller, slots
                            )
                            require(
                                observed.service.process is not None
                                and expected_role in observed.process_roles,
                                "native_running_role",
                            )
                            unit = observed.service.unit
                            require(
                                unit.invocation != previous_boot
                                and unit.pid > 0
                                and f"user@{os.geteuid()}.service" in Path(unit.cgroup).parts,
                                "native_service_membership",
                            )
                            final = self.read_database(record, expect_latest=expect_latest)
                            require(final.get("routing") == routing, "native_routing_changed")
                            self.note(
                                "projection",
                                request_id=request_id,
                                generation=record.generation,
                                phase=record.phase,
                                activation=record.activation,
                                boot=unit.invocation,
                                pid=unit.pid,
                            )
                            return unit.invocation
                await self.pause()
        finally:
            if journal is not None:
                journal.__exit__()

    async def drive(self) -> None:
        import fastapi
        import httpx
        import pydantic
        import uvicorn

        import muli
        from muli.config import ConfigFile
        from muli.platforms.filesystem_identity import verify_local_filesystems
        from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe
        from muli.platforms.managed_deployment import register_local_slot
        from muli.service import UserService

        _ = fastapi, pydantic, uvicorn, sqlite3
        settings = fixture(self.root)
        require(
            Path(sys.prefix) == self.root / "a"
            and Path(sys.executable) == self.root / "a/bin/python",
            "native_prefix_invalid",
        )
        require(
            Path(muli.__file__).parent
            == self.root
            / "a/lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages/muli",
            "native_import_invalid",
        )
        group = membership(settings["harness_unit"], settings["uid"])
        self.result["resources"]["harness"] = kernel_limits(
            group, memory=HARNESS_MEMORY, tasks=HARNESS_TASKS
        )
        require((group / "memory.oom.group").read_text().strip() == "1", "native_harness_oom")
        manager = next(
            parent for parent in group.parents if parent.name == f"user@{os.geteuid()}.service"
        )
        self.result["resources"]["manager"] = kernel_limits(manager, memory=MEMORY, tasks=TASKS)
        self.note("imports_verified")
        verify_local_filesystems([self.root])
        slots = {}
        for name in ("a", "b"):
            prefix = self.root / name
            package = (
                prefix
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages/muli"
            )
            slot = register_local_slot(prefix, prefix / "bin/python", package)
            require(slot.package.digest == settings["package_digest"], "native_package_changed")
            slots[name] = slot
        self.note("slots_verified")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        password = os.urandom(24).hex()
        config = ConfigFile(self.root / "config.json")
        config.update(
            {
                "server": {"host": "127.0.0.1", "port": port, "password": password},
                "workspace_roots": [str(self.root)],
                "onboarding_pending": False,
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
        service = UserService(config.path, self.root / "data", executable=slots["a"].lexical_python)
        require(
            service.path == self.root / "home/.config/systemd/user/muli.service", "native_unit_path"
        )
        service.path.parent.mkdir(parents=True, mode=0o700)
        service.path.write_text(service.render(), encoding="utf-8")
        service.path.chmod(0o600)
        self.note("manager_start")
        await self.command("systemctl", "--user", "daemon-reload")
        await self.command("systemctl", "--user", "start", service.unit)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Origin": f"http://127.0.0.1:{port}", "Accept-Encoding": "identity"},
            trust_env=False,
            follow_redirects=False,
            timeout=HTTP_SECONDS,
        ) as client:
            while True:
                try:
                    status, _ = await self.http(
                        client, "POST", "/api/auth/login", {"password": password}
                    )
                    require(status == 200, "native_login_failed")
                    break
                except NativeError as error:
                    if str(error) != "native_http_unavailable":
                        raise
                    await self.pause()
            probe = SystemdProbe(limits=ProbeLimits(seconds=HTTP_SECONDS, command_bytes=MAX_BYTES))
            initial = await probe.read(service)
            require(
                initial.process is not None and initial.process.uid == os.geteuid(),
                "native_origin_process",
            )
            require(
                f"user@{os.geteuid()}.service" in Path(initial.unit.cgroup).parts,
                "native_origin_membership",
            )
            self.note("origin_ready", boot=initial.unit.invocation, pid=initial.unit.pid)
            self.freeze_initial()
            boot = await self.transaction(
                client, password, "adopt", slots["a"], initial.unit.invocation
            )
            boot = await self.transaction(client, password, "switch", slots["b"], boot)
            await self.transaction(client, password, "switch", slots["a"], boot)
        self.result["resources"]["manager_peak"] = (manager / "memory.peak").read_text().strip()
        self.result["completed"] = True
        self.note("complete")

    async def cleanup(self) -> None:
        from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
        from muli.platforms.linux_controller import CgroupObservation
        from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe

        settings = fixture(self.root)
        membership("muli-native-cleanup.service", settings["uid"])
        saved = (
            read_json(self.root / "result.json")
            if (self.root / "result.json").exists()
            else self.result
        )
        self.result = saved
        self.recording = False
        history = saved.get("generations", [])
        require(isinstance(history, list) and len(history) <= 3, "native_cleanup_history")
        probe = SystemdProbe(
            limits=ProbeLimits(
                seconds=min(HTTP_SECONDS, remaining(self.deadline)), command_bytes=MAX_BYTES
            )
        )
        state = self.root / "data/management/transaction-v1/state.json"
        if state.exists():
            with self.journal() as journal:
                record = self.record(journal)
                if not any(item["generation"] == record.generation for item in history):
                    require(len(history) < 3, "native_cleanup_history")
                    history.append(
                        {
                            "deployment": record.identity.deployment,
                            "generation": record.generation,
                            "helper": managed_helper_unit_name(record),
                        }
                    )
                anchor, controller, _ = self.deployment(journal, record)
                expected_argv = managed_helper_arguments(anchor, controller)
                for item in history:
                    identity = json.dumps(
                        [2, item["deployment"], item["generation"]], separators=(",", ":")
                    ).encode()
                    name = "muli-transaction-" + hashlib.sha256(identity).hexdigest() + ".service"
                    require(
                        item["helper"] == name and item["deployment"] == anchor.digest,
                        "native_cleanup_helper",
                    )
                    if await probe.helper_exists(name):
                        unit = await probe._snapshot(await probe._unit_path(name))
                        require(unit.argv == expected_argv, "native_cleanup_helper")
                        await self.command("systemctl", "--user", "stop", name)
                        while not await self.helper_empty(probe, name, expected_argv):
                            await self.pause()
        if await service_exists(probe):
            unit = await probe._snapshot(await probe._unit_path("muli.service"))
            require(
                Path(unit.fragment) == self.root / "home/.config/systemd/user/muli.service",
                "native_cleanup_fragment",
            )
            await self.command("systemctl", "--user", "stop", "muli.service")
            unit = await probe._snapshot(await probe._unit_path("muli.service"))
            require(
                unit.pid == 0
                and unit.job == (0, "/")
                and CgroupObservation(Path("/sys/fs/cgroup"), MAX_BYTES).empty(unit.cgroup),
                "native_service_not_empty",
            )
        if state.exists():
            with self.journal() as journal, journal.writer_lease():
                record = self.record(journal)
                if saved.get("completed") is True:
                    self.read_database(record, expect_latest=True)
        remaining(self.deadline)


async def run(mode: str, root: Path) -> int:
    case = Case(root, seconds=CLEANUP_SECONDS if mode == "cleanup" else CASE_SECONDS)
    try:
        if mode == "cleanup":
            deadline = float(os.environ.get("MULI_NATIVE_CLEANUP_DEADLINE", "nan"))
            require(case.start < deadline <= case.deadline, "native_cleanup_deadline")
            case.deadline = deadline
            async with asyncio.timeout(remaining(case.deadline)):
                await case.cleanup()
        else:
            case.note("import")
            # The absolute budget also includes every synchronous import and
            # verification boundary before the first manager effect.
            async with asyncio.timeout(remaining(case.deadline)):
                await case.drive()
        return 0
    except (Exception, asyncio.CancelledError) as error:
        code = type(error).__name__
        if isinstance(error, NativeError) and re.fullmatch(r"native_[a-z_]{1,57}", str(error)):
            code = str(error)
        if mode != "cleanup":
            case.result["code"] = code
            case.note(case.stage)
            write_json(root / "result.json", case.result)
        print("native_" + mode + "_failed:" + case.stage + ":" + code, file=sys.stderr)
        return 1


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "describe":
        print(json.dumps(describe(), sort_keys=True, separators=(",", ":")))
        return 0
    require(len(sys.argv) == 3 and sys.argv[1] in {"drive", "cleanup"}, "native_arguments")
    return asyncio.run(run(sys.argv[1], Path(sys.argv[2])))


if __name__ == "__main__":
    raise SystemExit(main())
