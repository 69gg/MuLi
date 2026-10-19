"""平台规定的签名/媒体加密；cryptography 按需导入。"""

from __future__ import annotations

import base64
from typing import Any

from .types import ChannelError


def qq_key(secret: str) -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        raise ChannelError("QQ 回调需要可选 channels 加密依赖") from None
    raw = secret.encode("utf-8")
    if not raw:
        raise ChannelError("QQ client_secret 未配置")
    seed = (raw * ((32 + len(raw) - 1) // len(raw)))[:32]
    return Ed25519PrivateKey.from_private_bytes(seed)


def sign_qq(secret: str, content: bytes) -> str:
    return bytes(qq_key(secret).sign(content)).hex()


def verify_qq(secret: str, content: bytes, signature: str) -> None:
    try:
        qq_key(secret).public_key().verify(bytes.fromhex(signature), content)
    except ChannelError:
        raise
    except Exception:
        raise ChannelError("QQ 回调签名无效", status_code=401) from None


def aes_key(encoded: str) -> bytes:
    try:
        key = base64.b64decode(encoded, validate=True)
        if len(key) == 32:
            key = bytes.fromhex(key.decode("ascii"))
        if len(key) != 16:
            raise ValueError
        return key
    except (ValueError, UnicodeError):
        raise ChannelError("iLink 媒体密钥无效") from None


def aes_media(data: bytes, key: bytes, *, encrypt: bool) -> bytes:
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise ChannelError("iLink 媒体需要可选 channels 加密依赖") from None
    if len(key) != 16:
        raise ChannelError("iLink 媒体密钥长度无效")
    try:
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        if encrypt:
            padder = padding.PKCS7(128).padder()
            padded = padder.update(data) + padder.finalize()
            operation = cipher.encryptor()
            return operation.update(padded) + operation.finalize()
        operation = cipher.decryptor()
        padded = operation.update(data) + operation.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise ChannelError("iLink 媒体密文或填充无效") from None
