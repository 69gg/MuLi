from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Never

import pytest

from muli.__main__ import main
from muli.config import ConfigFile


def isolated_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: list[str]
) -> tuple[Path, Path]:
    root = tmp_path / "沐璃"
    config, data = root / "config.json", root / "data"
    monkeypatch.setenv("MULI_HOME", str(root))
    monkeypatch.delenv("MULI_CONFIG", raising=False)
    monkeypatch.delenv("MULI_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "argv", ["MuLi", *arguments])

    def unexpected(*args: Any, **kwargs: Any) -> Never:
        raise AssertionError("初始化不应下载源码或安装、启动服务")

    monkeypatch.setattr("muli.initialization.clone_source", unexpected)
    monkeypatch.setattr("muli.initialization.start_background", unexpected)
    return config, data


def test_defaults_cli_creates_local_configuration_without_network_or_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, data = isolated_command(monkeypatch, tmp_path, ["init", "--defaults"])
    main()
    settings = ConfigFile(config).settings
    assert settings.onboarding_pending and not settings.models.main.configured
    assert data.is_dir() and not (config.parent / "source").exists()
    output = capsys.readouterr().out
    assert str(config) in output and "MuLi serve" in output


def test_cli_never_overwrites_existing_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _ = isolated_command(monkeypatch, tmp_path, ["init", "--defaults"])
    config.parent.mkdir(parents=True)
    config.write_text('{"persona":{"user_name":"保留姓名"}}\n', encoding="utf-8")
    original = config.read_bytes()
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert config.read_bytes() == original
    assert "未覆盖" in capsys.readouterr().err


@pytest.mark.parametrize("arguments", [["unknown-command"], ["init", "--unknown-option"]])
def test_invalid_cli_command_does_not_initialize_or_start_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    config, data = isolated_command(monkeypatch, tmp_path, arguments)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert not config.exists() and not data.exists()


def test_invalid_interactive_settings_exit_without_partial_configuration_or_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, data = isolated_command(monkeypatch, tmp_path, ["init"])

    def invalid_settings(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"models": {"main": {"protocol": "unsupported-protocol"}}}

    monkeypatch.setattr("muli.initialization.prompt_settings", invalid_settings)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert not config.exists() and not data.exists()
