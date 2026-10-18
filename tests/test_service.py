from __future__ import annotations

from pathlib import Path

import pytest

from muli.service import UserService, quote_unit


def test_systemd_literal_arguments_escape_percent_and_dollar(tmp_path: Path) -> None:
    path = tmp_path / "${HOME} %i" / "config.json"
    service = UserService(path, tmp_path / "data")
    rendered = service.render()
    assert "$${HOME} %%i" in rendered
    assert "MemoryMax=512M" in rendered
    assert "CPUQuota=50%" in rendered
    assert "WantedBy=default.target" in rendered
    assert quote_unit('a"b\\c') == '"a\\"b\\\\c"'
    with pytest.raises(ValueError):
        quote_unit("bad\npath")
