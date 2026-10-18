# 模型协议

`muli.providers` 通过现有轻量依赖 HTTPX 发送异步 HTTP 请求，不依赖厂商 SDK，不按模型名称推断能力。三种协议均支持普通响应、SSE、函数工具、可读推理、用量和完整续轮数据。

| `protocol` | `base_url` 示例 | 追加路径 | 输出限制 |
| --- | --- | --- | --- |
| `openai_chat` | `https://api.openai.com/v1` | `/chat/completions` | `max_completion_tokens` |
| `openai_responses` | `https://api.openai.com/v1` | `/responses` | `max_output_tokens` |
| `anthropic` | `https://api.anthropic.com/v1` | `/messages` | `max_tokens` |

地址填写 API 基础地址，包含服务实际使用的版本前缀；不要填写完整接口路径，也不要将凭据放在 URL 中。密钥为空时不发送鉴权头，适用于本机无鉴权服务。

## 配置合同

`ModelConfig` 是可由 `dict` 构造的 dataclass。必填字段为 `protocol`、`model`、`base_url`；可选字段如下。

| 字段 | 默认值 | 行为 |
| --- | --- | --- |
| `api_key` | 空字符串 | OpenAI 使用 Bearer；Anthropic 使用 `x-api-key` |
| `max_output_tokens` | `4096` | 总生成 token 上限，包含厂商计入输出预算的推理 token |
| `timeout` | `60` 秒 | HTTPX 每次网络操作的超时；整轮任务另受运行时预算限制 |
| `max_retries` | `2` | 首次请求之外的最大重试次数 |
| `retry_backoff` | `0.5` 秒 | 指数退避初值；有 `Retry-After` 时优先采用，等待不超过 `timeout` |
| `extra_body` | `{}` | 厂商原生参数；受协议保护字段和输出限制校验 |
| `extra_headers` | `{}` | 附加或显式覆盖请求头，支持版本号和网关鉴权 |
| `reasoning` | `{}` | 按下表映射到当前协议的推理参数 |
| `stream` | `true` | 控制上游是否使用 SSE；普通响应也转换成同一种本地事件 |
| `max_response_bytes` | `8388608` | 每次响应的解压后累计字节上限，同时保护普通 JSON 和 SSE |

`to_dict()` 默认清空 `api_key` 并遮盖所有附加请求头值；只有本地持久化才使用 `to_dict(include_secrets=True)`。`repr(config)` 也隐藏密钥和附加请求头。不要将 `dataclasses.asdict(config)` 的结果直接发给界面。

| 协议 | `reasoning` 示例 | 请求字段 |
| --- | --- | --- |
| Chat | `{"effort":"high"}` | `reasoning_effort` |
| Responses | `{"effort":"high","summary":"auto"}` | 原样 `reasoning` |
| Anthropic | `{"type":"enabled","budget_tokens":1024}` | 原样 `thinking` |

不同模型接受的推理选项由上游决定。例如 Anthropic adaptive thinking 可配置为 `{"type":"adaptive"}`，effort 放入 `extra_body.output_config`。Chat 的厂商自定义 `thinking` 等参数放入 `extra_body`。本地不会根据模型名自动开启思考。

## 扩展参数

`temperature`、`top_p`、`seed`、`stop`、`tool_choice`、`parallel_tool_calls` 等可放入 `extra_body`。参数必须由所选模型和协议实际支持；例如 Responses 没有通用 `stop`、`seed` 参数，适配器不会伪造支持或静默丢弃上游拒绝的字段。

Chat 默认使用 `max_completion_tokens`；要求传统 `max_tokens` 的兼容服务可配置 `extra_body: {"max_tokens":4096}`。两种字段只能选一种。扩展参数的 token 限制可收紧 `max_output_tokens`，不能放大它。

工具声明统一使用 Chat function schema。选择具体函数时，`tool_choice` 可以使用 `{"type":"function","function":{"name":"工具名"}}`，适配器转换为 Responses 和 Anthropic 原生格式。Anthropic 将 `required` 转换为 `any`，`stop` 转换为 `stop_sequences`，`parallel_tool_calls` 转换为 `tool_choice.disable_parallel_tool_use`。

`extra_body` 禁止覆盖 `model`、`messages`、`input`、`tools`、`stream`、`system`、`instructions`、`api_key`、`auth`、`headers`、`extra_headers`、`base_url`、`timeout`、`previous_response_id`、`conversation`、`n`。这保证请求使用当前会话的提示词、工具和完整本地历史；需要鉴权扩展时使用 `extra_headers`。

## 运行时接线

```python
from muli.providers import Message, ModelClient, ModelConfig


async def answer(settings: ModelConfig, question: str) -> str:
    async with ModelClient(settings) as client:
        turn = await client.complete([Message("user", question)])
        return turn.content
```

`complete(messages, tools=())` 返回 `ModelTurn`。`stream(messages, tools=())` 依次产生 `StreamEvent`，类型为 `text_delta`、`reasoning_delta`、`tool_call_delta` 和 `done`。最后一个事件的 `turn` 是最终回合。工具增量的 id/name 是当前累计值，arguments 是本次片段；工具执行必须等待 `done`，读取 `turn.tool_calls` 中完整的 `ToolCall(id,name,arguments)`。

每次工具续轮将 `turn.to_message()` 和对应 `Message("tool", result, tool_call_id=call.id)` 追加到历史。不要根据显示文本重新拼接 assistant 消息，也不要修改它的 `provider_data`：

- Chat 保留原始 assistant 消息，包括 `reasoning_content`、`reasoning_details` 和厂商元数据。
- Responses 使用完整本地 input 回放，默认 `store:false`，保存原生输出项；流式回放优先使用 `response.output_item.done` 中完整的推理密文，避免使用开始事件的半截签名。
- Anthropic 保留 thinking、signature、redacted_thinking、tool_use 等内容块及原始顺序，连续工具结果合并为下一条 user 消息中的多个 tool_result。

`Usage` 含 input/output/reasoning/cached token 计数以及原始用量。Anthropic 的 input 统一包含普通输入、缓存读取和缓存创建 token；没有单列的推理 token 返回 `0`，不做估算。

可注入 `httpx.AsyncClient` 复用连接或 `MockTransport`。注入的客户端由调用方关闭；协议层创建的客户端由 `async with` 或 `aclose()` 关闭。提前结束读取流时应关闭异步迭代器。

## 失败与验证范围

网络异常、408/409/425/429/5xx 和明确的流式过载错误仅在没有向调用方发出任何增量时重试。已经输出文本、推理或工具增量后发生错误，会抛出 `ProviderError(partial=True)`，不会重新请求。缺少终止标记不视为成功；超出字节上限直接失败。HTTP 错误只暴露状态码，不回显上游正文、密钥或请求头。HTTP 重定向不自动跟随。

`tests/test_providers.py` 使用 MockTransport 验证三协议请求、原生工具回放、思维签名、增量参数、用量、断流重试边界、参数保护、输出限制和凭据遮盖。测试不调用真实付费模型，不能证明每个第三方兼容服务都支持全部扩展字段。

接口依据：[OpenAI Chat streaming](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)、[OpenAI Responses streaming](https://developers.openai.com/api/reference/resources/responses/streaming-events)、[Anthropic 官方内容块定义](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/thinking_block_param.py)、[HTTPX 异步客户端](https://www.python-httpx.org/async/)。
