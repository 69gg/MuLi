from core.tools.py_tools import tools as py_tools_list
from core.tools.mcp_tools import tools as mcp_tools_list, mcp_tool_names
from config_manage.manager import ConfigManager
from fastmcp import Client

config_f = ConfigManager()
config = config_f.get("mcp_tools")
tools = py_tools_list + mcp_tools_list
client = Client(config)

async def execute_mcp_tools(tool_name: str, arguments) -> str:
    async with client:
        result = await client.call_tool(tool_name, arguments)
        # 将 CallToolResult 对象转换为字符串
        if hasattr(result, 'content'):
            # MCP 的 CallToolResult 通常有 content 属性，可能是 TextContent 对象的列表
            content_parts = []
            for content_item in result.content:
                if hasattr(content_item, 'text'):
                    content_parts.append(content_item.text)
                elif hasattr(content_item, 'type') and content_item.type == 'text':
                    content_parts.append(str(content_item))
                else:
                    content_parts.append(str(content_item))
            return '\n'.join(content_parts)
        else:
            return str(result)
    