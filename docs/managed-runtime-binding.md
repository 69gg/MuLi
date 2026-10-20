# 运行期 V2 绑定与终态投影

本片只提供 `verify_running_binding(permit, store, record, role)` 和现有
`project_final` 的 V2 分派。它不执行服务操作、不准备新事务、不发布安装或恢复备份。
Runtime、Maintenance、实际 systemd bootstrap 与健康路由仍由宿主接线；本模块不能
凭 DTO 认证一个进程，也不单独构成 A/B 交付。

## 宿主与数据所有权

宿主先认证真实当前 boot、nonce 和 core Store/main，才调用运行绑定校验。
`permit` 可以是旧 `BootPermit` 或 `ManagedBootPermit`；调用者始终持有同数据库的
真实 writer lease，Store 和 journal 均保持调用者所有，校验器不关闭或替换它们。
Maintenance 可保存绑定了这两个实际对象的回调，无需额外维护一份权威 anchor/slot。

校验重新读取同一 journal 的严格 V2 状态；必须与本次传入记录精确一致，成功同步
journal 目录并复读后才使用状态。它不争 helper 的 controller flock，任何同步失败或
记录变化均固定拒绝，不将可见 rename 当持久激活许可。

`role=None` 仅表示经过认证的 origin boot，以 base 为实际文件合同，且只在既有原服务
观察阶段允许。新 base/target 必须符合当前启动阶段；已决 `committed` 只选 target，
`rolled_back/aborted` 只选 base。未解决代不得把 origin boot 当新角色健康。

## 每次核验的实际证据

校验从记录读取按摘要寻址的稳定 anchor、当前角色 slot 和固定 controller，复用
`DeploymentStore.verify_anchor`、`verify_current_slot` 和现有有限文件系统检查。
它只核当前选中 slot 与 controller，不因故障 target 的文件损坏阻止健康 base 回退。

anchor 路径/预算、完整 slot/声明摘要与记录必须对应；config、selector 和 unit 必须
符合当前角色冻结的精确 bytes 引用，unit 保持原始文本合同。legacy selector 明确
缺席，不能用缺文件推断任意未托管角色。读取格式证据后再核这三份文件及当前记录，
避免检查期间换入其它配置后仍返回成功。

对于 `ManagedBootPermit`，另核启动时保存的 anchor 摘要、安装及角色，保留 DB inode
描述符的路径复核。进程作为下一代真实 origin 时，当前 base 安装仍须匹配，但不把
上一代的启动角色当新一代角色；非 origin 继续要求本次启动代 generation/role 一致。
对于旧 `BootPermit`，其 installation/service 是 V1 manifest 身份，
不能直接当 V2 slot 摘要比较。校验使用 anchor 的路径和预算、当前 slot 的词法 Python
入口重新 `InstallationManifest.capture`，与许可实际 installation/service 精确比较，
并核 prefix/package 路径。这支持原服务只有已核 capture、尚无 `installation.json`
的情况；不从未认证记录任意生成一份 manifest 就放行。

同一个真实 Store 连接上，复用有界 `read_format_evidence` 和现有连接身份检查，验证
完整七领域格式、Store/main 与主对象、实际 main 数据库路径、WAL 和 FULL/EXTRA、
无外层事务及既有事务模式。当前 slot 的真实声明必须能读取并写回同一观察合同，无
迁移要求；marker 不存在、损坏或格式变化均拒绝。不会调用可能创建主会话的方法、
回填旧数据或改变调用者的事务模式。格式来源认证和合作锁边界仍沿用原有文档，
不宣称防护忽略锁的同用户恶意文件替换。

## 同一回执的 V2 投影

`project_final(permit, store, verify_binding=callback)` 继续使用原先固定
`management_projection/transaction-v1` 对象和 `ProjectionReceipt`，没有第二账本或
第二完成事件。V1 路径保持原身份合同；V2 新写入必须提供宿主绑定回调，原服务以
`role=None` 校验，其他终态以唯一 base/target 校验。没有回调不得写 V2 完成状态。

stable resolved → journal 目录同步/复读 → 精确身份与已存在回执 → 本次实际绑定 →
原 management plan 的 request/action/generation 核验 → 最后 journal/lease 复核 →
单个 `Store.event` 原子提交管理计划、固定回执和 `management.completed`。
保留原 plan 的 routing/其他元数据，不从当前会话猜回复目的地，不修复或重试 unknown。
既有 canonical management 来源适配器同时识别 `adopt/switch`，仍核对持久计划的
status/action/routing，并拒绝 independent 归类，完成事件才可沿原来源进入 Attention。

已存在精确 generation/revision/store 回执时只读返回，不重复通知，也不要求旧安装
仍是新一代的当前安装；这保留合法 successor 的只读重入窗口。相同 generation 的
不同 revision 仍拒绝。未解决、blocked/recovery_required 或尚未 released 不会变为
completed；已放行 target 是 completed，已放行回退是 failed，aborted/cancelled 为
cancelled，其余 aborted 为 failed。任何写入或 COMMIT 失败保持三个对象共同回滚。

## 验证边界

新增 33 项与既有 V1 投影 41 项、系统事件 25 项合计 99 项通过（pytest 1.42 秒，
受限 service 1.949 秒，峰值 95.9 MiB、swap 0）；三处源码严格 mypy 和四份 Python
文件 Ruff/格式检查通过，静态检查最高 126.1 MiB。全部串行经过统一守卫，单次
1 GiB、共享 2 GiB、30 秒硬限。

上一代 target 成为新 origin、非 origin 冒用另代同 slot 两条回归先分别复现错误拒绝
和未抛异常，修后通过。最终组另覆盖真实 V1 capture/lease 的失败 adoption 投影、
adopt/switch 完成事件到 Attention 原来源、同连接零业务写入、外来角色/文件/主身份
与 marker 拒绝、目录同步失败、未放行不完成，以及 receipt/event/COMMIT 三点回滚。

测试只用临时 Store、真实 writer lease/文件原语和合成安装声明、局部进程身份替身；
不启动 systemd、目标解释器、模型或真实用户数据。
实际 bootstrap、nonce 路由与外部组件生命周期由后续整合验收，不由这些 fixture 代替。
