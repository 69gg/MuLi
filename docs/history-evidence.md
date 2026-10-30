# 压缩后的证据续读

摘要用于延续任务，具体结论仍可回到本地保存的消息核对。`conversation_read` 提供消息目录、单条正文与原工具调用参数续读，主助手、独立会话、动态队友与工作流节点共用现有只读工具和权限门。没有新模型调用、常驻检索任务或独立历史数据库。

网页通过[已保存正文归档](history-archive.md)共用上述证据读取；旧向浏览使用独立 before/through 页，模型工具现有 after 正向协议保持原样。归档窗口不参与实时对话重建，也不发送新用户消息。

## 定位与读取

知道来源会话、但不记得消息位置时，可以先用 [`conversation_find`](history-find.md) 按原文片段查找，再把返回的 `read_arguments` 交给 `conversation_read`。查找只返回位置和来源，不能代替阅读正文；预算中止或遗漏不表示已经查全。

主人连来源会话也不记得时，可让主沐璃使用 [`history_find`](history-find.md#忘记来源会话时)，在自己的旧消息和现存直属队友资料中定位。该入口排除当前运行内容，游标只在本次运行内有效；正文仍由上述读取工具核对。

先列出消息：

```json
{"session_id":"来源会话 ID","after":0,"limit":5}
```

返回 `records`、`next_after` 和 `has_more`。目录记录保留 `id`、`seq`、`role`、`revision`、`content_bytes` 和有界 `content`；`content_truncated` 明确说明正文是否仅为预览。`next_after` 只推进已经返回的记录。目录不展开工具参数、原生协议数据、推理签名、图片或附件字节。

目录、单消息正文页与网页归档都完整返回该消息的 `attachment_ids`，便于压缩后按原序号找回交接资料，再调用既有附件工具读取。旧消息没有该字段时返回 `[]`；旧通用信封中的 opaque ID、大小写及重复顺序保持原样。新的本地 Team 交接入口另有严格 ID 校验，见[队友资料交接](teammate-attachments.md)。历史引用不证明文件当前仍可用，也不授予新权限。

附件引用计入完整 JSON 预算，不能被可选元数据省略规则移除。SQL 字节窗口或最终响应预算不足以容纳完整引用时返回 `output_budget`；显式 null、非数组、超出原信封数量/字符串范围或不能编码为 UTF-8 的损坏引用返回 `content_unavailable`，不将坏引用伪装成空数组或截断成功列表。

取得消息 ID 或已有摘要中的 seq 后，读取正文：

```json
{"session_id":"来源会话 ID","message_id":"目录中的 id","offset_bytes":0,"length_bytes":1024}
```

`message_id` 与 `seq` 二选一；正文模式不能混用 `after/limit`。返回 `content`、`revision`、`offset_bytes`、实际 `length_bytes`、`next_offset_bytes`、`total_bytes` 和 `has_more`。续读时把返回的 `revision` 与 `next_offset_bytes` 原样传回。

偏移从零开始，单位是 **UTF-8 字节**。中文通常占多个字节，不能把可见字符数当作偏移。起点位于一个字符中间、超过末尾，或非零起点没有版本都会明确拒绝。页尾只在完整字符边界结束；嵌入 NUL、中文和四字节字符不会被静默忽略。非末尾成功页一定向前推进，末尾可以返回空正文。

每页按外层完整 JSON 的最终字符长度核算 `limits.max_output_chars`，包含转义和游标。请求的 `length_bytes` 是上限，实际页可更短。目录条数还受 `limits.max_search_results` 约束。预算无法容纳必需身份和最少正文时返回 `output_budget`，不会返回缺失游标的预览。

## 原工具调用参数

默认仍读取正文 `content`。需要核对某条消息保存过的原调用参数时，按消息 ID 或 seq 指定 `field="tool_calls"`：

```json
{"session_id":"来源会话 ID","seq":42,"field":"tool_calls","offset_bytes":0,"length_bytes":1024}
```

每页返回 `field="tool_calls"`，`content` 是已保存调用数组的 JSON 文本片段。沿用上面的 UTF-8 字节位置、`revision` 和输出预算，续读时保留相同 `field`；先拼接全部 `content`，再解析这一层数组 JSON。数组中的 `arguments` 按原字符串返回，读取过程不再次解析或改写它。

`field` 只能用于单消息读取，不能与 `after/limit` 目录或 `section=continuations/main_recovery` 混用；显式指定 `field="content"` 也遵守这项限制。网页正文归档保持原有入口。

| 保存情况 | 读取结果 |
| --- | --- |
| 没有 `tool_calls` 字段 | `field_missing`，不能当作没有调用 |
| 明确保存空数组 | 正常返回 `content="[]"` |
| 字段为 null 或其他非数组类型 | `content_unavailable` |

调用参数页不带正文的 `content_provenance`，`side_effect_status` 为 `unknown`。记录只说明保存过什么，不证明执行、生效或成功，也不提供当前授权；读取不会再次调用这些工具。

## 版本与来源

`revision` 是宿主版本标识。标准消息保存路径每次写入都会换新版本，不能由调用者保留旧值。旧记录没有此字段时使用数据库实例与 seq 构成的稳定版本，不在首次读取时批量修改归档。内容或来源被重写后，旧续读返回 `evidence_changed`；删除、会话不匹配或目标不存在返回 `evidence_not_found`。多次调用不是跨请求数据库快照；需要新版本时从零重读，不能混拼新旧正文。直接绕开宿主修改 SQLite 不在版本保证内。

来源保留实际存储的 `source`、`principal`；没有记录就保持未知，不能因为 `role=user` 推断是主人。可选字段无法完整容纳时明确标记省略，不截出另一个貌似完整的身份。旧授权、assistant 结论和 tool 输出均为当前只读工具返回的资料，不重新插入为新的用户指令，也不触发原工具调用。

`content_provenance` 只描述存储前的处理：

| 值 | 含义 |
| --- | --- |
| `accepted` | 该结果序列化后未被 Runtime 的写前预览进一步裁剪；拒绝、错误结果也可以完整保存，不代表工具已执行或操作成功 |
| `runtime_excerpt` | Runtime 在保存前已节选或省略结果，只能读取实际保存的部分 |
| `unknown` | 未记录上述信息，包括旧工具记录及未标记的其他消息；不能按正文 JSON 的外形猜测 |

`has_more=false` 只表示已到这条持久 `content` 的末尾，不证明外部文件、网络响应或命令输出采集完整。工具自己的采集上限和省略仍按各自结果说明。进入数据库前已经丢失的内容不能通过本接口恢复；本轮没有新增无限原始输出归档。

## 最终模型上下文

可信工具对象可以声明 `preserve_output=True`；该宿主属性不进入工具 schema，返回 JSON 里的同名字段无效。运行器按实际执行的工具对象给可完整交付的结果记录 `preserve_content`。若这类结果超过当前输出上限，返回完整的 `output_budget` 错误和 `result_omitted` 标记，不能用通用预览破坏分页对象，也不会重跑原调用。标准设置入口会拒绝运行中重配或等待空闲；这项边界也覆盖本地受信扩展直接改预算的情况。最终打包时，紧邻下一次模型请求的最新 assistant 工具批次中，所有标记结果都保持完整；不会只保留最后一页，也不会保留游标却节选它声称覆盖的正文。

模型回应或开始下一批调用后，旧页才按普通上下文规则节选或整体移出。补充用户输入、system/developer 资料注入都不表示模型已消费该批结果。必要页、任务说明和原生回放放不下时，启用压缩可按[上下文整理规则](context.md#最终请求打包)尝试一次受限检查点；仍无法容纳则明确报上下文不足。该标记不进入厂商协议，不改变 tool-call 配对，也不赋予新权限。本机 Python 插件保持受信任扩展边界。

## 存储与资源

复用 `objects` 和 `message_order` 的消息 ID、会话与 seq 索引。SQL 只投影所需小字段及所选字段的字节窗口，Python 不为一次参数页读取展开完整调用数组、`provider_data` 或图片。SQLite 仍需访问并解析原有 JSON 记录；不能据此宣称底层 CPU/I/O 与整行大小无关。没有新增全文索引、后台轮询或大型依赖。

调用参数使用独立只读连接，并受宿主查询工作量和时间预算限制；达到预算返回 `evidence_read_budget`，已有事务或读取范围不能接管时返回 `evidence_transaction_active`。这不改变默认正文读取，也不是操作系统级硬超时保证。

## 记忆来源原文

2.7 的 `memory_source` 复用相同字节窗口、JSON 预算与工具批次保留规则，但读取 `memory_sources` 的独立来源快照，版本为其既有 digest。来源级遗忘后不回退查会话；`id/digest/truncated` 保留兼容，末页也不冒充从头开始的全文。网页核查同样按需读页，详见[记忆核查与更正](memory.md#核查与更正)。

## Codex 参考

只读核对 `/data1/codex/codex-rs/ext/history-notes/src/tools.rs` 的定位与正文窗口分离，以及 `core/src/tools/context.rs`、`context_manager/history.rs` 的采集上限、模型视图与持久记录分层。MuLi 复用本地 SQLite，不接入 Codex 的专用历史服务、私有端点或 Rust 运行时。`compact_retained_tool_outputs.rs` 的特殊保留条件也没有被当成通用绕过配对的方法。

正式测试、独立复审与发行状态见 [2.6 审查](review-v2.6.md)。设计稿与局部原型不等同于完整验收。
