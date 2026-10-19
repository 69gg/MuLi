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
    initialize = commands.add_parser("init", help="交互设置主模型、称呼与访问方式")
    initialize.add_argument("--defaults", action="store_true", help="不提问，只创建默认配置")
    initialize.add_argument("--no-source", action="store_true", help="暂不下载只读源码参考")
    initialize.add_argument(
        "--no-service", action="store_true", help="只配置，不安装或启动后台用户服务"
    )
    service = commands.add_parser("service", help="管理当前系统的用户后台服务")
    service.add_argument(
        "action",
        choices=["status", "print", "install", "start", "stop", "restart", "enable", "disable"],
    )
    commands.add_parser("doctor", help="显示配置和本机能力")
    args = parser.parse_args()
    config = ConfigFile(args.config)
    if args.command == "init":
        from muli.initialization import clone_source, start_background
        from muli.initialization import initialize as initialize_config

        try:
            initial_settings = initialize_config(args.config, args.data_dir, defaults=args.defaults)
        except (ValueError, FileExistsError) as exc:
            parser.error(str(exc))
        print(f"已创建 {args.config}；数据保存在 {args.data_dir}")
        if not args.no_source and not args.defaults:
            result = clone_source(
                args.config.expanduser().resolve().parent, initial_settings.updates.repository
            )
            print(f"源码参考：{result.get('path', result.get('reason'))}")
        if not args.defaults and not args.no_service:
            result = asyncio.run(start_background(UserService(args.config, args.data_dir)))
            if result["accepted"]:
                print(
                    "后台启动请求已提交，并已启用用户登录启动；可用 MuLi service status 查看状态。"
                )
            else:
                print(
                    f"后台设置未完成：{result.get('reason', '请查看服务状态')}。已完成：{result['completed']}"
                )
                print("配置已保留，可运行 MuLi serve，或稍后使用 MuLi service 继续设置。")
        else:
            print("运行 MuLi serve 启动工作台。")
        host = initial_settings.server.host
        host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
        address = f"[{host}]" if ":" in host else host
        print(
            f"工作台：http://{address}:{initial_settings.server.port}；沐璃会接着帮你配置其它能力。"
        )
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
            create_app(config, args.data_dir, listen_port=port),
            host=config.settings.server.host,
            port=port,
            log_level="info",
            access_log=False,
            timeout_graceful_shutdown=5,
        )


if __name__ == "__main__":
    main()
