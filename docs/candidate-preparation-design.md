# self_update 候选准备与托管切换设计

状态：2026-10-21 设计基线，最初**尚未实现**。源码依据为根 `/data0/MuLi` 的 2.22
候选 `5c6bac495bfd9f05ff2132da1e0c5cddbb96dd2e`；本独立树不含全部根集成。
设计阶段仅读代码、Context7/官方源码并写本文档，没有执行安装或下载。
v2.23现已实现独立候选准备核心，接口/定向验证见`candidate-preparation-core.md`；
管理入口和内核执行后端仍由集成片验收，不改变2.22功能范围。

首片纵向交付：同一次真实 `self_update` 审批、同 request_id/原 `FrozenRouting`，
受限准备最终保留候选，验证后交现有 V2 switch，最终精确投影后才完成。模型只选择
已展示的目标，策略来自宿主且审批可见；不提供三路径、自报 source_ref/approval 或另起 switch 请求。
首片采用本地 wheelhouse，但这不是长期要求用户自备整套 wheels 的最终更新体验。

## 现有源码与两处实际接缝

| 根候选源码点 | 复用能力及边界 |
| --- | --- |
| `updates.py:81` `Updates.check`、`providers/http.py:10` `read_response` | 直连有限 metadata；不是构件下载/流量后端 |
| `install_profile.py` `parse_install_intent` | 保留原主要求/extras/with/constraints/overrides、四个 options；仍是 context_required |
| `platforms/install_receipt.py:89` `read_install_receipt` | 固定 receipt、有限读取与变化检测；当前仅核 V1 manifest，不认证 uv producer |
| `platforms/managed_deployment.py:209` `register_local_slot` | 真实解释器/包/静态兼容声明；不证明依赖完整或候选可启动 |
| `managed_preparation.py:334` `ManagedPreparation.prepare` | 实际 binding、Store/格式、三文件、原计划/routing、唯一 V2 journal |
| `management_host.py:219` `RecoveryHost.prepare` | prepared 后只接续同代、固定 controller/helper、终态投影 |
| `process.py:130` `run_process` | argv、期限/输出及重复取消收尾；没有 RSS/CPU/磁盘/网络硬限制 |

`InstallationManifest._paths_and_unit` 绑定标准 legacy unit，不能为 managed 当前 slot
伪造 V1 manifest 来调用 receipt reader，也不能拿固定 controller 的 receipt 当当前
意图。提取共用有界读取/parse seam，新增当前 SlotIdentity reader：前后复核
`verify_current_slot`、anchor/实际 running binding，固定读 `slot.prefix/uv-receipt.toml`，
证据绑定 slot digest。原 V1 API/DTO/verify 不放宽。

`SelfManagement._run:233` 当前持 configuration_lock await `_apply`，安装必须移到锁外
强拥有阶段；`stage:87` 的原位 update 限制与 `_apply:253` 的原位 upgrade+restart须显式
分流。marked 库继续禁 in-place，不借自动 update 隐式 adopt。首次支持已托管、旧代
resolved 且实际 Store 精确投影、协议 2/同完整格式无需迁移的 Linux 安装。

## 来源、意图和本次策略

首片只有 `local_wheelhouse`：宿主实际核有限普通 wheels 的来源证据/大小/SHA256/
所有权，冻结到 attempt 自有材料目录；任意模型路径或自报哈希不是许可。已核现有
Python，未知 uv adapter、源码/VCS/直接 URL/editable 均拒绝，缺包 `offline_missing`，
不自动联网补齐。已知自定义 index/auth/TLS/proxy等与本次策略冲突且无法明确保留时
`context_unproven/unsupported_*`；不能抹掉主人意图后宣称匿名 PyPI。

原 receipt 主要求、顺序和已支持 options 保留；原范围与目标 pin 一起交官方 uv 求解，
不重写 PEP 求交、不删冲突约束重试。实际 receipt与明确批准的本次offline/no-config
匿名策略生成本次intent，不声称重建历史env/config，不因无关父环境键停工。子环境从允许键构造，不复用
`updates.direct_environment` 的“全继承减代理”；不重设 HOME/CODEX_HOME。
按[已核上下文边界](install-profile-design.md)使用现存空 NETRC、私有空
UV_CREDENTIALS_DIR、禁 keyring/native auth、no-config；不存在 NETRC 仍会回退。

首次从实际 resolved journal/同 Store exact receipt 选中 role/slot，再读其 prefix receipt。
“原安装意图”与“本次离线部署选项/精确 pin”分别保留：候选 receipt 的私有
find-links/no-index/no-build不能丢，也不成为下一轮永久主人来源。每次准备证据在本次
plan内有界自包含地复制原 intent、本次部署策略/候选 receipt，并绑定候选 slot digest。
第二轮只沿当前 record.request_id 读取对应既有 plan，重验当前 slot/活动 receipt/该份
证据，再按原意图生成候选；不另立全局 profile 账本或遍历祖先。缺链/外部重装则
stale/context_unproven，不拿 controller receipt、不编辑 receipt 消除 pin。

## 一次计划的顺序与所有权

准备模块直接接管理 worker，最小 seam 是 `prepare_candidate(持久计划, 已核来源,
已批准策略) -> 私有候选凭据` 与 `verify_candidate(凭据, 当前来源) -> LocalSlotPaths`。
凭据不是第二个 active selector；绑定 request、原 slot/receipt/profile、uv/Python、
目标版本/最终路径、材料和候选 receipt/依赖证据、deployment/controller、策略。
内部argv/要求原文作为有界私有材料，不写公开事件；有限status不含全路径。
策略单独持久在既有Store的`update_policy/candidate-v1`，不加主Settings字段，避免
2.22固定controller严格解析配置时拒绝；显式策略管理仍完整保留三路径与保存往返。

1. 现有 `self_update` 只审批 version，尚未覆盖来源/预算。managed 首片增加
   `expected_policy={source,limits,disk_mode,revision}`：self_status/config给有限安全摘要，
   revision覆盖宿主策略、实际slot/receipt/材料声明；原真实approval直接展示原args。
   字段只是 If-Match 前置条件，不授予来源；managed缺失时提示先查状态，legacy仅version不变。
   handler/worker精确比对宿主实际策略/来源并复核材料hash，漂移须重批；不新增通用
   Tool/Runtime审批hook或第二批准。首次持久plan即 `action=switch,operation=update`，
   保原version/request/routing；worker沿现有applying附 `phase=candidate_preparing`。
2. 分配独占**最终** retained attempt；锁外准备。UV_TOOL_DIR=`<attempt>/tools`，
   UV_TOOL_BIN_DIR=`<attempt>/entrypoints`，cache/temp/wheelhouse同属 attempt。
   不安装后移动 venv，不写当前/唯一上一/固定 controller/公共 uv bin，不 force。
3. 固定 argv 调 `uv tool install`，原主要求/with/constraints/overrides加目标约束，
   显式 python、no-python-downloads、no-build、no-compile-bytecode，及
   `--offline --no-index --find-links <冻结材料>`。不用 shell，不透传任意参数。
4. 核真实版本、候选 receipt、自消费后的部署策略、有限依赖一致性和材料来源/hash，
   再静态 register_local_slot；私有证据 fsync 后才 ready。uv exit0、pip check成功
   均不单独证明来源或可切换。预检使用临时配置/库，不加载真实 MCP/渠道/模型。
5. 回配置锁重验原计划/批准、slot/receipt/profile、实际 config/selector、Store/格式，
   await期间漂移则stale；ready短事务给原plan附candidate，再调原RecoveryHost.prepare。
   内部managed-update领取路径允许preparing尚无candidate，公共self_switch仍要求三路径。
   prepare/projection/旧包恢复均只消费既有switch；operation仅供worker准备分流/UI展示，无动作映射。
6. 交现 helper/controller/selector/被动启动；prepared后错误只 recover同代，不续期、
   不再安装。journal resolved且实际 Store精确投影才 completed；失败复用一次回退。

SelfManagement持实际 Task和安装树句柄，纳入 idle/close；移出配置锁不能丢所有权。
close先阻止领取、取消并等真实收尾；重复取消不丢句柄，cgroup确认为空才清attempt。
安装worker `Restart=no`，同一总截止；控制helper的on-failure/每次RuntimeMax不能
原样拿来证明一次prepare总180秒。start()须将无匹配journal的准备中applying投影为
interrupted并保留清理归属，不自动重装续预算；ready只可显式重验后继续。finally
先重读Store/journal，不能用旧item覆盖已发布transaction。只清本attempt且未被账本/
selector引用的材料，保当前、controller及唯一上一可用安装，不清用户cache腾空间。

## uv 官方依据与必须消费自身 receipt

已经 Context7 定位，再核[官方0.13.0固定提交](https://github.com/astral-sh/uv/tree/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17)，main片段只作线索。
[0.13.0→0.13.1官方差异](https://github.com/astral-sh/uv/compare/0.13.0...0.13.1)中receipt.rs/tool.rs/ToolOptions定义未变，
install/create环境移除缓存参数，upgrade写receipt条件有调整；不能将所有patch版一概推断兼容。
本机另一agent已观察uv0.13.1，所以实施应将**已核0.13.1**纳adapter/fixture，不能只拒绝
非0.13.0；未知字段/结构仍拒绝，当前uv版本仍不证明历史receipt producer。

- [工具实现](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-tool/src/lib.rs#L133-L135)固定工具环境`UV_TOOL_DIR/muli-agent`，receipt在其根；create_environment会移除已有目标，不能盲重发安装。
- [存储文档](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/docs/reference/storage.md#tool-executables)的UV_TOOL_BIN_DIR只管外部入口；[venv实现](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-virtualenv/src/virtualenv.rs#L188-L245)按scripts scheme建Python。Linux常规环境核prefix/bin/python与pyvenv/实际包路径，不硬编码Python小版本目录。
- [no-build定义](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-cli/src/lib.rs#L6395-L6410)拒需要构建的sdist，可用已构建cache wheel，first-party/editable仍有例外；不是禁执行代码开关。不能照抄pip子命令参数表。
- [wheel路径](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-install-wheel/src/wheel.rs#L302-L342)生成console/gui wrapper、迁移.data/scripts并改shebang，不将它们作为post-install hook调用；[tool完成步骤](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-tool-commands/src/common.rs#L700-L900)链接入口/写receipt。这不证明整个uv命令不启动Python。
- 后续解释器仍会执行环境[.pth/sitecustomize](https://docs.python.org/3.13/library/site.html)；[`-I`](https://docs.python.org/3.13/using/cmdline.html#cmdoption-I)隐含E/P/s，不隐含S。探测/依赖检查/试启动属于受限执行，no-build不是恶意wheel沙箱。
- [NoIndex/Offline区分](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-resolver/src/resolver/availability.rs#L209-L215)：wheelhouse首片明确同时用；未来index cache模式保索引身份，仅offline，不能无条件no-index导致缓存不可用。

独立实验作者报告：`/usr/bin/uv` 0.13.1（62faed335），0700临时根P，1450字节
stdlib生成wheel，仅单模块/console_script，无依赖或build脚本；实际安装一次。
argv为`uv tool install --offline --no-index --find-links P/wheelhouse --no-build
--no-python-downloads --python /data0/MuLi/.venv/bin/python --no-config muli-uv-offline-probe==0.0.1`。
UV_TOOL_DIR/BIN_DIR/CACHE_DIR/PYTHON_INSTALL_DIR及TMPDIR/TEMP/TMP均指P私有路径；
清继承UV/PIP、虚拟环境和Python覆盖键，不改HOME。prefix=P/tools/包名，P/bin入口
链接prefix/bin脚本；绝对入口和子进程私有PATH各执行一次，实际exe/prefix/module正确。
receipt为467字节，tool.options含no-index=true、file://私有find-links、no-build=true，
不保存offline/no-config/no-python-downloads。guard服务140ms/CPU129ms/峰40.7MiB/swap0；
finally回收进程组并删全部临时材料。未验证第二轮或真实with/constraints/overrides。
当前ReceiptOptions只认四项，同选项的muli-agent候选**安装后会被自身parser拒绝**；
首片须窄扩有限本地find-links/no-build及来源/所有权/预算校验，加入原receipt→候选
receipt→第二次更新链回归；不放行未知字段。微型实验和清理不等于产品链已实现。

## 资源、下一片与验收

沿[资源设计](recoverable-management-design.md#资源磁盘与保留预算)窄typed限制：准备180秒/预检30秒、进程树512MiB/swap0/Tasks64/单核等效；候选512MiB、cache/temp256MiB并留安全空间。
默认disk_mode=threshold，无quota可跑；候选/cache数字是停止阈值，有超调而非硬保证。
先冻结有限wheel数、压缩总bytes、中央目录声明解压总bytes/文件数，核同FS余量并保留current/controller/previous。
uv缓存/临时/输出均归attempt；运行有界周期核owned tree，达阈值取消，等实际cgroup空才清理。
内存/CPU/tasks仍核真实cgroup硬限，缺后端不退裸执行；sibling如实报独立上限，不冒称aggregate或强改现有slice。
仅显式strict要求已有可核quota，不自动创建；run_process/HTTP超时/开发guard不等于产品磁盘或流量保护。

后续尽快接有限直连材料获取，共用本片准备/切换后半段：元数据/文件数有界、来源和
SHA核验、逐块计实际传输，限制单文件/总量及声明和实际解压量、总期限，不自动代理
或扩大费用。构件流式落盘，不用read_response整包进内存。如何从官方uv有界取得完整
依赖wheel清单仍待该片落实，当前不具备现成平台清单；不以前置全配置/凭据/计费平台
替代实际默认匿名更新闭环。不得offline失败自动online，用户无本地材料时如实报告。

修改面：新增候选准备和窄Linux执行适配；窄接receipt reader/parser、management worker、
canonical switch/operation=update的内部领取与ready接续及工具展示；同步self-management/更新/
资源说明。保持V1与原位禁令。三项风险限于意图与部署pin混淆、阈值冒充硬预算、ready
与实际运行权威错配，分别用真实来源链、受限执行证明和唯一V2终态投影验收。

后续必要回归：临时最小wheel集真实安装→公开self_update→实际V2切换（同批准/request/
routing）；冲突/缺wheel/未知上下文/超预算拒绝；await内来源漂移；重复取消/close/
硬退出后interrupted与进程树回收；两次更新不固化部署pin/find-links；prepared后只
同代恢复与一次回退，不动固定controller/当前/唯一上一，不触真实用户库/外部服务。
