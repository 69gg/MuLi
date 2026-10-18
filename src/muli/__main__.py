"""Small CLI for local serving, diagnostics and user-service installation."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from muli import __version__
from muli.config import ConfigFile, default_config_path, default_data_dir
from muli.service import UserService


def main() -> None:
    parser = argparse.ArgumentParser(prog="muli", description="沐璃 · 本地数字生命助手")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="启动本地管理工作台")
    serve.add_argument("--port", type=int)
    commands.add_parser("init", help="创建默认配置（不覆盖已有文件）")
    service = commands.add_parser("service", help="管理 Linux 用户服务")
    service.add_argument(
        "action",
        choices=["status", "print", "install", "start", "stop", "restart", "enable", "disable"],
    )
    commands.add_parser("doctor", help="显示配置和本机能力")
    args = parser.parse_args()
    config = ConfigFile(args.config)
    if args.command == "init":
        if args.config.exists():
            parser.error("配置已存在，未覆盖")
        config.update({})
        print(f"已创建 {args.config}")
    elif args.command == "service":
        manager = UserService(args.config, args.data_dir)
        if args.action == "print":
            print(manager.render())
        else:
            result = asyncio.run(
                manager.status() if args.action == "status" else manager.action(args.action)
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        from muli.notifications import notification_capability
        from muli.tools.computer import Computer

        print(
            json.dumps(
                {
                    "version": __version__,
                    "configured": config.settings.models.main.configured,
                    "config": str(args.config),
                    "data_dir": str(args.data_dir),
                    "computer": asyncio.run(Computer(config.settings).status()),
                    "notifications": {
                        **notification_capability(),
                        "enabled": config.settings.notifications.enabled,
                    },
                    "service_supported": UserService(args.config, args.data_dir).supported,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        import uvicorn

        from muli.server import create_app

        port = getattr(args, "port", None) or config.settings.server.port
        uvicorn.run(
            create_app(config, args.data_dir),
            host=config.settings.server.host,
            port=port,
            log_level="info",
            access_log=False,
            timeout_graceful_shutdown=5,
        )


if __name__ == "__main__":
    main()
