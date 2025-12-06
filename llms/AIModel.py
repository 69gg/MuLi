import openai
from typing import Callable
import asyncio
from llms.providers import supported_providers, openai_get_text_response, deepseek_get_text_response

class AIModel:
    def __init__(self, api_key: str, base_url: str, model_name: str, provider_type: str, system_prompt: str, tools: None | list[dict] = None):
        self.api_key, self.base_url, self.model_name, self.provider_type, self.system_prompt, self.tools = api_key, base_url, model_name, provider_type, system_prompt, tools
        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def _clear_reasoning_content(self):
        for message in self.messages:
            if isinstance(message, dict):
                if 'reasoning_content' in message:
                    message['reasoning_content'] = None
            elif hasattr(message, 'reasoning_content'):
                message.reasoning_content = None

    def chat(self, send: str | None, after_tool: bool = False) -> dict:
        if not after_tool:
            self._clear_reasoning_content()
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

    def _print_reasoning(self, ans, console):
        if console and hasattr(ans, 'reasoning_content') and ans.reasoning_content:
            console.print(f"[grey50]{ans.reasoning_content}[/grey50]")

    async def chat_with_tools(
        self,
        send: str,
        tool_executor: Callable[[str, str], str],
        console = None
    ) -> str:
        """
        带工具调用循环的聊天方法。
        
        Args:
            send: 用户消息
            tool_executor: 工具执行器函数，接收 (tool_name, arguments) 返回结果字符串
            console: rich console 对象，用于打印输出
        
        Returns:
            最终的 AI 回复内容
        """
        from rich.markdown import Markdown
        
        ans = self.chat(send)
        self._print_reasoning(ans, console)

        if ans.tool_calls and console and ans.content:
            console.print(Markdown(str(ans.content)))

        tool_color = "cyan dim" if self.model_name == "deepseek-reasoner" else "cyan"

        while ans.tool_calls:
            for tool_call in ans.tool_calls:
                tool_call_id = tool_call.id
                tool_name = tool_call.function.name
                arguments = tool_call.function.arguments
                
                if console:
                    console.print(f"[{tool_color}]<工具调用> {tool_name}: {arguments}[/{tool_color}]")
                
                # Check if tool_executor is async
                if asyncio.iscoroutinefunction(tool_executor):
                     result = await tool_executor(tool_name, arguments)
                else:
                     result = tool_executor(tool_name, arguments)
                
                if console:
                    console.print(f"[{tool_color}]<工具结果> {str(result).replace("\n", "")[:100]}{'...' if len(str(result)) > 100 else ''}[/{tool_color}]")
                
                self.add_tool_result(tool_call_id, result)
            ans = self.chat(send=None, after_tool=True)
            self._print_reasoning(ans, console)
        return ans.content
    