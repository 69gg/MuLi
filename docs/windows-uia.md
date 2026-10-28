# Windows UIA 窗口与按钮

Windows UIA 后端可发现窗口、读取所选窗口的树，并对本次快照中明确提供能力的普通
按钮执行一次 `invoke`。电脑能力默认关闭；在现有配置中设置 `computer.enabled=true`、
`computer.backend=auto` 后，Windows 自动使用 UIA。显式 `atspi` / `ax` 不降级，
不新增 `uia` 配置取值。

## 使用方式

| 工具 | 用法与结果 |
| --- | --- |
| `computer_targets({})` | 有界读取桌面的直接顶层窗口，返回 `scope=desktop_windows` 和临时 `target_id`。 |
| `computer_snapshot({"target_id":"当前窗口标识"})` | 读取选定窗口，返回 `scope=window`、`snapshot_id` 和节点 `id`、`parent_id`、`depth`。 |
| `computer_action({"snapshot_id":"当前快照","node_id":"按钮节点","action":"invoke"})` | 只调用本次快照中实际展示且具有 `actions=["invoke"]` 的按钮，一次尝试后消费整个快照。 |
| `computer_release({})` | 释放当前执行持有的窗口引用与 helper。 |

先发现窗口，再读取快照。未知、失效或缺少窗口标识均拒绝，不回退整个桌面，也不按
标题寻找替代窗口。Windows 不接受 `application_id` 展开；`computer_type` 继续拒绝，
不使用键盘、焦点、坐标或其他模式兜底。动作沿用现有 `computer` 风险门，发现、
读取与释放沿用读取权限。

每个新观察消费旧发现与快照。引用只属于当前执行；另一个执行收到 `desktop_busy`，
旧执行不能释放新执行的引用。最终派发前重新检查真实执行、新输入、暂停和关闭。

## 内容与动作限制

树是当时的外部资料，不是原子桌面快照；名称本身不能授权操作。UIA 的 Name 可能
包含应用文字，后端不读取 ValuePattern / TextPattern。明确密码节点先核
`IsPassword`，再决定是否读取 Name；密码节点返回空名称和 `protected=true`。

只有能取得 InvokePattern 的普通、非密码、启用 Button 才提供
`actions=["invoke"]`、`read_only=false`；其余节点保持 `actions=[]`、
`read_only=true`，所有节点均为 `editable=false`。IsPassword 依赖 provider 声明，
不能识别所有未标记的秘密。

Invoke 前重新检查原对象的密码属性、Button 角色、启用状态、非保护名称、同
PID / 窗口句柄 / 进程启动时间，以及最多 12 层原窗口父链。关闭或变化的窗口拒绝，
不使用 RuntimeId、标题或句柄重新寻找控件。它不保证应用内部节点在读取期间未变。

成功回执为 `{"ok":true,"outcome":"accepted","effect_verified":false}`，只表示原生
调用被接受。进入原生 Invoke 后的异常、丢回执或超时均保守报告
`action_unknown_outcome/unknown`；调用前拒绝保留 `not_started`。取消不撤销已经
发生的界面效果，清理失败不把 accepted / unknown 改为可重试错误。没有自动重放；
失败或结果不确定后，必须重新观察。

## 固定系统桥接

宿主通过 `GetSystemDirectoryW` 定位系统 Windows PowerShell 5.1，不从 PATH
选择程序。固定参数为 `-NoLogo -NoProfile -NonInteractive -Mta -File` 加随包的
`tools/uia_helper.ps1`，启动使用 `CREATE_NO_WINDOW`；脚本路径与参数不接受用户输入。

脚本关闭模块自动加载，只从 `$PSHOME/Modules/Microsoft.PowerShell.Utility/`
的固定 manifest 显式导入 Add-Type 与 JSON 命令，并核实际模块路径。模块命令使用
限定名。Add-Type 只加载系统 UIAutomationClient / UIAutomationTypes 程序集并核
GAC；不编译 C#、安装 SDK / Pythonnet 或派生外部命令。

首次非 probe 请求才从主脚本所在目录的固定 `uia_tree.ps1` 加载原有树与动作函数，
在当前 helper 作用域保留并复用；不搜索 PATH 或用户脚本目录。加载计入原操作期限，
文件缺失或加载失败沿用现有固定错误与清理。probe 不读取该文件，仍执行相同的
真实 PowerShell 5.1、MTA 和 GAC 依赖检查。两个脚本随包安装，安装后 smoke 核验
各自位置、严格 UTF-8 可读性与函数职责。

系统 PowerShell、.NET Framework UIA 或当前会话不可用时固定失败。脚本仍受系统
执行策略约束；无签名脚本在受限环境中可能不可用。MuLi 不修改执行策略或授权，
不使用 `-ExecutionPolicy Bypass` 或其他宿主绕过策略。状态 probe 只证明桥接依赖
与 MTA 可运行，不枚举桌面；`session_accessible` 保持未知，不能承诺任意应用或
隔离桌面可读。

## 期限、预算与清理

复用 `AXHelperLease` 的有界 JSON-lines 传输、绝对 TTL 和强拥有回收。
`snapshot_ttl_seconds` 从 helper 创建开始计时，读取与状态查询不续期；
`operation_timeout_seconds` 覆盖启动与本次请求，默认 12 秒。取消、超时、格式
错误、执行结束、重载和关闭均回收准确的 helper。回收未确认时继续保留占用与固定错误。

逐节点使用 ControlView TreeWalker，不使用整桌面 `FindAll(Descendants)`。
`computer.max_nodes` 限制访问尝试，桌面根也消耗预算；属性调用与窗口身份复核
不另计节点。相对深度最多 12、每个节点的直接子项最多 100。省略报告原因，
未知子树数量不捏造精确值；不完整结果不能证明窗口或控件不存在。

完整 helper JSON 信封同时受 60000 字符和 250000 UTF-8 字节上限约束，保留完整
节点前缀及省略信息；宿主再按 `limits.max_output_chars` 限制公开 JSON。
单 helper 最多 16 请求，单请求最多 64 KiB，stdout / stderr 合计最多 1000000
字节。宿主和 helper 都只保留最终实际展示节点的引用。

外部 provider 可能阻塞或先分配内存，遍历预算不是系统 COM 内存硬上限。Windows
进程原语只终止并等待自有的直接 helper；系统 UIA 可能使用 COM 或跨位数代理，
后端不拥有或宣称回收这些系统 broker。

## 当前验证边界

本次拆分减少 probe 入口携带的树与动作定义，不证明冷启动超时的根因或超时已修复。
已在托管 Windows 的 Python 3.12、3.13 环境验证延迟加载、依赖正负控、
自有临时窗口读取、一次按钮操作与清理，保留原期限和协议。
这项验证不代表任意应用、隔离桌面或本机用户桌面均可用。

历史 CI 与诊断证据保留在[启动诊断记录](uia-native-startup-review.md)、
[目标窗夹具记录](uia-native-win32-fixture-review.md)及
[探针诊断设计](windows-uia-probe-diagnostics-design.md)。

## 官方依据

- [Windows PowerShell 5.1 参数](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1)：NoProfile、NonInteractive 与 MTA。
- [Import-Module](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/import-module?view=powershell-5.1) 与[系统模块目录](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_psmodulepath?view=powershell-5.1)：固定路径导入。
- [PowerShell 作用域](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_scopes?view=powershell-5.1)：dot-source 在当前作用域添加函数。
- [Add-Type](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/add-type?view=powershell-5.1)、[UIA 线程模型](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)与[TreeWalker](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/navigate-among-ui-automation-elements-with-treewalker)：系统程序集与 MTA 中的有限遍历。
