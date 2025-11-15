import openai
from llms.providers import supported_providers, openai_get_text_response, deepseek_get_text_response

class AIModel:
    def __init__(self, api_key: str, base_url: str, model_name: str, provider_type: str, system_prompt: str, tools: None | list[dict] = None):
        self.api_key, self.base_url, self.model_name, self.provider_type, self.system_prompt, self.tools = api_key, base_url, model_name, provider_type, system_prompt, tools
        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def chat(self, send: str | None, after_tool: bool = False) -> dict:
        if after_tool:
            if self.provider_type == "openai":
                ans =  openai_get_text_response(self.messages, self.tools, self.client, self.model_name)
            elif self.provider_type == "deepseek":
                ans =  deepseek_get_text_response(self.messages, self.tools, self.client, self.model_name)
            else:
                raise NotImplementedError(f"Provider type {self.provider_type} not supported yet. These providers are supported: {supported_providers}")
        else:
            self.messages.append({"role": "user", "content": send})
            if self.provider_type == "openai":
                ans =  openai_get_text_response(self.messages, self.tools, self.client, self.model_name)
            elif self.provider_type == "deepseek":
                ans =  deepseek_get_text_response(self.messages, self.tools, self.client, self.model_name)
            else:
                raise NotImplementedError(f"Provider type {self.provider_type} not supported yet. These providers are supported: {supported_providers}")
        self.messages.append(ans)
        return ans
    
    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})
    