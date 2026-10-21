# Windows UIA Invoke 首片设计

状态：根已批准按本设计实施，宿主、固定桥接与自有按钮验收已编写，受限本地
回归和静态检查通过；最终实现独审与真实 Windows CI 尚待完成。基于签名 `423596819405cae37311b278cfc30c46454f811b`
的已发布只读后端；能力与验证边界见[UIA 记录](windows-uia.md)和[电脑工具](computer.md)。
本片不改版本或 CI，没有在开发机运行 PowerShell 或桌面效果。

## 选择与实际接缝

首片只实现选定窗口中普通按钮的 `InvokePattern.Invoke()`，保留原 `computer_action`
工具与 `computer` 风险门。`computer_type` 继续明确拒绝，不加入 Value、Toggle、
Selection、焦点、坐标、键盘模拟或失败后寻找替代控件。

| 设计时的只读代码 | 可复用与本片补齐之处 |
| --- | --- |
| `tools/uia_helper.ps1` 的 `Read-Snapshot`、`Read-Node`、`Window-Stamp` | 现在只保存发现窗口；快照清掉引用后未保存子节点。须保存本次实际子节点对象和所选窗口绑定，不能仅给节点填 actions。 |
| `tools/uia_computer.py` 的 `_observe`、`_records`、`action` | 现在只缓存 target_id，且强制全部只读；须保存输出预算内的节点映射与 snapshot_id，接入单次消费和串行操作。 |
| `Computer.__init__` 与 `Runtime._computer_dispatch_guard` | Runtime 已有执行代次、维护/暂停、新输入检查，但只传给 AX。须把同一 guard 传入 UIA，复用最终无 await 派发门。 |
| `AXComputer.action`、`AXHelperLease.request` | 已有 action 载荷、before_send、绝对期限、已发送后的 unknown、强拥有回收；按现有形状窄复用，不重构公共租约。 |
| `UIAComputer._error` | 现只读转换未传 `AXLeaseError.outcome`。首片必须透传，否则新增动作的 unknown 将被错误降为默认 not_started；这不是当前只读后端已执行动作的证据。 |

Invoke 与 Value 可共享引用和期限，但 Value 另需完整文本替换、原生只读与多行限制、
文本预算、密码拒绝和真实可编辑窗口验收。先交付一次可证实的按钮操作，不把上述
独立能力混入首片。以后支持 Value 也只能使用实际 `ValuePattern.SetValue`，不能照抄
官方示例中的 SendKeys/focus fallback。[Value 接口](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.valuepattern.setvalue?view=netframework-4.8.1)
不保证多行文本可写；本次不读它的 Value，也不新增设置字段。

## 调用与能力

公开调用继续使用既有参数：

```json
{"snapshot_id":"当前快照","node_id":"该快照的按钮","action":"invoke"}
```

只有快照中明确非密码、启用、`ControlType.Button` 且能取得 InvokePattern 的节点
公开 `actions=["invoke"]`、`editable=false`、`read_only=false`；其余节点仍无动作。
模式读取失败不能假定可调用，沿既有 provider_error/不完整标记保守呈现。
发现列表本身不授权动作，不能把 target_id 或未展示节点转成操作凭据。
模式支持只是当时观察，最终派发前仍须向原对象重新取得该模式。
[TryGetCurrentPattern](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automationelement.trygetcurrentpattern?view=netframework-4.8.1)
返回不支持时直接拒绝，不尝试另一种操作。

返回成功仅沿现有固定回执：

```json
{"ok":true,"outcome":"accepted","effect_verified":false}
```

它表示原生调用正常返回，不表示按钮业务成功或异步界面变化已经完成。
窗口关闭也可能正是按钮效果，因此调用成功后不能用“窗口仍存在”作为成功条件。

## 短期对象与单次消费

helper 仍为固定系统 PowerShell 5.1、NoProfile/NonInteractive/MTA、无窗口和固定
系统 Utility 模块；不改执行策略、不加源码编译或第三方库。引用只留在该 MTA
helper 的内存：私有 ref_id 对应实际 AutomationElement、原记录和选定窗口原对象，
窗口 stamp 沿用 PID、原生窗口句柄及该 PID 的启动时间。节点还须属于该 PID；
跨进程嵌入的子节点可保留只读资料，但首片不授权 Invoke。

快照引用数受现有 max_nodes 限制。helper 装箱后只保留回执节点前缀对应的引用；
宿主再按最终公开 JSON 预算缓存实际展示的节点。不存在从模型提供的 PID、句柄、
RuntimeId、标题、AutomationId 或坐标重新搜索的入口。

同一窗口中原对象也可能被移除或重新归属。Invoke 前使用原对象、所选窗口原对象
及既有最多 12 层 ControlView 父链，确认节点仍归该窗口；对象比较可用
[Automation.Compare 的对象重载](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automation.compare?view=netframework-4.8.1)，
不保存 RuntimeId 数组或用它找替代节点。PID/窗口 stamp、节点角色和非保护名称
须与本次记录一致。父链缺失、变化、超过原深度或查询失败都在调用前拒绝。
[GetParent](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.treewalker.getparent?view=netframework-4.8.1)
明确说明父关系会变；这些有限复核不是外部 UI 的原子快照或永久身份保证。

每个 targets/snapshot 新观察尝试在首个 await 前使旧快照失效；先捕获选定窗口引用，
再清旧映射，以代次拒绝迟到观察。当前快照的一次操作尝试同样在首个 await 前
消费整组凭据，包括合法快照下的坏节点或不支持动作；失败、取消、重载、关闭与
结束均不复活旧凭据。不同调用者仍立即 desktop_busy，不能等待后领用别人的对象。

## 最后派发、密码与结果

1. 宿主捕获原 owner/lease、消费快照、建立一次操作的原期限并取得 Runtime guard。
   等待串行锁、helper 准备和传输都消耗它与 helper 绝对 TTL 的较小值，绝不续期。
2. 复用 `before_send` 同步核当前 owner/代次/关闭状态及 Runtime guard，紧邻 stdin
   write，中间无 await。收到新输入、暂停、权限或维护变化时零派发。
3. helper 先消费私有引用，再核原对象/窗口/归属；先读取 IsPassword，受保护或该项
   查询失败直接拒绝，不再读 Name、Value 或模式。普通按钮再核角色、启用状态、
   非保护名称及当前 InvokePattern，沿已有固定码返回 not_started。
4. 最后一次 Check-Deadline 后、紧邻 Invoke 调用前置“原生调用已进入”标志。之后
   包括 provider 异常、回执丢失或期限触发的任何不确定均为 action_unknown_outcome，
   不能把 provider 的异常类型当作无副作用证明。方法正常返回才发 accepted。
5. 宿主写入后没有可信回执时沿 lease 的 unknown 语义。取消仍传播 CancelledError，
   不等于 not_started，也不自动补发；不保证取消时另有一条结果已经写入会话。
   精确 helper 必须强拥有回收；清理不确定保留占用和诊断，不覆盖 accepted/unknown。

IsPassword 反映 provider 的保护声明，不能识别所有未标记秘密；首片不读 Value /
TextPattern，密码节点保持空 Name、无 actions、不可输入。普通按钮名称仍是外部资料。
现有 ask/auto/yolo、调用审批和运行器工具批次规则不变，没有“观察过就免审批”。
租约期限只能终止并等待自有直接 helper，不能撤销已发生动作或保证系统 COM broker
已退出；没有自动重放、动作轮询、事件订阅或后台重连。

## 最小实现文件与验收

实施仅需 UIA adapter/helper、Computer 的 guard 转交、专用宿主回归、现有原生目标
fixture 的有界按钮证据及用户文档；不改 Runtime 权限、AX/AT-SPI、Settings、Store
格式或新增通知/执行引擎。脚本打包继续走既有 wheel 资源 smoke。

纯回归沿现有假 lease/真实 Runtime 门控，拒绝真实 subprocess，至少覆盖：

- 输出装箱后只有已展示节点可动作；旧观察/owner/代次/TTL、并发消费及第二次尝试
  均拒绝，错误引用不触发搜索。新观察失败不能恢复旧快照。
- 等锁、准备或审批后到最终 write 之前注入新输入/暂停/关闭，实际 write 为零；
  正常调用只发一次，保持 computer 风险与最后请求门。
- 能力消失、禁用、密码翻转、名称/角色/PID/窗口/父链变化均在 Invoke 前拒绝。
  宿主回归验证固定 not_started/unknown 回执及 protected 能力拒绝，不冒称动态执行了
  PowerShell getter spy；桥接的 protected-first 顺序由源码审查核对，真实 provider
  由 Windows CI 的自有控件验证。Value/Text getter 不属于本片代码路径。
- 在 stdin write 前失败为 not_started；write 后断流/期限/坏回执为 unknown，
  helper 方法进入后异常同样 unknown；取消/reap、迟到进程与清理失败保占用不重放。
- accepted 不做业务断言，适配器 outcome 原样传递；最终 JSON 和现有字符/字节/
  请求数/节点/绝对时间预算全部保留，无新硬编码设置或更高上限。

真实验收继续只在现有 hosted Windows 3.12/3.13 CI 内创建自己的 stdlib Win32 窗口：
保持原基础解释器同 PID、12 秒启动、4 KiB 两管道预算和已有三项原生测试。
目标的专属 BUTTON 有固定控制 ID；其父窗口仅在收到该 HWND/ID 的
[WM_COMMAND/BN_CLICKED](https://learn.microsoft.com/en-us/windows/win32/controls/bn-clicked)
后递增一次计数，发一个固定、预算内的测试事件。沿现有并发 reader 增加这一项
事件门，不另起无界 stdout 读取；启动事件不计作动作证据。

新真实用例先对精确自建窗口观察和快照，再经 Computer.action 调 Invoke，同时
等待 accepted 与该按钮实际事件，二者共用该次原操作期限；目标计数必须恰为 1。
随后旧快照拒绝且计数不变，自有 reader 已退出而目标仍存活。另用启动参数固定
禁用按钮/密码控件，确认不提供 Invoke、不能产生目标事件。原关闭窗口失效、Unicode
及密码 label/value 不泄漏、MTA/系统模块来源和取消回收断言保留，不靠增加 sleep、
预热、扩大期限或 skip 通过。事件只来自自身窗口，绝不点击其它桌面应用。

该原生证明只覆盖标准按钮 provider；异常/阻塞 provider 以受控协议回归证明 unknown
与回收，不声称 CI 已覆盖所有 COM 阻塞、跨权限窗口或真实用户业务。Value 输入、
自定义控件、多进程嵌入、管理员/安全桌面及未标记秘密不属于首片验收。

## 依据与交付状态

先用 Context7 的 `/websites/learn_microsoft_en-us_dotnet_framework` 查询 Invoke/Value
及模式合同，再核微软 `.NET Framework 4.8.1` 固定接口页。尤其
[Invoke](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.invokepattern.invoke?view=netframework-4.8.1)
说明 provider 可能阻塞；[Invoke provider 合同](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/implementing-the-ui-automation-invoke-control-pattern)
的“应立即返回”不能当作实际可取消保证。
[IsPassword](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automationelement.automationelementinformation.ispassword?view=netframework-4.8.1)
和[Edit 限制](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/ui-automation-support-for-the-edit-control-type)
支持将 Value/受保护文本单独处理；不把文档示例的键盘 fallback 纳入本产品。

设计已签名为 `0273e89de60667dd6f7c629495fc7636afc285b0`，根与独立审查未发现
确定合同遗漏后另行批准实施。修改生产前的两条旧码反例均真实失败：实际
Computer.targets→snapshot 的普通按钮能力在旧 `_records` 被当作协议错误；
`AXLeaseError(outcome='unknown')` 经旧 UIA 转换后实际变成 `not_started`。两条均经
30 秒守卫，分别 0.13 秒/60.4 MiB 与 0.11 秒/57.1 MiB，swap 0，无真实 subprocess。
修后专用 Invoke 29 项、宿主/启动/窗口夹具 63 项、既有 Computer/AX 纯回归 101 项
通过，合计 193 个不同用例；其中 40 项新增（29 项 Invoke 与 11 项按钮夹具），
153 项既有回归。原生文件在 Linux 按已有 hosted Windows 门跳过 4 项，包含新增
的真实按钮用例；这些 skip 不是 Windows 验收。先前专用 28 项为新增取消用例前的
重叠小组，不重复累计。pytest 峰值 67.2 MiB；9 份 Python 的 Ruff/格式检查与两份
源码 strict mypy 通过，类型检查峰值 213.4 MiB，整轮 swap 0。首轮 Ruff 仅发现
新测试导入顺序，按建议整理后通过。所有命令串行经过原 30 秒检查守卫，未提高上限。
没有运行 PowerShell、操作开发机桌面、安装或下载组件；固定脚本语法、标准 provider
动作与实际 BN_CLICKED 的关联仍待真实 Windows CI，不能从本地受控组反推已通过。
