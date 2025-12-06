from llms.AIModel import AIModel
from config_manage.manager import ConfigManager
from charset_normalizer import from_path
from core.tools import tools
from core.tools.tool_executor import execute_tool


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
        return self.ai.chat_with_tools(
            send,
            tool_executor=execute_tool,
            console=self.console
        )

