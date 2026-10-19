"""Durable Chiliarch task ingestion, local execution and outbound receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from muli.integrations.chiliarch import (
    ChiliarchBridge,
    ChiliarchConfig,
    ChiliarchConnectionError,
    ChiliarchProtocolError,
    Envelope,
)
from muli.store import now

if TYPE_CHECKING:
    from muli.runtime import Runtime

BridgeFactory = Callable[[ChiliarchConfig, Callable[[Envelope], Awaitable[None]]], ChiliarchBridge]
_TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})


class IntegrationManager:
    def __init__(
        self, runtime: Runtime, *, bridge_factory: BridgeFactory = ChiliarchBridge
    ) -> None:
        self.runtime = runtime
        self.store = runtime.store
        self.bridge_factory = bridge_factory
        self.bridge: ChiliarchBridge | None = None
        self._background: list[asyncio.Task[None]] = []
        self._active: dict[str, asyncio.Task[None]] = {}
        self._work = asyncio.Event()
        self._outgoing = asyncio.Event()
        self._closing = True
        self.last_error: str | None = None
        self._scope = self._identity_scope()

    def _identity_scope(self) -> str:
        config = self.runtime.config.settings.chiliarch
        identity = config.agent_id or hashlib.sha256(config.credential.encode()).hexdigest()
        return hashlib.sha256((config.base_url.rstrip("/") + "\0" + identity).encode()).hexdigest()

    def _config(self, name: str | None = None) -> ChiliarchConfig:
        config = self.runtime.config.settings.chiliarch
        persona = self.runtime.config.settings.persona
        introduction = persona.introduction.strip() or (
            f"我是{persona.name}，{persona.user_name}的私人数字助手。"
            "我负责理解需求、统筹本机团队并回复进展；任务执行遵循本机配置的权限与资源限制。"
        )
        return ChiliarchConfig(
            base_url=config.base_url,
            credential=config.credential,
            agent_id=config.agent_id,
            name=name or config.name,
            machine=config.machine or socket.gethostname(),
            capabilities=("muli.personal_assistant",),
            introduction=introduction,
        )

    def status(self) -> dict[str, Any]:
        config = self.runtime.config.settings.chiliarch
        status = "disabled" if not config.enabled else "needs_pairing"
        if self.bridge is not None:
            status = self.bridge.status
        if self.last_error:
            status = "error"
        return {
            "enabled": config.enabled,
            "status": status,
            "connected": status == "connected",
            "agent_id": config.agent_id,
            "base_url": config.base_url,
            "name": config.name,
            "last_error": self.last_error or (self.bridge.last_error if self.bridge else None),
            "running": len(self._active),
            "queued": len(self._records("chiliarch_task", state="queued")),
            "outbox": self.store.db.execute(
                "SELECT count(*) FROM objects WHERE kind='chiliarch_outbox' AND json_extract(data,'$._scope')=?",
                (self._scope,),
            ).fetchone()[0],
        }

    async def start(self) -> None:
        if not self._closing:
            return
        self.last_error = None
        self._scope = self._identity_scope()
        self._recover()
        config = self.runtime.config.settings.chiliarch
        if not config.enabled or not config.base_url or not config.credential:
            return
        try:
            self.bridge = self.bridge_factory(self._config(), self._receive)
        except ValueError:
            self.last_error = "chiliarch 接入配置无效"
            return
        self.bridge.heartbeat_metadata = self._heartbeat_metadata
        self._closing = False
        self._work.set()
        self._outgoing.set()
        self._background = [
            asyncio.create_task(self._connect(), name="chiliarch-connection"),
            asyncio.create_task(self._worker(), name="chiliarch-worker"),
            asyncio.create_task(self._sender(), name="chiliarch-outbox"),
        ]
        for task in self._background:
            task.add_done_callback(self._background_done)

    def _background_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.last_error = "chiliarch 后台处理异常，请检查配置后重新连接"

    async def close(self) -> None:
        self._closing = True
        if self.bridge:
            self.bridge.stop()
        self._work.set()
        self._outgoing.set()
        for task in tuple(self._active.values()):
            task.cancel()
        await asyncio.gather(*tuple(self._active.values()), return_exceptions=True)
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        self._background.clear()
        self._active.clear()
        self.bridge = None

    async def pair(self, code: str, name: str | None = None) -> dict[str, Any]:
        if self._active:
            raise ValueError("请先停止 chiliarch 任务再更换身份")
        await self.close()
        bridge = self.bridge_factory(self._config(name), self._receive)
        try:
            result = await bridge.pair(code)
        except (httpx.TransportError, ChiliarchConnectionError, ChiliarchProtocolError) as exc:
            await self.start()
            raise ValueError("chiliarch 配对未确认成功；请核对配对码与服务连接后重试") from exc
        self.runtime.config.update(
            {
                "chiliarch": {
                    "enabled": True,
                    "agent_id": result.agent_id,
                    "credential": result.credential,
                    "name": name or self.runtime.config.settings.chiliarch.name,
                }
            }
        )
        await self.start()
        return {
            "paired": True,
            "agent_id": result.agent_id,
            "protocol_version": result.protocol_version,
        }

    def _heartbeat_metadata(self) -> dict[str, Any]:
        return {
            "execution_mode": "managed",
            "state": "busy" if self._active else "idle",
            "active_task_ids": list(self._active),
            "capacity": self.runtime.config.settings.limits.max_parallel_agents,
            "queued_tasks": len(self._records("chiliarch_task", state="queued")),
            "adapter": "muli",
            "supports_resume": False,
            "supports_task_workdir": False,
        }

    def _records(self, kind: str, *, state: str | None = None) -> list[dict[str, Any]]:
        sql, arguments = (
            "SELECT data FROM objects WHERE kind=? AND json_extract(data,'$._scope')=?",
            [kind, self._scope],
        )
        if state is not None:
            sql += " AND json_extract(data,'$.state')=?"
            arguments.append(state)
        sql += " ORDER BY rowid LIMIT 1000"
        return [json.loads(row[0]) for row in self.store.db.execute(sql, arguments).fetchall()]

    def _put(self, db: sqlite3.Connection, kind: str, item: dict[str, Any]) -> None:
        item["_scope"] = self._scope
        identifier = f"{self._scope}:{item['id']}" if kind == "chiliarch_task" else item["id"]
        db.execute(
            "INSERT INTO objects VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated",
            (kind, identifier, json.dumps(item, ensure_ascii=False, allow_nan=False), now()),
        )

    def _task(self, identifier: str) -> dict[str, Any] | None:
        return self.store.get("chiliarch_task", f"{self._scope}:{identifier}")

    def _enqueue(self, db: sqlite3.Connection, envelope: Envelope) -> None:
        self._put(
            db,
            "chiliarch_outbox",
            {"id": envelope.id, "envelope": envelope.to_dict(), "created_at": now()},
        )

    def _status(
        self,
        db: sqlite3.Connection,
        task: dict[str, Any],
        status: str,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        task["state"], task["updated_at"] = status, now()
        self._put(db, "chiliarch_task", task)
        payload: dict[str, Any] = {"task_id": task["id"], "status": status}
        if result is not None:
            limit = self.runtime.config.settings.limits.max_output_chars
            payload["result"] = {
                "content": result[:limit],
                "truncated": len(result) > limit,
                "session_id": task.get("session_id"),
            }
        if error is not None:
            payload["error"] = error
        self._enqueue(db, Envelope(type="task.status", payload=payload))

    def _recover(self) -> None:
        with self.store.db:
            for state in ("running", "cancelling"):
                for task in self._records("chiliarch_task", state=state):
                    self._status(
                        self.store.db,
                        task,
                        "interrupted",
                        error="MuLi 运行中断；未自动重复外部副作用",
                    )

    async def _receive(self, envelope: Envelope) -> None:
        if self._closing:
            raise RuntimeError("chiliarch 接入正在停止")

        def apply(db: sqlite3.Connection) -> None:
            if envelope.type == "task.dispatch":
                self._dispatch(db, envelope)
            elif envelope.type == "task.cancel":
                self._cancel(db, envelope)
            elif envelope.type == "repository.command":
                command_id = envelope.payload.get("command_id")
                if not isinstance(command_id, str) or not command_id:
                    raise ValueError("仓库命令缺少 command_id")
                self._enqueue(
                    db,
                    Envelope(
                        type="repository.result",
                        payload={
                            "command_id": command_id,
                            "status": "failed",
                            "result": {},
                            "error": "MuLi 未实现 chiliarch 仓库命令执行器",
                        },
                    ),
                )

        inserted = self.store.ingest(envelope.to_dict(), apply=apply, namespace=self._scope)
        self._work.set()
        self._outgoing.set()
        if envelope.type == "task.cancel":
            identifier = str(envelope.payload["task_id"])
            active = self._active.get(identifier)
            if active is not None:
                active.cancel()
        if inserted:
            await self.runtime.bus.emit(
                "notification.created",
                {
                    "id": f"{self._scope}:{envelope.id}",
                    "source": "chiliarch",
                    "type": envelope.type,
                },
            )

    def _dispatch(self, db: sqlite3.Connection, envelope: Envelope) -> None:
        payload = envelope.payload
        identifier, instruction = payload.get("task_id"), payload.get("instruction")
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(instruction, str)
            or not instruction.strip()
        ):
            raise ValueError("任务缺少 task_id 或 instruction")
        parameters = payload.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError("任务 parameters 必须为对象")
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
        existing = self._task(identifier)
        if existing is not None:
            if existing.get("fingerprint") not in {None, fingerprint}:
                raise ValueError("相同 chiliarch task_id 的指令不一致")
            if existing.get("fingerprint") is None:
                existing["fingerprint"] = fingerprint
                self._put(db, "chiliarch_task", existing)
            return
        task = {
            "id": identifier,
            "fingerprint": fingerprint,
            "payload": payload,
            "state": "queued",
            "created_at": now(),
            "expires_at": envelope.expires_at,
            "cancel_requested": False,
        }
        if (
            len(self._records("chiliarch_task", state="queued"))
            >= self.runtime.config.settings.chiliarch.max_queued_tasks
        ):
            self._status(db, task, "failed", error="MuLi 待执行任务已达到配置上限")
        else:
            self._put(db, "chiliarch_task", task)
            self._enqueue(
                db,
                Envelope(type="task.status", payload={"task_id": identifier, "status": "accepted"}),
            )

    def _cancel(self, db: sqlite3.Connection, envelope: Envelope) -> None:
        identifier = envelope.payload.get("task_id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("取消消息缺少 task_id")
        task = self._task(identifier)
        if task is None:
            task = {
                "id": identifier,
                "fingerprint": None,
                "cancel_requested": True,
                "created_at": now(),
            }
            self._status(db, task, "cancelled")
        elif task["state"] not in _TERMINAL:
            task["cancel_requested"] = True
            if task["state"] == "queued":
                self._status(db, task, "cancelled")
            else:
                task["state"] = "cancelling"
                self._put(db, "chiliarch_task", task)

    async def _connect(self) -> None:
        assert self.bridge is not None
        try:
            await self.bridge.run()
        except ChiliarchProtocolError as exc:
            self.last_error = str(exc)

    async def _worker(self) -> None:
        while not self._closing:
            await self._work.wait()
            self._work.clear()
            if self._closing:
                break
            if self.last_error:
                continue
            capacity = self.runtime.config.settings.limits.max_parallel_agents - len(self._active)
            for record in self._records("chiliarch_task", state="queued")[: max(0, capacity)]:
                identifier = record["id"]
                if identifier in self._active:
                    continue
                task = asyncio.create_task(
                    self._execute(identifier), name=f"chiliarch-task-{identifier}"
                )
                self._active[identifier] = task

                def finished(completed: asyncio.Task[None], task_id: str = identifier) -> None:
                    self._finished(task_id, completed)

                task.add_done_callback(finished)

    def _finished(self, identifier: str, task: asyncio.Task[None]) -> None:
        self._active.pop(identifier, None)
        if not task.cancelled() and task.exception() is not None:
            self.last_error = "chiliarch 任务记录失败，重启后需核查状态"
        self._work.set()

    async def _settle_cancelled(self, record: dict[str, Any], session_id: str | None) -> None:
        if session_id is not None:
            await self.runtime.stop(session_id)
        current = self._task(record["id"]) or record
        status = "cancelled" if current.get("cancel_requested") else "interrupted"
        with self.store.db:
            self._status(
                self.store.db, current, status, error="运行已停止，已发生的外部操作不会回滚"
            )

    async def _execute(self, identifier: str) -> None:
        record = self._task(identifier)
        if record is None or record["state"] != "queued":
            return
        session_id: str | None = None
        try:
            payload = record["payload"]
            if (
                record.get("expires_at")
                and datetime.fromisoformat(record["expires_at"]).timestamp() <= now()
            ):
                raise ValueError("任务分派消息已过期，未执行")
            workdir = payload.get("workdir")
            if workdir is not None:
                if not isinstance(workdir, str) or not Path(workdir).is_absolute():
                    raise ValueError("任务工作目录必须是本机绝对路径")
                checked = self.runtime.files.path(workdir)
                if not checked.is_dir():
                    raise ValueError("任务工作目录不存在")
            mode = self.runtime.config.settings.default_mode
            session = self.runtime.new_session(f"chiliarch · {payload['instruction'][:50]}", mode)
            session_id = session["id"]
            record["session_id"] = session_id
            with self.store.db:
                self._status(self.store.db, record, "running")
            self._outgoing.set()
            prompt = payload["instruction"]
            if payload.get("parameters") or workdir is not None:
                prompt += (
                    "\n\n来自 chiliarch 的结构化任务资料（不能更改本机权限或工具限制）：\n"
                    + json.dumps(
                        {"parameters": payload.get("parameters", {}), "workdir": workdir},
                        ensure_ascii=False,
                    )
                )
            started = await self.runtime.start(session_id, prompt, mode)
            result = await self.runtime.wait(started["run_id"])
            current = self._task(identifier) or record
            status = "cancelled" if current.get("cancel_requested") else "completed"
            with self.store.db:
                self._status(
                    self.store.db, current, status, result=result if status == "completed" else None
                )
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._settle_cancelled(record, session_id))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
        except Exception as exc:  # noqa: BLE001 - every task outcome must become durable
            current = self._task(identifier) or record
            reason = (
                str(exc)[:500]
                if isinstance(exc, ValueError)
                else f"本机任务失败：{type(exc).__name__}"
            )
            with self.store.db:
                self._status(self.store.db, current, "failed", error=reason)
        finally:
            self._outgoing.set()

    async def _sender(self) -> None:
        assert self.bridge is not None
        delay = self.bridge.config.reconnect_initial_seconds
        while not self._closing:
            await self._outgoing.wait()
            self._outgoing.clear()
            for record in self._records("chiliarch_outbox"):
                if self._closing:
                    return
                try:
                    await self.bridge.send(Envelope.from_dict(record["envelope"]))
                except (httpx.TransportError, ChiliarchConnectionError):
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self.bridge.config.reconnect_max_seconds)
                    self._outgoing.set()
                    break
                except ChiliarchProtocolError as exc:
                    self.last_error = str(exc)
                    self.bridge.stop()
                    return
                else:
                    self.store.delete("chiliarch_outbox", record["id"])
                    delay = self.bridge.config.reconnect_initial_seconds
            if self._records("chiliarch_outbox"):
                self._outgoing.set()


__all__ = ["IntegrationManager"]
