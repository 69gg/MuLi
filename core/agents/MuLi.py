from llms.AIModel import AIModel
from config_manage.manager import ConfigManager
from charset_normalizer import from_path
from core.tools import tools
from core.tools.tool_executor import execute_tool
from rich.markdown import Markdown

class MuLi:
    def __init__(self, console):
        self.console = console
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
        ans = self.ai.chat(send)
        if ans.tool_calls and self.console and ans.content:
            self.console.print(Markdown(str(ans.content)))
        while ans.tool_calls:
            for tool_call in ans.tool_calls:
                tool_call_id = tool_call.id
                tool_name = tool_call.function.name
                arguments = tool_call.function.arguments
                self.console.print(f"[cyan]<工具调用> {tool_name}: {arguments}[/cyan]")
                result = execute_tool(tool_name, arguments)
                self.console.print(f"[cyan]<工具结果> {result[:100]}{'...' if len(result) > 100 else ''}" + "[/cyan]")
                self.ai.add_tool_result(tool_call_id, result)
            ans = self.ai.chat(send=None, after_tool=True)
        return ans.content
