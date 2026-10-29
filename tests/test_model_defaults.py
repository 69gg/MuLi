"""Optional provider token limits and complete, capability-preserving role inheritance."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from muli.attachment_memory_source import attachment_source
from muli.attachments import Attachments
from muli.config import ConfigFile, ModelProfile, Settings
from muli.local_services import LocalServices
from muli.media import MediaConfig, MediaService
from muli.memory_application import MemoryApplication
from muli.providers import Message, ModelClient, ModelConfig, ProviderError
from muli.providers.codecs import build_request
from muli.providers.types import ProtocolName
from muli.runtime import Runtime


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses"])
async def test_unset_output_limit_is_absent_from_actual_openai_request(
    protocol: ProtocolName,
) -> None:
    seen: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & body.keys()
        if protocol == "openai_chat":
            return httpx.Response(200, json={"choices": [{"message": {"content": "完成"}}]})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "完成"}],
                    }
                ],
            },
        )

    profile = ModelConfig(protocol, "model", "https://model.test/v1", stream=False)
    assert profile.max_output_tokens is None
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        async with ModelClient(profile, http) as client:
            assert (await client.complete([Message("user", "测试")])).content == "完成"
    assert len(seen) == 1


def test_anthropic_requires_explicit_limit_only_for_configured_profiles() -> None:
    assert ModelProfile().max_output_tokens is None
    assert ModelProfile(protocol="anthropic").max_output_tokens is None
    assert Settings().models.main.max_output_tokens is None
    complete = {"protocol": "anthropic", "model": "claude", "base_url": "https://model.test/v1"}
    with pytest.raises(ValidationError, match="max_output_tokens"):
        ModelProfile.model_validate(complete)
    with pytest.raises(ValueError, match="max_output_tokens"):
        ModelConfig("anthropic", "claude", "https://model.test/v1")
    profile = ModelProfile.model_validate({**complete, "max_output_tokens": 2048})
    body = build_request(
        ModelConfig(
            "anthropic",
            profile.model,
            profile.base_url,
            max_output_tokens=profile.max_output_tokens,
        ),
        [Message("user", "测试")],
        [],
        stream=False,
    )
    assert body["max_tokens"] == 2048


@pytest.mark.parametrize("invalid", [True, 0, -1, "2048", 128.5])
def test_explicit_token_limit_rejects_non_integer_or_non_positive_values(invalid: Any) -> None:
    with pytest.raises(ValidationError):
        ModelProfile(max_output_tokens=invalid)
    with pytest.raises(ValueError):
        ModelConfig("openai_chat", "model", "https://model.test/v1", max_output_tokens=invalid)


@pytest.mark.parametrize(
    ("protocol", "field"),
    [
        ("openai_chat", "max_tokens"),
        ("openai_chat", "max_completion_tokens"),
        ("openai_responses", "max_output_tokens"),
    ],
)
def test_explicit_vendor_limit_survives_unset_cap_and_only_tightens_set_cap(
    protocol: ProtocolName,
    field: str,
) -> None:
    for cap, expected in ((None, 512), (256, 256)):
        profile = ModelConfig(
            protocol,
            "model",
            "https://model.test/v1",
            max_output_tokens=cap,
            extra_body={field: 512},
        )
        assert build_request(profile, [Message("user", "q")], [], stream=False)[field] == expected
    for invalid in (None, True, 0, "512"):
        profile = ModelConfig(
            protocol, "model", "https://model.test/v1", extra_body={field: invalid}
        )
        with pytest.raises(ValueError, match="正整数"):
            build_request(profile, [Message("user", "q")], [], stream=False)


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic"])
async def test_empty_llm_roles_inherit_complete_primary_but_not_http_services(
    tmp_path: Path,
    protocol: ProtocolName,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "models": {
                "main": {
                    "protocol": protocol,
                    "model": "primary",
                    "base_url": "https://model.test/v1",
                    "api_key": "key",
                    "max_output_tokens": 2048 if protocol == "anthropic" else None,
                    "vision": False,
                },
                "worker": {"protocol": "anthropic"},
            }
        }
    )
    runtime = Runtime(config, tmp_path / "data")
    try:
        main = runtime.model_config("main")
        for role in ("reviewer", "worker", "vision", "compactor", "memory_extractor"):
            assert runtime.model_config(role) == main
        assert not runtime.model_config("vision").vision
        config.update(
            {
                "models": {
                    "reviewer": {
                        "model": "separate-reviewer",
                        "base_url": "https://review.test/v1",
                    }
                }
            }
        )
        assert runtime.model_config("reviewer").model == "separate-reviewer"
        assert runtime.model_config("reviewer").max_output_tokens is None
        assert (
            ConfigFile(config.path).settings.models.main.max_output_tokens == main.max_output_tokens
        )
        local = LocalServices(runtime, Attachments(runtime.store, tmp_path / "attachments", config))
        async with local.media() as media:
            assert media.config.embedding is None and media.config.reranker is None
            assert media.config.asr is None and media.config.tts is None
            with pytest.raises(ProviderError, match="外挂视觉"):
                await media.describe_image(b"\x89PNG\r\n\x1a\n", "image/png", "看图")
    finally:
        await runtime.close()


async def test_inherited_reviewer_has_its_own_toolless_context(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "models": {
                "main": {
                    "model": "only-primary",
                    "base_url": "https://model.test/v1",
                    "stream": False,
                }
            },
            "updates": {"check_on_start": False},
        }
    )
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        text = "已正常结束" if len(requests) == 1 else '{"allow":true,"reason":"只读范围明确"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        runtime = Runtime(
            config, tmp_path / "data", client_factory=lambda profile: ModelClient(profile, http)
        )
        try:
            receipt = await runtime.submit("此前独立对话的私密正文", source="web", mode="yolo")
            assert await runtime.wait(receipt["run_id"]) == "已正常结束"
            assert await runtime.review("fs_read", {"path": "说明.txt"}, "read") == (
                True,
                "只读范围明确",
            )
            assert len(requests) == 2
            review = requests[1]
            assert review["model"] == requests[0]["model"] == "only-primary"
            assert "tools" not in review
            assert [message["role"] for message in review["messages"]] == ["system", "user"]
            assert "此前独立对话的私密正文" not in json.dumps(review, ensure_ascii=False)
            assert json.loads(review["messages"][1]["content"])["tool"] == "fs_read"
            assert "max_completion_tokens" not in review
        finally:
            await runtime.close()


@pytest.mark.parametrize("user_cap", [None, 128])
async def test_internal_compaction_and_extraction_keep_their_original_caps(
    tmp_path: Path,
    user_cap: int | None,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "models": {
                "main": {
                    "model": "primary",
                    "base_url": "https://model.test/v1",
                    "max_output_tokens": user_cap,
                    "stream": False,
                }
            }
        }
    )
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        text = "有界摘要" if len(requests) == 1 else '{"memories":[],"aliases":{}}'
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        runtime = Runtime(
            config, tmp_path / "data", client_factory=lambda profile: ModelClient(profile, http)
        )
        memory = MemoryApplication(runtime, lambda: MediaService(MediaConfig()))
        try:
            assert await runtime._summarize("待压缩资料", 512) == "有界摘要"
            attachments = Attachments(runtime.store, tmp_path / "attachments", config)

            async def chunks() -> AsyncIterator[bytes]:
                yield "只读来源".encode()

            item = await attachments.save(chunks(), "caps.txt", "text/plain", source="web")
            source = attachment_source(item["id"], "只读来源", item["sha256"], item["created_at"])
            memory.pipeline.ingest(source, trusted_owner=False)
            await memory.start()
            job = await asyncio.wait_for(memory.pipeline.wait(source["id"]), 4)
            assert job is not None and job["state"] == "done"
            assert job["checkpoint"]["extraction"] == {
                "memories": [],
                "aliases": {},
                "rejected_untrusted_owner_claims": 0,
            }
            assert len(requests) == 2
            assert requests[0]["max_completion_tokens"] == (128 if user_cap is not None else 512)
            assert requests[1]["max_completion_tokens"] == (128 if user_cap is not None else 4096)
            assert runtime.model_config("main").max_output_tokens == user_cap
        finally:
            await memory.close()
            await runtime.close()
