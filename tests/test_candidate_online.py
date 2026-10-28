from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from test_candidate_policy_management import policy_app as policy_app

from muli.candidate_online import (
    OnlinePolicyStore,
    OnlineSettings,
    check_approval,
    preview_artifact,
    receive,
)
from muli.candidate_policy import CandidateError, CandidateLimits, ExpectedPolicy
from muli.candidate_preparation import FrozenPolicy
from muli.format_admission import CURRENT_FORMAT_STATE
from muli.store import Store

VERSION = "2.31.0"
FILENAME = f"muli_agent-{VERSION}-py3-none-any.whl"
URL = f"https://files.pythonhosted.org/packages/fixture/{FILENAME}"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks, self.closed = chunks, False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def response(
    data: bytes, *, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json", **(headers or {})},
        stream=Chunks(data),
    )


def metadata(**changes: object) -> dict[str, Any]:
    file: dict[str, Any] = {
        "packagetype": "bdist_wheel",
        "filename": FILENAME,
        "url": URL,
        "size": 2048,
        "digests": {"sha256": "a" * 64},
        "requires_python": ">=3.12",
        "yanked": False,
    }
    file.update(changes)
    return {"info": {"name": "muli-agent", "version": VERSION}, "urls": [file]}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: object, **kwargs: object) -> httpx.Response:
        pytest.fail("online candidate tests must use MockTransport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@pytest.fixture
def frozen() -> FrozenPolicy:
    # This fixture tests pure metadata/If-Match, not installation authority.
    # Host integration tests separately use real Store/managed source observers.
    expected = ExpectedPolicy(
        source="local", revision="b" * 64, limits=CandidateLimits(), disk_mode="threshold"
    )
    return cast(
        FrozenPolicy,
        SimpleNamespace(
            expected=expected,
            source=SimpleNamespace(
                slot=SimpleNamespace(digest="c" * 64),
                binding_revision="d" * 64,
                observed_format=CURRENT_FORMAT_STATE,
            ),
            original_intent=SimpleNamespace(model_dump=lambda **kwargs: {"fixture_intent": True}),
            materials=SimpleNamespace(digest="e" * 64),
        ),
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    value = Store(tmp_path / "policy.sqlite3")
    try:
        yield value
    finally:
        value.close()


async def test_version_preview_is_small_read_only_and_binds_every_approval_fact(
    frozen: FrozenPolicy,
) -> None:
    requests: list[httpx.Request] = []

    def send(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["accept-encoding"] == "identity"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return response(json.dumps(metadata()).encode())

    settings = OnlineSettings(enabled=True)
    artifact = await preview_artifact(
        settings, frozen, VERSION, verify=lambda: None, transport=httpx.MockTransport(send)
    )
    assert len(requests) == 1
    assert str(requests[0].url) == f"https://pypi.org/pypi/muli-agent/{VERSION}/json"
    assert artifact.url == URL and artifact.filename == FILENAME
    assert artifact.size == 2048 and artifact.sha256 == "a" * 64
    assert artifact.requires_python == ">=3.12" and artifact.yanked is False
    assert artifact.online_revision == settings.revision
    assert artifact.candidate_revision == frozen.expected.revision
    assert artifact.installation == frozen.source.slot.digest
    assert artifact.binding_revision == frozen.source.binding_revision
    assert artifact.local_materials == frozen.materials.digest
    assert len(artifact.intent) == len(artifact.format) == 64
    check_approval(settings, artifact, frozen, VERSION)
    with pytest.raises(CandidateError, match="candidate_online_changed"):
        check_approval(settings.model_copy(update={"enabled": False}), artifact, frozen, VERSION)


@pytest.mark.parametrize(
    "change",
    [
        {"size": True},
        {"size": "2048"},
        {"size": 0},
        {"yanked": 0},
        {"yanked": True},
        {"yanked": "false"},
        {"digests": {}},
        {"digests": {"sha256": "bad"}},
        {"url": URL + "?secret=never-echo"},
        {"url": URL.replace("https:", "http:")},
        {"url": URL.replace("files.pythonhosted.org", "other.invalid")},
        {"requires_python": ">=99"},
        {"requires_python": 3},
    ],
    ids=[
        "bool-size",
        "string-size",
        "zero-size",
        "int-yank",
        "yanked",
        "string-yank",
        "missing-hash",
        "bad-hash",
        "query",
        "http",
        "origin",
        "python",
        "bad-python",
    ],
)
async def test_malformed_or_unapproved_metadata_is_a_fixed_failure(
    frozen: FrozenPolicy, change: dict[str, object]
) -> None:
    transport = httpx.MockTransport(lambda _: response(json.dumps(metadata(**change)).encode()))
    with pytest.raises(CandidateError) as error:
        await preview_artifact(
            OnlineSettings(enabled=True), frozen, VERSION, verify=lambda: None, transport=transport
        )
    assert "never-echo" not in str(error.value)


@pytest.mark.parametrize("kind", ["duplicate", "many", "other-version", "no-wheel", "bad-json"])
async def test_metadata_does_not_choose_an_ambiguous_or_different_file(
    frozen: FrozenPolicy, kind: str
) -> None:
    payload = metadata()
    if kind == "duplicate":
        payload["urls"] *= 2
    elif kind == "many":
        payload["urls"] *= 65
    elif kind == "other-version":
        payload["info"]["version"] = "2.32.0"
    elif kind == "no-wheel":
        payload["urls"][0]["packagetype"] = "sdist"
    data = b'{"info":{},"info":{}}' if kind == "bad-json" else json.dumps(payload).encode()
    with pytest.raises(CandidateError, match="candidate_online_metadata_invalid"):
        await preview_artifact(
            OnlineSettings(enabled=True),
            frozen,
            VERSION,
            verify=lambda: None,
            transport=httpx.MockTransport(lambda _: response(data)),
        )


@pytest.mark.parametrize("kind", ["overflow", "error", "redirect", "encoding", "length"])
async def test_raw_body_budget_and_transport_contract_apply_to_all_responses(
    frozen: FrozenPolicy,
    kind: str,
) -> None:
    headers: dict[str, str] = {}
    status, data = 200, b"x" * 1025
    if kind == "error":
        status = 500
    elif kind == "redirect":
        status, data, headers = 302, b"", {"location": "https://other.invalid/wheel"}
    elif kind == "encoding":
        headers = {"content-encoding": "gzip"}
    elif kind == "length":
        headers = {"content-length": "1"}
    stream = response(data, status=status, headers=headers)
    calls: list[httpx.Request] = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return stream

    with pytest.raises(CandidateError):
        await receive(
            URL,
            settings=OnlineSettings(enabled=True),
            limit=1024,
            deadline=asyncio.get_running_loop().time() + 2,
            close_seconds=frozen.expected.limits.stop_seconds,
            media_types=frozenset({"application/json"}),
            consume=lambda _: None,
            verify=lambda: None,
            transport=httpx.MockTransport(send),
        )
    assert len(calls) == 1  # No redirects/retries, including an error response.
    assert stream.is_closed


async def test_absolute_deadline_closes_a_stalled_response(
    frozen: FrozenPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    from muli import candidate_online

    entered = asyncio.Event()

    class Stalled(Chunks):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

    # Exercise the real 10ms response timer, independently of client construction.
    # The paired TLS-construction test verifies that setup cannot renew a deadline.
    monkeypatch.setattr(candidate_online, "time", SimpleNamespace(monotonic=lambda: 0.0))
    body = Stalled()
    with pytest.raises(CandidateError, match="candidate_online_unavailable"):
        await receive(
            URL,
            settings=OnlineSettings(),
            limit=1024,
            deadline=0.01,
            close_seconds=frozen.expected.limits.stop_seconds,
            media_types=frozenset({"application/json"}),
            consume=lambda _: None,
            verify=lambda: None,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body)),
        )
    assert entered.is_set() and body.closed


@pytest.mark.parametrize("failure", ["timeout", "error"])
async def test_cleanup_is_owned_bounded_and_always_attempts_client_close(
    frozen: FrozenPolicy,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    started, stopped = asyncio.Event(), asyncio.Event()
    clients: list[bool] = []

    class Failing(Chunks):
        async def aclose(self) -> None:
            started.set()
            try:
                if failure == "timeout":
                    await asyncio.Event().wait()
                raise OSError("synthetic private close error")
            finally:
                stopped.set()

    class Transport(httpx.MockTransport):
        async def aclose(self) -> None:
            clients.append(True)

    stream = Failing(b"bad")
    task = asyncio.create_task(
        receive(
            URL,
            settings=OnlineSettings(),
            limit=1024,
            deadline=asyncio.get_running_loop().time() + 2,
            close_seconds=0.02,
            media_types=frozenset({"application/json"}),
            consume=lambda _: None,
            verify=lambda: None,
            # Reject the header before body EOF's automatic response close.
            transport=Transport(
                lambda _: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
            ),
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    task.cancel()
    with pytest.raises(CandidateError, match="^candidate_online_close_failed$"):
        await asyncio.wait_for(task, 1)
    assert task.done() and stopped.is_set() and clients == [True]


async def test_client_tls_construction_cannot_extend_absolute_deadline(
    frozen: FrozenPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muli import candidate_online

    clock = [0.0]
    client = httpx.AsyncClient
    sent: list[httpx.Request] = []

    def construct(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False
        assert kwargs["verify"] is True and kwargs["follow_redirects"] is False
        clock[0] = 2.0
        return client(**kwargs)

    def send(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return response(b"{}")

    monkeypatch.setattr(candidate_online.httpx, "AsyncClient", construct)
    # Use a module-local clock object; changing time.monotonic globally would
    # perturb asyncio's own timer and conceal this boundary.
    monkeypatch.setattr(candidate_online, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(CandidateError, match="^candidate_deadline_exceeded$"):
        await receive(
            URL,
            settings=OnlineSettings(),
            limit=1024,
            deadline=1.0,
            close_seconds=frozen.expected.limits.stop_seconds,
            media_types=frozenset({"application/json"}),
            consume=lambda _: None,
            verify=lambda: None,
            transport=httpx.MockTransport(send),
        )
    assert not sent


async def test_online_policy_api_and_tool_reuse_owner_and_configuration_lock(
    policy_app: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    app, client = policy_app
    path = "/api/management/update-online-policy"
    config = app.state.runtime.config.path.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://localhost:8765",
    ) as stranger:
        assert (await stranger.get(path)).status_code == 401
        assert (await stranger.put(path, json={})).status_code == 401
    assert (
        await client.put(path, json={}, headers={"Origin": "https://foreign.invalid"})
    ).status_code == 403
    assert (
        await client.put(path, content="{}", headers={"Content-Type": "text/plain"})
    ).status_code == 415
    assert (await client.put(path, json={"enabled": True})).status_code == 200
    assert (await client.get(path)).json() == OnlineSettings(enabled=True).model_dump(mode="json")
    tool = app.state.runtime.tools["self_update_online_configure"]
    assert tool.risk == "write"
    assert await tool.handler({}) == OnlineSettings().model_dump(mode="json")
    assert app.state.runtime.config.path.read_bytes() == config
    assert app.state.management.plans() == []
    async with app.state.runtime.configuration_lock:
        queued = asyncio.create_task(client.put(path, json={"enabled": True}))
        await asyncio.sleep(0)
        app.state.management._closing = True
    try:
        result = await asyncio.wait_for(queued, 2)
        assert result.status_code == 503
        assert app.state.management.online_policy.read() == OnlineSettings()
    finally:
        app.state.management._closing = False


def test_policy_is_independent_bounded_and_does_not_reset_corruption(store: Store) -> None:
    policies = OnlinePolicyStore(store)
    before = store.db.total_changes
    assert policies.read() == OnlineSettings()
    assert store.db.total_changes == before
    enabled = OnlineSettings(enabled=True)
    assert policies.replace(enabled) == enabled == policies.read()
    assert store.get("update_policy", "candidate-v1") is None
    assert store.events()[-1]["data"] == {
        "id": "online-v1",
        "enabled": True,
        "revision": enabled.revision,
    }
    store.put("update_policy", {"id": "online-v1", "schema_version": 2, "policy": {}})
    before = store.db.total_changes
    for action in (policies.read, lambda: policies.replace(OnlineSettings())):
        with pytest.raises(CandidateError, match="candidate_online_policy_invalid"):
            action()
    assert store.db.total_changes == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("metadata_base", "http://pypi.org/pypi"),
        ("metadata_base", "https://user:secret@pypi.org/pypi"),
        ("metadata_base", "https://pypi.org/pypi?key=secret"),
        ("artifact_origins", ["https://files.pythonhosted.org/path"]),
        ("artifact_origins", ["https://files.pythonhosted.org"] * 2),
        ("max_files", True),
        ("download_bytes", 1024**3),
    ],
    ids=["http", "credentials", "query", "origin-path", "duplicate", "bool", "budget"],
)
def test_owner_policy_has_a_strict_finite_contract(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        OnlineSettings.model_validate({field: value})
