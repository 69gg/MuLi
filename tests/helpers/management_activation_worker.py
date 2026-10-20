"""Three fixed roles for the loopback activation/process-death regression."""

from __future__ import annotations

import asyncio
import hashlib
import json
import select
import socket
import sys
from pathlib import Path
from typing import NoReturn

import httpx
import uvicorn
from fastapi import FastAPI, Request
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from muli.config import ConfigFile
from muli.maintenance import BootPermit, MaintenanceError
from muli.maintenance_api import install_maintenance_routes
from muli.management_controller import ServiceObservation, TransactionController
from muli.management_projection import project_final, read_projection
from muli.management_transaction import (
    HealthProof,
    Role,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
)
from muli.platforms.linux_transaction import clock_reading
from muli.providers import ModelConfig
from muli.runtime import Runtime

GATE_SECONDS = 10.0
HTTP_SECONDS = 2.0
HTTP_BYTES = 4096
HOST_BOOT = "fixture-target"
PREFIX = "/api/internal/management"


def checkpoint(message: bytes) -> None:
    sys.stdout.buffer.write(message + b"\n")
    sys.stdout.buffer.flush()


def projection_command() -> None:
    if not select.select([sys.stdin.buffer], [], [], GATE_SECONDS)[0]:
        raise TimeoutError("projection checkpoint expired")
    assert sys.stdin.buffer.read(1) == b"P"


def forbidden_model(config: ModelConfig) -> NoReturn:
    raise AssertionError("model construction is forbidden in the recovery fixture")


class FixtureServer(uvicorn.Server):
    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        assert self.started
        checkpoint(b"host-ready")


async def host(root: Path, descriptor: int) -> None:
    with (
        socket.socket(fileno=descriptor) as listener,
        TransactionJournal(
            root / "journal", root / "data" / "muli.sqlite3", create=False
        ) as journal,
    ):
        permit = BootPermit.acquire(
            journal, root / "config.json", boot=HOST_BOOT, installation="a" * 64, service="b" * 64
        )
        runtime = Runtime(
            ConfigFile(root / "config.json"), root / "data", forbidden_model, boot_permit=permit
        )
        server: uvicorn.Server | None = None
        serving: asyncio.Task[None] | None = None
        try:
            activations, posts = 0, 0

            async def activate() -> None:
                nonlocal activations
                activations += 1

            runtime.maintenance.bind_activation(activate)
            runtime.maintenance.seal({"runtime"})
            assert not runtime.maintenance.admission.allowed
            app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

            @app.middleware("http")
            async def count_posts(
                request: Request, next_handler: RequestResponseEndpoint
            ) -> Response:
                nonlocal posts
                if request.method == "POST":
                    assert request.url.path == PREFIX + "/activate"
                    posts += 1
                return await next_handler(request)

            @app.exception_handler(MaintenanceError)
            async def denied(request: Request, error: MaintenanceError) -> JSONResponse:
                return JSONResponse({"error": error.code}, status_code=403)

            install_maintenance_routes(app, runtime, clock_reading)
            server = FixtureServer(
                uvicorn.Config(
                    app,
                    lifespan="off",
                    log_config=None,
                    log_level="critical",
                    access_log=False,
                    ws="none",
                    http="h11",
                    timeout_keep_alive=1,
                    timeout_graceful_shutdown=1,
                )
            )
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            await asyncio.to_thread(projection_command)
            assert activations == posts == 1
            assert runtime.maintenance.admission.allowed and not runtime.started
            record = journal.read()
            assert record is not None and record.resolved
            receipt = project_final(permit, runtime.store)
            assert receipt is not None and read_projection(runtime.store) == receipt
            position = runtime.store.event_position()
            assert project_final(permit, runtime.store) == receipt
            assert runtime.store.event_position() == position
            checkpoint(b"projected-once")
        finally:
            try:
                if server is not None:
                    server.should_exit = True
                if serving is not None:
                    try:
                        await asyncio.wait_for(serving, timeout=HTTP_SECONDS)
                    finally:
                        if not serving.done():
                            serving.cancel()
                            await asyncio.gather(serving, return_exceptions=True)
            finally:
                await runtime.close()


class LoopbackHost:
    """Observe a real host/lease; no in-memory state is shared between drivers."""

    def __init__(
        self, journal: TransactionJournal, root: Path, client: httpx.AsyncClient, *, first: bool
    ) -> None:
        self.journal, self.root, self.client, self.first = journal, root, client, first
        self.observations = 0

    async def proof(
        self, method: str, record: TransactionRecord, *, wrong_nonce: bool = False
    ) -> HealthProof | None:
        parameters = {"generation": record.generation, "boot": HOST_BOOT}
        async with asyncio.timeout(HTTP_SECONDS):
            async with self.client.stream(
                method,
                PREFIX + ("/readiness" if method == "GET" else "/activate"),
                params=parameters if method == "GET" else None,
                json=parameters if method == "POST" else None,
                headers={
                    "X-MuLi-Nonce": "incorrect-challenge" if wrong_nonce else record.challenge
                },
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=512):
                    body.extend(chunk)
                    assert len(body) <= HTTP_BYTES, "host response exceeded the fixture byte budget"
                if wrong_nonce:
                    assert response.status_code == 403
                    assert json.loads(body) == {"error": "transaction_unauthorized"}
                    return None
                assert response.status_code == 200
                return HealthProof.model_validate_json(body)

    async def observe(self, record: TransactionRecord) -> ServiceObservation:
        self.observations += 1
        try:
            with self.journal.writer_lease():
                raise AssertionError("the active host must own the real writer lease")
        except TransactionError as exc:
            assert exc.code == "lock_busy"
        health = await self.proof("GET", record)
        assert health is not None
        return ServiceObservation(
            identity_matches=(health.installation, health.service)
            == (record.identity.installation, record.identity.service),
            configuration=hashlib.sha256((self.root / "config.json").read_bytes()).hexdigest(),
            service="running",
            boot=health.boot,
            writer_free=False,
            health=health,
        )

    def forbidden(self) -> NoReturn:
        raise AssertionError("service effects and repeated activation are forbidden")

    async def drain(self, record: TransactionRecord) -> None:
        self.forbidden()

    async def resume(self, record: TransactionRecord) -> None:
        self.forbidden()

    async def stop(self, record: TransactionRecord, role: Role) -> None:
        self.forbidden()

    async def install(self, record: TransactionRecord, role: Role, data: bytes) -> None:
        self.forbidden()

    async def start(self, record: TransactionRecord, role: Role) -> None:
        self.forbidden()

    async def activate(self, record: TransactionRecord, role: Role) -> None:
        if not self.first:
            self.forbidden()
        assert role == "target"
        proof = await self.proof("POST", record)
        assert proof is not None and record.accepts(proof, role, released=True)
        checkpoint(b"activated-before-release-record")
        # The parent kills C1 inside the actual Controller call, before it can
        # return to step or write a release receipt. This wait remains bounded.
        async with asyncio.timeout(GATE_SECONDS):
            await asyncio.Event().wait()


async def controller(root: Path, port: int, *, first: bool) -> None:
    with TransactionJournal(
        root / "journal", root / "data" / "muli.sqlite3", create=False
    ) as journal:
        with journal.locked() as control:
            before = control.read()
            assert before is not None
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                timeout=HTTP_SECONDS,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            ) as client:
                backend = LoopbackHost(journal, root, client, first=first)
                driver = TransactionController(
                    control, backend, clock_reading, operation_timeout=GATE_SECONDS
                )
                if first:
                    assert before.phase == "verifying"
                    assert await backend.proof("GET", before, wrong_nonce=True) is None
                    decision = await driver.step()
                    assert decision.phase == "committed" and decision.activation == "pending"
                    assert decision.deadline == before.deadline
                    await driver.step()
                    raise AssertionError("C1 must die at the activation receipt checkpoint")
                assert before.phase == "committed" and before.activation == "pending"
                after = await driver.step()
                assert after.resolved and after.activation == "released"
                assert after.deadline == before.deadline and after.challenge == before.challenge
                assert after.revision == before.revision + 1
                assert after.health is not None and after.health.admission == "released"
                assert await driver.step() == after
                assert backend.observations == 1
    checkpoint(b"fresh-controller-released")


def main() -> None:
    assert sys.platform == "linux" and len(sys.argv) == 4
    role, root, endpoint = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
    if role == "host":
        asyncio.run(host(root, endpoint))
    elif role in {"first", "fresh"}:
        asyncio.run(controller(root, endpoint, first=role == "first"))
    else:
        raise AssertionError("unknown activation recovery worker role")


if __name__ == "__main__":
    main()
