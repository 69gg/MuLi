"""Install the built wheel offline and check it outside the source checkout."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


@contextmanager
def isolated_home() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="muli-wheel-data-") as directory:
        root = Path(directory)
        with patch.dict(
            os.environ,
            MULI_HOME=str(root),
            MULI_CONFIG=str(root / "config.json"),
            MULI_DATA_DIR=str(root / "data"),
        ):
            yield root


async def check_installed(target: Path) -> None:
    import httpx

    import muli
    from muli.compatibility import CompatibilityManifest, assess_compatibility
    from muli.config import ConfigFile
    from muli.format_admission import CURRENT_FORMAT_STATE
    from muli.server import create_app
    from muli.tools import atspi_helper, ax_helper, ax_lease, uia_computer
    from muli.tools.computer import ComputerError

    assert muli.__file__ is not None
    assert Path(muli.__file__).resolve().is_relative_to(target.resolve()), muli.__file__
    assert Path(atspi_helper.__file__).resolve().is_relative_to(target.resolve())
    assert Path(ax_helper.__file__).resolve().is_relative_to(target.resolve())
    assert Path(ax_lease.__file__).resolve().is_relative_to(target.resolve())
    uia_script = Path(uia_computer.__file__).with_name("uia_helper.ps1")
    assert uia_script.resolve().is_relative_to(target.resolve())
    uia_source = uia_script.read_bytes().decode("utf-8", errors="strict")
    assert "function Initialize-Uia" in uia_source
    assert "function Read-Snapshot" in uia_source
    assert "$PSModuleAutoLoadingPreference = 'None'" in uia_source
    with (Path(muli.__file__).parent / "compatibility.json").open("rb") as stream:
        declaration = CompatibilityManifest.from_json(stream.read(16_384 + 1))
    assert declaration.release == muli.__version__
    assert assess_compatibility(
        declaration, declaration, CURRENT_FORMAT_STATE, handoff_protocol=2
    ).format_compatible
    for domain, version in CURRENT_FORMAT_STATE.formats.model_dump().items():
        support = getattr(declaration.formats, domain)
        assert support.reads == (version,) and support.writes == version, domain
    with isolated_home() as root:
        config = ConfigFile(root / "config.json")
        config.update({"updates": {"check_on_start": False}})
        app = create_app(config, root / "data")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
            ) as client:
                image_types = {
                    "/assets/muli-avatar.webp": "image/webp",
                    "/assets/muli-empty.webp": "image/webp",
                    "/assets/muli-icon.png": "image/png",
                }
                for path in (
                    "/",
                    "/app.mjs",
                    "/conversation-layout.mjs",
                    "/task-control.mjs",
                    "/assets/muli-avatar.svg",
                    "/assets/muli-empty.svg",
                    *image_types,
                    "/api/docs",
                    "/api-docs.mjs",
                ):
                    response = await client.get(path)
                    assert response.status_code == 200, path
                    if path in image_types:
                        assert (
                            response.headers.get("content-type", "").split(";", 1)[0]
                            == image_types[path]
                        ), path
                state = (await client.get("/api/status")).json()
                assert state["version"] == muli.__version__
                assert state["configured"] is False
                assert app.state.runtime._history("new")[0].content
                computer = app.state.runtime.computer
                assert "computer_targets" in app.state.runtime.tools
                assert "computer_release" in app.state.runtime.tools
                try:
                    await computer.targets({})
                except ComputerError as exc:
                    assert exc.code == "disabled" and exc.outcome == "not_started"
                else:
                    raise AssertionError("Disabled desktop discovery must not access a desktop")
                task = await app.state.local_services.commitments.save(
                    "发行包任务控制检查",
                    brief={"goal": "验证离线管理入口", "constraints": [], "evidence_refs": []},
                )
                response = await client.post(
                    f"/api/commitments/{task['id']}/control",
                    json={"action": "pause", "expected_epoch": 0, "request_id": "wheel-check"},
                )
                assert response.status_code == 200, response.text
                assert response.json()["current"]["control"]["paused"] is True
                assert not app.state.runtime.tasks
    print(
        "Installed wheel: import, compatibility declaration, assets, API docs, prompts, "
        "desktop defaults, task control and lifespan passed"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", type=Path)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if args.installed:
        asyncio.run(check_installed(args.installed))
        return
    wheels = list(args.dist.glob("*.whl"))
    if len(wheels) != 1:
        parser.error("dist must contain exactly one freshly built wheel")
    with tempfile.TemporaryDirectory(prefix="muli-wheel-smoke-") as directory:
        target = Path(directory) / "site"
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--offline",
                "--no-deps",
                "--link-mode=copy",
                "--target",
                str(target),
                str(wheels[0].resolve()),
            ],
            check=True,
        )
        # Automatic extension discovery must never inspect the user's real home
        # during a package smoke test, including when it contains enabled MCP.
        environment = dict(
            os.environ, PYTHONPATH=str(target), MULI_HOME=str(Path(directory) / "home")
        )
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--installed", str(target)],
            cwd=directory,
            env=environment,
            check=True,
        )
        subprocess.run(
            [sys.executable, "-m", "muli", "--version"],
            cwd=directory,
            env=environment,
            check=True,
        )


if __name__ == "__main__":
    main()
