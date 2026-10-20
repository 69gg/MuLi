# uv 安装意图与 InstallProfile 最小设计

状态：独立纯 receipt 意图解析及定向检查已完成；采集器、完整 InstallProfile 和候选安装仍是后续方案。本片没有读取真实 receipt、用户配置、凭据或数据库，没有运行 uv、安装、下载或服务操作。不把当前本机 uv 版本列为已支持。

本片接续[跨版本兼容元数据](upgrade-compatibility.md)，细化[可恢复管理设计的候选准备](recoverable-management-design.md#uv-候选准备继承要求不碰当前环境)。当前只解析已知 receipt 结构并保留安装意图；完整解析上下文仍待补证，不回答目标是否可解、候选是否健康或能否切换。

## 当前已实现的窄入口

`muli.install_profile.parse_install_intent(raw, uv_version="0.13.0", limits=...)` 只接收 bytes 和调用者声明的版本，返回冻结的 `ParsedInstallIntent`。成功状态固定为 `intent_parsed`，`context_status` 固定为 `context_required`，不输出 `profile_complete`、候选版本或可安装结论。传入版本不是二进制认证，也不能证明 receipt 的历史 producer。

当前保留主 requirement、extras、with、constraints、overrides 的原顺序及原 specifier/marker，保留显式 Python 请求和 entrypoint 证据。省略的 specifier/marker/options 与显式空值区分；不求值 marker、不解析 PEP 版本、不排序或去重。使用上游规范名称格式，只接受主工具 `muli-agent`；with 再声明主工具时拒绝歧义。入口路径与 Python 请求只是文本证据，不会被打开或执行。

已支持 options **仅** `index-url`、`extra-index-url`、`no-index`、`index-strategy`，后者保留三个已核枚举，不擅改原策略。索引 URL 与 requirement 的 registry index 只接受无 userinfo/query/fragment 的 HTTPS；不验证真实连通性、认证或信任。named index、find-links、构建选项以及 tool 的 excludes/build constraints 即使显式为空也暂不支持；任何其它根/tool/requirement/entrypoint/option 字段都固定拒绝，不丢字段后成功。旧字符串 requirement、非 registry 来源、groups/conflict 和未知枚举同样拒绝。

输入使用标准库 [tomllib.loads](https://docs.python.org/3.12/library/tomllib.html)，先限制 bytes，解析后迭代限制节点、深度、字符串和集合；所有失败转换成不含原文的 `InstallProfileError.code`。模型复用 `StrictRecord`，内层集合使用 tuple。`IntentLimits` 集中提供有限上下界。当前没有 ResolverContext、CredentialReference 采集或合并，也没有接运行入口、发行 manifest 或兼容格式声明。

TOML 解析段也将 CPython 整数字符串转换限制产生的裸 `ValueError` 归一为
`invalid_receipt`；递归超限仍为 `receipt_budget`。回归根据当前解释器限制生成多一位的
十进制样本，最多 8193 位；限制关闭或超过该固定预算时明确 skip，不修改进程全局限制。

## 研究依据与版本边界

已用 Context7 核对 uv 工具 receipt、升级和配置语义，并阅读官方源码。研究 adapter 锚定 [uv 0.13.0](https://github.com/astral-sh/uv/releases/tag/0.13.0)，提交 `4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17`；这是上游格式研究基线，不是本机安装证据，也不是所有 uv 0.x 的兼容承诺。未在本仓发现可复用的 uv Rust 源码缓存，不继续扫描用户目录。

[receipt 根结构](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-tool/src/receipt.rs)没有独立的 producer 版本或 schema 版本。未来调用者必须提供受核验的 uv 二进制身份、版本和明确 adapter ID；不能靠文件名或 TOML 能解析就认定兼容。当前 uv 版本也不能证明这个 receipt 最初由该版本产生：只有完整匹配已审格式的输入才可归一化，旧字符串格式等分支必须单列支持或拒绝。

本地 `updates.py` 仍通过原位 `uv tool upgrade --no-python-downloads muli-agent==目标` 更新。官方 [upgrade 实现](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-tool-commands/src/upgrade.rs)会复用 receipt 要求与选项；新的候选安装不能假定自动继承另一个环境的这些内容。本片不改原位更新行为，也不调用它来试探格式。

## 后续完整 Profile 接口与结论

本节及以下完整 Profile 合同属于后续设计，不是当前纯解析器已交付的能力。后续归一化输入还需已核验 uv/安装身份、显式提供的非秘密解析上下文和凭据来源引用。它仍不自行发现配置、读取环境变量、打开文件、调用 uv、构造 Store 或访问网络。文件与环境采集、候选命令生成分别留给后续受限适配器；不在本片实现另一套安装器。

建议输入由 `ReceiptInput`、`ResolverContext`、`PythonBinding` 和 `CredentialReference` 四部分组成；输出不可变 `InstallProfile` 或固定拒绝代码。字段具体命名在实现前定稿，本稿 JSON 是 fixture 合同示意，不是已上线 API。

| 输出 | 含义 |
| --- | --- |
| `profile_complete` | 已知格式的安装要求、解析策略和来源缺口均得到明确处理；仍未判断目标可解、凭据可用或候选可执行 |
| `context_unproven` | 缺失非持久配置或来源信息，不能用管理进程当前继承值补猜 |
| `profile_conflict` | 显式上下文彼此冲突，或与已持久意图不一致，不能静默覆盖 |
| `unsupported_*` | 不认识版本、字段、来源或选项语义；拒绝继续，不能丢掉该部分后重试 |

区分字段省略、显式 false/空集合、无法确认和不适用；不能都转成 `None` 后填当前默认值。错误只含固定 code、有限字段路径和 adapter ID，不回显 TOML、URL、需求原文或异常 repr。

## 保留原意图，不把部署 pin 写回原意图

[Tool 字段](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-tool/src/tool.rs)中 requirements 首项是主工具，其余来自 `--with`；还有 constraints、overrides、excludes、`build-constraint-dependencies`、python、entrypoints 和 options。requirements 也有历史 PEP 508 字符串分支。首期只支持已核结构化分支，不能套用 lock 文件的 `version`/`source` 格式。

Profile 分成三组，不能互相覆盖：

1. `intent`：原始主 requirement、extras、按原顺序的 with/constraints/overrides，以及已支持的其它要求。确认主工具唯一且为规范分发名 `muli-agent`；markers 原样保留，不按当前机器提前删条目。extras 可按集合规范化，索引和要求数组不能为生成摘要而任意排序或去重。
2. `resolver`：已冻结的索引顺序/策略、解析与安装选项、非秘密上下文和来源引用，附各值来自 receipt、显式输入还是本次采用的当前配置。
3. `evidence`：adapter、uv 二进制和当前安装身份、receipt 变化检测证据、Python 绑定、来源引用及该 profile 的语义摘要。候选目录、目标版本与构件摘要属于后续部署记录，不能成为下一轮的原始 requirement。

原要求 `muli-agent[mcp,channels]>=2.18,<3` 与候选 `==某版本` 必须同时满足；不能用精确 pin 替换原要求再声称保留了意图。原要求已精确 pin 到另一版本时，应明确冲突；改变原约束是新的安装意图，不是自动升级的修复手段。候选 receipt 中新增的部署 pin 也不能在下一轮冒充主人原来的 pin。

首次采集的原 receipt 与后续活动候选 receipt 分别留证。下一轮仅在活动安装、上次部署 receipt 和原 profile 摘要具有已核管理链时复用原意图；不能因候选的新 pin 就丢掉旧意图，也不能拿孤立旧 profile 去忽略外部重装。链缺失返回 `context_unproven`，不编辑 uv receipt 或猜 pin 的来源。

纯归一化器不自己用正则实现 PEP 440/508 求交，也不根据字符串比较宣布目标符合约束。后续受限解析必须同时施加原始约束和目标 pin，并核实际选出的主包版本；解析失败就失败，不能去掉 extras/with/constraints/overrides 再试。现有直接依赖没有声明通用 PEP 解析库，未来若确需纯语义判定，应先确认可复用的已声明依赖或单独评审依赖变更。

[Requirement 序列化](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-distribution-types/src/requirement.rs)包含 name、extras、marker 及展开的来源字段，原始 origin 不持久化。首期接受 registry 来源；groups/conflict 非空、VCS、直接 URL、本地路径、editable、virtual 或未知来源均拒绝。不能把本地/远端输入换成同名 registry 包以求成功。entrypoints 只用于核验已有安装，不授权往记录中的任意路径写候选入口。

## 解析上下文与 receipt 的缺口

[工具配置规则](https://docs.astral.sh/uv/concepts/configuration-files/)与普通项目不同：tool 命令忽略项目配置，只发现用户/系统配置；显式 config-file 替代发现，no-config 禁止发现。环境覆盖配置，命令行覆盖环境。已核 upgrade 路径中，参数解析所得的显式 CLI/环境选项优先于 receipt，receipt 再优先于用户/系统文件；这个层级只适用于相应选项，不能推广成 receipt 保存了全部全局设置。因此不能将发现的几个 TOML 字典直接合并，也不能把当前 shell 的所有 `UV_*` 原样传入候选。

Receipt 保存的是部分解析结果，不是原安装命令、完整配置来源清单或环境锁。以下信息必须另行处理：

| receipt 单独不足的证据 | 最小处理 |
| --- | --- |
| producer 版本、当前 uv 二进制及其与 receipt 的关系 | 显式 adapter 与只读二进制证据；未识别版本/分支拒绝，不推断同一大版本都兼容 |
| 当时 CLI、环境、config-file/no-config 与用户/系统文件的来源、顺序 | 保存当前已核上下文及来源；不能标成已恢复当时全部上下文 |
| 没写入 receipt 的默认值、全局/TLS/发现设置，或后来改变的当前配置 | 需明确采用本次已核上下文，或者返回 `context_unproven`；不能悄悄沿用漂移值 |
| requirements/constraints/overrides 的输入文件、include 链、hash 指令和原始来源 | 不从展开后的条目重建原文件；需要依赖这些信息的模式缺证据即拒绝 |
| 显式 Python 请求与实际解释器、平台、实现及 ABI | 复用安装 manifest 的解释器证据，另核 uv；不把省略 python 解释为允许重新发现/下载任意 Python |
| 传递依赖、实际 wheel/hash、外部 pip 修改、可选锁内容与完整环境漂移 | Profile 不冒称 lock 或当前环境完整证明；后续另核候选实际依赖与构件 |
| 用户名/密码、NETRC、凭据存储、keyring、证书和信任根来源 | 只记录来源引用及非秘密策略，认证可用性由后续授权执行器核验 |
| 用户在 MuLi 管理之外重装工具或修改原要求 | 当前安装/receipt 身份变化使旧 profile stale；不能用旧 profile 覆盖新意图 |

“采用当前已核上下文”是明确的 provenance，必须由受信宿主输入，不是模型可自报的布尔豁免；它不宣称还原历史，也不允许覆盖 receipt 已记录的相反要求。这样可接纳历史上没有完整来源清单的正常安装，同时对真正影响行为的未知项保留拒绝。原安装要求仍以核验过的 receipt 为准。

[ToolOptions](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-settings/src/settings.rs)只保存解析/安装选项的一个子集。实现必须有字段级白名单和转换测试：

| 类别 | 保留与拒绝规则 |
| --- | --- |
| index、index-url、extra-index-url、no-index、find-links、index-strategy | 保存原顺序、显式默认/专用索引和认证策略；不能按 URL 去重、重排或把多个索引折成一个 |
| resolution、prerelease 及按包例外、fork-strategy、exclude-newer 家族 | 按 adapter 的确切类型/默认语义保存；相对时间保留原意图，另冻结本次准备的基准，无法等价重放就拒绝 |
| no-sources、no-build、no-binary 及按包例外、link-mode、compile-bytecode | 不丢包级设置；与未来禁止源码构建的宿主策略冲突时拒绝准备，不能放松任一方 |
| build constraints、excludes、dependency-metadata、config-settings、build-isolation、额外构建依赖/变量、torch-backend 等 | 都是已知影响行为的输入；首片未实现语义的非默认值返回 `unsupported_option`，不透传任意键值或默默删除 |
| 未知根键、未知 tool/requirement/options/index 内层键或新枚举值 | 一律拒绝，即使上游某层容忍未知字段也不放行 |

后续支持矩阵须与该 adapter 的源字段逐项对齐；当前只有前述四个 options 与有限要求字段实现了纯保留，其它选项明确拒绝。不能把此表的后续保留要求当成已实现转换，不扩成 uv 全配置解释器。

## 索引与凭据来源

[索引规则](https://docs.astral.sh/uv/concepts/indexes/)包含顺序、default/explicit、认证策略和 first-index 等解析策略。保留 URL 不等于保留这些语义。named index 的环境变量名转换还可能碰撞；两个索引生成同一凭据变量名时拒绝，不让它们意外共享认证身份。

[HTTP 认证](https://docs.astral.sh/uv/concepts/authentication/http/)还可来自 NETRC、uv 凭据存储或 keyring；receipt 不能提供完整恢复证据。上游升级仅在特定匹配索引条件下从当前配置恢复认证，不能把 URL 相近、同域名或去掉路径后的地址视为同一授权目标。

`CredentialReference` 只含已核索引身份、来源种类和宿主控制的 source ID；named-index 环境来源可含预期的变量**名称**。不保存或输出值、URL userinfo、完整环境、credential 文件内容、keyring 输出、客户端私钥或其可公开枚举的散列。公开状态最多给固定 code、索引数量和无秘密的 profile 摘要；私有来源定位也不能由模型任意指定文件或程序。

本稿不会读取真实来源。未来纯模块只能验证引用结构与索引绑定，不能据此声称密码存在、仍有效或能够访问仓库。认证执行器未支持的来源返回 `unsupported_credential_source`，不能改为匿名/另一个仓库试试；keyring subprocess、native auth 等额外机制也不能因当前环境恰好存在而自动启用。

[证书配置](https://docs.astral.sh/uv/concepts/authentication/certificates/)同样影响来源信任：SSL_CERT_FILE/SSL_CERT_DIR、system-certs 和 SSL_CLIENT_CERT 必须有明确策略与引用。客户端证书可能包含私钥，不能随索引配置复制进事件。首期 registry URL 限定可验证 HTTPS，拒绝 userinfo/query/fragment 和无法证明无秘密的地址；敏感或未知地址只给固定拒绝码，不回显。来源引用可允许凭据轮转，不把凭据值冻结进 profile。

## Python、隔离与后续效果边界

Receipt 只在显式 Python 请求存在时保存它，见 [install 实现](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-tool-commands/src/install.rs)。Profile 同时保留这个请求和安装 manifest 已核的实际解释器绑定；省略请求仍应选已核基础解释器，不能因新 uv 默认变化而换 Python。请求和实际绑定不一致时拒绝。解析语义不由模型任意构造 argv 决定。

后续候选准备才把完整 profile 编译成 argv 与受控环境：候选拥有的最终 tool/bin/cache/temp 路径、现有基础 Python、禁止下载 Python、保留全部支持的原要求，并在发送效果前复核 uv/安装/receipt/非秘密上下文身份。禁止默认源码构建的策略不能仅靠一句 `--no-build` 就宣称任意本地/可编辑来源不会执行后端；本片已拒绝这类来源。参考 [tool install CLI](https://docs.astral.sh/uv/reference/cli/#uv-tool-install)。

这些效果、资源/磁盘预算、产物核验、兼容元数据、稳定入口和 protocol 2 交接都不在本片。Profile 完整不放宽 protocol 1 的单安装身份，不触碰当前 uv 环境或公共 bin，不提供数据库迁移/恢复能力。

## 输入上界与持久化

当前 `IntentLimits` 默认：receipt 256 KiB；主/with/constraints/overrides 条目合计 256；options 中索引最多 8；entrypoints 最多 32；每条 extras 最多 32；单字符串最多 2 KiB；节点最多 4096；容器深度最多 16。先限制 bytes，再解析，再限制节点/字符串/集合数；深度/节点约束不用递归错误报告实现。阈值集中配置且有有限硬上界。未来若持久化完整 profile，另限制总 bytes 和来源引用数，不自动沿用未实现的默认值。

无目录扫描、无 include 递归、无 URL 拉取、无秘密探测和无环境全量复制。未来 parser 复用标准 TOML 解析与已有严格模型规则；不自造通用配置解析器。重复 TOML 键、未知类型、超限及未知枚举拒绝，不截断后接着安装。

真正需要保存时只写私有有界 profile，原始 receipt 变化检测摘要仅作为私有证据；不将可能含敏感输入的原始 bytes 或散列送模型/公开事件。对外 profile 摘要仅覆盖已核的非秘密语义。先完成 profile 耐久发布再允许候选效果，效果前检查 stale；本稿不新增 journal 状态、Store 表或写入入口。

## 合成 fixture 格式

当前 `tests/test_install_profile.py` 内嵌合成 receipt 和参数化变体，不创建或读取 fixture 文件。后续完整 Profile fixture 目录可采用 `tests/fixtures/install_profile/uv-0.13.0/<case>/`，每例仅有 `receipt.toml`、`inputs.json`、`expected.json`。全部路径/域名为合成，不能从真实 receipt “脱敏后复制”作为默认来源。下文 `fixture_version=1` 只版本化未来测试输入，不是 uv schema 或已发布 InstallProfile 格式 ID。

最小 receipt 使用官方结构化 requirement 的 `specifier` 键，而不是 lock 的版本字段；以下只展示合成文本，尚未生成或解析 fixture 文件：

```toml
[tool]
requirements = [
  { name = "muli-agent", extras = ["channels", "mcp"], specifier = ">=2.18,<3" },
  { name = "httpx", specifier = ">=0.28,<0.29", marker = "python_version >= '3.12'" },
]
constraints = [{ name = "httpx", specifier = "<0.29" }]
overrides = [{ name = "idna", specifier = "==3.10" }]
python = "3.12"
entrypoints = [
  { name = "muli", install-path = "/fixture/current/bin/muli" },
  { name = "MuLi", install-path = "/fixture/current/bin/MuLi" },
]

[tool.options]
index-url = "https://packages.example.invalid/simple"
index-strategy = "first-index"
```

以下 `inputs.json` 是**未来完整 Profile** 的上下文 fixture 设计，当前 API 只接收其中声明的版本与 receipt bytes，不消费该 JSON，也不产生其上下文核验结论。它不塞 secret 值，fixture 引用不是经过真实核验的主机身份：

```json
{
  "fixture_version": 1,
  "origin": "synthetic",
  "adapter": "uv-tool-0.13.0",
  "upstream_commit": "4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17",
  "uv": {"version": "0.13.0", "binary_ref": "fixture:uv-binary"},
  "installation_ref": "fixture:current-installation",
  "context": {
    "adoption": "freeze_current_verified_context",
    "discovery": "no-config",
    "resolver_environment": [],
    "sources": []
  },
  "python": {"requested": "3.12", "binding_ref": "fixture:base-python"},
  "credential_refs": []
}
```

当前测试明确断言 `intent_parsed` 和 `context_required`，并核完整保留的主要求/extras/with/constraints/overrides/索引顺序；不能只断言“不抛错”。未来的 `expected.json` 才能在完整上下文证据齐备时断言 `profile_complete`，仍不能给候选可选结论。认证专项 fixture 可另提供来源引用，例如 `{kind: "named_index_env", index_ref: "fixture:corp", username_env: "UV_INDEX_CORP_USERNAME", password_env: "UV_INDEX_CORP_PASSWORD"}`，没有这些变量的值，不真的读取环境。named-index 原始 TOML 必须按同一 pinned adapter 的实际序列化格式单独核验后再加入，不拿示意 JSON 当 uv 的格式。

| 必备 fixture 组 | 必须证明的行为 |
| --- | --- |
| extras/with/markers、registry constraints/overrides、多索引 | 字段与顺序保留，不提前删除条件条目；主工具之外的依赖不能遗失 |
| 原要求范围、原精确 pin、连续两个候选部署 pin、外部重装/管理链丢失 | 原意图不变且来源链必须匹配；纯模块不冒称目标可解；不能用新 pin 抹旧约束或用旧 profile 掩盖外部重装 |
| 未知 uv/旧字符串/未知各层 key/未知枚举/重复 TOML 键 | 固定拒绝；不利用上游宽松字段解析吞掉未知含义 |
| 显式参数/receipt/用户/系统上下文，数组合并、config-file/no-config | 遵循 pinned 规则；缺来源返回不完整；冲突不按当前 shell 猜一个值 |
| 缺认证引用、索引规范身份不匹配、环境名称碰撞、TLS 来源变化 | 不用其它索引/匿名认证兜底；不输出秘密，不自动读取凭据 |
| python 省略/显式请求、实际绑定变化 | 省略不等于重新选择 Python；变化标 stale，不下载或切换解释器 |
| hash 约束来源丢失、可变/未知来源、构建自定义 | 缺证据或未实现语义即拒绝，不“简化”为普通 registry install |
| 每项上界与总 bytes/节点/深度边界 | 超限拒绝，不产生截断 profile 或失控错误输出 |
| 禁止文件/环境/进程/网络访问的替身 | 纯归一化入口只消费已给输入，不能暗中发现或探测本机来源 |

## 交付次序与验收边界

本轮交独立纯 parser、合成回归和本设计；上述涉及来源合并、候选 pin 连续性、凭据和上下文的 fixture 仍是后续验收要求，当前不冒称已经覆盖。之后再分别交付有界只读采集、完整 profile、候选准备和 A/B 交接；每一步沿用现有安装/兼容/资源边界，不为通过意图解析放宽真实数据与外发安全。

2026-10-21，经共享 `scripts/check_local.py` guard 串行验证：115 项合成回归通过，用时 0.12 秒、峰值 54.5 MiB；两文件 Ruff、格式检查与严格 mypy 通过，静态检查峰值 185.3 MiB，全部 swap 0。有限整数反证在修复前实际以裸 `ValueError` 失败：当前上限 4300 位，4301 位样本仍在 receipt bytes 预算内；修复后固定拒绝且不回显原文。测试还覆盖字段/顺序保留、缺省与显式空值、版本/来源/URL、严格类型、输入上界、不可变集合及禁止文件/进程/网络等隐式访问；未实际读取 fixture 路径或凭据。

尚未验证：真实本机 uv 版本与安装来源、真实 receipt 样本、实际索引/认证、完整上下文合并、候选解析与安装、依赖完整性、跨版本启动/切换/回退。纯解析通过不能作为这些能力的验收证据。
