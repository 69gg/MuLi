"""iLink 2.4.6 文本及 AES/CDN 媒体；参考同作者的 weixin-ilink-client 协议。"""

from __future__ import annotations

import base64
import hashlib
import math
import secrets
from typing import Any
from urllib.parse import quote, urlsplit

from muli import __version__

from .adapters import Adapter, identifier, object_value
from .crypto import aes_key, aes_media
from .types import ChannelError, FileContent, IncomingMessage, PollBatch, RemoteFile

PROTOCOL_VERSION = "2.4.6"
DEFAULT_WEIXIN_BASE_URL = "https://ilinkai.weixin.qq.com"
_CLIENT_VERSION = (2 << 16) | (4 << 8) | 6


class Weixin(Adapter):
    # iLink 原生语音发送未实现；语音附件必须显式使用 media_mode=file。
    voice_mime_types: frozenset[str] = frozenset()

    def ready(self) -> None:
        super().ready()
        if (
            not self.profile.secret("token")
            or not self.profile.account_id
            or not self.profile.user_id
        ):
            raise ChannelError("iLink 需要 token/account_id/user_id")

    def _headers(self) -> dict[str, str]:
        uin = str(int.from_bytes(secrets.token_bytes(4), "big"))
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": "Bearer " + self.profile.secret("token"),
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": str(_CLIENT_VERSION),
            "X-WECHAT-UIN": base64.b64encode(uin.encode("ascii")).decode("ascii"),
        }

    @staticmethod
    def _qr_headers() -> dict[str, str]:
        # The QR endpoints are unauthenticated; a stale bot token must not
        # participate in a fresh login.
        uin = str(int.from_bytes(secrets.token_bytes(4), "big"))
        return {
            "Content-Type": "application/json",
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": str(_CLIENT_VERSION),
            "X-WECHAT-UIN": base64.b64encode(uin.encode("ascii")).decode("ascii"),
        }

    async def qr_login_start(self) -> tuple[str, str]:
        """Request one iLink QR payload and its encoded login content."""
        result = await self.http.json(
            "POST",
            self.endpoint("ilink/bot/get_bot_qrcode?bot_type=3"),
            json={"local_token_list": []},
            headers=self._qr_headers(),
            timeout=max(self.profile.timeout, 20),
            remote=True,
        )
        qrcode = result.get("qrcode")
        content = result.get("qrcode_img_content")
        if (
            not isinstance(qrcode, str)
            or not qrcode.strip()
            or len(qrcode) > 2048
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > 8192
        ):
            raise ChannelError("iLink 二维码响应格式错误")
        return qrcode.strip(), content.strip()

    async def qr_login_poll(
        self, qrcode: str, base_url: str, *, verify_code: str = ""
    ) -> dict[str, Any]:
        """One long poll of an iLink login session; no bot credentials are sent."""
        if not isinstance(qrcode, str) or not qrcode or len(qrcode) > 2048:
            raise ChannelError("iLink 二维码会话无效")
        endpoint = (
            qr_base_url(base_url) + "/ilink/bot/get_qrcode_status?qrcode=" + quote(qrcode, safe="")
        )
        if verify_code:
            endpoint += "&verify_code=" + quote(verify_code, safe="")
        return await self.http.json(
            "GET",
            endpoint,
            headers=self._qr_headers(),
            timeout=max(self.profile.timeout, 40),
            remote=True,
        )

    async def call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        delivery: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body = {
            **payload,
            "base_info": {"channel_version": PROTOCOL_VERSION, "bot_agent": f"muli/{__version__}"},
        }
        result = await self.http.json(
            "POST",
            self.endpoint("ilink/bot/" + method),
            json=body,
            headers=self._headers(),
            delivery=delivery,
            timeout=timeout,
        )
        if method == "sendmessage":
            for field in ("ret", "errcode"):
                if field not in result:
                    continue
                value = result[field]
                if type(value) is not int and not (type(value) is float and math.isfinite(value)):
                    raise ChannelError("iLink 发送回执格式错误", uncertain=True)
        code = result.get("ret", 0) or result.get("errcode", 0)
        if code:
            raise ChannelError(
                "iLink 令牌已失效" if code == -14 else "iLink 拒绝请求",
                retryable=True,
                retry_after=3600 if code == -14 else 0,
            )
        return result

    def _message(self, value: dict[str, Any]) -> IncomingMessage | None:
        sender = identifier(value.get("from_user_id"))
        event_id = identifier(value.get("message_id") or value.get("client_id"))
        if not event_id and type(value.get("seq")) is int:
            event_id = "seq:" + str(value["seq"]) + ":" + sender
        if value.get("message_type") != 1 or not sender or not event_id:
            return None
        texts: list[str] = []
        files: list[RemoteFile] = []
        previous_voice = False
        items = value.get("item_list", [])
        if not isinstance(items, list):
            raise ChannelError("iLink 消息 item_list 格式错误")
        for item in items[:64]:
            item = object_value(item)
            kind = item.get("type")
            text = ""
            if kind == 1:
                text = str(object_value(item.get("text_item")).get("text") or "")
            elif kind == 3:
                transcript = object_value(item.get("voice_item")).get("text")
                if isinstance(transcript, str) and transcript.strip():
                    text = transcript
            if text:
                if texts and (previous_voice or kind == 3):
                    texts.append("\n")
                texts.append(text)
                previous_voice = kind == 3
            if kind in {2, 3, 4, 5}:
                key = {2: "image_item", 3: "voice_item", 4: "file_item", 5: "video_item"}[kind]
                detail = object_value(item.get(key))
                media = object_value(detail.get("media"))
                name = str(
                    detail.get("file_name")
                    or {2: "image.bin", 3: "voice.silk", 4: "file.bin", 5: "video.mp4"}[kind]
                )
                files.append(
                    RemoteFile(
                        name,
                        "video/mp4"
                        if kind == 5
                        else "audio/silk"
                        if kind == 3
                        else "application/octet-stream",
                        metadata={"media": media, "aeskey": detail.get("aeskey", "")},
                    )
                )
        return IncomingMessage(
            event_id,
            sender,
            sender,
            "".join(texts),
            reply_to=event_id,
            files=tuple(files),
            context_token=str(value.get("context_token") or ""),
        )

    async def poll(self, cursor: str) -> PollBatch:
        result = await self.call(
            "getupdates",
            {"get_updates_buf": cursor},
            timeout=max(self.profile.timeout, self.profile.poll_timeout + 5),
        )
        values = result.get("msgs", [])
        if not isinstance(values, list) or len(values) > 1000:
            raise ChannelError("iLink updates 格式或数量无效")
        messages = tuple(
            message
            for value in values
            if isinstance(value, dict)
            if (message := self._message(value)) is not None
        )
        next_cursor = result.get("get_updates_buf", cursor)
        if not isinstance(next_cursor, str) or len(next_cursor) > self.profile.max_response_bytes:
            raise ChannelError("iLink cursor 格式错误")
        return PollBatch(messages, next_cursor)

    async def send(
        self,
        route: dict[str, Any],
        text: str,
        file: FileContent | None,
        *,
        key: str,
        sequence: int,
    ) -> str:
        context = route.get("context_token")
        if not isinstance(context, str) or not context:
            raise ChannelError("iLink 当前目标缺少有效 context_token；需要用户先发消息")
        item: dict[str, Any] = {"type": 1, "text_item": {"text": text}}
        if file is not None:
            item = await self._upload(route["target"], file)
        client_id = "muli-" + key + "-" + str(sequence)
        self.delivery_started = True
        await self.call(
            "sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": route["target"],
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [item],
                    "context_token": context,
                }
            },
            delivery=True,
        )
        return client_id

    async def _upload(self, peer: str, file: FileContent) -> dict[str, Any]:
        if not file.data or len(file.data) > self.profile.max_file_bytes:
            raise ChannelError("iLink 文件为空或超限")
        key, file_key = secrets.token_bytes(16), secrets.token_hex(16)
        encrypted = aes_media(file.data, key, encrypt=True)
        media_type = (
            1 if file.mime_type.startswith("image/") else 2 if file.mime_type == "video/mp4" else 3
        )
        result = await self.call(
            "getuploadurl",
            {
                "filekey": file_key,
                "media_type": media_type,
                "to_user_id": peer,
                "rawsize": len(file.data),
                "rawfilemd5": hashlib.md5(file.data, usedforsecurity=False).hexdigest(),
                "filesize": len(encrypted),
                "no_need_thumb": True,
                "aeskey": key.hex(),
            },
        )
        url, parameter = result.get("upload_full_url"), result.get("upload_param")
        if not isinstance(url, str) or not url:
            if not isinstance(parameter, str) or not parameter:
                raise ChannelError("iLink 上传响应缺少地址")
            url = (
                self.profile.cdn_base_url.rstrip("/")
                + "/upload?encrypted_query_param="
                + quote(parameter, safe="")
                + "&filekey="
                + file_key
            )
        _, headers = await self.http.request(
            "POST",
            url,
            remote=True,
            content=encrypted,
            headers={"Content-Type": "application/octet-stream"},
        )
        download = headers.get("x-encrypted-param")
        if not download:
            raise ChannelError("iLink CDN 上传缺少确认参数")
        media = {
            "encrypt_query_param": download,
            "aes_key": base64.b64encode(key.hex().encode("ascii")).decode("ascii"),
            "encrypt_type": 1,
        }
        if media_type == 1:
            return {"type": 2, "image_item": {"media": media, "mid_size": len(encrypted)}}
        if media_type == 2:
            return {"type": 5, "video_item": {"media": media, "video_size": len(encrypted)}}
        # iLink 2.4.6 has no supported outbound voice wire form: audio remains a file.
        return {
            "type": 4,
            "file_item": {"media": media, "file_name": file.name, "len": str(len(file.data))},
        }

    async def download(self, file: RemoteFile, route: dict[str, Any]) -> FileContent:
        media = object_value(file.metadata.get("media"))
        url = media.get("url") or media.get("full_url")
        if not isinstance(url, str) or not url:
            parameter = media.get("encrypt_query_param")
            if not isinstance(parameter, str) or not parameter:
                raise ChannelError("iLink 附件缺少 CDN 参数")
            url = (
                self.profile.cdn_base_url.rstrip("/")
                + "/download?encrypted_query_param="
                + quote(parameter, safe="")
            )
        maximum = ((self.profile.max_file_bytes // 16) + 1) * 16
        data, _ = await self.http.request("GET", url, remote=True, limit=maximum)
        hexadecimal = file.metadata.get("aeskey")
        try:
            key = (
                bytes.fromhex(hexadecimal)
                if isinstance(hexadecimal, str) and hexadecimal
                else aes_key(str(media.get("aes_key") or ""))
            )
        except ValueError:
            raise ChannelError("iLink 媒体 AES key 无效") from None
        content = aes_media(data, key, encrypt=False)
        if len(content) > self.profile.max_file_bytes:
            raise ChannelError("iLink 解密后文件超过上限")
        return FileContent(file.name, file.mime_type, content)


def qr_base_url(value: str) -> str:
    """Accept only a bare HTTPS origin returned by the QR status endpoint."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ChannelError("iLink 登录地址无效")
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError:
        raise ChannelError("iLink 登录地址无效") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is not None
    ):
        raise ChannelError("iLink 登录地址无效")
    return "https://" + parsed.hostname
