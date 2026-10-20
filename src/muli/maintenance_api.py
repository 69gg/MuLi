"""Nonce-only local transaction handshake; no caller supplied paths or commands."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Header, Query

from muli.maintenance import MaintenanceError
from muli.management_transaction import ClockReading, Identifier, StrictRecord

if TYPE_CHECKING:
    from muli.runtime import Runtime

_PREFIX = "/api/internal/management"
INTERNAL_METHODS = frozenset(
    {
        ("POST", _PREFIX + "/drain"),
        ("POST", _PREFIX + "/resume"),
        ("GET", _PREFIX + "/readiness"),
        ("POST", _PREFIX + "/activate"),
    }
)


class Handshake(StrictRecord):
    generation: Identifier
    boot: Identifier


def install_maintenance_routes(
    app: FastAPI,
    runtime: Runtime,
    clock: Callable[[], ClockReading] | None,
) -> None:
    @app.post(_PREFIX + "/drain")
    async def drain(
        body: Handshake,
        nonce: str = Header(default="", alias="X-MuLi-Nonce", max_length=96),
    ) -> dict[str, Any]:
        async with runtime.configuration_lock:
            return runtime.maintenance.drain(body.generation, body.boot, nonce)

    @app.post(_PREFIX + "/resume")
    async def resume(
        body: Handshake,
        nonce: str = Header(default="", alias="X-MuLi-Nonce", max_length=96),
    ) -> dict[str, bool]:
        async with runtime.configuration_lock:
            runtime.maintenance.resume(body.generation, body.boot, nonce)
        return {"released": True}

    @app.get(_PREFIX + "/readiness")
    async def readiness(
        generation: str = Query(min_length=1, max_length=96),
        boot: str = Query(min_length=1, max_length=96),
        nonce: str = Header(default="", alias="X-MuLi-Nonce", max_length=96),
    ) -> dict[str, Any]:
        return runtime.maintenance.readiness(generation, boot, nonce).model_dump(mode="json")

    @app.post(_PREFIX + "/activate")
    async def activate(
        body: Handshake,
        nonce: str = Header(default="", alias="X-MuLi-Nonce", max_length=96),
    ) -> dict[str, Any]:
        if clock is None:
            raise MaintenanceError("transaction_unavailable")
        proof = await runtime.maintenance.activate(body.generation, body.boot, nonce, clock)
        return proof.model_dump(mode="json")


def passive_request_allowed(method: str, path: str, main_session: str) -> bool:
    """Only audited local reads and durable owner ingress remain in probation."""
    if (method, path) in INTERNAL_METHODS or not path.startswith("/api/"):
        return True
    if method == "POST":
        return path in {
            "/api/auth/login",
            "/api/auth/logout",
            f"/api/sessions/{main_session}/messages",
        }
    if method != "GET":
        return False
    if path in {
        "/api/auth/status",
        "/api/settings",
        "/api/settings/schema",
        "/api/conversation",
        "/api/sessions",
        "/api/session-records",
        "/api/events",
        "/api/ui/event-position",
        "/api/team",
        "/api/management",
        "/api/extensions",
        "/api/memory/status",
        "/api/channels",
        "/api/approvals",
        "/api/notifications",
        "/api/reminders",
        "/api/workflows",
        "/api/workflow-runs/active",
        "/api/commitments",
        "/api/memories",
        "/api/memories/graph",
        "/api/uploads",
    }:
        return True
    # These route families only read local Store/evidence/files. Capture, voice,
    # semantic queries, media/service probes and future APIs remain closed.
    parts = path.split("/")
    if len(parts) >= 4 and parts[2] == "sessions":
        return (
            len(parts) == 4
            or (len(parts) == 5 and parts[4] in {"messages", "events"})
            or (len(parts) == 7 and parts[4] == "messages" and parts[6] == "content")
        )
    if len(parts) >= 4 and parts[2] == "memories":
        return len(parts) == 4 or (len(parts) == 5 and parts[4] in {"source", "revisions"})
    return (len(parts) == 4 and parts[2] == "commitments") or (
        len(parts) == 5 and parts[2] == "uploads" and parts[4] in {"download", "content"}
    )


def request_has_effect(method: str, path: str, main_session: str) -> bool:
    if not path.startswith("/api/") or (method, path) in INTERNAL_METHODS:
        return False
    # Durable owner ingress is allowed while paused and never launches directly.
    if method == "POST" and path == f"/api/sessions/{main_session}/messages":
        return False
    return not passive_request_allowed(method, path, main_session)
