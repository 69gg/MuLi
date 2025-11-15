from llms.AIModel import AIModel
from config_manage.manager import ConfigManager
from charset_normalizer import from_path
from core.tools import tools
from core.tools.tool_executor import execute_tool

class MuLi:
    def __init__(self):
        with open("core/prompts/MuLi.txt", "r", encoding=from_path("core/prompts/MuLi.txt").best().encoding) as f:
            spmp = f.read()

        config = ConfigManager("config.json")
        self.ai = AIModel(
            api_key=config.get("model_config.main_model.api_key"),
            base_url=config.get("model_config.main_model.api_base_url"),
            model_name=config.get("model_config.main_model.model_name"),
            provider_type=config.get("model_config.main_model.provider_type"),
            system_prompt=spmp,
            tools=tools
        )

    def chat(self, send: str) -> dict:
        """处理用户消息，支持工具调用"""
        ans = self.ai.chat(send)
        if ans.tool_calls:
            print(ans.content)
        # 处理工具调用
        while ans.tool_calls:
            for tool_call in ans.tool_calls:
                tool_call_id = tool_call.id
                tool_name = tool_call.function.name
                arguments = tool_call.function.arguments

                print(f"[工具调用] {tool_name}: {arguments}")

                # 执行工具函数
                result = execute_tool(tool_name, arguments)

                print(f"[工具结果] {result[:200]}{'...' if len(result) > 200 else ''}")

                # 将工具结果添加到对话历史
                self.ai.add_tool_result(tool_call_id, result)

            # 继续对话，让模型处理工具结果
            ans = self.ai.chat(send=None, after_tool=True)
            
        return ans.content
