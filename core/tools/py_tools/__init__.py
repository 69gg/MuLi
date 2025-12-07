import os
import re
import ast
import importlib
import inspect

def generate_openai_tools():
    tools = []
    current_dir = os.path.dirname(os.path.abspath(__file__))

    for filename in os.listdir(current_dir):
        if not filename.endswith(".py") or filename == "__init__.py":
            continue

        filepath = os.path.join(current_dir, filename)
        module_name = f"core.tools.py_tools.{filename[:-3]}"

        try:
            # 导入模块
            module = importlib.import_module(module_name)

            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            # 1. 提取整个工具定义块
            definition_match = re.search(
                r"## -!- START TOOL DEFINITION -!- ##(.*?)## -!- END TOOL DEFINITION -!- ##",
                content,
                re.DOTALL
            )
            if not definition_match:
                continue

            tool_definition_str = definition_match.group(1).strip()

            parsed_ast = ast.parse(tool_definition_str)

            tool_info = {}
            for node in parsed_ast.body:
                if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                    var_name = node.targets[0].id
                    if var_name in ["TOOL_NAME", "TOOL_DESCRIPTION", "TOOL_FUNCTIONS", "TOOL_PARAMETERS"]:
                        tool_info[var_name] = ast.literal_eval(node.value)

            required_keys = {"TOOL_NAME", "TOOL_DESCRIPTION", "TOOL_FUNCTIONS", "TOOL_PARAMETERS"}
            if not required_keys.issubset(tool_info.keys()):
                continue

            tool_description = tool_info["TOOL_DESCRIPTION"]

            # Special handling for shell_for_ai to inject mount_mapping
            if tool_info.get("TOOL_NAME") == "shell_for_ai":
                try:
                    from config_manage.manager import ConfigManager
                    cfg_mgr = ConfigManager("config.json")
                    mapping = cfg_mgr.get("tools_api_config.shell_for_ai.mount_mapping")
                    if mapping:
                        tool_description += f"\n\nEnvironment Info: Host-Container Mount Mapping: {mapping}"
                except Exception:
                    pass

            tool_functions = tool_info["TOOL_FUNCTIONS"]
            tool_parameters = tool_info["TOOL_PARAMETERS"]

            for i, func_name in enumerate(tool_functions):
                parameters_list = tool_parameters[i]

                # 动态提取参数信息
                properties = {}
                required = []

                if hasattr(module, func_name):
                    func = getattr(module, func_name)
                    sig = inspect.signature(func)

                    # 创建一个参数名到描述的映射
                    param_desc_map = {}
                    for param_dict in parameters_list:
                        for param_name, param_desc in param_dict.items():
                            param_desc_map[param_name] = param_desc

                    # 遍历函数签名中的参数
                    for param_name, param in sig.parameters.items():
                        # 跳过self参数
                        if param_name == 'self':
                            continue

                        # 获取参数描述
                        param_desc = param_desc_map.get(param_name, "")

                        # 确定参数类型
                        param_type = "string"  # 默认类型
                        if param.annotation != inspect.Parameter.empty:
                            annot = param.annotation
                            if annot == int:
                                param_type = "integer"
                            elif annot == float:
                                param_type = "number"
                            elif annot == bool:
                                param_type = "boolean"
                            elif annot == list or str(annot).startswith("list"):
                                param_type = "array"
                            elif annot == dict or str(annot).startswith("dict"):
                                param_type = "object"

                        properties[param_name] = {
                            "type": param_type,
                            "description": param_desc
                        }

                        # 如果参数没有默认值，则标记为required
                        if param.default == inspect.Parameter.empty:
                            required.append(param_name)
                else:
                    # 如果函数不存在，回退到旧的逻辑
                    for param_dict in parameters_list:
                        for param_name, param_desc in param_dict.items():
                            properties[param_name] = {
                                "type": "string",
                                "description": param_desc
                            }
                            required.append(param_name)

                tool_formatted = {
                    "type": "function",
                    "function": {
                        "name": func_name,
                        "description": tool_description,
                        "parameters": {
                            "type": "object",
                            "properties": properties,
                            "required": required,
                        },
                    },
                }
                tools.append(tool_formatted)

        except Exception:
            continue

    return tools

tools = generate_openai_tools()
