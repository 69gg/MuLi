import importlib
import os
import json
from core.tools import mcp_tool_names, execute_mcp_tools
import asyncio

def execute_tool(tool_name: str, arguments) -> str:
    try:
        args_dict = json.loads(arguments)
        if tool_name in mcp_tool_names:
            return asyncio.run(execute_mcp_tools(tool_name, args_dict))
    except json.JSONDecodeError:
        return f"错误：参数格式不正确"
    except Exception as e:
        return f"错误：执行工具时发生异常: {str(e)}"

    try:

        current_dir = os.path.dirname(os.path.abspath(__file__))
        py_tools_dir = os.path.join(current_dir, "py_tools")

        for filename in os.listdir(py_tools_dir):
            if filename.endswith(".py") and filename != "__init__.py":
                module_name = filename[:-3]
                module_path = f"core.tools.py_tools.{module_name}"

                try:
                    module = importlib.import_module(module_path)

                    if hasattr(module, tool_name):
                        tool_func = getattr(module, tool_name)
                        result = tool_func(**args_dict)
                        return str(result)
                except Exception:
                    continue

        return f"错误：未找到工具函数 '{tool_name}'"
    except json.JSONDecodeError:
        return f"错误：参数格式不正确"
    except Exception as e:
        return f"错误：执行工具时发生异常: {str(e)}"
