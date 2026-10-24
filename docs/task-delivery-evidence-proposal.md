# 显式登记带证据的任务交付验收单

2026-10-24，基线 `17f50d7`。2.48.0 未发行候选的发布证明、显式验收单 API、原查询扩展及有界目录已完成本地整合和独立后审，结果见末节及[候选审查](review-v2.48.md)。远端精确候选 CI 与发行核对仍待完成，不据本地结果声称发行成功。

历史首红：1 failed，pytest 0.75 秒，守卫 1.482 秒、97.9 MiB、swap 0、exit 1，准确失败于缺少 `task_file_publication`，此前实际写入、复制、读取、完成、重开与下载均已走完。修复后由根实收 7 个发布证明旅程通过（pytest 1.25 秒，守卫 2.097 秒、125.8 MiB、swap 0）；另 4 个相关文件 56 项通过（pytest 1.47 秒，守卫 2.156 秒、115.2 MiB、swap 0），两组不重叠，共 63 项。148 源文件 strict 通过（8.673 秒、441.5 MiB）；Ruff 通过，格式首检 3 文件需排版，根仅格式化后全仓 Ruff 与 383 文件格式复核通过。A 非作者独审三生产文件与旅程，未发现实际 P1/P2。

用户可以要求：“报告做好后，核查实际文件，留下逐条验收依据；重开后还能查为什么说完成。”首片限主 MuLi 的一个直接队友、一代准确完成报告和一个本地 UTF-8 文本附件副本。验收单是显式辅助资料，不改变 `agent/run.completed` 或 `commitment_save(status=done)` 的既有含义，不修改任务状态、计划批准、暂停或授权，不自动发送文件。

## 现有接点与准确缺口

- `LocalServices.tools.publish` 经现有文件根目录校验，调用 `Attachments.import_file/save`，生成独立附件副本并实际计算 `size/sha256`。现有附件的 `source=local`、模型返回的路径或摘要，均不证明它由本轮任务发布。
- `Runtime._tool_execution` 已保存真实 `(session_id, run_id)`，`request_context` 提供真实 `TaskSnapshot`；模型不能通过参数选择调用身份。原工具结果只有 `tool_call_id`，本片补足准确执行与发布关联；不能按同正文、可重复的调用 ID、最近任务或最近报告补猜。
- `Team._finish` 把终态、`team.finished` 和准确报告一起提交。事件已有 `run_id/generation/completion_revision`，可与真实直接父子关系、任务范围及完成运行交叉核验。原 `commitment_inspect` 提供实时资料，本片只新增按需读取不可变验收单的段。

## 专用于 file_publish 的宿主证明

保持原 `file_publish(path)` 参数和普通返回行为。只为真实绑定任务的直接队友新增可选 `task_file_publication` 对象，以实际附件 ID 为主键，版本为 1：

1. 可信 handler 从 `_tool_execution`、实际 `request_context.execution_scope` 和准确 agent 取得会话、运行、任务 revision/epoch、generation；不从工具参数、正文或“选中任务”获得。实际导入返回后复核运行仍活跃、任务快照仍有效且父会话仍为主 MuLi。跨 await 发生要求变化时，已复制附件可以保留，但不得完成证明。
2. handler 只登记由本次真实导入得到的附件 ID、规范路径、实际 size/sha256 和上述来源。此时证明尚不可用于验收；不把 `attachment.published` 事件当作最终工具回执。
3. Runtime 保存该次真实结果时，使用本轮已保存的准确 assistant 调用消息、实际 call 和新 tool 结果消息，补齐 `assistant_message_id/call_id/tool_message_id`。仅当完整结果 `content_provenance=accepted`、无 error/unknown/not_executed、附件与宿主登记一致，才在同一 `Store.put_many` 事务保存结果消息与完成证明。仅处理 `file_publish`，不建立通用工具 ledger。失败、截断、取消或丢失结果的前缀不能领到完成证明。

实现按准确主键、有界读取 pending 证明、持久 assistant 调用记录和附件元数据；完整实际结果正文须与本轮序列化结果一致，并重新核对原调用名与参数。证明元数据沿现有工具输出预算；坏 JSON、超预算或多义字段不完成证明，也不替换普通工具结果。不读取整段历史，不为证明重扫附件大文件，不领养已有证明。结果消息与 `recorded` 写入任一失败时共同回滚；已复制附件和此前 pending 记录可保留，但不是成功工具回执或验收单。

可选证明还受原始持久对象的字节门约束：当前 agent/run/task 均复用 `Store.bounded_object`，在字段跨入 Python 前限制为 `max_output_chars × 4` 字节；损坏或超预算只使可选证明不可用，不吞掉真实存储异常。Linux 上合法但不能严格编码为 UTF-8 的祖先路径保持普通复制能力，不登记该可选证明；这不宣称所有外围 surrogate ToolCall 参数都已受支持。

完成证明 `state=recorded`，固定字段包括 `version/commitment_id/task_revision/epoch/agent_id/run_id/generation/completion_revision/attachment_id/source_path/size/sha256` 及三个准确工具定位字段。此处 `completion_revision` 是发布执行的预期终态修订，须在登记验收单时由准确 `team.finished` 再证实，不把发布成功当作队友已经完成。

读取证明时按主键回查上述原记录、真实调用名/参数和完整结果，不只看 `source=local` 或调用 ID。旧附件、直接调用 handler、外来上传和旧结果没有证明时保持原功能，不补写、领养或迁移。

宿主只证明“这个执行在该任务快照下实际复制了这些字节”。即使路径和摘要存在，也不能据此断言文件由该代新写出、内容属于该任务或满足要求。本片不新增 `fs_write` 来源链；这些结论一律归模型判断。原文件后来改变不改变已复制附件；附件副本缺失、非普通文件、摘要变化或不可完整读取时，不得登记成功文件核验。

## 显式 API 候选与原子合同

新增主会话协调工具 `task_delivery_record`，参数为 `identifier/observed_revision/observed_epoch/report_id/attachment_id/idempotency_key/judgments`。`judgments.goal` 为 `{status, reason}`；`judgments.constraints` 为 `{index, status, reason}` 列表，逐项引用冻结 brief 的 constraint 索引，每项恰好出现一次。`status` 为 `satisfied/unsatisfied/unverified`，说明受现有预算约束；不允许模型传入宿主文件事实、调用身份、来源路由或执行代次。

工具风险仍为 `write`，只用独立的 `delivery_tools` provider 注册为协调工具，按真实会话隐藏于队友 schema，handler 再核当前主会话、真实运行和写入门禁；原 `LocalServices.tools` provider 标志及其它工具边界不变。参数精确限定为上述七项，限制判断按索引规范化，去重不依赖 JSON 字段顺序。

handler 必须再次验证真实主会话、当前工具执行、当前任务 revision/epoch、准确直接队友及任务范围、`report_id == completed run_id`、准确 `team.finished` 的 generation/completion_revision，以及完成的 publication 证明属于同一执行。其它任务、旧代、孙代、缺失/多义或损坏证明均拒绝；不查 latest 补救。准确完成事件先过 SQL 唯一性及字节预算门，再读取 JSON，不扫描全部历史。附件仅在 `min(limits.max_file_bytes, max_output_chars × 4)` 的有限字节窗内完整读取并核 UTF-8/size/sha256；超出直接拒登记，不整份扫描大文件，不执行文件内容。

单中分开保存：

- `host_facts`：宿主实际验证的附件 ID、size/sha256、UTF-8 可读性、准确发布与报告读取位置；不包含“内容正确”或“主人已验收”。
- `model_judgments`：模型逐条判断，明确其性质；缺证据或未检查项写 `unverified`，不能变成宿主通过结论。
- 冻结的任务 revision/epoch、完整 brief 要求、准确报告执行身份和记录时刻。它不扩充授权，也不把记忆候选当成本轮执行证据。

文件读取等 await 全部在写事务外。完成后同步重新核对当前身份、任务和证据，再在一个无 await 的 SQLite 事务中保存验收单、同任务去重凭据及 `task.delivery_recorded` 事件；失败全部回滚。去重键相同且规范参数相同返回原单，内容不同报冲突；重复调用不重写来源、不发布新附件，也不重跑队友。保存不修改 task.status。

查询扩展既有 `commitment_inspect` 的 `deliveries` 段与既有认证 inspection API，沿用目录/正文分页、scope 与预算；不加前端面板。单本身不可变，工具登记返回准确单 ID，正文保存 `brief/host_facts/model_judgments` 等冻结资料；目录计算 `current`，只有当前任务 revision/epoch 和准确执行仍匹配才为 true。它表示任务与执行版本仍当前，文件字节事实明确属于登记时的核验，普通查询不暗称重新读过文件。要求变化后旧单仍可读，但必须明确为历史，不能冒充当前验收。普通进度更新造成 revision 变化也保守标历史，不按摘要近似恢复为当前。

新 `task_delivery` 类型仅存在于本片未发行候选。其 ID 由共享 `delivery_prefix` 把准确任务 ID 严格编码为 UTF-8，取完整 SHA-256 十六进制摘要加 `:`，再拼原 `new_id()` 随机后缀，共 97 个 ASCII 字符；不截短摘要，不赋予用户可解释的内容含义。客户端应原样回传 ID 和游标，不能假定它是附件 ID 或 32 字符 UUID。目录复用现有 `(kind,id)` 主键，按该前缀闭/开范围和原 after 位置做 keyset；每条仍按有界完整对象核对真实 `commitment_id`，前缀本身不代替范围或权限证据。不新增 Store schema、索引或七格式变更，不全扫旧 kind 补猜或迁移尚未发行的旧候选数据。

最终说明沿当前真实 owner 来源及既有 2.47 完成回报规则；工具不新增外发。`file_publish` 仅产生本地附件下载入口，真正发文件仍需显式渠道工具并重新授权。撤销仍 blocked，unknown 不重发；不因验收单产生新投递或复位原回执。

## 正式 Runtime 旅程与先行红点

`tests/test_task_delivery_evidence.py` 的先行节点只使用已经存在的 Web ASGI 入站、真实 Runtime/Team、临时 Store、`commitment_save → spawn_agent → fs_write → file_publish` 和实际报告投递。模型为固定工具策略，应用使用默认工厂生命周期；先核完整工具结果、真实附件字节、真实完成运行/事件/报告及原文件后续变化不影响副本，再断言准确 publication 证明存在。预期红点是“真实复制已完成，但没有宿主执行关联”，不能是缺少新工具、导入错误或超时。此项只验证首个必要接缝，不冒称整个验收功能旅程。

发布证明已通过的六个受控前缀：实际复制返回前经真实 owner API 暂停、pending 后暂停、复制后取消、实际 handler 结果被标为 unknown、真实 Runtime 输出裁剪、以及 `recorded` 与工具消息共同提交时注入写入失败。均保留真实复制事实，只断言不存在完成证明；提交失败还须验证仅留下真实修尾的 unknown 消息，不能残留 accepted 结果。正向旅程另以实际记录逐项改变 assistant ID、调用参数、调用名和结果正文，交给同一 finalizer 拒绝；不伪造消息历史、报告或批准。unknown 和裁剪是复制后的受控结果故障，不宣称正常 `file_publish` 自然返回这些状态。

`test_completed_delivery_is_explicitly_recorded_and_inspected_after_reopen` 使用真实主/队友运行与相同生命周期，在准确完成报告和实际附件读取之后，主助手实际调用 `task_delivery_record`。缺工具时保存真实工具错误并让主运行正常结束，所有前置事实核对之后才由外层断言报功能缺口，不以超时或缺导入作红。候选还走原 `commitment_inspect(section=deliveries)` 读取分栏资料，正常重开后先验证未登录请求 401，再经真实登录和原 inspection HTTP 路径读回同一单；普通任务仍 open，文件零出站。

该节点首跑为历史夹具失败：1 failed，pytest 4.61 秒，守卫 5.266 秒、109.9 MiB、swap 0，等最终回复超时，尚未到验收缺口，不能算功能目标红。静态定位主助手登记调用把参数 `identifier` 与旧 `call(identifier, name, **arguments)` 的首参重名；仅这处及后续同形 inspect 调用改用直接 `ToolCall + json.dumps`。原 4 秒等待未增加，失败时仅补真实 main run 状态/错误、前 4 个持久错误帧及最后工具结果的 1000 字节窗口，不展开历史图。

修正后根已实收准确功能红：1 failed，pytest 0.58 秒，守卫 1.278 秒、110.1 MiB、swap 0、exit 1，准确失败于登记结果缺少 `id`，`tool_available=False`，真实结果为 `{"error":"工具未启用"}`；任务完成、附件完整读取及持久 publication 证明均已成立。随后才授权本次 API 实现。

独立边界负控另实收 3 failed：pytest 0.41 秒，守卫 0.992 秒、95.6 MiB、swap 0、exit 1。合法 surrogateescape 祖先路径的真实附件复制已经成功，可选证明却因 SQLite UTF-8 绑定失败使普通发布失败；另两项分别观测到百万字符 `agent.status` 与 `commitment.revision` 在原投影中先进入 Python。候选仅收紧可选证明的 UTF-8 路径与原始对象字节门，不扩大外层路径支持声明。根已实收这三项修后通过：pytest 0.41 秒，守卫 1.039 秒、95.1 MiB、swap 0、exit 0；不替代 API 旅程验收。

API 候选旅程为 `stable/owner_pause` 两参数。前者正常重开后通过真实主工具调用验证同键准确返回原单、不同判断冲突；再经真实 owner API 暂停，旧页拒绝、新目录与正文均为 `current=false`，冻结正文保留，历史同键仍原样返回。验收单、回执和事件各只有一份，重复调用不得重新读取附件。后者在实际有限附件读取的 await 后提交 owner 暂停，要求末事务复核拒绝登记，单、回执与事件均为零；已存在的真实附件、publication 与完成报告保留。根已实收本文件 9 项旅程与三项 publication 边界负控共 12 passed：pytest 1.72 秒，守卫 2.635 秒、200.5 MiB、swap 0、exit 0。这与前述历史 63 项有重叠，不叠加累计。

独立 reader 损坏负控随后实收 2 failed：pytest 0.18 秒，守卫 0.703 秒、73.6 MiB、swap 0、exit 1。只破坏临时 Store 中读夹具的 SQL 主键，一项观测到 1 MiB 文本主键跨入 Python，另一项在 fetch 时因坏 UTF-8 抛 SQLite `OperationalError`；这不是登记或真实任务证据旅程。窄修在原 SQL limit 内先按主键原始字节门返回 BLOB，再严格 UTF-8 解码；坏行仍消耗所选窗口，不跳过补取，统一拒绝为 `content_unavailable`，没有改写损坏数据。

随后根实收该 reader 文件 3 项中两项主键负控通过、一项访问量目标红：pytest 0.15 秒，守卫 0.669 秒、71.5 MiB、swap 0、exit 1。相同空任务目录的 SQLite VM 计数从基线 90 变为加入 64 条其它任务小记录后的 463，越过原 244 oracle，证实全 kind JSON 过滤使访问量受无关任务影响；这不是大数据压测或耗时阈值。任务范围 ID 与范围 keyset 窄修后，根实收三文件合计 15 passed：pytest 1.73 秒，守卫 2.665 秒、202.8 MiB、swap 0、exit 0，包含原字节门、坏行、访问量 oracle 及 97 字符真实登记→查询→正常重开链；原阈值未增加。C 对这两生产窄修独立后审未发现新的 must-fix。这与上述历史小集有重叠，不叠加累计。

同一正式 API 旅程现另加 `changed_copy/receipt_commit` 两个参数，生产不改：前者在真实写入、复制、报告完成与主助手初读之后，只把临时附件副本的一个字节改成另一段有效 UTF-8，长度、目录元数据、publication 与报告不变，再让实际有限读取核验，因摘要不符拒绝登记。后者仅在真实 `task_delivery_receipt` 的 `_put` 上抛一次受控错误，故障前已在同一真实事务中读到待提交的验收单，故障后单、去重回执与事件均未残留；原附件、publication 与完成报告保留。两项不伪造工具历史、来源或批准，不增加预算和期限。根按新增 selector 实收 2 passed、9 deselected，pytest 0.82 秒，守卫 1.603 秒、140.9 MiB、swap 0、exit 0；与前述 15 项不重叠。

验证范围分为三组：

1. 上述真实发布后，主助手读取实际附件，显式登记单；准确文件事实与模型判断分栏，普通任务状态不变，正常关闭重开后经原认证 inspection 入口读回同一单。
2. 已有 publication 单字段定位负控、六失败前缀与本轮核验期间 owner 暂停分别验证真实结果及版本边界；旧任务、旧代、孙代和其它损坏证明的完整 API 矩阵没有据此声称全部实测，仍须按审查发现补必要回归。已留附件不充当成功验收。
3. 渠道仍复用既有 2.47 OneBot/MockTransport 回报来源、撤销与 unknown 不重发合同，本片工具不新增任何投递。当前新旅程明确核零出站，没有重新宣称验收单与所有真实平台发送组合均已联调。

全程沿原 16000 上下文、6000 输出、3 个 pending slot 和原期限；不增加模型分类轮次、后台批、轮询或任意验证器。固定策略只证明证据、持久化和身份合同；自然模型的要求判断、真实平台送达仍需另行验收。所有检查由根统一执行。

## 候选本地整合闭合

除上述互不重叠的 17 项新回归外，根实收既有任务核查、认证 HTTP API、Runtime、LocalServices、队友附件与任务控制六文件 93 passed（pytest 2.60 秒，守卫 3.543 秒、205.3 MiB、swap 0），发行元数据及兼容声明两文件 166 passed（pytest 0.95 秒，守卫 1.452 秒、68 MiB、swap 0）。共 276 个不同测试节点，不累计此前重叠历史小集。

实际 149 个源码文件 strict 通过（守卫 8.603 秒、416 MiB、swap 0）；全仓 Ruff、386 文件格式与离线锁文件核验通过。2.48.0 四处元数据同步后，离线 wheel/sdist 构建通过（守卫 1.413 秒、105.8 MiB、swap 0），发行一致性核验及隔离离线安装、CLI/本地 ASGI 冒烟通过。非作者后审未留实际 P1/P2。没有下载库或模型、提高上限、跳过失败，亦未触及真实账户、数据或桌面；远端 CI 和最终发行状态待另行记录。
