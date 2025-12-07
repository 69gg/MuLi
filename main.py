from rich.console import Console
console = Console()
console.print("[green]正在加载工具，请稍候...[/green]")

from rich.markdown import Markdown
from core.agents.MuLi import MuLi
from core.tools.mcp_tools.mcp_tools import mcp_client
import asyncio
import os
import shutil

async def main():
    ml = MuLi(console=console)
    console.print("[green]加载完成！[/green]")

    # Use persistent MCP client context
    async with mcp_client:
        while True:
            # Use to_thread to avoid blocking the event loop with input(), 
            # allowing background tasks (like MCP keepalives) to run.
            try:
                user_input = await asyncio.to_thread(input, "> ")
            except EOFError:
                break

            # Command handling
            if user_input.startswith("/"):
                command = user_input.strip()
                if command == "/exit":
                    console.print("[yellow]再见！[/yellow]")
                    break
                elif command == "/clear-history":
                    try:
                        if os.path.exists(ml.history_dir):
                            shutil.rmtree(ml.history_dir)
                            console.print(f"[green]已清空历史记录 ({ml.history_dir})[/green]")
                        else:
                            console.print("[yellow]历史记录目录不存在[/yellow]")
                    except Exception as e:
                        console.print(f"[red]清空历史失败: {e}[/red]")
                    break # Exit after clearing history as per requirement
                elif command == "/help":
                    help_text = """
# 可用命令

- `/exit` : 退出程序
- `/clear-history` : 清空聊天记录并退出
- `/help` : 显示此帮助信息
"""
                    console.print(Markdown(help_text))
                    continue
                else:
                    console.print(f"[red]未知命令: {command}[/red]")
                    continue

            response = await ml.chat(user_input)
            console.print(Markdown(response))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
