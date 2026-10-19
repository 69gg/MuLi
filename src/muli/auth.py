"""Single-owner password sessions; plaintext passwords never enter saved configuration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable

from muli.config import ConfigFile

COOKIE_NAME = "muli_session"


def hash_password(password: str) -> str:
    if not 8 <= len(password) <= 1024:
        raise ValueError("访问密码长度需为 8–1024 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if len(password) > 1024:
        return False
    try:
        algorithm, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt" or len(salt_hex) != 32 or len(digest_hex) != 64:
            return False
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(expected, actual)
    except (TypeError, ValueError):
        return False


class Authentication:
    def __init__(self, config: ConfigFile, clock: Callable[[], float] = time.monotonic) -> None:
        self.config, self.clock = config, clock
        self.sessions: dict[str, tuple[float, str]] = {}
        self.attempts: deque[float] = deque()
        self.lock = asyncio.Lock()

    @property
    def required(self) -> bool:
        return bool(self.config.settings.server.password_hash)

    def authenticated(self, token: str) -> bool:
        if not self.required:
            return True
        now = self.clock()
        saved = self.sessions.get(hashlib.sha256(token.encode()).hexdigest())
        return bool(
            saved and saved[0] > now and saved[1] == self.config.settings.server.password_hash
        )

    async def login(self, password: str) -> str:
        async with self.lock:
            now = self.clock()
            while self.attempts and self.attempts[0] <= now - 60:
                self.attempts.popleft()
            if len(self.attempts) >= 10:
                raise ValueError("登录尝试过多，请稍后再试")
            self.attempts.append(now)
            encoded = self.config.settings.server.password_hash
            if not encoded or not await asyncio.to_thread(verify_password, password, encoded):
                raise PermissionError("密码不正确")
            if encoded != self.config.settings.server.password_hash:
                raise PermissionError("密码已变更，请重新登录")
            self.sessions = {key: value for key, value in self.sessions.items() if value[0] > now}
            if len(self.sessions) >= 64:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = (
                now + self.config.settings.server.session_ttl_seconds,
                encoded,
            )
            return token

    def logout(self, token: str) -> None:
        self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
