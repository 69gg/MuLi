# 跨版本兼容元数据与纯判定

状态：纯判定已供 protocol 2 宿主复用；2.22 新增真实静态声明，源资产、发行校验反证、沿用该资产的两条进程回归，以及实际 wheel/sdist 离线构建和源码目录外安装 smoke 均已通过受限检查，远端发行状态见[2.22 审查](review-v2.22.md)。声明不代表任意历史数据库已经获得跨版本兼容认证，也不证明某个候选环境已经可用。总体边界见[可恢复管理设计](recoverable-management-design.md#数据兼容与回退边界)。

## 本片的边界

`muli.compatibility` 只接收有限元数据，不打开文件或数据库，不构造 `Store`、`Memory`、`ConfigFile` 或 `Runtime`，不安装、迁移、备份、恢复、启动或停止任何组件。2.22 的静态声明是包内独立资产，不改变纯模块的职责，也不自动给旧库补格式标签。

这项区分直接对应现有代码：legacy 的 `Store._initialize` 会建表、建索引及补消息序号；`Memory` 会补元数据、别名证据与搜索索引。`message_sequence_v1` 和 `alias_evidence_version` 分别证明局部迁移事实，不能当成整库、配置或外发语义相同的证据。完整标记的来源、[连接内格式证据读取器](format-evidence.md)、[禁止迁移构造器](store-existing.md)与[真实构造准入](fresh-format-admission.md)分别承担证据职责；纯判定本身没有这些能力。

## 随包声明与发行检查

[`src/muli/compatibility.json`](../src/muli/compatibility.json) 声明 `muli-agent` 2.28.0、`metadata_version=1`、交接协议 2、无需迁移，以及七领域各 `reads=[1]`、`writes=1`。这些 ID 对应 [`CURRENT_FORMAT_STATE`](../src/muli/format_admission.py) 及[七领域合同 1](fresh-format-admission.md#七领域合同-1)；`install_profile=1` 仍允许尚未准备 profile 的缺省状态，不生成 receipt、不证明 uv 环境或解析上下文。版本整合须同步 `pyproject.toml`、`muli.__version__` 和声明中的 `release`。

现有 Hatch `packages=["src/muli"]` 将包内非 Python 资产一并选入，不需要另造 manifest 生成器或增加强制覆盖规则，见 [Hatch 文件选择](https://hatch.pypa.io/latest/config/build/#file-selection)。构建必须保留源码中的真实文件；进程回归复制整个有界 MuLi 包并沿用它，不再临时写出一份自报声明。

现有 CI 继续构建一次，并将同一 wheel/sdist 交给发布作业。`scripts/check_wheel.py` 在源码目录外安装 wheel 后，先在安装目录有界读取声明，复用 `CompatibilityManifest.from_json` 核严格 16 KiB 合同、正式版本、协议与七领域精确读写 ID，再构造临时应用做原有 smoke。它证明被测安装包携带本版本声明，不认证候选依赖环境、真实服务或历史库。

`scripts/release.py` 保持标准库入口，不导入应用或 Pydantic。它核源码声明的 `release`，并要求 wheel 中的 `muli/compatibility.json` / `muli/__init__.py` 和 sdist 中同一包路径的两文件均唯一、各不超过 16 KiB、原始 bytes 与源码完全一致；sdist 两文件须为普通文件。原有名称、METADATA/PKG-INFO、标签与 CHANGELOG 校验继续保留。声明的完整语义由前序安装包 smoke 负责，此处只补同一受测构件的内容与版本一致性，不扩大成通用压缩包沙箱。

## 格式合同

`CompatibilityManifest` 包含必填 `metadata_version=1`、`distribution="muli-agent"`、`release`、`handoff_protocol`、`requires_migration` 以及七个完整的格式领域。`FormatState` 包含必填 `metadata_version=1` 和这七个当前实际格式 ID。没有证据时调用者传 `None`，不得从表存在、程序版本号或最近事件猜一个 ID。

| 领域 | 对应的完整持久语义，不能只核其中一张表 |
| --- | --- |
| `database` | SQLite 结构、约束、索引依赖与各持久标记的解释 |
| `objects` | 对象/消息/事件 JSON、任务/审批/恢复结论、原始证据及路由 proof |
| `configuration` | 配置字段、默认值、校验及授权含义 |
| `ingress` | 收件状态、幂等、claim/run 归属、principal、FIFO 与完整原路/绑定证明 |
| `outbox` | 发送模式、幂等比较、已发送/未执行/unknown、原路授权及撤销、显式重试规则 |
| `memory` | 来源、版本、修订、遗忘及其与主动投递授权的关系 |
| `install_profile` | 原安装要求、extras/约束/索引及凭据来源引用、receipt 与入口选择解释 |

每个领域的 `FormatSupport` 必须显式列出 `reads`（1–8 个不重复 ID）和一个 `writes`，且写格式必须可读。ID 是 1–2147483647 的严格整数，是命名空间内的合同标签；支持 3 不意味着支持 2，不按大小推导兼容，不接受 `true`、浮点数或数字字符串。合同涉及 unknown、not_executed、路由和授权的改变，即使 SQL 一字未改，也必须重新审查相应领域，不能继续沿用未经证明的 ID。

`release` 只接受有界、无前导零的 ASCII 正式版本三元组；不接受预发布号或任意 SemVer 扩展。版本只标明声明属于哪个发行版，判定既不比较版本大小，也不据其推定格式。元数据版本、格式 ID 和交接协议版本分别独立。

对于来自 JSON 的输入，应使用 `CompatibilityManifest.from_json(raw)` / `FormatState.from_json(raw)`。二者复用 journal 的严格 JSON 检查，拒绝任意层级重复 key、非有限常量、未知/缺失字段及不支持的元数据版本；单份原始输入最多 16 KiB。模型与内层 tuple 不可变。格式错误抛固定 `CompatibilityError.code`，不把原始数据复制到错误消息中。Python 构造器供受信调用者使用，不是元数据来源认证接口。

通过 Context7 核对 [Pydantic JSON 严格模式](https://docs.pydantic.dev/latest/concepts/json/)：`model_validate_json` 可以将 JSON 数组解析为 tuple，同时保留严格字段类型；不能先将 JSON 转成普通字典再依赖严格模式接受列表。`frozen` 本身不冻结内层可变容器，因此本片读格式集合使用 tuple，见[不可变模型边界](https://docs.pydantic.dev/latest/concepts/models/#faux-immutability)。

## 纯判定合同

```python
result = assess_compatibility(base, candidate, current, handoff_protocol=protocol)
```

1. 缺少旧版、候选或当前格式证据，明确返回拒绝原因。
2. 调用者的交接协议必须与双方声明完全一致。双方任一需要迁移就拒绝。
3. 七个领域均要求双方能读当前格式，双方写格式均与当前格式完全相同。即使双方都声称可以读一个新写格式，本片仍拒绝改变共享持久格式；那需要另一个已审查协议。
4. 返回不可变 `CompatibilityAssessment`：`format_compatible`、固定 `code`，必要时附有限 `domain` 和 `side`。正结果名为 `declared_format_compatible`，只表示这组声明通过相同格式判定，**不是** `upgrade_ready`、包认证、真实库审计或健康检查。

调用者必须另行保证声明来自核验过的发行构件、当前格式证据真实且在停写边界重新核验。相同的任意自报 ID 并不能证明对应实现遵守合同。新发行版实际采用某组 ID 前，仍需用跨版本样本核验 ingress/outbox/审批/原路/记忆证明等行为，不能靠给表结构起相同标签完成认证。

现有管理 protocol 1 使用单个安装身份；元数据中交接协议数字相同不改变这个限制。protocol 2 的宿主仍须分别核旧/新安装、稳定入口、控制者存活、准入与健康证明。纯判定与静态文件不执行这些操作，也不放宽现有 protocol 1 的身份比较。

回退继续使用包含最新 ingress/outbox/unknown 的当前数据库，只选旧程序及已验证兼容的配置；本片没有把旧数据库备份还原作为兼容分支，也不承诺恢复未保存的 token 或恰好一次外部效果。

## 验证范围

原始纯判定回归使用内存中的测试声明，覆盖七领域、双方读写、未知/缺失/非法版本、迁移和交接协议拒绝、严格有界 JSON、不可变声明以及禁止数据库/文件 I/O。这里的可变格式 ID 和版本仍为夹具。2.22 另增直接读取真实源资产的版本/七领域一致性回归、wheel/sdist 缺失/重复/超限/内容不符的发布反证，并让真实进程回归直接沿用包内资产。

原始纯判定的 145 项定向测试已于 2026-10-21 通过共享 guard，用时 0.15 秒、峰值 53.7 MiB；当时两文件 Ruff 与格式检查、单源码严格 mypy 通过，全部 swap 0。

本轮同样经 `scripts/check_local.py --seconds 30 run` 串行验证：20 项发布反证与 146 项兼容回归共 166 项通过，用时 0.96 秒、峰值 64.9 MiB。五份 Python 的 Ruff、格式检查及 strict mypy 通过，最终 mypy 峰值 186.9 MiB。沿用真实源资产的成功切换与目标拒绝回退分别通过，详见[真实进程验收](managed-process-recovery.md)。所有检查均 swap 0；发布测试中的压缩包是小型合成夹具，尚不能替代实际 wheel/sdist 的构建、安装及字节一致性验收，也未取得跨版本发行包或其它平台的运行证据。
