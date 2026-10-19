"""Telegram、OneBot/NapCat 和 QQ 官方 HTTP 协议。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import time
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import quote

import httpx

from .crypto import qq_key, sign_qq, verify_qq
from .http import ChannelHTTP
from .types import ChannelError, ChannelProfile, FileContent, IncomingMessage, PollBatch, RemoteFile


def object_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def identifier(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    text = str(value)
    return text if 0 < len(text) <= 200 else ""


class Adapter:
    def __init__(self, profile: ChannelProfile, client: httpx.AsyncClient) -> None:
        self.profile, self.http = profile, ChannelHTTP(client, profile)
        self.delivery_started = False

    def ready(self) -> None:
        if not self.profile.base_url or not self.profile.owner_ids:
            raise ChannelError("渠道需要明确 base_url 和 owner_ids")

    def endpoint(self, path: str) -> str:
        return self.profile.base_url.rstrip("/") + "/" + path.lstrip("/")

    async def poll(self, cursor: str) -> PollBatch:
        raise ChannelError("此渠道使用签名 webhook 接收")

    def authenticate(self, raw: bytes, headers: Mapping[str, str]) -> None:
        raise ChannelError("此渠道不接收 webhook", status_code=404)

    def webhook(self, payload: dict[str, Any]) -> tuple[IncomingMessage, ...]:
        return ()

    async def send(
        self,
        route: dict[str, Any],
        text: str,
        file: FileContent | None,
        *,
        key: str,
        sequence: int,
    ) -> str:
        raise ChannelError("渠道未实现此消息格式")

    async def download(self, file: RemoteFile, route: dict[str, Any]) -> FileContent:
        if not file.url:
            raise ChannelError("附件没有可用下载地址")
        if file.size is not None and file.size > self.profile.max_file_bytes:
            raise ChannelError("渠道附件超过 max_file_bytes")
        data, _ = await self.http.request(
            "GET",
            file.url,
            remote=True,
            limit=self.profile.max_file_bytes,
        )
        return FileContent(file.name, file.mime_type, data)


class Telegram(Adapter):
    def ready(self) -> None:
        super().ready()
        if not self.profile.secret("token"):
            raise ChannelError("Telegram token 未配置")

    def endpoint(self, path: str) -> str:
        token = quote(self.profile.secret("token"), safe=":")
        return super().endpoint("bot" + token + "/" + path)

    async def call(self, method: str, *, delivery: bool = False, **kwargs: Any) -> Any:
        result = await self.http.json("POST", self.endpoint(method), delivery=delivery, **kwargs)
        if result.get("ok") is not True:
            raise ChannelError("Telegram 拒绝请求", retryable=result.get("error_code") == 429)
        return result.get("result")

    def webhook(self, payload: dict[str, Any]) -> tuple[IncomingMessage, ...]:
        event_id = identifier(payload.get("update_id"))
        message = object_value(payload.get("message"))
        sender, chat = object_value(message.get("from")), object_value(message.get("chat"))
        sender_id, target = identifier(sender.get("id")), identifier(chat.get("id"))
        if not event_id or not sender_id or not target or sender.get("is_bot") is True:
            return ()
        chat_type = chat.get("type")
        if chat_type not in {"private", "group", "supergroup"}:
            return ()
        files: list[RemoteFile] = []
        document = object_value(
            message.get("document")
            or message.get("audio")
            or message.get("voice")
            or message.get("video")
        )
        photos = message.get("photo")
        if not document and isinstance(photos, list) and photos:
            document = object_value(photos[-1])
        file_id = identifier(document.get("file_id"))
        if file_id:
            size = document.get("file_size")
            files.append(
                RemoteFile(
                    str(document.get("file_name") or "attachment.bin"),
                    str(document.get("mime_type") or "application/octet-stream"),
                    file_id=file_id,
                    size=size if type(size) is int and size >= 0 else None,
                )
            )
        return (
            IncomingMessage(
                event_id,
                sender_id,
                target,
                str(message.get("text") or message.get("caption") or ""),
                "private" if chat_type == "private" else "group",
                identifier(message.get("message_id")),
                tuple(files),
            ),
        )

    def authenticate(self, raw: bytes, headers: Mapping[str, str]) -> None:
        expected = self.profile.secret("webhook_secret")
        if not expected or not hmac.compare_digest(
            headers.get("x-telegram-bot-api-secret-token", ""), expected
        ):
            raise ChannelError("Telegram webhook 密钥无效", status_code=401)

    async def poll(self, cursor: str) -> PollBatch:
        updates = await self.call(
            "getUpdates",
            json={
                "offset": int(cursor or "0"),
                "timeout": self.profile.poll_timeout,
                "limit": 100,
                "allowed_updates": ["message"],
            },
            timeout=max(self.profile.timeout, self.profile.poll_timeout + 5),
        )
        if not isinstance(updates, list):
            raise ChannelError("Telegram updates 格式错误")
        messages: list[IncomingMessage] = []
        offset = int(cursor or "0")
        for update in updates:
            if isinstance(update, dict):
                messages.extend(self.webhook(update))
                if type(update.get("update_id")) is int:
                    offset = max(offset, update["update_id"] + 1)
        return PollBatch(tuple(messages), str(offset))

    async def send(
        self,
        route: dict[str, Any],
        text: str,
        file: FileContent | None,
        *,
        key: str,
        sequence: int,
    ) -> str:
        self.delivery_started = True
        if file is None:
            result = await self.call(
                "sendMessage", delivery=True, json={"chat_id": route["target"], "text": text}
            )
        else:
            result = await self.call(
                "sendDocument",
                delivery=True,
                data={"chat_id": route["target"]},
                files={"document": (file.name, file.data, file.mime_type)},
            )
        message_id = identifier(object_value(result).get("message_id"))
        if not message_id:
            raise ChannelError("Telegram 发送确认缺少消息 ID", uncertain=True)
        return message_id

    async def download(self, file: RemoteFile, route: dict[str, Any]) -> FileContent:
        limit = min(self.profile.max_file_bytes, 20 * 1024 * 1024)
        if file.size is not None and file.size > limit:
            raise ChannelError("Telegram 附件超过接收上限")
        result = object_value(await self.call("getFile", json={"file_id": file.file_id}))
        path = result.get("file_path")
        if (
            not isinstance(path, str)
            or not path
            or any(piece in {"..", "."} for piece in path.split("/"))
            or path.startswith("/")
        ):
            raise ChannelError("Telegram 文件路径无效")
        token = quote(self.profile.secret("token"), safe=":")
        url = self.profile.base_url.rstrip("/") + "/file/bot" + token + "/" + quote(path, safe="/")
        data, _ = await self.http.request("GET", url, limit=limit)
        return FileContent(file.name, file.mime_type, data)


class OneBot(Adapter):
    def authenticate(self, raw: bytes, headers: Mapping[str, str]) -> None:
        secret = self.profile.secret("webhook_secret")
        expected = "sha1=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha1).hexdigest()
        if not secret or not hmac.compare_digest(headers.get("x-signature", ""), expected):
            raise ChannelError("OneBot webhook 签名无效", status_code=401)

    async def call(self, action: str, parameters: dict[str, Any], *, delivery: bool = False) -> Any:
        token = self.profile.secret("token")
        result = await self.http.json(
            "POST",
            self.endpoint(action),
            delivery=delivery,
            json=parameters,
            headers={"Authorization": "Bearer " + token} if token else {},
        )
        if result.get("retcode") != 0 or result.get("status") != "ok":
            raise ChannelError("OneBot 拒绝请求")
        return result.get("data")

    def webhook(self, payload: dict[str, Any]) -> tuple[IncomingMessage, ...]:
        if payload.get("post_type") not in {"message", "notice"} or payload.get("anonymous"):
            return ()
        sender, event_id = identifier(payload.get("user_id")), identifier(payload.get("message_id"))
        group = identifier(payload.get("group_id"))
        kind: Literal["private", "group"] = "group" if group else "private"
        text: list[str] = []
        files: list[RemoteFile] = []
        segments = payload.get("message", [])
        if isinstance(segments, str):
            text.append(segments)
        elif isinstance(segments, list):
            for segment in segments[:64]:
                segment = object_value(segment)
                data = object_value(segment.get("data"))
                segment_type = segment.get("type")
                if segment_type == "text":
                    text.append(str(data.get("text", "")))
                elif segment_type in {"image", "record", "video", "file"}:
                    files.append(
                        RemoteFile(
                            str(data.get("name") or segment_type + ".bin"),
                            url=str(data.get("url") or ""),
                            file_id=str(data.get("file_id") or data.get("file") or ""),
                            metadata={"type": segment_type},
                        )
                    )
        notice = payload.get("notice_type")
        if notice in {"group_upload", "offline_file"}:
            file = object_value(payload.get("file"))
            file_id = identifier(file.get("id"))
            size = file.get("size")
            if file_id:
                files.append(
                    RemoteFile(
                        str(file.get("name") or "file.bin"),
                        file_id=file_id,
                        url=str(file.get("url") or ""),
                        size=size if type(size) is int else None,
                        metadata={"type": "file"},
                    )
                )
                event_id = f"file:{payload.get('time', '')}:{file_id}"
        if not sender or not event_id or (not text and not files):
            return ()
        return (
            IncomingMessage(
                event_id, sender, group or sender, "".join(text), kind, event_id, tuple(files)
            ),
        )

    async def send(
        self,
        route: dict[str, Any],
        text: str,
        file: FileContent | None,
        *,
        key: str,
        sequence: int,
    ) -> str:
        group = route["kind"] == "group"
        parameters: dict[str, Any] = {"group_id" if group else "user_id": route["target"]}
        if file is None:
            parameters["message"] = [{"type": "text", "data": {"text": text}}]
            action = "send_group_msg" if group else "send_private_msg"
        else:
            # NapCat's file upload action accepts base64://, without a shared host path.
            parameters.update(
                file="base64://" + base64.b64encode(file.data).decode("ascii"), name=file.name
            )
            action = "upload_group_file" if group else "upload_private_file"
        self.delivery_started = True
        result = await self.call(action, parameters, delivery=True)
        return identifier(object_value(result).get("message_id")) or key

    async def download(self, file: RemoteFile, route: dict[str, Any]) -> FileContent:
        url = file.url
        if file.file_id:
            kind = file.metadata.get("type")
            if kind in {"image", "record"}:
                action, parameters = (
                    ("get_image" if kind == "image" else "get_record"),
                    {"file": file.file_id},
                )
            elif route["kind"] == "group":
                action, parameters = (
                    "get_group_file_url",
                    {"group_id": route["target"], "file_id": file.file_id},
                )
            else:
                action, parameters = "get_private_file_url", {"file_id": file.file_id}
            try:
                result = object_value(await self.call(action, parameters))
                candidate = result.get("url")
                if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
                    url = candidate
            except ChannelError:
                if not url:
                    raise
        return await super().download(
            RemoteFile(file.name, file.mime_type, url, size=file.size), route
        )


class QQ(Adapter):
    def __init__(self, profile: ChannelProfile, client: httpx.AsyncClient) -> None:
        super().__init__(profile, client)
        self._token = ""
        self._expires = 0.0

    def ready(self) -> None:
        super().ready()
        if not self.profile.app_id or not self.profile.secret("client_secret"):
            raise ChannelError("QQ app_id/client_secret 未配置")
        qq_key(self.profile.secret("client_secret"))

    def authenticate(self, raw: bytes, headers: Mapping[str, str]) -> None:
        if headers.get("x-bot-appid") != self.profile.app_id:
            raise ChannelError("QQ webhook app_id 不匹配", status_code=401)
        timestamp = headers.get("x-signature-timestamp", "")
        if not re.fullmatch(r"[0-9]{1,20}", timestamp):
            raise ChannelError("QQ webhook 缺少签名时间", status_code=401)
        verify_qq(
            self.profile.secret("client_secret"),
            timestamp.encode("ascii") + raw,
            headers.get("x-signature-ed25519", ""),
        )

    def validation(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if payload.get("op") != 13:
            return None
        data = object_value(payload.get("d"))
        token, timestamp = data.get("plain_token"), data.get("event_ts")
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9_.~+/=-]{1,512}", token)
            or not isinstance(timestamp, str)
            or not re.fullmatch(r"[0-9]{1,20}", timestamp)
        ):
            raise ChannelError("QQ webhook 验证请求无效")
        return {
            "plain_token": token,
            "signature": sign_qq(
                self.profile.secret("client_secret"),
                (timestamp + token).encode("utf-8"),
            ),
        }

    def webhook(self, payload: dict[str, Any]) -> tuple[IncomingMessage, ...]:
        event_type, message = payload.get("t"), object_value(payload.get("d"))
        if payload.get("op") != 0 or event_type not in {
            "C2C_MESSAGE_CREATE",
            "GROUP_AT_MESSAGE_CREATE",
        }:
            return ()
        author = object_value(message.get("author"))
        group = identifier(message.get("group_openid"))
        sender = identifier(
            author.get("member_openid") or author.get("user_openid") or author.get("id")
        )
        message_id = identifier(message.get("id"))
        event_id = identifier(payload.get("id")) or message_id
        files = []
        attachments = message.get("attachments", [])
        if isinstance(attachments, list):
            for file in attachments[: self.profile.max_attachments]:
                file = object_value(file)
                url = str(file.get("url") or "")
                if url.startswith("//"):
                    url = "https:" + url
                if url:
                    size = file.get("size")
                    files.append(
                        RemoteFile(
                            str(file.get("filename") or "file.bin"),
                            str(file.get("content_type") or "application/octet-stream"),
                            url=url,
                            size=size if type(size) is int else None,
                        )
                    )
        if not sender or not event_id:
            return ()
        return (
            IncomingMessage(
                event_id,
                sender,
                group or sender,
                str(message.get("content") or ""),
                "group" if group else "private",
                message_id,
                tuple(files),
            ),
        )

    async def _authorization(self) -> dict[str, str]:
        if time.monotonic() >= self._expires:
            result = await self.http.json(
                "POST",
                self.profile.token_url,
                json={
                    "appId": self.profile.app_id,
                    "clientSecret": self.profile.secret("client_secret"),
                },
            )
            token = result.get("access_token")
            try:
                expiry = float(result.get("expires_in", 0))
            except (ValueError, TypeError):
                expiry = 0
            if not isinstance(token, str) or not token or not math.isfinite(expiry) or expiry <= 0:
                raise ChannelError("QQ access_token 响应无效")
            expiry = min(expiry, 86400.0)
            self._token, self._expires = (
                token,
                time.monotonic() + max(1.0, expiry - min(60.0, expiry / 2)),
            )
        return {"Authorization": "QQBot " + self._token, "X-Union-Appid": self.profile.app_id}

    async def send(
        self,
        route: dict[str, Any],
        text: str,
        file: FileContent | None,
        *,
        key: str,
        sequence: int,
    ) -> str:
        if route.get("reply_to") and sequence >= 5:
            raise ChannelError("QQ 同一被动消息的五次回复预算已用完")
        prefix = (
            "/v2/"
            + ("groups/" if route["kind"] == "group" else "users/")
            + quote(route["target"], safe="")
        )
        headers = await self._authorization()
        payload: dict[str, Any] = {"msg_type": 0, "content": text}
        if file is not None:
            if len(file.data) > min(self.profile.max_file_bytes, 5 * 1024 * 1024):
                raise ChannelError("QQ 直接文件上传超过 5 MiB；请使用更小文件")
            file_type = (
                1
                if file.mime_type in {"image/png", "image/jpeg"}
                else 2
                if file.mime_type == "video/mp4"
                else 3
                if file.mime_type.startswith("audio/")
                else 4
            )
            uploaded = await self.http.json(
                "POST",
                self.endpoint(prefix + "/files"),
                headers=headers,
                json={
                    "file_type": file_type,
                    "file_name": file.name,
                    "srv_send_msg": False,
                    "file_data": base64.b64encode(file.data).decode("ascii"),
                },
            )
            info = uploaded.get("file_info")
            if not isinstance(info, str) or not info:
                raise ChannelError("QQ 文件上传没有返回 file_info")
            payload = {"msg_type": 7, "media": {"file_info": info}}
        if route.get("reply_to"):
            payload.update(msg_id=route["reply_to"], msg_seq=sequence + 1)
        self.delivery_started = True
        result = await self.http.json(
            "POST",
            self.endpoint(prefix + "/messages"),
            headers=headers,
            json=payload,
            delivery=True,
        )
        if "code" in result and result["code"] != 0:
            raise ChannelError("QQ 拒绝发送请求")
        message_id = identifier(result.get("id"))
        if not message_id:
            raise ChannelError("QQ 发送确认缺少消息 ID", uncertain=True)
        return message_id
