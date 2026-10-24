# 手机主动审批通知

当主助手或队友的真实工具调用等待批准时，向发起本轮请求的已绑定 OneBot / Telegram 主人私聊发送一条审批提示。提示包含完整审批 ID、工具、风险、会话和运行 ID，以及已有 `/审批`、`/批准`、`/拒绝` 命令；不自动推送调用参数、文件路径或完整工具资料。批准只登记决定，不表示工具已经执行或成功。

这不增加模型回合，也不把通知正文当作主人输入。手机 8 随后与主助手对话，不会把手机 7 已发起的队友审批改投手机 8。结果继续沿原运行的既有回复来源返回，审批操作本身不改变任务来源。

## 来源与持久化

Runtime 在进入 Permissions 前显式传入本次 `PreparedRequest.reply_origin`；此时尚未安装工具执行 ContextVar，不能从最近输入或工具 ContextVar 猜测来源。仅准确、完整的 OneBot / Telegram 私聊来源可产生通知；Web、无来源和群聊保留本地审批，没有外部默认目的地回退。

`ApprovalNotice` 只包含 `version=1`、`approval_id`、`session_id`、`run_id`、`reply_origin`。它在 `approval.requested` 事件的独立 `approval_notice` 字段中保存；审批对象保持原来的九个字段。审批对象与 requested 事件通过现有 `Store.event(objects=...)` 同事务提交，提交后才唤醒消费者。

原 ChannelManager 事件消费者把有限提示交给原 outbox，再推进事件游标；不伪造助手消息、不另建队列或轮询。幂等键由固定审批 ID 与完整冻结路由共同确定，保留 `reply_to`。重读事件复用同一投递记录。队列已满时沿用原游标等待，不忙轮询。

## 发送前校验与撤销边界

入队和平台发送前均复用手机审批的有界 `live_approval` 点读：真实 pending future 存在且未完成、九字段审批仍为 pending、会话和运行准确匹配。另核当前 Runtime Task / run 所有权、真实运行状态、停止和关闭、维护接纳，以及当前渠道账号、绑定、配置和管理器代次。

审批通知显式检查 `Admission.allowed`。即使 outbox 已进入原 `Admission.operation()`，维护暂停后尚未进入平台调用的通知也不能借原操作所有者豁免继续发送。有效冻结来源已失效的通知保留为 blocked，后续正常事件继续消费；坏来源不降级为普通自动回复。普通自动回复与 Attention 沿用原有路由和授权行为。

提示必须完整装入原渠道单条文本预算；不足时保留 Web 审批，不截断审批 ID。审批内容详情仍由用户显式查询。真正工具的原审批、任务 epoch 与执行末门不变。

平台调用已经开始后的撤销不能追回已发生的外部发送。结果未知沿原 outbox 的 unknown 语义保存，正常关闭重开不自动补发；unknown 不表示平台肯定没有收到。重开后的历史 pending 行没有原 live future，不能复活审批或发出旧通知。不承诺进程强杀后的外部 exactly-once。

## 验证状态

实现前已取得正式目标红：真实 OneBot 队友等待 `fs_write` 批准、手机 8 的独立主运行已结束、渠道消费者游标已消费 requested 事件，准确断言原手机通知应为一条而实际为零。结果为 `1 failed`，pytest 0.59 秒，守卫 1.247 秒、107.2 MiB、swap 0、exit 1；不是导入、权限夹具或等待超时。

正式离线验收入口是 [test_mobile_approval_notifications_runtime.py](../tests/test_mobile_approval_notifications_runtime.py)：真实 Runtime / Team / ASGI、真实 OneBot / Telegram adapter 和受控 HTTP，检查完整来源、手机命令批准、真实文件写入及原路结果；另覆盖入队后 Web 已批准、停止、撤权、维护和正常重开边界。本地执行记录如下；离线固定模型与 HTTP 替身不证明自然模型质量、真实平台账号或设备联调。

实现后的两条正式私聊旅程2 passed（pytest0.81秒，守卫1.526秒、141.2MiB、swap0），
五个撤销/unknown边界5 passed（pytest1.30秒，守卫2.112秒、154.8MiB、swap0）。
非生产及非测试作者沿实际五生产后审未发现剩余P1/P2；不是只审方案。

扩大23个完整渠道、控制、队友、定时委托、文件/语音及维护文件311 passed
（pytest12.83秒，守卫14.978秒、483.2MiB、swap0、exit0），包含上述7项，不重复累计。
另一组不重叠15个完整Runtime、Team、上下文、权限、恢复、MCP、主动投递、发行与
格式文件479 passed、1 failed（pytest8.60秒，守卫9.665秒、324.9MiB、swap0、exit1）。
唯一失败为旧暂停回归仅拦截bus.emit来观察approval.requested，原子Store.event+notify
路径不经该观察器而在原2秒等待处失败；不能写为工具执行或暂停断言已失败或通过。

旧审批分支改在原2秒内沿bus.changed等待真实live审批，精确核worker、run和tool后
暂停并只批准该准确ID。tool.started最后await屏障、全部暂停状态、结果配对与零效果
断言不变；没有为测试退回非原子审批或增加期限。另新增真实pending通知的正常重开
节点：未先批准或停止真实队友，正常lifespan关闭撤原future，重开后原通知blocked，
保持原route/payload、零HTTP、模型和工具重做。该新增节点单验1 passed
（pytest0.70秒，守卫1.378秒、107.9MiB、swap0）。

最终新通知完整8项与旧任务控制完整29项共37 passed（pytest2.26秒，守卫3.137秒、
187MiB、swap0、exit0）。这些是上述组的子集加一个新恢复节点，不重复累加：
各轮去重累计792个本地节点有通过记录，不声称是同一次命令或同一head的全量。
真实平台账号、网络送达、强杀恢复与自然模型质量仍未验。

153源文件strict/no-incremental且MYPYPATH=src通过（守卫8.078秒、381.7MiB、swap0）。
Ruff首查为新测试导入顺序，格式首查为四文件排版；只用现有格式器修正后Ruff及418文件
格式通过。83包离线锁校验通过，没有改变依赖或持久格式版本；离线sdist/wheel构建
通过（守卫1.422秒、99MiB、swap0），隔离wheel离线安装、导入、兼容声明、资产、API、
提示词、桌面默认关闭、任务控制和lifespan通过（守卫1.247秒、101.1MiB、swap0），
版本2.62。发行内容只读validate通过（守卫183毫秒、19.3MiB、swap0），没有覆写旧notes。
上述检查均exit0，不代替精确签名候选CI、正式发行或外部账号验收。
