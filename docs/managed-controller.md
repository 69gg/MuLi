# 共享控制器与 V2 角色观察

共享引擎与 V1/V2 策略已实现，通过独立审查与下述模块回归。V2 Linux backend、启动准入与 CLI 已接线；完整入口和真实进程验收状态见[本地托管切换](managed-operations.md)。本模块回归没有操作用户真实服务。

`management_controller._Controller` 保存单份有界调用、原 deadline、回退 reserve、阶段推进、取消传播和持久 start 意图语义。公开的 `TransactionController`、`ControllerBackend`、`ServiceObservation` 保留原 V1 构造接口，由 V1 policy 执行原配置比较、快照读取与 advance。V1 Linux backend 不修改。

`managed_controller.ManagedTransactionController` 复用该引擎，由 V2 policy 调用同一 journal 的 `read_current`、`read_snapshots`、`advance_managed`。它不把多个文件拼成假的 configuration 摘要：`ManagedServiceObservation` 分别保存精确 configuration、selector（可证明缺席时为 null）、原始 unit 摘要；`ManagedProcessIdentity` 分别保存实际进程的 entry、installation、compatibility。运行态必须同时符合一个完整角色的文件和实际安装，不能只因文件选择了 B 就将仍在运行的 A 视为 B。

安装的合法文件顺序是 configuration → selector → unit。只有 stopped、writer 可独占且无实际进程，在持久 `install_target` 或 `restore_base` 意图下，才能处理本代明确冻结的中间前缀。回退反向经过同一组前缀；各组件相同时自然合并重复前缀。任一 foreign bytes、未获许可的排列、运行中半写状态都拒绝。

安装部分完成后普通错误会先持久化 `rolling_back/stop_target`。若此时已 stopped、writer 可独占、无实际进程，且观察仍严格落在本代冻结前缀，唯一窄 hook 只把意图推进 `restore_base`，当步不调用 stop/install，不将前缀当作健康或安装完成；下一步才按恢复窗口的实际 guard 逆序 CAS。running、busy 或 unknown bytes 不享有此过渡。取消仍传播并保留原安装意图，不被记成普通失败。

完整 bytes 不是安装完成证据。`BindingConfirmation` 绑定当前 generation/revision、role 和三文件摘要，并明确绑定已核对 loaded 属性的 unit 摘要。backend 只能在真实 writer lease、空 cgroup 下逐项 CAS/confirm 文件及父目录耐久，必要时 daemon-reload，并比较 fresh loaded 属性后生成该证明。`loaded_unit` 不声称 systemd 返回了原 unit 文本；它是 backend 对所核 unit 的显式声明。旧 revision、错 role、缺 loaded 证明都不能触发 start；同配置切换也必须经过 configuration 确认、selector、unit。新 owner 必须重新观察与确认。

`start()` 仍需在自己的 effect 边界再次核三文件、loaded 属性和实际 writer/cgroup/安装，不可把内存 `BindingConfirmation` 当永久授权。先耐久写 start 意图，后调用 start；若取消或崩溃留下 ambiguous inactive start，不自动重复命令。健康仍经 `HealthProofV2` 精确绑定 role、安装、配置、selector、unit、Store/main、format、nonce 和新 boot；origin proof 不可作为新角色成功。decision 与 released 分开，缺少观察不改 deadline、不盲目再激活。

回归使用真实临时 journal/锁、有限 fake 文件和进程证据，覆盖 V1 旧 controller 组、V2 同配置换 slot、两个方向的文件前缀、取消后接管、foreign 排列、缺确认、错误实际进程、旧 boot、回退 legacy 与延迟观察。它不能证明真实 systemd loaded 属性、CAS 实现或实际服务启动；这些需要 backend 与宿主闭环的独立验证。所有本地检查必须事先协调共享槽并经过 `scripts/check_local.py`。

本片受限检查：原 `tests/test_management_controller.py` 全组与新 `tests/test_managed_controller.py` 合计 86 项通过（0.69 秒，服务 1.168 秒、内存峰值 63.3 MiB）；三份 Python 文件 Ruff/格式、两源码严格 mypy 通过，整轮最高内存 118.7 MiB、swap 为零。每次经守卫设置 30 秒外层期限、单次 1 GiB、共享 2 GiB/单 CPU，串行执行。
