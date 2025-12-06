from config_manage.manager import ConfigManager
from fastmcp import Client
import copy

config_f = ConfigManager()
config = config_f.get("mcp_tools")
client = Client(config)

def fastmcp_to_openai_tools(fastmcp_tools):
    """
    将 FastMCP 工具列表转换为 OpenAI Chat Completions API 所需的 tools 格式。
    
    Args:
        fastmcp_tools (list): 包含 FastMCP Tool 对象或字典的列表
        
    Returns:
        list: OpenAI 格式的工具列表 [{'type': 'function', 'function': {...}}, ...]
    """
    openai_tools = []

    for tool in fastmcp_tools:
        def get_attr(obj, key):
            if isinstance(obj, dict):
                return obj.get(key)
            return getattr(obj, key, None)

        name = get_attr(tool, 'name')
        description = get_attr(tool, 'description')
        raw_schema = get_attr(tool, 'inputSchema')

        if raw_schema:
            # 深拷贝以避免修改原始对象
            parameters = copy.deepcopy(raw_schema)
            
            # 移除 OpenAI API 不支持的 JSON Schema 元数据字段
            if '$schema' in parameters:
                del parameters['$schema']
        else:
            # 如果没有参数定义，提供一个空的 object
            parameters = {"type": "object", "properties": {}}

        tool_def = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters
            }
        }
        openai_tools.append(tool_def)
    return openai_tools


async def list_tools() -> tuple[list[dict], list[str]]:
    async with client:
        fastmcp_tools = await client.list_tools()
        tools = fastmcp_to_openai_tools(fastmcp_tools)
        tool_names = [tool.name if hasattr(tool, 'name') else tool['name'] for tool in fastmcp_tools]
        #print(tools)
        #print(tool_names)
        return (tools, tool_names)

