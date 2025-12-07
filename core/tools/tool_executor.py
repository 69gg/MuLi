import importlib
import os
import json
import asyncio
import logging
from core.tools import mcp_tool_names, execute_mcp_tools

# Configure logger
logger = logging.getLogger(__name__)

_TOOL_CACHE = {}

def _load_tools():
    """Scans and loads all tools from py_tools directory into cache."""
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        py_tools_dir = os.path.join(current_dir, "py_tools")
        
        if not os.path.exists(py_tools_dir):
            logger.warning(f"py_tools directory not found: {py_tools_dir}")
            return

        for filename in os.listdir(py_tools_dir):
            if filename.endswith(".py") and filename != "__init__.py":
                module_name = filename[:-3]
                module_path = f"core.tools.py_tools.{module_name}"

                try:
                    module = importlib.import_module(module_path)
                    # Register all functions in the module that don't start with _
                    for attr_name in dir(module):
                        if not attr_name.startswith("_"):
                            attr = getattr(module, attr_name)
                            # Only register callables defined in the module itself to avoid registering imports
                            if callable(attr) and getattr(attr, "__module__", None) == module.__name__:
                                _TOOL_CACHE[attr_name] = attr
                                
                except Exception as e:
                    logger.error(f"Failed to import tool module {module_name}: {e}")
                    continue
    except Exception as e:
        logger.error(f"Error loading tools: {e}")

# Load tools on import
_load_tools()

async def execute_tool(tool_name: str, arguments) -> str:
    try:
        # Normalize arguments
        if isinstance(arguments, str):
            try:
                args_dict = json.loads(arguments)
            except json.JSONDecodeError:
                return f"错误：参数格式不正确，需要 JSON 字符串"
        else:
            args_dict = arguments

        # 1. Check MCP Tools
        if tool_name in mcp_tool_names:
            return await execute_mcp_tools(tool_name, args_dict)

        # 2. Check Python Tools (Cached)
        if tool_name in _TOOL_CACHE:
            tool_func = _TOOL_CACHE[tool_name]
            try:
                if asyncio.iscoroutinefunction(tool_func):
                    result = await tool_func(**args_dict)
                else:
                    result = tool_func(**args_dict)
                return str(result)
            except TypeError as te:
                 return f"错误：工具 '{tool_name}' 参数不匹配: {str(te)}"
            except Exception as e:
                return f"错误：执行工具 '{tool_name}' 时发生异常: {str(e)}"

        return f"错误：未找到工具函数 '{tool_name}'"

    except Exception as e:
        return f"错误：执行工具系统异常: {str(e)}"
