# 任务控制的运行边界

任务控制复用完整 task brief、SQLite 控制回执和原有团队调度。结构化 owner 操作只有 `pause` 与 `request_replan`；普通聊天不做意图分类，不自动暂停所有任务。计划、队友报告和系统注意事项都不是新的主人授权。持久事务与通知证明见 [task-control.md](task-control.md)。

## 宿主接线

`LocalServices` 创建 `TaskControl(runtime.store, commitments)`，与 `runtime.task_control` 共用同一实例。没有 LocalServices 的独立 Runtime 默认不启用控制，保留原接口。

- `await Runtime.submit_task_control(identifier, request)`：在配置锁内提交 owner 操作，复核仍在收尾的 workflow handles，停靠尚未执行的队友；事务完成后通知事件与调度器。调用方负责认证真实 owner。
- `Runtime.task_control_current(identifier)`：同步返回持久控制与当前活动 workflow 的统一投影，供详情和冲突响应使用。
- `await Runtime.task_control_committed(result)`：事务成功后的共同通知入口；主模型确认方案也使用它。内部提交事实与公开 `result.public()` 不混用。

纯 pause 只登记事实，清除旧重规划意图，不增加 main 输入或自动调用模型；无模型配置也可暂停。`request_replan` 明确登记主人重新规划的意图，允许追加完整要求；原始要求不被计划覆盖。启动、确认、输入容量释放和实际完成事件驱动通知投递，没有定时模型轮询。每页受 `limits.max_pending_runs` 限制，越过满邮箱继续扫描；重入事件会请求后续一轮处理，不丢失唯一容量释放唤醒。

手机重规划后续回复在最终准入后的私有提交闭包中，将可信 `MobileControlSource` 转为 `ReplyOrigin`，与 owner 控制事实及 main ingress 同事务保存。公开 `OwnerControlRequest` 和 Web 请求不能指定该来源；既有指纹不变，重复领域回执原样返回，不补改旧 ingress。输入仍为 `source=task-control, principal=owner, route=null`，显式回复来源只用于外发归属，不增加权限。

主会话同一运行的工具批次后和无工具续轮都使用实际 claim 的有限输入列表选择来源。严格匹配 `source=team:<id>, principal=agent:<id>` 的队内报告只补充当前请求证据；若没有其它新输入，沿用前一 `PreparedRequest` 的来源与主动通知选路。新主人输入、提醒等其它真实输入按其持久来源更新。首次准备、新运行及队友自身请求仍沿原规则；报告 metadata 和独立 Attention 路由不变，不查询全历史最近主人，也不保存永久目的地偏好。2.46仅覆盖重规划说明回本次手机，独立完成回报的新增持久关联见下节。该版十五文件431项相关整合、全147源码类型检查和独立后审通过，精确远端矩阵与正式发行已闭合，见[2.46审查](review-v2.46.md#发版闭合)。

## 独立完成回报候选（本地已验证）

后续片在上述运行内保护之外，为主 MuLi 的直接队友保存准确通知与执行关联：批准只从已持久的原 owner 回执取得手机来源；Runtime 每轮准备实际请求时，核验保留的准确通知、当前宿主快照及真实 run/generation。Team 完成时再次校验预期 completion_revision，在原完成事务中仅为该报告覆盖来源，不修改 agent 的派工来源、通用 PreparedRequest 或子代继承。通知仅入队、被其它 run 消费、证明损坏或多义均不能取得手机来源；没有有效证明的报告保留原行为。

实际请求的承接身份由宿主 `Message.source_message_id` 从持久消息传入，经过正常 pack 副本后仍须与完整通知正文同时匹配。不同消息的相同正文不能代替已移除的通知；hook丢失宿主身份时不通过内容猜回。此字段不进入 provider 显式协议或文本预算，未增加公开输入与模型可指定的来源字段。

真实完成报告随后沿原 flush/startup、主 ingress 和独立主运行发布。发送仍重核当前 owner；撤销得到 blocked，unknown 不因正常重开或控制重投重发。范围仅是直接队友该代完成说明，嵌套控制语义及旧报告保持；旧 binary 不兑现新证明，不保证降级后仍回手机。历史三条目标红及身份反例保留；修复后十二参数、21个相关文件411项、静态和离线构件检查已完成[2.47本地闭合](review-v2.47.md#最终候选准备)，远端与发行待验，详见[方案与证据限制](mobile-task-completion-proposal.md)。

## 同一请求中的完整快照

`PreparedRequest` 分开保存 `execution_scope` 与 `selected_replan`。执行队友和 workflow 节点从真实持久父链继承 scope；主会话仍可协调不同任务，选择待规划任务不会把它绑定成该任务的 worker。

构建请求时一次同步读取完整 brief、epoch、approved_epoch 和方案，再调用异步 context hooks。最终加入不可剪裁的 system 资料，明确标为协调资料。等待 hooks、摘要或模型名额期间不把新 epoch 贴到旧正文上；已构建请求仍使用原快照。完整正文、最新输入和原生回放超限则明确报错，不用摘要替代限制。停车只检查轻量控制事实，热缩上下文预算不会把已暂停身份误记为 failed。

主助手先通过 `commitment_read` 读取有当前 owner 重规划意图的任务，选择下一次模型请求的完整快照，再调用 `task_approve_plan(observed_epoch, plan, targets)`。同一原生工具批次中刚读取不能立刻确认；猜中持久 epoch 也不能替代宿主实际展示的快照。确认成功或 owner 意图失效后清除临时选择。targets 必须显式列出同范围的未完成队友，空列表不唤醒队友。

确认工具仅持续 main 的实际 session 可用。schema 依据真实 session 单独生成，handler 再核验 caller；工作流中角色名为 main 不升级权限。每次工具入口覆盖 ContextVar 并在正常、异常和取消后 reset，`create_task` 继承来的 main 上下文不能代替实际 worker 身份。

## 工具门禁与停靠

模型请求在进入模型及取得并发槽后复核执行快照。副作用工具在审批前检查，并在最后一次 await（包括 `tool.started` 发布）后同步再检查，然后直接进入 handler。旧 epoch 的写操作，即使新 epoch 已获确认也不能执行。原有权限与独立审查仍然生效；read 工具可完成，已进入 handler 的操作允许收尾，不声称撤回或回滚已发生的副作用。

遇到暂停或 stale 请求时，本批未执行调用逐一保存：

```json
{"status":"not_executed","code":"task_paused","reason":"暂停原因","observed_epoch":0}
```

assistant 的原生 tool_calls、参数、provider_data 和全部 tool results 保持完整配对，之后抛 `TaskPaused` 结束本次运行。它不是普通工具失败，也不继续生成成功回复。每个未执行调用发一次 `tool.completed`；除兼容的结果预览外，事件独立保留 `status=not_executed,code=task_paused`，不依赖被截断 JSON 判断状态。

Runtime 保存 `run.status=paused`、`pause_reason`、`observed_epoch`，发布 `run.paused`。团队在同一事务写该代 `team.finished(status=paused)`、completion_revision、agent 和报告。暂停是本次执行的终态，不是任务已完成；实际协程与模型名额释放，不挂一个等待批准的任务。暂停身份仍占未完成任务预算、出现在 active 列表，wait 可读取这一代结果。

暂停范围内的所有报告保留 pending，包括在最后执行检查后才完成的报告，不能自动唤醒主模型重新规划。其他 scope 的报告通过有界 keyset 分页继续交接。长期暂停不算活动执行，不因此永久拦住只读系统注意事项；注意事项仍无 owner 权限，不能解除暂停。主人明确请求重新规划、主助手确认后，再由真实事件投递积压报告。

## 可靠续办与恢复

`send` 可以有界写入暂停队友的邮箱，`resume` 不清除 control；纯 pause 下这两个入口均不能启动新模型。暂停范围也不能派生新队友。显式 stop 可取消暂停身份，并保留 2.2 固定停止批次与后来新代报告的行为。

计划通知必须同时匹配目标 scope、当前批准 epoch、generation 和 completion_revision。目标已停靠且普通邮箱满时，事务直接存档唯一真实内部通知并置 queued，不扩容或删除原 pending 输入。通知先入运行中邮箱、旧请求随后停靠时，完成边界同步核验真实通知，再以一次终态提交写 paused 日志和 queued 身份；不产生第二个 completion_revision。普通消息、取消后的旧通知、旧代或伪造来源均不能续办。

暂停与通知事实落盘，重启保留 paused 身份、原邮箱与报告；启动事件恢复可靠通知，不自动重做未知副作用。通知已投递只证明资料到达，不代表工具已执行或任务完成。

首版不支持活动 DAG 中途暂停。同 scope 存在 queued/running DAG，或状态已结束但实际收尾 task 尚未完成时，pause 返回 `workflow_active`，不改变控制事实。DAG 首次 await 前同步检查 scope 并登记运行、节点继承绑定；旧节点通过普通执行入口启动也不能逃离门禁。已批准范围可启动新 DAG，独立 DAG 不受无关暂停影响。

`tests/test_task_control_runtime.py` 使用临时 SQLite、假模型、真实 Runtime/Team/AttentionInbox 与 Event 门控验证以上边界，包括原生批次、最后 await、快照冻结、继承身份、满箱恢复、先投递后停靠、报告公平交接和真实系统注意事项。没有调用真实模型、网络或设备。

## 约定时间重新规划原任务（v2.65 开发候选）

主人可以明确约定：“这个任务先暂停，明天九点到十点之间交给这位原队友继续。”主助手核实当前时间、完整任务和已停靠的原直系队友后，调用 `task_schedule_replan(task_id,target_id,due_at,expires_at,key)`，保存明确的开始与失效窗口；不是另建独立工作流，也不直接解除暂停。

`task_schedule_replan_list` 分页核查约定：等待表示尚未接纳，到期接纳表示重规划意图与主输入已保存，批准表示主助手真正读取完整说明后，在下一轮确认了原目标的方案。三者均不等于工具已执行、任务完成或交付验收。`task_schedule_replan_cancel(id)` 撤销尚未批准的资格；已批准的执行仍使用原暂停或停止入口，不承诺追回已发生的副作用。

等候复用原提醒时钟与事件唤醒，零 AI 轮询；后台未运行时不唤醒电脑。普通提醒仍只提醒，定时独立工作流仍执行其独立委托。完整到期、正常重开、新代及原路回报的离线旅程与独立后审已通过，发行检查尚未闭合，范围与回退限制见[约定时间继续原任务](scheduled-task-replans.md)。

## 显式交付验收候选

主助手可显式调用 `task_delivery_record`，为当前任务版本的一个准确直接队友完成报告和一个本地 UTF-8 附件副本保存验收依据。独立协调 provider 只登记这个 write 工具；队友 schema 不提供它，handler 仍复核真实主会话和运行。原工具 provider、普通 completed、任务状态、方案批准与 owner 权限不变。

宿主从真实 file_publish 的完整 accepted 工具结果、准确调用消息和当前完成事件核关联，再在有限字节窗内读取附件副本。宿主文件事实与逐条模型判断分栏；文件属于本任务、由本代写出及满足要求均不能只凭路径或摘要升级为宿主事实。文件 await 在事务外，末 `BEGIN IMMEDIATE` 中无 await 复核任务 revision/epoch 和完整证据后，将不可变单、同任务去重回执及事件原子保存。owner 在核验期间暂停或追加要求，不能留下成功单。

相同规范参数与去重键只返回原单，内容不同报冲突，不重读文件、不重跑队友；任务要求或执行代次变化后原单保留为历史。读取复用 `commitment_inspect(section=deliveries)` 与原认证 HTTP 入口，不新增前端面板。登记不自动发文件，后续说明沿当前真实来源，显式渠道发送继续沿原授权、撤销和 unknown 回执规则。候选、本地历史红点及待验证范围见[交付验收方案](task-delivery-evidence-proposal.md)，不代表本片已发行。
