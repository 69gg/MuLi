# 手机调整后，独立完成回报仍回发令私聊

2026-10-24。基线为 `5a5ae91`。2.47候选已完成本地实现、十二参数旅程、21个相关文件411项整合及独立后审，类型、格式和离线构件检查通过，见[本地闭合记录](review-v2.47.md#最终候选准备)；精确远端与发行仍待完成。历史首轮先冻结的 sent、blocked、unknown 三条真实旅程均在最终手机7完成说明 outbox 为0处失败，3 failed in 1.50秒；守卫2.420秒、117.7MiB、swap0、exit1。2.46 已发布当前主运行的重规划说明回手机，其本身的证据不覆盖本片独立完成回报，也不等于真实平台送达。

## 用户行为与已有缺口

主人先在 Web 交代任务，手机7通过真实 `/暂停`、`/调整` 修改要求；主助手真实 `commitment_read` 后，下一轮 `task_approve_plan` 通知原队友。重规划说明结束后，主人在另一已绑定私聊8普通聊天。原队友按新方案真正完成，应用正常关闭重开后，独立主运行的完成说明应回手机7，Web仍可见。普通私聊8应照常回复8，不能变成这个任务的完成订阅。

基线中的 `TaskControl.owner_control` 已在首次控制事务中把可信手机 `ReplyOrigin` 与主 ingress 一起持久化；旧无来源控制回执重投不补写来源。基线 `TaskControl.approve_plan` 的 `task_plan_delivery` 只保存任务、epoch和目标代次/CAS证据，没有完成目的地关联。`Team._run` 生成完成报告时复制队友原始派工 `reply_origin`。同一主运行的续轮保护无法覆盖本片中的独立运行，因此原 Web 派工来源会再次成为完成说明的目的地。

相关入口：[控制事务与计划交付](../src/muli/task_control.py)、[真实代次启动与报告](../src/muli/team.py)、[主运行准备及启动恢复](../src/muli/runtime.py)、[应用默认工厂与关闭生命周期](../src/muli/server.py)、[发送时当前授权和 unknown](../src/muli/channels/manager.py)。

## 最小持久关联候选（本地已验证）

1. `approve_plan` 在原事务内，沿宿主冻结的 `snapshot.replan_request_id → task_control_receipt.ingress_id → 主 ingress` 核对实际控制来源。须匹配当前任务、epoch、owner回执、main会话及受保护 `task_control` 引用，且 ingress 的 `source=task-control`、`principal=owner`、`route=null`。仅从这条实际持久输入的显式 `ReplyOrigin` 冻结原手机来源到既有 `task_plan_delivery`；模型参数、当前 PreparedRequest、最新聊天、摘要均不提供目的地。
2. 复用现有通知代次与批准凭据的核验。archive 路径须匹配真实 `archive_message_id`、`run_id=None` 的受保护通知和实际下一代承接；ingress 路径须匹配本次真实有限 claim 的通知ID、seq、delivered_seq及run_id。`delivery.state=delivered` 在 ingress 路径只表示已入队，pending、未被本次有限 claim 取到或被其它run取到的通知均不得绑定。不能仅凭最新epoch、新run启动或一个 delivery ID 放宽 CAS。
3. 只为主MuLi直接队友的本次准确 target 新代持久绑定实际 `run_id/generation/completion_revision` 和交付证明，完成时与 `team.finished`、报告在原事务内落盘。证明可选且带版本；缺失按旧语义，损坏、多义或不匹配不能绑定，也不能查询latest补救。保持 `agent.reply_origin` 的原始 Web 派工来源、旧暂停报告和旧代报告不变。随后复用真实 `flush_reports → 主 ingress → 独立 Runtime → channel_outbox`，不回写已发布报告，也不从当前模型上下文猜来源。
4. 出站仍由当前绑定、账户与适配器重新授权。撤销手机7后只保留本地完成说明并记 blocked；一次明确 `ReadTimeout` 留 unknown，正常重开或原控制事件重投不得重发、重新批准或再启动队友。沿用既有旧 Web 控制回执兼容，不补来源。

候选只增加既有对象的可选字段。批准事务保存 `delivery.report_source={version:1,owner_receipt_id,reply_origin}`；实际模型请求保留准确通知后，Runtime 保存 `agent.report_binding={version:1,delivery_id,notice_id,run_id,generation,completion_revision,reply_origin}`，其中 completion_revision 是预期的本次完成修订。Team 完成时再次点读原 owner 输入、准确通知和代次证明，才在原完成事务中把相同证明及手机来源写入本次报告。证明损坏、多义或缺失时保留原派工来源；下一代启动清除旧证明。没有新字段的旧对象保持既有行为，重复批准不会补字段。

本片不改 Web/模型公开输入合同，不做永久最近主人偏好、通用订阅、后代/DAG传播、自动接班或未知副作用重试。嵌套任务控制仍保持既有行为；Team只报告直接parent，本片不宣称孙代结果会穿过父链回手机，也不改通用PreparedRequest或子代继承来源。新增字段限于既有 delivery 和准确目标代次，不引入独立通知系统。

旧reader会保留未知delivery字段及已落盘的 `report.reply_origin`，但旧binary不会兑现新绑定。降级只能承诺既有行为，不能保证尚未完成的新代仍回手机；不得补改legacy duplicate来源来伪造兼容。

## 十二参数候选、四个可证伪旅程节点

新增 [test_mobile_task_completion.py](../tests/test_mobile_task_completion.py)，原三条 archive 旅程为 sent、blocked、unknown；另加一条真实 ingress 旅程、六条只损坏一个证明字段的 archive 负控制、一条原私聊8派工、私聊7调整的坏版本负控制，以及准确通知退出请求、普通同正文消息仍保留的身份负控制。沿用真实 ASGI、OneBot适配器、ChannelApplication、Runtime、临时 Store和既有小助手；只有模型工具策略和外部平台使用 FakeClient/MockTransport。历史首检前十一项与持久层实收90 passed、2 failed（pytest6.37秒；守卫7.713秒、192.8MiB、swap0）；修正后十二参数均通过并纳入[最终相关整合](review-v2.47.md#最终候选准备)。

1. Web真实创建任务和原队友；手机真实暂停使旧写工具 `not_executed/task_paused`，保留真实 pending 暂停报告及 Web 来源。手机调整后真实读取、下一轮批准、一次archive计划交付，核对准确archive_message_id、run_id=None、受保护引用和原队友下一代承接；主重规划运行完整结束且手机7确实收到说明。原任务、主会话和队友ID保持。
2. 另一已绑定私聊8提交普通输入，真实独立运行完成并回复8。原队友之后执行真实临时文件写工具、得到真实结果后返回最终结果；验证新代 run completed、generation和completion_revision各前进一次、实际草稿存在、禁止发布的旧路径不存在。
3. 在 `Team._run` 的真实 `_finish` 提交之后、报告尚 pending 的 `await bus.notify()` 停靠。包装只对持有该实际完成报告的 Team finalizer 生效，先调用原 notify，不替换 flush、工厂或 Store。此时核验报告已持久、主 ingress 尚无结果、主运行已空闲，再执行真实 `app.state.close()` 和 lifespan退出。正常关闭会取消这个尚未结束的发布 finalizer，而不会把已完成的队友改为 interrupted；重开不承诺恢复执行中断的模型运行。
4. 按正式默认生命周期重新构造 Runtime和应用，工厂在 create_app 前配置，正常 startup/flush把持久报告交给新的独立主运行。断言 Web可见的实际最终说明只创建一条 automatic outbox，来源完整等于原 `/调整` 的手机7输入ID和路由；私聊8、原 Web派工及旧暂停报告均不抢目的地。sent实际HTTP一次；blocked在原应用关闭后真实撤销owner7、HTTP零次；unknown实际一次ReadTimeout后再次正常重开，原事件重投和真实 `/任务` 回执均不重发完成说明。

停靠点只放大发布前原本存在的 await，不制造完成报告、来源、批准、回执或 SQL历史。已通过的旅程验证“已完成且报告已提交”的正常关闭重开，不覆盖任意崩溃、执行中断自动续跑或真实操作系统服务恢复；没有替换 flush制造成功。

提示词与并发预算沿用旧旅程：context 16000、output 6000、pending 3；主历史不超过32条，单次主运行至多三次固定策略请求。沿用既有4秒事件等待和3秒生命周期/门控等待，不提高运行期限或检查守卫限制。历史HTTP次数由实际 MockTransport recorder精确断言，成功后的 `attempts=0` 是当前重试计数，不能当作未发送或历史次数。

原三项到达最终完成说明 outbox 缺失断言的结果保留为历史功能红。当前十二参数及21个相关文件411项已通过，独立后审无剩余P1/P2，详见[本地闭合记录](review-v2.47.md#最终候选准备)。固定工具策略证明来源、持久化、真实工具与安全合同；自然模型规划质量、真实OneBot账户及远端平台送达仍需后续外部联调。

ingress 候选经真实 Team 消息入口先排一条内部补充，仅在该临时 Store 把队友的实际 claim 批次收窄到1，并委派原 Store 实现；其它会话、生产默认预算和期限不变。这是受控批次规模，不是默认批次实测。通知投递后及队友首轮只取补充时，均要求通知仍 pending、无绑定；首轮有限过渡说明后的真实无工具安全边界准确 claim 通知，第二轮验证已绑定再执行真实写工具，第三轮核工具结果并完成，继续关闭重开与手机回报旅程。历史首检该夹具曾在通知仍 pending 时尝试写工具，正式门禁正确拒绝，后续 Fake 对工具结果的错误假定使完成门控超时；这是夹具失败，不是新的功能红。原渠道来源负控制也曾误按控制事件计算普通用户输入ID，现从实际持久输入和完整 route 读取来源。这两处修正均已通过最终整合。

六条局部负控制各从真实证明复制后，仅改变 binding 的版本、run、generation、completion_revision，或来源版本、archive_message_id；其它事实保持，真实完成后的报告和主说明须精确保留原 Web 派工来源，不产生手机完成出站。正路径另核对实际通知证明；把其执行改为另一真实主 run，或把 parent 改为非 main，均不得接纳。这两项是准确证明的局部核验，不等于实际孙代端到端旅程。旧无来源批准重投回归核对 delivery 原样保留且没有 report_source，不 retrofit。旧代报告全对象保持原样；损坏或多义证明不查询 latest 补救。

额外双私聊负控制经真实私聊8创建任务并派工，私聊7调整后仅损坏绑定版本；报告正常完成和应用重开后，须保留原私聊8来源并实际发送唯一完成说明给8，7没有完成出站。这防止原 Web 夹具把“无效证明被改成 unattributed”误认作兼容行为。

身份负控制在真实 archive 通知提交后、队友启动前，通过 `Conversation.enqueue` 排入一条没有 task_control 引用的普通内部输入，逐字复制该通知正文；由 Runtime 正常 claim 产生另一准确消息ID和seq。上下文 hook 只移除较早的准确通知对应消息，保留这条普通同正文消息；实际 provider 请求只能看到一份正文。原存储通知的来源、代次和批准证明仍有效，但本次请求不得建立绑定。历史身份首测实收93项中92 passed、唯一此反例失败（pytest3.42秒；守卫4.824秒、206.3MiB、swap0）；此前两处夹具修正均通过。这确认了正文集合命中会误代替准确消息身份。

获准修复只为 `Message` 增加宿主可选 `source_message_id`：Runtime 从实际持久消息赋值，正常 pack 的 deepcopy 保留它；绑定核验准确 `(id, 完整正文)`，hook移除或重建而丢失该身份时不补猜。三个 provider codec 仍按原显式字段构造，不发送宿主ID；新增三协议回归比较有/无ID时完整请求相同。ID从 `_text_data` 的本地字符测量中排除，既有预算与期限不变，测试现有测量 helper 同步；已有独立副本回归同时核对 deepcopy 保留ID。身份修复、统一文本计量及三协议回归已通过[最终相关整合与静态检查](review-v2.47.md#最终候选准备)，逐轮失败保留为历史记录。
