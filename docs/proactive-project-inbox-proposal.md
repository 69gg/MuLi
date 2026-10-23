# 项目收件后的主动资料复核（首片方案审查完成，未实现）

2026-10-24。主人明确把现有 Linux 收件目录关联到一个长期任务，新 UTF-8 文本到达后，同一 MuLi 在 Web 主会话核查当前任务要求、原附件和新资料，留下有引用的复核记录并提出下一步。**首片仅 Web 本地，不外发项目报告**，不自动改任务、派工、导入附件或执行建议。自然模型的影响判断质量不由固定 Fake 回答证明。

两位Sol交叉方案审查已完成。先将跨渠道项目外发留到独立切片，明确任务变更后的有限重消费；后审发现工具批后认领新主人输入可重算项目路由，现增加下述持久项目段与独立后续run合同，原P2在方案层闭合、无剩余必须修复项。尚无生产实现或该方案测试结果，不能把审查通过写作能力交付。

## 代码依据与选择

现有 `file_inbox.py::_commit/read_record/settings_state` 已提供 canonical event、有限元数据、来源 scope/revision、目录身份与重启合同；`attention.py::_consume` 有 cursor/receipt、忙时延后及配置锁内最终 guard。`Runtime.submit_attention/_round_tools` 在当前触发仍为 Attention 时保持同一主会话、`system:attention`、ask 和仅 read 工具。**这不等于跨工具批已隔离**：`_execute` 工具批后及无工具回复分支会再 `_claim_inputs`，随后 `_prepare_request` 重算来源；若此时纳入手机 owner 输入，续轮可变成渠道来源并失去 attention_routing，首片必须补下述执行段边界。`LocalServices` 主会话 scope=None，已有 `commitment_read/commitment_inspect/attachment_read/fs_read` 可按需读；收件路径不是附件 ID，`file_publish/import_file` 会复制正文且不能借事件默认执行。`Commitments` 已有版本、完整 brief 和 message/attachment 引用；受控 brief 不能由 `commitment_save` 整体替换。

| 最小方向 | 判断 |
| --- | --- |
| 只在记忆/提示词写“目录属于项目” | 无可靠绑定、撤销和项目证据定位，不选。 |
| **一个来源绑定一个任务，沿原 Attention 做 Web 本地只读复核** | 推荐；补上关联和当前版本核查，复用已有资料/报告路径，可独立交付。 |
| 自动导入、改账本、派工或跨渠道项目外发 | 增加写权限、生命周期或旧 reader 兼容责任，另片设计。 |

## 最小持久合同

- 扩展现有配置工具/认证 settings 入口，增加可空 `project_id`，完整替换、省略/null解绑。只接受真实主会话配置工具或认证管理入口；沿配置锁、维护/格式/closing与取消结算。绑定须明确指定现存、有完整 brief、非 done/cancelled 的 commitment，不能由文件名/正文猜任务。单个独立有版本 Store 绑定对象与来源期望配置同一 SQLite 提交；主 Settings、数据库 schema、附件 ACL 不变，不新增观察器/表/索引。
- **不可变来源证明**仅包含 canonical record/event ID、来源 scope/revision、binding revision、project ID；与收件事实同事务冻结。不把到达时 task revision/epoch 当来源有效期，不因任务正常修改重签或删除原事实。新记录只提供元数据，不展开正文。旧无项目记录保持 generic 合同；坏/未来项目证明拒绝项目处理，不降级成可外发 generic 通知。
- **消费时任务快照**另行读取：对指定 task 做 SQL 字节有界点查，记录当前 revision、status、epoch/approved_epoch等必要准入字段；构造当前任务读取定位。它是本次接纳快照，不改写来源证明。任务正文和原附件仍由模型沿已有分页/只读工具获取；不在宿主事件载荷默认注入。首片新资料只支持已有 `fs_read` 可读取的 UTF-8 文本；非文本、过大、缺失或读取被拒均明确未核查，不自动尝试 OCR/ASR/导入。

## 接纳算法：任务变更可重消费，终止不自旋

现有 `_record=None` 会令 cursor 前进，`_ProducerPolicyChanged` 会立即重消费，普通异常则保持 cursor 等 bus。必须区分以下结果，不能把当前任务 revision 不同当无效源返回 None，也不能借普通 ValueError 偶然等待下一文件事件：

1. 从当前 cursor 取原有有限事件批次，先核 immutable 来源证明，再读本次 task 快照。原项目证明对应的绑定已不存在、解绑/换项目/目录授权换代、task 缺失或 done/cancelled 是**终止**：该旧项目事实不再触发复核，可由正常 cursor 提交跳过；不能改归新项目或回落 generic。从来没有项目证明的 generic 事件仍走原处理分支。暂停不是终止：允许只读说明暂停，不恢复/批准/执行。
2. 按来源、binding revision、project 和 Web-local 路由分批。配置锁内最终 guard 再核来源/绑定和 task 快照；任务 revision/epoch等变化时抛专用窄类型（如 `_ProjectTaskSnapshotChanged`），必须发生在 enqueue 之前，因此 **cursor、attention_receipt、ingress 均不提交**。只对这一结果清掉临时快照，立即从原 cursor 有界重读当前 task、重建元数据；不等待另一文件事件，不重提交来源事实。
3. 为每个未前进的批次最多允许一次无新 bus 的立即刷新；第二次快照冲突则保留 cursor，在下一次真实 bus revision 变化后再尝试。等待比较使用本次构造快照**之前**捕获的 bus revision，不能在冲突后新取 revision 而错过已经发生的任务通知；沿现有 condition，无计时轮询。宿主任务更改沿现有通知路径唤醒；绕过宿主直接 SQL 改正文不在保证内。每轮让出事件循环，不把新窄异常塞入无条件 `return True` 重试环。
4. 稳定接纳时，与 cursor/receipt/ingress 原子提交不可变证明及**本次成功快照**。同一事实只接纳一次，成功后普通任务变更不再唤醒旧收件。源撤销沿现有取消 pending 来源批次；已开始的只读回合可以留本地，不能声称能收回已发送给模型的内容或保证整轮任务快照隔离。

锁后修改 task 而不追加任何收件事件的回归必须证明：第一次快照拒绝无 cursor/receipt/ingress 提交，随后读到当前版本、恰一次接纳；另测连续两次冲突后确实等待 condition，不忙循环。task取消/解绑则终止推进，不永久堵塞其它 Attention。

## Web 本地表示、证据和权限

项目批次使用现有 `FrozenRouting.bound(ReplyOrigin(kind="web"))` 的标准 **v1 Web 形状**：`selected_by=origin`、destination仅`kind=web`、无 channel route。`FrozenRouting.load` 认识该形状，`ReplyOrigin.delivery_routes()` 返回空；旧 reader 对这个路由值也不会选渠道。当前 `read_record` 只接受 independent，须仅为已核项目证明增加这个**精确 Web 形状**分支，不能普遍接纳任意 bound。项目证明与任务快照独立存放，不向 v1 bound 塞扩展权限字段。已有本地最终消息保持空 delivery_routes，不变成自动外发。

**降级只承诺明确拒绝与本地路由值安全，不承诺连续消费。** 旧 `read_record` 拒绝 bound 时，Attention 的 None 分支仍可能推进持久 cursor；若新版独立 settings 中加入 `project_id`，旧 `FileInboxSettings(extra=forbid)` 还可能令整收件源不可用。返升后不能假定旧事件重投或自动补通知，不为此回滚 cursor、扫描目录或建立兼容队列。旧二进制也没有下面的新执行段边界，不能把标准 Web 路由可读夸成跨版本运行中混入 owner 的完整隔离保证；降级/返升覆盖缺口须如实说明。跨版本外发 proof/旧 slot 拒发仍属另片，本片不创造兼容框架。

即使当前配置有默认主动渠道、配置来自手机、最近输入来自手机，项目批次和最终消息也只能本地：不能调用 `FrozenRouting.independent` 选默认目的地，不能与 generic 批次合并或继承最近渠道。既有 generic 文件提醒的原目的地/行为不改。项目跨渠道报告所需独立 proof、旧 slot 拒发、发送前绑定撤销门均**未交付、另片设计**，本片不调整通用 outbox 或 unknown 重试规则。

### 项目执行段跨工具批保持本地，新主人输入另轮处理

最小落点在 `runtime.py::PreparedRequest/_prepare_request/_execute`，项目 marker 由已验证、已持久化的 attention ingress/message 传入，不能从模型正文推断。沿现有 `Store.claim_inputs` 的来源与 routing batch_key 分组做首次认领，不改 generic 输入分组；项目与普通 owner 即使同为 Web，其 attention routing key 也不同。项目段由此次实际认领的项目证明建立，跨 `PreparedRequest` 重建显式保留 Web-local 与 read-only 限制，不能仅重新查询“最新触发”来决定权限/目的地。

- 新 owner 仍按现有接纳即时持久入 pending，保留原内容、principal、route、幂等键与 mode；不取消、不提前认领或标 delivered，不把它并入项目资料证明。仅此项目段在 `_execute` 的**工具批后及无工具最终回复后两处**都不追加 `_claim_inputs`；generic/普通 owner 回合保持原合同。
- 没有新输入时，项目段可以读完工具并生成本地最终记录，然后结束该 run；每次中间消息、异常/部分消息及最终消息始终空 delivery_routes。若有 pending 输入，在当前已发出的工具批完整配对后结束项目段，留下固定的本地“复核被新输入中断、尚未完成”状态，不再作项目模型续轮；原工具未开始项沿现有 not_executed，不能省掉配对、取消已完成读取或伪造影响结论。此状态只描述项目复核，不能冒称工具或外部操作已恢复。
- 不新增唤醒器。当前 run 正常结算并释放主会话槽后，复用 `_drive` finally 的 `_wake_main`，由**独立下一 run**认领 pending owner、按其原 route/权限处理。项目段结束不意味着同一主身份、历史或附件被清空；下一真实主人回合可能引用已保存项目资料并沿其自身路由回复，这是主人回合，不是自动项目报告。本片不承诺跨回合数据隔离。
- 正常关闭/重开仍保留原项目消息 marker、本地结果和 owner pending；不得为了隔离删输入或重做已接纳资料。恢复若只剩 owner pending，则按原恢复路径交独立 owner run，不从旧项目摘要重建一个复核任务。未完成工具尾部仍按既有 interrupted/unknown 规则处理，不能重发副作用。

项目证明随 attention receipt/ingress/message 的宿主元数据保存；`file_inbox_list/get` 暴露有限关联。attention 消息内有项目/record定位，后续压缩可沿既有 `conversation_read` 找原 seq/revision，普通 assistant 本地最终记录保存已读/未读/缺失、引用和建议。这是项目资料附记，**不冒称已写入 brief.evidence_refs 或完成任务**。原附件由 brief 当前引用按需 `attachment_read`；不扩大 fs_read 权限、不自动批准 ask。文件路径仍 `current_file_verified=false`，只能称当前读取内容，不能称事件时快照/下载完成。

## 有限成本与三项验收

沿现有候选、记录、批次、窗口模型总量及一次 gap+暂停，不另开额度、AI轮询、目录/团队/历史扫描。正常重启恢复绑定与已提交资料，不因 watcher 代次变化失效；停机、目录换代、到期/溢出不补扫，gap仅本地说明覆盖未知。剪裁或原文丢失明确 unavailable，不承诺永久档案。旧 unknown 出站不因本片新事件、重启或解绑而重排。

1. **真正读到项目依据：** 临时 Linux 目录、临时 SQLite、真实 Runtime/Attention/工具，固定 client 仅编排工具。真实移入 UTF-8文本后，核对实际请求中的当前任务定位、真实 `commitment_read/inspect` 结果、原 `attachment_read` 与新 `fs_read` 结果及本地最终消息。正文不得预注入；外部文件名/正文不能取得配置/派工/写权限。不能以脚本预设“影响判断正确”作评分，只证明证据可达与当前版本一致。
2. **变化不丢提醒：** 用真实锁屏障在构造快照后改要求/epoch，不补新文件事件，验证上述无提交→重消费→当前版本恰一次接纳；连续冲突不自旋，done/cancelled/解绑终止且不堵后续事件。任务暂停只读说明、不自动恢复。工具 ask 未授权时不得读正文。
3. **持续且项目段不外发：** 正常关闭/重开与压缩后原证明/seq/附件可读，重放不新增复核，风暴/gap/目录换代沿原上限。配置真实默认渠道并用 HTTP MockTransport 计数；在项目真实 `fs_read` 已进入、尚未返回的屏障内，通过真实签名手机信封送入 owner 输入。释放读取后先核工具批完整、项目段只留本地中断状态且零项目 outbox/HTTP，再核手机输入没有被吞、被独立新 run 按原 route 恰一次处理，不能把合法 owner 回应误计为项目泄漏。以对应持久 message.completed event 和渠道 cursor 屏障核对两段处理，避免消费者落后假绿；另测无工具最终回复分支也不合并新 owner。旧 v1 reader解码相同 Web-local dump无路由，并用旧拒绝逻辑确认可能推进cursor/整源拒settings的缺口，不宣称返升补收。保留 generic 原路及已有 unknown attempt不增加，不能通过禁用全部渠道掩盖误发。

预期涉及 `file_inbox.py`、`system_events.py`、`attention.py`、Runtime 项目 marker/PreparedRequest与两处输入认领边界及 `server.py` 配置接线，不改 Commitments/Team 授权，不新增附件存储或通用路由协议。实现时补专用真实 Runtime 场景及原文件收件/配置/批次回归，同步 file-inbox 使用/设计文档。真实模型理解、真实账号、用户目录、远端文件系统、跨平台 watcher、降级返升连续性、外发兼容与未知副作用恢复均未验。
