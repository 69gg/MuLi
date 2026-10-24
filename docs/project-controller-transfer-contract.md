# 阶段一 bridge：固定 controller 换代的合同与实施记录（未闭环）

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

## 8. 2A：released Transfer 作为普通前代的首批行为红测

根签名第一片 checkpoint `d4b648c3b09fc18052faaa7596d11e9929e06b85` 后，先仅在 `tests/test_project_controller_transfer.py` 追加以下三组、八节点，未先改生产：

- `test_released_transfer_is_ordinary_restart_predecessor[committed/rolled_back]`：完整合法 released Transfer 经正式 Journal 读取，持原 writer lease 准备普通 restart。角色来自新 anchor 的 base/target，而非 origin；保留本代九快照，随后同 request 重试不改变原 deadline、nonce、revision 或 state bytes。
- `test_transfer_predecessor_requires_current_projection_without_effects[origin/missing/wrong-revision]`：旧 origin receipt、缺失 receipt、错误本代 revision 均须以 `projection_required` 拒绝，整目录实际 bytes 不变，包含可回收旧快照，不能先 GC/写入再发现投影不符。
- `test_transfer_predecessor_nine_snapshots_entry_budget[exact/one-short]` 与 `test_transfer_origin_snapshots_gc_only_after_next_predecessor`：实际九份前代文件、六份 incoming、state/lock、未知条目及一个原子替换临时项构成精确额度；只收紧原默认额度，不提高上限。刚接普通后继仍保留九份 Transfer 文件；再下一代仅回收已不再引用的完整九份（含 origin 三份），保留当前普通前代六份及未知条目。

根实际收取这八节点：**8 failed**，pytest 0.34s、守卫 906ms、峰值 71.6MiB、swap 0、actual exit 1；全部停在正式 `prepare_managed` 的显式 `project_controller_transfer_unsupported`，投影反例也先遇到该一刀切门而未到预期投影拒绝。这不是尚未存在函数、导入或坏 fixture 制造的红。

根据此授权并落盘的 2A 候选仅修改 `managed_transaction.py` 与 `managed_journal.py`：`prepare_record_v2` 显式接纳 resolved Transfer 前代且只开普通 restart；精确本代投影、Store/main、新 anchor deployment、选定角色的 entry/installation/compatibility/selector/unit、完整 observed format 和新 generation 均在写入前核验。同 request 仍按原普通记录规则保留原 deadline/nonce/position，Transfer request_id 不可伪装成普通重试。未 released 及其它普通动作仍返回原 unsupported，不放宽旧 V1/V2 模型、parser 或兼容判定。

Journal 在纯门通过后，经同一受限目录读取并验证本代 origin/base/target 三组完整九份文件，之后才进入原 record/目录额度、guard、GC、snapshot 写入和最后 state 发布；缺失或损坏不修复、不靠 incoming 替代旧证据。GC 仅额外识别固定 `origin-configuration.json`、`origin-selector.json`、`origin-unit.bin`，保留当前前代整个 generation；下一代已无引用时才回收三组，`origin.json` 等未定义条目继续保留并计额度。原默认 limits、原临时条目预留和旧普通重试分支均未提升或另造。

同文件另补 `test_transfer_predecessor_verifies_every_retained_snapshot_before_effects`（三组×三组件）、`test_transfer_predecessor_missing_origin_snapshot_is_not_repaired`、`test_restart_after_transfer_retry_does_not_repair_changed_snapshot`、`test_released_transfer_does_not_open_ordinary_configure`、`test_transfer_predecessor_rejects_foreign_ordinary_material_without_effects`（deployment/Store/main/旧 origin binding/重代/重 request 六项）、`test_transfer_predecessor_keeps_host_guard_before_new_material`。拒绝前后逐项比较整个临时目录 bytes；保留旧未 released unsupported oracle。

2A 实现后，根唯一守卫实际收取六文件 **296 passed**，pytest 2.27s、守卫 2.865s、峰值 109.5MiB、swap 0、actual exit 0；其中已包含此前 269 项，不将两轮数字相加。根仅格式化本片三份 Python 文件，三文件 Ruff 通过；全 146 源 strict/no-incremental 实际 **exit 0**，同一守卫包含格式/Ruff，合计 8.317s、峰值 419.8MiB、swap 0。C 对冻结 2A 生产与回归独立只读后审未见剩余 P1/P2，未执行检查。上文八红保留为改动前真实历史；本轮通过的是当前纯规则、临时文件/Journal/lease、快照读验与 GC 接缝，不是实际 Transfer 执行或恢复验收。

首批都走现有正式 prepare/Journal/lock/lease 和临时文件，不新增 Transfer 发布入口或真实宿主效果。新请求 `origin_boot` 为独立的调用方测试材料，显式不同于历史健康证明；不以历史 `health.boot` 合成 fresh permit。GC 后继的终态由现有纯 transition/proof helper 构造，不声称真实 controller 已执行。普通 live configuration 可能通过既有 Settings 路径变化，本片只比较角色的其它不可变材料；调用方仍必须交付与实际新配置匹配的快照并通过原 guard，不强行恢复历史配置。guard 反例只证明原同步回调在新材料写入前仍能拒绝，不证明真实宿主准入；`ManagedPreparation._source`、Maintenance、boot、host、projection 的 Transfer 执行分支仍未开放。2A 不代替后续 2B 的当前安装/实际配置/fresh permit、关闭重开或正式 launcher exec 验收，也不证明真实 transfer 已可发布或恢复。

## 9. 2B：正式 terminal permit 到普通 restart 的读链红测

2A 已由根签名为 `f248fc1aabdea307a7370c96fa597bafa100a1c2`（本地、未推）。下一片先仅新增 `tests/test_project_controller_transfer_runtime.py` 的五节点。根唯一守卫已实际收取 **5 failed**，pytest 0.42s、守卫 971ms、峰值 74.9MiB、swap 0、actual exit 1；全部停在正式 `ManagedBootPermit.acquire` → `_record` 的 `managed_boot_verification_failed`，两项预期 `managed_boot_not_permitted` 的负控制也先遇同一接缝。这是完整材料通过正式入口后的行为红，不是 fixture/import 错误。

复用现有 `test_managed_boot.Host` 的真实临时 SQLite、私有 journal、两个已登记安装、launcher/anchor 文件与进程属性边界。先从实际 slot、Store/main、渲染 unit 和 canonical selector 生成合法普通旧代，**通过正式 `ManagedBootPermit.acquire/open_store`、`verify_running_binding`、`project_final` 产生 origin 精确投影**，关闭旧 writer 后再冻结本代 Transfer 九快照与终态。当前 state 始终是严格 `ProjectControllerTransferRecord`；不把它转成普通 V2 交给被测读链，不手工实例化 permit、不替换任何 reader、Maintenance.current、verifier 或 projection checker。

旧 anchor.controller_slot 是另一个实际 retained 安装，new anchor.controller_slot 正是当前 bridge 安装；新旧 unit 均由 `platforms/managed_probe.py` 的 `ManagedUserService` 正式渲染且 bytes 不同，launcher 指纹/路径保持。旧普通前代仅用于真实 origin receipt 生成；Transfer 终态由严格模型在夹具中发布，不冒充 controller 已执行。新测试重开的是原 journal/DB/lease 同 inode 的一组真实描述符，由正式 `BootSession`/Runtime 持有和关闭；原 Host 仍拥有其夹具描述符，不另建账本或第二把 writer lease。

- `test_terminal_transfer_real_permit_runtime_observe_and_restart[committed/rolled_back]`：正式 EntryPlan → `ManagedBootPermit.acquire` → 再次 `_record` → Runtime 自有 `open_store`/Maintenance。fresh caller boot 与 Transfer 历史 health.boot、旧 origin boot 三者不同，不能拿历史健康证明当 fresh 许可；Runtime 必须绑定同一真实 permit，选新 anchor 的 base/target。旧 origin receipt 仍使 configuration_blocked 为真；fresh permit 的 `role=None` verifier 请求拒绝且无 DML。只有本代 `project_final` 经 Runtime 真实绑定 verifier 写入精确投影后，才解除配置屏障并走 `observe_current`、真实管理 plan、`prepare(restart)`，新 record 的 origin_boot 必须来自 fresh permit。仅放开 Maintenance.current、不补投影屏障不能让本测试通过。
- `test_terminal_transfer_invalid_boot_is_rejected_before_dml[origin-boot/unreleased]`：本代旧 origin boot，或未 released 终态下的新 boot，在 acquire 处须以 `managed_boot_not_permitted` 拒绝；既有 DB 逻辑快照与 state bytes 不变。不让本片的 terminal 读链授权任何未 resolved Transfer/helper 执行。
- `test_terminal_transfer_old_projection_blocks_observe_and_prepare_without_dml`：permit 与 Runtime 正常重开，但仍只有真实旧 origin receipt；observe 和 restart prepare 都须在 probe/新 plan 事件/新 journal generation 前以 `transaction_in_progress` 拒绝，SQLite total_changes、逻辑快照和 state bytes 不变。

改动前首个真实拒绝接缝是 `managed_boot.ManagedBootPermit._record` 调用仅支持普通四动作的 `parse_record_v2`：完整 Transfer 成为 `invalid_record` 并由 acquire 包装为 `managed_boot_verification_failed`，尚未到 Runtime。不能只改首个 parser 或 current() 就称整链完成。实现阶段补足夹具的后续前置：旧真实 writer 在关闭及发布 Transfer state 前保存本代 `management` plan，使新 `project_final` 有实际 action/generation 可核；这不是生产缺陷，也不改变上述首红的 parser 因果。`observe_current` 的内部 `_source(..., action='switch', item=None)` 是只读定位，不等同本片开放 switch 发布；实际新增效果仍只允许 ordinary restart。

根据根授权，当前候选仅接 `committed/rolled_back` 且已 released 的 Transfer：`managed_boot` 使用严格 action 分派并拒绝旧 origin boot，旧 EntryPlan revision 只路由，正式 `_record` 仍重读当前 bytes/同代身份/允许 role；open_store 前及原连接准入前后都核不可变字段（包括 origin/action）。Runtime 的真实 permit/Store 绑定 verifier、Maintenance 的精确 projection checker、`project_final`、`managed_runtime_binding`、`ManagedPreparation._source/observe_current` 及 `platforms/managed_probe` 显式接纳这一终态联合。新角色实际 anchor/slot/config/unit/selector、Store/main、完整格式、lease 和前后记录门保留；source 在任何写入/probe 前还读验九份本代快照。fresh boot 不要求等于历史 health.boot，不从后者制造许可。

Maintenance 只开放终态读取/精确投影屏障；旧 BootPermit、旧 origin、`role=None` 与所有 Transfer drain/readiness/activate/resume RPC 仍拒绝。未 resolved Transfer、新动作 prepare/advance、CLI/bootstrap、controller/helper 和真实宿主效果仍未开放。普通 V1/V2 的 parser、角色与投影接受规则不变，没有新增期限、条目或字节预算。旧 receipt 在观察/准备的 probe、DML 或新 journal 代之前阻断；实际新效果只允许 ordinary restart。

新增候选反例 `test_terminal_transfer_old_entry_plan_is_only_routing[released/now-unreleased]` 核旧 plan 后当前 revision 的真正接纳或拒绝；`test_terminal_transfer_origin_cannot_change_between_acquire_and_open_store` 核 immutable origin 漂移在 Store 打开前拒绝；`test_terminal_transfer_runtime_rejects_changed_frozen_origin_before_probe` 在真实 Runtime 已产生精确本代 receipt 后，仅改同代 origin，核强 verifier/source 仍拒绝且无 DML/probe。后者保留 ID/position/receipt，不能只靠配置投影不匹配得到绿。

根首次执行这九节点实际 **8 passed / 1 failed**，pytest 0.79s、守卫 1.364s、峰值 81.5MiB、swap 0、actual exit 1。唯一失败是 acquire→open_store 漂移用例的错误码断言：同 revision 原始 state bytes 已改变，既有 EntryPlan digest 门更早以 `managed_entry_changed` 拒绝，DB 无写；原预期 `managed_record_changed` 不符。这不是生产未拒绝。本次只将该节点改为精确 `managed_entry_changed` 并注明更早原始 bytes 门，生产保持冻结；修后重验和全源类型检查仍待根收取，不将 8 项通过宣称为整片通过。

C 随后独立只读后审指出同代 action 降类型 P2：Runtime 已精确投影后，仅将 Transfer 去掉 origin、改 action 为 restart，正式 `TransactionRecordV2.model_validate` 仍可接受；position 和 receipt 不变，而原两处 immutable 门只识别 incoming Transfer。根先实收新增参数 `test_terminal_transfer_runtime_rejects_changed_frozen_origin_before_probe[action-to-ordinary]` **1 failed**，pytest 0.28s、守卫 923ms、峰值 72.5MiB、swap 0、actual exit 1，准确失败于真实 `Maintenance.verify_managed` 的 DID NOT RAISE，非坏 fixture 或模型拒绝。

根据该红授权的窄修复用一项纯身份比较：incoming Transfer 或 permit 原记录为同 generation Transfer 时，必须两侧仍为 Transfer 且所有 immutable 字段相等；不同 generation 的普通后继沿原规则，不被此门拦截。强 verifier、preparation source 和 Runtime projection checker 共用这一门；配置屏障即使 receipt position 相等也须闭锁，不增加普通记录文件核验。漂移两参数保留原 receipt，分别断言真实 verifier 拒绝、配置闭锁、直接 `_source` 在 probe/DML 前拒绝，以及 `observe_current` 在配置屏障拒绝。原两项正向仍须真正准备下一 generation ordinary restart，不能以同代不可变性拦掉合法后继。当时尚未重验，以下另记根最终实收结果。

根在窄修后实际收取本片 **10 passed**，pytest 0.83s、守卫 1.412s、峰值 81.2MiB、swap 0、exit 0。随后本片与 transfer/transaction/journal/preparation/boot/runtime-binding/maintenance/projection/probe/management/bootstrap/CLI/host-wiring 共 16 个测试文件串行整合 **481 passed**，pytest 12.10s、守卫 13.045s、峰值 262.8MiB、swap 0、exit 0；该总数包含前述 10 项，不相加。全 146 源 strict/no-incremental 检查 exit 0，守卫 8.104s、峰值 413.9MiB、swap 0。Ruff 导入排序及四文件格式候选修整后，八个 Python 文件 lint 和 format check 均通过；未修改运行语义。

C 与另一位未参与实现的审查 agent 分别只读后审，均确认 action 降类型 P2 在双侧身份门、三处消费者及反例合同中闭合，未见新增 P1/P2。不同 generation ordinary 后继仍走原宿主/格式/角色准入，Transfer RPC 与新动作执行仍拒绝；精确 receipt 的幂等早返不能绕过 Runtime 的身份门。审查本身未运行检查，不将其计作额外测试证据。

受控 probe 复用现有 process/systemd 边界，显式区分 loaded unit 的 launcher argv 与 exec 后 app argv；磁盘文件、模型、SQLite 和持有的 permit/lease 都走真实本地实现。没有启动 Runtime lifespan、模型、服务、解释器或新进程，未扩大宿主能力。这组通过也只能证明 terminal 读链与普通后继准备，真实 controller/helper、retained launcher 正式 exec、未知 start 与真实 systemd 交接仍待后续独立片。
