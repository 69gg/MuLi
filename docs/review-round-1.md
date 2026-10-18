# 第一轮独立审查记录

审查范围：`runtime/server/service/reminders/workflows`，以及此前的配置、SQLite、事件、权限、文件/网页/AT-SPI 工具与 chiliarch 接入。参考仓库 `/data0/chiliarch` 和 `/data0/Undefined` 只读；没有实际模型调用、配对、桌面点击或服务安装。自动化回归使用临时目录、模拟 HTTP 和假执行器；另做了下文记录的 AT-SPI 实机只读探测与快照。

## 确定问题与验证

### P1：同一会话并发 start 丢失任务追踪

`Runtime.start` 在检查 `self.tasks` 后，先 `await bus.emit(message.completed)`，然后才登记执行任务。当事件条件锁暂时被占用时，两次请求都通过检查并在同一会话启动；第二次覆盖任务字典，旧运行的 finally 还会删除新运行的追踪。

独立复现临时占住 `runtime.bus.changed`，并发调用两次 `start` 后释放，使用只等待 Event 的假 `_execute`。结果为 `start_results=['dict','dict'], executions=2, tracked_tasks=1`。事件流的 Condition 等待本身可能产生短暂持锁调度窗口，因此不只是假锁下的问题。

已修复并独立复验：在首次 await 前预留运行身份；stop 与 finally 只清理自己仍拥有的 run。`test_concurrent_start_reserves_session_before_event_wakeup` 确认两个并发请求仅一次成功，只有一条用户消息和一次模型执行。

### P1：自动创建的工作流脱离父任务取消与完成边界

`Runtime._workflow_tool` 创建并启动工作流后直接返回 queued 快照，没有父 session/run 关联。主会话可先完成，而子节点仍在执行；停止主会话不会取消子工作流。chiliarch 任务因此也可能先上报 completed，之后仍有属于该任务的副作用。

独立复现用假主执行器调用真实 workflow_create、让工作流假节点等待 Event，随后完成并停止父会话。结果为 `parent='completed', workflow='running', child_cancelled=False`。

已修复并独立复验：模型创建的工作流绑定父 run，父运行释放执行容量后等待子工作流结果；失败和取消向下传播。`test_parent_waits_for_children_without_single_slot_deadlock` 验证并行容量为 1 时仍可完成，最终返回子结果；`test_parent_cancellation_propagates_to_spawned_workflow` 验证取消后没有残留本机任务。用户手动创建的独立工作流保留独立生命周期。

### P2：systemd 参数中的字面美元符号未转义

`service.quote_unit` 处理了引号、反斜线和 `%`，没有处理 `$`。systemd 会在 ExecStart 参数中展开 `${NAME}`，即使原参数写在双引号内。合法配置路径包含这类字面名称时，会使用错误的配置或数据目录。

已修复并独立复验：Context7 核对 systemd 官方实现确认 `$$` 表示字面 `$`，`%%` 表示字面 `%`，纯 render 测试检查两者及引号/换行处理，没有安装或启动服务。[systemd 环境展开实现](https://github.com/systemd/systemd/blob/main/src/basic/env-util.c)

### P2：文档中的路径边界需精确表述

已修正文档：README 和 `docs/architecture.md` 均说明文件访问目录不是操作系统沙箱，shell 与桌面操作具有当前用户能力；三种模式仍遵守启用状态、时间和输出限制。

### P1：AT-SPI 子进程警告混入 JSON

实机只读快照返回有效 JSON，但同一进程的弃用警告写到 stderr。原 `run_process` 合并 stdout/stderr，导致上层 JSON 解码失败，无法执行 snapshot 流程。

已修复：`merge_stderr=False` 同时读取独立 stdout/stderr，两者共同受输出字节上限约束；Computer 使用该模式，保留诊断渠道而不屏蔽警告。回归测试验证警告不污染 JSON、两路输出共享上限。实机只读验证结果：probe available=true、3 个 application、snapshot 30 个节点且 30 个都有稳定身份。没有打印应用标题，没有点击或输入。

### P1：配置更新与新运行可交错

原管理流程先持久替换 `config.settings`，再 await 关闭接入和重建 Runtime。期间新运行可能读取新模型配置，却继续使用旧 Files、工具集和审批对象；收紧访问目录时，实际工具仍保留旧范围，重建随后还可能因 busy 失败。

已修复并独立复验：配置更新、配对、手工工作流启动和运行登记共用配置锁，运行登记后的事件发布在锁外；停止及取消清理不抢配置锁。新增 `tests/test_config_concurrency.py` 验证关闭接入期间 start 不越过旧 roots、并发 PATCH 保持串行结果、配对/手工工作流共享锁，以及取消正在等待配置锁的远端 start 不死锁。

## 已修复的基础风险

- SQLite inbox 相同 ID 内容冲突拒绝、信封与业务队列同事务；POSIX 上 DB 文件为 0600。
- SSE 心跳输出不持有发布条件锁；审批取消后清除等待者，已结束审批不可重放。
- 公共网页先验证全部解析地址，再固定连接地址并保留原 Host/TLS SNI，重定向重新检查；不通过环境代理绕过地址检查。
- 文件搜索限制目录项、深度、时间和结果数；取消等待工作线程收尾。全部文件工具共享串行锁，消除本产品并发 agent 的存在性检查竞争；新建和复制文件用原子 no-clobber 发布，外部竞争写入不覆盖已有目标。恢复拒绝跨文件系统的隐式大规模复制。
- AT-SPI 快照动作使用串行锁，提前消耗快照，校验 D-Bus application bus name、对象路径、PID、role、name；不只依靠树索引或标签。
- 进程超时/取消/超限时清理进程组，即使直接父进程已经退出；清理可承受重复取消。
- chiliarch 配对/ACK 响应有读取上限；运行取消的清理和终态提交可承受重复取消；身份切换隔离旧队列及 outbox。
- MCP 配置保存前校验结构；管理视图脱敏所有 env/headers 值和完整 URL query。原位占位符保留真实值，未知占位符拒绝，整对象替换允许删除服务器和密钥；不将占位符落为真实密钥。
- 配置与回收元数据明确使用 UTF-8。权限位断言限 POSIX，符号链接测试按系统实际能力跳过；Linux 进程组回归使用 `/proc` 验证后代清理，不将该验证伪装为其他平台的实测。

对应证据：`tests/test_safety.py`、`tests/test_chiliarch.py`、`tests/test_integration_manager.py`，合计 64 项通过；配置并发及最新事件窗口另有 6 项通过。相关 Ruff 检查通过，独立负责的 11 个源文件 strict mypy 通过。本轮复验核心 runtime/server/service/reminders/workflows 共 53 项通过；完整测试数量以最终测试记录为准。

本轮确认的 P1/P2 项均已修复并关闭。`Store.recent_events(session_id, limit)` 返回最新 N 条事件并恢复时间升序，初始界面不会只看到最早窗口；原 `events(after, ...)` 仍保留游标回放语义。

## 审查确认与限制

后续两轮只读复审没有发现新的发布阻塞问题：协议/上下文/记忆/安全专项 103 项通过；桌面通知专项 25 项通过，确认命令参数隔离、标记转义、无无限重试、队列与输出上限、超时取消与关闭清理。通知测试使用假执行器，未实际发出系统通知。MCP 文档中的设置接口笔误已改为 `/api/settings`。

- auto 审查使用独立无工具的模型调用，严格要求布尔 allow；风险操作与审查失败转人工审批。工作流 reviewer 只能看到 read 工具，不能创建下级工作流。
- Host 限制回环地址，Origin 必须同源，跨站 Fetch Metadata 拒绝，修改请求使用 JSON。实际请求体上限中间件包含 chunked 请求，HTTP 回归已独立复验。
- 提醒使用确定性 deadline/Event 等待，空闲不调用模型。通知与下一次触发状态同事务；停机漏期按一次合并，避免补发风暴。
- 尚未做真实模型、chiliarch 凭据、桌面动作、systemd 用户服务安装联调。AT-SPI 已完成上述实机只读探测和快照，不能据此声明点击或输入已实测。
- 文件路径检查与目录 move/restore 不能防止另一个有相同用户权限的本机进程在校验后交换目录或创建目标；当前产品不是 OS 沙箱。内部并发操作已经串行，新建/复制文件也具有原子不覆盖保护。
