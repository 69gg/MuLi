# 跨版本兼容元数据与纯判定

状态：独立实现切片，定向验证通过，尚未接入启动、安装或切换入口。它为后续 A/B 更新提供声明格式和纯判定，不代表当前发行版或任意历史数据库已经获得跨版本兼容认证。总体边界见[可恢复管理设计](recoverable-management-design.md#数据兼容与回退边界)。

## 本片的边界

`muli.compatibility` 只接收有限元数据，不打开文件或数据库，不构造 `Store`、`Memory`、`ConfigFile` 或 `Runtime`，不安装、迁移、备份、恢复、启动或停止任何组件。没有随本片发出当前版本的兼容 manifest，也没有自动给旧库补格式标签。

这项区分直接对应现有代码：`Store._initialize` 会建表、建索引及补消息序号；`Memory` 会补元数据、别名证据与搜索索引。`message_sequence_v1` 和 `alias_evidence_version` 分别证明局部迁移事实，不能当成整库、配置或外发语义相同的证据。[连接内格式证据读取器](format-evidence.md)及[三个禁止迁移构造器](store-existing.md)已独立实现，但实际路径/来源认证、完整准入和启动接线仍待完成；纯判定本身没有这些能力。

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

现有管理 protocol 1 使用单个安装身份；元数据中交接协议数字相同不改变这个限制。真正 A/B 切换仍需分别核旧/新安装、稳定入口、控制者存活、准入与健康证明。本片没有接任何这些操作，也不放宽现有 protocol 1 的身份比较。

回退继续使用包含最新 ingress/outbox/unknown 的当前数据库，只选旧程序及已验证兼容的配置；本片没有把旧数据库备份还原作为兼容分支，也不承诺恢复未保存的 token 或恰好一次外部效果。

## 验证范围

新回归只使用内存中的测试声明，覆盖七领域、双方读写、未知/缺失/非法版本、迁移和交接协议拒绝、严格有界 JSON、不可变声明以及禁止数据库/文件 I/O。测试里的格式 ID 和 protocol 2 仅为夹具，不是对现有版本的发行声明。

2026-10-21，经共享 `scripts/check_local.py` guard 串行验证：145 项定向测试通过，用时 0.15 秒、峰值 53.7 MiB；两文件 Ruff 与格式检查、单源码严格 mypy 通过，静态检查最高峰值 199.6 MiB，全部 swap 0。未读取用户数据库、未执行迁移/安装/服务操作，未核验跨版本发行包，也未取得真实 A/B 更新或其它平台的运行证据。
