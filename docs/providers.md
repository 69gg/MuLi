# 配置模型与媒体服务

在工作台设置中填写模型，也可以通过 `MuLi init` 创建初始配置。先配置主模型，再按需要添加独立的队友、视觉或媒体服务。接口兼容性与模型实际能力取决于所选供应商，不会仅凭模型名称自动识别。

## 选择协议与地址

| 协议 | `protocol` | 基础地址示例 | 沐璃追加的接口路径 |
| --- | --- | --- | --- |
| OpenAI Chat Completions | `openai_chat` | `https://api.openai.com/v1` | `/chat/completions` |
| OpenAI Responses | `openai_responses` | `https://api.openai.com/v1` | `/responses` |
| Anthropic Messages | `anthropic` | `https://api.anthropic.com/v1` | `/messages` |

填写服务的 **API 基础地址**，包含它实际需要的版本前缀，不要填写完整接口路径。兼容服务应选择它真正支持的协议，不一定与模型厂商同名。

填写准确的模型 ID 与 API 密钥。不要把密钥放进 URL；无鉴权的本机服务可以留空密钥。

也可以用 `api_key_env` 指定环境变量名。此项非空时从该变量取值，变量缺失不会退回明文 `api_key`。后台服务必须能读取该变量，在终端临时导出不保证会传给系统服务。

## 主模型与角色继承

对话模型配置在 `models` 下：

| 配置项 | 用途 |
| --- | --- |
| `main` | 主沐璃，理解需求并统筹工作 |
| `worker` | 执行队友任务 |
| `reviewer` | 审查需要模型判断的工具请求 |
| `compactor` | 压缩较长的上下文 |
| `memory_extractor` | 在后台整理长期记忆 |
| `vision` | 独立的图片理解模型 |

只完整配置主模型即可开始。审查、队友、压缩和记忆提炼角色未同时填写模型与地址时，继承主模型的完整连接配置；不是逐字段混用两套配置。完整配置某个角色后，它使用自己的设置。

继承连接不会增加能力或权限。看图仍要求主模型明确设置 `vision=true`，或配置可用的独立视觉模型；审查模型也不会因为继承主模型而获得主助手的工具上下文。

embedding、reranker、ASR 和 TTS 是独立服务，不继承主模型。

## 输出上限与常用参数

`max_output_tokens` 默认留空（配置文件中为 `null`）。OpenAI Chat 和 Responses 在未设置时不发送输出 token 上限，由上游决定。需要限制时可填写 **128–131072 的整数**，同时应符合供应商对该模型的要求。

Anthropic Messages 必须提供 `max_tokens`，因此已完整配置的 Anthropic 角色必须显式填写 `max_output_tokens`。留空继承主模型的角色不必重复填写。`MuLi init` 仅在选择 Anthropic 时询问这个必填值；OpenAI 的初始设置默认不配置它。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `temperature` | 留空 | 不发送此参数；设置后须在 0–2 之间 |
| `top_p` | 留空 | 不发送此参数；设置后须在 0–1 之间 |
| `stream` | `true` | 使用流式响应；关闭后仍能显示完成结果 |
| `vision` | `false` | 是否明确声明该模型可以接收图片 |
| `timeout` | 60 秒 | 网络操作超时；任务本身还有独立的运行限制 |
| `max_retries` | 2 | 可安全重试阶段的最大额外请求次数 |
| `retry_backoff` | 0.5 秒 | 重试退避的起始等待时间 |
| `max_response_bytes` | 8388608 | 单次响应的字节上限 |

供应商可能不支持某些采样参数，留空即可省略。已经输出部分正文、思考或工具调用的流不会自动从头重试，以免混合两次结果。连接中断或响应未完整结束不等于操作成功。

不配置用户输出上限，不代表无限制：上下文、响应大小、工具输出，以及压缩和记忆提炼各自的处理限制仍然有效。

## 思考与高级参数

按模型文档配置 `reasoning`、`thinking` 或 `reasoning_effort`。不要仅因为模型名称里含有某个词就打开思考。

| 参数 | OpenAI Chat | OpenAI Responses | Anthropic Messages |
| --- | --- | --- | --- |
| `reasoning_effort` | `reasoning_effort` | `reasoning.effort` | `output_config.effort` |
| `reasoning` | 可映射其中的 `effort` | 使用 `reasoning` 对象 | 兼容映射为 `thinking` |
| `thinking` | 仅适用于支持该扩展的服务 | 仅适用于支持该扩展的服务 | 使用显式 `thinking` 对象 |

例如，支持手动思考预算的 Anthropic 模型可配置：

```json
{
  "max_output_tokens": 4096,
  "thinking": {"type": "enabled", "budget_tokens": 1024}
}
```

手动思考预算至少 1024，并且小于实际输出上限。自适应或关闭模式不要附带手动预算；能否使用这些模式及 effort 值，以服务支持情况为准。空思考对象表示省略，显式关闭应使用服务支持的关闭参数。

`extra_body` 和 `extra_headers` 用于兼容服务的附加参数与请求头。普通使用无需填写。高级参数不能替代消息、工具、鉴权地址等由沐璃管理的请求内容；显式输出上限也不能被它扩大。OpenAI Chat 默认映射到 `max_completion_tokens`，仅支持旧字段的服务可按其要求使用 `extra_body.max_tokens`，不要同时发送两个相冲突的上限字段。

<a id="视觉语音和检索服务"></a>

## 图片、语音与检索服务

以下配置在 `media` 下，均须填写各自实际的模型、基础地址和鉴权：

| 配置项 | 接口 | 用途与注意事项 |
| --- | --- | --- |
| `embedding` | `/embeddings` | 可选的语义向量服务；维度和查询指令须与模型一致 |
| `reranker` | `/rerank` | 可选的结果重排服务；这是兼容服务接口，不是 OpenAI 官方接口 |
| `asr` | `/audio/transcriptions` | 录音转文字，可设置语言与服务支持的返回格式 |
| `tts` | `/audio/speech` | 文字转语音，必须设置供应商支持的 `voice` |

图片理解使用 `models.vision`，不在 `media` 下。上传图片不会自动调用视觉；显式要求看图时，优先使用已声明视觉能力的主模型，否则使用配置的独立视觉模型。

### 向量服务的文档与查询设置

部分 embedding 服务要求资料和问题使用不同的前缀或任务参数。可在嵌入设置中填写“文档前缀”和“查询前缀”，或让沐璃按服务文档配置：

| 配置 | 用途 |
| --- | --- |
| `media.embedding.document_instruction` | 为建索引的资料加前缀，默认空 |
| `media.embedding.query_instruction` | 为语义查询的问题加前缀，默认空 |
| `media.embedding.extra_body` | 文档与查询共用的附加参数 |
| `media.embedding.query_extra_body` | 查询时覆盖同名的附加参数，默认 `{}` |

前缀原样连接正文，需要空格或换行时一并填写；原始资料和问题不会因此改写。查询专用参数按顶层字段覆盖，嵌套对象整体替换。模型、正文、维度和鉴权等核心字段仍由沐璃管理，不能用附加参数替代。

按实际服务填写参数，沐璃不会根据模型名自动猜测。改变文档前缀或文档侧参数后，已有向量需要通过[重建索引](memory-reindex.md)更新；不会自动启动全库工作。只改变查询专用参数不改变文档向量。原有查询指令也属于既有索引配置，改动时请核查索引状态。关键词与关系查询仍使用原问题，未配置服务时不需要填写这些选项。

### 视觉与语音示例

例如，为不支持看图的主模型补充视觉和语音输出，可合并以下配置并替换占位值：

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
      "voice": "填写音色标识"
    }
  }
}
```

TTS 默认格式为 `mp3`，也可按服务能力选择 `opus`、`aac`、`flac`、`wav` 或 `pcm`；PCM 是原始音频，不是 WAV 文件。可选速度范围为 0.25–4，留空不发送。ASR 的返回格式可按服务支持选择 `json`、`verbose_json`、`diarized_json`、`text`、`srt` 或 `vtt`。

这些 HTTP 能力包含在基础安装中，不需要另外安装视觉或语音模型包，也不会自动下载本地模型。网页录音、声学唤醒和渠道语音还需要相应配置与权限，见[语音说明](voice.md)。

原图、录音、待提炼正文和查询可能发送到你配置的服务。先确认地址与数据处理约定；配置完整不等于真实账号或外部服务已经验证可用。长期记忆与显式收藏的区别见[记忆说明](memory.md)。
