# 模型协议

`muli.providers` 通过现有轻量依赖 HTTPX 发送异步 HTTP 请求，不依赖厂商 SDK，不按模型名称推断能力。三种协议均支持普通响应、SSE、函数工具、可读推理、用量和完整续轮数据。

| `protocol` | `base_url` 示例 | 追加路径 | 输出限制 |
| --- | --- | --- | --- |
| `openai_chat` | `https://api.openai.com/v1` | `/chat/completions` | `max_completion_tokens` |
| `openai_responses` | `https://api.openai.com/v1` | `/responses` | `max_output_tokens` |
| `anthropic` | `https://api.anthropic.com/v1` | `/messages` | `max_tokens` |

地址填写 API 基础地址，包含服务实际使用的版本前缀；不要填写完整接口路径，也不要将凭据放在 URL 中。密钥为空时不发送鉴权头，适用于本机无鉴权服务。

## 配置合同

工作台与 `config.json` 使用应用配置：对话模型位于 `models.main` / `reviewer` / `worker` / `vision` / `compactor` / `memory_extractor`；独立视觉模型使用 `models.vision`，其余 HTTP 服务使用 `media.embedding` / `reranker` / `asr` / `tts`。下文的 `ModelConfig`、`MediaConfig` 是 Python 模块接口，不应直接作为配置文件根字段。

应用配置额外支持 `api_key_env`，在每次创建连接配置时读取指定环境变量。环境变量名已填写但值缺失时不回退到明文 `api_key`；后台服务也必须能读取该变量，终端中临时导出的值不保证会传给 systemd、launchd 或计划任务。`temperature` / `top_p` 可直接写在模型项中，默认 `null` 表示省略；非空时优先于 `extra_body` 中同名值。

只配置主模型即可运行：未完整配置的审查、队友、压缩和记忆提炼模型沿用主模型的完整连接配置，包括协议、密钥与可选输出限制；显式完整配置的角色使用自己的设置。`model_config("vision")` 同样可解析主模型，但看图仍要求主模型已声明 `vision=true` 或另配外挂视觉，继承不会自动赋予视觉能力。embedding、reranker、ASR、TTS 是独立 HTTP 服务，不继承主模型。

`max_output_tokens` 默认 `null`：OpenAI Chat/Responses 请求省略对应输出 token 字段，由上游决定；需要自定时填写正整数。Anthropic Messages 的 `max_tokens` 必填，因此使用该协议且已填写模型和地址的配置必须显式填写正整数 `max_output_tokens`；留空并继承主模型的角色无需重复填写。保存已有显式上限时仍保留原值，不会自动清除。响应字节、工具输出与上下文预算继续生效；内部压缩和记忆提炼仍可按各自原有宿主预算限制本次输出，不改变用户默认值。

`MuLi init` 仅在选择 Anthropic 时询问必填输出上限，接受 ASCII 十进制整数，并复用模型配置的范围校验；无效输入不会创建配置。选择 OpenAI Chat/Responses 时不增加此问题，保存为 `null`，仍可稍后在工作台设置。

例如为不支持看图的主模型配置外挂视觉和 TTS，合并以下片段后替换模型、服务地址和音色，并在服务环境中提供密钥：

```json
{
  "models": {
    "vision": {
      "protocol": "openai_chat",
      "model": "填写视觉模型ID",
      "base_url": "https://provider.example/v1",
      "api_key_env": "MULI_VISION_API_KEY",
      "vision": true
    }
  },
  "media": {
    "tts": {
      "model": "填写语音模型ID",
      "base_url": "https://provider.example/v1",
      "api_key_env": "MULI_TTS_API_KEY",
      "voice": "填写供应商音色标识"
    }
  }
}
```

这些 HTTP 能力包含在基础安装中，无需安装名为 vision、voice 或 media 的 extra；本地语音唤醒仍需另外配置声学桥接，见 [语音](voice.md)。

`ModelConfig` 是可由 `dict` 构造的 dataclass。必填字段为 `protocol`、`model`、`base_url`；可选字段如下。

| 字段 | 默认值 | 行为 |
| --- | --- | --- |
| `api_key` | 空字符串 | OpenAI 使用 Bearer；Anthropic 使用 `x-api-key` |
| `max_output_tokens` | `null` | 可选总生成 token 上限；Anthropic 必须显式填写，包含厂商计入输出预算的推理 token |
| `timeout` | `60` 秒 | HTTPX 每次网络操作的超时；整轮任务另受运行时预算限制 |
| `max_retries` | `2` | 首次请求之外的最大重试次数 |
| `retry_backoff` | `0.5` 秒 | 指数退避初值；有 `Retry-After` 时优先采用，等待不超过 `timeout` |
| `extra_body` | `{}` | 厂商原生参数；受协议保护字段和输出限制校验 |
| `extra_headers` | `{}` | 附加或显式覆盖请求头，支持版本号和网关鉴权 |
| `reasoning` | `{}` | 按下表映射到当前协议的推理参数 |
| `thinking` | `{}` | 显式原生思考对象；空对象完全省略，`{"type":"disabled"}` 才发送关闭 |
| `reasoning_effort` | `null` | 可选 effort；按三种协议映射，不按模型名称猜测支持级别 |
| `vision` | `false` | 明确声明模型能读图；未声明时拒绝图片消息 |
| `stream` | `true` | 控制上游是否使用 SSE；普通响应也转换成同一种本地事件 |
| `max_response_bytes` | `8388608` | 每次响应的解压后累计字节上限，同时保护普通 JSON 和 SSE |

`to_dict()` 默认清空 `api_key` 并遮盖所有附加请求头值；只有本地持久化才使用 `to_dict(include_secrets=True)`。`repr(config)` 也隐藏密钥和附加请求头。不要将 `dataclasses.asdict(config)` 的结果直接发给界面。

| 协议 | `reasoning` 示例 | 请求字段 |
| --- | --- | --- |
| Chat | `{"effort":"high"}` | `reasoning_effort` |
| Responses | `{"effort":"high","summary":"auto"}` | 原样 `reasoning` |
| Anthropic | `{"type":"enabled","budget_tokens":1024}` | 原样 `thinking` |

新的 `thinking` 与 `reasoning_effort` 可以独立配置。例如 `thinking:{"type":"adaptive"}` 与 `reasoning_effort:"high"` 分别发送思考方式和努力程度。effort 映射为 Chat 的 `reasoning_effort`、Responses 的 `reasoning.effort`、Anthropic 的 `output_config.effort`。字符串保留供应商自定义值；本地不按模型名称自动开启思考，也不将所有供应商锁定到同一份 effort 枚举。

手动预算配置为 `thinking:{"type":"enabled","budget_tokens":2048}`。Anthropic 原生协议要求预算至少 1024 且严格小于本次生效的输出 token 上限；`adaptive`、`disabled` 应省略 `budget_tokens`。OpenAI 官方没有通用的 reasoning budget 字段，兼容服务可按其文档选择 `thinking`、Responses 的 `reasoning` 或 `extra_body` 中正确的私有参数。向 OpenAI 官方模型发送其不支持的 `thinking` 会被上游拒绝，因此无需该参数时保持 `{}`。

旧 `reasoning` 配置仍按上表兼容。新 `thinking` 和 `reasoning_effort` 优先于旧的同义配置，`extra_body` 中显式指定的原生字段优先于便捷字段，输出上限始终只能收紧。

## 扩展参数

`temperature`、`top_p`、`seed`、`stop`、`tool_choice`、`parallel_tool_calls` 等可放入 `extra_body`。参数必须由所选模型和协议实际支持；例如 Responses 没有通用 `stop`、`seed` 参数，适配器不会伪造支持或静默丢弃上游拒绝的字段。

Chat 设置输出上限时默认使用 `max_completion_tokens`；要求传统 `max_tokens` 的兼容服务可配置 `extra_body: {"max_tokens":4096}`。两种字段只能选一种。未配置上限且没有显式扩展参数时不发送 token 字段；扩展参数显式填写的值仍须为正整数。已配置 `max_output_tokens` 时，扩展参数只能收紧它，不能放大。

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

## 显式看图

`Message.images` 保存 `ImageInput` 列表，图片使用 MIME、base64 字符串与可选 `detail`，可与消息一起 JSON 序列化。只有显式传入的 user 图片消息才会编码成 Chat 的 `image_url`、Responses 的 `input_image`、Anthropic 的 `image.source`；不会抓取 URL，也不会把文件路径当成图片内容。

```python
from muli.providers import ImageInput, Message


def image_question(data: bytes, question: str) -> Message:
    image = ImageInput.from_bytes(data, "image/png", max_bytes=2_000_000)
    return Message("user", question, images=[image])
```

调用方仍须明确设置 `ModelConfig.vision=True`。普通文本任务不能因上传图片而自动带上图像；应用应将文件保存为附件，等用户或代理显式调用看图工具时，在权限检查后将字节交给下述 `MediaService.describe_image`。原生 assistant 回放内容与工具参数继续原样保留，图片不会被替换为自动生成的提示词。

`ImageInput.from_bytes` 检查非空、大小、支持的 MIME 和 base64 格式；`MediaService` 还核对 PNG/JPEG/GIF/WebP 文件头与声明的 MIME。轻量服务不解码、转码或判定动画，具体尺寸与动画支持由上游决定。

v2.56候选可显式调用 `image_describe(id=原图ID, prompt=问题, archive=true)` 保留本次完整解释。存档绑定实际发送的原图字节、提问和本次实际模型的安全快照；随后用 `attachment_search(source="vision", name_query=原图ID)` 找回工件，用 `attachment_read(id=工件ID, revision=工件SHA, offset_bytes=上一页游标)` 续读，无需再次调用视觉。普通看图不自动存档；解释是模型资料，不是主人事实或授权，详见[视觉存档](vision-archive-proposal.md)。

`image_describe` 与 `attachment_read` 的短默认结果保持；长默认结果改为工具内有界的 `description/content` 和 `truncated=true`，不再依赖 Runtime 的通用 `preview` 封套。完整回执和视觉页按最终 JSON 计入输出预算并保留在紧邻的工具续轮；之后仍可能压缩，可重新检索。原 `MediaService.describe_image` 继续返回字符串；内部 `describe_image_result` 另返回同次实际选型的 `ImageDescription`，不携带密钥、请求头或完整配置。

## 视觉、语音和检索服务

`muli.media` 复用 HTTPX，不依赖 OpenAI SDK、音频处理库、向量数据库或本地模型。`MediaConfig` 包含可选的 `vision`、`embedding`、`reranker`、`asr`、`tts`；没有配置的服务不会发起请求。模型名、地址、密钥和音色均由配置提供，不自动安装或下载模型。

视觉配置使用 `ModelConfig`。其余使用 `ServiceConfig`：必填 `model`、`base_url`；公共字段为 `api_key`、`timeout=60`、`max_input_bytes=20000000`、`max_response_bytes=8388608`、`extra_body={}`、`extra_headers={}`。`api_key_env` 在应用配置层解析，传给服务的是解析后的密钥。`to_dict()` 和 `repr` 隐藏密钥及附加请求头值。

| 方法 | 追加路径 | 配置与结果 |
| --- | --- | --- |
| `describe_image(image,mime_type,prompt)` | 所选对话协议路径 | 主模型 `vision=True` 时使用主模型，否则使用外挂 `vision`；返回描述字符串 |
| `embed(texts)` | `/embeddings` | 可选 `dimensions`；请求浮点向量，返回按输入索引排序的 `list[list[float]]` |
| `rerank(query,documents,top_n=None)` | `/rerank` | 可选 `query_instruction` 前缀；返回 `RerankResult(index,relevance_score,document)` 列表 |
| `transcribe(audio,filename,mime_type,language=None,prompt="")` | `/audio/transcriptions` | multipart 文件上传，配置 `language` 可被本次参数覆盖；返回转写文本 |
| `synthesize(text,voice=None)` | `/audio/speech` | 配置 `voice` 可被本次音色覆盖；返回 `SpeechResult(data,mime_type,extension)` |

`MediaService(config,main_model=None,client=None,max_image_bytes=10000000)` 支持 `async with` / `aclose()`，注入的 HTTPX 客户端由调用方关闭。`status()` 只报告服务是否配置及视觉路由，不返回凭据。视觉调用只使用本次问题与图片创建临时消息，图像不会自动写入主对话。示例：

```python
from muli.media import MediaConfig, MediaService, ServiceConfig
from muli.providers import ModelConfig


async def read_picture(data: bytes, vision: ModelConfig) -> str:
    async with MediaService(MediaConfig(vision=vision)) as media:
        return await media.describe_image(data, "image/png", "列出图片中的文字")


async def speak(text: str, settings: ServiceConfig) -> bytes:
    async with MediaService(MediaConfig(tts=settings)) as media:
        result = await media.synthesize(text)
        return result.data
```

embedding 验证返回条数、索引、有限数值和一致维度，结果按输入顺序返回，不生成伪造向量。reranker 沿用 Undefined 的兼容请求格式：`model/query/documents/top_n/return_documents:false`，接受 `results`、`data` 或顶层数组，分数支持 `relevance_score`、`score`、`similarity`，包括有限的数字字符串。缺少 `index` 时按输入顺序解释；负数、越界、重复索引、非有限分数或空结果明确失败，文档始终取原始输入。`/rerank` 是第三方 Cohere 风格的兼容接口，并非 OpenAI 官方 API；具体版本前缀和参数支持应按服务商文档配置。

更换embedding服务后，原资料、事实和关系保留；查询只使用当前模型标识的向量，不把旧
向量直接交给新模型。v2.68开发候选支持由主人明确选择已有完成来源，沿原单worker
后台重建向量，不重复提炼或自动全库运行；用法、取消与预算边界见[语义索引接续](memory-reindex.md)。

ASR 默认请求 JSON，支持 `extra_body.response_format` 选择 `json`、`verbose_json`、`diarized_json`、`text`、`srt`、`vtt`，返回对应文本；其他原生选项可写入 `extra_body`，数组转为 multipart 的 `key[]` 字段，嵌套对象序列化为 JSON。文件名只发送 basename，不发送本地目录。`ServiceConfig.response_format` 专用于 TTS。

可选网页录音复用 `media.asr`，以认证的 `/api/media/transcribe` 接口生成草稿；不调用主模型或自动提交对话。启用、格式探测、取消与资源限制见 [网页点击录音](voice.md#网页点击录音)。

TTS 要求明确配置 `voice` 字符串，保留供应商音色标识，不硬编码可选音色清单。`response_format` 支持 `mp3`（默认）、`opus`、`aac`、`flac`、`wav`、`pcm`；`speed:null` 省略语速，数值范围 0.25–4；`instructions:""` 省略语气指导。附加原生参数走 `extra_body`。返回只包含音频数据和文件类型信息，保存、播放、发送文件由应用负责；PCM 是原始样本数据，服务不自动封装 WAV。JSON/HTML、空音频或与所选格式冲突的响应类型会明确失败。

媒体服务不自动重试，请求和响应分别受字节限制，整个 HTTP 调用（包括缓慢分块响应）受总超时限制；取消向外传播并关闭响应。视觉调用整体同样受所选模型的 `timeout` 限制。HTTP 重定向不跟随，错误仅暴露安全原因和状态码，不回显上游正文。`extra_body` 不能覆盖模型、输入、文件、文档、查询、音色、鉴权等本次操作的核心字段；附加请求头禁止覆盖 Host、Content-Type、Content-Length、Transfer-Encoding，避免破坏 multipart 或请求边界。

`Usage` 含 input/output/reasoning/cached token 计数以及原始用量。Anthropic 的 input 统一包含普通输入、缓存读取和缓存创建 token；没有单列的推理 token 返回 `0`，不做估算。

可注入 `httpx.AsyncClient` 复用连接或 `MockTransport`。注入的客户端由调用方关闭；协议层创建的客户端由 `async with` 或 `aclose()` 关闭。提前结束读取流时应关闭异步迭代器。

## 失败与验证范围

网络异常、408/409/425/429/5xx 和明确的流式过载错误仅在没有向调用方发出任何增量时重试。已经输出文本、推理或工具增量后发生错误，会抛出 `ProviderError(partial=True)`，不会重新请求。缺少终止标记不视为成功；超出字节上限直接失败。HTTP 错误只暴露状态码，不回显上游正文、密钥或请求头。HTTP 重定向不自动跟随。

`tests/test_providers.py` 和 `tests/test_media.py` 使用 MockTransport 验证三协议请求、图片与原生工具续轮、可选思考参数、思维签名、增量参数、用量、断流重试边界、multipart、向量与重排解析、音色与格式、输入输出限制、总超时、取消和凭据遮盖。测试不调用真实付费模型、不下载模型、不播放音频，不能证明每个第三方兼容服务都支持全部扩展字段。

接口依据：[OpenAI Chat streaming](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)、[OpenAI Responses streaming](https://developers.openai.com/api/reference/resources/responses/streaming-events)、[Anthropic 官方内容块定义](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/thinking_block_param.py)、[HTTPX 异步客户端](https://www.python-httpx.org/async/)。

新增接口按 Context7 检索到的官方资料核对：[OpenAI 图片输入](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[OpenAI Responses](https://developers.openai.com/api/reference/resources/responses/methods/create)、[转写](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)、[语音合成](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)、[embeddings](https://developers.openai.com/api/reference/resources/embeddings/methods/create)、[Anthropic thinking 配置类型](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/thinking_config_param.py)、[Cohere rerank 官方规格](https://github.com/cohere-ai/cohere-developer-experience/blob/main/cohere-openapi.yaml)。
