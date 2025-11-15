import openai
from response.providers import supported_providers, openai_get_text_response, openai_get_structure_output, deepseek_get_structure_output, deepseek_get_text_response

class AIModel:
    def __init__(self, api_key: str, base_url: str, model_name: str, provider_type: str):
        self.api_key, self.base_url, self.model_name, self.provider_type = api_key, base_url, model_name, provider_type
        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)

    def get_text_response(self, messages: list[dict], tools: None | list[dict]) -> dict:
        if self.provider_type == "openai":
            return openai_get_text_response(messages, tools, self.client, self.model_name)
        elif self.provider_type == "deepseek":
            return deepseek_get_text_response(messages, tools, self.client, self.model_name)
        raise NotImplementedError(f"Provider type {self.provider_type} not supported yet. These providers are supported: {supported_providers}")
    
    def get_json_output(self, messages: list[dict], text_format) -> dict:
        if self.provider_type == "openai":
            return openai_get_structure_output(messages, text_format, self.client, self.model_name)
        elif self.provider_type == "deepseek":
            return deepseek_get_structure_output(messages, text_format, self.client, self.model_name)
        raise NotImplementedError(f"Provider type {self.provider_type} not supported yet. These providers are supported: {supported_providers}")
    