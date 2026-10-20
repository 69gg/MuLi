# 本地托管事务准备

`ManagedPreparation` 只准备已保留在本机的安装，不创建环境、不运行候选解释器、不下载、
安装或迁移，也不调用 helper、systemd 停启或重新加载。它返回
`PreparedManaged(record, anchor)`；宿主随后依据同一 journal 的实际当前代调用
`ensure_managed_helper`。本模块本身不代表管理 API、启动与真实服务的整链已验收。

## 最小宿主接口

```python
preparation = ManagedPreparation(runtime, service)
async with runtime.configuration_lock:
    prepared = await preparation.prepare(
        persisted_plan,
        validated_target_settings,
        candidate=LocalSlotPaths(prefix, python, package_root),
    )
```

`runtime.boot_session` 与 `runtime.maintenance.permit` 必须是同一受支持启动所有者，
真实 PID、InvocationID、标准 unit 的进程观察仍由已有 systemd/proc 探针完成。
Store、writer lease 和 journal 由宿主持有，本模块不取得第二份生命周期所有权。
调用方已持配置锁；模块不通过“锁已上锁”证明任意不合作调用者的任务身份。

`adopt` 和 `switch` 必须显式提供 retained prefix、词法 Python 入口和包目录三路径；
`configure/restart` 禁止提供候选路径。stage 接受计划仅表示输入和支持面初检通过，
不表示已核候选可切换；所有实际准入集中在 `prepare`，避免另一套易漂移的验证器。

四个动作复用当前七领域格式与 V2 准备合同：

| 动作 | 当前 base | target |
| --- | --- | --- |
| adopt | 实际运行 retained slot、精确标准 legacy unit、selector 缺席 | 同 slot、同配置、固定 launcher 的 managed unit 与显式 selector |
| switch | 已托管的当前实际 slot/config/selector/unit | 显式本地另一 slot、原始配置 bytes、同稳定 unit/anchor/controller |
| configure | 已托管的当前实际绑定 | 同 slot、目标配置、同 selector/unit |
| restart | 已托管的当前实际绑定 | 三份原始 bytes 与 slot 不变 |

slot 使用 `register_local_slot` 读取实际包内兼容声明与有限文件指纹，当前安装额外使用
`verify_current_slot` 核词法入口、prefix、当前真实 import 路径。两个声明必须经
`assess_compatibility(..., handoff_protocol=2)` 允许当前完整持久格式且无需迁移。
声明必须随真实保留安装提供；准备器不会为旧包生成声明或用版本号猜格式兼容。
兼容声明不证明 uv resolver 上下文可重建，`install_profile` 的未准备状态不被伪造。

首次 adopt 从受支持真实 V1 manifest 核当前 slot 与原服务；V1 终态迁入还必须已有
实际 Store 的精确投影回执。先前失败 adopt 已回到 legacy 时，重新捕获实际 V1 安装
验证 permit，不能将 V2 record 自报的 slot/路径当授权。后续动作复用稳定 launcher、
anchor 和 controller；不执行非当前旧 target 的包文件来阻碍健康 base 的恢复。

## 写入顺序与故障

准备前从真实 Store 按主键有界重读计划，核原 request/action/status/routing；来源采用
已持久计划，调用参数不能换回复地址。旧代必须 resolved，且同实际 Store 中固定
generation/revision/store_instance 回执精确相符。未投影终态、未决代、坏/缺格式或
主身份、外来路径、文件变化均拒绝；不创建身份、不补格式、不修旧 journal。

首次读取和最后验证均调用已准入 Store 的 `revalidate_format()`，核现有三组完整构造
结构与局部 marker；完整格式证据读取本身不能替代该 schema 合同。`switch` 只换 slot，
不能夹带 Settings 变更；除 `configure` 外均保留当前原始配置 bytes，不重序列化。

真实 systemd 当前进程探针的 await 完成后，重新核候选、当前安装、完整格式、Store/main、
实际文件和目标主动投递偏好。此后到返回不再 await，按顺序执行：

1. 在原 journal lock 与真实 writer 下复核旧代和持久期限。
2. 用已有 WAL/FULL `Store.event` 先提交原计划 `applying + transaction.generation`。
3. 发布有限、按摘要寻址的静态 slot/anchor 与固定 launcher。
4. `prepare_managed` 在快照前和最后 state 发布前调用宿主验证，写原 journal 快照和
   唯一 `state.json`，保留仍被旧代引用的证据。
5. 再核实际文件、Store 和本代记录，返回 record/anchor。

计划提交失败时不发布材料或 state。计划已提交、state 尚未发布时，保留 applying 与
generation，宿主恢复应按“找不到匹配 journal”处理 interrupted，不制造完成。
prepared 已发布后任何失败均保留该代和 matching plan，由现有 recovery/ensure 路径
接续；不写 completed、不回滚外部文件、不另起代或延长期限。相同 request 若已存在
于当前 journal，明确要求恢复该代，不能重新准备以续期。

只有 resolved 且实际 Store 已精确投影，当前 config 才可不同于旧终态快照；新一代
捕获实际当前 bytes 为 base。selector、unit、slot、anchor、完整格式始终保持原绑定。
快照和状态同步沿用已有 poison/adoption 语义，未确认耐久性不能报告 prepared 成功。

## 验证边界

新增回归使用临时真实 WAL Store、writer/journal、静态文件与合成 retained 安装，
systemd/proc 和服务动作边界为 fake。未访问真实用户数据库，未执行解释器、helper、
模型或真实服务。已通过统一资源守卫串行验证：新增准备测试 27 项与既有 journal
测试 29 项，共 56 项，pytest 1.24 秒、service 1.753 秒、峰值 81.6 MiB、swap 0。
3 份 Python 文件 Ruff/format、两份源码 strict mypy 与 diffcheck 均通过；类型检查
峰值 134.3 MiB。首次静态检查指出 import 排序和局部 Role 推断，已修复后复验。

覆盖四动作、真实 V1 终态投影后迁入、失败 adopt 重入及外来 permit 拒绝、精确回执
后的热配置成为新 base、候选格式不兼容、probe await 内变化、真实 SQLite COMMIT
拒绝与回滚、快照后/状态后故障保留、取消和相同 prepared request 不续期。独立审查
补充的两项也已覆盖：实际构造索引在准备前/探针 await 内损坏均拒绝；仅换 slot 的
switch 保留非规范 JSON 原始配置，并拒绝夹带 Settings 变更。
V2 完成夹具使用真实 Store/启动准入/投影，服务效果为合成前提；测试树尚未包含根
Runtime/Maintenance 的 V2 分派，夹具仅将其 current 读取绑定到已有 `read_current`。
