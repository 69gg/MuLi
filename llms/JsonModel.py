import openai
from llms.providers import supported_providers, openai_get_structure_output, deepseek_get_structure_output
from config_manage.manager import ConfigManager

class JsonModel:
    def __init__(self):
        config = ConfigManager("config.json")
        self.provider_type = config.get("model_config.json_model.provider_type")
        self.model_name = config.get("model_config.json_model.model_name")
        self.client = openai.OpenAI(api_key=config.get("model_config.json_model.api_key"), base_url=config.get("model_config.json_model.api_base_url"))
    
    def get_json(self, send: str, text_format) -> dict:
        messages = [{"role": "user", "content": send}]
        if self.provider_type == "openai":
            return openai_get_structure_output(messages, text_format, self.client, self.model_name)
        elif self.provider_type == "deepseek":
            return deepseek_get_structure_output(messages, text_format, self.client, self.model_name)
        raise NotImplementedError(f"Provider type {self.provider_type} not supported yet. These providers are supported: {supported_providers}")
    