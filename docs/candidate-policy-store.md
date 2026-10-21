# 候选准备策略的固定 Store 对象

本片实现独立的策略读写仓库，尚未接入公开工具、管理 API 或候选准备执行链，也不单独作为可发布功能。它沿用当前共享数据库的普通管理对象，不向 `Settings`、`config.json` 或插件配置增加字段。

固定 controller 可能仍运行 2.22 包；其 `transaction_url()` 会用该版本的 `Settings.model_validate_json()` 完整解析冻结配置。新增嵌套 `candidate` 配置即使关闭也会随默认值保存，导致旧 controller 的 nonce HTTP 入口和旧 base 启动拒绝配置。把策略独立存在 Store 可保持这一既有格式合同。

## 接口与持久格式

`CandidatePolicyStore(store)` 借用调用者已打开的 `Store`，不创建连接、目录、配置文件或锁：

- `read() -> CandidateSettings` 返回完整策略，包括明确配置的三个路径，供经过认证的策略管理入口往返读取。固定对象缺失时返回 `CandidateSettings()`，即关闭且没有选择路径；读取不写默认对象或事件。
- `replace(policy: CandidateSettings) -> CandidateSettings` 完整替换策略；没有 JSON merge 或继承旧路径的副作用。先读取已有对象，未知版本或坏数据都明确拒绝，不借完整替换偷偷重置。
- `policy_revision(policy) -> str` 返回完整、重新校验后的策略 canonical JSON 的 SHA-256。它是值摘要，不是新的 revision 账本，也不是候选准备的 `ExpectedPolicy.revision`；后者还绑定真实来源和材料。

对象固定为 `kind=update_policy`、`id=candidate-v1`：

```json
{"id":"candidate-v1","schema_version":1,"policy":{"enabled":false,"source":"local-wheelhouse","wheelhouse":"","retained_root":"","uv_executable":"","disk_mode":"threshold","limits":{}}}
```

上例中的 `limits` 仅为简写。实际完整替换会保存 `CandidateSettings` 的全部当前缺省值和 `CandidateLimits` 字段；三个路径原样保留，每项沿用模型的 4096 字符上限，关闭时也允许保存已配置路径。所有字段沿用候选策略模型的严格类型、路径词法和预算校验，不据此认证路径所有者、wheelhouse、uv 或安装身份。

读取复用 `_read_object`，只查询固定主键的长度与至多 16 KiB 加一个探测字节，不调用无界 `Store.get()`、`list()` 或扫描其他对象。坏 JSON、重复键、未知字段、非精确整数版本、未知版本、错误 ID 或无效策略均返回固定 `candidate_policy_invalid`；超预算返回 `candidate_policy_budget`。SQL 读取失败返回 `candidate_policy_read_failed`，不能当作对象缺失或正常关闭状态。归一后的完整对象也必须可由 `Store._put()` 的实际 JSON 格式在 16 KiB 内保存，预算包含 UTF-8、转义和分隔符。错误不包含原策略或路径。

## 原子性与宿主责任

完整替换重新校验模型字典，不能通过 `model_copy(update=...)` 绕过。它调用一次 `Store.event()`，在同一个数据库事务内保存对象和 `update.policy.changed` 事件；事件仅含固定 ID、启用状态和策略摘要，不把路径、requirements 或完整策略再次送入全局事件流。摘要事件插入失败时策略写入也回滚。

替换拒绝已有外层事务，以及不符合 Store 既定语义的 autocommit/`isolation_level=None` 连接，避免提交调用者事务或把两次写入拆成非原子效果。仓库不更改连接 PRAGMA；数据库耐久策略和 writer lease 继续由宿主管理。

公开工具/API 的认证、任务权限、配置锁、关闭及维护门由宿主接线。策略配置本身不批准安装。候选审批仍需显示并实核 `expected_policy`；宿主在每个效果和相关 await 后必须从真实 Store 重新读取策略，并与冻结值比较，再同时复核当前 slot/配置/selector/Store/lease 的来源观察。只重新散列已冻结的 `CandidateSettings` 不能发现持久策略被修改。

`read()` 的完整返回值仅用于认证管理入口。安全状态摘要应由宿主另行组成，不把策略虚拟键塞回 `/api/settings` 或真实 `Settings` JSON。

## 旧版本与恢复边界

2.22 的普通 Store 使用相同对象表，旧恢复逻辑只处理明确的 session/run/approval 等领域，最终管理投影只更新原 plan 与固定 projection receipt。它们不会把未知 `update_policy` 对象当作配置、管理任务或需要删除的记录。事务回退仅还原冻结配置、selector 与 unit，不回滚共享最新数据库，因此策略可以保留，旧包也不会因不认识策略而执行候选准备。

本片回归核对未变更的 2.22 `Settings` JSON 在策略保存前后字节不变，并复用真实临时 Store/旧投影逻辑确认未来版本策略对象在两次最终投影后仍原样保留。这不是另行安装旧版本的跨版本进程实验，也不认证旧版本任意插件或外部写入者的行为。

## 验证

所有检查经根目录 `scripts/check_local.py --seconds 30 run -- ...` 串行执行，单次 1 GiB、共享 2 GiB、swap 0、共享一个逻辑 CPU：

- `test_candidate_policy_store.py` 的 34 项新回归与 `test_candidate_policy.py` 的 13 项既有回归共 **47 passed**，0.23 秒；守卫服务运行 739 ms，峰值 64.8 MiB，swap 0。
- 两份 Python 文件的 Ruff 与格式检查通过；新模块严格 mypy 通过，守卫服务运行 319 ms，峰值 122.4 MiB，swap 0。

回归包括缺失只读、坏/未来对象拒绝、固定主键有界读取、三个各不超过 4096 字符的路径组合后因 UTF-8/转义超预算、三项最长 ASCII 路径仍可保存、未验证模型复制重新拒绝、原子写入失败回滚、外层事务保留、marked 库无迁移重开、冻结配置字节不变和未来对象经过两次旧投影仍保留。所有材料仅为临时数据库与词法路径，不读取真实 receipt，不启动服务或 uv，不下载依赖。
