"""Small CLI for local serving, diagnostics and user-service installation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from muli import __version__
from muli.config import ConfigFile, default_config_path, default_data_dir
from muli.service import UserService


async def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from muli.server import create_app

    session = None
    app = None
    handed_off = False
    try:
        if sys.platform == "linux":
            from muli.platforms.linux_bootstrap import open_boot_session

            session = await open_boot_session(
                UserService(args.config, args.data_dir),
                args.data_dir / "muli.sqlite3",
                managed_root=getattr(args, "managed_root", None),
            )
        from muli.managed_boot import ManagedBootPermit

        config = (
            session.permit.config
            if session is not None and isinstance(session.permit, ManagedBootPermit)
            else ConfigFile(args.config)
        )
        port = getattr(args, "port", None) or config.settings.server.port
        if (
            session is not None
            and session.permit is not None
            and port != config.settings.server.port
        ):
            raise ValueError("transaction_port_override_unsupported")
        from muli.platforms.linux_transaction import clock_reading

        handed_off = True
        app = create_app(
            config,
            args.data_dir,
            listen_port=port,
            boot_session=session,
            maintenance_clock=clock_reading if session and session.permit else None,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.settings.server.host,
                port=port,
                log_level="info",
                access_log=False,
                timeout_graceful_shutdown=5,
                workers=1,
            )
        )
        await server.serve()
        if not server.started:
            raise RuntimeError("service_startup_failed")
    finally:
        if app is not None:
            # Also covers server/config failures before ASGI lifespan entry.
            # The same owned task is joined by lifespan and this outer owner.
            await app.state.close()
        elif session is not None and not handed_off:
            # After handoff only the writer owner can certify successful close.
            # A failed close must retain the lease until process exit.
            session.close_after_writers()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="muli", description="沐璃 · 本地数字生命助手", allow_abbrev=False
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--managed-root", type=Path, help=argparse.SUPPRESS)
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
    recovery = commands.add_parser("management", help="查看恢复事务或接续现有控制者")
    recovery.add_argument("action", choices=["status", "recover"])
    plugins = commands.add_parser("plugins", help="检查插件，或停机后调整下次启动的启停设置")
    plugins.add_argument("action", choices=["inspect", "enable", "disable"])
    plugins.add_argument("identifier", help="插件目录 ID")
    commands.add_parser(
        "upgrade-channel-ownership", help="停机升级支持的旧格式（Linux；macOS 仅精确 memory2）"
    )
    args = parser.parse_args()
    if args.command not in {None, "serve", "upgrade-channel-ownership"}:
        from muli.managed_cli import route_command

        arguments = [args.command]
        if args.command in {"service", "management"}:
            arguments.append(args.action)
        elif args.command == "plugins":
            arguments.extend([args.action, args.identifier])
        elif args.command == "init":
            arguments.extend(
                flag
                for flag, enabled in (
                    ("--defaults", args.defaults),
                    ("--no-source", args.no_source),
                    ("--no-service", args.no_service),
                )
                if enabled
            )
        try:
            route_command(args.config, args.data_dir, arguments, managed_root=args.managed_root)
        except (OSError, ValueError) as exc:
            parser.error(getattr(exc, "code", "managed_entry_unavailable"))
    if args.command == "upgrade-channel-ownership":
        from muli.channel_format_upgrade import upgrade_channel_ownership_format

        try:
            upgraded = upgrade_channel_ownership_format(
                args.data_dir, managed_root=args.managed_root
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            parser.error(getattr(exc, "code", "store_format_upgrade_failed"))
        print(json.dumps({"upgraded": upgraded}))
    elif args.command == "init":
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
        from muli.management_cli import guarded_service_action

        manager = UserService(args.config, args.data_dir)
        if args.action == "print":
            print(manager.render())
        else:
            try:
                result = asyncio.run(
                    manager.status()
                    if args.action == "status"
                    else guarded_service_action(
                        manager, args.action, managed_root=args.managed_root
                    )
                )
            except (OSError, ValueError) as exc:
                parser.error(getattr(exc, "code", "service_action_failed"))
            print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "management":
        from muli.management_cli import management_action

        try:
            result = asyncio.run(
                management_action(
                    UserService(args.config, args.data_dir),
                    args.action,
                    managed_root=args.managed_root,
                )
            )
        except (OSError, ValueError) as exc:
            parser.error(getattr(exc, "code", "management_unavailable"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "plugins":
        try:
            if args.action == "inspect":
                from muli.plugins import inspect_plugin

                config = ConfigFile(args.config)
                result = inspect_plugin(
                    config.path.resolve().parent / "plugins",
                    args.identifier,
                    config.settings.plugins,
                )
            else:
                from muli.plugin_recovery import set_plugin_disabled

                result = asyncio.run(
                    set_plugin_disabled(
                        UserService(args.config, args.data_dir),
                        args.identifier,
                        args.action == "disable",
                        managed_root=args.managed_root,
                    )
                )
        except (OSError, ValueError, sqlite3.Error) as exc:
            parser.error(getattr(exc, "code", "plugin_operation_failed"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        from muli.notifications import notification_capability
        from muli.tools.computer import Computer

        config = ConfigFile(args.config)
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
        asyncio.run(_serve(args))


if __name__ == "__main__":
    main()
