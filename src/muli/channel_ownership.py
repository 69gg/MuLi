"""Host-owned channel assignments, sharing the existing agent and input journal."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from muli.format_admission import require_channel_ownership_format
from muli.reply_origin import authorize_route, channel_account
from muli.store import SQLITE_MAX_INTEGER, now

if TYPE_CHECKING:
    from muli.runtime import Runtime


class ChannelOwnership:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    @staticmethod
    def identifier(account: str, owner: str | None) -> str:
        return hashlib.sha256(
            json.dumps(["onebot", account, owner], separators=(",", ":")).encode()
        ).hexdigest()

    def _profile(self) -> tuple[str, str]:
        profile = self.runtime.config.settings.channels.onebot
        if not profile.enabled or not profile.owner_ids:
            raise PermissionError("请先启用并绑定 OneBot 主人私聊")
        account = channel_account("onebot", profile)
        fingerprint = hashlib.sha256(profile.model_dump_json().encode("utf-8")).hexdigest()
        return account, fingerprint

    def _get(self, identifier: str) -> dict[str, Any] | None:
        return self.runtime.store.bounded_object(
            "channel_ownership",
            identifier,
            max_bytes=self.runtime.config.settings.limits.max_output_chars * 4,
        )

    def for_agent(self, session_id: str) -> dict[str, Any] | None:
        row = self.runtime.store.db.execute(
            "SELECT id FROM objects WHERE kind='channel_ownership' "
            "AND json_extract(data,'$.agent_id')=? "
            "AND json_extract(data,'$.state') IN ('active','return_requested') LIMIT 2",
            (session_id,),
        ).fetchall()
        if len(row) > 1:
            raise PermissionError("队友存在冲突的渠道归属，须由主助手收回")
        return self._get(row[0][0]) if row else None

    def status(self) -> list[dict[str, Any]]:
        limit = self.runtime.config.settings.limits.max_search_results
        rows = self.runtime.store.db.execute(
            "SELECT id FROM objects WHERE kind='channel_ownership' ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = [item for row in rows if (item := self._get(row[0])) is not None]
        if (
            len(json.dumps(result, ensure_ascii=False))
            > self.runtime.config.settings.limits.max_output_chars
        ):
            raise ValueError("渠道归属目录超过当前输出预算")
        return result

    def _scope(self, args: dict[str, Any]) -> tuple[str, str | None, str]:
        if args.get("channel") != "onebot" or args.get("scope") not in {"account", "private"}:
            raise ValueError("本版仅支持 OneBot 已绑定主人私聊的接入级或指定私聊归属")
        account, fingerprint = self._profile()
        owner = args.get("owner_id")
        if args["scope"] == "private":
            if (
                not isinstance(owner, str)
                or owner not in self.runtime.config.settings.channels.onebot.owner_ids
            ):
                raise PermissionError("私聊归属需要当前已绑定的准确 owner_id")
        elif owner is not None:
            raise ValueError("接入级归属不能同时指定 owner_id")
        return account, owner, fingerprint

    def control(self, caller: str, args: dict[str, Any]) -> dict[str, Any]:
        runtime = self.runtime
        runtime.store.revalidate_format()
        require_channel_ownership_format(runtime.store.format_state)
        account, owner, fingerprint = self._scope(args)
        identifier = self.identifier(account, owner)
        existing = self._get(identifier)
        action = args["action"]
        main_id = runtime.store.existing_main_session_id()
        if main_id is None:
            raise PermissionError("主助手不存在")
        if caller != main_id:
            if action != "return" or existing is None or existing.get("agent_id") != caller:
                raise PermissionError("队友只能归还自己的当前渠道范围")
            self.guard(self.proof(existing), session_id=caller)
        elif action == "return":
            raise ValueError("主助手请使用 reclaim 收回；return 仅供当前受托队友使用")
        if action == "assign":
            agent = runtime.team.get(args.get("agent_id", ""))
            session = runtime.store.get("session", agent["session_id"])
            if (
                agent.get("parent_id") != main_id
                or agent.get("role") == "reviewer"
                or agent.get("status") != "completed"
                or agent.get("commitment_id") is not None
                or session is None
                or session.get("principal") != "owner"
                or session.get("mode") != agent.get("mode")
                or agent.get("mode") != runtime.main_session()["mode"]
                or agent["id"] in runtime.tasks
                or runtime.store.pending_inputs(agent["id"])
                or runtime.team._active.get(agent["id"]) is not None
            ):
                raise PermissionError("只能选择已完成、空闲、非任务绑定的直属执行队友")
            assigned = self.for_agent(agent["id"])
            if assigned is not None and assigned["id"] != identifier:
                raise PermissionError("该队友已接管其他范围，请先归还")
            if existing is not None and existing["state"] != "main":
                raise PermissionError("该范围已被接管，请先明确收回，不隐式替换")
            value = {
                "id": identifier,
                "channel": "onebot",
                "account_id": account,
                "owner_id": owner,
                "main_id": main_id,
                "agent_id": agent["id"],
                "state": "active",
                "epoch": (existing["epoch"] if existing else 0) + 1,
                "profile": fingerprint,
                "main_mode": runtime.main_session()["mode"],
                "agent_mode": agent["mode"],
                "updated_at": now(),
            }
            if value["epoch"] > SQLITE_MAX_INTEGER:
                raise ValueError("渠道归属代次已耗尽")
            with runtime.store.db:
                runtime.store._put("channel_ownership", value)
            return value
        if existing is None or existing["state"] == "main":
            fallback = self._get(self.identifier(account, None)) if owner is not None else None
            if action != "reclaim" or fallback is None or fallback["state"] == "main":
                raise ValueError("该范围当前没有受托队友")
            # A private main pin takes precedence over an account assignment.
            existing = {**fallback, "id": identifier, "owner_id": owner, "epoch": 0}
        if action == "request_return":
            if existing["state"] == "return_requested" and existing.get("return_input_id"):
                return existing
            value = {**existing, "state": "return_requested", "updated_at": now()}
            agent = runtime.team.get(value["agent_id"])
            payload = runtime.conversation.payload(
                agent["id"],
                "主助手请求归还当前渠道范围。这是宿主控制通知，不是主人新指令；"
                "本轮只能读取现状或用 channel_delegate(action=return) 归还，不得执行其他写入或外发。",
                source="channel-ownership",
                principal="agent:" + main_id,
                mode=agent["mode"],
                event_id=f"return:{value['id']}:{value['epoch']}",
            )
            payload["channel_ownership"] = self.proof(value)
            payload["channel_ownership_control"] = True
            value["return_input_id"] = payload["id"]
            with runtime.store.db:
                runtime.store._enqueue_input(
                    payload,
                    max_pending=runtime.config.settings.limits.max_pending_runs,
                    objects=[
                        ("channel_ownership", value),
                        (
                            "agent",
                            {
                                **agent,
                                "status": "running" if agent["id"] in runtime.tasks else "queued",
                            },
                        ),
                    ],
                )
            return value
        if action not in {"return", "reclaim"}:
            raise ValueError("未知渠道归属动作")
        with runtime.store.db:
            value = self._revoke(existing)
            if owner is not None:
                runtime.store.db.execute(
                    "UPDATE ingress SET state='cancelled' WHERE state='pending' "
                    "AND json_extract(payload,'$.route.name')='onebot' "
                    "AND json_extract(payload,'$.route.account_id')=? "
                    "AND json_extract(payload,'$.route.sender_id')=? "
                    "AND json_extract(payload,'$.channel_ownership.main_id')=?",
                    (account, owner, main_id),
                )
        return value

    def _revoke(self, value: dict[str, Any]) -> dict[str, Any]:
        if type(value.get("epoch")) is not int or value["epoch"] >= SQLITE_MAX_INTEGER:
            raise PermissionError("渠道归属代次无效")
        updated = {**value, "state": "main", "epoch": value["epoch"] + 1, "updated_at": now()}
        self.runtime.store._put("channel_ownership", updated)
        # Pending inputs remain in their original journal and are never replayed
        # as new main requests. Claimed effects cannot be guessed or retried.
        self.runtime.store.db.execute(
            "UPDATE ingress SET state='cancelled' WHERE session_id=? AND state='pending' "
            "AND json_extract(payload,'$.channel_ownership.id')=?",
            (value["agent_id"], value["id"]),
        )
        session_id = value["agent_id"]
        if (run_id := self.runtime.run_ids.get(session_id)) and self.affects_running(value):
            for identifier in self.runtime.permissions.pending_for(session_id, run_id):
                approval = self.runtime.store.get("approval", identifier)
                future = self.runtime.permissions.pending.get(identifier)
                if (
                    approval is None
                    or future is None
                    or future.done()
                    or approval.get("status") != "pending"
                ):
                    continue
                self.runtime.store._event(
                    "approval.resolved",
                    {"id": identifier, "decision": "cancelled"},
                    session_id,
                    objects=[("approval", {**approval, "status": "cancelled"})],
                )

                def cancel_committed(
                    ticket_id: str,
                    waiter: asyncio.Future[bool],
                ) -> None:
                    # Runs only after the caller's synchronous transaction has
                    # exited. A rollback leaves the original future untouched.
                    if waiter.done() or self.runtime._closing:
                        return
                    current = self.runtime.store.get("approval", ticket_id)
                    if current is not None and current.get("status") == "cancelled":
                        waiter.cancel()

                asyncio.get_running_loop().call_soon(cancel_committed, identifier, future)
        return updated

    def affects_running(self, value: dict[str, Any]) -> bool:
        session_id = value["agent_id"]
        run_id = self.runtime.run_ids.get(session_id)
        if run_id is None or session_id not in self.runtime.tasks:
            return False
        row = self.runtime.store.db.execute(
            "SELECT json_extract(o.data,'$.channel_ownership.id'),"
            "json_extract(o.data,'$.route.account_id'),json_extract(o.data,'$.route.sender_id') "
            "FROM message_order m JOIN objects o ON o.kind='message' AND o.id=m.message_id "
            "WHERE m.session_id=? AND json_extract(o.data,'$.run_id')=? "
            "AND json_extract(o.data,'$.role')='user' ORDER BY m.seq DESC LIMIT 1",
            (session_id, run_id),
        ).fetchone()
        return bool(
            row is not None
            and (
                row[0] == value["id"]
                or (
                    value["owner_id"] is not None
                    and row[0] is not None
                    and row[1] == value["account_id"]
                    and row[2] == value["owner_id"]
                )
            )
        )

    def mode_changed(self, session_id: str, previous: str, current: str) -> None:
        if previous == current:
            return
        rows = self.runtime.store.db.execute(
            "SELECT id FROM objects WHERE kind='channel_ownership' "
            "AND json_extract(data,'$.state') IN ('active','return_requested') "
            "AND (json_extract(data,'$.agent_id')=? OR json_extract(data,'$.main_id')=?)",
            (session_id, session_id),
        ).fetchall()
        for row in rows:
            if value := self._get(row[0]):
                self._revoke(value)

    def reconcile(self) -> None:
        rows = self.runtime.store.db.execute(
            "SELECT id FROM objects WHERE kind='channel_ownership' "
            "AND json_extract(data,'$.state') IN ('active','return_requested')"
        ).fetchall()
        for row in rows:
            value = self._get(row[0])
            if value is None:
                continue
            try:
                self.guard(self.proof(value), session_id=value["agent_id"])
            except (ValueError, PermissionError):
                with self.runtime.store.db:
                    self._revoke(value)

    @staticmethod
    def proof(value: dict[str, Any]) -> dict[str, Any]:
        return {key: value[key] for key in ("id", "epoch", "main_id", "agent_id")}

    def returned(self, proof: dict[str, Any] | None, result: Any) -> bool:
        if proof is None or not isinstance(result, dict):
            return False
        current = self._get(proof["id"])
        return bool(
            current is not None
            and current == result
            and current["state"] == "main"
            and current["epoch"] == proof["epoch"] + 1
            and current["agent_id"] == proof["agent_id"]
            and current["main_id"] == proof["main_id"]
        )

    def destination(self, route: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        runtime = self.runtime
        main = runtime.main_session()["id"]
        if route.get("name") != "onebot" or route.get("kind") != "private":
            return main, None
        account, _ = self._profile()
        authorize_route(
            "onebot",
            route,
            account_id=account,
            owner_ids=runtime.config.settings.channels.onebot.owner_ids,
            allowed_groups=[],
            outbound=False,
        )
        specific = self._get(self.identifier(account, route["sender_id"]))
        value = specific or self._get(self.identifier(account, None))
        if value is None or value["state"] == "main":
            return main, None
        proof = self.proof(value)
        self.guard(proof, session_id=value["agent_id"])
        return value["agent_id"], proof

    def guard(self, proof: object, *, session_id: str) -> None:
        if (
            not isinstance(proof, dict)
            or set(proof)
            not in (
                {"id", "epoch", "main_id", "agent_id"},
                {"id", "epoch", "main_id", "agent_id", "input_id", "run_id"},
            )
            or type(proof.get("epoch")) is not int
            or not 1 <= proof["epoch"] <= SQLITE_MAX_INTEGER
            or any(
                not isinstance(proof.get(key), str) or not proof[key]
                for key in ("id", "main_id", "agent_id")
            )
        ):
            raise PermissionError("渠道归属证明无效")
        value = self._get(proof["id"])
        if (
            value is None
            or value.get("state") not in {"active", "return_requested"}
            or any(self.proof(value)[key] != proof[key] for key in self.proof(value))
            or value.get("agent_id") != session_id
            or value.get("main_id") != self.runtime.store.existing_main_session_id()
        ):
            raise PermissionError("渠道归属已收回或被替换，旧轮不得继续执行")
        account, fingerprint = self._profile()
        session = self.runtime.store.get("session", session_id)
        agent = self.runtime.store.get("agent", session_id)
        if (
            account != value.get("account_id")
            or fingerprint != value.get("profile")
            or self.runtime.main_session()["mode"] != value.get("main_mode")
            or session is None
            or agent is None
            or session.get("principal") != "owner"
            or session.get("mode") != value.get("agent_mode")
            or agent.get("mode") != value.get("agent_mode")
            or agent.get("parent_id") != value["main_id"]
        ):
            raise PermissionError("渠道账号、主人或执行权限已改变")
        if "input_id" in proof:
            if any(
                not isinstance(proof[key], str) or not proof[key] for key in ("input_id", "run_id")
            ):
                raise PermissionError("渠道归属缺少本批真实运行引用")
            row = self.runtime.store.db.execute(
                "SELECT json_extract(data,'$.channel_ownership'),json_extract(data,'$.route.sender_id'),"
                "json_extract(data,'$.principal'),json_extract(data,'$.source'),"
                "json_extract(data,'$.channel_ownership_control'),json_type(data,'$.channel_ownership_control') FROM objects "
                "WHERE kind='message' AND id=? AND json_extract(data,'$.session_id')=? "
                "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.role')='user'",
                (proof["input_id"], session_id, proof["run_id"]),
            ).fetchone()
            if row is None or row[0] is None or json.loads(row[0]) != self.proof(value):
                raise PermissionError("渠道归属不属于该运行实际接纳的输入")
            if row[4] is not None:
                if (
                    row[4] != 1
                    or row[5] != "true"
                    or row[2] != "agent:" + value["main_id"]
                    or row[3] != "channel-ownership"
                    or value["state"] != "return_requested"
                    or value.get("return_input_id") != proof["input_id"]
                ):
                    raise PermissionError("归还通知不是该范围本代宿主请求")
                return
            if row[2] != "owner":
                raise PermissionError("渠道执行不是准确主人输入")
            specific = self._get(self.identifier(account, row[1]))
            selected = specific or self._get(self.identifier(account, None))
            if selected is None or selected["id"] != value["id"] or selected["state"] == "main":
                raise PermissionError("该私聊的有效归属已经改变")

    def return_request(self, proof: dict[str, Any] | None) -> bool:
        if proof is None or "input_id" not in proof:
            return False
        row = self.runtime.store.db.execute(
            "SELECT json_type(data,'$.channel_ownership_control') FROM objects "
            "WHERE kind='message' AND id=?",
            (proof["input_id"],),
        ).fetchone()
        return row is not None and row[0] == "true"

    def guard_call(
        self, proof: dict[str, Any] | None, name: str, args: dict[str, Any], risk: str
    ) -> None:
        if proof is None:
            return
        self.guard(proof, session_id=proof["agent_id"])
        if self.return_request(proof) and risk != "read":
            value = self._get(proof["id"])
            if not (
                name == "channel_delegate"
                and args.get("action") == "return"
                and args.get("channel") == "onebot"
                and value is not None
                and args.get("scope") == ("account" if value["owner_id"] is None else "private")
                and args.get("owner_id") == value["owner_id"]
                and "agent_id" not in args
            ):
                raise PermissionError("归还通知只允许读取与归还准确范围，不授予新操作权限")

    def guard_destination(self, proof: object, route: Mapping[str, Any]) -> None:
        if not isinstance(proof, dict) or not isinstance(proof.get("agent_id"), str):
            raise PermissionError("渠道归属投递证明无效")
        self.guard(proof, session_id=proof["agent_id"])
        if "input_id" not in proof or self.return_request(proof):
            raise PermissionError("外发必须有本批实际主人私聊来源")
        row = self.runtime.store.db.execute(
            "SELECT json_extract(data,'$.route') FROM objects WHERE kind='message' AND id=?",
            (proof["input_id"],),
        ).fetchone()
        original = json.loads(row[0]) if row is not None and row[0] is not None else None
        if not isinstance(original, dict) or any(
            route.get(key) != original.get(key)
            for key in ("name", "account_id", "kind", "target", "sender_id")
        ):
            raise PermissionError("受托队友只能外发到本批实际接纳的私聊，不能换收件人或渠道")

    def current_proof(self, session_id: str, run_id: str | None) -> dict[str, Any] | None:
        if run_id is None:
            return None
        row = self.runtime.store.db.execute(
            "SELECT o.id,json_extract(o.data,'$.channel_ownership') FROM message_order m "
            "JOIN objects o ON o.kind='message' AND o.id=m.message_id "
            "WHERE m.session_id=? AND json_extract(o.data,'$.role')='user' "
            "AND json_extract(o.data,'$.run_id')=? "
            "ORDER BY m.seq DESC LIMIT 1",
            (session_id, run_id),
        ).fetchone()
        if row is None or row[1] is None:
            agent = self.runtime.store.get("agent", session_id)
            if agent is not None and agent.get("run_id") == run_id and "channel_ownership" in agent:
                raise PermissionError("受托运行缺少当前实际接纳的渠道输入，不得借用旧来源")
            return None
        proof = {**json.loads(row[1]), "input_id": row[0], "run_id": run_id}
        self.guard(proof, session_id=session_id)
        return proof
