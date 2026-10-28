# 显式阅读书签

状态：持久服务、真实页凭据与 Runtime 工具已实现并完成独立后审。十五文件 242 项关联回归、181 份源码非增量严格类型检查已通过；另补的真实队友交接回归通过。构件验证及新提交的跨平台 CI 仍待收取，不代表发行或外部渠道验收完成。

## 用户目标与首版范围

主人可以在电脑读一份资料后明确说“记住这里，下次从这里继续”，重开或换到已绑定的主人私聊后找回同一位置。位置属于本地同一个主人身份，不按消息渠道另存一份，也不随最近联系人改变身份。

首版提供目录、读取、设置、清除四个工具，支持普通 UTF-8 附件、PDF 文字层和 DOCX 主正文。“继续上次那份”先查最近明确设置的书签，不依赖压缩后仍记得附件 ID；同一 root main 下的队友也能查书签协作。位置可以来自主助手或真实队友已完整归档的阅读页，由当前主人请求通过主助手显式设置，始终保留页面的真实阅读者身份。普通阅读、队友阅读、查书签和按书签续读都不自动推进位置。设置不宣称真人已经看完，没有明确书签时也不猜最后阅读位置。

不新增 worker、模型调用、OCR、正文缓存、记忆收藏、渠道发送或配置项；不新增书签命名、管理 API 或前端面板。已有 `conversation_read` 和证据正文入口负责定位历史页，已有阅读工具负责实际续读。第一版不接受 ASR、视觉解释、任意文件路径、URL 或记忆来源正文作为书签对象。

## 已由当前代码核对的事实

| 入口 | 当前事实与复用点 |
| --- | --- |
| `attachment_text.read_attachment_text` | 每页核验普通附件、完整文件摘要及 UTF-8，非零偏移必须带版本；等待后重验附件元数据和当前额度。函数不持久化位置。 |
| `document_reading.read_document/document_view` | 原附件摘要和完整提取正文共同决定文档版本；每次核验完整有界原文件及提取结果，等待后核对附件、限制和文档设置。函数不持久化位置。 |
| `evidence.content_page/decode_utf8/validate_integer` | 返回 `offset_bytes/length_bytes/next_offset_bytes/total_bytes/has_more`，拒绝半个 UTF-8 字符和非法整数，完整定位元数据与正文共用输出预算。 |
| `LocalServices.tools` | 已注册 session-scoped 的 `attachment_read/document_read`，均为 `preserve_output=True`；不传分页参数的文本预览、ASR 与视觉解释有不同分支。不能仅凭工具名称或 `truncated` 字段识别普通分页。 |
| `Runtime._tool/_execute/_tool_completed` | 宿主绑定真实会话、运行及请求，沿原模式检查工具权限。`tool.completed` 发生在完整工具消息归档前；后续还可能因预算得到 `runtime_excerpt/result_omitted`。该事件不证明完整页已归档。 |
| `HistoryEvidence.read/read_content` | 已提供有界消息目录和按 `message_id + revision` 读取正文的入口；消息证据版本、正文完整性及 `content_provenance` 来自宿主。分页游标不授予身份或写权限。 |
| `AttachmentRecords.read/_cursor`、`Store.attachment_record_rows` | 已有先取有限原始候选、再筛选/核查的目录模式，带额外一条 lookahead、`scanned/has_more/next_cursor` 及整页预算；附件名称查询不会扫描全库寻找足够匹配。 |
| `store_schema.CORE_SCHEMA`、`Store._event` | `objects` 已有 `(kind,id)` 主键，支持指定 kind 和 ID 范围内的有界 seek；`events.id` 为 `INTEGER PRIMARY KEY AUTOINCREMENT`，`_event` 在调用方事务内返回实际插入 ID，可表示成功设置的持久顺序。`objects.updated` 是墙钟时间，不能表示最后一次设置。`message_order.seq` 是全 store 的自增序号，可比较主/队友页与主助手当前工具批的先后。 |
| `Store._admit_format/_initialize/open_existing`、`validate_existing_schema` | 已标记当前格式库进入 `migration_allowed=False`，启动不自动建表或补索引；结构核验要求当前声明中的每个必需对象。直接把新目录索引加入核心声明会使缺索引的已有库不能打开。书签目录改用既有主键，不需要这类 DDL 或启动迁移。 |
| `TaskControl.scope_lineage/_resolve_scope`、`TaskInspection._handoff/handoff_limits` | 前者接受跳数/字节预算，只沿指定 session 的 agent/workflow 父链点查，拒绝环、缺失和不一致绑定，返回有序 frames；后者已有 agent/session 一致性核查及有限父链调用。返回的 binding 可以为 None，不能把“无任务绑定”误当成无 root main。 |
| `attachment_io.read_verified_window` | 核验完整有界原文件的真实 SHA-256、inode/size/时间变化，`window=0` 可只做完整流式校验而不读取正文窗口；可复用 `_owned(file_io(...))`，保存队友页时不必由主助手再次提取整份文档。 |
| `Store._put/bounded_object/message_evidence_slice/event` | 消息每次保存都会取得新的 `evidence_revision`；已有主键点查、SQL 字节门和事务事件写入。`task_file_publication.completed_publication` 已有真实工具结果与宿主证据一起归档的参考模式。 |
| `MemoryApplication._attachment_guard` | 可参考真实主会话、当前 driver/run/request、主人触发输入、维护准入、新输入及关闭检查；不能直接套用其记忆开关、pipeline 代次或收藏工具名称条件。 |

这里有四种独立身份，接口不得混用：原文件 `source_revision`、阅读正文 `revision`、归档页消息的 `evidence_revision`、书签的 `generation`。普通文本前两者相同；文档提取版本不是原文件摘要，消息证据版本也不是附件版本。

## 最小工具接口

四个工具沿现有 LocalServices session-scoped provider 注册。目录、读取为 `read`，真实 main 及同 root main 的真实队友均可使用；设置、清除为 `write`，仅真实主助手当前主人请求可用，并在 handler 内复验。参数不能选择主人、root main、调用会话、运行或渠道；来源页的真实 session 从持久消息及宿主凭据取得。全部 `preserve_output=True`，沿现有 ask/auto/yolo 和 disabled-tools 合同，不另加确认状态机或权限框架。

| 工具 | 参数 | 返回与语义 |
| --- | --- | --- |
| `reading_bookmark_list` | `name_query="", cursor?, limit=20` | 只列当前 root main 的活动明确书签，按最近显式写入倒序；返回 `items/scanned/omitted/has_more/next_cursor/consistency="live"`。每项含完整附件 ID、可核对的名称或元数据不可用标记、书签代次/位置/版本、更新时间及真实来源页身份，不读正文或运行转换器。`limit=1` 是最近明确书签入口，仍须遵守续页合同。 |
| `reading_bookmark_read` | `id`：准确附件 ID | 返回 `state/generation`。活动书签另含 reader、原文件版本、正文版本、偏移、EOF 标记和原阅读工具参数；不读取正文或运行转换器。返回 `verification="not_checked"`，不以目录元数据声称文件当前可读。 |
| `reading_bookmark_set` | `id, page_message_id, page_message_revision, position, expected_generation` | `position` 仅为 `start` 或 `next`，分别取真实页的起点或 `next_offset_bytes`；没有可直接填写的偏移。核验来源页及当前文件，再做代次 CAS，成功返回完整新书签。 |
| `reading_bookmark_delete` | `id, expected_generation` | 以 CAS 写入清除状态并增加代次。书签存在但文件丢失时仍可清除；不删除附件、聊天或已收藏的记忆。 |

未登记时读取返回 `state="empty", generation=0`；清除后返回 `state="deleted"` 及真实代次，不能把清除当成从未登记。清除不存在的书签只有在附件元数据确实存在时才可建立 tombstone，避免任意 ID 制造记录。读取无书签不需要创建记录。

`page_message_revision` 是现有证据入口返回的 opaque 消息版本，不是文件摘要。所有 ID、版本、枚举和整数均做真实类型及长度校验；不接受 bool 冒充整数。错误返回固定码和有限说明，不回传原文、异常正文或文件系统路径。

## 发现最近明确书签的有界目录

“继续上次那份”调用 `reading_bookmark_list(limit=1)`，取得最近的活动明确书签，再按返回的完整附件 ID 读取位置。最近指成功设置事务的持久先后，取其真实 set 事件 ID，既不比较墙钟时间，也不比较书签最初插入的 rowid。普通读、队友读、书签读取和续读不更新顺序；删除不把被清除的位置留作“最近”。不得回退到最近附件或最近工具结果。如果候选损坏或筛选不符，本页可以没有 items；`has_more=true` 时继续同一游标，不能把这一页空结果称为没有书签。

复用 `AttachmentRecords` 的候选窗口模式：`limit` 为 1–100 并受当前 `limits.max_search_results` 收紧，单次至多检查 200 条原始排序定位对象及一条 lookahead。先按下面的主键范围取有限窗口，再按主键点查对应书签，核对 root、schema、活动状态、代次、真实 set 事件 ID 和附件名称。损坏/不支持、不匹配或悬空的定位与名称不符均占 scanned；无法输出的条目计 omitted，不扫描全库凑满。清除正常情况下同事务移除定位，tombstone 不进入目录。当前 root 的直接按 ID 读取仍对损坏记录明确失败，不把省略当空记录。

每个活动书签增加一个最多 1 KiB 的 `kind='reading_bookmark_order'` 对象，只作目录定位，不复制位置或来源页。其 ID 为 `root_key + ':' + event_id 的 19 位补零十进制`；`root_key` 是 store instance 与真实 root main 的固定无歧义编码的 SHA-256，不从模型参数取得。事件 ID 必须是本次 `_event` 实际返回的正 SQLite 整数，最大为 `2^63-1`；不能用 `event_position()+1`、内存计数、墙钟或伪造 `objects.updated`。全 store 事件 ID 可以有间隙，目录不要求连续。

目录只依赖现有 `(kind,id)` 主键，查询形状为：

```sql
SELECT id, CASE WHEN length(CAST(data AS BLOB)) <= ? THEN CAST(data AS BLOB) END
FROM objects
WHERE kind='reading_bookmark_order' AND id >= ? AND id < ?
ORDER BY id DESC LIMIT ?
```

下界是 `root_key + ':'`；首页上界是 `root_key + ';'`，续页上界是最后一个已扫描定位的完整 ID，不是最后一个匹配项。固定宽度使 ID 的字典序就是实际 set 事件顺序，root 范围及倒序直接对应现有主键；不采用 JSON 排序、事件历史扫描、深 OFFSET 或同时间补扫。定位对象仅含 schema、store/main、书签 ID、附件 ID、generation、event_id；返回前必须与有界书签记录中的 `last_set_event_id` 等字段逐项一致，不能仅凭 ID 前缀授予身份。lookahead 不算已扫描，不越过它生成游标。实际查询计划和固定候选窗口仍需实现后经守卫验证。

游标最长 512 字符，绑定 schema、store instance、当前 root main、固定活动状态/`last_set_event_v1` 排序及 `name_query` 筛选。seek 位置必须在当前 root 的主键范围内且不超过 128 UTF-8 bytes，让键长仍有界但内容损坏的定位也能占候选预算并继续；正常定位必须另外符合真实事件键及当前书签的一致性要求。超额或无法完整解码的定位键直接失败，不能以截断键跳过未知候选。沿用严格解码、类型检查和失配拒绝模式，不把游标当授权。名称是最多 120 字符的字面包含匹配，目录最多对有限候选逐条点查有界附件元数据；附件缺失时保留可定位的书签和不可用标记，不编造名称或读取文件验证。整项、完整 ID 与真实续页位置都计入输出预算，无法容纳首项就失败，不截断身份。

目录是 live 视图，设置/清除可能改变排序；更新后从首页发现最新位置，不承诺固定快照或跨修改无重复。只有主人明确 set 才成为候选，名称变化也不会把普通阅读变成书签。

## 页证据：复用工具归档，不把事件当交付

仅引用模型提供的 JSON、`tool.completed`、文件名、工具名称或目录预览不足以设置书签。首版为未来的真实普通分页结果增加可选宿主页凭据，放在原 tool message 的宿主元数据中，不为每次读取另建持久对象。

1. `LocalServices` 的真实普通文本分页、文档阅读 handler 在读取成功后暂存一份有界页候选。候选绑定实际结果对象、本次宿主请求与 reader；legacy 预览、ASR、视觉解释、错误和取消不产生候选。暂存只服务于紧随其后的工具归档，不排队、不写书签；覆盖同名工具的插件不能仅靠伪造同形结果取得凭据。
2. `Runtime._execute` 在已有完整工具消息的构造点核对候选、实际 call、session/run、原始结果与最终序列化正文。只有 `content_provenance="accepted"`、完整输出未被 runtime 改写时，才将小型 `reading_page` 元数据随该 tool message 同事务保存。可参考 `completed_publication` 的真实结果投影方式，不照搬它对 `result.truncated` 的拒绝：合法文本分页本身可能有 `truncated=true`。
3. 凭据包含 schema、store instance、核验的 root main、真实 `source_session_id/source_agent_id/source_run_id`、队友当次 generation、真实触发输入身份（不改写为 owner）、reader、实际 assistant/call 定位、附件 ID、原文件与正文版本、页起止位置、总字节数、EOF 信息，以及完整序列化页结果的摘要；文档页另绑定当次实际文档策略指纹，不复制正文。单份凭据最多 2 KiB。候选必须在结束、失败或取消后释放，不能串到下一调用。可选凭据无法形成时不掩盖原读取结果，后续设置明确拒绝该无凭据页。
4. 设置 handler 使用已有 `conversation_read` 所提供的页 `message_id/revision` 定位，按主键读取并验证宿主凭据。按当前 `max_output_chars * 4` 门控完整原始结果字节，复用 `message_evidence_slice` 的 SQL 字节门；不把 `HistoryEvidence.read_content` 返回的 JSON 外层页误当成附件页，不无限拼页。正文超出当前核验预算就明确拒绝，不取一个前缀认证整页。
5. 重验实际 role/source session、消息证据版本、`accepted`、真实 read call 配对、结果摘要、页结构和字段一致性；主助手页或同 store/root main 的真实队友页均可作为来源。要求 `length_bytes == len(content.encode("utf-8"))`、`next_offset_bytes == offset_bytes + length_bytes <= total_bytes`，以及准确的 `has_more`。旧版无宿主凭据的页不能追认；可由主助手或原有协作队友按需重新读取，重新读取仍不自动设置书签。

来源页必须在**本次设置工具所在 assistant 批次之前**已完整归档。批次边界使用 `_execute` 已持有的真实 `assistant_message.seq`；实现时向既有工具执行上下文传递一个只读宿主批次边界，不从模型参数接收，也不另建持久授权状态。相同批次“先读再设”拒绝，即使前一调用刚落档；下一次模型轮次可在同一次主人请求中显式设置，不要求额外主人输入或普通助手答复。

队友页通过已有全 store 的 message_order 序号做相同先后比较，不把不同会话的局部位置混为一谈。可从队友已有回报/交接提供的真实消息定位，或用 `conversation_read` 对其 session 取得消息证据；主助手不需要为了把队友那一页设为书签，再发起一次 `document_read`。

这一条件只证明本地完整归档，不能证明真人已阅读、浏览器已呈现或外部私聊已送达。因此产品只说“已保存续读位置”，不说“你已经读完这一页”。工具调用是显式选择位置；模型不得因为普通阅读或队友报告自行设置。

## 书签记录与身份边界

复用既有 objects，新增 `reading_bookmark` 及小型目录定位 kind `reading_bookmark_order`，各为 schema 1；不增改 SQLite 表或索引，不改变现有格式准入、启动迁移策略或主配置。书签记录键由当前 `store.instance_id + existing_main_session_id + attachment_id` 的固定编码派生，不能由文件名、渠道或模型传入的 owner 生成。定位键按上一节的真实事件顺序派生。

活动记录只保存 `id/schema_version/store_instance/main_id/attachment_id/generation/state/last_set_event_id`、有界 `page` 元数据、选定的 `position` 和更新时间；最多 4 KiB。page 保留 reader、原文件与正文版本、页起止坐标/总字节数，以及来源页真实 session/agent/run、消息 ID/证据版本；不保留正文。响应中的选定偏移和 EOF 从该页及 position 确定。更新时间只供展示，回拨不改变排序。清除记录保留身份、schema、代次和删除状态，不保留位置或活动定位。每个真实附件每个 main 至多一条活动或清除记录，以及至多一个活动定位；重新设置删除旧定位，不为每页或每次设置累积排序对象。事件沿现有日志保存。未知 schema、损坏、定位不一致或超额记录失败关闭，不覆盖成空记录。

所有持久读取先经 SQL 字节门；页凭据、书签和来源元数据分别有上限，输出还受当前工具额度限制。写入前预先检查完整响应预算，不因持久化已成功才发现必要身份放不下。代次使用非负有界整数，到上限拒绝写入，不回绕为 0。

不同渠道中经现有接收门验证为同一主人的输入共用位置。目录/读取的 caller 使用真实工具上下文确定身份：main 直接对应 existing main；队友先核对 agent.id/session_id 与真实 session 一致，再复用 `TaskControl.scope_lineage`，按现有 `TaskInspection.handoff_limits()` 的跳数与字节预算只核指定父链，要求末端确实为当前 main 的真实主会话。无任务绑定的队友可通过；环、断链、异常或超额拒绝，不枚举整个团队。现有 Runtime 的任务/咨询/渠道 read guard 继续适用，读书签不新增写权限。

来源队友页在生成凭据时绑定其真实 actor/run/generation 和核验的 root；设置时复验来源 session/agent、历史 run 归属和同 root 的结构父链。普通从 running 到 completed、报告落档或后续 generation 更新不抹掉真实历史页，不将当前 agent generation 冒充源页当次 generation。来源页的 root/actor 断链或不一致则拒绝；主助手不能把 teammate principal 改写为 owner 来补证明。

`task_file_publication._current/_completed_publication` 可复用真实 driver/run、agent/session、实际结果对象、call 配对及原子归档的核对思路，但它要求直属、绑定任务且批准 epoch 一致，不能原样用于普通队友页。根归属复用上面的 `scope_lineage` 和 HistoryEvidence 的真实 source session，而不是引入一套新的身份登记或把文件发布证明当阅读证明。

设置/清除仍仅真实主助手当前主人请求可调用。队友、工作流、咨询、系统 Attention、历史假 owner 标签和直接伪造 handler 参数都不能修改位置；主助手选择真实队友页也不会给源队友新增写资格。写 handler 参考现有真实主人操作门，复验当前 driver、run、request、真实主人触发输入、维护准入、停止/关闭、新 pending 输入和工具开关；不把附件正文里的要求当主人请求，也不绑定 `memory.enabled`。

记录不保存回复路由，保存旧位置不重启原会话或复活旧权限。后续回复使用当前请求原有来源合同；不因为位置来自另一渠道就主动向那个旧渠道发送。

## 设置、清除与续读的提交顺序

设置按以下顺序进行，数据库事务中不 await：

1. 捕获真实主人请求、当前工具批次边界和调用方传入的预期代次；有界核对当前书签和主/队友来源页凭据、真实 root 归属，选择页 `start/next`，不由模型计算字节位置。
2. 复用 `read_verified_window(window=0)` 与 `_owned(file_io(...))` 做有界流式原文件摘要、inode 与大小校验，核对当前附件元数据及文件额度，原文件版本必须与来源页相同。文本位置因同一完整文件和真实页字节边界而成立；文档位置来自该真实已提取页，保存时不运行 PDF 转换器或 DOCX 提取，也不重新生成正文。文档策略必须仍与页凭据捕获的指纹一致；变化时需要一页新的真实读取证据，可来自原协作队友。
3. await 返回后重验真实 driver/run/request、停止/关闭、工具开关、维护准入、pending 输入、来源页证据版本/结果摘要、source root 父链、批次身份、附件元数据、限制及文档策略指纹。文件或设置在校验期间变化拒绝，不用旧元数据批准晚返。
4. 显式 `BEGIN IMMEDIATE` 取得短写事务，再重新读取书签，要求代次仍为 `expected_generation`，并核对已有活动定位；不能只用 `with db` 包着 SELECT 就假称 CAS 读取已锁定。复用 `Store._event` 先插入有限 set 事件并取得实际 ID；以该 ID 生成 `last_set_event_id` 和新定位键，完整校验响应预算后用 `_put` 保存 `generation+1` 的书签，删除旧定位并保存唯一新定位，一起提交。使用事务内的 `_event/_put` 和同连接 DELETE，不调用 `Store.delete/put/event` 等可能独立结束事务的外层便利方法；事件不在提交前发布。事件 ID 不是先读 max 再加一，SQLite 分配失败也不得留下新位置。CAS 同时绑定当前已核原文件版本和来源页真实正文版本，不把代次相同当作版本相同。任何一步失败回滚事件、书签与定位；提交前不再插入 await。服务入口发现调用方已有活跃事务就拒绝，不能替调用方提交或回滚。

这里保存的是“经核验原文件仍相同、实际观察过的正文版本位置”，不是提前证明未来提取结果。外部转换器二进制更新或非确定输出可能在设置不变时产生不同正文，元数据与原文件 hash 不能排除这一点；下一次真正续读仍携带所存正文 revision，由原 `document_read` 重新提取并拒绝不同结果。这个边界既保留版本保护，也避免为保存队友页而让主助手再独自提取一整份文档。

清除不做文件读取，但复验相同主人写门和预期代次，同事务写清除事件、tombstone、增加代次并移除活动定位。禁止物理删除书签后退回 generation 0，以免旧创建请求发生 ABA。清除后再次有意设置可以读取新代次再操作；重开、重放和晚返不会自动重新设置。读取、目录和普通启动不修复或重建定位；损坏时保持失败边界，不能扫描全库按时间重排。

`reading_bookmark_read` 只读保存的位置。助手取得参数后再显式调用原 `attachment_read/document_read`；该工具照常检查当前权限、版本、字符边界和输出预算。续读成功也不自动改书签。EOF 位置可以保存并通过原 reader 核验，EOF 只代表本次文字正文末尾，不代表 PDF 图像、布局等全部内容已读完。

## 持久服务与工具集成的接口边界

独立 `ReadingBookmarks(store)` 只负责有界持久化，不注册模型工具，不读取原文件、不验证真实主人请求、页归档正文或 team 父链。调用方必须先完成上述运行期核验，传入已核 root 和页元数据；服务仍复验真实 existing main marker 及其 `kind='main'/principal='owner'`，不调用会创建主身份的入口，并核对全部输入类型、身份、长度、版本、页坐标与当前附件元数据。不将内部 `set` 暴露为可由模型直接提交任意坐标的工具。

固定内部接口均返回 `dict[str, Any]`，沿用 `EvidenceError(code, message)`：

- `read(main_id, attachment_id, *, max_output_chars)`。
- `list(main_id, *, name_query='', cursor=None, limit=20, max_results=100, max_output_chars)`；调用方传入当前 `limits.max_search_results`，服务收紧本页数量。
- `set(main_id, attachment_id, *, page, position, expected_generation, max_output_chars)`；`position` 仍仅 `start/next`。
- `delete(main_id, attachment_id, *, expected_generation, max_output_chars)`。

`page` 是严格键集合的扁平 mapping：`store_instance/main_id/attachment_id/reader/source_revision/revision/offset_bytes/length_bytes/next_offset_bytes/total_bytes/has_more/source_session_id/source_agent_id/source_run_id/source_generation/source_message_id/source_message_revision`。reader 为 `attachment_read/document_read`，两个正文版本为完整 64 位小写摘要；所有坐标拒绝 bool 并使用非负有界整数。主助手页的 source session 等于 main，agent/generation 均为 None；队友页保留非 main session、真实 agent 与非负 generation。host ID 与 opaque 消息版本限 1–96 UTF-8 bytes，附件 ID 沿既有 32 位小写 hex。文档策略指纹只由调用方捕获并在最后效果前重验，不作为服务可独立证明的权限。

读取、设置、清除的响应都含完整附件 `id/state/generation/verification='not_checked'`。活动响应另含 `reader/source_revision/revision/offset_bytes/total_bytes/eof/last_set_event_id/updated_at`、原 reader 的 `read_arguments={id,revision,offset_bytes}`，及 `source_page={session_id,agent_id,run_id,generation,message_id,message_revision}`。目录项沿用活动响应，加 `name/metadata_available`；不把位置保存前的原文件核验说成下一次续读已经核验。

## 失效、取消与恢复

| 情形 | 合同 |
| --- | --- |
| 文件缺失、原始字节或提取结果变化 | 原文件缺失/变化时设置和续读拒绝；策略变化使旧页不能用于设置；原文件/策略未变但重新提取结果不同，由实际续读的 revision 门拒绝。均不静默回到首页或猜新偏移；原书签作为未核验历史位置保留，主人可明确清除或重新设置。 |
| 清除、新设置在旧设置 await 期间发生 | 旧请求 CAS 失败，不覆盖新代次；不同附件位置互不影响。 |
| 源页被编辑、删除、移到其它身份或是 runtime 节选 | 凭据/证据版本/结果摘要不匹配即拒绝，不以页正文中的同名字段补证。 |
| 读取、核验或审批等待期间取消/停止/关闭 | 未提交的设置不落盘，已有工作线程/转换器沿原取消清理收尾，迟到候选不写书签。 |
| 已提交后，响应通知或连接取消 | 不能承诺回滚。返回/记录可能不明时先读取书签核查；不自动重试写操作或反向清除。 |
| 同一 store 正常重开或墙钟回拨 | 活动位置、tombstone、代次及真实 set 事件顺序保留；不按墙钟重排或自动回填，不重读正文、不运行模型。下一次显式续读重新核验文件。 |
| 凭据复制到别的 store/main，或旧 handler 的真实执行已结束 | 身份或当前执行门拒绝，不能把同名 ID 当同一主人。 |
| 队友完成、报告落档或后来执行新一代任务 | 真实已归档页仍是历史资料，保留原 actor/run/generation；不能自动设置或把历史页伪装为当前执行。root/来源关系损坏则拒绝。 |
| 遗忘记忆来源 | 不重建来源、不收藏、不恢复已忘记的正文。书签属于仍保留的附件；继续读附件仍须显式走原读取合同，不能把书签当记忆副本。 |

文件系统校验只能证明该次读取的快照，数据库事务不能冻结外部程序以后修改文件。每次续读重新核验，而不是把书签写入成功称为文件永远不变。

## 必要回归与可证伪验收

以下为设计的完整验收要求，具体已覆盖节点见四份 `tests/test_reading_bookmark*.py` 及 `tests/test_reading_pages.py`。上述关联回归包含实际持久层、Runtime 与页凭据；额外队友用例通过真实旧代次 DOCX 页、主人保存、队友查询和写入拒绝，提取次数始终为一。全部使用合成附件、真实 Runtime/Store 和可控 await，不模拟 guard 或 CAS 本体，不碰真实用户数据或桌面；不能由这些本机用例推断所有渠道、跨版本升级和真实 PDF 环境已验收：

- 中文、emoji、引号与换行的真实分页结果保存 `start/next`，正常重开后读取书签，再用原 reader 从同一位置续读；同名不同附件互不串位。读取书签和续读前后代次不变。
- 从另一主人渠道且上下文已压缩/正常重开后，`reading_bookmark_list(limit=1)` 找到最近明确设置的书签，无须先知道附件 ID；大量最近普通阅读不能改变结果。冻结 `time.time()`，先建 A、再建 B，再依次重新设置 A、B，每一步 `limit=1` 必须返回最后成功 set 的对象，不能受原 rowid 影响；将墙钟调回更早时间后设置 A，仍必须返回 A，正常重开后顺序不变。必须走真实事件插入与书签事务，不模拟排序 ID。
- 目录覆盖名称筛选、损坏/悬空定位占预算、空页但有后续、游标错 root/store/filter/order、更新后 live 翻页及很小输出预算；清除最近项后下一项可发现，同一附件多次设置始终只留一个活动定位。给其它 root、普通对象及无关事件加入大量合成条目，验证真实查询按 `(kind,id)` 范围 seek，无全 JSON 排序、全事件扫描、深 OFFSET 或为凑匹配项越过候选预算；冻结时间与回拨时的跨页顺序也必须与实际 set 事件一致。
- 同一主人请求中，读与设位于同一工具批次时拒绝；下一模型轮次引用已归档页可成功，不要求额外主人消息。普通读、队友读、失败读及取消读本身均不产生书签。
- 真实直属及有限父链中的队友均可查同 root 书签，不能写；其它 root、假 agent/session 配对、断链与环拒绝。真实队友读并完整落档后，主助手可显式引用那一页设置位置，凭据/书签保留真实 team session/agent/run 身份。统计真实转换器调用，保存和读取书签不得额外转换文档；后续续读仍校验正文 revision。
- 强制真实 Runtime 在 handler 返回后缩紧输出预算，得到 `runtime_excerpt/result_omitted`，不得形成可用页凭据；合法 `has_more=true/truncated=true` 的完整分页仍可设置。旧预览、ASR、视觉解释、伪造同名工具结果与假 owner 字段拒绝。
- 任意 offset 不是接口参数；篡改归档页正文、偏移、长度、摘要、证据版本或半字符位置拒绝。当前原文件字节或文档策略在设置等待中变化不得把旧位置提交到新版本；设置不重新提取，原文件/策略相同但后续转换输出变化时，实际续读必须因正文 revision 不同而拒绝。
- 设置等待真实原文件校验时，分别清除、另设位置、停用工具、出现新主人输入、停止、关闭或取消；旧请求不晚返覆盖。source 页在等待期间被编辑、root 链变化或文档策略变化也拒绝。不同附件仍可独立设置；无书签的清除也保留代次屏障。
- 真实 main 与队友/咨询/系统事项分别调用写入口；仅真实当前主人请求可通过。两个绑定主人渠道共用位置，不把旧渠道路由带入后续回复；其它 store/main 的凭据与书签拒绝。
- 损坏、未知 schema、超额记录、过小输出预算和代次耗尽都失败关闭；文件丢失的已有位置仍能显式清除。分别在事件插入后、旧定位删除后、新定位保存后注入事务故障，事件、书签及定位必须一起回滚，原最近顺序不变；CAS 失败不插 set 事件或移动顺序，事件整数耗尽也不留半次设置。提交后取消按读取结果核查，不假称已撤回。
- 保留原文本/文档、历史证据、工具完整回执、注册权限与 Store 重开回归。已有 current-format 库缺少任何书签对象时应能在禁止 DDL/DML 迁移的构造门下正常打开，首个显式 set 才写两类业务对象；不增必需 schema、不改 marker、不启动回填。新增对象/宿主元数据的旧版本读取兼容性仍需实际验证，不能由“不改表”直接推导完整升级交接成功。

## 实现与交付边界

实现分为 `ReadingBookmarks` 持久服务、`ReadingPages` 页归档核验与 `ReadingApplication` 四工具集成；LocalServices 挂接主/队友真实分页候选。Runtime 只增加只读批次边界及原 tool message 的可选页凭据归档投影，HistoryEvidence/Store 复用现有点查和字节窗口，不重写历史协议。目录复用真实事件 ID、小型活动定位对象、既有 `(kind,id)` 主键和有限候选模式，父链复用 TaskControl，不新建权限登记、SQLite 表、索引或格式门。当前 no-migration 启动合同继续成立。

本机验证覆盖主键范围查询与候选预算、事件/书签/定位事务回滚和 CAS、重开及墙钟回拨、真实消息归档凭据、读取前文档策略快照、单次主人请求中的下一模型轮次保存，以及完成/新代次后的历史队友页。两名实现者交叉审查，未发现有证据的 P1/P2；其要求补充的单请求与队友交接旅程均已通过。没有把 `scope_lineage` 的整对象摘要冻结成会随进度变化失效的书签授权。

已补短用户指南和文档索引。验证由根任务在共享资源守卫内串行收取终态；十五文件整合守卫 94.449 秒、峰值 247.8 MiB，额外队友回归守卫 3.243 秒、峰值 89.6 MiB，均 swap 0。相互重叠的定向检查不累加。发行包、托管 CI、真实外部渠道及跨版本更新交接须按各自实际结果报告；本功能不改变 #37 的独立原生验收范围。
