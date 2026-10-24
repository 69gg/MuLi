# 队友完成报告接入长期记忆

## 状态与用户旅程

当前为 v2.64 实施候选。正式首节点已在真实完成报告与启动恢复全部前置通过后，准确取得来源缺失的产品红；生产接缝已按以下合同实现，并通过下述本地检查。远端 CI 与本版本发行尚未验收。

主人让队友调查问题，队友从真实资料中找到主人此前没有说过的原因并完成报告。后来会话经过压缩、应用正常关闭重开，主人再问这个决定的原因，主助手或新队友应能检索到原报告的资料与引文，并说明它是队友报告中的判断。完成状态只证明该次运行正常结束，不证明报告内容真实、约束已经满足或任务已经验收。

现有 `MemoryApplication.ingest` 只接纳已验证主人输入，恢复也只补录 owner ingress。队友报告已经持久保存并能通过会话证据入口查阅，但内部报告投递没有进入这条自动归档链。现有显式 `memory_remember` 不等于报告会自动留下；本片补接资料来源，不重做多跳图、向量检索、reranker 或 v2.61 的任务背景补位。

## 最小接缝

目标生产范围为 `src/muli/team.py` 与 `src/muli/memory_application.py`，复用现有 `MemoryPipeline`、来源表、遗忘墓碑、单后台 worker 和维护准入。无新数据库表、格式版本、模型工具、常驻轮询、独立任务队列或网络依赖。

`Team._finish` 原先已通过 `Store.event(objects=...)` 同事务提交完成事件、agent 与报告。只在该事务成功以后调用一个可选的窄完成 observer；observer 不在事务内等待模型或媒体网络。现有 `EventBus` 没有同步完成订阅回调，不为此另建常驻事件消费者。

记忆核验或归档失败不能截断队友原有的 `_active.pop`、bus 通知及报告 flush，也不能把已完成运行改成 failed、取消其 driver 或重做工具。失败仅保留有限错误类别供诊断，已持久完成证据留给正常启动补录。关闭或配置重建期间保持原组件生命周期，不让旧 observer 借旧 pipeline 开启新网络工作。

普通 `MemoryApplication.ingest(message, trusted_owner=False)` 继续拒绝 quarantine 或任意外来资料。只有独立、已核验的队友完成入口可以向原 `pipeline.ingest` 传 `trusted_owner=False`。

## 历史完成证明

`team_report.id` 是该次 `run_id`；报告本体没有 `generation` 或 `completion_revision`。权威证明是 `Team._finish` 同事务提交的历史 `team.finished` 事件，其资料包含：

- `agent_id`、`run_id`、`generation`、`completion_revision`、`status`；
- 事件的 `session_id`、持久事件 ID 与 `created_at`。

在线与补录使用同一个 builder，核完整历史事件、准确 run 和准确报告：

1. 完成事件唯一，`status == completed`，代次与完成修订为严格正整数；
2. 事件 `run_id` 等于 run 与报告主键，事件 `agent_id/session_id`、run 的 `session_id` 与报告 `agent_id` 一致；run 确为 completed，报告 `execution_status` 为 completed；
3. 报告 `parent_id` 等于已有真实 main，会话确为 main；首片只接直接队友；
4. 数据缺失、冲突、多义、损坏或超出当前核查限额时不归档，不能查 latest 猜测补齐。

当前 agent 可以已经进入新代。不得比较其最新 generation、completion_revision、run_id 或 result 来拒绝合法旧报告，也不能借新代给旧报告重新签名。不得整体套用 `TaskDeliveries.delivery_current`，因为它有意要求当前完成代次。

`Store.team_completions(session_id, after_revision, limit)` 已沿 `team_completion_revision` 部分唯一索引取得历史完成前缀；该索引按 `(session_id, completion_revision)` 排列，仅覆盖 `team.finished`。补录从有界 agent 主键目录沿这个完成索引分页，再点读 run/report，避免逐报告按未索引的 run_id 反复搜索事件或遍历完整会话。原 helper 的 `SELECT *` 不直接作为无预算 reader：完整事件解码沿 `TaskDeliveries._completion` 的 SQL 字节门、BLOB 严格 UTF-8 与有限候选模式，对象点读沿 `Store.bounded_object`。

## 同一来源构造与遗忘

来源使用既有 `memory_sources` 字段，在线与补录不得产生两种格式：

| 字段 | 固定来源 |
| --- | --- |
| `id` | `team-report:<run_id>`，不包含当前代次或提炼修订 |
| `content` | 准确持久报告的原 `content`，保留“待核实资料”含义 |
| `source` | `team:<agent_id>` |
| `session_id` | 已核实的准确 run session |
| `role` | `assistant` |
| `principal` | `agent:<agent_id>` |
| `created_at` | 该历史完成事件的持久时间 |
| 归档授权参数 | `trusted_owner=False` |

不把报告可变投递 `status`、后来改动的 `reply_origin`、当前 agent 名称/result/更新时间拼进来源 digest。不读取会话摘要冒充报告正文，也不将报告中不存在的完整运行输出补入原文。

`Memory.register_source` 已对同 ID 的正文与身份计算 digest；不一致应拒绝，不覆盖旧原文。原 episodic `dedup_key`、提炼去重键与作业 `source_id UNIQUE` 负责重复归档，不新建重复 job 或已完成提炼。恢复同时沿用现有 deferred/job/checkpoint 机制，不能仅因来源行已存在就假定后续派生步骤全部完成。

`Memory.delete` 对有来源条目沿 `forget_source` 删除整份来源与派生资料，并留下 `memory_forgotten_sources`。补录先排除同一稳定来源的墓碑，最终仍经过 `register_source` 的墓碑门；不能换 source ID 绕过遗忘。提炼及 embedding await 后继续复用现有来源存在性、条目版本与 revision 复核，旧网络结果不得令已忘资料复活。

报告不是主人输入。identity/preference 与主人别名仍须拒绝；其语义事实、图边及检索结果只表示报告所述资料，不能形成主人授权、执行许可或验收结论。

## 资源、配置与检索

入口检查当前 `memory.enabled` 和当前来源/输出限额；配置应用沿现 `MemoryApplication.reconfigure` 更新 pipeline。报告、事件与 run 均先在 SQL 中检查完整字节大小再解码，正文再受当前 `max_source_chars` 限制。超额不扩大预算，不以新 ID 保存节选来假装完整归档。

复用原 `max_pending_jobs`、每来源事实数量、job timeout/retry、维护准入及单 worker。满队列沿原 deferred 机制，前台与 Team 收口不等待提炼。正常成功路径每份新来源使用一次原 extractor；失败按原重试合同，已有 extraction checkpoint 不重复提炼。配置 embedding 时沿原有限批处理；没有该配置则不调用它。

`auto_extract=False` 只关闭事实提炼 LLM，仍可保留原文与 episodic；现有 pipeline 在 embedding 已配置时仍可能向量化 episodic，不能把该开关写成关闭全部网络。`memory.enabled=False` 不新增本片归档或后台工作。

向量与 reranker 继续使用现 MediaService 协议和当前配置，无新增 SDK 或模型下载。向量写入核原条目版本，查询只使用匹配 model key 与维度的向量，保留原 `max_vector_scan/max_candidates`、图边与查询超时门。显式混合查询沿既有一次 query embedding、可选 rerank 与本地降级；自动提示仍走本地缓存/检索，不借本片增加前台网络等待。新报告资料和当前主人查询共享原提示、profile、候选及整请求预算，不扩大上下文，也不把报告变成系统指令。

## 验收计划

首节点由独立测试作者编写，selector 为 `tests/test_team_report_memory_runtime.py::test_completed_direct_child_report_is_recovered_as_a_stable_memory_source`，先定位归档入口缺口：

1. 真 Runtime 接纳主人输入，主助手通过真实工具启动直接队友；队友实际读取本地 fixture 资料并完成，固定模型策略只负责协议动作；
2. 核 accepted 工具结果、完整持久报告、准确 run、直接 parent 与唯一历史完成事件；
3. 实际启动 MemoryApplication 并等原恢复任务收口，直接断言 `runtime.memory.source("team-report:" + run_id)` 存在。

根 agent 已在第三步实收 `None` 的准确功能红，不是缺导入、新方法或等待超时。首节点使用 `auto_extract=False`，只证明正常启动补录的原文归档合同，不提前证明在线 observer、提炼、向量或压缩重开后的回答。

根 agent 首次实跑为 `1 failed in 0.48s`，守卫 `1.204s / 94.3 MiB`；失败发生在主助手第 3 轮的队友 completed 前置，尚未执行来源存在性断言。此次只记录旅程前置失败，不算产品目标红。测试作者按真实 `fs_read` 的 `{path, content}` 工具结果合同校准夹具并补有限诊断，原证据要求与预算不放宽。随后复验为 `1 failed in 0.41s`，守卫 `1.041s / 84 MiB / swap 0`：真实主/子运行、accepted 工具结果、完整持久报告、唯一历史完成 tuple 与恢复 idle 全部成立，准确在 `source("team-report:" + run_id) is None` 失败。

后续完整正向旅程再验在线完成归档、真实后台提炼及图关系、MockTransport 观测的 embedding/reranker 请求；主/队友真实 extractive compaction、正常 close/reopen 后，核实际 provider 请求或显式 query 结果中的准确 fact/source/quote。报告中的关键理由必须确实来自资料，不存在于初始主人输入；原报告 covered_seq 退出投影后不能借完整历史假绿。新查询不依赖 selected_replan，也不复用预先手填的 semantic cache。

必要负控：

- 同 agent 两代真实完成，旧报告仍按历史证明补录；重复观察、启动补录和新代均不覆盖旧来源、不重复提炼；
- 来源被 forget 后正常重开，报告仍在也不能复活；慢提炼或 embedding 返回后同样不恢复已忘内容；
- 非直接队友、非 completed、损坏/多义完成证明及超预算资料不归档；报告伪造主人身份/偏好/指令不能取得主人信任；
- 当前 disabled、auto_extract 与限额生效，满队列沿原 deferred；提炼持有受控屏障时主会话仍能继续处理真实输入，observer 失败不破坏 Team 收口。

所有本机执行由根 agent 经原守卫串行完成；预算、轮数与原 3/4 秒事件门不提高。固定模型与 MockTransport 验证的是宿主数据流、来源和生命周期合同，不代表自然模型回答质量、真实远端 embedding/reranker 服务或真实账号集成已经实测。正常关闭重开不等于进程强杀恢复测试。

## 本地实施验证

报告来源补接后，首节点修后通过；32 个相关旧记忆、团队与压缩文件共 458 项通过，守卫 14.793 秒、峰值 525.2 MiB、swap 0。在线非阻塞、观察失败、历史代次、遗忘与中断补录的 4 项策略回归，以及来源身份、损坏隔离和旧观察者生命周期的 3 项边界回归均通过。旧观察者在配置重建后的保留引用由实例身份门拒绝；这项加强负控不代表已证明线上存在该调用竞态。

压缩重开旅程曾出现队友召回页未完成的失败，已保留有界阶段与计数诊断。静态查明测试 reranker 替身未按其相关分数排序，真实服务适配会保留服务器返回顺序并截取 top_n；仅修正替身排序和 top_n 合同，未改生产排序、原事实与原文断言或期限。它解释了可能的截断路径，不倒推确认此前每次失败的同一根因。

采用该夹具修正后，9 个完整文件整合共 179 项通过（报告旅程、策略与边界，及本轮模型默认、初始化、CLI、协议和媒体回归）；守卫 2.072 秒、峰值 115.2 MiB、swap 0。与上述 458 项有重叠，不相加为不同节点总数。当前全源码 strict mypy 153 个文件通过，Ruff 及 425 个 Python 文件格式通过。独立非作者审查没有剩余 P1/P2；本节仍不代表远端 CI 或真实外部服务已通过。

扩大检查所有直接涉及协议或模型输出预算的 17 个文件时，首次为 19 失败、377 通过（守卫 6.630 秒、峰值 286.1 MiB）。这些旧 Anthropic 用例没有显式填写原本依赖默认值的 4096 上限，新必填校验在进入原旅程前正确拒绝。仅为 Anthropic 测试配置显式保留原 4096，OpenAI 保持不设上限，未改原断言、期限、上下文预算或生产校验；整组复验 396 项通过（守卫 6.433 秒、峰值 266.7 MiB、swap 0）。该组与上文存在重叠，不叠加为唯一节点数。

本轮中央时间轴与纯前端预览另经全前端 325 项及实际浏览器演示验证，具体范围见[前端验证](frontend.md#验证)。离线锁、sdist/wheel 构建和隔离目录中的离线 wheel 安装烟测通过，不代表远端发行已经完成。
