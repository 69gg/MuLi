"""Fixed receive failures shared by the guard and host evidence; no optional SDK."""

from typing import Literal, get_args

ReceiveFailureCode = Literal[
    "mcp_receive_budget_exceeded",
    "mcp_receive_encoding_unsupported",
    "mcp_receive_charset_unsupported",
    "mcp_receive_headers_invalid",
    "mcp_receive_stream_unsupported",
    "mcp_receive_failed",
]

RECEIVE_FAILURE_CODES: frozenset[str] = frozenset(get_args(ReceiveFailureCode))
