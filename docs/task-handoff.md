# 按最新证据接办队友任务

开发中的接办扩展，尚未发行。先由主人沿既有入口暂停任务、追加要求并请求重规划；主助手读取当前完整说明，在下一轮确认方案。核查本身不选择重规划、批准计划、派工、恢复或重试操作。

使用现有工具，只指定一个队友：

```json
{"identifier":"任务ID","section":"handoff","agent_id":"队友ID","limit":20}
```

同一认证管理入口为 `GET /api/commitments/{id}/inspection?section=handoff&agent_id={agent_id}`。主助手可以选择任务；其它调用者逐次核对实际任务绑定，模型参数、复制的工具 schema 和旧游标不能取得主身份。

返回当前任务的 epoch/批准版本、完整说明读取位置、指定队友代次与执行状态、当前 run/report 的读取位置，以及有限近期原消息的 seq、revision 和附件引用。目录不读取正文、自动转写或搜索记忆；记忆只携带 revision，候选仍需显式查询。原文使用 `conversation_read`，文件和语音沿既有工具按需读取。不要将本段 revision 跨 section 当作正文版本；沿 `task_read` / `report_read` 给出的参数取得正文页，再按其返回版本与字节游标续读。

排队重跑可能仍保留上一轮 run ID，此时执行标为 `historical`，不加载上轮成功报告冒充本次结果。缺 run/report 不从历史搜索成功记录替代。`side_effect_status` 保持 `unknown`；执行完成、工具返回或报告已保存均不等于外部发布或发送成功。

## 有限观察与续页

`source_completeness="recent_window_only"`、`consistency="live"` 明示这是一段有限近期观察。消息窗口取 `min(limit, limits.max_search_results, 100)` 个原始映射，损坏或缺失项也占额度，不向旧历史无限补齐；`has_older` 提示窗口外还有资料。正文未存下或已删除时不能由摘要复原。

输出预算造成分页时，原样回传 `cursor` 并保留相同参数，只在这个窗口内续读；修改 limit 不是下一页。revision 绑定实例、参数、任务、指定队友及父链、当前执行/报告、窗口内宿主消息版本和记忆版本。新增尾部消息或这些事实变化时返回 `evidence_changed`，从首页重新核查。窗口外旧消息和其它队友不在此版本保证内，绕过宿主版本机制直接改数据库正文也不由消息 revision 认证。

父链另受配置记录预算及既有100项上限限制，不随本次 limit=1 变成只能一跳；完整点查对象先经 SQL 字节门，预算不足或首项放不下明确失败。消息只将有界元数据投影送入 Python，损坏项明确 unavailable；这不是 SQLite 对任意巨大 JSON 的恒定 I/O 保证。

此有限成本合同覆盖新处理器及其专用调用者 scope 回调。通用 Runtime 的模型请求快照和工具执行控制仍有既有完整任务读取及旧父链遍历，尚未取得整条执行链同等有界的证明。本段不增加队友总数限制、数据库、自动接班器或副作用重试。

设计与独立审查见[方案](task-handoff-proposal.md)，离线完整旅程见[场景记录](task-handoff-journey.md)。真实模型接办质量、真实账号送达及外部副作用恢复仍需分别验收。
