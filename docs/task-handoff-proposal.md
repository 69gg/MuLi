# 下一片：按最新证据接办

状态：静态设计候选，未实现、运行检查或验证真实服务。

**用户场景。** 主人把“整理并发布”改成“先交草稿，不得发布”，同时更正过期记忆；原队友在操作结果落盘前失败。主助手沿现有暂停→主人请求重规划→读取说明→确认计划，取得接办资料包，显式派新队友核查未知操作、复用资料，完成草稿并沿原任务来源汇报。不会自动重试发布。

**代码依据与缺口。** `src/muli/task_inspection.py::_read` 已有任务、队友、报告、工作流、依据和记忆分段，但目录声明 `consistency=live`，revision主要绑定任务/section/记忆；队友换代、新增工具证据未必改变任务版本，接办者须自行拼接失败执行、最新要求与原始证据位置。`src/muli/local_services.py::read_commitment/approve_plan`、`task_control.py::approve_plan`已有主会话重规划快照/epoch门；`memory_application.py::resolve`已有更正；`runtime.py::_repair_tail`已有中断 unknown入库。这些不作为新功能重造。

**最小生产合同。** 扩展既有 `commitment_inspect(section="handoff", agent_id=...)`，每次只接办一个指定队友，复用原 scope/只读 savepoint，不新增工具、数据库、权限或自动接班器。

- 返回有限元数据索引：任务 revision/epoch/批准状态与完整说明读取位置；队友 generation/completion_revision、run/status、该 run 的报告定位（无则 missing）；原会话 through_seq及有限消息的 seq/evidence_revision、附件引用读取位置。正文沿已有检查、conversation_read、附件/转写工具读取；条数/字节遵循现有限额，分页或明确 output_budget，不扫全团队正文。
- 资料包 revision绑定上述当前事实、所列原消息版本及 memory.revision；续读发现变化返回 evidence_changed。它只是一次观察的版本标识，不是持久历史快照或派工授权。记忆须显式 query，沿用候选检索与 candidate_usage=unknown，不自动混入最近记忆。确认计划会改变版本，正式派工前重取包。
- “工具返回/run完成”不证明外部副作用成功。仅展示已有宿主执行标记及原始定位，缺证默认 unknown；缺记录不能标为未执行，也不借旧成功记录覆盖最新失败。计划确认、spawn/send、commitment_id、显式附件引用和原路汇报全部沿既有入口，不自动重试、更改目的地或唤醒队友。

**三项离线验收。**
1. 真实 Runtime完成“改要求+记忆更正+队友失败→取包→既有重规划→显式派工→按原seq读资料/已存转写→原路交草稿”；不重复 ASR、不重放 unknown写操作。
2. 取包后改变任务epoch、队友代次/报告、所列消息版本或记忆，旧续读拒绝；其它任务不混入；缺报告/截断证据保持unknown。
3. 压缩和重开后定位仍可读；小预算/大量后缀有界分页或明确不足；取包不写库、不选重规划、不通知/启动任务。

**实现与复用。** 主要涉及 `task_inspection.py`、必要 `store.py`有界投影、`tests/test_task_inspection.py`及一条Runtime场景，补任务检查/连续性文档。复用该测试文件已有只读无副作用、任务改版、记忆更正使游标失效用例，以及当前附件片压缩后按seq找回证据。局部参考 `/data1/codex/codex-rs/history/src/compaction_checkpoint.rs`及其测试：最新资料缺生产者/不可用时保留未知，不倒退借旧凭证；不移植其存储。自然模型接办质量、真实渠道送达、外部副作用恢复均未验。

**根初审边界。** 现有 section 的 `consistency=live` 是公开合同，不能追溯改成全记录快照。新段的版本绑定只围绕明确指定队友及有界证据页，不为生成 token 全量读取消息或团队正文；先确定哪些新增事实使旧页失效，再实现游标。记忆查询仍须显式选择，候选不变为确认事实。下一片按下述查询计划实现并验证消息/报告版本变化反例，当前只是候选方案。

**后续静态复核收窄的查询计划。** 新段声明 `recent_window_only/live`，固定一个有限消息窗口；输出分页只在该窗口内续读。任务、指定队友、当前 run/report 沿 `objects(kind,id)` 点查，缺报告不搜索旧成功记录。需要完整摘要的对象先检查 SQL 字节长度，超过既有限额明确失败，不以时间戳、长度或截断前缀充当版本。

- 消息先沿 `message_order_session(session_id,seq)` 取有上限的原始映射及额外一条分页指示，再按 ID 做现有有界投影；损坏或缺失映射也消耗额度，不持续回溯补齐有效条数。现有 `message_evidence_max_seq()` 会过滤无效映射，不直接作为此段的有限窗口选择器。
- 归属只沿指定队友的真实父链点查；现有 `resolve_scope()` 仅防环，新段还需给复用的解析逻辑传入有限跳数预算，超限不可用，不调用全局团队枚举。记忆只点查 revision；候选检索继续显式查询。
- revision 绑定实例、窗口参数与边界、任务 epoch/revision、归属链、指定队友代次/run/完成版本、当前报告存在性与完整有界摘要、窗口内消息版本及记忆 revision。新增尾部消息、当前报告生成/变化/删除或窗口内修改应拒绝旧页；窗口外旧消息、待处理入站及其它队友不在此版本保证内。
- 不为寻找某条工具完成事件持续扫描历史。若确需事件证据，先用既有 `events_session(session_id,id)` 取有限原始窗口再分类，明确缺口；缺证仍为 unknown，不增加自动派工或重试。

以上仅为代码静态复核后的设计约束，查询成本、游标反例和完整 Runtime 场景仍待实现与受限验证。
