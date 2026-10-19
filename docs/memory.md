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

`traverse(entity,hops=...)` 默认扩展两跳，可配置 0–8 跳。查询限定候选数量和总边数，超限返回 `truncated`。社区由真实关系连接形成，保存成员、实体、来源 ID 和带证据编号的确定性摘录；它不是一次额外的 LLM 总结。社区受成员和边数上限约束，截断会明确标记。修订、冲突选择和忘记会使受影响的社区失效并重建。

`claim_key` 用于表达同一个互斥事实槽位。相同分层、相同信任级别、同一槽位出现不同内容时，条目标记 `conflict` 并互指证据；不同信任级别的资料不会把可信用户偏好标成冲突。系统不会仅凭时间或置信度静默覆盖。`resolve_conflict(id,chosen_id)` 记录选择，保留双方来源和修订快照，将另一项标记 `superseded`，使其退出正常召回。`conflicts(include_resolved=False)` 只返回仍有效的未处理冲突，默认可读处理历史。

## 混合检索与提示缓存

1. 全历史倒排索引召回英文词和中文双字词，最多使用 64 个查询词。候选上限限制加载量，不会先按最近记录截断再查找。
2. 明确命中的实体按配置多跳扩展，保留关系出处；基础排名综合词命中、图距离、置信度和时间。
3. 配置 embedding 后，用相同模型标识和维度的向量作余弦检索，与文字/图结果按排名融合。向量逐行读取，默认最多扫描 512 条最近更新的向量，不构造全库矩阵。更大库可能漏召回，结果以 `vector_scan_limited` 公开这一限制。
4. 配置 reranker 后，对有界候选重排。网络或锁等待达到 `query_timeout` 后返回本地结果，`degraded` 说明降级原因；不会将错误响应或密钥拼入模型提示。
5. 返回 `memories/communities/sources/degraded`。条目保留 `matched_by`、`score`、分层、信任、冲突、来源和时间；原文按需通过 `source(id,max_chars=...)` 读取。

`cached_context(query,max_chars=None)` 是纯本地同步接口，不发网络请求、不等待后台提炼。它优先利用同一查询的检索缓存，加上已确认身份/偏好，再按字符预算提供少量有证据编号的资料。每次记忆修改、向量更新、冲突处理、遗忘都会推进 revision，使缓存失效。输出包含“仅作为数据”的提示；较长内容标明节选，最终长度不超过预算。

查询在读取候选前保存 revision，并在媒体锁、embedding 和 reranker 返回后重新核对。等待期间发生遗忘、编辑、提炼合并或其他记忆修改时，丢弃旧候选，按当前数据库重新进行一次本地检索，返回 `degraded:["memory_changed"]`；此前已有降级原因会一并保留。此分支不重试网络，也不把旧证据写入新 revision 的缓存。上游失败或超时后的本地降级同样受此检查约束。

## 遗忘与持久化边界

独立手动记忆按条目删除。带 `source_id` 的派生记忆采用**来源级遗忘**：删除它会同时删除同源原文、所有派生事实、词索引、向量、图边、别名证据、修订、冲突和作业，并清理或重建相关社区。后台在每次网络返回后检查来源是否仍在，不能恢复已删除内容。只保留来源 ID 和遗忘时间的 tombstone，防止相同消息重放或启动恢复重新记住原文。

原始会话消息、导出文件、已有备份是不同记录，记忆遗忘不删除这些文件。SQLite 删除也不代表介质安全擦除；用户需要单独管理已有备份。

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

离线验证：`python -m pytest -q tests/test_memory.py tests/test_memory_pipeline.py tests/test_memory_application.py`，覆盖旧接口、多跳证据、社区、冲突、信任隔离、严格预算、慢提炼、持久恢复、失败重试、混合检索缓存，以及在途遗忘不复活。并发回归分别暂停 embedding / reranker 后执行来源遗忘、编辑或提炼合并，并覆盖上游失败返回，验证即时结果与后续提示均不保留旧证据、没有额外网络重试；测试不访问真实模型。
