"""Offline owner journeys through real tools, ingress, workers and reopen.

Fake clients consume model messages/tool results only. The host assertions may
read persisted facts; those facts are never fed back as a model-side oracle.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from copy import deepcopy
from itertools import count
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_channels import onebot_body, onebot_event, terminal
from test_mobile_task_control_followup import delivery_text
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import eventually, tool_result

from muli.config import ConfigFile
from muli.context import ContextLimitError, context_size, minimum_context
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app

_OWNER = "真实主人委托：\n"
_REVIEW = "仅用正式问题目录核对原问、已答未答与交回事实。"
_INTERPRETATION = "AI协调解释：按原任务补充出处；这段转述不是主人原文。"
_ANSWER_MARKER = "owner_question_answer"
_READ_CALL_IDS = count(1)


def call(identifier: str, name: str, /, **arguments: Any) -> ToolCall:
    return ToolCall(identifier, name, json.dumps(arguments, ensure_ascii=False))


def latest_user(messages: list[Message]) -> Message:
    return next(message for message in reversed(messages) if message.role == "user")


def input_text(message: Message) -> str:
    if message.content.startswith("渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："):
        return message.content.split("\n", 1)[1]
    return message.content


def tool_message(messages: list[Message], identifier: str) -> Message:
    return next(
        message
        for message in reversed(messages)
        if message.role == "tool" and message.tool_call_id == identifier
    )


def preserved_result(messages: list[Message], identifier: str) -> dict[str, Any]:
    message = tool_message(messages, identifier)
    assert message.preserve_content is True
    value = json.loads(message.content)
    assert isinstance(value, dict) and "error" not in value, value
    return value


class QuestionEvidence:
    """Read only official directory/record pages, including empty continuation pages."""

    def __init__(
        self,
        *,
        target: str | None = None,
        task_ids: list[str] | None = None,
    ) -> None:
        self.target = target
        self.task_ids = list(task_ids or [])
        self.stage = "directory" if task_ids is None else "all"
        self.pending: tuple[str, str, dict[str, Any]] | None = None
        self.task_index = 0
        self.metadata: list[dict[str, Any]] = []
        self.open_ids: list[str] = []
        self.records: dict[str, dict[str, Any]] = {}
        self.read_items: list[dict[str, Any]] = []
        self.record_index = 0
        self.fragments: list[str] = []
        self.body_pages = 0

    def _request(self, kind: str, name: str, arguments: dict[str, Any]) -> ModelTurn:
        # Protocol identities only; shared across readers, clients and reopened runtimes.
        identifier = f"r{next(_READ_CALL_IDS):03x}"
        self.pending = kind, identifier, arguments
        return ModelTurn(
            tool_calls=[
                ToolCall(
                    identifier,
                    name,
                    json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                )
            ]
        )

    def advance(self, messages: list[Message]) -> ModelTurn | None:
        if self.pending is not None:
            kind, identifier, arguments = self.pending
            page = preserved_result(messages, identifier)
            self.pending = None
            if kind == "directory":
                self.task_ids.extend(item["id"] for item in page["items"])
                if page["has_more"]:
                    return self._request(
                        "directory",
                        "commitment_directory",
                        {
                            "status": "all",
                            "limit": 2,
                            "cursor": page["next_cursor"],
                        },
                    )
                assert self.task_ids and len(self.task_ids) == len(set(self.task_ids))
                self.stage = "all"
            elif kind == "all":
                assert page["section"] == "owner_questions"
                assert page["unavailable"] == 0
                self.metadata.extend(page["items"])
                if page["has_more"]:
                    return self._request(
                        kind,
                        "commitment_inspect",
                        {
                            **arguments,
                            "cursor": page["next_cursor"],
                            "revision": page["revision"],
                        },
                    )
                self.task_index += 1
            else:
                assert kind == "body" and page["section"] == "owner_questions"
                assert page["record_id"] == arguments["record_id"]
                assert page["offset_bytes"] == arguments["offset_bytes"]
                assert page["length_bytes"] == len(page["content"].encode("utf-8"))
                self.body_pages += 1
                self.fragments.append(page["content"])
                if page["has_more"]:
                    assert page["next_offset_bytes"] > page["offset_bytes"]
                    return self._request(
                        "body",
                        "commitment_inspect",
                        {
                            **arguments,
                            "record_revision": page["record_revision"],
                            "offset_bytes": page["next_offset_bytes"],
                        },
                    )
                record = json.loads("".join(self.fragments))
                assert record["id"] == page["record_id"]
                self.records[record["id"]] = record
                self.fragments = []
                self.record_index += 1
        if self.stage == "directory":
            return self._request("directory", "commitment_directory", {"status": "all", "limit": 2})
        if self.stage == "all":
            if self.task_index < len(self.task_ids):
                return self._request(
                    self.stage,
                    "commitment_inspect",
                    {
                        "identifier": self.task_ids[self.task_index],
                        "section": "owner_questions",
                        "question_status": self.stage,
                        "limit": 2,
                    },
                )
            self.read_items = [
                item
                for item in self.metadata
                if self.target is None or item["question_id"] == self.target
            ]
            assert self.read_items and len(
                {item["question_id"] for item in self.read_items}
            ) == len(self.read_items)
            if self.target is not None:
                assert len(self.read_items) == 1
            self.stage = "body"
        if self.stage == "body" and self.record_index < len(self.read_items):
            item = self.read_items[self.record_index]
            return self._request(
                "body",
                "commitment_inspect",
                {
                    "identifier": item["task_id"],
                    "section": "owner_questions",
                    "record_id": item["record_id"],
                    "record_revision": item["record_revision"],
                    "offset_bytes": 0,
                    "length_bytes": 4096,
                },
            )
        self.open_ids = [
            item["question_id"]
            for item in self.metadata
            if item["status"] == "open" and item["answer_recorded"] is False
        ]
        self.stage = "done"
        return None

    def summary(self) -> str:
        items: list[dict[str, Any]] = []
        for metadata in self.metadata:
            record = self.records[metadata["question_id"]]
            answer = record.get("answer")
            items.append(
                {
                    "question_id": record["id"],
                    "task_id": record["task_id"],
                    "worker_id": record["worker_id"],
                    "message": record["message"],
                    "attachment_ids": record["attachment_ids"],
                    "status": metadata["status"],
                    "answer_recorded": answer is not None,
                    "owner_body": answer["owner"]["body"] if answer else None,
                    "owner_original_text": answer["owner"]["original_text"] if answer else None,
                    "owner_source": answer["owner"]["source"] if answer else None,
                    "owner_principal": answer["owner"]["principal"] if answer else None,
                    "interpretation": answer["interpretation"] if answer else None,
                    "answer_attachment_ids": answer["owner"]["attachment_ids"] if answer else [],
                    "delivery_state": metadata["delivery_state"],
                }
            )
        return json.dumps(
            {"items": items, "open_ids": self.open_ids, "body_pages": self.body_pages},
            ensure_ascii=False,
        )


class JourneyMain(FakeClient):
    """Current real owner input and formal tools are the only decision inputs."""

    def __init__(self) -> None:
        super().__init__([], [])
        self.prefix = ""
        self.operation: dict[str, Any] | None = None
        self.assignment_index = 0
        self.saving = True
        self.reader: QuestionEvidence | None = None
        self.wrote = False
        self.unknown_started = False
        self.command: tuple[str, str, str | None] | None = None

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        if not self.prefix:
            incoming = latest_user(messages)
            assert incoming.source_message_id
            self.prefix = "oq-" + incoming.source_message_id
            text = input_text(incoming)
            if text.startswith(_OWNER):
                self.operation = json.loads(text.removeprefix(_OWNER))
            elif text == _REVIEW:
                self.reader = QuestionEvidence()
            elif text == "好的":
                # Deliberately attempt the new action without an explicit owner selection.
                self.command = "answer", "", "dispatch"
                self.reader = QuestionEvidence()
            else:
                answer = re.fullmatch(r"/(回答问题|回答并继续) (\S+)\n([\s\S]+)", text)
                dispatch = re.fullmatch(r"/继续问题 (\S+)", text)
                if answer is not None:
                    self.command = (
                        "answer",
                        answer[2],
                        "record_only" if answer[1] == "回答问题" else "dispatch",
                    )
                elif dispatch is not None:
                    self.command = "dispatch", dispatch[1], None
                else:
                    yield StreamEvent(
                        "done", turn=ModelTurn(content="队友资料已接收，任务尚未验收。")
                    )
                    return
                self.reader = QuestionEvidence(target=self.command[1])
        if self.operation is not None:
            operation = self.operation
            if operation["action"] == "assign":
                tasks = operation["tasks"]
                if self.assignment_index == len(tasks):
                    turn = ModelTurn(content="完整任务及各自附件已交给原资料队友。")
                else:
                    task = tasks[self.assignment_index]
                    suffix = str(self.assignment_index)
                    if self.saving:
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    self.prefix + "-save-" + suffix,
                                    "commitment_save",
                                    title=task["title"],
                                    brief={
                                        "goal": task["goal"],
                                        "constraints": task["constraints"],
                                        "evidence_refs": [
                                            {"kind": "attachment", "id": task["attachment_id"]}
                                        ],
                                    },
                                )
                            ]
                        )
                        self.saving = False
                    else:
                        saved = tool_result(messages, self.prefix + "-save-" + suffix)
                        assert "error" not in saved and saved["has_brief"] is True
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    self.prefix + "-spawn-" + suffix,
                                    "spawn_agent",
                                    task_name=task["worker_name"],
                                    prompt=json.dumps(
                                        {"action": "work", "attachment_id": task["attachment_id"]},
                                        ensure_ascii=False,
                                    ),
                                    commitment_id=saved["id"],
                                    attachment_ids=[task["attachment_id"]],
                                )
                            ]
                        )
                        self.assignment_index += 1
                        self.saving = True
            elif operation["action"] == "unknown":
                if not self.unknown_started:
                    self.unknown_started = True
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                self.prefix + "-unknown",
                                "channel_send",
                                channel=operation["channel"],
                                target=operation["target"],
                                text=operation["text"],
                                idempotency_key=operation["key"],
                            )
                        ]
                    )
                else:
                    # Non-preserve replies may be rendered excerpts; never JSON-parse them here.
                    assert tool_message(messages, self.prefix + "-unknown").content
                    turn = ModelTurn(content="未知边界投递已登记，后续先核原状态。")
            else:
                assert operation["action"] == "archive"
                turn = ModelTurn(content="有界背景已归档，没有新增回答或派工。")
        elif self.reader is not None:
            if self.wrote:
                assert tool_message(messages, self.prefix + "-write").content
                turn = ModelTurn(content="本轮明确问题操作已提交，交回及领取须另核事实。")
            else:
                turn = self.reader.advance(messages)
                if turn is None:
                    if self.command is None:
                        turn = ModelTurn(content=self.reader.summary())
                    else:
                        action, identifier, delivery = self.command
                        if not identifier:
                            assert len(self.reader.records) == 1
                            identifier = next(iter(self.reader.records))
                        record = self.reader.records[identifier]
                        question: dict[str, Any] = {"action": action, "id": identifier}
                        if delivery is not None:
                            question["delivery"] = delivery
                        self.wrote = True
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    self.prefix + "-write",
                                    "send_input",
                                    target=record["worker_id"],
                                    message=_INTERPRETATION,
                                    question=question,
                                )
                            ]
                        )
        else:
            turn = ModelTurn(content="队友资料已接收，任务尚未验收。")
        yield StreamEvent("done", turn=turn)


class JourneyWorker(FakeClient):
    def __init__(
        self, parked: asyncio.Event | None = None, release: asyncio.Event | None = None
    ) -> None:
        super().__init__([], [])
        self.parked, self.release = parked, release
        self.prefix = ""
        self.parent = ""
        self.job: dict[str, Any] | None = None
        self.answer_body: str | None = None
        self.question_id: str | None = None
        self.stage = "initial"
        self.reader: QuestionEvidence | None = None
        self.source: dict[str, Any] | None = None
        self.brief: dict[str, Any] | None = None
        self.answer_attachment_ids: list[str] = []

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        if not self.prefix:
            incoming = latest_user(messages)
            assert incoming.source_message_id
            self.prefix = "oq-worker-" + incoming.source_message_id
            for message in messages:
                if message.role == "user" and message.content.startswith("队内任务消息；"):
                    payload = json.loads(message.content.split("\n", 1)[1])
                    job = json.loads(payload["content"])
                    if job["action"] == "work":
                        self.parent, self.job = payload["sender"], job
            assert self.job is not None and self.parent
            if incoming.content.startswith("主人已明确交回问题 "):
                heading = incoming.content.split("\n", 1)[0]
                _, body = incoming.content.split("\n主人原答：\n", 1)
                self.answer_body, interpretation = body.split(
                    "\n\n主助手协调解释（不是主人原文）：\n", 1
                )
                match = re.fullmatch(
                    r"主人已明确交回问题 (\S+) 的原答；这是原任务补充，不增加其他授权。", heading
                )
                assert match is not None and interpretation != self.answer_body
                self.question_id = match[1]
        if self.stage == "initial":
            self.stage = "source"
            turn = ModelTurn(
                tool_calls=[
                    call(self.prefix + "-brief", "commitment_read"),
                    call(
                        self.prefix + "-source",
                        "attachment_read",
                        id=self.job["attachment_id"],
                        offset_bytes=0,
                    ),
                ]
            )
        elif self.stage == "source":
            self.brief = preserved_result(messages, self.prefix + "-brief")
            source = preserved_result(messages, self.prefix + "-source")
            assert self.brief["has_brief"] is True and self.brief["has_more"] is False
            assert source["has_more"] is False and source["id"] == self.job["attachment_id"]
            self.source = json.loads(source["content"])
            if self.question_id is None:
                self.stage = "registered"
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            self.prefix + "-register",
                            "send_input",
                            target=self.parent,
                            message=self.source["question"],
                            attachment_ids=[source["id"]],
                            question={"action": "register", "key": self.source["key"]},
                        )
                    ]
                )
            else:
                self.reader = QuestionEvidence(target=self.question_id, task_ids=[self.brief["id"]])
                self.stage = "answer_record"
                turn = self.reader.advance(messages)
                assert turn is not None
        elif self.stage == "registered":
            assert tool_message(messages, self.prefix + "-register").content
            if self.parked is not None and self.release is not None:
                self.parked.set()
                await self.release.wait()
            turn = ModelTurn(content="本轮已在显式问题边界停靠；尚未验收任务，没有发布。")
        elif self.stage == "answer_record":
            assert self.reader is not None and self.question_id is not None
            turn = self.reader.advance(messages)
            if turn is None:
                record = self.reader.records[self.question_id]
                assert record["answer"]["owner"]["body"] == self.answer_body
                self.answer_attachment_ids = list(record["answer"]["owner"]["attachment_ids"])
                assert self.answer_attachment_ids
                self.stage = "answer_files"
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            self.prefix + "-answer-file-" + str(index),
                            "attachment_read",
                            id=identifier,
                            offset_bytes=0,
                        )
                        for index, identifier in enumerate(self.answer_attachment_ids)
                    ]
                )
        else:
            assert (
                self.stage == "answer_files" and self.source is not None and self.brief is not None
            )
            answer_files = [
                preserved_result(messages, self.prefix + "-answer-file-" + str(index))
                for index in range(len(self.answer_attachment_ids))
            ]
            assert all(page["has_more"] is False for page in answer_files)
            turn = ModelTurn(
                content=json.dumps(
                    {
                        "kind": "supplemented_result",
                        "task_id": self.brief["id"],
                        "question_id": self.question_id,
                        "facts": self.source["facts"],
                        "owner_answer": self.answer_body,
                        "answer_sources": [page["content"] for page in answer_files],
                        "answer_attachment_ids": [page["id"] for page in answer_files],
                    },
                    ensure_ascii=False,
                )
            )
        yield StreamEvent("done", turn=turn)


class JourneyFactory:
    def __init__(
        self, parked: asyncio.Event | None = None, release: asyncio.Event | None = None
    ) -> None:
        self.parked, self.release = parked, release
        self.created: list[str] = []

    def __call__(self, config: ModelConfig) -> FakeClient:
        self.created.append(config.model)
        return (
            JourneyWorker(self.parked, self.release) if config.model == "worker" else JourneyMain()
        )


class NoModelCalls(FakeClient):
    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        raise AssertionError("正常重开与真实渠道重投不得调用模型或重放旧任务")
        yield StreamEvent("done", turn=ModelTurn())


class IdleFactory:
    def __init__(self) -> None:
        self.created: list[str] = []

    def __call__(self, config: ModelConfig) -> FakeClient:
        self.created.append(config.model)
        return NoModelCalls([], [])


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("问题旅程只允许 ASGITransport 和本测试显式平台传输")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)


async def configured(tmp_path: Path, *, channels: bool = False) -> Runtime:
    runtime, _ = make_runtime(tmp_path, [])
    values: dict[str, Any] = {
        "default_mode": "yolo",
        "updates": {"check_on_start": False},
        "memory": {"auto_extract": False},
        "compaction": {"enabled": False},
        "attention": {"enabled": False},
        "extensions": {"watch_enabled": False},
        "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
        "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
    }
    if channels:
        values["channels"] = {
            "onebot": {
                "enabled": True,
                "account_id": "123",
                "base_url": "https://platform.invalid",
                "webhook_secret": "FAKE_WEBHOOK",
                "owner_ids": ["7"],
                "receive_files": True,
            }
        }
    runtime.config.update(values)
    await runtime.reconfigure()
    return runtime


async def main_idle(runtime: Runtime) -> None:
    identifier = runtime.main_session()["id"]
    await eventually(
        runtime,
        lambda: identifier not in runtime.tasks and not runtime.conversation.pending(identifier),
    )


async def all_idle(runtime: Runtime) -> None:
    await eventually(
        runtime,
        lambda: (
            not any(not task.done() for task in runtime.tasks.values())
            and not runtime.conversation.pending(runtime.main_session()["id"])
            and all(agent["status"] == "completed" for agent in runtime.team.list())
            and all(report["status"] != "pending" for report in runtime.store.list("team_report"))
        ),
    )


async def web_input(
    web: httpx.AsyncClient,
    runtime: Runtime,
    text: str,
    *,
    attachment_ids: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    main_id = runtime.main_session()["id"]
    observed: list[Message] = []

    async def observe_context(session_id: str, messages: list[Message]) -> list[Message]:
        if session_id == main_id:
            observed[:] = messages
        return messages

    runtime.context_hooks.append(observe_context)
    try:
        response = await web.post(
            f"/api/sessions/{main_id}/messages",
            json={"content": text, "attachment_ids": attachment_ids or []},
        )
        assert response.status_code == 200, response.text[:1024]
        receipt = response.json()
        result = await asyncio.wait_for(runtime.wait(receipt["run_id"]), 5)
        await main_idle(runtime)
        return receipt, result
    except ContextLimitError:
        # This final, read-only hook sees the real main context. Report sizes
        # only after failure; never print input, tool arguments or result text.
        minimum = minimum_context(observed)
        print(
            "OWNER_QUESTIONS_CONTEXT_DIAGNOSTIC "
            + json.dumps(
                {
                    "source": "main_after_context_hooks",
                    "skills_context_present": runtime.skills_context is not None,
                    "observed_count": len(observed),
                    "minimum_count": len(minimum),
                    "context_size": context_size(minimum),
                    "recent": [
                        {
                            "role": message.role,
                            "call_id": message.tool_call_id,
                            "call_ids": [call.id for call in message.tool_calls],
                            "preserve": message.preserve_content,
                            "content_chars": len(message.content),
                            "argument_chars": sum(
                                len(call.arguments) for call in message.tool_calls
                            ),
                            "context_size": context_size([message]),
                        }
                        for message in minimum[-30:]
                    ],
                },
                ensure_ascii=False,
            )
        )
        raise
    finally:
        runtime.context_hooks.remove(observe_context)


async def upload(web: httpx.AsyncClient, name: str, text: str) -> str:
    response = await web.post(
        "/api/uploads",
        content=text.encode("utf-8"),
        headers={"x-file-name": name, "content-type": "text/plain"},
    )
    assert response.status_code == 200, response.text[:512]
    return str(response.json()["id"])


async def assign(
    web: httpx.AsyncClient, runtime: Runtime, sources: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for index, (title, facts) in enumerate(sources):
        original = json.dumps(
            {
                "facts": facts,
                "question": "请确认只补出处，还是也补“对照表”？🙂",
                "key": "comparison-scope",
            },
            ensure_ascii=False,
        )
        attachment_id = await upload(web, f"source-{index}.txt", original)
        tasks.append(
            {
                "title": title,
                "worker_name": title + "队友",
                "goal": "核查" + title + "并按明确补充完善草稿",
                "constraints": ["仅使用合成资料，不发布，不重做结果未知的投递"],
                "attachment_id": attachment_id,
            }
        )
    await web_input(
        web,
        runtime,
        _OWNER + json.dumps({"action": "assign", "tasks": tasks}, ensure_ascii=False),
        attachment_ids=[task["attachment_id"] for task in tasks],
    )
    return tasks


def canonical_reply(runtime: Runtime, session_id: str, identifier: str) -> dict[str, Any]:
    message = next(
        item
        for item in reversed(runtime.store.messages(session_id))
        if item.get("tool_call_id") == identifier
    )
    assert message["content_provenance"] == "accepted"
    value = json.loads(message["content"])
    assert isinstance(value, dict)
    return value


def canonical_tool(runtime: Runtime, session_id: str, identifier: str) -> dict[str, Any]:
    value = canonical_reply(runtime, session_id, identifier)
    assert "error" not in value, value
    return value


def question_records(runtime: Runtime) -> list[dict[str, Any]]:
    return runtime.store.list("owner_question")


def assert_registered(
    runtime: Runtime,
    record: dict[str, Any],
    task: dict[str, Any],
    worker: dict[str, Any],
) -> None:
    assert record["worker_id"] == worker["id"] and record["task_id"] == worker["commitment_id"]
    assert record["attachment_ids"] == [task["attachment_id"]]
    assert record["author"]["generation"] == 1
    run = runtime.store.get("run", record["author"]["run_id"])
    assert run["session_id"] == worker["id"] and run["role"] == "worker"
    brief = runtime.store.get("commitment", record["task_id"])["brief"]
    assert brief["goal"] == task["goal"] and brief["constraints"] == task["constraints"]
    assert brief["evidence_refs"] == [{"kind": "attachment", "id": task["attachment_id"]}]
    messages = runtime.store.messages(worker["id"])
    source = next(
        item for item in messages if str(item.get("tool_call_id") or "").endswith("-source")
    )
    assert source["content_provenance"] == "accepted" and source["preserve_content"] is True
    content = json.loads(json.loads(source["content"])["content"])
    assert content["question"] == record["message"]
    registration = next(
        item
        for message in messages
        for item in message.get("tool_calls", [])
        if item["name"] == "send_input"
        and json.loads(item["arguments"]).get("question", {}).get("action") == "register"
    )
    receipt = canonical_tool(runtime, worker["id"], registration["id"])
    assert receipt["accepted"] is True and receipt["question_id"] == record["id"]
    notice = runtime.store.get("message", record["notice"]["input_id"])
    assert notice["source"] == "team-progress:" + worker["id"]
    assert notice["principal"] == "agent:" + worker["id"] and record["message"] in notice["content"]


def supplemented(runtime: Runtime, worker_id: str) -> dict[str, Any]:
    reports = [item for item in runtime.store.list("team_report") if item["agent_id"] == worker_id]
    payloads = [json.loads(item["content"].split("\n", 1)[1]) for item in reports]
    return next(
        json.loads(item["result"])
        for item in payloads
        if item["result"].startswith('{"kind": "supplemented_result"')
    )


def answer_inputs(runtime: Runtime, worker_id: str) -> list[dict[str, Any]]:
    rows = runtime.store.db.execute(
        "SELECT * FROM ingress WHERE session_id=? ORDER BY seq", (worker_id,)
    ).fetchall()
    return [dict(row) for row in rows if _ANSWER_MARKER in json.loads(row["payload"])]


def assert_claim(runtime: Runtime, record: dict[str, Any], body: str, attachment_id: str) -> None:
    assert record["answer"]["owner"]["body"] == body
    assert record["answer"]["owner"]["attachment_ids"] == [attachment_id]
    assert record["answer"]["interpretation"] == _INTERPRETATION
    assert body != _INTERPRETATION
    assert record["delivery"]["state"] == "claimed"
    claim = record["delivery"]["claim"]
    mailbox = answer_inputs(runtime, record["worker_id"])
    assert len(mailbox) == 1 and mailbox[0]["state"] == "delivered"
    assert mailbox[0]["id"] == record["delivery"]["input_id"]
    message = runtime.store.get("message", claim["message_id"])
    assert message["id"] == mailbox[0]["id"] and message["run_id"] == claim["run_id"]
    assert message["seq"] == claim["seq"] and message["input_seq"] == claim["input_seq"]
    assert message["principal"] == "agent:" + record["main_id"]
    assert message[_ANSWER_MARKER]["question_id"] == record["id"]
    assert message["attachment_ids"] == [attachment_id]
    assert "主人原答：\n" + body in message["content"]
    result = supplemented(runtime, record["worker_id"])
    assert result["question_id"] == record["id"] and result["owner_answer"] == body
    assert result["answer_attachment_ids"] == [attachment_id]


async def test_running_worker_records_web_answer_without_mailbox_then_owner_explicitly_dispatches(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    parked, release = asyncio.Event(), asyncio.Event()
    runtime.client_factory = JourneyFactory(parked, release)  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            main_id = runtime.main_session()["id"]
            assert (
                await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            ).status_code == 200
            tasks = await assign(
                web, runtime, [("物理笔记", "物理原记录：自由落体0.45秒，位移约一米；尚缺出处。")]
            )
            await asyncio.wait_for(parked.wait(), 3)
            await main_idle(runtime)
            worker = runtime.team.get(tasks[0]["worker_name"])
            assert worker["status"] == "running" and worker["generation"] == 1
            _, reviewed = await web_input(web, runtime, _REVIEW)
            facts = json.loads(reviewed)
            assert len(facts["items"]) == 1 and facts["open_ids"] == [
                facts["items"][0]["question_id"]
            ]
            selected = facts["items"][0]
            assert selected["worker_id"] == worker["id"] and selected["message"].endswith("🙂")
            assert_registered(runtime, question_records(runtime)[0], tasks[0], worker)
            ordinary, _ = await web_input(web, runtime, "好的")
            denied = canonical_reply(runtime, main_id, "oq-" + ordinary["message"]["id"] + "-write")
            assert "error" in denied and "本轮原文须完整使用" in denied["error"]
            assert question_records(runtime)[0]["answer"] is None
            assert answer_inputs(runtime, worker["id"]) == []
            answer_attachment = await upload(
                web, "owner-web-answer.txt", "出处补充：实验教材第二章；保留原实验数字。"
            )
            original_answer = "只补出处，不补对照表；保留0.45秒这一原始记录🙂。"
            command = f"/回答问题 {selected['question_id']}\n{original_answer}"
            before_worker_messages = deepcopy(runtime.store.messages(worker["id"]))
            before_generation = runtime.team.get(worker["id"])["generation"]
            answered, _ = await web_input(web, runtime, command, attachment_ids=[answer_attachment])
            receipt = canonical_tool(runtime, main_id, "oq-" + answered["message"]["id"] + "-write")
            assert receipt["accepted"] is True and receipt["answer_recorded"] is True
            assert receipt["delivery_state"] == "not_dispatched"
            recorded = question_records(runtime)[0]
            assert recorded["answer"]["owner"]["original_text"] == command
            assert recorded["answer"]["owner"]["input_id"] == answered["message"]["id"]
            assert recorded["answer"]["owner"]["body"] == original_answer
            assert recorded["answer"]["interpretation"] == _INTERPRETATION
            assert recorded["delivery"] is None and runtime.conversation.pending(worker["id"]) == []
            assert answer_inputs(runtime, worker["id"]) == []
            assert runtime.store.messages(worker["id"]) == before_worker_messages
            assert runtime.team.get(worker["id"])["generation"] == before_generation
            assert not release.is_set() and runtime.team.get(worker["id"])["status"] == "running"
            # Let this generation park naturally; explicit dispatch resumes that durable identity.
            release.set()
            await all_idle(runtime)
            dispatch_command = f"/继续问题 {selected['question_id']}"
            dispatched, _ = await web_input(web, runtime, dispatch_command)
            await all_idle(runtime)
            dispatched_receipt = canonical_tool(
                runtime, main_id, "oq-" + dispatched["message"]["id"] + "-write"
            )
            assert (
                dispatched_receipt["accepted"] is True and dispatched_receipt["duplicate"] is False
            )
            delivered = question_records(runtime)[0]
            assert delivered["answer"] == recorded["answer"]
            assert delivered["delivery"]["owner"]["original_text"] == dispatch_command
            assert delivered["delivery"]["owner"]["input_id"] == dispatched["message"]["id"]
            assert_claim(runtime, delivered, original_answer, answer_attachment)
            result = supplemented(runtime, worker["id"])
            assert (
                "自由落体0.45秒" in result["facts"] and "教材第二章" in result["answer_sources"][0]
            )
            assert runtime.team.get(worker["id"])["generation"] == 2
            assert runtime.team.get(worker["id"])["reply_origin"] == worker["reply_origin"]
            _, after = await web_input(web, runtime, _REVIEW)
            reviewed_after = json.loads(after)
            assert (
                reviewed_after["open_ids"] == []
                and reviewed_after["items"][0]["answer_recorded"] is True
            )
            assert reviewed_after["items"][0]["owner_body"] == original_answer
            assert reviewed_after["items"][0]["delivery_state"] == "claimed"
            assert len(runtime.team.list()) == 1
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        release.set()
        await runtime.close()


async def test_two_similar_questions_survive_compaction_reopen_and_cross_channel_answer_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []
    downloads: list[str] = []
    unknown_text = "合成未知边界投递 OWNER-QUESTIONS-JOURNEY，结果未知不得重放。"
    mobile_attachment_body = "活动出处补充：校历中的周五社团课；两名主持人的安排继续保留。"

    async def platform(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        if request.method == "GET":
            assert (
                request.url.host == "93.184.216.34" and request.url.path == "/activity-answer.txt"
            )
            downloads.append(str(request.url))
            return httpx.Response(200, content=mobile_attachment_body.encode("utf-8"))
        assert (
            request.method == "POST"
            and request.url.host == "platform.invalid"
            and request.url.path == "/send_private_msg"
        )
        body = json.loads(request.content)
        requests.append(body)
        text = "".join(part["data"]["text"] for part in body["message"])
        if text == unknown_text:
            raise httpx.ReadTimeout("synthetic unknown once", request=request)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(requests)}}
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", platform)
    runtime = await configured(tmp_path, channels=True)
    runtime.client_factory = JourneyFactory()  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            main_id = runtime.main_session()["id"]
            assert (
                await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            ).status_code == 200
            tasks = await assign(
                web,
                runtime,
                [
                    ("物理笔记", "物理原记录：自由落体0.45秒，位移约一米；尚缺出处。"),
                    ("活动方案", "活动原记录：周五社团课，两名主持人；场地出处待确认。"),
                ],
            )
            await all_idle(runtime)
            workers = [runtime.team.get(task["worker_name"]) for task in tasks]
            assert len({worker["commitment_id"] for worker in workers}) == 2
            _, initial_review = await web_input(web, runtime, _REVIEW)
            initial_facts = json.loads(initial_review)
            assert len(initial_facts["items"]) == 2 and len(initial_facts["open_ids"]) == 2
            selected = {item["worker_id"]: item for item in initial_facts["items"]}
            a, b = (selected[worker["id"]] for worker in workers)
            assert a["question_id"] != b["question_id"] and a["message"] == b["message"]
            assert a["attachment_ids"] == [tasks[0]["attachment_id"]] and b["attachment_ids"] == [
                tasks[1]["attachment_id"]
            ]
            answer_a_attachment = await upload(
                web, "physics-answer.txt", "物理出处：实验教材第二章；不添加对照表。"
            )
            answer_a = "物理笔记只补出处；保留0.45秒，不补对照表🙂。"
            command_a = f"/回答并继续 {a['question_id']}\n{answer_a}"
            answered_a, _ = await web_input(
                web, runtime, command_a, attachment_ids=[answer_a_attachment]
            )
            await all_idle(runtime)
            a_record = next(
                item for item in question_records(runtime) if item["id"] == a["question_id"]
            )
            b_record = next(
                item for item in question_records(runtime) if item["id"] == b["question_id"]
            )
            assert_registered(runtime, a_record, tasks[0], workers[0])
            assert_registered(runtime, b_record, tasks[1], workers[1])
            assert_claim(runtime, a_record, answer_a, answer_a_attachment)
            result_a = supplemented(runtime, workers[0]["id"])
            assert (
                "自由落体0.45秒" in result_a["facts"]
                and "实验教材第二章" in result_a["answer_sources"][0]
            )
            assert a_record["answer"]["owner"]["original_text"] == command_a
            assert b_record["answer"] is None and b_record["delivery"] is None
            assert [runtime.team.get(worker["id"])["generation"] for worker in workers] == [2, 1]
            assert all(
                answer_a not in item["content"] for item in runtime.store.messages(workers[1]["id"])
            )
            unknown_input, _ = await web_input(
                web,
                runtime,
                _OWNER
                + json.dumps(
                    {
                        "action": "unknown",
                        "channel": "onebot",
                        "target": "7",
                        "text": unknown_text,
                        "key": "owner-questions-unknown-once",
                    },
                    ensure_ascii=False,
                ),
            )
            canonical_tool(runtime, main_id, "oq-" + unknown_input["message"]["id"] + "-unknown")
            manager = app.state.channel_application.manager
            deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == unknown_text
            ]
            assert len(deliveries) == 1
            unknown = deepcopy(await terminal(manager, deliveries[0]["id"]))
            assert unknown["state"] == "unknown" and unknown["attempts"] == 1
            notice_ids = [record["notice"]["input_id"] for record in (a_record, b_record)]
            required_seq = max(
                answered_a["message"]["seq"],
                *(runtime.store.get("message", identifier)["seq"] for identifier in notice_ids),
            )
            runtime.config.update(
                {
                    "compaction": {
                        "enabled": True,
                        "mode": "extractive",
                        "trigger_ratio": 0.25,
                        "target_ratio": 0.2,
                    }
                }
            )
            await runtime.reconfigure()
            threshold = int(
                runtime.config.settings.limits.max_context_chars
                * runtime.config.settings.compaction.trigger_ratio
            )
            await web_input(
                web,
                runtime,
                _OWNER
                + json.dumps(
                    {
                        "action": "archive",
                        "background": "归档背景，不是新回答或派工。" * (threshold // 10 + 128),
                    },
                    ensure_ascii=False,
                ),
            )
            active: dict[str, Any] | None = None
            for _ in range(6):
                runtime.compaction.observe(main_id)
                await asyncio.wait_for(runtime.compaction.finish(main_id), 3)
                active = runtime.store.get("compaction", main_id)
                if active and active["covered_seq"] >= required_seq:
                    break
            assert (
                active is not None
                and active["kind"] == "extractive"
                and active["covered_seq"] >= required_seq
            )
            assert manager.delivery(unknown["id"]) == unknown
            instance_id = runtime.store.instance_id
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        await runtime.close()

    reopened_factory = JourneyFactory()
    reopened = Runtime(
        ConfigFile(runtime.config.path), runtime.data_dir, client_factory=reopened_factory
    )  # type: ignore[arg-type]
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    try:
        async with (
            reopened_app.router.lifespan_context(reopened_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=reopened_app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert (
                reopened.store.instance_id == instance_id
                and reopened.main_session()["id"] == main_id
            )
            assert reopened_factory.created == []
            _, after_reopen = await web_input(web, reopened, _REVIEW)
            reopened_facts = json.loads(after_reopen)
            items = {item["question_id"]: item for item in reopened_facts["items"]}
            assert reopened_facts["open_ids"] == [b["question_id"]]
            assert items[a["question_id"]]["owner_body"] == answer_a
            assert items[a["question_id"]]["answer_recorded"] is True
            assert items[b["question_id"]]["answer_recorded"] is False
            assert items[b["question_id"]]["message"] == b["message"]
            assert "worker" not in reopened_factory.created
            # Host-only default-filter proof is not supplied to the frozen reader.
            inspection = reopened_app.state.local_services.task_inspection
            for question, expected in ((a, []), (b, [b["question_id"]])):
                default_open = inspection.read(
                    question["task_id"], section="owner_questions", limit=2
                )
                assert default_open["question_status"] == "open"
                assert default_open["unavailable"] == 0 and default_open["has_more"] is False
                assert [item["question_id"] for item in default_open["items"]] == expected
            answer_b = "活动方案只补出处，继续原活动队友；保留周五与两名主持人🙂。"
            command_b = f"/回答并继续 {items[b['question_id']]['question_id']}\n{answer_b}"
            event = onebot_event(sender=7)
            event["self_id"], event["message_id"] = 123, 9471
            event["message"] = [
                {"type": "text", "data": {"text": command_b}},
                {
                    "type": "file",
                    "data": {
                        "name": "activity-answer.txt",
                        "url": "https://93.184.216.34/activity-answer.txt",
                    },
                },
            ]
            event_body, event_headers = onebot_body(event)
            event_headers["Content-Type"] = "application/json"
            incoming = await web.post(
                "/api/channels/onebot/webhook", content=event_body, headers=event_headers
            )
            assert incoming.status_code == 200, incoming.text[:1024]
            channel_receipt = incoming.json()
            if "run_id" in channel_receipt:
                await asyncio.wait_for(reopened.wait(channel_receipt["run_id"]), 5)
            await all_idle(reopened)
            after_records = {item["id"]: item for item in question_records(reopened)}
            actual_b = after_records[b["question_id"]]
            owner_b = actual_b["answer"]["owner"]
            assert owner_b["original_text"] == command_b and owner_b["body"] == answer_b
            assert (
                owner_b["payload"]["principal"] == "owner"
                and owner_b["payload"]["source"] == "channel:onebot:123"
            )
            assert len(owner_b["attachment_ids"]) == 1 and len(downloads) == 1
            assert_claim(reopened, actual_b, answer_b, owner_b["attachment_ids"][0])
            result_b = supplemented(reopened, workers[1]["id"])
            assert "周五社团课" in result_b["facts"] and result_b["answer_sources"] == [
                mobile_attachment_body
            ]
            assert after_records[a["question_id"]] == a_record
            assert [reopened.team.get(worker["id"])["generation"] for worker in workers] == [2, 2]
            assert all(
                answer_b not in item["content"]
                and owner_b["attachment_ids"][0] not in item.get("attachment_ids", [])
                for item in reopened.store.messages(workers[0]["id"])
            )
            assert all(
                reopened.team.get(worker["id"])["reply_origin"] == worker["reply_origin"]
                for worker in workers
            )
            _, final_review = await web_input(web, reopened, _REVIEW)
            final_facts = json.loads(final_review)
            assert final_facts["open_ids"] == [] and all(
                item["answer_recorded"] for item in final_facts["items"]
            )
            public_b = next(
                item for item in final_facts["items"] if item["question_id"] == b["question_id"]
            )
            assert public_b["owner_source"] == "channel:onebot:123"
            assert public_b["owner_principal"] == "owner"
            assert all(item["delivery_state"] == "claimed" for item in final_facts["items"])
            assert reopened_app.state.channel_application.manager.delivery(unknown["id"]) == unknown
            saved_records = deepcopy(question_records(reopened))
            saved_agents = [deepcopy(reopened.team.get(worker["id"])) for worker in workers]
            saved_mailboxes = [answer_inputs(reopened, worker["id"]) for worker in workers]
            before_ingress = reopened.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
            before_inbound = [
                dict(row)
                for row in reopened.store.db.execute(
                    "SELECT * FROM channel_inbound ORDER BY id"
                ).fetchall()
            ]
            await asyncio.wait_for(reopened_app.state.close(), 3)
    finally:
        await reopened.close()

    idle_factory = IdleFactory()
    replayed = Runtime(
        ConfigFile(runtime.config.path), runtime.data_dir, client_factory=idle_factory
    )  # type: ignore[arg-type]
    replayed_app = create_app(replayed.config, replayed.data_dir, replayed)
    try:
        async with (
            replayed_app.router.lifespan_context(replayed_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=replayed_app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert replayed.store.instance_id == instance_id and idle_factory.created == []
            duplicate = await web.post(
                "/api/channels/onebot/webhook", content=event_body, headers=event_headers
            )
            assert duplicate.status_code == 200 and duplicate.json() == channel_receipt
            assert idle_factory.created == [] and not replayed.tasks
            assert question_records(replayed) == saved_records
            assert [replayed.team.get(worker["id"]) for worker in workers] == saved_agents
            assert [answer_inputs(replayed, worker["id"]) for worker in workers] == saved_mailboxes
            assert (
                replayed.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
                == before_ingress
            )
            assert [
                dict(row)
                for row in replayed.store.db.execute(
                    "SELECT * FROM channel_inbound ORDER BY id"
                ).fetchall()
            ] == before_inbound
            assert replayed_app.state.channel_application.manager.delivery(unknown["id"]) == unknown
            assert len(downloads) == 1
            assert (
                sum(
                    "".join(part["data"]["text"] for part in item["message"]) == unknown_text
                    for item in requests
                )
                == 1
            )
            await asyncio.wait_for(replayed_app.state.close(), 3)
    finally:
        await replayed.close()
