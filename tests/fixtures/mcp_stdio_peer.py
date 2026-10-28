"""Owned, finite JSON-RPC peer for the locked SDK stdio journey only."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from threading import Timer
from typing import Any

MAX_REQUESTS = 12
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 1024
LIFETIME_SECONDS = 15


def expire() -> None:
    # The fixture never starts children; this also bounds an abandoned stdin read.
    os._exit(124)


def respond(request_id: int | str, **fields: Any) -> None:
    data = json.dumps({"jsonrpc": "2.0", "id": request_id, **fields}).encode() + b"\n"
    assert len(data) <= MAX_RESPONSE_BYTES
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def serve(generation: str) -> None:
    assert generation in {"before", "after", "rejected"}
    calls = 0
    for _ in range(MAX_REQUESTS):
        data = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if not data:
            return
        assert len(data) <= MAX_REQUEST_BYTES and data.endswith(b"\n")
        request: dict[str, Any] = json.loads(data)
        method = request["method"]
        if "id" not in request:
            assert method in {"notifications/initialized", "notifications/cancelled"}
            continue
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "owned-stdio-peer", "version": "1"},
            }
        elif method == "tools/list":
            if generation == "rejected":
                respond(request["id"], error={"code": -32603, "message": "fixture rejection"})
                continue
            result = {
                "tools": [
                    {
                        "name": "identity",
                        "description": "Owned process identity",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"wait": {"type": "boolean"}},
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        elif method == "tools/call":
            assert request["params"]["name"] == "identity"
            calls += 1
            Path(f"{generation}.calls").write_text(str(calls), encoding="ascii")
            if request["params"].get("arguments", {}).get("wait"):
                Path(f"{generation}.entered").touch()
                while not Path(f"{generation}.release").exists():
                    time.sleep(0.01)
            identity = {
                "pid": os.getpid(),
                "argument": generation,
                "environment_sha256": hashlib.sha256(
                    os.environ["MULI_TEST_STDIO_GENERATION"].encode()
                ).hexdigest(),
                "call": calls,
            }
            result = {"content": [{"type": "text", "text": json.dumps(identity)}]}
        else:
            raise AssertionError(f"unexpected fixture method: {method}")
        respond(request["id"], result=result)
    raise AssertionError("owned fixture request budget exhausted")


def main() -> None:
    watchdog = Timer(LIFETIME_SECONDS, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        serve(sys.argv[1])
    finally:
        watchdog.cancel()


if __name__ == "__main__":
    main()
