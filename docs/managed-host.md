# 固定 controller 的 V2 helper 入口

固定 controller 入口及受限 helper 驱动已实现，通过下述离线回归及独立源码审查。公开迁入、管理宿主和服务启动链已接线；完整验收状态见[本地托管切换](managed-operations.md)。

稳定 launcher 的恢复命令为固定 controller 的词法 Python 入口：

```text
controller-python -I -m muli.managed_host --root ROOT recover
```

`recover` 使用现有 launcher 的只读恢复路由核验 root、V2 record、固定 anchor 和 controller，再在原 journal 锁下调用 `ensure_managed_helper`。它不读取当前 selector、不导入 selected target，不构造 Runtime、Store 或 effects backend，也不直接 stop、install、start 或 activate。root 使用 anchor 冻结的路径；数据库须为 Runtime 既有的 `data_dir/muli.sqlite3`。配置/数据库只核无链接路径链及普通所有者文件，不读取 SQLite 内容。

`ensure_managed_helper(control, anchor)` 已由管理宿主复用，调用者已持有同 journal 锁。调用进程可为当前 base/target；新 helper 的 executable 始终来自固定 controller。unit 名由 protocol 2、deployment digest 与完整 generation 共同摘要得到。同一代只使用该名称；已存在时必须通过现有 `SystemdProbe.read_helper` 核验 argv、PID、cgroup、资源属性及内核限制，不能 reset、改名或另起 helper。只有明确不存在时才调用一次 `systemd-run`，成功仅返回 `accepted`，不声称运行或恢复成功。

实际 transient helper 命令为：

```text
controller-python -I -m muli.managed_host --root ROOT run
```

它复用 V1 已有的 `HelperLimits`、受限 runner、全部 systemd 资源属性和 nonce HTTP 协议。包括内存/零 swap/CPU/tasks、运行上限、停止期限、restart burst/delay、无 core、控制组 kill 与固定 umask。重启不会更改 record 的原始 deadline；controller 对每次实际效果继续使用更早的剩余期限。没有 `--wait`、`--scope`、自动清除 unit 或 application unit 生命周期依赖。

`run` 在构造 backend 前核实际 helper 的 PID 等于当前 PID，且精确 argv 指向固定 controller。随后在同一个 journal controller 锁下使用 `ManagedTransactionController` 与 `ManagedLinuxBackend`，每个驱动 step 前再次核固定 anchor/controller、实际 helper 和 fresh record。base/target 的冻结 slot 元数据、兼容声明摘要、七领域声明与 snapshots 都必须一致；不会把 slot 声明当成环境/依赖认证。这里不先核对侧包文件，以免坏 target 阻止已冻结的 base 回退；实际 effects backend 对当前 role 的包、三文件、loaded unit、writer lease 和 cgroup 保留原有核验。

驱动循环只提取到 V1/V2 共用的 typed `drive_loop`，不合并两种 proof 或角色语义。resolved、`recovery_required` 或 `activation=blocked` 即返回；取消传播并保留已耐久 intent，不在 finally 重试未知 start/activate。最终只读 hint 的丢失不能撤销 `released` 或再发 activate。V1 的公开入口、unit 名、资源参数与效果行为保持原样。

CLI 只接受精确的 `--root ROOT recover|run` 三参数，不接受缩写、额外参数或环境中的目标选择；错误仅输出固定 reason，不输出配置、nonce 或原始异常。缺失 root 不创建目录。所有 helper 所依赖路径须通过既有本地文件系统 verifier，既有/读取失败 unit 不能降级为另起进程。

## 验证与未证明范围

新回归使用临时 retained slot/package、真实 journal/private file/snapshot/锁，fake systemd/helper probe、受限 runner 与内存 effects backend。实际当前解释器匹配单独有拒绝用例；收敛测试只替换该进程身份和 filesystem 观察，不把 fake helper evidence 声称为真实 systemd 验证。

覆盖固定资源 argv、同代复用/冲突、不支持路径/协议/预算、损坏 selector/target 下恢复、helper PID/limits、controller 在 await 内变更、提交/一次回退、原 deadline、取消保留 start intent、blocked 不重激活，以及 final hint 丢失的幂等。V1 helper 全组用于检查共享提取的兼容性。

全部检查串行经过统一 guard，每次 30 秒、1 GiB、共享 2 GiB/1 CPU、swap 0。新增 30 项按不重叠三批 7/16/7 全部通过，分别为 1.63/2.22/10.87 秒；原 V1 helper 完整 24 项通过，2.12 秒。两个源码严格 mypy、三个 Python 文件 Ruff/格式及 diff 检查通过。测试最高峰 65.5 MiB，整轮最高峰 123.3 MiB 来自 mypy；没有提高资源上限或运行真实 systemd/service。

本模块测试不启动真实服务，不安装或下载候选，不开用户数据库，也不认证完整 Python 依赖环境。公开 CLI/服务/管理事务已接线，其[临时进程验收](managed-process-recovery.md)单独记录；本片 fake 服务结果不能代替该证明。
