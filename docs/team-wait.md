# 动态团队的增量等待

`Runtime.team.wait(targets, *, timeout=30, caller=None, cursor=None)` 与模型工具 `wait_agents` 支持可选 `cursor`。等待只观察持久状态，不领取邮箱、不占模型执行槽，不定时调用模型。目标仍按 ID 或唯一名字解析，单次目标数及等待时限沿用现有运行预算。

## 兼容快照与增量订阅

不传 cursor 时保留原来的 any-terminal 快照：任一目标已经结束便立即返回完整 `agents` 和原 `result`，或在输入/期限到达时返回原 `new_input` / `timed_out` 字段。仅附加 `cursor`、`reason`；不会为新模式顺带裁剪旧结果。

Python 接口始终保留上述完整快照。模型工具 `wait_agents` 有输出预算：小快照保持同样形状；完整快照超过 `limits.max_output_chars` 时，明确返回 `legacy_snapshot=true,truncated=true`，以有界 `result_preview` / 每项 truncated 和会话 history 代替完整 result，并保留完整 cursor 及明确的 baseline。这使模型能继续增量等待，不被通用 JSON 预览截断吞掉初始 cursor；连元数据都放不下则明确要求拆分目标。此快照只建立当前观察基线，不宣称逐条回放了之前的完成历史。

首次响应的 cursor 表示“从这次快照之后开始观察”，不表示已经逐条回放之前的历史。下一次携带它，已知的旧结果不会反复唤醒等待：

```python
snapshot = await runtime.team.wait([first_id, second_id], caller=parent_id)
update = await runtime.team.wait(
    [first_id, second_id], caller=parent_id, cursor=snapshot["cursor"], timeout=30
)
```

增量响应包含：

- `agents`：当前 ID、名称、状态、执行 generation、completion_revision，无旧结果正文。
- `completed`：游标之后、按持久事件顺序返回的完成记录，包含 agent_id、generation、completion_revision、status、run_id、result_preview、truncated，以及 `history:{session_id,run_id,event_id}`。
- `cursor`：仅确认本次实际返回的完成前缀；保存它用于继续等待。
- `reason`：completed、new_input、mailbox、scope_changed 或 timeout；`timed_out` 只在 timeout 为真。
- `has_more`：仍有未返回完成记录时为真；继续传返回 cursor 读取。

每条完成记录的 `coalesced=false,count=1` 表示一次准确的终态提交，不把多代完成合并冒充全量。一次执行结束后，即使该身份已经 queued/running 下一代，仍可读到上一代完成。generation 标识执行开始次数；completion_revision 还包括尚未开始就取消的情况，不能互换。

result_preview 是节选，不是完整结果。事件最多保留 2000 字预览，响应根据 `limits.max_output_chars` 继续收紧，并明确 truncated；完整会话/运行证据仍在原存档。当前 Web API 可通过已有会话详情/事件查看历史，没有新增 `/api/runs/:id` 路由。新模式先按运行时相同的 JSON 序列化方式计算长度，保留合法对象及完整 token；元数据自身放不下时报 `wait_budget_exceeded`，要求拆分目标，不交给通用工具截断器破坏 token。

## 观察范围、输入和取消

两个观察者分别保存自己的 cursor，互不标记“全局已读”。重试同一个旧 cursor 可重新得到同一记录；这是可重放交付，不承诺恰好一次。

targets 改变时，保留目标沿用原进度；新加入或曾移出的目标以当前修订建立新基线。响应通过 `baseline:[{agent_id,completion_revision}]` 明示新基线；没有新完成/输入时返回 reason=scope_changed。这不会回放新增目标加入之前的全部历史。移出的目标从返回 token 中删除，避免 token 随历史团队无限增长。

增量检查顺序是 pending owner 输入、新完成、其它可交付邮箱、范围变化、期限。owner 与完成同时到达时返回 new_input=true，完成列表为空且不推进 cursor，先让执行器在安全边界处理输入。系统资料、队内消息只能产生 reason=mailbox；quarantined 外部输入不唤醒为用户请求。等待不领取、删除或改写任何入站记录，超时返回前也会重核已经提交的输入/完成。

取消等待只清理本次登记，不停止目标。同 caller 的并行等待各持登记 token，等待图聚合仍然检测自等待与循环，其中一位退出不会删掉另一位。父子任务的显式取消传播保持原样。

任务提交完成后即释放调度槽，允许同一身份开始下一代；尚未完成通知或报告投递的收尾协程仍被独立追踪。服务关闭先取消并等待全部团队协程，再关闭数据库，重复关闭或外部取消关闭等待不会再次中断正在清理的协程。已提交的完成日志和待投递报告保留，重启仍按原有持久投递规则处理。

## 持久性与错误边界

agent 的 completion_revision、结果、原 team_report 发件意图与 `team.finished` 事件在一个 SQLite 事务中提交。正常完成、失败、运行中取消、调度前取消和启动恢复 interrupted 共用同一终态入口；重复停止已结束身份不增加修订。启动恢复不自动重做未知副作用，也不把上一代已完成 run 的 ID/内容作为本代崩溃证据。

cursor 带版本、持久 Store 实例 ID、调用方作用域和各目标已见修订；它是观察进度，不是授权凭证。处理器重新校验实际目标与边界。格式错误报 wait_cursor_invalid；异库、调用方不符、未知版本/目标、来自未来、修订回退或缺少中间事件报 wait_cursor_stale，不能静默越过结果。旧记录缺省修订 0，无需无界回填旧 team.updated。

正常关闭重开数据库保留实例 ID 和完成日志，因此旧 cursor 仍可用。恢复较旧备份会明确拒绝来自未来的修订；未标记的数据库复制/分叉不在自动识别保证内。当前没有完成事件清理策略，若未来清理，必须保留明确的过期边界。

实现复用 `events` 的部分索引有界读取后续修订；每个目标只预取下一条，按事件 ID 合并，响应只推进已放入预算的记录。不读取图片、provider_data 或整段消息历史，不新增后台工作器。

`tests/test_team_wait.py` 使用临时 SQLite、事件门控和假模型，覆盖旧合同、多观察者、同 caller 取消、逐代分页、输入/期限竞态、范围基线、原子回滚、调度前取消、重启恢复、坏 token/日志缺口、输出预算与真实生命周期接线。原单并发派生/等待/收集测试继续覆盖执行槽释放；没有调用真实模型、网络或平台采集。
