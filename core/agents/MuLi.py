from llms.AIModel import AIModel
from config_manage.manager import ConfigManager
from charset_normalizer import from_path
from core.tools import tools
from core.tools.tool_executor import execute_tool
from core.utils.logger import DisplayLogger

import tiktoken
import json
import os
import time
import asyncio

class MuLi:
    def __init__(self, console):
        self.console = console
        self.logger = DisplayLogger()
        with open("core/prompts/MuLi.txt", "r", encoding=from_path("core/prompts/MuLi.txt").best().encoding) as f:
            spmp = f.read()

        self.config = ConfigManager("config.json")
        self.max_context_tokens = self.config.get("model_config.max_context_tokens", 8000)
        self.ai = AIModel(
            api_key=self.config.get("model_config.main_model.api_key"),
            base_url=self.config.get("model_config.main_model.api_base_url"),
            model_name=self.config.get("model_config.main_model.model_name"),
            provider_type=self.config.get("model_config.main_model.provider_type"),
            system_prompt=spmp,
            tools=tools
        )
        self.ai.logger = self.logger # Inject logger into AIModel
        
        try:
            self.encoding = tiktoken.encoding_for_model(self.ai.model_name)
        except KeyError:
            self.encoding = tiktoken.get_encoding("cl100k_base")

        self.history_dir = "history"
        os.makedirs(self.history_dir, exist_ok=True)
        self.session_file = os.path.join(self.history_dir, "dialog.json")
        
        # Try to restore latest session
        self._restore_session()

    def _restore_session(self):
        """Attempts to restore the session from dialog.json and replay display log."""
        try:
            # Restore dialog history
            if os.path.exists(self.session_file):
                with open(self.session_file, "r", encoding="utf-8") as f:
                    messages = json.load(f)
                if messages:
                    self.ai.messages = messages
                    self.console.print(f"[dim]已恢复对话历史: {self.session_file}[/dim]")
            
            # Replay display log from loaded logger entries
            if self.logger.entries:
                 self._replay_display_log(self.logger.entries)

        except Exception as e:
            self.console.print(f"[red]恢复会话失败: {e}[/red]")

    def _replay_display_log(self, logs: list):
        """Replays the display log to the console."""
        try:
            from rich.markdown import Markdown
            
            self.console.print("[dim]--- 历史会话回放开始 ---[/dim]")
            for entry in logs:
                role = entry.get("role")
                content = entry.get("content")
                type_ = entry.get("type", "text")
                
                if type_ == "markdown":
                    self.console.print(Markdown(str(content)))
                elif type_ == "tool":
                    tool_color = "cyan" # Default
                    self.console.print(f"[{tool_color}]{content}[/{tool_color}]")
                else: # text
                    if role == "user":
                        self.console.print(f"> {content}")
                    elif role == "system":
                         self.console.print(f"[yellow]{content}[/yellow]")
                    else:
                        self.console.print(content)
            self.console.print("[dim]--- 历史会话回放结束 ---[/dim]")
            
        except Exception as e:
            self.console.print(f"[red]回放显示记录失败: {e}[/red]")


    def _count_tokens(self, messages: list[dict]) -> int:
        """计算消息列表的 token 数量"""
        encoding = self.encoding
        
        num_tokens = 0
        for message in messages:
            num_tokens += 4  # every message follows <im_start>{role/name}\n{content}<im_end>\n
            for key, value in message.items():
                if key == "reasoning_content": continue # Skip reasoning for token count if intended, or keep it? User wants to exclude it from summary, not necessarily count. But usually CoT is not in context window for *next* turn if we summarized. 
                # Let's count it for now as it IS in context until summarized.
                if isinstance(value, str):
                    num_tokens += len(encoding.encode(value))
                elif isinstance(value, list): # For tool calls etc
                     num_tokens += len(encoding.encode(str(value)))
        num_tokens += 2  # every reply is primed with <im_start>assistant
        return num_tokens

    async def _summarize_conversation(self):
        """总结对话历史并压缩"""
        self.console.print("[yellow]正在压缩对话历史...[/yellow]")
        
        try:
            with open("core/prompts/summary.txt", "r", encoding="utf-8") as f:
                summary_prompt = f.read()
        except Exception:
            summary_prompt = "请简要总结上述对话的关键信息、用户需求以及你已完成的任务。保持关键上下文，忽略无关细节。尽量保持简明扼要。你的总结将作为system提示，在你的上下文窗口不足的时候用于提示。"

        # Filter out reasoning_content and existing summaries
        SUMMARY_MARKER = "[SUMMARY_CONTEXT]"
        messages_to_summarize = []
        for msg in self.ai.messages[1:]: # Skip initial system prompt
             # Deep copy to avoid modifying original during iteration if we were strictly copying, but here we just build new dicts
             new_msg = msg.copy()
             if "reasoning_content" in new_msg:
                 del new_msg["reasoning_content"]
             
             # Skip previous summaries to avoid infinite recursion or double summary if not handled by marker replacement yet
             # Actually, we want to summarize everything INCLUDING previous summary if we are moving the window forward?
             # Usually yes. But if we replace the WHOLE history with [System, Summary], then the previous summary is effectively 'consumed' into the new one.
             # So we can just summarize the current state.
             
             # Remove internal marker if present in content (though it should be in system prompt)
             if isinstance(new_msg.get("content"), str):
                 new_msg["content"] = new_msg["content"].replace(SUMMARY_MARKER, "")
             
             messages_to_summarize.append(new_msg)

        summary_messages = [{"role": "system", "content": "You are a helpful assistant."}] + messages_to_summarize + [{"role": "user", "content": summary_prompt}]
        
        try:
             # Use the generic generate_response method
            summary_text = self.ai.generate_response(summary_messages)

            # summary_text is already a string from generate_response

            # Fix: Access content via attribute .content, not dict item ['content']
            new_system_context = f"{SUMMARY_MARKER} Previous conversation summary: {summary_text}"
            
            # Reconstruct messages: 
            # 1. Original System Prompt
            # 2. New Summary (with marker)
            # 3. (Optional) Keep last K messages? 
            # The prompt says "replace original conversation", implying aggressive compression.
            # But usually we keep the last user message if we are "in loop"? 
            # "Detect if close to max_tokens... summarize... replace original conversation". 
            # If we are at the end of a turn, "original conversation" is the whole history.
            # So [System, NewSummary] is a valid strict state.
            
            self.ai.messages = [
                {"role": "system", "content": self.ai.system_prompt},
                {"role": "system", "content": new_system_context}
            ]
            
            self.console.print(f"[green]对话压缩完成。当前 Token: {self._count_tokens(self.ai.messages)}[/green]")
            self.console.print(f"[dim]摘要内容: {summary_text}[/dim]")
            self.logger.log("system", f"对话已压缩: {summary_text}", type="text")
            
        except Exception as e:
            self.console.print(f"[red]压缩对话失败: {e}[/red]")


    def _save_history(self):
        """保存对话历史"""
        try:
            with open(self.session_file, "w", encoding="utf-8") as f:
                json.dump(self.ai.messages, f, ensure_ascii=False, indent=2)
            # self.console.print(f"[dim]对话已保存到 {self.session_file}[/dim]")
        except Exception as e:
            self.console.print(f"[red]保存历史失败: {e}[/red]")

    async def chat(self, send: str) -> dict:
        response = await self.ai.chat_with_tools(
            send,
            tool_executor=execute_tool,
            console=self.console
        )
        
        # Post-chat processing
        try:
            current_tokens = self._count_tokens(self.ai.messages)
            # self.console.print(f"[dim]Current tokens: {current_tokens}/{self.max_context_tokens}[/dim]")
            
            if current_tokens > self.max_context_tokens:
                self.console.print(f"[yellow]Token 数量 ({current_tokens}) 超过限制 ({self.max_context_tokens})，触发压缩...[/yellow]")
                await self._summarize_conversation()
            
            self._save_history()
            
        except Exception as e:
            self.console.print(f"[red]对话后处理错误: {e}[/red]")

        return response

