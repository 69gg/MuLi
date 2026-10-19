"""A finite local log for launchd and pythonw, without a console or shell redirection."""

from __future__ import annotations

import argparse
import io
import sys
import threading
from pathlib import Path


class BoundedLog(io.TextIOBase):
    encoding = "utf-8"

    def __init__(self, path: Path, max_bytes: int = 1024 * 1024) -> None:
        super().__init__()
        self.path, self.max_bytes = path, max_bytes
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        data = value.encode("utf-8", errors="replace")
        if not data:
            return 0
        data = data[-self.max_bytes :]
        with self._lock:
            if self.path.exists() and self.path.stat().st_size + len(data) > self.max_bytes:
                self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
            with self.path.open("ab") as output:
                self.path.chmod(0o600)
                output.write(data)
        return len(value)

    def flush(self) -> None:
        # Each write closes the file, so both explicit flush and shutdown are already durable.
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-file", required=True, type=Path)
    arguments, remaining = parser.parse_known_args()
    if remaining and remaining[0] == "--":
        remaining = remaining[1:]
    log = BoundedLog(arguments.log_file)
    sys.stdout, sys.stderr = log, log
    sys.argv = ["muli", *remaining]
    from muli.__main__ import main as muli_main

    muli_main()


if __name__ == "__main__":
    main()
