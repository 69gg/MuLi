from rich.console import Console
from rich.markdown import Markdown
from core.agents.MuLi import MuLi
from core.tools.mcp_tools.mcp_tools import mcp_client
import asyncio

async def main():
    console = Console()

    console.print("[green]正在加载工具，请稍候...[/green]")
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
                
            response = await ml.chat(user_input)
            console.print(Markdown(response))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
