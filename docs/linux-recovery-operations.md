# Linux 同版本恢复入口

状态：已随 2.19.0 发布，完整验证见[发行审查](review-v2.19.md)；没有在用户真实服务上执行重启、停止或故障注入。跨版本 A/B 安装及数据库回滚不在本片范围。

2.22 扩展同一事务 API，增加显式 `adopt/switch` 和三路径 `candidate`，见[本地托管切换](managed-operations.md)。本页保留 V1 操作合同；托管后的重启和配置使用 V2，失败迁入回原入口后可以继续 V1，不增加第二份账本。

## 可用条件

主进程必须由当前用户的标准 `muli.service` 启动，实际 unit、解释器、包、配置和数据路径均可核验；需要 systemd 用户会话、cgroup v2 与受支持的本地持久文件系统。符号链接、自定义 unit/drop-in、未知文件系统、运行身份不符或缺少可证明空闲合同的 legacy Python 插件会明确报告 unsupported。MCP/Skills 使用已有换代和空闲合同。完整约束见[安装身份](installation-manifest.md)、[文件系统核验](filesystem-identity.md)和[控制者设计](linux-controller-helper-design.md)。

普通前台 `MuLi serve` 也取得独占 writer lease，可以正常使用；它不提供 systemd 恢复事务。现有未完成事务缺少正确 manifest、数据库或可支持宿主时，在构造 ConfigFile/Store 之前拒绝启动，不新建空数据库冒充恢复。

## 提交与查看

`self_restart` 始终使用恢复事务。`self_configure` 默认 `apply_mode=in_process`；指定 `restart_transaction` 时，先验证并保存私人候选，等待空闲后准备事务。两者沿用工具权限和原请求来源。新配置中的通知记忆证据在排队时和最后准备写入前分别核验。

认证管理 API：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/management` | 计划状态及 `recovery` 能力、当前事务 |
| `GET /api/management/transactions/current` | 被动维护阶段也可读取的有限状态 |
| `POST /api/management/transactions` | `{"action":"restart"}` 或 `{"action":"configure","changes":{...}}`；未知字段拒绝，restart 不接受 changes |
| `POST /api/management/transactions/recover` | 2.22 起以 `{}` 确认运行宿主并接续当前代；仍须通过既有用户认证 |

`queued` 表示待应用计划，`applying` 表示正在交接；helper 的 `accepted` 仅表示 systemd 接受创建请求。最终以 resolved journal 和原子业务投影为准，不把进程启动请求当成健康。配置候选、口令和私有 nonce 不出现在公开事务状态中。

以下命令不构造 Runtime、ConfigFile 或 Store；自定义路径参数放在 management 前：

```sh
MuLi management status
MuLi --config /absolute/config.json --data-dir /absolute/data management recover
```

status 不创建缺失目录；recover 只接续原代控制者，沿用原期限和有限重启预算。已有控制者持锁时显示 controller_busy。同名失败单元保留，不 reset-failed、不换名绕过预算。recovery_required 是需要人工核查的终态边界，不自动重复未知效果；先查看状态与 journal、保留数据及控制者日志，再决定处理。

原始 `service install/start/stop/restart/enable/disable`、初始化安装步骤和 `/api/service` 共用事务保护；未解决事务期间拒绝操作。固定 unit 必须与调用者的配置、数据和解释器匹配，不能换一个数据目录绕过保护。该约束面向遵守协议的程序，不是阻止管理员直接调用 systemctl、删改 unit 或数据库的沙箱。

## 崩溃与续接

计划与预分配代次先以 WAL/FULL 持久化，然后发布 manifest、配置快照和 journal，最后请求独立控制者。旧进程等待真实空闲，关闭执行准入后由控制者停止；确认 cgroup 为空并持独占 writer lease 后才比较/替换配置。候选以被动模式打开同一业务库，完成独立新 boot 的健康证明；控制者持久提交后才允许其激活。

控制者与主服务使用不同 cgroup，实际核验内存、swap、CPU、任务数和有限时间预算。调用者取消或 helper 创建结果未知不会抹掉已经准备的代次。终态、原计划和汇报意图在一次数据库事务中投影；已投影的结果不会重发，旧无来源计划保持本地。输入及未知外部效果沿用既有账本，不恢复旧数据库覆盖新收件，也不自动重放文件写入或电脑点击。

CLI 在同一个事件循环里完成 bootstrap、Web 服务和关闭；固定单 worker，不继承环境中的多 worker 设置。ASGI 启动前失败也进入同一个幂等关闭任务。构造失败先关已打开的数据库，正常关闭等待实际生产者退出后才释放 writer lease；关闭失败不能以提前释放锁伪装成功。

真实系统停机时长、Arch 桌面集成和断电恢复仍需后续受控环境验证。此实现不承诺零停顿，也不包含自动安装新 Python、升级环境切换或跨格式迁移回退。
