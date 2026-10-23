# 分层记忆、GraphRAG 与后台提炼

MuLi 使用现有 SQLite 保存原文、分层事实、关系证据、修订和后台作业，不下载本地 embedding 模型，不依赖向量数据库。配置的提炼、embedding、reranker 都通过 HTTP 模型服务运行。没有配置这些服务时，原文归档、事件记忆、关键词和图查询仍可用。

## 来源与分层

| 层 | 用途 | 自动提炼的信任条件 |
| --- | --- | --- |
| `identity` | 用户身份、长期背景 | 必须来自运行时认证的 owner 用户输入 |
| `preference` | 用户明确表达的偏好 | 同上，不从助手猜测或陌生人自报身份建立 |
| `episodic` | 一次输入及其发生背景 | 原文登记后立即保存，先于任何网络调用 |
| `semantic` | 有出处的事实、实体知识 | 保留来源和可信标志，不自动视为用户身份 |
| `procedural` | 可复用步骤、操作方法 | 同上，内容作为资料，不作为自动执行指令 |
| 原始来源 | 完整消息、渠道、会话、实际 principal 和时间 | 与提炼条目分表保存；每项提炼必须引用逐字证据 |

`trusted_owner` 由运行时的真实 receipt/渠道映射传入，模型输出中的身份文本不会改变它。`MemoryApplication` 只接纳已验证 owner 的输入；独立 `MemoryPipeline` 也支持把其他来源作为未验证资料归档，但会拒绝其身份、偏好提炼和别名注册。助手/工具来源即使传入 `trusted_owner=True`，也不能成为可信 owner 来源。

`source.id` 是去重键，同 ID 的内容、来源或授权不一致会报错。原文默认最多 64000 字符，事件记忆最多 12000 字符，较长内容明确标记节选，完整原文仍可按 source ID 读取。自动归档保存输入原文，因此原始来源可能包含输入中已有的敏感信息；提炼提示要求模型不再把密码、令牌等保存成长期事实。

既有 `observation/fact/preference/procedure/summary` 类型和 `save/get/search/graph/delete/revisions` 接口继续可用，分别映射到事件、语义、偏好、步骤、语义层。旧记录的倒排索引按批次建立，既有关系迁移为有 `memory_id` 证据的图边。

## 提炼队列与成本

`ingest()` 同步提交原始来源和事件记忆，随后登记持久作业；这里不调用 LLM 或 embedding。后台只有一个 worker，使用 `asyncio.Event` 等待新消息或重试期限，空闲时不轮询模型。

作业经过 `queued → processing → done`，错误转为 `retry`，达到次数上限后变为 `failed`。达到 `max_pending_jobs` 后，新原文和事件记忆仍会保存，作业显示 `deferred`；释放槽位后按创建顺序补入，避免一次加载无限队列。状态接口公开各状态数量、延后数量、服务配置和安全错误类型。

提炼回调只接收来源快照，不获得工具。返回格式为：

```json
{
  "memories": [{
    "layer": "semantic",
    "content": "项目甲依赖工具乙",
    "quote": "项目甲依赖工具乙",
    "entities": ["项目甲", "工具乙"],
    "relations": [{"subject": "项目甲", "predicate": "依赖", "object": "工具乙"}],
    "confidence": 0.9,
    "claim_key": "项目甲.依赖工具"
  }],
  "aliases": {"项目甲的原文别名": "项目甲"}
}
```

整批检查通过才写入事实和图，`quote` 必须逐字存在于原文。别名还要求真实 owner 来源，并且别名和规范名称都出现在来源中。提炼结果先保存 checkpoint，再写入确定性去重的事实；embedding 失败重试会复用 checkpoint，不重新调用已成功的 LLM。向量写回前检查条目的版本，编辑或删除期间返回的旧向量不能覆盖新记录。

正常停机会取消网络工作并将作业恢复为 `queued`；重启会恢复未完成的 `processing` 作业。重试等待按 `retry_backoff_seconds × attempts` 计算；提炼加 embedding 共用 `job_timeout`，错误只保存异常类别，避免记录上游回显的令牌。作业失败不删除原文和事件记忆，可调用 `retry(source_id)` 明确重试。

默认 `auto_extract=true`。运行时优先使用 `models.memory_extractor`，未配置独立提炼模型时复用已配置的主模型。没有模型时也能保存原文，但不会伪造提炼结果。新来源触发后台工作，不为旧会话反复调用模型；启动恢复只补登记尚未入库且未忘记的已授权输入。

## 图谱、社区与冲突

实体使用 Unicode NFKC、大小写和空白规范化，避免 `ＭｕＬｉ` 与 `muli` 分裂。明确别名指向规范实体，已有别名不能静默改绑到另一个实体。每条关系保留 `subject/predicate/object`、原始 `memory_id`、`source_id` 和引文。

别名保留唯一规范映射，并以 `(alias,memory_id)` 记录各项真实贡献。两条来源都明确登记 `Lumi → MuLi` 后，遗忘其中一条只撤销该来源的贡献；剩余依据继续支持查询，最后一项消失才移除别名。贡献中的 `source_id` 必须与事实的实际来源一致，事实/原文任一删除都会撤销这一项；同一来源的多个事实不算多个独立来源。可见字段更正保留原登记，底层整份 `save` 替换才撤销该事实的旧别名贡献。重试幂等，事实、贡献、路由同事务更新，别名不能静默改绑。

迁移只登记旧索引已明确保存且关联一致的那一项依据，不从全文共现猜测被旧版忽略的其他来源，不自动重放提炼。旧 source-only 索引继续保留原来源生命周期，并标为 `legacy_unknown`；规范自名是结构入口，标为 `direct`，不伪造来源。每个实际入口最多返回一个稳定贡献及 `has_more`，后者仅表示还有登记未展示，不代表独立来源数或已核验的共同结论。

`traverse(entity,hops=...)` 默认扩展两跳，可配置 0–8 跳。查询限定候选数量和总边数，超限返回 `truncated`。社区由真实关系连接形成，保存成员、实体、来源 ID 和带证据编号的确定性摘录；它不是一次额外的 LLM 总结。社区受成员和边数上限约束，截断会明确标记。修订、冲突选择和忘记会使受影响的社区失效并重建。

遍历还返回同一 `revision` 下的 `alias_evidence` 和与边对应的 `paths`。每条路径从实际 BFS 父边重建，保留入口、完整事实/来源/实体 ID、显示名、关系方向和有序步骤；不是从平铺边猜一条链，也不承诺全部可行路径。实际由图补入的搜索结果附 `graph_via`，包含入口登记及该链；纯文字或独立向量命中不补造路径。融合/重排可保留已有链，但不会把后来生成的图接到旧候选上。

`claim_key` 用于表达同一个互斥事实槽位。相同分层、相同信任级别、同一槽位出现不同内容时，条目标记 `conflict` 并互指证据；不同信任级别的资料不会把可信用户偏好标成冲突。系统不会仅凭时间或置信度静默覆盖。`resolve_conflict(id,chosen_id)` 记录选择，保留双方来源和修订快照，将另一项标记 `superseded`，使其退出正常召回。`conflicts(include_resolved=False)` 只返回仍有效的未处理冲突，默认可读处理历史。

## 混合检索与提示缓存

1. 全历史倒排索引召回英文词和中文双字词，最多使用 64 个查询词。候选上限限制加载量，不会先按最近记录截断再查找。
2. 明确命中的实体按配置多跳扩展，保留关系出处；基础排名综合词命中、图距离、置信度和时间。
3. 配置 embedding 后，用相同模型标识和维度的向量作余弦检索，与文字/图结果按排名融合。向量逐行读取，默认最多扫描 512 条最近更新的向量，不构造全库矩阵。更大库可能漏召回，结果以 `vector_scan_limited` 公开这一限制。
4. 配置 reranker 后，对有界候选重排。网络或锁等待达到 `query_timeout` 后返回本地结果，`degraded` 说明降级原因；不会将错误响应或密钥拼入模型提示。
5. 返回 `memories/communities/sources/degraded/revision`。条目保留 `matched_by`、`score`、分层、信任、冲突、来源和时间；原文按需通过 `source(id,max_chars=...)` 读取。指定 `max_chars` 的来源预览使用有界 SQL BLOB 窗口，在进入 Python 前限制读取量；按字符节选且保留 NUL/中文/emoji，身份元数据不截短，`max_chars=None` 保留完整读取语义。

工具 `memory_query` 与 `GET /api/memory/query` 共用最终投影，完整 JSON 不超过当前 `limits.max_output_chars`。优先按排名保留整条事实及其完整路径，再放来源预览和社区附录；放不下的条目整体省略，可以继续保留后面的较小候选。`truncated` 和 `omitted` 各分区计数说明当前候选中没有返回多少项，不是全库总数，也不是分页游标；极小预算可能没有事实，不能据此断言没有相关记忆。固定定位元数据也放不下时返回 `output_budget`。投影不修改检索缓存，调大预算可以重新获取完整候选，不截短身份/ID，不交付半条关系链。当前工具批次保留完整 JSON；处理结束前预算再次变小时，由运行器明确返回结构化预算错误。

管理列表 `GET /api/memories?query=...` 继续返回至多 100 条现有完整记录。新增的 `graph_via` 附加区共用 `max_output_chars` 预算，放不下时整条路径省略并在该记录上标 `path_omitted=true`，信封提供 `revision/truncated`。此限额只约束路径，固定省略标志和原有记录不计入，不能把它理解为整个管理列表的大小上限。网页按需展开一条实际路径，使用原查询重新核查，并复用已有详情读取登记和关系原文；图变化后要求重新检索，不用旧路径证明当前结果。

`cached_context(query,max_chars=None,*,latest_user_id=None)` 是纯本地同步接口，不发网络请求、不等待后台提炼。它利用同一查询的检索缓存或当前本地检索，与已确认身份/偏好共同分配 `context_limit` 和字符预算。有查询时优先相关命中，空查询优先侧写；名额至少为二、预算能放下完整元数据时，两类各保留一项，再交替补充。共享 ID 只输出一次，仍可同时属于两类；只有一个名额时优先可容纳的相关命中，没有命中才用侧写。

先预留所选条目的完整来源、状态、冲突和分隔符，再给正文分摊余量，避免一条长偏好吃掉所有查询证据。无法完整表示元数据的候选整条跳过，不缩短身份或冲突 ID；极小预算可能只能返回较少条目或空内容。每条正文按最终 JSON 转义后的字符数测量，节选有明确标记，前缀和换行一起计入总预算。两流各取至多 `context_limit` 项，未引入模型、网络或全库拼装。每次记忆修改、向量更新、冲突处理、遗忘都会推进 revision，使缓存失效。输出仍是“仅作为数据”的提示，不授予新权限。

Runtime 在召回前分配本轮预算：沿用 `min(12000, limits.max_context_chars // 4)` 的记忆块上限，扣除外层资料标题，再与当前 `memory.context_max_chars` 取较小值。MemoryApplication 显式注册 `MemoryContextProvider`，把预算和最新完整 user 消息的真实 ID 传给流水线；没有 MemoryApplication 时也使用相同完整 JSON 两流渲染器，最多沿用五个名额，并遵守更小的 `context_limit`。运行时每次构建请求重新读取开关和预算；应用适配器还读取当前名额，通过流水线的 `context_for_prompt` 接缝复用选材，不改变后台 worker 配置。关闭记忆、调大或缩小预算在下一次请求立即生效；已经发送的请求不撤回。最终 `pack_context` 仍核对整个模型请求的 JSON 总预算，不能据此保证任意系统提示都放得下。

自动提示仅排除本次最新 user 来源的初始自动 episodic 副本：小主键查询同时核对 `episodic:<source_id>` 去重键、来源 ID 与标签、observation/episodic、初始版本、active 且无未解决冲突，不读取来源全文或按正文猜测身份。同源提炼、更正后的 episode、冲突资料和其他来源的相同文本继续可选；没有真实 ID 不推断排除。过滤发生在名额截断前，缓存保留已有顺序；有效召回不足当前 `min(context_limit, max_candidates)` 时（包括配置名额增大而命中旧小缓存），至多一次本地检索补位，合并后的召回仍受该上限约束，主人侧写继续按独立双流规则参与总名额分配，图限制不变；只改变当次提示投影，不修改显式查询、缓存或持久正文。工具续轮沿用同一真实 user ID，不同输入和队友会话按各自投影处理。

普通旧 `memory_context_provider(query) -> str` 同步回调仍只调用一次。返回结果或自定义具名适配器输出超过实际预算时整体省略，不裁剪字符串、不捕获 `TypeError` 重试。需要宿主预算的受信扩展应显式使用 `muli.memory_context.MemoryContextProvider(renderer)`；renderer 接收 `(query, max_chars=None, *, latest_user_id=None)`，适配器本身仍支持旧的一参数调用。这里不新增网络调用或持久格式。

### 自动提示中的冲突组合（开发中）

普通记忆保持原 JSON 行形状。尚未解决的冲突使用 `type="conflict"` 资料行：能完整放下双方定位元数据及可读正文片段时，`availability="paired"`，`memories` 中的双方各占一个既有名额；否则整组变为 `availability="pending"`，占一个名额，`memories=[]`，仅在 `seed` 中保留当前仍有效的记忆定位元数据及外层 `revision`，不交付单侧正文。提示本身也放不下时整体省略，不裁剪 ID 或 JSON。

组合始终标 `status="unresolved"`、`partial=true`：只核验本次呈现的一组，不证明已经列全冲突，即使实际只有两项也不声称完整。seed 保留当前候选的完整冲突 ID；增量读取的对方只携已核对的 seed ID，并标 `conflicts_partial=true`。这避免为凑齐对方全部冲突而调用旧无界展开。三方冲突只解决其中一组后仍可能有未解决关联，不保证只余一条事实。

召回与主人侧写两流各取至多 `L=min(context_limit,max_candidates)` 个 seed，跨流同 ID 只复核一次，新增组合点查总数不超过 `2L`。失败占额度，不因组合缺失补扫更旧资料；每项最多核验第一个登记对方，关系走排序后的 `(left_id,right_id)` 唯一键点查。SQL 先限制元数据及正文 BLOB 窗口，再在 Python 严格解码；不读取 source 全文，不展开对方的全部关系，不调用额外模型。来源关联、分层、信任和版本按当前记录复核；旧缓存中的 seed 已遗忘时不能留下含旧来源的 pending 提示。无来源的旧手工记忆仍按原信任可读，不由组合升级成主人偏好。

完整 pair 是不可拆的选材单元；两流或多组共享成员时，不裁掉其中一方来勉强凑名额。与已选成员相交的后续组整体降为 pending 或省略，同一个 seed 不重复提示。两名额且可行的独立侧写需要留位时，可使用 pending 加侧写；原同 ID 覆盖两流的规则继续保留，不保证再额外留一个无关侧写。所有元数据、通知和双方正文共用现有当轮字符预算；选材不会自动解决冲突、修改事实或增加权限。流水线和没有 MemoryApplication 的 Runtime fallback 共用该路径，显式 `memory_query`、冲突管理及来源读取合同不变。

这是 v2.44 开发行为，尚未完成整合或发行验收。新增点查与最终提示受上述边界约束，既有 `Memory.get` 的完整关系/冲突列表读取和旧检索链的总成本并未因此取得相同证明；模型能看到双方也不代表自然模型一定会正确判断。方案与离线验证状态见[候选及实施记录](memory-quality-next-proposal.md)。

查询在读取候选前保存 revision，并在媒体锁、embedding 和 reranker 返回后重新核对。等待期间发生遗忘、编辑、提炼合并或其他记忆修改时，丢弃旧候选，按当前数据库重新进行一次本地检索，返回 `degraded:["memory_changed"]`；此前已有降级原因会一并保留。此分支不重试网络，也不把旧证据写入新 revision 的缓存。上游失败或超时后的本地降级同样受此检查约束。

## 核查与更正

设置中的每条记忆均可进入核查，不要求存在关系或已配置模型。详情重新读取当前记录，分别显示当前内容、原始引文、来源标签和真实来源快照。`source` 是可编辑标签；`origin.source/role/principal/trusted_owner` 才来自已登记的原始消息。没有快照时明确显示无原文，不从标签猜测身份。原始引文只说明曾经引用了哪段资料，不证明后来更正的正文。

`GET /api/memories/{id}` 返回现有条目及 `origin` 元数据摘要；不在摘要中附带整篇原文。`GET /api/memories/{id}/source` 按需返回一页原文，参数为 `offset_bytes`、可选 `length_bytes` 和 `revision`。首次展开带上详情中的 `origin.digest`，后续使用上一页的 `revision/next_offset_bytes`。响应含 `memory_id/source_id`、真实身份、时间、正文、字节范围、总字节数和 `has_more`，网页还须核对来源 ID；相同内容的不同来源可能有相同摘要。详情和分页均沿用登录、同源防护与 `Cache-Control: no-store`。

模型工具 `memory_source(id, offset_bytes=0, length_bytes=..., revision=...)` 按原始来源 ID 读取同一类页面。保留原 `id/digest` 字段，新增 `revision` 和续读信息；`truncated` 在起点非零或尚有后文时为真，末页不冒充整篇原文。返回整页 JSON（含身份）不超过 `limits.max_output_chars`。此页享有与 `conversation_read` 相同的当前工具批次保留规则，后续轮次恢复普通压缩；来源、引文和旧授权均是资料。

两入口复用 UTF-8 字节窗口与完整 JSON 预算逻辑。SQL 只向 Python 返回所请求的 BLOB 窗口，保留中文、emoji、NUL 和转义字符；分页会退让不完整的末尾码点，起点在字符内部时明确拒绝。身份元数据放不下时返回 `output_budget`，不会截短 principal。来源消失返回 `evidence_not_found`，版本变化为 `evidence_changed`；API 分别使用 404/409，其他分页错误为 400（查询参数验证错误为 422）。即使会话里还有原消息，也不回退恢复已忘记的来源。

`PATCH /api/memories/{id}` 通过 `Memory.edit` 更正可见字段，未提供字段保持原值；宿主的来源关联、引文、信任、事实槽位、提炼去重键和别名证据不能由表单伪造。相同 kind 保留原 layer，实际改变 kind 才按已有映射更换层。无变化不产生修订；实际更正保留旧快照、失效向量和提示缓存并重建相关图。被取代的事实不会因编辑而自动重新生效；内容变化重算未解决冲突，已处理的选择仍保留。显式更改提及实体不会删除仍有来源证据的别名，来源遗忘后才清理无其他证据的实体。提炼重试沿用去重键，不重新插入更正前的旧正文。

公开 HTTP 和模型工具更改分别广播 `memory.updated` / `memory.forgotten`，包含 `memory_ids` 与可空的 `source_id`；来源级遗忘使所有同源核查失效。网页收到匹配事件、关闭弹窗或退出登录时丢弃在途资料，不自动覆盖正在更正的草稿。底层 `Memory` 是同步库，受信插件直接调用时应由插件广播变更；没有事件不能撤回已经发出的响应，下一次读取仍会复核当前关联和版本。此处不承诺对并行编辑进行事务外的自动合并。

## 遗忘与持久化边界

独立手动记忆按条目删除。带 `source_id` 的派生记忆采用**来源级遗忘**：删除它会同时删除同源原文、所有派生事实、词索引、向量、图边、别名证据、修订、冲突和作业，并清理或重建相关社区。后台在每次网络返回后检查来源是否仍在，不能恢复已删除内容。只保留来源 ID 和遗忘时间的 tombstone，防止相同消息重放或启动恢复重新记住原文。

原始会话消息、导出文件、已有备份是不同记录，记忆遗忘不删除这些文件。SQLite 删除也不代表介质安全擦除；用户需要单独管理已有备份。

## 主动通知的显式偏好

`channels.proactive.memory_preference` 可引用一条确切的 `memory_id/version` 并明确配置 `destination`，不从正文、相似检索结果或后台摘要推断地址。主人的来源与逐字引用供人工核查，结构化配置承担选路含义。事实需为 active preference，版本匹配，事实及来源均有主人信任，原始来源为 user/owner；无来源的手工记忆、agent/system 来源或冲突记录不能启用该绑定。

宿主选路及发送复核只按主键连接 `memories/memory_meta/memory_sources`，返回有限版本、状态、信任、来源 ID 与摘要，不读取正文、关系或冲突清单，不调用检索、embedding/reranker 或模型，也不写资料库。原始来源摘要沿用现有登记的不可变合同；不承诺抵御同权限插件直接修改 SQLite。更正与冲突解决会递增版本，来源级遗忘删除关联事实，旧通知依据因此失效；更正后重新绑定只影响后续事项，旧依赖项不会换地址或自动重发。关闭记忆或取消绑定无需先修复旧依据，普通默认仍可用于新独立事项。设置与投递边界见[渠道文档](channels.md#无发令会话时的默认通知)。

## 配置与接口

`settings.memory` 的默认值：

```json
{
  "enabled": true, "auto_extract": true,
  "max_pending_jobs": 128, "max_attempts": 3,
  "retry_backoff_seconds": 5, "job_timeout": 60,
  "max_source_chars": 64000, "max_facts_per_source": 16,
  "graph_hops": 2, "max_graph_edges": 256, "max_candidates": 256,
  "max_vector_scan": 512, "max_community_members": 32,
  "context_max_chars": 6000, "context_limit": 6,
  "query_timeout": 5, "cache_entries": 64
}
```

embedding 和 reranker 复用 `settings.media.embedding/reranker` 的配置，详见 [模型服务](providers.md)。向量模型标识由 endpoint、model、维度、指令和附加参数散列形成，不保存密钥。

```python
pipeline = MemoryPipeline(memory, bus, extractor=extractor, media=media, config=config)
pipeline.ingest(message, trusted_owner=receipt.trusted_owner)  # 同步、无网络
await pipeline.start()
result = await pipeline.query("项目甲的依赖", hops=2)
context = pipeline.cached_context("项目甲的依赖")
await pipeline.close()
```

`MemoryPipeline.status()` 用于可观察性；`wait(source_id)` 等待作业终态，不是对模型轮询。所有数据库操作在事件循环线程内同步提交，事务中不等待网络，不在线程间共享事务。

离线验证：`python scripts/check_local.py run -- .venv/bin/python -m pytest -q tests/test_memory*.py`，覆盖旧接口、多跳证据、社区、冲突、信任隔离、严格预算、慢提炼、持久恢复、失败重试、混合检索缓存、两类注入公平性、来源分页、真实 HTTP 更正及模型工具请求，以及在途遗忘不复活。并发回归分别暂停 embedding / reranker 后执行来源遗忘、编辑或提炼合并，并覆盖上游失败返回，验证即时结果与后续提示均不保留旧证据、没有额外网络重试；测试不访问真实模型。
