# 约定时间复盘指定个人目标

2026-10-24，基线 `312f4abc1bc2e562829b64eaddece9fef6cb84dc`。v2.49 候选的正式旅程、十项接纳负控和一项非作者源读取负控均已有通过终态，全 150 个源码严格类型检查、Ruff 与 390 个文件格式通过；本地发布/兼容及离线构建安装验证已闭合，合计 279 个不同测试节点。签名候选、远端 PR/CI 和发行尚待完成，见[候选审查](review-v2.49.md)。以下保留逐轮先红事实。

根首跑 1 failed，pytest 0.60 秒，守卫 1.290 秒、94 MiB、exit 1；准确停在 `schedule_available=False`。当时 `reminder_schedule` 虽已登记为 write 工具，却没有列入主会话协调工具，故尚未触及参数绑定断言，更未运行到期链。随后仅补两个内建提醒名称的协调登记，根复验 1 failed，pytest 0.60 秒，守卫 1.330 秒、91 MiB、swap 0、exit 1：原工具可用和轮数断言通过，真实 accepted 工具结果为 `{"error":"工具执行失败：ValidationError"}`，准确在缺少 `followup` 断言取得参数绑定入口红。拒绝来自 schema，不是 handler TypeError；所有任务、要求修改、队友交付和验收前置均成立。此后才授权当前绑定与到期链实现。

用户可以说：“周末提醒我复盘这个错题目标，看看已有依据、哪些还没验证、下一步是什么。”到期沿创建约定的私聊回报，Web 原会话也可见；中间关闭再打开仍处理同一任务，另一私聊的普通输入不改约定来源。

首版实现复验曾为 1 failed，pytest 4.67 秒，守卫 5.383 秒、96.4 MiB；绑定、正常重开、真实到期读取最新要求与准确单、说明及首次送达均已通过，末段等待重复事件游标超时，当时尚非整条旅程通过。重复事件跳过只保存 Attention 游标，不产生新总线通知；原夹具只等下一次总线变化，可能在游标实际推进后仍睡眠。仅此观察改用现有 Attention 测试的 3 秒有界 helper，原 4 秒未增加，模型轮数与唯一 outbox 断言保留。

根随后对原 selector 实收完整 1 passed：pytest 0.79 秒，守卫 1.518 秒、95.7 MiB、swap 0。三个实际只读请求、手机 7 唯一 outbox、Web 可见、真实 notice 重放及再次正常重开不增轮次与出站全部通过。此为离线固定工具策略旅程通过，仍非真实模型或平台联调。

## 最小接口与持久合同

先行窄片选择让主助手直接统筹约定提醒：仅把内建 `reminder_schedule/reminder_cancel` 纳入协调工具，保持 write 风险级别及 Attention 仅 read 的过滤，沿真实宿主请求取得来源；不扩大其它个人写工具。原旅程保留工具可用断言，协调接缝落地后根已收取真实参数绑定缺口红，随后才实施绑定与到期链。

既有 `reminder_schedule` 和提醒创建接口增加可选 `commitment_id`，只关联已经存在的准确任务。新增关联以宿主生成的 `followup` 保存：

```json
{"version": 1, "commitment_id": "准确任务 ID", "revision": 1}
```

调用者只选任务，不能自填绑定版本或来源。任务定位按准确主键只投影 id/status，沿原工具输出预算先过 SQL 字节门，再以 BLOB 严格 UTF-8 解码；不扫描任务或把完整 brief 加载到提醒。notice 与提醒证明复用 `Store.bounded_object` 有界读取。普通从未绑定提醒不增加上述字段，现有返回形状和行为保持。

绑定 revision 与任务 revision 分开：任何显式编辑都会推进绑定 revision，包括同值关闭、修改文字、重新关联和解绑。曾关联过的提醒必须保留宿主单调计数，解绑不能抹掉计数，重新绑定同一任务仍继续加一，不能重置为 1 使旧 notice 复活；工具和 API 不能自填宿主 revision。从未关联的普通提醒形状保持不变。自然一次触发会使 `enabled=false`，重复提醒会推进 due；这些调度状态变化不推进绑定 revision。任务要求后来更新不使约定永久失效，到期读取当前完整要求。

到期 notice 与提醒状态在原事务中保存，冻结准确绑定、发生次序和原 `FrozenRouting`。实现前 `Reminders._fire_admitted` 只比较 enabled/due，文案和 interval 仍取旧调度快照；新关联现使用 current 一致快照，防止旧文案与新绑定混合。旧快照窗口由接纳文件的独立节点验证，不借首红声称已验。

`system_events._notification_source` 先核真实 notice/提醒及绑定；`AttentionInbox._consume` 的最终接纳 guard 在 Runtime 配置锁内再次核绑定、发生次序和 routing，随后与原 ingress/receipt 原子接纳。它不要求 enabled 仍为 true，也不要求当前 due 等于已发生时间。删除或显式编辑可以撤销尚未接纳的旧 notice；坏绑定不能降级为普通提醒。只承诺最终接纳前撤销，已经原子接纳或开始的运行不承诺追回。

到期给模型的可信事实条目公开 `followup` 定位，来源核验仍由宿主完成，正文不构成授权。沿原 Attention 只读工具限制，实际读取 `commitment_read` 和 `commitment_inspect(section=deliveries)`；有记录时再按准确单 ID 读取正文。回复区分登记时的宿主文件事实、模型判断、仍未验证的要求和下一步。旧单保留历史标记，不能用作当前验收。

`commitment_read` 返回的当前任务页会完整送达下一次模型请求，与 `commitment_inspect` 一致。自动记忆或历史整理不会截断该页的 JSON 和正文；分页参数、读取范围和输出预算保持原有限制。必要内容整体放不下时会明确报错，助手可改读更小的页。

`done/cancelled` 任务终止只读跟进；暂停或结果未知不触发恢复、批准、派工或重发。绑定不增加执行权限，不修改任务状态，也不自动发送文件。结果沿已有原来源投递和发送时 owner 重核；unknown 保留原回执，不重发。本片复用原 scheduler、Attention、ReplyOrigin 和 Store，不增加数据库、轮询模型或新前端面板。

## 单一正式旅程与首红边界

`tests/test_goal_followup_runtime.py::test_goal_followup_reads_current_requirements_and_delivery_after_reopen` 使用默认应用工厂和生命周期、临时 Store、真实 OneBot adapter/ASGI 入站、真实 Runtime/Team，以及固定工具策略和显式 HTTP MockTransport。

1. 手机 7 实际创建个人目标，再调用已登记的 `reminder_schedule` 并传准确任务 ID。先红阶段即使返回不支持参数的工具错误，主运行仍正常结束；真实工具结果必须已作为 accepted 消息持久化。当前实现已接受并保存关联。
2. 从 Web 修改同一任务的完整 brief，真实直接队友执行 `fs_write → file_publish` 并完成。主助手实际读取附件、登记交付验收单，外层核准确任务 revision、报告代次、运行、附件字节和分栏判断。
3. 所有前置成立后先核工具在真实主请求中可用，再断言 schedule 结果及真实提醒存在正式绑定。先行协调窄片后的准确红为已存在工具的 schema 不接受 `commitment_id`；缺工具、导入错误、Fake 内异常或等待超时均不算参数绑定目标红。
4. 绑定实现后同一节点已继续完成：另一私聊 8 的独立运行正常结束，正常 close/reopen 后推进受控时钟，真实 scheduler/Attention 发起复盘；三个实际模型请求读取最新 revision 和准确验收单，再核原手机 7 一次 outbox 与 Web 可见。重放真实 notice 及再次正常重开不增加模型轮次或出站。

两次历史红分别证明主协调工具边界缺口和第 3 步的 schema 参数绑定缺口；修正游标观察后，第 4 步已取得上述单节点完整通过。`tests/test_goal_followup_admission.py` 另提供十个有限节点，均已有通过终态：六种显式变更在真实 notice 到达最终接纳锁前发生，同值关闭、改文案、删除、解绑后同任务重绑、done、cancelled 都需拒旧 notice，随后普通提醒仍可入队；另核旧调度快照不能混合新绑定与旧文案/interval，自然周期推进和真实任务 revision 更新不误撤旧 occurrence，准确任务存在且宿主计数不能由调用者填写，以及只损坏真实 notice 的绑定版本不能降级普通提醒。

上述接纳负控用真实创建/编辑 HTTP API、真实 Reminders `_fire`、真实 Attention 和配置锁；任务终态经真实 Commitments 服务保存，不手造任务历史、批准或报告。它们是受控调度/撤销前缀，不冒称每项都重跑真实时钟调度和跨渠道完整旅程。坏 notice 一项明确属于已落盘真实通知的单字段 reader 损坏负控，重新发相同实际坏正文避免只因事件/对象不同而拒绝。固定工具策略只证明宿主证据、读取与路由合同，不代表自然模型的复盘质量；真实模型、平台联调和本片 unknown 发送故障完整旅程尚未实测，仍沿既有不重发合同。

根首收该十项为 9 passed、1 failed：pytest 1.26 秒，守卫 2.119 秒、128.4 MiB、swap 0。唯一失败为缺失任务的 HTTP oracle 原写 400、实际 404；既有 `EvidenceError(evidence_not_found)` 的服务器映射及相邻 inspection/历史读取均为 404。仅此状态预期改为 404，不改“不创建提醒”、宿主字段拒绝或其它 oracle。

根随后单跑该 HTTP selector 实收 1 passed：pytest 0.45 秒，守卫 1.195 秒、86.1 MiB、swap 0，至此新增十项均已有通过终态；先前九项不重复累计。另六个既有相关文件实收 92 passed：pytest 1.19 秒，守卫 1.774 秒、100 MiB、swap 0。

C 非作者独立发现一个源读取边界并由根实收目标红：1 failed，pytest 0.12 秒，守卫 0.643 秒、59.9 MiB。只把真实通知的时间字段改为合法 JSON 整数 `10**400`，实际 `read_followup_notice → _timestamp → math.isfinite` 因整数转浮点溢出抛 `OverflowError`。候选仅在该数值转换处把溢出转换为 False，令可选来源证明受控拒绝；没有硬编码时间上限，也没有捕获整个存储流程。根修后实收 1 passed，pytest 0.11 秒，守卫 0.648 秒、59.5 MiB。该独立负控是源读取损坏边界，不冒称已经运行模型消费或复盘发送。C 完成六个生产文件和此窄修的最终后审，未发现剩余必须修复的 P1/P2。

根随后完成候选离线发布验证：发布/兼容 166 passed，pytest 0.95 秒，守卫 1.463 秒、67.1 MiB；加此前新 12、旧 92 和 server 9，共 279 个不同节点，不重复累计子集。`uv lock --check --offline` 核对 83 个包且无下载；离线 wheel/sdist 构建守卫 1.359 秒、102.5 MiB；`release.py` v2.49 一致性守卫 182 毫秒、19.6 MiB；`check_wheel` 隔离离线安装及 CLI、ASGI、默认桌面关闭与任务控制守卫 1.331 秒、100 MiB，均成功。具体证据与尚未完成的远端步骤见[候选审查](review-v2.49.md#离线发布验证)。

正式旅程保持原 16,000 上下文字符、6,000 输出字符、3 个 pending 槽和原 3/4 秒等待；每次复盘最多 32 条历史、两项同时只读工具和三个主请求。正式旅程不手造提醒 notice、报告、批准或历史；受控损坏负控的边界如上，不扩大普通提醒行为。
