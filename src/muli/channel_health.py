"""Finite facts for the channels that actually own a background receiver."""

from __future__ import annotations

POLLING_FAILURES: dict[str, tuple[str, str]] = {
    "telegram": ("telegram_receive_unavailable", "telegram_receive_failed"),
    "weixin": ("weixin_receive_unavailable", "weixin_receive_failed"),
}
CHANNEL_RECEIVE_FAILURE_CODES: frozenset[str] = frozenset(
    code for pair in POLLING_FAILURES.values() for code in pair
)
