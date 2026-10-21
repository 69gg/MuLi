# 候选准备前的当前安装观察

状态：下一版候选准备的只读接缝，尚未接入 `self_update`，不代表已能自动安装环境。

`ManagedPreparation.observe_current()` 在配置锁内复用已有准备器对当前来源的核验，
只接受实际 `ManagedBootPermit`、已终结的 protocol 2 事务、实际 Store 的精确终态投影
以及当前 managed 角色。不伪造或写入管理计划，不创建新 generation，不执行候选。
原有公开 `self_switch` 仍要求完整候选路径，真正切换仍走原准备流程。

观察包含实际 writer lease、Store/格式、保留安装、固定 controller、deployment、
selector、unit、当前配置字节及 Runtime 配置；实际进程观察 await 后重新核对来源。
返回的 `binding_revision` 只绑定这份观察，既不是批准，也不是安装来源证明。
调用者还须核实际 receipt、宿主来源策略、材料与执行限制。

返回的同步 `verify` 重新读取相同来源，供候选准备在各个效果边界检查漂移。
主会话新增普通对象不使安装观察过期；热配置变化会改变 revision，并使旧回调失败。
核当前配置而非要求它永远等于上一事务的配置快照，所以正常热配置后仍可重新观察。
回调不能替代后续实际进程确认、候选预检或 A/B 准备时的完整验证。

本地原准备器及五项观察回归共 32 项通过，1.64 秒、峰值 86.6 MiB，仍使用串行
30 秒资源守卫，swap 0。未投影终态由既有 Maintenance 以 `transaction_in_progress`
提前拒绝；测试没有为深入某一分支关闭这层门。系统进程边界仍使用该测试模块已有
合成 probe，不将此结果称为真实用户 systemd 服务验收。

两份 Python 的 Ruff/格式及生产模块 strict mypy 通过，后者峰值 249.6 MiB。
额外尝试把整个测试模块及其导入的旧夹具也纳入 strict mypy 时，发现 35 处现有测试
类型问题；这不是仓库原有 CI 的类型检查范围，未将该次失败称为通过，也未用 ignore
掩盖。新增生产代码仍按原有 `src/muli` 严格范围检查。
