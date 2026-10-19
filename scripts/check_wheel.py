"""Install the built wheel offline and check it outside the source checkout."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path


async def check_installed(target: Path) -> None:
    import httpx

    import muli
    from muli.config import ConfigFile
    from muli.server import create_app

    assert Path(muli.__file__).resolve().is_relative_to(target.resolve()), muli.__file__
    with tempfile.TemporaryDirectory(prefix="muli-wheel-data-") as directory:
        root = Path(directory)
        config = ConfigFile(root / "config.json")
        config.update({"updates": {"check_on_start": False}})
        app = create_app(config, root / "data")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
            ) as client:
                for path in (
                    "/",
                    "/app.mjs",
                    "/task-control.mjs",
                    "/assets/muli-avatar.svg",
                    "/assets/muli-empty.svg",
                    "/api/docs",
                    "/api-docs.mjs",
                ):
                    assert (await client.get(path)).status_code == 200, path
                state = (await client.get("/api/status")).json()
                assert state["version"] == muli.__version__
                assert state["configured"] is False
                assert app.state.runtime._history("new")[0].content
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
    print("Installed wheel: import, assets, API docs, prompts, task control and lifespan passed")


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
        environment = dict(os.environ, PYTHONPATH=str(target))
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
