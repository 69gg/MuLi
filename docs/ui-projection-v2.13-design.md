# 2.13 主会话显示投影与恢复合同

状态：设计稿，正在独立审查；没有实现、迁移或性能验收。本稿基于 2.12 候选 `8d4763a`。用户已授权自主设计和后续实施；下述配置默认值与接口名是本稿的工程选择，仍需按切片验证，文档不增加新的批准门。

## 要交付的行为

沐璃继续使用同一个持久主会话。网页重开、跨渠道输入、工具审批和进程中断都不创建替代身份，也不把旧记录重新提交成主人请求。首屏只读取显示所需的小记录；模型收到的上下文、已保存的原生消息和网页显示窗口是不同的视图。

恢复后显示当前事实：已被运行器判为中断的任务不能因旧 `run.started` 再次变成运行中；未决审批不因事件窗口截断而消失；缺少完整工具结果的调用显示“结果未知”，不会自动重做。正在生成的正文和 reasoning 继续逐片出现，不能以定时重新请求整个 snapshot 代替 token 流。

交付拆成两片。**A 是可以先独立交付的恢复正确性修复；B 才建立完整首屏/续流资源边界。只有 A 通过时，不宣称首屏、完整活动恢复或长期浏览器内存已经有界。** 不要求一次合并全部投影架构。

## 当前代码给出的事实与限制

| 路径 | 已有行为 | 问题 |
| --- | --- | --- |
| `server.py:visible_messages/conversation`、`Conversation.pending` | 最近 1000 条消息全文、最多 1000 条 pending，再附最近 3000 条事件 | 限条数，不限完整响应字节；消息仍含 provider_data、工具参数等非显示字段 |
| `Store.events/recent_events` | 每批 500/最近 3000 条完整 `data` 解码 | 事件窗口不是活动状态的权威来源；大载荷同时经过 snapshot 与 SSE |
| `runtime._recover` | 持久 session 变 idle，未完 run/approval 变 interrupted | 不补终态事件；`hydrateSession` 仍可能由旧 started 得到 running；app 只向上补 running |
| `runtime._repair_tail` | 以原有 tool 消息写 unknown，禁止自动重做副作用 | 工具时间线不从该消息恢复；终态 reducer 不修正 running tools |
| `web/state.mjs` | 每事件复制并增加 `seen`，消息和工具追加 | 长期开页随事件增长；seen 累计复制成本也增长 |
| 两路原始 SSE | 全局流处理管理失效，会话流处理聊天 | 主会话完整消息/参数会在全局流再次传输，很多消费者并不读取它们 |

工具结果已经受 `limits.max_output_chars` 约束，不能把所有单条载荷都称为完全无限。缺口是显示链路没有独立的字节、窗口和恢复合同。`message_previews` 与证据 SQL 投影避免 Python 展开原生 blocks，但 SQLite 仍访问、解析源 JSON；它们本身不证明巨型单行的进程峰值内存有界。

## A：先修恢复权威，不引入投影数据库

### 服务端返回当前运行事实

在现有 `/api/conversation` 增加 `recovery_version: 1` 与 `live`。`live` 在无 await 的同一宿主快照步骤中取得，字段如下：

| 字段 | 合同 |
| --- | --- |
| `session_id` | 真实主会话 ID，必须与外层 session 一致 |
| `run_id` | 当前进程 `runtime.run_ids` 中该 session 的运行 ID；没有活动运行时为 null，不从旧事件猜测 |
| `status` | idle/queued/running/approval/paused/interrupted；活动 run 必须确实属于该 session；无活动运行不得返回 running/approval |
| `pending_approvals` | 由该进程有效 `Permissions.pending` 的 ID 定位持久 approval，再验证 session/run/status；不使用 `Store.list('approval')[:200]` 后过滤 |
| `interrupted_run_id` | 最近回放候选 run 若持久状态为 interrupted，可返回其真实 ID；没有可验证候选时为 null，不能扫描全库寻找一个看似最近的运行 |
| `after` | 与该快照同一短读事务取得的精确 raw event 水位字符串；会话流从它继续，不从 hydrate 最后一条数字 ID 猜位置 |

原事件仍可为时间线提供历史，但 hydrate 完成后必须以 `live` 盖定当前 run/status/可操作审批。服务端用当前执行槽判断是否仍在运行，用对应持久 run/approval 验证归属；旧事件不能覆写它。重启已取消的审批只作历史资料，不能重新获得等待 future 或授权。

对于已有窗口内的 tool：若对应运行已经停止而工具仍只有 started、没有可信终态，显示 unknown，保留原调用 ID 和原因。执行结论与模型回放收据是两件事：`_repair_tail` 合成的 interrupted/unknown 只说明工具消息缺失，不能抹掉已验证的 completed/failed/not_executed。反之，事件名叫 tool.completed 也不能直接证明成功，其 result 可能是错误、not_executed、unknown，或因裁剪成为无效 JSON。A 至少尊重现有顶层 not_executed；其他结论只在结构化结果可验证时采用，并记录来源完整性。截断且无法验证的结果不得因事件名变成绿色成功。关联无法从窗口证明时明示未知，不跨运行按 call_id 乱配。

只纠正新 snapshot 不够：服务重启前已打开的页面可能只重连 SSE。恢复处理须将权威对象修正与窄 `session.recovered` 事件置于同一事务，事件携带 session/run 归属、canonical status、审批失效标记和 recovery ID；运行器对外接收新执行之前提交。既有订阅收到后按归属收敛，不让旧 run 的恢复事实压住随后启动的新 run。没有未完成任务的启动不制造假中断。A 的快照水位和该事件只解决恢复顺序，不声称跨数据库替换的 epoch 问题已解决。

### A 的验收与刻意保留的缺口

先交付回归：重启后持久 idle/interrupted 压过旧 started；已打开页面的 SSE 跨服务重启收敛；有效审批不被无关的新审批历史挤出；无可信终态的可见工具显示 unknown；可信 not_executed/完成报告不被合成 unknown 抹掉；旧审批不能再次批准。前端仍须遵守 2.12 owner，旧连接的 live 响应不得盖住新连接。

**A 不保证找回事件/消息窗口外全部工具，不缩小全文响应，不修改 `seen` 算法，不声称 token/DOM 内存有界。** 若 A 没有 B，不应将版本描述写成“真正有界的会话首屏”。这让恢复错误可以先修，而不把数据库迁移混进同一补丁。

## B：显示行、活动清单与轻量续流

### 选择与不变量

纯游标/页窗口可以限制返回条数，但不能找回窗口之前仍未完成的审批与工具；在源 JSON 上临时截字段也不能消除解析巨型单行的成本。因此 B 选择**写入时生成有上限的显示行，加上可淘汰的轻量帧缓存**。这是可重建索引，不是第二套会话、审批或工具结果账本。

权威数据仍是现有 session/run/approval 对象、ingress、消息及 events；结果原文继续走 [已保存正文归档](history-archive.md) 和 [证据续读](history-evidence.md)。投影不得成为模型 replay 输入，不参与 compaction 的授权判断，不以 UI 节选覆盖原数据。API 显式返回 `content_provenance`；runtime_excerpt 的缺失部分不能由投影恢复。

### 候选持久结构：两个派生表

这是 B 的候选实现，必须单独审查改表、写放大和维护成本后再决定实施。较小替代是先保持 A，给既有字段加明确显示上限与 SQL 小窗口，但如实保留巨型源 JSON/窗口外活动的边界；不能把这个替代称为完整 B。下述两表不应仅为让方案看起来完整而提前加入主干。

`ui_view` 保存当前显示行。主键为 `(session_id, kind, item_id)`；`kind` 为 head/message/ingress/tool/approval_ref。公共列为 `run_id/state/order_seq/source_kind/source_id/source_revision/applied_seq/body_json`。索引至少覆盖 `(session_id,kind,order_seq,item_id)` 和活动项 `(session_id,kind,state,order_seq,item_id)`；不靠扫描 objects 中所有 JSON 找当前项目。

| kind | 必需显示字段与来源 |
| --- | --- |
| head | 当前 run/status、输出 `stream_id`、有界正文/reasoning 尾部、各自完整字节位置、截断标记、错误代码；每 session 一行 |
| message | 原 message ID/seq/revision/role、完整或明确省略的 principal/source、内容预览/总字节数/截断/保存前完整性、有限附件 ID；只引用原记录，不复制 provider_data/图片/原生 blocks |
| ingress | 原 ingress ID/seq、身份/来源、pending/quarantined/delivered/cancelled、预览和附件引用；消息落盘后按同一原 ID 去重，不将 ingress seq 当消息 seq |
| tool | 稳定 invocation 定位、原 call_id、工具名、execution_state、receipt_state、source_completeness、有限参数/结果说明及原事件/消息引用；不能只按 call_id 或 `(run,call)` 合并 |
| approval_ref | 原 approval ID/run/tool/risk/status、短理由、参数大小和详情可用性；参数与决策仍在原 approval，不再复制一份审批实体 |

`body_json` 有写入字节上限。原 ID、role、归属、版本及游标必须完整，不能截出另一个似乎完整的身份；可选 metadata 放不下时用 `metadata_omitted` 标记，身份未知就展示未知，不由 user role 推成主人。附件只给受限数量的 ID 和总数，原附件接口负责字节读取。

invocation 取 `(session_id, assistant_message_id, call_index)`，可编码成稳定宿主 ID。现有运行器只保证单次模型批次内 call_id 唯一，同一 run 的下一轮允许复用它。生产方在原 assistant 消息及原工具批次落盘后，向工具开始/完成、审批和工具结果消息传递该标识、run_id 和 batch 来源，不能由 head 当前 run 反推迟到调用。模型原生 call_id 与协议回放保持不变。旧数据只能在可验证的 assistant 批次边界内配对；无法配对标 limited，不跨批猜测。

`ui_frames` 是有限续接缓存：`seq INTEGER PRIMARY KEY AUTOINCREMENT`、scope、session_id、source_event_id（对象直写可为 null）、kind、body_json、wire_bytes。其内容仅为小 upsert、append、终态或失效通知；没有第二份完整工具结果。超出保留预算就淘汰旧帧，不删权威事实与 `ui_view`。投影重建可删除这两个表并重做，不能改变任何执行决策。

复用 `Store.instance_id` 区分资料库；新增 `stream_epoch` 每次服务启动和不兼容重建时更换。epoch 改变要求重新取得有界快照，而不是跨备份恢复或重启沿用可能复用的序号。schema 版本与 epoch 不冒充消息 revision。

### 原子写入点与中断语义

沿用 `Store._put/_event/_enqueue_input` 的调用者事务，不在其中嵌套提交；`bus.notify` 只能发生在提交后。对已选用 projection_v1、承诺完整 ready 的写路径，投影失败回滚同次权威写入，不能静默留下漏项却仍宣称 ready。

默认 A/B 禁用时不启用投影写钩子。候选 building/blocked 阶段不得以同等强耦合卡住 A：候选派生操作放在外层事务的 savepoint，失败时只撤销候选修改，在同次提交把投影完整性置 limited/不可选并暂停候选钩子，再提交正常权威事实。若连一致性失效标记也无法持久提交，则整个事务失败，不谎报候选可用；底层数据库自身不可写不是可忽略错误。回填恢复后需核对 checkpoint 和新写入缺口，不能直接把旧候选重新标 ready。此候选隔离与 ready 强一致的差别须在 B 启用前单独验收。

| 现有写入点 | 同一事务内的派生动作 |
| --- | --- |
| `_put('message')` | 原消息与 message_order/revision 保存后，更新 message 显示行、对应 ingress 去重或 tool 结果引用，并写 upsert 帧；不等待随后可能未发生的 message.completed 才让显示行存在 |
| `_enqueue_input`、claim/cancel ingress | 更新小输入行、计数和状态帧；重复输入保持原幂等合同，不重复生成显示消息 |
| `_event(run.started/terminal)`、run/session 状态写入 | 更新匹配 run 的 head 与终态帧；旧 run 的迟到终态不能清空新 run；不能让 session 的 running 字符串单独复活已中断 run |
| `_event(tool.started/completed)` | 保存原事件，同时更新工具显示行和帧；started 必须先于工具 handler；工具结果消息写入时补齐原文引用 |
| approval 创建/resolve/cancel | 将原 approval 对象与对应事件收进现有 `Store.event(..., objects=...)` 同一事务，再更新 approval_ref 和帧；创建前注册等待者，提交失败则撤销等待者；resolve 提交成功后才唤醒 future |
| `_recover` | 在公开投影 ready 前分批修正生命周期与派生行；started 且没有可信执行终态的中断调用标 unknown；审批 interrupted，不恢复批准能力；A 服务可继续，不因可选投影回填阻断主会话 |

工具 completed 事件和工具消息目前分开落盘，B 不以重构所有工具调用作为前提：显示行分别保存 `execution_state` 与 `receipt_state=present/missing/repaired_unknown`。B 写入方持有未裁剪 result 时先归一化执行结论与完整性，将小型 execution_state/source_completeness 同时写进原 tool.completed envelope 和派生帧，再对显示结果做节选；这不是另建结果账本。已知 not_executed 始终保留，已验证的完成/失败报告也不会仅因结果消息缺失而改成 unknown。只有没有可信终态的 started 在运行中断后归为 unknown。后到的工具结果或 `_repair_tail` 合成消息更新 receipt_state 和原文引用，不能抹去更可靠的执行结论。界面可明确写“工具已报告完成；模型回放结果记录未齐”。

只有从原记录得到的 not_executed 才能显示“未执行”。unknown 不携带重试动作；修复尾部仍沿用原消息账本，不能另造可执行任务。工具名/参数的历史缺失通过 source completeness 标记暴露，不以空对象冒充完整信息。

### 配置和最终编码预算

增加独立 `ui_projection` 配置节，避免拿模型 `max_output_chars` 冒充 UI 字节预算。下表是建议默认值，均须 strict 类型、正范围和上界校验；具体上界不得在测试失败后放宽。

| 设置 | 建议默认值 | 作用 |
| --- | --- | --- |
| `enabled` | false | B 先作为显式可选能力；默认升级使用 A，投影未 ready 不停用原主会话 |
| `response_bytes` | 131072 | 单次完整 UTF-8 JSON 响应，包括外层字段和转义 |
| `window_items` | 40 | 主屏消息候选条数；实际还受完整响应字节限制 |
| `preview_bytes` | 2048 | 普通消息/工具说明的显示正文上限 |
| `stream_tail_bytes` | 16384 | 当前正文和 reasoning 各自的常驻尾部上限 |
| `frame_bytes` | 8192 | 一个完整 SSE 帧，包括 ID、JSON、转义与分隔符 |
| `replay_bytes` / `replay_frames` | 262144 / 256 | 一次恢复最多读取的缓存字节/帧数；不足则 reset，不循环追完历史 |
| `retention_bytes` | 8388608 | 全库轻量帧缓存的目标字节上限；按有界批次淘汰 |
| `source_record_bytes` | 1048576 | 显式迁移/审批详情允许整体解码的单条源记录上限 |

固定关系必须校验：head、最小身份、游标和最小活动入口能放入响应；最小 patch 能放入一帧。head 的两路尾部可以大于 frame_bytes，**head 整行只用于 snapshot，不发整行 head-upsert**；流里只发标量 patch、UTF-8 append 和重置引用，普通 item upsert 也须独立满足一帧预算。序列化时按最终 UTF-8 长度测量，不能仅按字符或源正文长度估计。对象列表用条数与累计字节双限，不先 fetchall 再节选。

响应先保留 head、恢复位置、各类活动数量/继续读取入口，再分配消息与活动页；不为凑正文省掉 unknown/审批的存在提示。单项身份放不下返回 output_budget，不推进页游标。预算修改只允许空闲重配；降低预算后先处理小投影行和缓存、换 epoch，再 ready，不能借机读原始全文。

这些预算约束显示派生物与每连接读取，不承诺模型驱动自身 partial/native 内容、磁盘总历史或任意数量并发客户端的资源上限。

### API：首屏、活动页和一条 UI 流

新增认证、同源、no-store 的接口。网页先通过小型 capability 事实选择 recovery_v1（A）或 projection_v1（B），不把试验 B 作为升级后唯一聊天入口。仅当用户已启用且投影达到本节完整性合同才选择 B；一个已经选择 B 的连接失败后不能悄悄转成全文 A 而继续声称有界。

1. `GET /api/ui/conversation` 返回 `version/instance/epoch/session/live/through/messages/activities/activity_counts/cursors/budget`。`through` 是全局 UI 帧序号的精确十进制字符串，始终与 raw event ID 分开命名。在同一短读事务内取得水位、head、当前消息和活动第一页，不查询原 events.data、messages JSON 或 ingress.payload；保留主 session ID，不触发模型或发送。
2. `GET /api/ui/sessions/{id}/activities?kind=...&after=...&limit=...` 分页列工具 unknown/运行项、审批与 pending。返回完整定位、`has_more/next` 和当前版本；活动列表随写入变化，明确不是跨请求快照。游标为排序键加 ID 的复合位置，删除项不会造成跳到未知位置；新增项使第一页失效。每页必须前进，重复/伪造游标拒绝。
3. `GET /api/ui/events?session_id=...&instance=...&epoch=...&after=...` 取代网页原两路 raw SSE。流里只包含该主会话 patch 与全局轻量 invalidation；其他会话完整消息、参数、结果不进入全局流。管理界面按 2.12 owner/singleflight 读取当前事实，不再从全局事件拿完整 run/reminder 对象原地合并。

消息的完整 `content` 继续用原 `/sessions/{id}/messages/{message}/content` 和 revision 字节续读；不增加同义正文存储或新归档路由。审批参数不是消息正文，增加只读 `/api/ui/approvals/{id}/arguments`：复用 UTF-8 窗口、revision、输出预算与变更拒绝的公共逻辑，读取原 approval.arguments，不新存参数副本。先用源记录字节长度拒绝超过 `source_record_bytes` 的整体解码；不能用 SQL json_extract 绕过该检查。新建需人工审批的超预算参数在工具开始前明确 not_executed/approval_details_budget，不能展示不完整参数却声称完整可审。

首屏审批卡不内嵌全部参数，但必须显示参数完整性与详情入口。审批决定仍由现有 resolve 验证当前 future、session/run、pending；原审批变更或中止后拒绝。旧巨型审批详情不可读时显示 details_unavailable，保留 ID、风险与存在提示，不伪装成已批准或已取消。

### 保留真正的流式增量

每个模型输出段拥有持久的 `stream_id`，不是只用 run_id：同一运行可以多次发起模型轮次。文本/reasoning 帧形式为 `append {session_id,run_id,stream_id,channel,from_bytes,to_bytes,text}`；from/to 是完整输出中的 UTF-8 字节位置。宿主生产方提供 run/stream 归属，不能由迟到事件随意覆盖 head。

每个 append 与 head 对应尾部、位置以及帧写入在同一事务提交。超大原 delta 按完整 UTF-8 字符边界拆成多帧，逐帧写入，不先构造无上限帧数组；原 delta/消息的保存语义不变。前端收到正常帧立即追加并按帧调度 render，**不为每个 token 请求 snapshot**。reasoning 同合同。

前端按独立 UI seq 去重，只保留一个精确水位；同 epoch 中 `seq<=cursor` 忽略，合法帧才推进。append 起点必须等于当前 stream 的 to；重复帧不追加，缺口、错误 stream 或非法 UTF-8 定位触发 reset，不能拼接两段不相邻内容。尾部超过配置时裁掉完整字符前缀，并显示 prefix_omitted/完整字节位置。saved-message upsert 结束对应 stream，保留长文归档入口，不宣称当前显示就是全文。

客户端只保留当前消息窗口、当前活动页及固定 head/tail；window 外计数/游标仍在。移出窗口时同时清理 DOM、messageNodes/toolNodes，不只裁数组。pending/unknown/审批从活动入口可找回，不能因移出聊天窗而删原记录。历史草稿、附件待发引用与当前渠道身份不随重同步重置。

server 复用 EventBus 的通知唤醒与数据库游标模式，不建每客户端无界队列。单批 SELECT 和最终编码受帧数/字节双限；浏览器 SSE reader 也限制未完成帧缓冲，超预算断开并重同步。读取到既定水位且没有匹配帧时可发只含位置的 checkpoint，防止被过滤的其他会话事件使游标长期落后；未读完该水位之前的匹配帧时不得跳到 checkpoint。

慢消费者超过 replay 预算或缓存最早位置时，HTTP 未开始则返回 409 ui_reset_required；流已开始则发送无恢复 ID 的 reset 控制帧并关闭。客户端取消旧 owner 的流，以一次有界快照重新建立当前状态。reset 不是每个 token 的常规路径。缓存不足前已经淘汰的流片段不伪称可恢复；快照用有界 tail 和截断标记恢复当前可见段，最终已保存正文仍可归档续读。

## 迁移、兼容和明确失败

投影状态为 `absent/building/blocked/ready`，并独立返回 `history_completeness=limited/complete`、`activity_completeness=limited/complete`、缺失数量或未知数量标记、schema、位置、最后成功原 ID 和原因。ready 只表示小投影可服务，不等于完整历史已覆盖；projection_v1 的完整活动恢复承诺必须同时满足 activity_completeness=complete。空新库可以在受控初始化后 ready。已有库**不会因 HTTP 首屏自动运行全量回填，也不在 Store 构造器加入一次性全库 JSON 解码**。

默认升级先启用 A，现有主会话、跨渠道及工具继续服务；B 的表/帧缓存只有显式启用后才以双写方式建立，不切换现有客户端。先建立真实当前 head 和新写入投影，再按单独的历史 checkpoint 渐进补旧消息；当前活跃信息可从进程有效运行/审批槽取得。历史缺失可以 limited 并提供原归档入口，不要求所有古老正文都先完成迁移才能聊天。未知旧工具无法验证时 activity_completeness=limited，不能假称已恢复全部活动。

本期可实现的完整重建仍是显式离线维护步骤，不作为一般用户默认升级要求：运行器暂停接收执行，检查没有有效工具/审批等待者，然后在统一 resource guard 内分批建立投影。顺序读取源 ID/seq 与 SQL 字节长度；每批最多 window_items 条、累计不超过 source_record_bytes，单条也不能超过该预算。先读长度，超限不得把正文取回 Python 或用 json_extract 偷解码。每批事务记录 checkpoint，可恢复；中断后为 building。在线自动回填若要上线，必须另补明确的稳定水位、逐行 revision 检查与有界读事务；未验证前不宣称已支持。

单条超限、身份/工具配对无法验证、损坏 JSON 均保留源定位与原因，使对应回填段 blocked 或完整性 limited；**不能因一条古老巨行停用整个主会话**。第一期不发明流式 JSON 解析器，也不删掉难处理记录冒充 complete。B 专用接口不可服务时才返回 503 projection_not_ready；capability 仍明确 A 可服务，默认网页继续 A。limited B 可作为显式试验显示模式，但必须暴露完整性，不能把缺失项目数量未知说成零。一般上线前，必须具备自动有界迁移或已验证的可服务渐进状态，不能要求所有用户手动全库迁移后才能使用。

建好投影后，启动只检查小版本/位置状态，再执行现有生命周期恢复及对应投影修正；未完成时 B 投影接口不 ready，A 仍服务。来自迁移/运行恢复的 source completeness 不能伪造完整。重建换 epoch，旧缓存与游标显式失效。

旧 `/api/conversation`、`/api/events`、session 原 SSE 和工具内部 EventBus 协议保留兼容；A 继续使用，选择 B 的连接才切到轻量接口。旧客户端和 A 不获得 B 的资源保证。回滚分两种：关闭 B/回退兼容二进制只停止采用派生视图，不能删除或倒退原事实；恢复旧数据库则会丢失备份之后的事实，必须作为单独有恢复证据的运维操作，绝非投影失败的自动动作。归档窗口既有字符预算、SQLite 仍解析选中源 JSON 的边界保持如实说明。其他管理页、settings 和业务 mutation 也不因 B 自动获得同等预算。

| 情况 | 明确行为 |
| --- | --- |
| instance/epoch/schema 不匹配，future 游标，游标已淘汰或恢复量超限 | 409 ui_reset_required，附原因与有界 bootstrap 地址；不从 0 重放 |
| 参数格式、范围或负值错误 | 422/400；不进入流工厂，不夹成合法值 |
| 无法放下必需身份、游标或最小 patch | 400 output_budget，游标不前进，配置不自动加大 |
| B 投影不可服务或源版本不一致 | B 接口 503 projection_not_ready；不发假空状态，A 服务仍可用；已选 B 的连接不静默降级 |
| 审批已结束/中断、原参数版本变化 | 409 approval_unavailable/approval_changed；不恢复旧 future |
| 权威事实或完整 ready 投影更新失败 | 同事务回滚；不得执行依赖该 started/approval 提交成功的工具；未服务的候选阶段按 savepoint/limited 隔离，不拖停 A |

## 独立验证顺序与交付边界

A 可独立落地，只做当前运行事实合同、恢复事实事件、hydrate/重连权威覆盖、可见 unknown 与针对性回归，另同步当前 frontend/event 文档；通过后可先提交。不得将 A 的正确性结果当成 B 的资源结果。

B 的实施顺序是写入钩子/派生表与迁移状态、预算化只读 API、轻量 append 流与 reset、网页窗口/节点回收，最后切换启动入口。未覆盖所有标准消息/ingress/lifecycle/approval 写路径前不得标 ready；受信插件绕开 Store 直写 SQLite 不在一致性保证内。

所有执行验证必须使用 `/data0/MuLi/scripts/check_local.py`，前端单并发、每用例硬超时。使用临时库和离线有限帧，不动真实用户数据，不用压测或扩大资源上限。必要验证包括：

- A 的重启权威、在线跨重启及审批回归；B 再覆盖 started 早于保留窗口、同 run 跨批复用 call_id、completed/not_executed 与结果消息之间中断、执行结论与回放收据独立、旧 run 迟到、审批跨断线与重启。
- 在隔离临时库加入大正文/native 字段，以查询替身或 trace 证明 ready 首屏和 UI 流不访问原 payload 列、不调用旧 messages/recent_events/events；只验证响应小不够。SQL 长度检查也不得取回源全文。
- snapshot 与后继帧交界、UTF-8/NUL/emoji、单 delta 分片、多模型轮次、大整数 UI seq、重复/缺口、缓存淘汰/慢消费者、重启 epoch、未来位置与备份恢复。
- 单项/总 JSON/SSE 最终编码预算、极小预算明确拒绝、DOM/数组/节点 map 稳定窗口、草稿/附件保留；身份比较断言布尔结果，避免展开循环 DOM。
- 迁移中断续接、超限旧记录 blocked/limited、失败不推进 checkpoint、投影/权威同事务回滚；默认升级仍可使用 A，一条古老巨行不阻塞主会话；已选 B 的连接不隐式改用全文 bootstrap。

本稿不附实测峰值或吞吐量。完成 B 的协议回归后再在 guard 内记录有限运行的峰值证据；这不等同于真实模型、外部渠道和任意长期运行的整机保证。

## 参考代码与取舍

只读参考 `/data1/codex` 的 `app-server/src/thread_state.rs` listener 顺序与 generation、`request_processors/thread_lifecycle.rs` 的活动回合合并及未决请求重放、`app-server-protocol/src/protocol/thread_history.rs` 的 metadata snapshot，以及 `thread_processor.rs` 的独立分页/重复游标拒绝。MuLi 采用 SQLite 提交水位，不引入 Rust listener 或其无界 channel。

Codex compaction 更换的是模型上下文窗口；MuLi 已有 compaction/原文证据读取。这里不增加摘要模型、第二个正文档案、全文检索或新后台模型轮询。创新点限定为**把当前活动事实和直接 token 增量做成同一提交水位下的小显示视图**，保留原主身份与真实结果的不确定性。
