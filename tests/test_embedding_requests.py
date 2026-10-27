from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from muli.config import ConfigFile
from muli.media import MediaConfig, MediaService, ServiceConfig
from muli.providers import ProviderError


async def test_embedding_purposes_use_exact_prefixes_and_isolated_parameters() -> None:
    common = {"task": "document-task", "nested": {"document": True}, "normalized": True}
    query_only = {"task": "query-task", "nested": {"query": True}}
    before = deepcopy((common, query_only))
    bodies: list[dict[str, Any]] = []

    def receive(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/embeddings"
        bodies.append(body)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [1, 0]} for index, _ in enumerate(body["input"])
                ]
            },
        )

    config = ServiceConfig(
        model="embedding",
        base_url="https://unused.invalid/v1",
        dimensions=2,
        document_instruction="passage: ",
        query_instruction="query:\n",
        extra_body=common,
        query_extra_body=query_only,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        media = MediaService(MediaConfig(embedding=config), client=client)
        assert await media.embed(["原始资料", "另一份资料"]) == [[1.0, 0.0], [1.0, 0.0]]
        assert await media.embed(["原始问题"], purpose="query") == [[1.0, 0.0]]
        assert await media.embed(["新的资料"], purpose="document") == [[1.0, 0.0]]
    assert bodies[0]["input"] == ["passage: 原始资料", "passage: 另一份资料"]
    assert bodies[1]["input"] == ["query:\n原始问题"]
    assert bodies[2]["input"] == ["passage: 新的资料"]
    assert [body["task"] for body in bodies] == ["document-task", "query-task", "document-task"]
    assert [body["nested"] for body in bodies] == [
        {"document": True},
        {"query": True},
        {"document": True},
    ]
    assert all(body["normalized"] is True for body in bodies)
    assert all(
        body["model"] == "embedding"
        and body["encoding_format"] == "float"
        and body["dimensions"] == 2
        for body in bodies
    )
    assert (common, query_only) == before


@pytest.mark.parametrize(
    "field",
    [
        "input",
        "dimensions",
        "encoding_format",
        "model",
        "base_url",
        "api_key",
        "headers",
        "messages",
    ],
)
async def test_query_parameters_cannot_override_embedding_core(field: str) -> None:
    requests: list[httpx.Request] = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("invalid parameters must be rejected before dispatch")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        media = MediaService(
            MediaConfig(
                embedding=ServiceConfig(
                    model="embedding",
                    base_url="https://unused.invalid/v1",
                    query_extra_body={field: "forged"},
                )
            ),
            client=client,
        )
        with pytest.raises(ProviderError, match="核心字段"):
            await media.embed(["问题"], purpose="query")
    assert not requests


@pytest.mark.parametrize("overflow", ["query-prefix", "document-prefix", "query-parameters"])
async def test_complete_embedding_prefix_and_parameters_count_toward_input_budget(
    overflow: str,
) -> None:
    requests: list[httpx.Request] = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("oversized complete request must not reach transport")

    options: dict[str, Any] = {
        "model": "embedding",
        "base_url": "https://unused.invalid/v1",
        "max_input_bytes": 512,
    }
    if overflow == "query-parameters":
        options["query_extra_body"] = {"provider_options": {"long": "中" * 512}}
    else:
        options[
            "document_instruction" if overflow == "document-prefix" else "query_instruction"
        ] = "中" * 512
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        media = MediaService(MediaConfig(embedding=ServiceConfig(**options)), client=client)
        with pytest.raises(ProviderError, match="max_input_bytes"):
            await media.embed(
                ["短"], purpose="document" if overflow == "document-prefix" else "query"
            )
    assert not requests


async def test_document_embedding_does_not_apply_inactive_query_options() -> None:
    bodies: list[dict[str, Any]] = []

    def receive(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})

    config = ServiceConfig(
        model="embedding",
        base_url="https://unused.invalid/v1",
        max_input_bytes=512,
        query_instruction="中" * 512,
        query_extra_body={"input": "not allowed for queries"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        media = MediaService(MediaConfig(embedding=config), client=client)
        assert await media.embed(["文档"]) == [[1.0, 0.0]]
        with pytest.raises(ProviderError, match="purpose"):
            await media.embed(["问题"], purpose="unknown")  # type: ignore[arg-type]
    assert len(bodies) == 1 and bodies[0]["input"] == ["文档"]


def test_embedding_configuration_replaces_query_parameters_and_survives_reopen(
    tmp_path: Path,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "media": {
                "embedding": {
                    "model": "embedding",
                    "base_url": "https://unused.invalid/v1",
                    "document_instruction": "passage: ",
                    "query_instruction": "query: ",
                    "extra_body": {"task": "document", "shared": True},
                    "query_extra_body": {"task": "query", "removed": "old-value"},
                }
            }
        }
    )
    config.update({"media": {"embedding": {"query_extra_body": {"task": "new-query"}}}})
    reopened = ConfigFile(config.path)
    profile = reopened.settings.media.embedding
    assert profile.query_extra_body == {"task": "new-query"}
    assert profile.extra_body == {"task": "document", "shared": True}
    assert profile.document_instruction == "passage: " and profile.query_instruction == "query: "
    reopened.update({"media": {"embedding": {"query_extra_body": {}}}})
    assert ConfigFile(config.path).settings.media.embedding.query_extra_body == {}
