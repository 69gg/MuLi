# MCP 活跃任务期间的代次发布

状态：实现与受限回归完成，已通过根及 Windows reviewer 的独立审查，等待整合，尚未发布。研究基线为 `67bd0766b9ee1ad2d165d3d8d12bcfc760f35c94`，实现已整合 `1cf156b7b498c394b4aa719e3c3391ddafaba003`。现状与历史验证见 [MCP](mcp.md)、[扩展重载设计](extensions-reload-design.md)。本片不运行真实 MCP 服务、模型或安装。

## 用户结果与首片范围

队友长期运行时，MCP 目录变化、工具目录通知和纯扩展配置可以准备并发布新版。每个模型请求使用一份确定的工具表；本轮返回的调用不会按旧名字误投到新端点。已经通过真实派发门的 MCP 调用保留旧连接完成；尚未派发的旧代调用明确记录未执行，下一模型请求取新 schema 重新规划。

首片包含完整 catalog 刷新、HTTP/SSE 新增、替换和删除，以及已有窄配置入口。后续[同名 stdio 热替换](superpowers/specs/2026-10-28-mcp-stdio-swap-design.md)复用同一候选事务，允许 `command/args/env/cwd/timeout` 变化及 stdio→HTTP/SSE 先验证独立连接再发布；复用未变 stdio 的 catalog 刷新仍沿原客户端。完整 `Runtime.reconfigure()`、模型/渠道等混合配置和维护事务保留原空闲门。无自动安装、工具重试、持久代次账本或 Python 插件热卸载。

## 已核生命周期与代码接缝

`uv.lock` 锁定 FastMCP **2.13.1**。先经 Context7 核文档，再对照精确 tag，避免把当前 FastMCP 主分支行为套到 2.x：

- [v2 客户端生命周期](https://gofastmcp.com/v2/clients/client#connection-lifecycle)要求在客户端上下文内使用连接，退出后关闭；[v2 transport](https://gofastmcp.com/v2/clients/transports#session-persistence)区分 stdio 子进程持有与远端服务。MuLi 已为 stdio 显式使用 `keep_alive=False`。
- [2.13.1 Client 源码](https://github.com/jlowin/fastmcp/blob/v2.13.1/src/fastmcp/client/client.py)支持引用计数的重入/并发上下文，但 `close()` 的强制断开绕过计数；`new()` 对 stdio 仍共用 session state。SDK 上下文不能代替 MuLi 的调用所有权，也不能用 `new()` 证明存在独立 stdio 代。

| 现有接缝 | 必须保留或调整的事实 |
| --- | --- |
| `Runtime._execute()` | 当前只在整次执行开头构造 `session_tools()`；后续 `_prepare_request()` 更新消息，却不更新工具表。必须改为每轮物化。 |
| `MCPManager._tools_for()` | handler 已绑定具体 `_Connection`，可复用；不得改成执行时按 server 名寻找任意新连接。 |
| `prepare()/publish()/discard()` | 已有完整候选、复用连接、固定预算及无 await 发布；失败只回收本候选新连接。 |
| `_retire()/_cleanup()` | 当前退休立即设 `usable=False` 并关闭；需延后到实际在途调用归零，保留强拥有关闭、失败引用与 shutdown 再清理。 |
| `_call()/_result()/_sanitize()` | 当前结果使用管理器最新 secrets/输出上限，跨发布会丢失旧密钥脱敏依据；须随候选 handler 固定。 |
| `Extensions.apply_locked()/_watch()` | 当前全局 busy 拦住 MCP，只有 Skills-only 自主发布例外；改为明确的 MCP 活跃发布准入与 draining 状态。 |
| `SelfManagement._idle()/_run()` | 当前在任何 pending 计划前等待全局空闲；不改这道门，agent 的纯扩展自配置仍不能在任务期间生效。 |
| `server.apply_configuration()` | 已按 `Extensions.configuration_only()` 分派纯扩展目标，无需新增 API。 |

Team 和 Workflow 的实际模型执行都经过 `Runtime._execute()`，首片不再为它们各建一套代次机制。`PreparedRequest` 的 principal、task scope、reply origin 与现有权限门保持独立。

## 选定合同：每轮快照，实际调用持有连接

整批连接租约会让旧模型流和审批等待也持续拥有旧端点，删除后仍可能发新请求。选用更小的调用租约：工具表快照只保证 schema/handler 配对，不代表继续授权端点。

1. **请求物化。** 每轮取得 capacity 后、开始模型 stream 前，同步取得当前工具表。继续施加 main、reviewer、attention、disabled 过滤，保留当前 `max_tool_steps` 计算和最后无工具回合。本轮的 schema 和后续所有调用查同一 dict；已发模型请求不在中途替换表。
2. **独立发布 token。** 每个实际 MCP 候选携带宿主私有 token，handler 捕获同一 token、确切连接及不可变结果策略；发布同步切换 current token。不能用 `Extensions.generation`，因为 Skills-only 发布也会推进它。未发布候选不获执行权；纯 Skills 快照发布不撤销 MCP。
3. **保守撤销。** 每次实际 MCP 发布统一撤销上一 token 的未派发调用，包括显式完整 reload 和 catalog 发布；即便某个连接未改变，旧 token 也返回小型固定 `status:not_executed, code:generation_changed`，让下一轮重新规划。首片不增加 per-server token 优化。
4. **最后派发门。** MCP handler 进入 `_call()` 后、第一次 await 前，同步核管理器未关闭、token 仍 current、实际连接仍为当前绑定且可用。失败不调用 SDK，不按旧名字换新端点；关闭使用同样明确的未执行结果。通过后立即登记该连接 active call，再 await SDK。完成审批、收到 tool name 或持有旧 view 都不算通过此门。
5. **在途的准确含义。** 登记后，即使底层 transport 仍在等待写 socket，也已是被宿主接纳的在途调用。发布不再取消它来伪称未执行；其返回值仍经旧策略转换。SDK 超时、取消或故障不能改记 `not_executed`，也不能因换代自动重试未知副作用。
6. **释放与回收。** `_call()` 在实际 SDK 调用结束或取消已 settle、结果已转换为纯值后，于 finally 减计数。删除/替换连接先标 draining；计数为零才进入现有强拥有 retirement task，复用连接不关闭。工具回执仍由 Runtime 按原 ID 逐条持久化，不拆断原批次 pairing。

调用计数是管理器的显式内存状态，不通过模型参数、可继承的 ContextVar 或 TaskSnapshot 授权。任务暂停、新输入和维护门仍在原最终边界复核；取消从真实执行栈释放计数，不把 Task 结束通知当作底层效果已停。

v2.51 的[工具结果资源链接候选](mcp-result-resources-proposal.md)沿用此合同：实际 MCP 发布连同复用连接一起清除结果集合和最新调用 token，Skills-only 不额外清除。已经接纳的旧调用可按原策略返回普通文本；签发结果集合前则须无 await 再核准确 generation、当前连接、usable、receive latch、原绝对期限与最新调用 token。新调用先清旧集合，因此较新失败或取消后，较早调用晚返不能恢复可读引用。不改变旧在途调用的连接 lease 或退休规则；本片验证结果单独记于[2.51候选审查](review-v2.51.md)。

## 结果策略与资源上界

候选 handler 同时冻结本代 `secrets` 与输出预算。`_result/_sanitize/_redact` 显式消费这份策略，不读取发布后的管理器全局值；旧 header、env 和 Bearer token 在迟到结果中仍被遮盖。策略和 publication token 均不进入模型 schema、公共状态或持久工具正文；不引入另一套 Settings。

同一时刻至多当前与一个候选或 draining 连接集合，沿现有最多 32 个 server 的单集合上限。已有 draining 或未确认关闭时，不再准备第三套连接；新通知/目录变化仍合并为一个 dirty 请求。draining 是待回收状态，不冒充发布失败或重启必需；已实际关闭失败仍沿现有固定错误与 restart-required 边界。

保存和发布之间仍没有 await；配置锁内不得等待旧调用结束或持有该锁关闭旧客户端。发布只更新引用并调度已有退休所有者，最后一个调用释放后触发关闭；关闭失败的连接引用不能丢弃。等待 draining 使用现有 watcher 间隔/唤醒，不通过短间隔重复连接，也不新增轮询器或后台队列。

Skills 局部候选被新请求或 MCP 变化作废、或准入暂阻时，保留 dirty 和原请求来源，按正常扫描间隔进入新的观察轮；合法的新请求或显式 wake 仍可提前唤醒。目录观察失败也不能在同一轮消费待处理请求并再次准备候选。排队原因只记录 watcher 已观察到的固定状态；查询状态不额外读取维护门，不能改变观察次数或让状态查询自身触发维护错误。

回收时长分为在途调用已有 timeout/运行期限与归零后的现有关闭 timeout，不能声称发布后固定 30 秒内一切客户端必已回收；不合作 transport 仍受既有保守清理失败边界。Runtime shutdown 保留先取消并 settle drivers、再关闭 MCP 的顺序，不为热更强杀未知外部操作。

同名 stdio 的独立候选可能因独占端口、锁或外设无法与旧进程并存；启动或目录验证失败只回收候选，旧代继续服务，报告实际阶段的固定错误码，不凭普通启动错误推断 `restart_required`。候选初始化也可能有外部效果，回收进程不等于回滚这些效果。此边界与现有新增 stdio 一致，不自动先杀旧进程、不重复尝试候选。

## 配置、暂停和重启

目录 watch、catalog 通知、显式 reload、API 纯扩展补丁与 agent 纯扩展计划共用候选发布器。管理 worker 只对轮到的、从真实私有目标重新读取并验证指纹的纯 `mcp/skills/extensions` 目标绕过全局 idle；锁内再核当前 plan、base 与目标分类，不能相信外部 `live` 标志或历史分类。已有 pending/applying 管理计划时，原 stage 入口拒绝第二计划；须先完成或撤销已有计划，纯扩展也不越队或抢占它的空闲等待。其它审批及全局空闲规则保持不变。

候选失败/取消不保存新配置、不撤销 current token，只回收新连接；配置保存失败也不发布。发布完成后的调用者取消不回滚 token 或重做效果，退休任务仍有所有者。显式删除/停用通过发布撤销尚未派发的旧调用，已经接纳的调用等待原结果。

暂停/取消不因换代失去原权限和 task epoch 检查；恢复后的新模型请求取当前表。重启后仍按当前配置重新初始化，不保存 Python handler/连接，也不承诺跨进程恢复上一可用 MCP 代或自动补发中断调用。同名 stdio 配置替换也遵循先完整准备、保存后同步发布，不能发布的候选不保存；目录 watch 失败仅保留有效内存代，不回写已编辑来源文件。

## 最小实施范围与红绿门控

生产范围预计为 `tools/mcp.py`、`runtime.py`、`extensions.py`、`management.py` 的窄接线；必要时只调整 server 既有分类分派，不改完整 reconfigure。复用 `tests/test_mcp.py`、`test_extensions.py` 的 fake client 与真实 Runtime，新增专用代次回归；同步 MCP 用户说明和本设计。不新增服务、安装器、通用代次框架或持久协议。

先用真实 Team worker/假 provider 与 event 门控复现旧实现：worker 仍 running 时请求实际 reload，期待发布并在下一模型轮得到新 schema；当前全局 busy 与执行开头固定工具表应使它确定性失败。进一步锁定：

- 旧模型流/审批等待中完成替换或删除，旧批调用 SDK 次数为零且有原 ID 的 `not_executed/generation_changed` 回执；不得向新端点调用旧名称。
- 旧调用已经进入 SDK 时发布新代，旧调用正常结束、旧秘密仍遮盖；下一轮才使用新 schema/handler。同一批多个 MCP 调用，只有已通过门者可继续，未进入者逐项记未执行。
- 两个 active call 的最后一个结束才回收；共享连接不误关，第三集合只排队。候选失败、分页/schema 错误、保存失败及 prepare 取消保留旧 token/连接。
- catalog-only 发布也撤销未进入的旧 token，纯 Skills-only 不撤销；未变 stdio 复用连接。同名 stdio 变更经独立候选发布，失败/取消/保存失败保旧，旧调用迟到结果仍用旧 env 脱敏；旧连接实际关闭失败保持 `restart_required`。
- capacity 等待后取新表；main/reviewer/attention/disabled 过滤与最终无工具回合不回退。覆盖模型流、审批和 SDK 等待的取消/暂停、重复关闭及回收失败，禁止泄漏计数或自动重发未知效果。
- 通过真实 self_configure/API 证明纯扩展计划无需等全部队友结束，混合目标及维护阻断仍不绕门；失败保留真实配置与现有计划终态语义。

仅在根显式交接共享守卫后执行分组红绿回归；每次不超过现有 30 秒与资源限制，fake 客户端禁止网络/子进程。可选依赖已有时复用进程内 FastMCP 协议验证，不安装或连接用户 server。远端服务自行改变工具语义不由本地连接代次隔离；本片只保证本地 schema/handler、派发撤销和连接所有权，不宣称服务端具备版本快照。

## 既有测试的合同调整

| 原预期 | 本片预期与保留证据 |
| --- | --- |
| 旧 handler 发布后可继续派发，直到显式退休 | 未进入的旧 handler 立即 `not_executed/generation_changed`；旧 SDK 零调用，新端点只接自己的新名称，实际退休仍核关闭完成。 |
| agent/workflow 忙碌阻止任何完整候选 | 纯扩展可发布，持久 owner 的原状态不变；仍须取得配置锁，不结束或替换正在运行的任务。 |
| 准备期间真实队友报告唤醒主会话，API 冲突或管理计划重排 | API、watch 与纯扩展管理计划都可完成原候选；主模型仍被门控，旧批 handler 被撤销，原报告与最终运行结果不变，不建立第二候选。 |
| 手动/catalog/混合扩展目录变化等空闲后完整发布 | 忙时仍走完整候选，不冒充 Skills-only；完整接受后旧 MCP token 撤销。 |
| Skills-only 不关闭 MCP 连接 | 保留原 identity/快照/正文断言，并直接调用先前取得的 MCP handler，证明其 token 仍获授权。 |
| Skills 局部候选中途出现更新请求、MCP 变化或最后一道门失效 | 继续保留原无发布断言，留到下一轮重选；不在同一轮转为完整发布来吞掉局部候选失效。 |

配置保存失败、维护门、已标记库格式复核、源变化、关闭和取消的失败断言不放宽。完整 Runtime 重配仍遵循原 idle 与组件所有权合同。

## 本片验证

旧实现先以真实 Runtime/Team 和假 provider 门控复现：队友仍在第一轮模型流内时，实际扩展应用抛出 `runtime_busy`，新行为断言失败（1 项，0.36 秒，峰值 78.9 MiB）。修复后，两个专用文件新增 21 项，用真实持久工具回执验证未执行、原批次配对和下一轮 schema；不以只调用私有过滤 helper 代替实际模型轮。

| 最终分组 | 通过项数 | 用例运行时间 | 守卫峰值 |
| --- | ---: | ---: | ---: |
| 新代次、新 Runtime 接线、既有 MCP 与扩展，共 4 文件 | 88 | 2.33 秒 | 155.7 MiB |
| 既有 Skills 活跃发布 | 26 | 0.66 秒 | 89.4 MiB |
| Runtime、Team、Attention、任务控制、Workflow、工作流来源，共 6 文件 | 150 | 1.87 秒 | 124.1 MiB |
| 管理、API、watcher、系统错误来源、Skills，共 5 文件 | 113 | 4.88 秒 | 156.2 MiB |

合计 **16 个文件、377 项唯一用例（21 项新增、356 项既有）**；中间的 41 项尾验属于上述集合，不重复计数。为遵守本片不启动实际 MCP 的执行范围，既有进程内 FastMCP 用例 1 项明确 deselect；没有因此删除或修改它。10 个修改 Python 文件 Ruff/格式通过，5 个生产文件严格 mypy 通过（峰值 278.1 MiB）。各次均由共享资源守卫串行运行，单次上限 30 秒、1 GiB、共享 2 GiB、CPU 1、swap 0，未提高任何预算。

关联回归实际抓住并修正了三处等待边界：局部候选失效及暂阻不进入 10 毫秒重试、目录观察失败同轮不再准备、状态查询不增加维护观察。原 watcher 和系统错误来源用例未改；Skills 的末次配置/维护/格式/关闭/源复核断言也保留。配置锁、保存失败、取消 settle、旧密钥对象值与结构键脱敏、两个在途调用的最后释放、真实 API 拒第三集合、每轮四类过滤及最后无工具轮均有覆盖。

本机只使用临时配置/数据库、假 MCP client 和假模型 provider，没有连接外部服务或下载依赖；不把这些结果当作真实 SDK transport、外部服务器或三平台远端矩阵验收。根与 Windows reviewer 已独立审查主链、最后等待/状态尾差及验证文字，均无剩余确定 P1/P2；两位 reviewer 没有为独审重复运行检查。
