import json
import os
import time
from typing import Literal

class DisplayLogger:
    def __init__(self, log_dir="history"):
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_file = os.path.join(self.log_dir, "screen.json")
        self.entries = []
        self._load()

    def _load(self):
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file, "r", encoding="utf-8") as f:
                    self.entries = json.load(f)
            except Exception:
                self.entries = []

    def log(self, role: str, content: str, type: Literal["text", "markdown", "tool"] = "text"):
        entry = {
            "timestamp": time.time(),
            "role": role,
            "content": content,
            "type": type
        }
        self.entries.append(entry)
        self._save()

    def _save(self):
        try:
            with open(self.log_file, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, ensure_ascii=False, indent=2)
        except Exception as e:
            # Avoid crashing if logging fails, but maybe print to stderr
            pass
