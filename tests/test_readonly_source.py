from __future__ import annotations

from pathlib import Path

import pytest

from muli.config import Settings
from muli.tools.files import Files


def test_reference_sources_are_readable_copyable_but_not_mutable(tmp_path: Path) -> None:
    source = tmp_path / "MuLi/source"
    source.mkdir(parents=True)
    module = source / "main.py"
    module.write_text("print('reference')", encoding="utf-8")
    files = Files(
        Settings(workspace_roots=[str(tmp_path)]), tmp_path / "data", readonly_roots=[source]
    )
    assert files._execute("read", {"path": str(module)})["content"] == "print('reference')"
    for target in (source, module, source.parent):
        with pytest.raises(PermissionError, match="只读"):
            files.editable(str(target))
    destination = tmp_path / "copy.py"
    files._execute("copy", {"path": str(module), "destination": str(destination)})
    assert destination.read_bytes() == module.read_bytes()
    with pytest.raises(PermissionError, match="只读"):
        files._execute("copy", {"path": str(destination), "destination": str(source / "other.py")})
