# Workflow 结果回原渠道

状态：2.28 独立切片，基线 `28e638589bf61ef95efb950c35fe2df5651a540f`；实现、受限回归与独立源码审查已完成，待根分支集成，不代表已发行或真实平台送达。

基线 `_workflow_tool` 只冻结任务执行范围，`workflow_run` 没有来源。主 driver 可以先为 A 创建工作流，再领取 B 的输入创建另一个工作流；它最后把所有子结果合成一条无路由 assistant 消息。直接给该汇总套最后来源会把 A 的结果发送给 B。

## 最小合同

- `_workflow_tool` 在实际创建定义与运行之前，从当前 `PreparedRequest` 取不可变 `ReplyOrigin`。`WorkflowEngine.start` 仅接收宿主参数，在首次 await 前存到本次 `workflow_run.reply_origin`；定义、节点工具参数及权限不增加收件人字段。任务范围、principal 和回复目的地继续分开。
- 旧运行、没有当前请求的调用及 WebUI 独立启动不推断渠道，不使用主动通知默认。节点会话不直接外发。Team 父会话继续得到本地汇总，由现有 Team 结果报告继承原来源；Workflow 不绕过父报告链再发一次。
- 保留原失败语义：父 driver 必须先结束 `_execute` 的完整工具批次，并等待该父 run 的**全部**子工作流成功，才写任何工作流结果消息。任一失败、取消、中断或父等待被取消，都不发布先前成功子项的部分报告。现有取消、超时和父子范围规则不变。
- 保留一个总 `max_output_chars`：按原 child ID 分页顺序计费名称、分隔符及结果，继续检查预算耗尽后的所有子项。`run.result` 保留原有有界汇总；主会话按每个子工作流的贡献拆消息，各消息文本总长不超过该总预算。只裁结果文本，完整工作流 ID、消息 ID、input ID、route 等协议字段不截断。预算用完的子项只在工作流详情可查，不额外补发。

## 发布边界与原子性

在所有子项验证成功后，由 parent driver 同步发布有贡献的结果片段。每条使用其自己的已冻结来源及最多一个 `delivery_route`，不按当前会话最新输入选址，也不把 A/B 放进同一条可外发消息；不再追加第二条聚合外发消息。

现有 `workflow_run` 增加 `result_message_id`，仅证明本地结果消息已提交，不代表外部送达。使用既有 Store 的事务内 `_event` 原语（`Store.event` 的内部实现），在一个 `store.db` 事务内写各 `message.completed`、消息和对应 marker，整批提交后仅一次 `bus.notify`。不嵌套会提前提交的 `Store.event`；不增加表、对象种类、队列、定时器或恢复扫描。失败回滚不留下半条消息/marker；重复发布遇到已有 marker 不另建消息。发布中没有 await，取消不能切开已提交批次；提交后的取消仍保留已成事实，沿用现有终态边界。

Channels 继续按稳定消息 ID 建 outbox，复用账号/owner/group 重验、blocked、unknown 和显式重试。失效来源不改投最近或默认渠道；重启不自动重跑节点或恢复未知发送。父在发布意图提交前崩溃时，已完成子结果仍可从工作流详情查询，本期不自动补发；提交后则依已有事件游标/outbox 恢复，不重造结果消息。

## 门控验收

先在旧代码上验证缺少来源与 A/B 汇报缺口，再实现与复验。离线 FakeClient、真实临时 Store 和事件门控覆盖：同一主 run A→B 创建、完成反序；await 中新输入不换源；原 tool batch 全部有回执后才发布；全部成功前无片段、失败/取消无部分结果；多来源共享一个小总预算且最后一页失败仍被检查；Team/旧/API 本地；事务中途失败、重复 finalizer、重开与 outbox unknown 不重置；撤销来源 blocked。所有本地检查等共享守卫明确交接后逐组执行，不操作真实平台或服务。

本片沿用 [Python shield 的调用者取消边界](https://docs.python.org/3/library/asyncio-task.html#asyncio.shield) 与 [SQLite 连接事务上下文](https://docs.python.org/3/library/sqlite3.html#how-to-use-the-connection-context-manager)，不改变现有任务收尾机制。

## 实测与审查

两条门控反例先在旧生产代码上分别失败：创建定义过程中切换请求后，运行缺少原始来源；同一主 run 真正领取 A/B、完成两次工具批次并等待反序结束的子工作流后，没有两条独立来源结果。最小实现后两例通过，扩充后的新文件共 25 项通过（0.46 秒）。新回归额外覆盖总预算只剩 0/1/2 字符时不向另一来源发送分隔符、后续失败阻止整批、父取消、第二条 marker 写失败全部回滚、幂等发布、提交后通知被取消、重开不补发、Team 实际父报告链，以及真实结果消息进入现有 outbox 后的撤销与 unknown 保护。

既有测试有去重 234 项通过证据：Workflow/作用域 59 项；终态与活动接口 18 项；Runtime/会话终态/原路报告 55 项；Team 五文件与 Channels 102 项。原终态测试唯一预期调整是允许新增 `result_message_id`：移除该字段后仍比较完整原对象，包含 status/result/nodes/结束时间；marker 另核已提交的 assistant、父会话、子工作流、完整汇总文本及 `message.completed` 事件。两条晚取消参数均通过，未改原取消行为或弱化终态断言。

新增 25 项与既有 234 项合计 259 项；修改的 4 个 Python 文件通过 Ruff/格式检查，两份源码通过 mypy。全部经共享 `check_local.py --seconds 30` 串行执行，测试最高峰 155.4 MiB，mypy 最高峰 231.2 MiB，swap 为 0。首轮存量子项夹具替换了 executor 却使用持久入站入口，导致输入没有被领取而合法再运行；夹具改用直接本地 `Runtime.start`，实际 A/B 用例仍使用真实入站、executor 与工具回执门控。没有运行模型、真实渠道、用户服务或子进程，新测试也明确禁止进程创建。

父与独立 reviewer 已静态核对冻结来源、总预算、整批事务、Team 父链及晚取消 marker。此证据不覆盖真实外部平台送达、进程强杀或父发布前崩溃后的自动补发；后一项仍明确不提供。根分支集成与远端 CI 尚待执行。
