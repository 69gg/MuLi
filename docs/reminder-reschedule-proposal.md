# 自然语言改期已有提醒

状态：v2.54 已正式发行。唯一生产文件 `runtime.py` 的窄实现、两条正式旅程及五项独立策略测试均已本地通过，合相关旧回归与发布兼容共249个不同节点；150源码严格类型、Ruff/格式、离线包以及生产和第二旅程的独立后审通过。精确候选十项、正式发行十三项、签名标签及GitHub/PyPI两构件核对已闭合，见[发行记录](review-v2.54.md#发行闭合)。此方案不代表自然模型的时间理解或真实渠道已验收。

用户在私聊7创建目标复盘约定，随后在私聊8说“把这个约定改到今晚，仍在原来的私聊提醒”。沐璃原地调整已有提醒，不要求用户打开管理页，不删除再建，不把收件人换成本次说话的私聊8。

## 最小实现合同

修改前 `Runtime._personal_tools` 的提醒工具只有 `reminder_schedule`、`reminder_list`、`reminder_cancel`；主协调白名单中的提醒写操作只有 schedule/cancel。`Reminders.update` 和 Web PATCH 已经支持编辑，能够保留提醒 ID、创建时间、触发次数、原路由及任务绑定，并为显式编辑推进任务复盘修订号。首片只补同一 `runtime.py` 中的窄主工具 `reminder_reschedule(id,due_at)` 及其原协调白名单，委托现有 update，仅改 `due_at` 和 `enabled=True`，发送原 `reminder.updated` 事件。

- 支持普通提醒与任务复盘约定。任务绑定必须通过既有严格 `followup_binding` 验证，再核当前 `task_status` 不为 done/cancelled；`Reminders._bind` 自身只核合法状态，不能替代这个终态拒绝。核验至同步 update 之间不得 await。关联任务不存在、损坏、done 或 cancelled 时拒绝。普通提醒没有绑定时沿原编辑规则，不能把坏绑定当普通提醒降级。
- 参数仅准确提醒 ID 和未来 Unix 秒；不接收路由、任务 ID、版本、标题、内容或任意 patch。不将模型选择的提醒 ID 当作修改其他用户身份的授权。
- 保留原 write 权限、真实主工具上下文、暂停/停止/维护/禁用边界与原输出预算。Attention 的只读工具集不获得改期能力。
- 显式改期重新启用已有提醒，包括之前已触发的一次性提醒；保留历史次数与最后触发时间。周期提醒仅改变下一次时间，后续沿现有周期调度，不宣称“只挪这次但固定原日历相位”。
- 任务复盘旧 notice 若已持久但尚未最终接纳，使用原单调绑定修订撤销。已接纳、模型已处理或平台已发送的旧提醒不承诺追回。普通提醒不具有该任务绑定撤销证明；不能把任务复盘性质推广为全部已生成通知的撤销承诺。
- 不新增依赖、队列、轮询、持久格式或渠道发送能力。取消再创建会重置 ID/次数并从新输入捕获路由，不能用来替代本合同。

## 第一条正式旅程与目标红

独占新增 `tests/test_reminder_reschedule_runtime.py::test_phone_reschedule_preserves_goal_and_original_route_after_reopen`：

1. 沿真实 OneBot webhook，私聊7进入真实主 Runtime 工具轮，`commitment_save` 创建目标，再 `reminder_schedule` 创建带任务绑定的提醒；从真实回包取得 ID。主会话模式经现有管理 PATCH 明确设置，不伪造批准或持久对象。
2. 私聊8进入主工具轮，以真实 `reminder_list` 结果定位原提醒，同时真实更新目标；再调用准确 `reminder_reschedule`，运行正常收口后，在外层检查真实工具表是否包含该工具。根已确认准确目标红为此工具未启用，不以缺 import 或超时作为功能红。
3. 后续正向 oracle 要求同 ID、原私聊7路由、同创建时间与触发次数、同任务 ID；仅 due/enabled/任务绑定修订变化，不新建提醒。
4. 正常关闭并重开同一数据目录。受控时钟先到旧时间，等待真实调度器空闲观察，要求零通知/零复盘模型轮；再到新时间，原调度器与 Attention 读取真实最新目标，通过原渠道 outbox 只向7发送一次，Web 同时可见。重复通知事件不增加模型轮或投递。

所有模型响应与平台 HTTP 为离线受控输入；真实 Runtime、工具执行、Store、Reminders、Attention、适配器和 outbox 不替换。首片不需要人为生成队友报告或交付验收单；它验证改期后读取最新目标，已有交付读取能力由原目标复盘旅程覆盖。保留 16000 上下文字符、6000 输出字符、3 个 pending run，等待沿原 3/4 秒门，不延长预算。

## 第二条正式旅程：已持久但尚未接纳的旧复盘

新增同文件 selector `test_reschedule_revokes_persisted_unadmitted_followup_before_reopen`，与原 selector 复用有限旅程 helper，不改变生产。真实 scheduler 在旧 due 产生 notice 并令原提醒次数变为1、enabled=false；Attention 捕获真实有效证明后，测试仅在进入原 `submit_attention` 前以有限 Event 暂停，不替代返回或伪造 receipt。私聊8真实主工具改期后，释放该 Event，仍调用原 `submit_attention`，其配置锁内同步最终守卫必须抛出原 ValueError，要求旧证明失效、旧通知正文不变、零 attention ingress/receipt/模型轮。

随后正常关闭并重开：旧时间仅保留原通知，不开始复盘；新 due 沿原调度器产生第2次 occurrence，累计次数为2、绑定版本为2，原 Attention 读取最新目标并只回私聊7一次，Web 可见，重复事件不再生成运行或出站。同步包装只记录原守卫确实拒绝，重新抛出同一异常，不改变决策；不用持有配置锁等待 owner 回调。该 selector 已由根实际运行通过，包含原最终守卫拒绝、零旧 ingress/receipt/模型轮及新 occurrence2 的完整证明。普通提醒没有该任务绑定撤销证明，已经接纳的旧回合不在本测试撤回范围。

## 首跑夹具定位

根首次实跑该 selector：1 failed，pytest 0.55 秒，守卫 1.235 秒、97.4 MiB、swap 0。真实创建阶段已通过，但私聊8的模型轮组装 `commitment_save` 调用时发生 Python TypeError，尚未抵达新工具缺口，不能计作功能目标红。原因是测试 helper `call(identifier,name,**arguments)` 的首参数已经由位置传入，又以 `identifier=task_id` 作为工具参数传入。沿原目标复盘旅程改为 `ToolCall` 加 JSON 参数，并同样修正后续 `commitment_read` 的组装；不改真实前置、生产或超时。

修后根复验：1 failed，pytest 0.60 秒，守卫 1.283 秒、90.8 MiB、swap 0。私聊7真实创建、私聊8真实读取提醒及更新目标、两个主运行完整收口均已通过；失败准确位于 `reschedule_available=False`，实际工具结果为“工具未启用”。这是已确认的功能目标红。

随后授权的候选仅修改 `runtime.py`：原协调工具白名单、两个必填参数 schema、同步 maintenance admission、严格绑定及当前任务状态核验、原 update 和原事件。核验至写入无 await，成功写入后才 await 事件通知。普通提醒不要求任务绑定，坏绑定不能降级；原 update 继续拒绝非法时间。

根首次实收首条正式旅程加独立策略文件共6个节点全部通过：pytest 0.83 秒，守卫 1.520 秒、98.4 MiB、swap 0。首旅程已实际完成正常重开、旧时间不触发、新时间原私聊7一次；五项策略覆盖普通提醒重新启用、任务绑定修订、坏绑定/终态/缺任务、参数及原权限边界。生产非作者独立源码后审未发现剩余 P1/P2；这不是自然模型或真实平台验收。

补第二旅程、提取共同 helper 后，根实收两个正式 selector 全部通过：pytest 0.80 秒，守卫 1.547 秒、103.1 MiB、swap 0；含首 selector 重验，不把此前6节点重复相加。五项独立策略文件未改，故新增合计7个不同节点。相关旧七文件76项通过：pytest 2.35 秒，守卫 3.404 秒、170.7 MiB、swap 0；发布兼容166项通过：pytest 0.95 秒，守卫 1.459 秒、68.4 MiB、swap 0。合计249个不同本地节点。

全150源码strict通过：守卫8.405秒、417.8MiB、swap0。另一位非作者对第二旅程完成正式只读后审，无剩余P1/P2，确认真实通知、原配置锁内最终守卫、游标进度屏障、正常重开及真实occurrence2均非伪造。Ruff首查仅一个import排序，根按原工具整理该处及三个变更文件的纯格式后，全Ruff和399文件格式通过；离线83包锁检查、wheel/sdist、元数据一致性及隔离安装/API/lifespan均通过。签推、精确候选十项及正式发行十三项、GitHub/PyPI两构件核对已闭合，证据见[发行审查](review-v2.54.md#发行闭合)，不据此推断真实账号收发或自然模型质量。
