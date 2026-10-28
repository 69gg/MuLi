from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import muli.__main__ as cli
from muli.service import UserService


@pytest.mark.parametrize("action", ["inspect", "disable", "enable"])
def test_plugins_cli_routes_complete_arguments_and_never_starts_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    config, data, managed = tmp_path / "config.json", tmp_path / "data", tmp_path / "managed"
    route: list[tuple[Path, Path, Sequence[str], Path | None]] = []
    changes: list[tuple[Path, Path, str, bool, Path | None]] = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "MuLi",
            "--config",
            str(config),
            "--data-dir",
            str(data),
            "--managed-root",
            str(managed),
            "plugins",
            action,
            "guard",
        ],
    )

    def routed(
        config: Path, data: Path, arguments: Sequence[str], *, managed_root: Path | None
    ) -> None:
        route.append((config, data, arguments, managed_root))

    async def change(
        service: UserService, identifier: str, disabled: bool, *, managed_root: Path | None
    ) -> dict[str, Any]:
        changes.append((service.config_path, service.data_dir, identifier, disabled, managed_root))
        return {"changed": True}

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("plugin CLI must not construct runtime or start service")

    monkeypatch.setattr("muli.managed_cli.route_command", routed)
    monkeypatch.setattr("muli.plugin_recovery.set_plugin_disabled", change)
    monkeypatch.setattr("muli.runtime.Runtime.__init__", forbidden)
    monkeypatch.setattr(UserService, "action", forbidden)
    cli.main()
    assert route == [(config, data, ["plugins", action, "guard"], managed)]
    result = json.loads(capsys.readouterr().out)
    if action == "inspect":
        assert result["status"] == "invalid"
        assert result["runtime_health"] == "not_checked" and result["code_executed"] is False
        assert changes == []
    else:
        assert result == {"changed": True}
        assert changes == [(config, data, "guard", action == "disable", managed)]
    assert not config.exists() and not data.exists()


def test_plugins_cli_reports_fixed_error_without_secret_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "MuLi",
            "--config",
            str(tmp_path / "config.json"),
            "--data-dir",
            str(tmp_path / "data"),
            "plugins",
            "disable",
            "guard",
        ],
    )

    async def failure(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ValueError("PRIVATE_PATH_AND_CREDENTIAL")

    monkeypatch.setattr("muli.plugin_recovery.set_plugin_disabled", failure)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "plugin_operation_failed" in output.err and "PRIVATE" not in output.err
    assert output.out == "" and not (tmp_path / "config.json").exists()


def test_inspection_uses_same_real_configuration_directory_as_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    actual, alias = tmp_path / "actual", tmp_path / "alias"
    actual.mkdir()
    alias.mkdir()
    config = actual / "config.json"
    config.write_text("{}", encoding="utf-8")
    linked = alias / "config.json"
    linked.symlink_to(config)
    marker = tmp_path / "must-not-execute"
    for parent in (actual, alias):
        directory = parent / "plugins" / "guard"
        directory.mkdir(parents=True)
        (directory / "plugin.json").write_text(
            json.dumps({"id": "guard", "api_version": 1, "name": parent.name}), encoding="utf-8"
        )
        (directory / "main.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8"
        )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "MuLi",
            "--config",
            str(linked),
            "--data-dir",
            str(tmp_path / "data"),
            "plugins",
            "inspect",
            "guard",
        ],
    )
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["name"] == "actual" and result["status"] == "ready"
    assert result["runtime_health"] == "not_checked" and result["code_executed"] is False
    assert not marker.exists() and not (tmp_path / "data").exists()
