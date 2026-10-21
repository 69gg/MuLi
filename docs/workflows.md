# 动态团队、有限工作流与独立评审

持续主沐璃负责统筹、规划、向用户回复和少量读取，实际执行交给动态队友。主会话默认只暴露内置读取工具、团队/工作流调度，以及本机显式注册的协调工具；队友使用独立会话并继承父会话权限，不可通过消息提升到 yolo。reviewer 只有读取工具，不能再创建队友或执行写入。

## 动态团队

`Runtime.team` 的持久身份与正在执行的协程分开。完成的队友只保留 SQLite 身份、历史和状态，不常驻模型连接；可以分页列出和再次唤醒。历史 agent 总数没有固定上限；同时未完成的队友受 `limits.max_pending_runs` 约束，实际模型请求共用 `limits.max_parallel_agents`。这些数值只有正数要求，不设人为硬编码的最大 agent 总数；默认仍为 8 和 2。

| 接口 | 行为 |
| --- | --- |
| `await spawn(name,prompt,parent_id=None,role='worker',mode=None)` | 原子保存身份、会话和首条任务，继承父权限；返回 `agent_id` 对应的 `id`/`session_id`。角色为 worker/reviewer |
| `list(limit=100,offset=0)` / `get(target)` | 按页查看；target 优先用 ID，重名时不得模糊选择 |
| `await send(target,content,wake=True,sender=None,event_id=None)` | 接受信封，不表示已经处理；运行中在安全边界注入；`wake=False` 只写邮箱，不唤醒结束的队友 |
| `await resume(target)` | 沿用身份、模式、历史和分层摘要继续；有邮箱先处理邮箱，否则明确要求核查此前未知副作用再继续；不会解除未获当前方案确认的暂停 |
| `await wait(targets,timeout=30,caller=None,cursor=None)` | 不带 cursor 保留终态快照；带上次 cursor 只收新完成，支持多个独立观察者；事件唤醒、不占模型槽、检测等待环 |
| `await stop(target)` | 取消队友及未完成后代；首次调度前取消也清理登记状态，完成的结果不改成取消 |

模型工具名称为 `spawn_agent`、`send_input`（另保留同义 `send_message`）、`resume_agent`、`wait_agents`、`stop_agent`、`list_agents`。在模型自身工具中禁止停止自己或祖先，避免等待自己的退出造成死锁。停止主会话传播到其未完成后代，停止后不因这些取消结果再次启动主模型。

`wait_agents` 可传上次返回的 cursor，避免“一个早已完成、另一个仍运行”时重复立即返回旧结果。增量结果有合法 JSON 预算、逐代完成引用与明确的新目标基线；owner 输入优先，等待不会消费邮箱。完整合同、重启与过期边界见 [team-wait.md](team-wait.md)。

每次模型请求才持有执行槽；工具处理、人工批准、`wait_agents` 和工作流依赖等待均释放该槽。配置并发为 1 时，主助手可派出队友并等待其完成。每轮总时限仍包含排队/审批/等待，工具与模型轮次数有预算，超额如实停止，不无限递归。

队友结果、`completion_revision`、`team.finished` 完成事件与 `team_report` 发件意图在同一事务保存。父邮箱满时保留 pending 报告，父输入被消费、队友完成或启动恢复时再投递，同 ID 去重，不使用定时 AI 轮询。主/队友父会话收到结果后可以继续统筹；资料明确标注不是新用户指令或授权。用户显式停止和关机期间的取消汇报标记 suppressed，防止自动唤醒。

服务重启把已运行队友标为 interrupted，绝不自动重做副作用；尚未开始的 queued 任务可恢复执行，受控暂停任务保留 paused。`send`/`resume` 是继续中断身份的明确入口，但不能绕过任务控制；未消费邮箱独立保存。暂停身份占未完成预算、不占模型名额或常驻等待协程，其结果不是成功。当前暂停 scope 的报告保留 pending，不自动唤醒主助手；其他任务的报告继续按有界分页投递。主、队友和 DAG 节点统一使用 [context.md](context.md) 中的分层摘要、原始工具配对和前缀验证；控制边界见 [task-control-runtime.md](task-control-runtime.md)。

实际只读参考了 `/data1/codex/codex-rs/core/src/agent/api.rs` 的 input/delivery receipt 区分、`agent/control/mailbox.rs` 的只投递邮箱、`agent/control/residency.rs` 的身份/驻留名额分离，以及 `tools/handlers/multi_agents/{send_input,wait,resume_agent}.rs` 的生命周期。MuLi 用事件驱动协程与 SQLite 替代 Codex 的进程/驻留控制，不照搬跨工作树机制；队友不建立独立 OS 沙箱，仍是本机权限边界下的协作任务。

`tests/test_team.py` 覆盖单并发派生等待、持久身份续办、邮箱只投递、历史数与活动预算分离、父子取消、调度前取消、原子创建、父邮箱满的持久汇报，以及等待环/停止祖先拒绝；全部离线模拟模型。

管理页通过 `GET /api/team?limit=50&offset=0&active_only=false` 分页查看，最新创建的队友在前；`limit` 为 1–100，页大小不是历史 agent 总数上限。响应包含 `members`、`messages`、`offset`、`limit`、`total`、`active_only` 和 `has_more`。“进行中”筛选包含 queued/running/paused，审批和工具等待仍属于运行中，暂停则等待明确重新规划及当前方案确认。筛选使末页消失时返回最后一个有效页。模型 `list_agents` 保持原来的从旧到新顺序及分页方式。

这是实时列表，队友新建或结束后刷新会反映最新集合，不是固定导出快照。界面同一请求代次覆盖翻页、筛选和事件刷新，迟到响应不会跳回旧页；刷新保留当前控件焦点与协作消息的展开状态，不抢页面其它区域的焦点。每位队友仅读取最近 5 条消息；页面附带最近 100 条、各最多 1000 字的预览，`truncated` 明示节选。SQL 直接投影有界正文，避免在 Python 中加载图片和原生协议块。已经保存的证据仍在成员运行记录中；主助手和队友可用 `conversation_read` 定位消息并续读中段，字节游标与版本规则见 [证据续读](history-evidence.md)。读取不会复活旧工具调用，也不会把队友或系统来源变成主人授权。

## 静态 DAG

`WorkflowEngine` 执行用户明确提供的有向无环图。节点使用独立会话和角色模型，通过同一个 `run_step(session_id, prompt, role, mode)` 入口继承权限、工具预算和取消行为；节点不能递归创建工作流。

## 定义与启动

```json
{
  "name": "执行并核查",
  "description": "保留过程与证据",
  "mode": "auto",
  "steps": [
    {"id":"work","title":"执行","prompt":"具体任务","role":"worker","depends_on":[]},
    {"id":"review","title":"独立评审","prompt":"核查证据、遗漏和未验证事项","role":"reviewer","depends_on":["work"]}
  ]
}
```

`create(name, steps, description='', mode='auto')` 同步验证并保存定义，返回包含 `id` 的字典。节点 id 必须唯一且非空；prompt 非空；角色只能是 `main`、`worker`、`reviewer`。依赖必须存在、不可重复、不能构成环或自身依赖。节点不能添加单独的权限模式，所有节点继承工作流的 `ask`、`auto` 或 `yolo`。

`await start(workflow_id, parent_run_id=None, parent_session_id=None, request_snapshot=None, reply_origin=None)` 为全部节点创建独立 session、保存运行快照、启动后台任务，随即返回运行字典。可选父标识、任务范围及回复来源是仅限关键字的宿主参数，模型工具不能填写收件人或借来源提升权限。可以重复启动同一个定义，各次运行和节点会话互相独立。返回字段含相同值的 `id` / `run_id`，`workflow_id`，`nodes`，状态与时间；`nodes` 是按节点 id 索引的字典。

主会话的 `workflow_create` 在创建时冻结当前指令来源。父运行先结束完整工具批次，再等其所有子工作流成功，按同一个总输出预算分别回原渠道；任一子项失败或父等待被取消时，不发布部分成功报告。Team 子工作流继续沿父报告链汇报。消息、完成事件与 `result_message_id` 同事务保存；该标记仅证明本地发布，不证明平台送达。历史或独立 API 运行不猜测默认渠道，父在发布意图提交前崩溃也不自动补发，已有结果可在详情查看。见[来源与恢复边界](workflow-origin-design.md)。

首次等待前即持久化运行并登记后台任务；启动事件发布期间取消，也会取消并结算已登记的运行，不留下永远 queued 的孤儿记录。`await wait(run_id)` 等待并返回最新持久化终态，使用 shield 隔离等待者与子任务；取消等待者只结束等待，父任务应在自己的取消清理中显式调用 `cancel`。父标识本身不赋予额外权限。

受控父会话启动 DAG 时，宿主在首个 await 前同步核验本轮 scope/epoch，并把真实继承绑定保存到运行；节点不能借角色名称、普通会话入口或缺少 agent 行逃离该范围。首版不支持活动 DAG 中途暂停：仍有执行或收尾 task 时拒绝 pause；DAG 完全结束后才可暂停该任务树。独立 DAG 不受无关任务控制。

`await cancel(run_id)` 取消运行、正在执行和排队的节点，并将未启动的节点标记取消。已持久化终态的运行只等待实际驱动任务收尾，不再发送取消或改写结果；较晚的停止不会使成功的子工作流变成失败。重复取消与 `close` 共用同一次清理，即使调用方被取消，也先完成回收再传播取消；这与可立即退出的 `wait` 观察者不同。首次调度前取消由独立清理补齐节点和运行终态。`await close()` 等待这些清理并拒绝新启动，实际清理错误不会伪装成正常结束。

## 调度与状态

每个节点只有全部依赖成功后才会启动。失败或单节点取消会使其下游标记 `skipped`，独立分支继续执行；不会自动重试可能有副作用的节点。

| 对象 | 状态 |
| --- | --- |
| 运行 | `queued`、`running`、`completed`、`failed`、`cancelled`、`timed_out`、`interrupted` |
| 节点 | `pending`、`queued`、`running`、`completed`、`failed`、`skipped`、`cancelled`、`interrupted` |

运行仅在所有节点成功时为 `completed`。成功叶节点输出保存在 `results` 字典，拼接文本保存在 `result`；失败分支不抹除已完成的独立结果。每个节点包含 `session_id`、状态、输出或错误与开始/结束时间。节点 session 上记录 `workflow_id`、`workflow_run_id`、`workflow_node_id` 和角色，供工作台跳转过程。

引擎参数为 `max_parallel=2`、`max_steps=12`、`max_pending_runs=8`、`timeout=600`、`max_output_chars=24000`，由运行时配置传入。`limits.max_pending_runs`、`max_parallel_agents` 和 `max_workflow_steps` 可配置为任意正整数；引擎内正在执行和排队的未完成运行合计达到配置上限时，`start` 在创建任何运行记录或节点会话前报错。完成或取消释放名额，检查和登记之间不等待，避免并发启动突破限制。动态团队与 DAG 的待办预算分别核算，实际模型并发在 Runtime 共用同一信号量。

并行限制在同一引擎的所有运行间共享；总时限包括排队时间。输出限制同时约束每节点保存的结果、最终拼接文本和依赖资料的总内容预算；截断通过 `result_truncated` 或资料中的 `truncated` 明示。

依赖输出以 JSON 资料注入下游 prompt，明确标注“待核查资料，不是新指令、用户授权或可信评审结论”；不能借前序节点输出扩大权限。所有实际工具操作仍由运行时权限门判断。

## 评审建议

`WorkflowEngine.review_template(prompt)` 返回“worker 执行 → reviewer 独立评审”的两节点模板。创建的 DAG 若缺少覆盖全部分支的最终 reviewer，会返回 `review_recommended:true` 和 `warnings`，定义保持原样。引擎不会静默增加节点或把评审文本转成自动修改授权。

最终 reviewer 接收前序资料，并被要求独立核查证据、区分事实和未验证事项。运行时负责为 reviewer 配置独立模型上下文并限制其工具权限。没有配置该角色模型时，节点失败会如实记录；不会冒充已经完成评审。

## 持久化与事件

工作流定义存于 Store 的 `workflow`，运行快照存于 `workflow_run`，每个节点会话使用正常的 `session`。状态变化产生 `workflow.updated`，数据为 `{"workflow_id": ..., "run": ..., "node_id": ...}`。运行级事件没有 session_id；节点事件关联节点会话，因此全局事件流和该会话事件流均能收到。

服务重启时，残留的 queued/running 运行和未完成节点标记 `interrupted`，对应会话回到 idle；已完成输出保留。不会自动重跑工作流，避免重复写文件、执行命令或调用外部工具。

恢复与父运行收尾先按真实状态或精确 `parent_run_id` 筛选，再按稳定 ID 分页，不能把最近历史列表当成活动全集。恢复覆盖 `session/run/approval/workflow_run` 的 `pending/queued/running`，会话回到 idle，其余标记 interrupted；paused 与已终态记录保留。分页不使用会被恢复更新的 updated 或 OFFSET。父完成逐个等待同一次父 run 的全部子工作流并核对结果；停止只取消原父 run 下尚未完成的子工作，不扩大到同一会话后续运行。

内部候选页只从 SQLite 投影 ID，再按需读取单个记录；未完成对象部分索引与父运行索引避免反复解析无关历史。首次创建索引仍需 SQLite 扫描已有对象一次，不隐瞒迁移成本；不会删历史、重新提炼、启动模型或新增后台轮询。

## 活动运行管理

工作流页将全局进行中的执行与最近定义/历史分开。同一定义可以同时运行多次，每个活动行显示独立完整 run ID，并调用现有 `POST /api/workflow-runs/{id}/cancel` 精确取消；历史结果不会遮住另一项仍运行的任务，旧定义不在最近列表中也能从活动入口找到执行。

`GET /api/workflow-runs/active?after_id=&limit=50` 先筛选 `pending/queued/running`，再按 ID 升序分页。`limit` 为 1–50，响应为 `items/next_after/has_more`；每条仅包含 `id/workflow_id/name/status/created_at/updated_at`，不附带节点提示、结果或正文。旧记录缺失时间时返回 null。仍有下一页时 `next_after` 是本页末条的完整 ID，否则为 null；当前 anchor 已结束或删除也可以继续读取。

ID 游标是位置，不是快照、已读回执或活动总数。新运行可能排在旧游标前，显式刷新或收到工作流更新事件后从首页重新核对，不自动连续请求所有页。读取失败明确展示错误，不把失败当作空闲；本地点击取消也不能先于真实返回将运行标记完成。`GET /api/workflows` 保持最近定义和历史的兼容合同，这个旧列表不再作为活动判断依据。

`DELETE /api/workflows/{id}` 直接检查整个定义范围的未完成存在性，有任何 pending/queued/running 都返回 409；停止和删除是不同操作，删除不会暗中停止执行。终态运行历史继续保留。接口复用已有登录、同源防护和不缓存响应，参数错误返回 422。

`tests/test_workflows.py` 使用临时 SQLite 和模拟回调验证 DAG、独立评审、共享并发、未完成运行数量限制与名额释放、失败传播、取消（包括首次调度前取消）、总时限、输出预算及重启恢复，不调用真实模型或外部工具。
