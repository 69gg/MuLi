from core.tools.mcp_tools.mcp_tools import list_tools, client
import asyncio

tools, mcp_tool_names = asyncio.run(list_tools())
