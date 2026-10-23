# 队友附件显式交接设计

状态：方案已批准，生产改动与本片固定离线场景已实现并通过定向验证，待根任务整合收口；本文不代表真实模型、账号、平台或外部送达已经验收。此功能不修改 v2.41 发行历史。

## 目标场景

主人在网页上传资料，要求 MuLi 交给队友处理；离开网页后，从已绑定的移动渠道给同一任务补充一份文件或语音。主统筹明确选择目标队友，交付结构化附件引用；队友在自己的下一轮安全边界获得资料元数据，并按需调用现有读取、视觉或转写工具。队友结果仍沿最初任务来源汇报，移动渠道补充不会偷偷改变原任务目的地。

本片新增的是可靠、显式、可追溯的队友资料交接。既有同一主身份、渠道接收、语音工具、Team 邮箱、权限和原路汇报继续复用。

## 实施前的代码依据

- `src/muli/runtime.py:563` 的 `Runtime.submit` 把网页和渠道输入送入同一持久主会话；`src/muli/channel_application.py:29` 在最终配置锁内复核渠道配置代次。
- `src/muli/team.py:170` 的 `spawn` 创建独立队友会话并冻结父任务 `ReplyOrigin`；`send`（约 279 行）只把文本入目标邮箱。二者都没有 `attachment_ids` 参数，调用 `Conversation.enqueue` 时也没有传附件引用。
- `src/muli/local_services.py:96` 的上下文钩子只看当前会话最近 20 条消息中的 `attachment_ids`，默认仅注入 id/name/mime_type/size。它不读取正文、不爬父链，也不会解析补充文本中的裸 ID 来生成附件目录。
- `src/muli/local_services.py:131` 的 `read_bytes` 通过同一个 `Attachments.get/path` 读取；`attachment_read`、`image_describe`、`audio_transcribe` 都使用它。`audio_transcript_read` 使用 `src/muli/transcripts.py:101` 的既有转写工件读取与 revision/hash/字节预算校验。
- `src/muli/attachments.py:100` 的 get/path 使用实例全局附件库，没有 session/parent ACL。`LocalServices.tools` 中的 `read_scope` 只用于任务说明工具，并未限制附件 ID。任务控制 scope/epoch 和 `Runtime._tool` 的 ask/auto/yolo 权限仍适用于工具调用。
- 因而“队友拿正确 ID 可以按现有权限读到附件”成立；“已经有父链附件授权共享或可靠的结构化交接”不成立。不能把全局可读改称为父链授权，也不能借此新增 ACL。
- 当前仓库未发现 `fork_context` 参数或入口。Team.spawn 的现有行为是独立会话，不继承父历史；本片不顺带添加上下文 fork。

现有证据仍是分段的：`tests/test_application_integrations.py:229` 证明渠道/Web 同主身份但没有 Team/资料交接；`tests/test_report_origin.py:307` 证明派工后换渠道不改原路，但后续输入是新话题；该文件约 373 行的补充用例直接调用 team.send 并只检查绑定持久化；`tests/test_weixin_voice_ingress.py:136` 使用收集 submit 参数的 Harness；`tests/test_local_services.py:43` 是本地附件上下文及直接 handler 读取；`tests/test_attachment_records.py:284` 证明 ASR 工件找回和全文续读，但未连到队友模型请求。

## 最小生产合同

### 1. 给既有派工和补充入口增加同一个可选字段

`Team.spawn`、`Team.send` 增加 `attachment_ids: list[str] | None = None`。模型工具 `spawn_agent`、`send_message`、`send_input` 同步暴露该可选字段；两种补充工具继续共用现有 send handler。`POST /api/team/{identifier}/messages` 的输入模型和调用链同步接受附件引用。网页主聊天和移动渠道入站原本已有该字段，不重建入口或改变信封格式。

不新增专用发附件工具，不增加常驻面板，不改工作流节点 schema。子队友如需进一步派工，沿同一个显式字段传递；不隐式继承整个父会话附件集合。

### 2. 一处校验，有限引用，不读取正文

在现有 `Attachments` 上抽取共享引用校验，复用其 get/path 规则。由 `LocalServices` 的既有组合阶段把当前 Attachments 实例交给 Team 使用，不构造第二附件库，不 mkdir 另一个目录，不引入通用能力注册框架。没有组合附件能力的轻量 Runtime 仍能沿用不带附件的 Team；传非空附件时明确报能力未就绪。

共同规则为：

- 缺省和空列表表示不带附件；上限复用 Conversation 当前每输入 32 个引用，抽取共同常量，避免 API、schema、Team 和 Conversation 数值漂移。
- 严格接受列表及小写 32 位十六进制 ID；不把字符串当序列、不把数字转换成字符串、不把任意路径当 ID。
- 重复 ID 明确拒绝，保留输入顺序，不悄悄去重；schema 使用同一上限、ID pattern 和 `uniqueItems`，直接 Python 调用也执行同一校验。
- 每个 ID 必须在现有附件库中存在且对应文件当前可用；沿既有 path 规则拒绝缺文件、目录和符号链接。校验只查元数据/路径，不读文件正文、不调用 ASR/视觉、不下载、不复制。
- 校验在创建 agent/session 或变更邮箱状态之前全部完成；任何一项失败整次拒绝，不留下空队友、部分引用、部分补充或已接纳回执。
- 最后一次校验到 enqueue 之间不增加 await。文件此后仍可能被删除或改变，实际读取继续执行原有检查；“引用已接纳”不承诺文件永远可用或模型已经读过。

共享校验可以给现有 Web 主聊天入口复用，但不得顺带改变无关上传、配额或工具权限行为。异常对外只给固定原因/附件 ID，不能把宿主绝对路径或外部正文拼入错误。

### 3. 原子保存引用，保持信任与目的地

通过现有 `Conversation.enqueue(attachment_ids=...)` 将完整引用与补充文本放进同一 ingress payload；spawn 的 agent/session 对象仍和首次输入一起提交，send 的状态变更仍使用既有 objects 事务。不得先单独写“附件共享表”再投递文本。

发送者只来自既有工具 caller 或认证宿主，模型参数不能指定 sender/principal/origin。接收方输入继续是 `source=team:<发送者>`、`principal=agent:<发送者>`、internal=True，不能因为上游来自主人上传而改成 owner。文件名、正文、视觉解释和转写都是资料，不是额外授权。

spawn 仍冻结当次 PreparedRequest 的 ReplyOrigin；send 不修改已有 agent.reply_origin。Web 创建的任务最终留在 Web；移动补充的即时确认回该移动输入原路。若确有显式改发要求，继续使用既有发送工具与权限，不在本片添加目的地覆盖字段。已接纳资料不因渠道后来撤销而自动删除；渠道撤销对待接纳输入和出站投递的作用沿既有合同，不能悄悄扩成全任务撤销。

工具参数通过真实 Runtime schema 校验和逐次权限门。新字段不让外部 principal 或 system:attention 得到 write 工具，不跳过任务暂停、维护准入或 ask/auto/yolo。

### 4. 重启、压缩与证据找回

引用存在原始持久消息/ingress 中，队友 resume 或进程重开复用既有身份和邮箱，不重传文件、不重新下载、不自动重做 ASR。历史元数据不等于当前文件可用，缺失或版本变化由现有读取工具明确报错。

实施前压缩已经保留原消息，但存在本片需要闭合的小缺口：`LocalServices.context` 的最近 20 条窗口过后，`HistoryEvidence` 当前只投影 role/source/principal 等元数据，没有 attachment_ids；压缩摘要若漏掉裸 ID，队友无法可靠地按原消息找回交接引用。

本片将现有 `conversation_read` / HistoryEvidence 的目录和单消息证据元数据扩展为可返回 `attachment_ids`，不新增历史查询工具、不加载整条超长原消息、不重建消息索引或另建共享注册表。Store 的证据投影维持先界定 session/seq/页数，再有界读取引用 JSON；引用数量及原信封 shape 必须完整验证，并计入最终 JSON 输出预算。历史引用继续接受原有每项非空且不超过 256 字符的字符串，保留 opaque ID、大小写和重复项的原顺序；只有新 Team 本地交接入口要求严格 32 位小写十六进制 ID、唯一且文件存在，不能追溯收紧旧插件/渠道通用信封。坏元数据或预算不足明确失败，不返回被截成半个 ID 的“成功”数组，不把摘要当原始引用来源。旧消息没有该字段时返回空数组；显式 null、非数组、空 ID、超长项、超数量视为损坏元数据，返回 content_unavailable。SQL 字节投影或最终输出预算不足时返回 output_budget；不把非完整 JSON 当作空数组。当前输出预算若不足以完整容纳某条消息的引用，应返回既有明确 output_budget，不能自动放宽配置；本片不另造附件引用分页协议。

已有压缩摘要与原消息 seq 仍是导航方式，conversation_read 只用于按需找回，不把整个团队或父链资料长期注入每轮上下文。默认上下文保持最近有限元数据的现有行为；未来如新增 fork_context，复制原消息必须连同其既有结构化引用保留，但不属于本片实施范围。

### 5. 兼容与资源

复用现有 ingress/message 的 attachment_ids 字段，不改数据库 DDL、索引、主配置或七领域格式标记。旧版本已经能存取该字段；它没有新 Team 参数但不应因此拒绝已有消息。需静态复核并用已有格式样本覆盖，不能只凭 JSON 看起来相似就宣称 A/B 已验。

一条输入最多沿既有 32 引用；不扫描父会话历史、不下载模型、不常驻监听、不新增轮询。开发只用临时附件、离线 HTTP Mock 和有限 FakeClient；所有验证沿现有 check_local 串行守卫，等待父任务明确授予槽。

## 固定离线验收清单

使用真实 create_app、LocalServices、ChannelApplication、Conversation、Team、Runtime 和工具分派；只替换模型及平台/ASR HTTP，不替换 Runtime.submit、Team.send、附件读取 handler 或报告入库来制造成功。

1. **网页派工确实读到资料。** 经 Web 上传接口保存文本/音频，再用主会话 HTTP 输入；主 FakeClient 调用带 attachment_ids 的 spawn_agent。目标队友真实请求包含附件名/MIME/大小而无正文；随后经 Runtime 工具调用 attachment_read 得到独有标记。音频分支调用一次 mock ASR，关键句位于预览之后，必须经 audio_transcript_read 拿到；图像可用同一机制补一个有界 Mock vision 变体，不要求真实设备或模型。不能只断言传了一个 ID 或工具 schema 存在。
2. **移动补充送到同一任务的正确队友。** 主统筹等待队友时，通过既有签名 webhook 的 owner 输入带第二附件；主请求取得引用后显式 send_input。保持队友原工具批阻塞，证明当前流式/工具不取消、不插入新资料；批完成后下一请求看到新元数据并实际读取。另一队友不会默认收到该消息或元数据，但不能据此声称它被全局附件 ACL 禁止读取。两队友反序完成，结果保持 Web 原路，移动渠道只收到补充确认。
3. **完整调用链与拒绝原子性。** spawn_agent/send_message/send_input 逐个使用真实 JSON schema + Runtime 工具调用；认证 Team HTTP 入口另验一次。覆盖空列表、边界数量、超上限、重复 ID、非列表、错误长度/大小写、数字、未知 ID、缺文件、目录、symlink；失败不增 agent/session/目标 ingress，也不部分投递。不带附件的旧调用保持原行为。工具未 offered、待审批/暂停/维护时仍使用既有语义。
4. **续接不靠摘要记住 ID。** 队友 wake=false 的带附件补充跨 Runtime 重开后仍在原邮箱；resume 保持身份和原路，只消费一次。再让该资料超过最近 20 条并进入真实压缩，摘要故意不含附件 ID；队友按原消息 seq 通过 conversation_read 找回结构化引用，再经原读取工具取正文。覆盖坏引用证据、旧 opaque 引用、损坏超大 JSON 的有界 SQL 投影和小预算明确失败。转写已生成时传递 transcript_id/revision 并续读，不再次调用 ASR。
5. **跨渠道反证。** 同一个移动 event 重投不重复下载/保存、入主人邮箱或给队友追加补充；配置锁等待期间撤销 owner 后没有主人输入、ASR 或 Team 补充；已排队的移动确认若发送结果 unknown，重开不自动重发。原路撤销时不改用默认目的地，不把 Web 派工结果泄到最后一个活跃渠道。

下列已执行场景证明实际信封、模型请求、工具执行、持久状态和路由合同，不能证明自然模型会正确理解任务或稳定选择工具。真实 Telegram/OneBot/微信账号、平台音频编解码、真实网络失联/回连、已配置 ASR/视觉模型质量及外部送达仍明确未验，后续按平台分别验收。

## 预计改动范围

生产：`src/muli/attachments.py`（共享有界引用校验）、`src/muli/local_services.py`（已有 Attachments 组合给 Team）、`src/muli/team.py`（spawn/send/工具 schema及enqueue）、`src/muli/conversation.py`（共用上限，原信封字段复用）、`src/muli/server.py`（TeamMessageInput/认证 Team 发送链；Web引用校验复用）、`src/muli/history_evidence.py` 与 `src/muli/store.py`（仅有界附件引用证据投影，不改持久格式）。如需 Runtime 保存组合期能力引用，只增加明确的一处类型化连接，不造通用注册机制。

测试：优先新增 `tests/test_teammate_attachments.py` 与独立 Runtime 场景文件，复用现有 FakeClient/HTTP Mock；针对性补 `tests/test_team.py`、`tests/test_local_services.py`、`tests/test_history_evidence.py`、`tests/test_compaction.py`、`tests/test_application_integrations.py` 的必要边界，不重复整套已有渠道/语音测试。

文档：README 的简短用法、`docs/agent-continuity-v2.2.md`、`docs/channels.md`、`docs/voice.md`、`docs/history-evidence.md`、`docs/history-archive.md`、`docs/continuity-extensions-plan.md`，以及本片方案/验收记录。若工具使用说明需要提醒显式传引用，可小改 `src/muli/prompts/system.txt`，不能把宿主必须保证的契约只写进提示词。

## 评审结论与实施边界

这是一个可独立交付的小功能片：补齐结构化派工/补充资料与历史找回，证明一条完整跨渠道流程；无需改主身份或路由架构。实施前评审仅确认代码路径和原有测试缺证，实际新增验证另见下节。后续若发现必须更改全局附件可读、加入新 ACL/永久同步表、全量父历史扫描、格式迁移或大模型安装，应回到负责人缩范围，不顺手扩大本片。

## 当前实施与验收记录

新增 `tests/test_teammate_attachment_runtime.py` 使用真实 Runtime、Conversation、Team、LocalServices 和报告持久化路径：网页文本/音频派工、ASR HTTP 屏障期间移动签名 webhook 补充（含重复事件）、明确 send_input 后安全边界读取、转写分页续读与最初 Web 原路报告。另有真实压缩丢弃 ID 后，wake=false 补充跨 Runtime 重开、resume 原身份、conversation_read 按原 seq 找回引用并实际读取两份文件的场景。图像变体经真实 image_describe 与 MediaService/ModelClient，分别核对主模型 vision 和独立 vision 配置的 HTTP 请求，交接前不展开图像。

实际检查：`test_history_evidence.py`、`test_teammate_attachment_runtime.py`（3 个场景、4 个参数用例）、`test_compaction.py` 合计 75 passed；其余历史证据 SQLite/Runtime、归档/API、压缩恢复、Store 回放 77 passed。两次 pytest 均经 30 秒 check_local 串行守卫实际 exit 0；生产两文件 mypy、四文件 Ruff 均 exit 0。峰值内存 181.5 MiB、swap 0。清单中的额外多队友逆序、配置锁撤销及 unknown 出站恢复仍依赖既有专项回归，本片新增完整场景没有宣称这些组合已经重新验证。模型 FakeClient 只固定工具选择，HTTP Mock 只替代平台/ASR/视觉网络，不替换 submit、Team.send、读取 handler 或报告入库。有限 PNG/音频签名样本不证明真实编解码质量；自然模型决策、真实账号与真实外部送达仍未验。

### 损坏 JSON 边界补充

SQLite 的 `json_valid` 不是引用业务 shape 或 Unicode 输出可用性的证明。通过 raw SQL 写入的 `"\ud800"` / `"\udfff"` 孤立 surrogate 转义虽是 SQLite 合法 JSON，解码后的引用却不能严格编码为 UTF-8。本片在完整 shape 校验后逐项核对 UTF-8 可编码性，三种证据入口均返回固定 `content_unavailable`，不会把 surrogate 留给 HTTP 响应或工具结果持久化时报错；正常 opaque ID、重复项及原有 `output_budget` 不变。

[SQLite JSON1 官方兼容说明](https://www.sqlite.org/json1.html#compatibility) 规定超过 1000 层嵌套无效；[Python JSON 说明](https://docs.python.org/3/library/json.html#implementation-limitations) 不承诺同一个固定深度阈值。受 30 秒守卫保护的有限探针在本机 Python 3.13.11 / SQLite 3.50.4 默认 decoder 下：900、990、998、999、1000 层均通过 SQLite 与 Python，1001 层被 SQLite 拒绝，但 Python 仍可解码。没有重现 SQLite 合法深度内默认 decoder 抛 `RecursionError`，因此仅保留防御捕获，不宣称此异常已实测触发，不改递归上限或替换 decoder。raw SQL 900 层非字符串嵌套数组则确实覆盖“SQL 合法、完整有界读取、业务 shape 不合法”的三入口固定错误。

边界补充检查：history evidence、SQLite 和 Runtime 三个文件合计 67 passed，30 秒守卫真实 exit 0；Ruff 通过。新增回归包含两种孤立 surrogate 和一个深层数组；未触发网络、外部模型或真实用户数据。
