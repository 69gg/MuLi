# 查看已保存的正文

主对话和任务记录共用独立的只读归档窗口，可以逐页回到更早的已保存消息，并按需续读长正文。打开记录不创建新对话，不改变当前主沐璃的任务、输入草稿或流式回复。它展示消息的 `content`，不代替附件浏览或完整数据导出；原任务详情保留已有媒体入口。

窗口默认从最新一页开始，通过“更早一页”和“较新一页”导航。每次仅保留当前页与导航位置，正文续读也只保留当前段，上一段按原位置重新读取。普通角色、来源和身份分别展示；缺少 principal 的 user 消息不会因此成为主人请求，工具和系统资料也不会重新执行。

## 接口

所有接口沿用本地认证与 `Cache-Control: no-store`：

| GET 路径 | 参数与结果 |
| --- | --- |
| `/api/sessions/{session_id}/messages` | `limit=50`，范围 1–100；可选 `before_seq`、`through_seq`。返回 `session_id/messages/through_seq/next_before_seq/has_older` |
| `/api/sessions/{session_id}/messages/{message_id}/content` | `offset_bytes=0`、可选 `length_bytes` 与 `revision`；复用 [证据续读](history-evidence.md) 的完整身份、版本及 UTF-8 字节页 |

消息项包含完整 `id/seq/revision/role/content/content_bytes/content_truncated/content_provenance`；source/principal 只在实际保存且预算足够时返回，省略用 `metadata_omitted` 明示。无法读取的正文明确标记，不以空字符串冒充完整内容。主、队友、工作流和独立记录均按真实 session ID 查询，不触发模型、记忆提炼、原工具、主人授权 hook 或渠道发送。

消息页及正文续读同时返回完整 `attachment_ids`，不加载附件正文；旧消息缺字段时为 `[]`，旧 opaque 引用及重复顺序保持不变。引用不作为可选元数据静默省略：字节投影或最终 JSON 预算不足返回 `output_budget`；字段存在但为 null、形状超出原信封范围或含无效 UTF-8 字符时返回 `content_unavailable`。它只是已保存资料的定位，不代表当前文件仍存在或获得新的读取授权，详细合同见[证据续读](history-evidence.md)。

首次读取取得该会话已保存的最高消息 seq，作为 `through_seq`；空会话为 0。续页原样携带该上界和 `next_before_seq`，只取小于 before 的记录。每页按 seq 正序展示；还有更早资料时，next 为实际返回最小 seq，否则为 null。预算不足时只保留候选较新端的连续完整项，不能越过未返回的记录。pending 入站序号与未持久流片段不参与这个游标。

through 仅限制本次遍历纳入的追加消息，不是内容快照：删除的记录不会补回，重写正文会产生新 revision。正文续读必须携带旧页版本，避免混拼；返回 409 `evidence_changed` 后从零读取当前版本，404 `evidence_not_found` 表示删除、不存在或会话不匹配。没有一项完整定位能放下时返回 400 `output_budget`，参数范围错误为 422。网页序号限于 JavaScript 可精确表示的非负整数；归档数据超过该范围返回 400 `archive_sequence_range`，不能用舍入游标跳过记录。

## 资源与现有边界

新增页的完整 JSON 预算为 `min(limits.max_output_chars, 32000)` **字符**，包含外层字段、转义和续读位置，不是 32KB。正文按 UTF-8 字节定位；NUL、中文、emoji 和 HTML 均作为原文保留。复用现有 SQLite 索引和窄证据投影，不把 provider_data、原生工具参数或内嵌图片整体装入 Python/响应。SQLite 仍需访问并解析被选行的 JSON，不宣称底层 I/O 与整行大小无关。

归档窗口独立于实时聊天 state、hydrate 与 SSE。此版本没有缩小既有 `/api/conversation` 的最近 1000 条消息、历史事件或全局 SSE 首次重放；新增页的预算不能被解释为整个启动链路或浏览器内存的上限。正文归档只负责已知会话内的向前浏览；2.11 起通过[完整标题目录](session-records.md)发现更早任务，再打开同一归档入口。标题目录同样不改变既有实时首屏资源边界。

`accepted/runtime_excerpt/unknown` 的存储含义沿用证据工具；进入数据库前已丢失的尾部不能恢复。记录查询不会把旧授权重新变成新授权。窗口关闭、记录切换或认证失效会取消读取并丢弃迟到响应；服务器不能撤回读者已经看过的字节，不承诺跨客户端即时擦除。

## 实现参考

只读参考 `/data1/codex` 的 thread/turn/item 分层历史接口及状态索引的先筛选再分页；MuLi 使用现有 message_order 和 HistoryEvidence。没有引入 Rust 运行时、新历史数据库、全文检索或模型轮询。Codex 的旧式全 rollout 回放不作为 MuLi 分页实现。正式验证与未完成阶段见 [2.10 审查](review-v2.10.md)。
