# 项目收件后的主动资料复核（实现中，格式迁移前置未交付）

根签名 WIP 前再验：全仓 Ruff 通过；全仓格式首检只发现新 producer 测试未格式化（实际 exit 1），仅格式化该文件后 382 文件通过。收件器、API、运行时质量、系统事件及真实格式 1 拒写/普通来源五文件 **66 passed**（pytest 1.59 秒；守卫 2.403 秒、149.5 MiB、swap 0、exit 0）。该集合与下述 110 项分开记录，不当作全后端或格式 2 验收；九项 producer 正向和六项 Runtime 正向仍需真实格式 2 前置。

2026-10-24。主人明确把现有 Linux 收件目录关联到一个长期任务，新 UTF-8 文本到达后，同一 MuLi 在 Web 主会话核查当前任务要求、原附件和新资料，留下有引用的复核记录并提出下一步。**首片仅 Web 本地，不外发项目报告**，不自动改任务、派工、导入附件或执行建议。自然模型的影响判断质量不由固定 Fake 回答证明。

两位 Sol 交叉方案审查已完成。先将跨渠道项目外发留到独立切片，明确任务变更后的有限重消费；后审发现工具批后认领新主人输入可重算项目路由，增加下述持久项目段与独立后续 run 合同。当前已有未发行的生产候选，但旧 slot 会忽略新宿主 marker，属于实质兼容前置；格式迁移、真实格式 2 上的正向执行和相关验收尚未完成，不能把方案审查或格式 1 拒写写作能力交付。

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

**项目持久语义必须先有真实格式门，不能仅以标准 Web 路由宣称旧 slot 安全。** 旧 `read_record` 拒绝 bound 时，Attention 的 None 分支仍可能推进持久 cursor；旧 `FileInboxSettings(extra=forbid)` 也可能拒绝新项目设置。更关键的是，旧 Runtime 会忽略已入队或已归档的 `project_review`，仍可在工具批后混入手机 owner 并重算路由。仅停新通知、文档标记不支持降级或正常关闭一次，均不能阻止旧 slot 消费这类持久状态。

因此当前候选只接受真实证据中的 `objects=2`、`ingress=2` 及其余现行合同，按已有有界格式 reader 重读并复核 Store 身份，不在项目接纳时改写格式。当前 `CURRENT_FORMAT_STATE` 仍为 1，项目正向必须停在前置；generic 与解绑仍沿旧合同。现有 managed 格式 1 用户的安全更新也是交付范围，不能用全新安装格式 2 替代。最小两阶段 controller/格式升级与提交不明恢复见 [项目格式迁移前置方案](project-format-transition-proposal.md)；该方案尚未实现，项目候选不得发行或激活到真实旧状态，不回滚数据库，也不把旧 reader 拒绝等同已经完成安全迁移。

即使当前配置有默认主动渠道、配置来自手机、最近输入来自手机，项目批次和最终消息也只能本地：不能调用 `FrozenRouting.independent` 选默认目的地，不能与 generic 批次合并或继承最近渠道。既有 generic 文件提醒的原目的地/行为不改。项目跨渠道报告所需独立 proof、旧 slot 拒发、发送前绑定撤销门均**未交付、另片设计**，本片不调整通用 outbox 或 unknown 重试规则。

### 项目执行段跨工具批保持本地，新主人输入另轮处理

最小落点在 `runtime.py::PreparedRequest/_prepare_request/_execute`，项目 marker 由已验证、已持久化的 attention ingress/message 传入，不能从模型正文推断。`Store.claim_inputs` 保留原来源与 routing batch_key，再按完整、有界校验后的规范 marker 分组：generic 与项目分开，project、binding、来源或 task 快照任一不同也不混批；generic 的原路由分组含义不变。这在归档 DML 之前完成，不先消耗 pending 再拒绝。`Runtime._claim_inputs` 返回此次实际归档的有界消息列表，项目定位只检查这批 ID、session/run 归属及 marker/宿主 Web 形状，并防御拒绝混合证明；没有 claim 便不建立项目段，不按会话历史或 JSON run_id 搜索。批量上限仍为当前 `limits.max_pending_runs`，没有新索引、缓存或历史扫描。

初次认领、普通无工具回复后的认领、普通工具批完整配对后的认领三处均从**该次实际 claimed 列表**恢复 marker。普通事项后面取得项目批时，允许同一 run 从下一 `PreparedRequest` 开始进入 Web-local、ask、read-only 的项目段，不另开 run、不增加 peek/队列。项目段一旦建立，跨 `PreparedRequest` 重建始终显式保留限制，不能只重新查询“最新触发”来决定权限或目的地；项目中的新 owner 仍按下述独立下一 run 处理。

- 新 owner 仍按现有接纳即时持久入 pending，保留原内容、principal、route、幂等键与 mode；不取消、不提前认领或标 delivered，不把它并入项目资料证明。仅此项目段在 `_execute` 的**工具批后及无工具最终回复后两处**都不追加 `_claim_inputs`；generic/普通 owner 回合保持原合同。
- 没有新输入时，项目段可以读完工具并生成本地最终记录，然后结束该 run；每次中间消息、异常/部分消息及最终消息始终空 delivery_routes。若有 pending 输入，在当前已发出的工具批完整配对后结束项目段，留下固定的本地“复核被新输入中断、尚未完成”状态，不再作项目模型续轮；原工具未开始项沿现有 not_executed，不能省掉配对、取消已完成读取或伪造影响结论。此状态只描述项目复核，不能冒称工具或外部操作已恢复。
- 不新增唤醒器。当前 run 正常结算并释放主会话槽后，复用 `_drive` finally 的 `_wake_main`，由**独立下一 run**认领 pending owner、按其原 route/权限处理。项目段结束不意味着同一主身份、历史或附件被清空；下一真实主人回合可能引用已保存项目资料并沿其自身路由回复，这是主人回合，不是自动项目报告。本片不承诺跨回合数据隔离。
- 正常关闭/重开仍保留原项目消息 marker、本地结果和 owner pending；不得为了隔离删输入或重做已接纳资料。恢复若只剩 owner pending，则按原恢复路径交独立 owner run，不从旧项目摘要重建一个复核任务。未完成工具尾部仍按既有 interrupted/unknown 规则处理，不能重发副作用。

项目证明随 attention receipt/ingress/message 的宿主元数据保存；`file_inbox_list/get` 暴露有限关联。attention 消息内有项目/record定位，后续压缩可沿既有 `conversation_read` 找原 seq/revision，普通 assistant 本地最终记录保存已读/未读/缺失、引用和建议。这是项目资料附记，**不冒称已写入 brief.evidence_refs 或完成任务**。原附件由 brief 当前引用按需 `attachment_read`；不扩大 fs_read 权限、不自动批准 ask。文件路径仍 `current_file_verified=false`，只能称当前读取内容，不能称事件时快照/下载完成。

### 当前执行段候选与证据边界

宿主 marker 固定包含版本、project/binding、有限有序来源与本次 task 快照，复用现有 Attention 的批次及正文预算；普通 owner/channel 入口不能伪造此字段。`Conversation.enqueue` 的 marker 持久入口独立经过真实格式门，纯 `payload` 构造不写入、不隐式升级。底层 `Store.enqueue_input` 是原有 raw payload 原语，并非宿主来源认证接口。恢复归档先在无 DML、无 await 的边界按原 limit、目的地与规范 marker 分组构造实际候选批；仅批内有 marker 时重读真实格式，通过后才沿原同一事务归档和标 delivered。普通事项在项目之前时可以先独立归档，项目保留 pending；项目在前而格式被拒时，本次四张相关表和后续 pending 均不变。格式 reader 自带只读快照，不能在已有写事务内调用。

根已实际收取两个旧实现红节点：`test_project_review_host_metadata_is_atomic_and_archived` 与 `test_project_review_rejects_channel_destination_before_commit`，均因旧 `Runtime.submit_attention` 不接受 `project_review` 而失败（2 failed，pytest 0.25 秒；守卫 847 毫秒、71.7 MiB、swap 0、exit 1）。这仅证明旧宿主 API 缺失，**没有复现跨工具批混入手机输入**。后续低层格式门缺口另收独立反例，不混算为这两个红的覆盖。

根另实际收取 `tests/test_project_format_gate.py` 中的 Conversation 持久入口与 Store 恢复 claim 缺门反例，均曾 `DID NOT RAISE`。补 Conversation 门后的同次两节点中，Conversation 已绿、Store claim 仍真红（守卫 914 毫秒、67.9 MiB、swap 0）。随后补上述写事务前 claim 门，已纳入下述本轮检查；不能把真实格式 1 拒写反例算成格式 2 正向恢复通过。

根又实际收取 ordinary→project 的真实格式 1 反例：原实现将同 route 两项并为一批，普通事项也被 `project_review_format_required` 阻断（1 failed，pytest 0.21 秒；守卫 748 毫秒、70.5 MiB、swap 0、exit 1）。这促成归档前规范 marker 分批；完整 marker 相同才同批，JSON 字段顺序本身不改变分组。该反例在本轮已转绿，格式 2 多项目正向尚待迁移前置。

根此前统一守卫实测三个真实格式门节点及不需要格式 2 的核心小项，24 passed、6 deselected（pytest 0.53 秒；守卫 1.170 秒、85.6 MiB、swap 0、exit 0）。C 独立源码后审未见剩余 P1/P2。Ruff 首检实际 exit 1：Attention 的 revision 与测试 expected_tool 两处循环闭包未绑定，以及 Runtime import 顺序；已作最小绑定/排序修正。根随后格式化九个相关文件（八个改变格式、一个保持），Ruff 九文件通过；七测试文件实收 **110 passed、6 deselected**（pytest 1.63 秒；守卫 2.306 秒、127.7 MiB、swap 0、exit 0）。110 已包含前述 24 项核心和 86 项普通回归，不能再次累计 24。全 148 个生产源文件 strict/no-incremental 实际 exit 0（守卫 8.365 秒、436.1 MiB、swap 0）。六项正向仍受真实格式 1 前置阻断，不计入通过，尚未验证手机屏障、项目段恢复或零项目外发；本轮仅是未发行 WIP 检查点，不开放格式 2。

`tests/test_project_attention_runtime.py` 新增纯 shape/复制与预算、规范分组及不同宿主事实负控制、未证明旧库无部分写入、普通 owner 不可伪造、错误路由先拒绝及无 claim 普通运行不承接历史 marker 的小项。历史 marker 的损坏/外来恢复夹具只测本轮选择边界，不冒称一次真实项目接纳。正向宿主原子归档、正常 close 后同进程重开和四项手机输入场景均保留真实格式门，不 mock gate、不手写格式 2 标记。当前格式 1 下它们会先被拒绝，尚未进入读取或无工具回复屏障，不能称已验证段隔离、重开恢复或项目零外发。源码核对显示 `_recover` 仅结算旧 run；`_wake_main` 认领实际 pending 启动新 run，Team resume 也启动新 generation/run，不重执行旧 delivered run；待真实格式 2 验收时仍须核对同库原输入与独立新 run。

手机屏障用原生临时目录来源、真实任务工具和配置入口产生项目证明；读取分支沿实际工具结果中的定位、真实 ask 审批和真实 `fs_read`，在读取已完成而结果尚未交回 Runtime 时接入真实签名 OneBot owner 输入。无工具分支在实际模型请求内接入同样输入。断言基于持久 user 的 run 归属、消息 `seq` 区间、工具完整配对、原 ingress payload 不变、独立下一 run 及最终 `message.completed` 已被渠道 cursor 消费；assistant/tool 消息不被假定自动带 `run_id`。HTTP MockTransport 只计数替身外部传输，不替换渠道管理器、签名解析或 Runtime composition。固定模型只编排工具，不用最后一句判断资料分析质量。

原直接项目参数 `tool-batch`、`no-tools` 保留，另列 `generic-tool-batch`、`generic-no-tools`。新参数经真实 settings 入口暂时清空 main model，持久接纳但不启动模型；在 Attention 已选定真实项目事实、通过背压检查而尚未调用宿主提交时，受控包装先调用原 `Runtime.submit_attention` 接纳普通 Web 本地事项，再以原参数调用同一原入口提交项目。项目原 guard、来源证明及格式门均实际运行，不构造替代 marker；恢复原方法和模型配置后，从真实 pending 开始执行。普通工具批因项目已 pending，未开始的只读调用须完整配对为 `not_executed`，不声称普通读取发生；后续项目请求才核真实 ask 读取与本地路由。两新参数核普通事项与项目属于同一 run 的前后执行段，签名手机输入则保留给独立下一 run；配置/方法在失败清理时也恢复。

## 有限成本与三项验收

沿现有候选、记录、批次、窗口模型总量及一次 gap+暂停，不另开额度、AI轮询、目录/团队/历史扫描。正常重启恢复绑定与已提交资料，不因 watcher 代次变化失效；停机、目录换代、到期/溢出不补扫，gap仅本地说明覆盖未知。剪裁或原文丢失明确 unavailable，不承诺永久档案。旧 unknown 出站不因本片新事件、重启或解绑而重排。

1. **真正读到项目依据：** 临时 Linux 目录、临时 SQLite、真实 Runtime/Attention/工具，固定 client 仅编排工具。真实移入 UTF-8文本后，核对实际请求中的当前任务定位、真实 `commitment_read/inspect` 结果、原 `attachment_read` 与新 `fs_read` 结果及本地最终消息。正文不得预注入；外部文件名/正文不能取得配置/派工/写权限。不能以脚本预设“影响判断正确”作评分，只证明证据可达与当前版本一致。
2. **变化不丢提醒：** 用真实锁屏障在构造快照后改要求/epoch，不补新文件事件，验证上述无提交→重消费→当前版本恰一次接纳；连续冲突不自旋，done/cancelled/解绑终止且不堵后续事件。任务暂停只读说明、不自动恢复。工具 ask 未授权时不得读正文。
3. **持续且项目段不外发：** 正常关闭/重开与压缩后原证明/seq/附件可读，重放不新增复核，风暴/gap/目录换代沿原上限。配置真实默认渠道并用 HTTP MockTransport 计数；在项目真实 `fs_read` 已进入、尚未返回的屏障内，通过真实签名手机信封送入 owner 输入。释放读取后先核工具批完整、项目段只留本地中断状态且零项目 outbox/HTTP，再核手机输入没有被吞、被独立新 run 按原 route 恰一次处理，不能把合法 owner 回应误计为项目泄漏。以对应持久 message.completed event 和渠道 cursor 屏障核对两段处理，避免消费者落后假绿；另测无工具最终回复分支也不合并新 owner。旧 slot 必须由真实格式接纳和维护事务拒绝不兼容数据库，托管格式 1 升级须具备可用的格式 2 candidate/fallback 与提交不明恢复，不能以旧 Web-local dump 解码无路由代替。保留 generic 原路及已有 unknown attempt 不增加，不能通过禁用全部渠道掩盖误发。

预期涉及 `file_inbox.py`、`system_events.py`、`attention.py`、Runtime 项目 marker/PreparedRequest与三处输入认领点、Conversation/Store 正规持久接缝及 `server.py` 配置接线，不改 Commitments/Team 授权，不新增附件存储或通用路由协议。实现时补专用真实 Runtime 场景及原文件收件/配置/批次回归，同步 file-inbox 使用/设计文档。真实模型理解、真实账号、用户目录、远端文件系统、跨平台 watcher、降级返升连续性、外发兼容与未知副作用恢复均未验。
