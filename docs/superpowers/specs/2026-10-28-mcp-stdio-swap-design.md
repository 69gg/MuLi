# 同名 stdio MCP 原子热替换

状态：实现与离线回归已准备，由根代理串行运行受限验证；尚未发布。基线为签名主线 `711b38884fb280ad2ba84bb8fc58cf4f678d5e44`。另补自有 stdio 协议 fixture 的锁定 SDK 旅程，待根代理执行；不连接第三方 MCP、用户程序、桌面或外部模型，不修改依赖、配置 schema 或资源上限。

## 用户结果与缺口

同一 `mcpServers` 名下修改 stdio 的 `command`、`args`、`env`、`cwd` 或 `timeout` 时，先启动并完整验证独立候选，成功后一次切换工具和配置。旧代在准备期间继续可用，候选失败、取消或保存失败均保留旧代。stdio 改为 HTTP/SSE 同样经过这条既有候选路径，不新增跨 transport 特判。

此前 `MCPManager.prepare()` 在完整 raw 定义不相等后，只要旧连接是 stdio 就返回 `restart_required`，工厂尚未执行。目录刷新、停用后重新启用和换名替换虽已可用，却不能完成同名、单次、失败保旧的事务。本片只移除这项统一拒绝；独立候选、保存后同步发布、generation token、调用租约和退休所有权已有实现。

## 生命周期与实施决定

1. 未变且可用的 raw 定义仍复用原连接。未变 stdio 的 catalog refresh 或 HTTP 接收策略变化不会重启该 stdio。
2. 同名变化走已有新连接分支，每次拥有独立 `AsyncExitStack`、`ReceiveState`、transport、client 和 `_Connection`。不使用会共享 stdio session state 的 `Client.new()`，也不移动旧清理栈。
3. 初始化、capabilities、完整目录、schema、分页、数量、总字节和期限均复用现有校验。候选未发布时，其 handler 没有派发资格；旧 handler 仍可接纳调用。
4. `Extensions.apply_locked()` 仍在配置锁内最终核对来源、维护状态、Store、关闭状态和接收策略，再保存配置；保存到 MCP/registry/Skills 同步发布之间没有 await。
5. 发布更新 current token 和工具表，旧 handler 的新调用固定返回 `not_executed/generation_changed`。同名 remote tool 可保留工具名，新 schema 与闭包指向新连接；旧批次不能按名称重绑新端点或重放。
6. 已 admitted 的旧调用保有原连接、timeout、secrets 和输出预算；最后一个调用实际 settle 后，现有退休所有者才关闭旧 client/transport。发布不在配置锁内等待旧调用或清理。
7. 失败/取消只回收候选新建连接。当前加一个候选或 draining 集合的上界、`candidate_pending`、`generation_draining`、清理失败引用和 shutdown 再清理均保持原合同，第三套工厂不得执行。

这属于现有候选事务的窄修复，不新增连接池、代次框架、持久账本、后台队列、自动重试、依赖或配置开关。Skills-only 不撤销 MCP token，混合模型/渠道配置仍等空闲，权限与管理计划顺序保持原入口行为。stdio 普通资源、prompts 和图片结果支持范围不扩大。

## 失败、保密与实际限制

- 本机程序持有独占端口、锁或外设时，新旧进程可能不能并存。候选启动失败便保留旧连接，报告 `server_unavailable`、`candidate_timeout` 等真实阶段固定码；不凭普通启动异常推断独占，也不先杀旧进程来验证。
- 候选验证会启动一次已授权本机程序。初始化产生的外部效果不保证随进程回收而回滚，不自动重复候选或工具调用。
- 回收先等 admitted 调用在原 timeout/运行约束下 settle，再进入现有关闭 timeout。不承诺不配合取消的 SDK 在发布后固定 30 秒内退出，也不增加强杀机制或提高期限。
- 设置/API/`self_configure` 在成功前不保存目标配置；目录 watch 失败保留当前有效内存代，不回写用户编辑的分片。进程重启仍按磁盘配置初始化，不持久恢复上一进程的 last-known-good。
- 原代 env secrets 继续用于迟到结果，新代使用自己的 secrets；状态、异常、事件不增加 raw/env、命令异常正文、stderr 或候选 repr。测试只用虚构凭据，敏感定义比对只断言布尔结果。该合同不识别任意未配置的宿主秘密。

## 红绿证据与回归范围

先仅新增 `test_same_name_stdio_args_and_env_change_publishes_an_independent_candidate`，生产源码冻结在基线。根代理通过共享守卫确认其在 `prepare()` 抛 `restart_required`：1 failed，pytest 0.70 秒；守卫退出码 1、2.033 秒、78.1 MiB，日志位于 `/tmp/muli-mcp-stdio-v2108-baseline-red.log`。随后才删除生产拒绝分支。该正例现已并入参数化测试 `test_same_name_stdio_change_publishes_an_independent_candidate[args-env]`。

复用现有 fake client 和真实 Runtime 测试，覆盖：

| 范围 | 证据 |
| --- | --- |
| 定义变化和转移 | command、args、env、cwd、timeout、args+env、stdio→HTTP、stdio→SSE；独立实例、同名工具、新 schema/结果、候选不提前获权、旧 token 撤销 |
| 完整验证与失败保旧 | 初始化失败、分页/预算/目录错误、准备取消、来源变化、关闭 fence、总期限和配置保存失败；旧连接与原配置继续有效 |
| 调用所有权 | 两个旧调用分别完成，最后一个释放才关闭；已取消 SDK 必须 settle；新代同时可调用，第三套不执行工厂 |
| 保密与回收故障 | 迟到旧结果继续按旧 env 和原输出预算脱敏，新结果按新 env；清理失败保留所有者与 restart-required，shutdown 可再清理 |
| 实际入口 | API、同名 mcp.d watch、运行中的真实 `self_configure` 管理计划；审批等待、旧批次及混合配置空闲门 |
| 保留负控 | 未变 stdio catalog/接收策略复用、Skills-only token、维护门、计划顺序、模型权限过滤与最终无工具回合沿既有相关组验证 |

本机检查全部交根代理串行经 `python scripts/check_local.py` 收取最终退出码和守卫结果。实现代理未运行 Git、检查、测试、实验、外部服务或下载。完整跨平台矩阵由托管 CI 负责；通过离线 fake 回归不代表第三方 stdio 程序已实测可并存。

`tests/test_mcp_stdio_sdk.py` 另覆盖 Linux 上已安装 `mcp` extra 的实际默认工厂旅程：从临时目录启动自有最小 JSON-RPC fixture，比较不同 args/env 的真实 PID、响应与环境摘要；同名发布后保留已进入的旧调用，再确认旧进程退出；目录验证失败仅回收候选，当前连接继续响应。测试只观察 SDK 返回的真实进程句柄，使用原 manager 退休/关闭及 SDK 的失败清理原语；未替换 client factory 或 transport。无 extra 时跳过，现有 MCP CI 的 `tests/test_mcp*.py` 自动纳入。单个 fixture 无 children，15 秒自行退出，最多处理 12 条、每条输入 4 KiB/输出 1 KiB；主旅程 20 秒，manager 单次操作 4 秒。实现代理仅编写，执行结果由根代理补记。自有进程退出不能证明第三方程序的外部效果可回滚。

根代理使用已安装的 FastMCP 2.13.1 / MCP SDK 1.21.0 完成首次受限执行：`1 passed`，pytest 1.72 秒；守卫最终退出码 0、3.356 秒、峰值 127.4 MiB，三个自有进程均正常退出。此项仅证明本机 Linux 的自有最小服务旅程，不代表任意第三方程序或其它系统已经实测。
