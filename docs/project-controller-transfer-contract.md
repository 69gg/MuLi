# 阶段一 bridge：固定 controller 换代的最小合同（待审、未实现）

2026-10-24。本文细化 [格式转换方案](project-format-transition-proposal.md) 的阶段一，仅新增严格动作 `project_controller_transfer`。数据库完整格式始终为 1，项目功能保持关闭；不实现 `project_review_1_to_2`、不改数据库 marker，不扩大普通 V1/V2 或 `assess_compatibility` 的接受集合。本文为实现合同与分片验证记录；strict model/action reader 和旧消费者临时拒绝保护已通过下述回归及全源 strict 检查，尚未实现可发布的 transfer 或验证真实 systemd 换代。

实现树由根指定为 `/home/pyl/.codex/worktrees/muli-task-control/MuLi`，基线 `77af672`；本文件是方案树已审合同的实施副本。所有检查由根唯一守卫执行；本 agent 未执行检查、Git、网络或真实进程/服务效果，不能把可解析的新动作当作已有可恢复换代能力。

## 1. 固定输入与两种权限

前置必须是：实际运行应用已经通过普通 same-format switch 成为支持本动作的 bridge；旧 journal 已 resolved 且已精确投影，当前 permit、boot、配置、selector、unit、真实 slot、Store/main 身份及完整格式 1 都重新验证。旧固定 controller 可以仍是早期版本，这正是本动作要解决的问题。第一次 adopt、legacy/V1 origin、未投影旧代及未释放的旧事务均拒绝。

首片只将**真实当前正在执行新 prepare 实现的 bridge 安装**提升为新固定 controller：`origin` 是该真实活进程及旧 anchor 的冻结证据；base/target 都是在新 anchor 下重启这同一 bridge 安装的角色。请求不接受任意新 slot/candidate 路径，不执行候选或新增 capability 协议。现有 `CompatibilityManifest`/`SlotIdentity` 的 protocol/format/bytes 不能证明任意 retained 安装认识此 action，因此不授权这种输入。

固定 `new_anchor.controller_slot == base.installation == target.installation == origin.binding.installation == source.slot.digest`，且 `base == target`；target/base 的 boot 与 nonce 仍独立。本片回退仅是新 anchor 下对已知 bridge 的一次独立恢复启动，不宣称有另一安装副本；同一安装损坏时只能 fail closed。两角色都用新 anchor 的 selector 与重新渲染的 unit，不能把 origin 的旧 selector/unit 当成 base。旧 anchor.controller_slot 必须与此实际 bridge slot 不同，否则在发布前拒绝。

能力与身份的实据分开：本次受信发行代码包含新 prepare、reader/host/boot/维护实现及其正式入口离线验收；运行时正在执行的就是该 prepare。复用 `ManagedPreparation._source` 对实际前代/permit 的选槽，再由 `verify_current_slot` 验完整文件、`sys.executable`/`sys.prefix`/`muli.__file__`（`managed_deployment.py:265–277`），`_observe_process` 核真实 loaded/proc role、`pid == os.getpid()`、`InvocationID == permit.boot`（`managed_preparation.py:378–402`），发布前 `validate` 再验 current slot（:506）。这条链只授权当前已核实现，不从 manifest 声明推导代码能力，也不把“Python 指向同一个 resolved executable”当成同安装证明。发行测试不能由调用方 DTO 或伪造能力字段替代。

运行准备端仍用旧 permit 持有的数据库 writer lease 写本代计划；helper 只凭唯一 controller lock 推进。origin 关闭并完全释放 writer 后，helper 才取得同一个数据库的 writer lease实施文件效果，随后交由实际新 boot 的 permit 独占；不能另建 journal、临时“当前控制者”文件、第二把数据库 lease，或同时让 helper 与新进程占有 writer。

## 2. 精确 typed 记录

在 `managed_transaction.py` 增加独立 `ProjectControllerTransferRecord`，沿用 `StrictRecord` 的 frozen/strict/extra-forbid、重复 JSON 键及非有限数拒绝。**不把新 action 添到旧 `ManagedAction` 后让 `TransactionRecordV2.consistent` 顺带接纳。** 增加有界 action 分派入口，例如 `parse_managed_record`，返回显式 `TransactionRecordV2 | ProjectControllerTransferRecord`；旧 `parse_record_v2` 保持原合同，只认原四动作。未知 action、拼写变体、嵌套额外字段与错误类型都拒绝。

新记录的完整顶层字段如下；除新增 `origin` 和固定 action 外均复用现有类型与命名，不增加通用版本协商：

| 字段 | 类型与固定含义 |
| --- | --- |
| `protocol` | 精确整数 `Literal[2]`，拒绝 bool/float/string |
| `action` | `Literal['project_controller_transfer']` |
| `request_id`, `generation`, `revision` | 原 `Identifier`、`Identifier`、严格正整数；原 `Revision` CAS |
| `identity` | 原 `ServiceIdentityV2`；`deployment` 从首次发布就等于新 anchor digest，`origin_boot` 是真实旧进程 boot，Store/main 原样 |
| `origin` | 下述独立 `TransferOrigin` |
| `base`, `target` | 原 `RoleBinding`，均 `entry='managed'`，selector 非空且规范；仅代表新 anchor 下两个可启动角色 |
| `observed_format` | 原 `FormatState`，本片限定 metadata 1、七域完整 1；全程冻结 |
| `deadline` | 原 `Deadline`；按已冻结原 helper 限额创建一次，重试/recover 不刷新 |
| `phase`, `intent` | 原 `Phase`/`Intent` 的下面动作专用子集；保留 launcher 已认识的拼写 |
| `challenge`, `rollback_challenge`, `rollback_attempted` | 原 Identifier/可空 Identifier/bool；回退首次生成独立 nonce，此后不更换 |
| `health`, `activation`, `code`, `failure_code` | 原 `HealthProofV2`、`Activation`、`Code`；首因保留，异常正文不进入记录 |

`TransferOrigin` 只有三个字段：`deployment: Digest`（真实旧 anchor）、`binding: RoleBinding`（旧 anchor 下当前实际 managed 安装/selector/unit/config）、`projected: ProjectionReceipt`（已核旧代精确投影）。旧 boot 只在 `identity.origin_boot` 存一份。该对象不是第三个 `Role`，没有 origin selector 路由、新 boot 或 rollback nonce。旧 anchor 自身已经内容寻址并保存旧 controller，不再冗余冻结可漂移的 controller 路径字符串。

纯模型与宿主验证分工必须明确：模型检查字段形状、阶段/nonce/health 一致性、全部 configuration 引用相等、base/target binding 相等且安装复用 origin、正整数/CAS 与前后不可变字段；宿主从本代快照和不可变 anchor/slot 重新验证 selector 语义、unit render、路径、文件身份、完整格式及上述当前实现链。`origin.projected` 必须在 prepare 时等于实际前代位置与 Store 投影，origin 的 boot/安装必须来自 fresh 观察；它不是调用方可提交的自签证明。完成后不再依赖前代 state.json 仍存在。

新旧 anchor 只允许 `controller_slot`、由新 controller 渲染得出的 `managed_unit` 不同；config/data/db/unit/journal 路径、launcher 完整指纹、helper/journal/package 限制都相同。origin selector 指向旧 anchor/实际 origin 安装；base/target selector 分别指向新 anchor/其真实安装；base/target unit 都必须等于新 anchor 的 `managed_unit` 和 `ManagedUserService(..., controller=new_controller).render()` 的完整 bytes。三份配置必须为相同原始 bytes，不能仅语义相等。

boot role 只允许：`activating/start_target`、`verifying`、`committed` 为 target；`rolling_back/start_base|verify_base`、`rolled_back` 为 base。其余为空。**本动作禁止 `aborted`**，因为不变 launcher 将 aborted 路由为 base，而这时磁盘可能仍属 origin。terminal 只接受本代新 anchor 下非 origin boot 的强 paused/released 健康证明，原 proof 的 generation、角色 nonce、slot/compat/config/selector/unit、格式摘要和主身份检查全部保留。`origin` 不得满足成功、恢复启动或最终投影的角色证明。

## 3. 发布、snapshot、GC 与固定预算

顺序固定：configuration lock 下真实准入与材料预检 → 唯一 controller lock 下再次核前代/投影/permit → 有界发布新 slot/anchor → 冻结本代三个快照组 → **最后持久发布 state.json** → 交新 controller helper。保持现有准备端锁顺序，不在持 controller lock 时等 maintenance HTTP 回调取得同锁。一次新 anchor 发布本身不撤销旧权威；state.json 的耐久替换才是交接边界。

沿用 `RoleSnapshots` 验证与原子 `_replace`。本代文件固定为每组 configuration.json、selector.json、unit.bin，共 origin/base/target **9 个**，均在当前 generation 下。扩展有限 snapshot 命名识别支持固定 origin 组，不接受任意 role/path。不得只引用前代 base/target 文件；新代发布后旧代可以按原 GC 规则回收，不能破坏本代 origin 认证或回退。

预检同时满足：每个实际 snapshot `<= max_snapshot_bytes`；完整新记录规范编码 `<= max_record_bytes`；DeploymentStore 私有对象/slot package/launcher 的原预算；目录原 `max_entries`。目录计算复用 `_collect`：保留前代全部文件与非可回收项，计入所有新增 slot/anchor、9 个本代 snapshots、state.json、锁及实际现存条目，并再留 **1 个 atomic replace 临时条目**。不能只算快照或忽略旧/新 anchor。先求完整 incoming/retained 集合是否能放下，再写新材料；不提升 limits，不靠先删前代补容量。

GC 只处理原受限 generation snapshot/tmp 命名及已证明不再引用的旧代，读取与数量上限不变；未知条目计入额度且不删。旧 anchor/slot 本片不新增自动删除，尤其不能删 origin 所引用的材料。预算不足在 state 发布前拒绝；部分孤立新材料只占空间、不授权，重试须逐项确认 exact bytes，不能悄悄覆盖或“修复”既有证据。目录 fsync/rename 结果不明沿现有 poisoned/control 重取与精确确认处理，不伪造成功 receipt。

## 4. 新 controller 推进与所有可恢复前缀

共享 `_Controller._step` 当前在 prepared/draining/stop_base 一律 `matches(base)`，rollback stop 也用 origin_boot 把来源算作 base；**不能让新记录未经适配直接进入它**。在 `managed_controller.py` 增加本动作的固定 controller/policy 分支，复用原有有界 step、CAS、deadline、proof、target/base 后半程；显式处理 origin 匹配、首次 drain/stop、origin 存活时的回退 stop 和已发布后的失败。不要为本动作扩普通 policy 的允许集合，或把 origin 临时塞进 `record.base`。实现若复用共享 driver，必须把这些来源判断作为显式动作分派，而非靠覆盖 `matches(base)` 冒充 origin；不引入通用迁移策略注册器。

记文件三元组为 `(configuration, selector, unit)`，固定相同配置 Q，旧 selector 为 So，角色 selector 为 Sb/St，旧/新 unit 为 Uo/Un。本片 `Sb == St`、`B == T`，下面保留角色名字以区别 nonce/boot/决策，不借此接纳不同安装或 selector：

- O=`(Q,So,Uo)`，B=`(Q,Sb,Un)`，T=`(Q,St,Un)`。
- 正向只接受 O → X=`(Q,St,Uo)` → T；配置步骤仍先确认 Q 的数据和父目录耐久，再 selector CAS、unit CAS。
- 回退从已冻结正向前缀恢复 B，按 unit→selector→配置顺序；其额外中间态仅 Y=`(Q,So,Un)`（O 的 unit 已替换）。所以固定集合为 `{O,X,T,Y,B}`，并按 intent/方向进一步限制。不能笼统接受三个绑定各字段的笛卡尔积。
- stopped/空 cgroup/持同一 writer lease 才允许这些混合前缀；running 只接受完整 origin 或本阶段的完整 base/target，且必须绑定实际进程/boot/slot/unit。同一磁盘元组不能替代进程证明。

| 耐久记录/实际前缀 | 唯一允许的接续 |
| --- | --- |
| 新 slot/anchor/部分 snapshots 已写，state 未发布 | 旧记录仍权威；不执行新 helper、drain 或文件效果。孤立材料不能被当作已交接。 |
| 新 state 已发布，prepared/draining，仍是 O + 原 boot | stable recover 只到新 controller；用冻结 origin 认证本代 drain，完整 owners 屏障不变。普通 service/CLI 不可启动新角色。取消、drain 期限不足或故障保持 recovery_required；不写 aborted、恢复旧 controller 或自动释放一个未提交的 origin。 |
| activating/stop_base，origin 正在运行/停止 | 名称保留 launcher 外壳兼容，但此动作明确 stop 的是 origin。每次重观测先核其旧 anchor/boot/完整 O；不以 base 新 unit 认证它。确认 stopped、cgroup 空、原 writer 已释放后才进入 install_target。 |
| install_target，stopped，O/X/T | 持 lease，逐项 exact CAS/耐久确认到 T；fresh daemon-reload 后 loaded unit 与新 controller argv 完全吻合才可认定 installed。bytes 相等不能代替 loaded/durable confirmation。 |
| 任一文件 CAS/replace/fsync 丢返回 | 重取本代、目录身份和实际前缀，仅确认已有 exact bytes 及持久性；foreign/missing 非法前缀不覆盖。不能根据 exit=0 或异常文本判断效果。 |
| start_target 已耐久，启动结果未知 | 仅观察同一新 boot/角色/实际进程；服务 inactive 含义不明则按原 ambiguous_start 进入固定回退，不重发 start。 |
| verifying/committed，T 新 boot | paused 健康证明 → commit → 原激活/投影；未知激活按原 boot/nonce 重观察和原幂等合同，不开新 attempt 或延长期限。 |
| rolling_back/stop_target | 若仍活着的是 origin，只凭冻结 origin 停它；若是 target，只凭 target 停它；外来/不明 boot 拒绝。已 stopped 的 O/X/T 前缀可推进 restore_base，不能要求它先冒充 B。 |
| rolling_back/restore_base，stopped，O/X/T/Y/B | 原 lease 下逆序到 B，重新确认 loaded unit。再耐久 start_base，唯一一次启动；nonce/boot 与 target、origin 不同，之后 verify_base → rolled_back → 激活/投影。 |
| base/target 启动、健康、材料或期限失败 | 保留首因；剩余回退只在原 reserve 内，无法证明安全则 recovery_required/blocked。不能退回旧 anchor、启动 origin 或续期。 |
| released terminal 且精确投影 | 此后的普通 configure/restart/same-format switch 从新 anchor 的已提交 base/target 取真实 source；继续严格普通准备规则。不能因前代是新动作而降成 legacy，也不能永久卡在只能 recover。 |

所有 effect 前后仍核同代 revision、目录/slot 文件身份与实际 unit/boot。origin 维护 RPC 的身份例外只限本 action、本代、原 boot、主身份、primary challenge 和冻结旧 anchor/binding；不授予 origin activate、健康提交、新 boot 或改配置权限。沿原 lock-free fresh journal + fsync 复核，不在宿主回调中再拿 controller flock。已接受 owner 输入和关闭期间 handler 结算沿既有维护屏障，不能通过 transfer 清空入站或重发 unknown。

## 5. 稳定 launcher 与 unit 前置不能省略

当前 `platforms/managed_launcher.py:425` 的 recover 确实只核有界 JSON、protocol/generation/revision、record 指向的 anchor、该 anchor 精确绑定的 launcher 和 controller 文件，随后 exec controller 的 `muli.managed_host ... recover`；不读 action 白名单，不依赖 selector。`_role:396` 的 service 路由仅懂现有 phase/intent，因此上一节固定保留其拼写。launcher 只是路线，不替代新 controller 的完整 typed/lease/format 准入。

首次发布前必须核实际 retained launcher 精确 bytes/文件身份对应已审且离线验收过的实现；不能仅信当前源码、`handoff_protocol=2` 或调用方声称支持。首片可只支持与本次已验证 launcher 精确一致的 retained 文件；不匹配就在任何 transfer 发布前拒绝，另审其 launcher 升级，不能从任意文件内容推测能力。离线验收须复制这份 exact bytes，经真实临时 Python exec 走新 action/new record/当前 bridge 被提升后的正式 recover 入口，并证明 selector 仍旧/缺失不改变 controller 选择、旧 controller 未被导入。只观察 stub 打印 argv 不足以证明完整 host 接纳；最终还要到正式 `_entry/_record` 严格校验与受控 runner 的 helper 选择。此验收不需要本机真实 systemd，也不要求运行时先执行任意候选来探测能力。

unit 不能声称不变：`ManagedUserService._arguments` 的 Python 来自 controller.lexical_python。新 anchor 必须同步绑定新 render，安装仍 config→selector→unit，回退仍 unit→selector→config；新旧 unit 可不同，路径不变。每次 install/start 保留 exact raw CAS、文件/父目录耐久、必要 daemon-reload、fresh loaded ExecStart/FragmentPath 等原属性复核，以及 exec 后 app 角色进程验证。仅改 selector/anchor、宽松接受旧 loaded unit 或把 app python 误当 unit controller python，均不满足合同。

## 6. 既有函数的最小实现映射

以下是明确需要改动的接缝，不是“现有 V2 已经支持”的清单。无需改 stable launcher bytes、DeploymentAnchor schema、普通 format assess 或数据库 schema。

| 位置 | 窄改动 |
| --- | --- |
| `managed_transaction.py` | 独立 origin/transfer strict 模型与纯 prepare/advance；新 bounded action 分派；旧 parser/action/ordinary invariants 不变。固定 frozen 字段与本动作 phases/proofs。 |
| `management_transaction.py:TransactionJournal.read_current` | 协议 1 原路，协议 2 交严格 action 分派；`JournalControl` 仍唯一 durable writer/CAS，不新增 ledger。 |
| `managed_journal.py:prepare_managed/read_snapshots/_collect/advance_managed` | 原函数保留原动作语义；新增固定 transfer prepare/advance、origin snapshot reader；仅扩本片有限文件命名与预算/GC，读写前后重核。 |
| `managed_preparation.py:ManagedPreparation.prepare/_source/_observe_process` | 分派独立 prepare-transfer；复用 actual source/投影/permit/文件 guard，产生新 anchor 与新 render；收尾 source 选择接纳严格已释放 transfer 前代。禁止把旧 ordinary switch 的 unit 相等条件放宽。 |
| `managed_requests.py`、`management.py`、`management_host.py` 的请求/prepare 分派 | 一条明确的受信本地管理 action：不接受 candidate/new slot/自填 origin 参数；新 controller/base/target 都从实际当前执行 prepare 的 source.slot 派生。不新增模型自动更新、远端安装或通用脚本参数；沿原管理 plan 持久化及预算，只有宿主能构造 origin/anchor。 |
| `managed_host.py:_entry/_record/recover/run_managed_helper` | 新 strict union；按 action 选择固定 transfer controller/backend，所有入口/重入都重新核 anchor/controller、deadline、真实 helper PID/unit 和前后 state digest，不得只修最外 recover。 |
| `managed_controller.py` 与 `management_controller.py` 的上述来源接缝 | 新动作独立来源/前缀 policy；普通两套 controller 旧规则不变。复用单步/异常首因/原资源与期限，不建设通用策略框架。 |
| `platforms/managed_backend.py` 与 `platforms/managed_probe.py:read_managed` | 新动作显式区分旧 origin anchor/controller/unit 观察与新角色观察；固定 O/X/T/Y/B，旧 probe 规则不宽松；stop/drain 是 origin 专用验证，start/install 是新 anchor 角色验证。 |
| `maintenance.py:Maintenance._record/verify_managed`、`managed_runtime_binding.py:verify_running_binding` | 新动作 + role=None 的窄 origin 认证；旧 permit/anchor 与新 record 的区别必须逐项匹配 frozen origin。base/target 仍执行新 anchor 完整原规则。禁止笼统允许 anchor mismatch。 |
| `managed_cli.py:managed_record`、`managed_boot.py:ManagedBootPermit._record/_format`、`platforms/managed_bootstrap.py:admit_managed_boot` | 使用严格联合；普通启动只接 base/target 允许阶段，新 record 的全部不可变字段包括 origin 都纳入前后比较；writer lease/格式/真实当前安装规则不变，缺/坏记录不降级普通启动。 |
| `management_projection.py:project_final` 与 Maintenance 的 projection/configuration 屏障 | 新动作只有新 anchor 的强角色 verifier 可投影；精确 management plan/action/generation 与原 Store 同事务。旧 origin 无最终投影/解除屏障权限。后续普通 prepare 接纳该已投影终态，不偷改 `previous.observed_format`。 |

实现时新增 Python 类型必须有注解，联合分派穷尽；不得用 `Any`/`getattr` 绕过新记录在 boot/projection 的强类型分支。复用现有 record、目录、snapshot、unit render、writer lease、维护和 probe 类型，不为 bridge 另造一套文件协议。

## 7. 先红后实现的最小验证片

第一组行为红测复用现有 journal fixture 与 prepare/proof/release helper，已由根实际执行：**1 failed / 3 passed**，唯一红点为正式 Journal 的 `invalid_record`，不是 fixture/import 错误；pytest 0.11s，守卫 714ms、峰值 61.6MiB、swap 0、exit 1。四个节点为：

- `test_journal_dispatches_complete_transfer_record`：完整 origin 与同安装 base/target、新旧 selector/unit、已释放前代投影、本代九份快照；经正式 `TransactionJournal.read_current` / `JournalControl.read_current` 期待接受独立新动作。先证明旧前代和去掉新 action/origin 的 ordinary envelope 各自合法。旧实现已实际因正式 reader 拒绝新 action 而红，不依赖缺 import/新类。
- `test_ordinary_v2_still_rejects_origin_field[journal]`、`[ordinary]`：先证明无 origin 的普通记录通过，再仅加该字段，Journal 和原 parser 均拒绝且不覆盖原 bytes。
- `test_ordinary_parser_does_not_accept_transfer_action`：原 `parse_record_v2` 继续拒绝完整新 action，不能用放宽旧模型实现第一项。

根随后只授权实现第一片类型/读取接缝，目前已落盘：`managed_transaction.py` 中独立 `TransferOrigin`、`ProjectControllerTransferRecord` 与 bounded `parse_managed_record`；`management_transaction.py` 两处 `read_current` 显式返回新联合并分派。旧 `ManagedAction`、`parse_record_v2`、普通模型与兼容判定未变。新模型固定 base==target==origin 安装/兼容、原配置、不同 anchor/selector、同主身份的前代投影、完整格式 1、合法 phase/intent、独立 nonce 和非 origin boot 的同代健康证明；字段及嵌套模型冻结。模型不能从摘要读出实际新 controller 或 selector 的语义，仍需后续宿主核实实际 bytes/anchor/进程。

同一新测试文件现补字段/阶段/健康/预算负控制：`test_transfer_dispatch_rejects_invalid_fields`、`test_transfer_boot_window_matches_retained_launcher`、`test_transfer_terminal_requires_own_role_proof_and_release`、`test_transfer_terminal_rejects_foreign_or_origin_health`、`test_transfer_nonce_pair_and_frozen_origin_cannot_change`、`test_transfer_dispatch_keeps_original_whole_record_budget`、`test_transfer_dispatch_rejects_duplicate_keys`。包括原 `max_record_bytes` 的恰好 4096/4097 边界与超额坏 JSON 先拒、重复键、bool 协议/revision、额外嵌套字段、未知 action、错误格式/角色/旧 boot/nonce 等。

根已真实执行新文件与旧 managed_transaction/journal 三文件：**165 passed**，pytest 0.41s、守卫 941ms、峰值 63.4MiB、swap 0、exit 0；显式两生产文件 strict/no-incremental、`MYPYPATH=src` 检查通过，3.010s、194.4MiB、swap 0。后者不覆盖所有消费者。随后根全 146 源文件 strict 实际 **exit 1：7 errors / 4 files**，8.225s、423.5MiB、swap 0，具体为 `managed_journal` 两处 previous 类型、Maintenance.current 返回旧联合、projection 三处身份类型/误落 V1、preparation._Source.previous。不能用前两文件绿声称全链可用。

本片没有 transfer prepare/advance 发布入口，没有 snapshot writer、controller/helper/boot/probe/maintenance/projection 的新动作授权。`read_current` 的真实联合类型会显露旧消费者尚未分派的接缝；不以 `Any`/cast/错误旧返回注解隐藏，根类型检查若指出未接入消费者，应先显式拒绝新动作或按后续授权逐段集成。跨 revision 的 immutable 字段/nonce/首因守卫属于后续纯 advance/CAS，本片 frozen 模型不冒充历史比较。

根据全源反例授权的临时保护已仅落在上述四处：`prepare_managed` 在传 previous/快照写入/GC 前、`Maintenance.current` 返回旧联合前、`project_final` 在身份分支/数据库写入前、`ManagedPreparation._source` 在读旧投影/推导 V1 前，显式识别 Transfer 并抛各自领域错误 `project_controller_transfer_unsupported`。不伪装为 None/V1、不降级、不覆盖新记录。这些是未开放功能的 fail-closed 保护，**不是最终接入合同**；后续必须用强 typed origin/新角色验证替换，并真正支持已投影 Transfer 成为 ordinary 后继 source。

新文件补 `test_ordinary_journal_prepare_rejects_transfer_before_snapshot_gc`、`test_ordinary_maintenance_rejects_transfer_instead_of_v1_fallback`、`test_ordinary_projection_rejects_transfer_without_writes_or_verification[False/True]`、`test_ordinary_source_rejects_transfer_before_process_or_new_plan` 五节点；复用现有临时 journal/Store/permit/Runtime host，核原 state/快照/plan/event/receipt/DB changes/probe/verifier 调用不变，退出前恢复夹具原记录。这里允许既有只读预检：`project_final` 先 `_connection_identity`，`_source` 先 `revalidate_format`；合同是 **DML/持久写入、GC、probe 前拒绝，不是零 SQL**。

根后续实收该保护片六文件 **269 passed**，pytest 2.24s、守卫 2.809s、峰值 110MiB、swap 0、exit 0；全 146 源 strict 再验真实 **exit 0**，守卫 8.252s、414.6MiB、swap 0，取代上文全源 7-error 的候选状态并保留其红历史。C 对四处临时 guard 独立只读后审无 P1/P2。上述仅证明未开放动作严格拒绝及现有行为回归，绝不等同 transfer 已可准备、发布、重启或恢复。

这些是 parser/journal 证据，不构造真实 anchor/进程，也不替代宿主准入；后续实际红绿与资源结果仍由根唯一守卫收取。其余拟议节点及证据边界如下，仍需逐段授权，不复制全套测试框架：

1. `test_transfer_typed_dispatch_isolated`：合法新 action 被原 reader 拒绝、新分派才接纳；参数反例为普通 switch 偷带 origin/不同 unit、新动作未知字段/bool revision、错 selector deployment、base/target 不同或安装非实际 origin、非完整格式 1。普通 V1/V2/same-format 反例继续原样拒绝。
2. `test_transfer_freezes_origin_and_new_unit_before_publication`：真实前代精确投影与 O；当前 bridge python 与旧固定 controller 不同，断言新 controller 就是该 bridge、base==target、新 anchor/unit render 确实不同、角色 selector 相等但不同于 origin、origin 三文件都存本代。传任意 candidate/slot、错旧投影/boot、current slot 内容漂移、unit 渲染不符在 publish 前无 drain；重试不重置 deadline/nonce。
3. `test_transfer_snapshot_and_entry_budget`：实际保留前代 + 9 本代 + anchor/slot + tmp 精确临界成功/超 1 拒绝；record/snapshot 超额分别拒绝。下一代 GC 后本代所需 origin/base/target 仍完整；未知条目不删、仍计额度。不能把最多 32 项常量当可用余量。
4. `test_retained_launcher_routes_new_controller_before_selector`：以实际 retained exact bytes/真实临时 exec 走正式 recover，new state 已发布但 selector 旧/缺失；到达当前 bridge 正式 `_entry/_record` 并通过受控 runner 选择其新 helper，旧 controller 不能执行。未经核验的 launcher bytes 在发布前拒绝；非当前 bridge 的 slot 参数根本不接纳，不靠执行它测试 action 能力，不能 stub host 成功替代正式入口验收。
5. `test_transfer_origin_is_not_base`：origin 真实旧 unit/anchor 能 drain/stop，不能满足新 base 健康/boot/投影；替换 origin_boot/旧 selector/旧 anchor 任一项拒绝。专测 prepared/draining reserve 不生成 aborted，不启动旧 controller；固定 rollback stop_origin 路径不误停外来进程。
6. `test_transfer_recovers_exact_prefixes_and_unknown`：小参数覆盖 publish 前、publish 后 O、X/T/Y/B 的合法 intent、foreign 前缀、unit CAS/fsync 丢返回、旧 loaded unit、start 返回丢失、paused health/activate 未知。断言 writer 与 cgroup 屏障、每个持久 start intent 至多一次调用、原 absolute deadline/reserve、首因不变，角色 boot/nonce 不混。
7. `test_transfer_terminal_projects_and_allows_next_same_format_request`：新角色提交或回退在同一真实 DB 投影，close/reopen 后严格接纳该终态为普通新请求 source，当前 anchor/controller/unit 是新值；Store/main、原消息/已接受 pending 与 unknown receipts 不重建、不丢失或重发。格式 1 不变，普通 1→2 仍被原 assess 拒绝。

前 1–3/5–7 的替身通过只证明纯规则、文件/SQLite/lease 与调用边界；第 4 另外证明精确 retained launcher/正式 host 进程路径。真实 systemd loaded unit、进程/cgroup、关闭 owner 与新 writer 交接还需要单独受控验收，不能用 DTO 或 fake probe 宣称已部署可恢复。该批准也不授权运行原生 CI、新进程或真实服务；根先审合同，再给独立实现树与唯一守卫执行窗口。
