# 压缩与正常重开后的手机接办验收提案

根已收取本次新增组合及整个旅程文件的实际通过结果；以下仅记录该离线验收范围，不代表全后端、发行或真实外部送达通过。作者未运行检查，也未修改生产代码。

| 根实际执行范围 | pytest 结果与耗时 | 守卫结果 |
| --- | --- | --- |
| 新增 `onebot-compacted-reopened` 节点 | 1 passed，0.73 秒 | 1.414 秒，95.9 MiB，swap 0，exit 0 |
| 本文件 `web`、`onebot`、`onebot-compacted-reopened` 三参数 | 3 passed，0.99 秒 | 1.705 秒，105.5 MiB，swap 0，exit 0 |

此前保留两次实际失败记录。首次新增参数已走过压缩、正常重开与原事件幂等，失败于测试误把 webhook 对内部权限拒绝的成功 ack 期望为直接抛错；修正了该既有 transport 合同的测试期望。随后复验在等待新队友屏障处四秒超时；只读该次临时 SQLite 可见重开后主 run 先报 IndexError，尚无新队友或 handoff-web/spawn-draft 工具事件。原因是测试在首次 create_app 后才安装场景工厂，PluginManager 正常关闭时恢复了它构造时捕获的空基础 FakeClient；第二套 Runtime 继承了错误的测试工厂。将场景工厂安装移到首次 create_app 前，并补上正常关闭后及新 Runtime 的工厂身份断言后，根取得上表结果。第二次失败时定位解析器尚未执行；后续通过结果才覆盖真实 Main → fresh handoff → spawn → 队友解析原定位并读取附件的完整后半旅程。原四秒屏障期限未改。

这里的重开是同一 pytest 进程内正常关闭应用和 SQLite 后重建 Runtime/create_app；模型与平台 HTTP 均使用显式离线替身。它不证明新的 OS 进程、强杀/断电恢复、自然模型接办质量或真实账号/平台送达。上一片候选由根报告已签名为 `3b2f3e29f4045552496fcd3e5caa5bf6e2311f3c`，十八文件 521 项与全仓 147 个 source files 的 mypy 通过；本轮没有重新核验该提交或检查结果。

目标是用一条真实 Runtime 组合旅程补齐：Web 派工 → 原外发只尝试一次并成为 unknown → 手机暂停、追加草稿要求 → 真实压缩并正常关闭重开 → Web 明确派新队友 → 原 seq/revision 读取消息和附件 → 草稿仍回 Web。

## 已有证据与本轮缺口

| 当前代码中的证据 | 已经覆盖 | 不能据此宣称 |
| --- | --- | --- |
| `tests/test_task_handoff_runtime.py::test_owner_replans_failed_unknown_delivery_and_hands_original_evidence_to_draft_worker[web/onebot]` | 真实 create_app、Runtime、Team、LocalServices、手机最终准入、unknown、完整任务控制、指定旧队友 handoff、新队友读取原消息/附件、Web 原路报告 | 两项都配置了 `compaction.enabled=False`，没有关闭重开 |
| `tests/test_teammate_attachment_runtime.py::test_pending_handoff_reopens_same_worker_and_recovers_compacted_references` | 原附件、持久补充、同队友跨 Runtime 重开与原路、原 seq 找回附件 | 为准备历史直接写入 Store 消息，另建 Compactor，并恢复同一队友；不等于本轮真实组合 |
| `tests/test_history_evidence_runtime.py::test_compacted_reopened_archive_can_be_read_from_every_runtime_path` | Runtime 自有压缩器、真实关闭重开、四种执行入口读取原工具证据且不重跑 | 不是手机控制、失败队友换人、真实应用/渠道重组合 |
| `tests/test_mobile_task_control_runtime.py` 中保存后重开、通知失败与重投的用例 | 控制回执持久化、通知继续、epoch 与 ingress 不重复 | 不能替代手机信封经过新 create_app 后的完整接办 |
| `tests/test_compaction_recovery.py` | 持久中断状态修复、unknown 工具尾部与后续压缩 | 它准备的是中断边界，不是本轮正常退出的证据 |

以上是原有路径的只读源码结论。单项已有通过记录沿用其原限定范围；新增组合与本文件三参数的根实测结果见开头，没有扩展为其它集合或发行结论。本次两项失败均定位到测试设置，未据此修改生产代码。

## 选择与改动范围

选择扩展现有 `test_task_handoff_runtime.py`，增加一个 `onebot-compacted-reopened` 参数分支。保留原 `web`、`onebot` 两个参数 ID、原断言和原小场景配置；新参数才启用历史压力与关闭重开。首轮仅需改这一个测试文件，不复制一套约五百行旅程，不新增生产恢复入口或通用测试框架。

这是方案作者作出的实施选择：继续使用 OneBot，是因为当前完整手机旅程已经使用可靠显式账户、签名原始私聊和真实最终 guard。Telegram 的信封/轮询与拒绝语义继续由已有专项回归承担，不在同一新参数中再次展开矩阵。

模型 FakeClient 仅固定工具选择和回应；HTTP MockTransport 仅替代平台 HTTP。场景模型工厂必须在首次 create_app 前安装，使真实 PluginManager 捕获正确的基础工厂；应用正常关闭后及第二套 Runtime 构造时核对其身份，不绕过插件关闭逻辑。保留 `create_app` 创建的当前 Manager、adapter 及闭包身份，每个阶段只给该 adapter 的 HTTP 客户端接入显式替身。禁止替换 Runtime.submit/submit_mobile_task_control、TaskControl、Team、工具 handler、报告投递或压缩器。

“正常重开”的准确证据边界是：在同一 pytest 进程内完整退出第一套应用 lifespan，等待所有关闭完成，关闭 SQLite，再从同一路径新建 ConfigFile、Runtime 与 create_app。第二套对象不能引用第一套 Store、LocalServices、Manager 或 adapter。这验证磁盘状态重建与正常关闭合同，不声称已经执行了新的 OS 进程、强杀恢复、断电恢复或真实服务重启。

## 原恢复路径必须保留

- `Runtime.__init__` → `Store.open_runtime` 先接纳数据库格式与身份，再 `_recover`、TeamManager、Runtime 自有 Compactor。重开不能跳过格式门、修改格式分类、手工重新指定主会话或回写旧 Python 状态。
- `create_app` 重新构建 Attachments、LocalServices、ChannelApplication 与其余正常应用组件；不能用单独 attach_services 冒充第二套完整应用。
- 应用 lifespan 正常启动会调用 Runtime.startup、渠道 start 和既有持久任务/报告唤醒。当前流程先达到空闲，重开不能靠清空 ingress、报告或队友记录压掉意外工作。
- 应用关闭会先结清配置事务与应用组件、关闭 ChannelApplication，再 Runtime.close 结清压缩器、Team、前台任务和 finalizer，最后关闭 Store。第一套未退出前不能开第二套 writer。
- ChannelManager.recover 只将 `sending` 改为 unknown；原本已 unknown 的投递不能重新排队。失败旧队友也不应被当作待恢复运行中的代次。

## 完整验收步骤

### 1. 保留原 Web 派工与 unknown 失败

沿用现有 Web 上传、记忆建立、`commitment_save`、`spawn_agent`、`wait_agents`、`channel_send`、`channel_status` 与旧队友失败流程。保持原文件正文唯一标记只存在附件中；主助手与队友在显式读取前均不能获得该正文。

新分支仅增加两份有界历史样本：最初 Web 派工正文中的非授权背景资料、原 `channel_send` 的外发正文。二者均走现有接纳/工具处理器落盘；不直接 `Store.add_message`、不注入 object/summary、不重新运行旧外发来制造历史。背景文本明确是资料，完整任务限制仍由原 brief 和后续主人要求保存，不添加新的执行权限。

新分支配置使用合法的较小上下文，例如 `max_context_chars=32000`、`compaction.mode="extractive"`、`trigger_ratio=0.25`、`target_ratio=0.2`；资源守卫和运行时限保持原值。样本长度从该配置的触发字数派生，加一份固定小余量，并限制在现有入站/工具参数上限内，约为一万字符级；不靠扩大预算或无界循环凑压缩。

该选择的作用是让主会话和旧队友各自产生足够的真实历史，同时保留至少三个完整 replay group。摘要由 Runtime 自有 Compactor 的既有 extractive 路径产生，不替换 `_summarize` 或 Compactor，也不构造第二套压缩器。

原外发正文仍按 OneBot 当前 `max_message_chars` 切分，保留默认四千字符限制。HTTP 替身只在精确匹配真实首段时抛 ReadTimeout，不与万字全文作相等比较，也不提高分段限额。持久 outbox 必须保存完整各段，进入 unknown 后 `next_part=0`、`receipts=[]`、`attempts=1`、实际首段 HTTP 一次；后段一旦进入替身就失败，不能把它算作手机确认。

### 2. 手机暂停、调整、确认方案

沿用签名 OneBot 原始私聊：`/任务` 取得当前票据，复制返回的完整 `/暂停` 指令，再复制暂停回执中的 `/调整` 指令并追加完整“只交草稿，不得发布”。完整事件键由真实 Manager 派生，不从短码伪造入站记录。

必须继续核对：

- 纯查询/暂停不增加模型输入，纯暂停后原代次不重启；相同暂停事件重投只生效一次。
- 调整只产生一个携带完整要求的任务控制 ingress，`route=null`，引用正确任务、领域回执和 epoch。
- 主助手真实 `commitment_read` 选择任务，下一轮持完整快照调用 `task_approve_plan`；`targets=[]`，不能确认即派工。
- 旧 handoff 包 revision 因任务状态改变而失效；重新取得的资料仍显示失败执行与 unknown，而非成功或已回滚。

完成已有重规划轮次后，记录主会话 ID、Store instance、原队友 ID/代次/ReplyOrigin、控制双版本、三个手机回执原文和确认投递 ID、原 unknown 投递 ID/attempts，以及 handoff 中原附件消息的 `read.session_id/seq/revision`。这些测试期望用于比对，不能注入第二阶段模型请求来代替读取。

### 3. 核实真实压缩落盘，随后正常关闭

等待真实 run、队友报告与三条手机确认进入原有稳定状态。主/旧队友没有待消费 ingress；这必须是工作正常完成的结果，不能删除或取消业务记录来达成。

在完整工具批次结束后，对现有 `runtime.compaction` 做一次 observe/finish 以结清已有候选。自动收养可能已经发生，不能把 `finish()` 的布尔值当作唯一成功依据，也不能循环追加历史或反复触发直到通过。必须直接断言持久事实：

1. 主会话与旧队友各存在真实 compaction/compaction_snapshot，generation 大于零，且属于各自会话。
2. 主会话 `covered_seq` 覆盖原 Web 派工消息；旧队友 `covered_seq` 覆盖前述带附件的原消息及已关闭的原外发工具批次。未覆盖则本新参数失败，不能只看摘要存在。
3. 原消息对象、消息顺序、附件引用、文件 SHA-256，以及原外发工具结果均未因压缩变化。原 unknown 仍只有一个尝试。
4. 活跃投影确实来自既有 summary+未覆盖后缀，并保留完整工具批次。Compactor 可以合法保留用户 anchor；不把“所有上下文中附件 ID 消失”作为伪前提，任务 brief/显式交接本来也可能含 ID。

正常退出 Web client 与应用 lifespan，等待 ChannelApplication 和 Runtime 的关闭任务结束，最后退出平台 Mock 客户端。第二阶段只能在此之后启动。测试应核对第一套 SQLite 已关闭；finally 释放现有屏障并结清两阶段，不靠遗留后台异常使测试表面结束。

### 4. 从持久文件重新组合，并检验重投/权限

用新的 `ConfigFile` 读取已有 config.json，再用同一 data_dir 创建新的 Runtime、新 create_app、新 Manager/adapter 和新 HTTP 替身。不要用会写入默认模型配置的 `make_runtime` 作为第二阶段恢复器，也不要把第一套内存缓存或工具实例搬进去。FakeClient 工厂可以继续使用测试脚本与期望 ID，但所有业务资料必须由新 Runtime 的真实读取路径提供。

进入新 lifespan 后、发送新 Web 派工前核对：同一 Store instance 和主会话，控制双版本不变，原旧队友仍 failed 且 generation/ReplyOrigin 不变，原 compaction snapshot 与原附件均可读，unknown 的 state/attempts 保持不变。没有新用户输入时不能自行出现新的旧队友执行或主模型派工。意外 FakeClient 请求要记录并在测试主协程最终断言，不能只在后台协程里 raise 后被 Runtime 作为普通失败吞掉。

重投原三个手机事件，读取既有 `channel_inbound.receipt` 原文并比对固定正文、票据和来源；domain receipt/控制 ingress 数量、双版本、确认 outbox ID 与 attempts 不变。已保存的控制回执可以重新执行原通知路径，但当前没有 pending 工作时不能凭此派新队友或制造模型输入。

再做两项小反证，仍走真实签名 webhook：

- 新事件复用原 `/任务` 的旧暂停码，必须得到稳定 epoch_conflict；不能动态换成当前版本。允许产生该次固定拒绝回执与一条手机确认，原任务控制版本和领域 ingress 不变。
- 使用另一个未绑定主人、其自身私聊目标一致的完整签名信封，尝试同一旧码，必须在权限门拒绝，不能产生控制入站回执、领域变更、确认出站或模型输入。保持现有 webhook 合同：它捕获 receive 层的 ChannelError 403 后返回 `{"ok":true}`，用成功 ack 加上述无副作用断言核验拒绝，不把 ack 当作控制准入成功，也不期望 webhook 直接抛 403。不伪造 `principal=owner` 或直接调用核心服务。

最后的投递数量按事件键与种类核对：原 unknown 一项、原三个确认不重复、新旧码拒绝确认一项；非主人及最后 Web 报告不得增加手机投递。不要把原先硬编码“总共四项”留在新分支，导致额外合法拒绝确认破坏计数含义。

### 5. 新队友从原定位读取，并把草稿交回 Web

主人经新 Web 消息明确要求读取旧 handoff、派新队友接办。主助手经第二套真实工具重新取得指定旧队友的 handoff，不能复用第一套返回包冒充新读取。保持 `source_completeness=recent_window_only` 与 `authority=coordination_data`；当前有限窗口应仍含原附件消息，缺项必须失败，不能扩大窗口、扫描更旧历史或从另处偷偷补 ID。

取得的原消息 `session_id/seq/revision` 必须与关闭前保存的定位完全一致。关闭前定位只作为断言 oracle；主助手把重新读取的 fresh handoff 定位写为 spawn_agent 的一行有限 JSON。新队友先解包实际 user 消息中 Team 保留的 `sender/content` JSON，再从 content 解析这一行，核对唯一性、字段集合、类型和字节上限，最后用解析值调用 `conversation_read`。不能用测试闭包中的旧 ID/seq/revision 直接组成工具实参。测试主协程还仅删除该真实请求 content 中的定位行，断言同一解析器拒绝它，来源信封、其他消息内容与字段保持原值。

handoff 包 revision、消息 evidence revision、控制 epoch/approved_epoch、压缩 generation 是四种不同版本，不能互换。压缩不改原消息证据版本；若包 revision 改变，先比较其绑定的任务/队友/报告/记忆事实，不用随意放宽断言掩盖变化。

沿用原 `spawn_agent`，显式给新队友同一 commitment_id 和从 fresh handoff 得到的附件 ID；新队友 ID 必须不同于原队友。新队友先真实调用 `conversation_read(session_id=原会话, seq=原序号, revision=原证据版本)`，核对返回 seq/revision 与结构化附件引用，再调用真实 `attachment_read`，最终核对附件正文唯一标记后仅交草稿。不能从测试闭包、摘要或提示预先提供文件正文，不能绕过原读取 handler 或重跑旧工具。

保留原工作者无 `task_approve_plan` 权限、继承父会话 mode、同任务绑定等断言。新队友不调用 channel_send、audio_transcribe，原 unknown 请求数和 attempts 全程仍为一。报告经既有 team_report → 主会话 ingress → Runtime 最终回复，不能直接写消息或模拟报告完成。

“回原 Web”的具体断言是同一主 Web 会话、最终 `delivery_routes=[]`、新队友 ReplyOrigin 的 `kind=web` 且 input_id 等于本次明确 Web 再派工消息。旧队友仍保留最初 Web 派工的 ReplyOrigin；不得为了对齐字面值强行把新队友改成旧 input_id，也不得因最近手机控制而采用手机路由。

最终外发数量断言前，先取得这条最终助手消息的持久 `message.completed` event ID，再通过真实 Manager 的 condition 等待 `channel_state.events` 至少达到该 ID。不能只等助手正文已落盘或手机已有三个确认，就声称异步渠道消费者已经核查过最终报告。关闭前对确认方案消息也采用同一屏障，避免在消费者仍落后时截取 outbox 基线。

## 最小实施与检查顺序

1. 测试改动只在 `tests/test_task_handoff_runtime.py`：为新参数增加配置与有界样本；以标准 AsyncExitStack 正常退出第一套平台 HTTP、Web client 和应用 lifespan，再接入第二套；复用 Main/OldWorker/DraftWorker、phone_control、call、tool_result、completed、eventually、wait_agent、terminal 和已有 HTTP 屏障。两个旧参数行为保持，最终持久事件游标屏障同时强化三项。
2. 第二阶段创建全新组合，闭包中的当前 runtime/manager/web 引用必须明确切换；旧对象只供关闭/身份对比，不能继续接纳请求。两个阶段的 HTTP 客户端分别拥有生命周期，不复用旧 adapter。
3. 根已串行完成新增 `onebot-compacted-reopened` 节点，1 passed、pytest 0.73 秒、守卫 1.414 秒/95.9 MiB/swap 0/exit 0。前述 webhook 期望错误与错误工厂导致主 run IndexError 的两次失败保留，不能以最终通过倒写为首轮即通过。
4. 根随后完成本文件三个参数，3 passed、pytest 0.99 秒、守卫 1.705 秒/105.5 MiB/swap 0/exit 0。`test_teammate_attachment_runtime.py`、`test_history_evidence_runtime.py`、`test_compaction.py`、`test_compaction_recovery.py` 与手机核心/渠道相关集合继续按根各自收取的结果记录；本文件不据上述三参数宣称它们或全后端通过。检查仍由根经 `/data0/MuLi/scripts/check_local.py` 分批串行，沿用共享 2 GiB/swap 0/1 CPU、单次 1 GiB/180 秒，不裸跑、不并发占槽。
5. 根统一执行必要静态检查、独立后审和文档整合。本文件、`task-handoff-journey.md`、`mobile-task-control-proposal.md` 只能在实际结果收取后写“新增组合通过”，不提前修改发行结论。

新增节点预计为：

`tests/test_task_handoff_runtime.py::test_owner_replans_failed_unknown_delivery_and_hands_original_evidence_to_draft_worker[onebot-compacted-reopened]`

## 若出现真实失败，先收什么证据

| 首个失败边界 | 最小只读证据与定位 | 不可先做的改动 |
| --- | --- | --- |
| 压缩未覆盖原消息/工具批次 | 两个会话的真实消息 seq/group、Compactor status、snapshot covered_seq、合法配置与 pending/ready 状态；对照 `observe/_prepare/adopt` 和 Runtime 正常收尾 | 手写摘要、另建 Compactor、删消息、加重跑循环或扩大资源预算 |
| 第二套 Store/Runtime 构造失败 | 正常关闭是否已结清、异常原类/code、格式证据与身份差分；对照 `Store.open_runtime`、`Runtime._recover`，只取有限日志 | 改格式分类/超时、补造身份、跳过接纳或吞存储异常 |
| 重开后 unknown 重发或旧代次启动 | 原 outbox ID、state/attempts、实际 HTTP 次数；原 agent/run/generation、pending ingress、报告；对照 Manager.recover/start、Team 初始化/start 与 Runtime.startup | 把 unknown 改成 sent/failed、清队列、禁止正常恢复以凑结果 |
| 手机重投多一条控制/确认 | 原完整 inbound/event 键、回执原文/摘要、领域 receipt/ingress、双版本、outbox 事件键；对照核心缓存路径与 Manager 本地 outbox 修补 | 换事件 ID、重新保存旧回执、直接调用领域 duplicate、跳过当前 guard |
| 原 seq/revision 或附件不可读 | fresh handoff 的原始索引、message_order、HistoryEvidence 返回 code、原消息/附件哈希；对照 LocalServices scope、TaskInspection 与 conversation_read/attachment_read | 从摘要造正文、绕过版本/权限、传第一阶段工具结果或放宽到另一消息 |
| 草稿泄到手机或报告未回 Web | 新 Web 输入 ID、新队友冻结 ReplyOrigin、team_report/ingress、最终 delivery_routes 和持久渠道事件游标；对照 Team 报告投递与 Runtime 请求来源 | 重写来源、借最后活跃渠道、直接插最终消息或在替身里伪造送达 |

只有实际失败指向可复核生产缺陷时，才据最小 red 另提修复边界。本方案未修改 Runtime、Store、任务控制、权限、渠道或压缩生产实现。本组合虽已取得上述离线通过结果，仍未验证自然模型接办质量、真实账号/平台送达、未知远端副作用是否发生、OS 进程强杀/断电恢复或托管 CI 全平台表现。
