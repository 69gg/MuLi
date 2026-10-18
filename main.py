"""Source checkout entry point; prefer the installed muli command."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from muli.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
