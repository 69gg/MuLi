# 已授权的定时研究委托

状态：v2.57 开发候选，正式离线旅程、独立策略和容量收尾屏障已通过，尚不代表发行或真实账号验收。基线为已发行 v2.56 的签名提交 `7beae8e7524c2416344db528dd585e55c29a0fce`。本轮根另已实收8项 API 与287个不同前端节点通过，后者含6项新增；各范围分开记录，不将界面通过替代执行旅程。

主人在私聊7说：“明早首次搜索指定主题，以后每周整理，经执行队友和独立评审后把文字成果发回来。”沐璃持久保存这份明确委托，未来由现有时钟触发固定 worker→reviewer 工作流，文字回原私聊，Web 保留过程和结果。后来私聊8的输入不替换原来源。首片只做精确取消，不承诺活动 DAG 暂停、自动发送文件、任意 DAG、完整日历 cron 或进程未运行时唤醒电脑。

## 已有能力与真实缺口

本轮前，`Runtime._workflow_tool` 的 `workflow_create` 创建后立即启动；`WorkflowEngine.review_template(prompt)` 已提供固定执行与独立评审两节点。`Reminders` 已有持久 due、固定 interval、漏期合并和无需模型的空闲等待；普通到期通知走 Attention。本候选补“未来启动明确计划”的入口，普通通知的只读工具限制保持。

原 `WorkflowEngine.publish_results` 由创建工作流的父 Runtime 运行收尾调用。定时委托的创建运行早已结束，不能把旧 parent run ID 当成未来活父，也不能把通知正文重新 `submit` 为 owner。候选持久关联准确 occurrence 与 workflow run，完成后由宿主沿原发布事务生成唯一文本消息，复用 `result_message_id` 与渠道 outbox。

## 最小公开合同

- 新主协调 write 工具 `workflow_schedule(name,prompt,due_at,interval_seconds?)`。`due_at` 为未来 Unix 秒，重复间隔沿现 Reminders 合法范围；每周首先表示从首次时间起的固定七天间隔，不承诺时区或夏令时日历语义。固定复用 `review_template`，模型不能传 origin、mode、epoch、角色、任意 steps 或宿主授权字段。
- 创建仅接受当前真实 owner 主工具上下文。首片发布独立委托：宿主冻结准确创建输入/运行、原 `ReplyOrigin`、当前执行模式上限及固定计划版本，真实主入口的 `execution_scope` 与已保存 `snapshot` 为 `None`。选中或读取一个 commitment、进入重规划上下文，都不取得该任务的执行绑定；独立委托也不自动受某个任务的暂停或终态控制，撤销使用本委托的精确 cancel。代码保留严格快照解析与范围守卫作为防御，不将其称为公开 taskbound 委托能力或已验证的绑定旅程，不用最新任务快照给旧计划补授权。
- 管理入口选择窄 `workflow_schedule_list` 与 `workflow_schedule_cancel(id)`，提供有界当前计划目录、next due、启用/撤销状态及准确 occurrence/run 历史定位；不加载无限历史。按本轮准入合同，取消先同步推进授权版本、撤销未来接纳，再 await 既有精确取消关联在途 workflow run 的收口；不是活动 pause/resume，不宣称追回已经发生的副作用。列表/历史字段在生产阶段按现分页约定收口。
- 每个 occurrence 的 key 绑定 schedule ID、单调授权修订和原 due；同一 key 最多登记一个实际 workflow run。最后同步核验和 occurrence/run/节点关联原子提交，不得先 start 后补 receipt。显式编辑/取消/删除后重建不能让旧证明复活。每计划不积压多份待执行；漏期沿现周期合并，不补发一串任务，不新增 AI 轮询。
- 仅准确、仍有效的独立委托授权可以触发工作流。通知、普通文字、伪造事件和普通提醒无执行权；不放宽 `system:attention` 的 read 门。启动不占用/插入 owner 主队列，不取消当前主运行；复用现忙碌和容量边界等待可接纳时机。每次节点效果前仍重核本委托的撤销版本、准确 occurrence/run 关联、维护/停止/关闭和原工具权限。原 TaskControl 守卫保持，首片不据此增加任务绑定或任务暂停能力。创建计划不是未来所有工具的批准，ask/auto/yolo 沿原规则，触发时只能收窄权限。
- 完成文字仅在全 DAG 成功且准确关联有效时沿冻结来源发布；消息、发布标记和 occurrence 完成意图原子保存。发送时仍复核当前 owner/账号绑定；未知副作用或 outbox unknown 不自动重做。普通提醒、现有立即工作流、TaskControl 和 Store 七格式语义保持。

## 最小复用接缝

候选复用 Runtime 的主协调工具上下文与最终守卫、Reminders 的现有时钟、WorkflowEngine 的两节点模板与结果发布。窄 `ScheduledWorkflows` 保存计划及 occurrence 关联，使用现 Store 对象/事件，不新增数据库或调度引擎；同步 `_admit_start` 用一次 `put_many` 保存 occurrence/run/节点后才启动任务，创建定义与计划也同事务保存，正常立即启动合同保持。容量退避覆盖已持久终态但任务尚在收尾的窗口：引擎统一按实际未结束 driver 判断容量，时钟在推进 due/occurrence 前复核，满时等待原 `on_idle` 唤醒，不吞掉其它 ValueError 或终止普通提醒时钟。独立终态屏障已由根实收1项通过（pytest 0.46秒，守卫1.130秒、87.9 MiB）；其首版夹具在直接 start 前置未过，改用真实 Web submit 后验证，不将此前提错误计作生产红。

正常重开后，尚未触发/尚未接纳的有效 occurrence 可以接纳一次；已开始的 queued/running 工作流沿现恢复合同变为 interrupted，不自动重跑可能有副作用的节点。已经完成但未发布的准确关联可以仅补本地发布意图，不重新调用模型或搜索；已经发布不重复发。固定周期下一次是新 occurrence，不能把上次未知操作当成功继续重复它。

## 第一条正式旅程与目标位置

`tests/test_scheduled_delegation_runtime.py::test_owner_schedules_a_reviewed_delegation_through_the_real_phone_runtime` 使用真实 OneBot webhook、ASGI、ChannelApplication、Runtime、临时 Store 和适配器；只有模型策略与 HTTP 为受控离线输入。认证后的现有 session PATCH 明确设置 yolo，避免把审批前置失败当功能缺口。

旅程以真实 owner 私聊7输入，先调用 `clock_now`，再把实际未来时间交给 `workflow_schedule`；模型收到真实工具回包后正常结束创建对话。根首跑时工具已可见，owner 来源、实际时钟回执、工具失败回执后的 ACK、主 run 正常收口、原7出站及 Web 前置均通过；`schedule_result` 实际为含 AttributeError 的失败回执，正式测试在第319行失败。该首红不是缺 import/API，也不是到期超时；该次运行未成功保存计划，未进入后续成功分支。

修正后第二轮已越过创建，实际 worker 与 reviewer 均为 completed，真实搜索一次；等待 `workflow.result_message_id` 的4秒屏障超时。原 `on_idle` 绑定了被观察夹具替换前的 Event，改为调用时取得当前 `reminders.changed` 后，正式旅程由根实收1项通过（pytest 0.57秒，守卫1.439秒、94 MiB）。两次失败保留，不将节点完成单独算作文字已回原私聊。

预算沿既有正式旅程：16000 上下文字符、6000 输出字符、3 pending；每请求至多32条历史，创建与管理各最多三轮主请求，另一私聊问候一轮，执行队友两轮、评审一轮。4秒事件等待与3秒渠道/调度观察等待不增加。有限失败诊断仅状态、异常类型、轮数和计数，总长不超过1 KiB；不输出消息图、路径或凭据。

同一 selector 已布置完整成功分支：另一私聊8独立主运行完成后，正常 close/reopen 原 Store；受控时钟仅替换原 `muli.reminders.now`，原 Reminders 调度器处理 due。真实 `web_search` 通过 SearXNG MockTransport 取得带不可信标记的合成来源，实际 worker→reviewer 独立会话完整执行，评审下一次真实请求必须含前序实际结果。准确 occurrence/run/完成消息发布标记和原7唯一文字出站、Web 均核验；原8不得收到成果。重复唤醒原时钟不生成第二 occurrence、搜索或模型请求。

最后私聊8通过实际管理工具目录和真实 `workflow_schedule_list` 回包定位原计划，再精确取消；推进到下一周并观察真实调度器进入下一次等待，要求历史 occurrence/run 保留且零新搜索或模型轮。此正式旅程的取消发生在第一 occurrence 已完成后；在途取消另由下面独立 policy 验证，容量收尾也另有上述独立屏障回归。

根另实收 `tests/test_scheduled_delegation_policy.py` 五项全部通过（pytest 0.65秒，守卫1.537秒、91.9 MiB、swap0）：真实 owner 工具创建正控与无上下文、真实 worker/阶段资料、Attention 拒授权；ask 真审批拒绝、auto 原审查及触发前模式收紧；原时钟忙闲退避、漏期合并与重复唤醒；真实 API 在 node `tool.started` 等待边界撤销版本并收口，零新文件效果；未接纳计划正常重开一次，以及真实写文件后在途 Store 快照恢复 interrupted、零自动重做。最后一项是临时数据库快照和正常关闭，不冒称真实进程崩溃；取消也不承诺追回已经派发的效果。随后给第一项补已创建 `snapshot is None` 断言及原3秒门内 Team idle/真实报告交接屏障，根单项复验1 passed（pytest 0.39秒，守卫1.017秒、82.5 MiB），不重复累计五项。

正式旅程已通过结果发布、原来源唯一出站、去重和后续取消断言；根另实收12份既有文件176项通过、152份源码严格类型通过，各自范围不与 API、界面或正式旅程重复累计。公开 taskbound 创建/任务暂停联动不在首片能力内；维护和渠道 unknown 等未在本片正式旅程走到的边界，不据此宣称完整验收。固定工具策略只证明协议与宿主合同，不代表自然模型研究质量或真实账号联调。作者和文档审查者均未自行运行检查，根负责串行验证。
