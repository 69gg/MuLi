# Windows UIA 只读窗口

状态：Windows UIA 只读后端已随 2.32.0 发布。最终精确候选和 main 各八项、
发行十一项检查全部成功，包含 Windows 3.12/3.13 的三个真实原生用例。
前两轮失败仍保留在[发行审查](review-v2.32.md)，不反推旧夹具超时的唯一原因。
没有在开发机调用 PowerShell、读取桌面、安装组件或修改执行策略。

## 使用范围

保留主配置格式和 `computer.backend` 的现有取值。电脑能力仍默认关闭；启用后，
`backend=auto` 在 Windows 选择 UIA。显式 `atspi` / `ax` 不降级，不新增 `uia`
配置枚举；旧固定恢复控制器仍可解析同一份配置。

- `computer_targets({})` 有界读取桌面的直接顶层窗口，返回 `scope=desktop_windows`
  与当前调用者持有的临时 `target_id`。Windows 首片不接受 `application_id` 展开。
- `computer_snapshot({"target_id": "当前窗口标识"})` 读取该窗口的树，返回
  `scope=window`、`snapshot_id`、节点 `id` / `parent_id` / `depth`。必须先发现窗口；
  不传标识、未知或失效标识都拒绝，不回退整个桌面，也不按标题寻找替代窗口。
- `computer_release({})` 释放当前执行的引用。外部原生引用、PID、窗口句柄、模块路径
  与 UIA RuntimeId 不提供给模型。另一个执行收到 `desktop_busy`，旧执行不能释放新执行。
- `computer_action` / `computer_type` 明确 `unsupported_operation`；仍保留原 `computer`
  风险门。发现、读取与释放沿用读取权限，不新增授权旁路。

每个新观察消费旧发现集合。树只是当时的外部资料，不是原子桌面快照或可执行凭据。
UIA 的 Name 可能包含普通应用文字；首片不读取 ValuePattern / TextPattern。明确密码
节点先核 `IsPassword`，再决定是否读取 Name；密码节点返回空名称和 `protected=true`。
所有节点均为 `read_only=true`、`editable=false`、`actions=[]`。

## 固定系统桥接

宿主以 `GetSystemDirectoryW` 定位系统 Windows PowerShell 5.1，不从 PATH 选择程序。
固定参数为 `-NoLogo -NoProfile -NonInteractive -Mta -File` 加随包提供的
`tools/uia_helper.ps1`，启动使用已有 `CREATE_NO_WINDOW`。没有用户可注入的命令、脚本
路径或 PowerShell 参数，也不使用 `-Command` / `-ExecutionPolicy Bypass`。

脚本关闭模块自动加载，只从 `$PSHOME/Modules/Microsoft.PowerShell.Utility/` 的固定
manifest 显式导入 Add-Type 与 JSON 命令，并核实际模块路径。模块命令使用限定名。
Add-Type 只加载系统 UIAutomationClient / UIAutomationTypes 程序集，并核 GAC；
不传 C# 源码、MemberDefinition 或编译选项，不安装 SDK / Pythonnet，不派生外部命令。
脚本资源沿用现有 Hatch 包目录，安装后 smoke 只核资源路径及严格 UTF-8 可读性。

系统 PowerShell、.NET Framework UIA 或当前会话不可用时固定报告失败。脚本仍受系统
执行策略约束；没有脚本签名的安装在受限环境中可能不可用。MuLi 不修改执行策略，
不提示提权、不使用其他宿主绕过策略。状态探测只证明桥接依赖与 MTA 可运行，不枚举
桌面；`session_accessible` 保持未知，不能据此承诺任意应用或隔离桌面可读。

## 引用、期限与预算

同一个短期 MTA helper 持有实际 `AutomationElement`，不持久保存 RuntimeId 后回搜。
选定窗口在读取前后核 PID、原生窗口句柄和该进程启动时间，并继续使用原对象；窗口
销毁或原生调用失败时拒绝。它不保证应用内部节点在读取过程中从未变化。

复用 `AXHelperLease` 的有界 JSON-lines 传输、绝对 TTL、调用者代次和强拥有回收，
不修改 AX 原生实现。`snapshot_ttl_seconds` 从 helper 创建开始计时，读取与状态查询
不续期；`operation_timeout_seconds` 覆盖启动和该次请求。新观察使旧标识失效；取消、
超时、格式错误、执行结束、重载与关闭都回收准确的 helper。回收未确认时保留占用和
固定错误，不把计数或空引用当成退出证明。

Windows 的现有进程原语只终止并等待自有的直接 helper。桥接没有显式派生子进程，
但系统 UIA 可能使用 COM / 跨位数代理；本片不拥有、终止或宣称回收这些系统 broker。

逐节点使用 ControlView TreeWalker，不使用整桌面 `FindAll(Descendants)`。单次节点预算
复用 `computer.max_nodes`；桌面根与 Read-Node 访问尝试消耗预算。每个属性调用和前后
窗口身份复核不另计为节点，因此节点数不是 COM 调用数。相对深度最多 12、直接子项
最多 100；省略只报告原因，未知子树数量不捏造精确值。

helper 的完整 JSON 信封同时受 60000 字符与 250000 UTF-8 字节上限约束，保留完整节点
前缀并注明省略。宿主再按现有 `limits.max_output_chars` 计完整公开 JSON，复用父节点
先于子节点的装箱逻辑。单 helper 仍最多 16 请求、单请求 64 KiB、stdout/stderr 合计
1000000 字节。不完整结果不能证明某个窗口或控件不存在。外部 provider 可能阻塞或
内部先分配内存，不能把遍历预算说成系统 COM 内存硬上限。

## 已检查与待验证

所有本地命令经 `scripts/check_local.py` 串行、每组 30 秒，单次 1 GiB / 共享 2 GiB、
swap 0 / 一逻辑 CPU 上限不变：

- 新宿主 fake 27 项通过；真实 Windows 文件在 Linux 明确跳过 3 项。
- 既有 AXComputer / Computer targets / integration 合计 83 项通过。
- 既有 lease 76 项通过；总计 186 项通过、3 项原生 Windows 跳过。
- 六份 Python 的 Ruff / 格式与两份源码 strict mypy 通过。峰值 128.7 MiB，swap 0。
  首轮 Linux ctypes stub 缺 WinDLL 类型，改为现有 Windows 适配器相同的按需 loader
  后通过；旧 auto Windows unavailable 预期只改为 uia，禁用不启动断言保留。

上述本地结果本身不证明 PowerShell 脚本语法、系统模块实际布局或 UIA 已工作。
`test_uia_native.py` 只在 hosted Windows CI 创建自己的临时 WinForms 窗口；若未在
Windows CI 执行，不能把 skip 当作验收。用例要求：

1. 系统 Utility 的实际路径、自动加载关闭、真实 NoProfile / NonInteractive / MTA、
   helper 无自有窗口、CREATE_NO_WINDOW，以及独立 PSModulePath 下依然能解析和编码 JSON。
2. 实际选定临时窗口读回 Unicode，密码名称和值不出现在结果，所有节点只读；释放只
   回收 reader，临时目标仍存活。关闭该目标后旧引用拒绝，不重新按标题搜。
3. 固定 test-only PowerShell sleeper 已进入等待后，取消请求必须确认直接 helper 已退出。
   该项是进程/协议收尾验证，不冒称实际阻塞 provider 已测试。

根分支另完成 428 项整合回归、全仓静态及完整离线构建/安装 smoke，安装后脚本资源
完整性已验证。精确候选 `6a8ad38` 的 CI `39554055600` 在 Windows 3.12/3.13
实际通过上述三个用例；同一轮 Ubuntu 3.13 的旧回滚用例失败。后续精确候选
`c60a77bb396d9ecfbc063284e1b71350c7e35891` 的 CI `39557587927` 为 7/8，
原 Ubuntu 3.13 回退用例通过，但 Windows 3.12 的
`test_real_windows_system_module_probe_mta_and_owned_unicode_tree` 在夹具等待首次
Shown 回执时触及原 12 秒期限。其测试正文尚未执行；现有日志没有脚本阶段或 stderr
事实，不能判定具体卡点。其余两项原生用例和 Windows 3.13 均通过。

当前仅为该夹具增加固定阶段和退出/回收诊断，stdout/stderr 并发消费、合计仍限
4096 字节；异常在强拥有清理完成后附有界固定标量，不输出 stderr 正文。原 12 秒
启动总期限、三个原生断言和 hosted Windows CI 门均不变，没有预热或新增跳过。
本地新增 9 项纯假流回归通过，宿主 27 项通过、3 项原生 Windows 仅按原规则跳过；
三份测试 Python 的 Ruff、格式和 strict mypy 通过，整轮峰值 189 MiB、swap 0。
这些本地结果只证明诊断与清理逻辑；随后精确 `b30fdd2` 的完整候选、main 和
发行矩阵均通过，真实 Windows 验收由该轮 CI 提供。详见
[启动诊断记录](uia-native-startup-review.md)。
完整计数、失败定位和后续结果见[2.32 审查](review-v2.32.md)。
没有执行用户桌面动作、截图、输入、授权修改或系统 broker 清理。

## 官方依据

Context7 先查 PowerShell / .NET 接口；模块自动加载问题无匹配时再核微软原文：

- [Windows PowerShell 5.1 启动参数](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1)：NoProfile、NonInteractive 与 MTA。
- [Import-Module](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/import-module?view=powershell-5.1) 与 [系统模块目录](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_psmodulepath?view=powershell-5.1)：显式路径导入，避免依赖用户模块搜索。
- [Add-Type](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/add-type?view=powershell-5.1)：AssemblyName 加载已有程序集与源码编译是不同参数集。
- [UIA 线程模型](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)、[TreeWalker](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/navigate-among-ui-automation-elements-with-treewalker) 与 [RuntimeId](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-getruntimeid)：独立 MTA、有限树遍历与非持久身份。
- [进程创建标志](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)、[ConvertTo-Json](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/convertto-json?view=powershell-5.1) 与 [Console 编码](https://learn.microsoft.com/en-us/dotnet/api/system.console.outputencoding?view=netframework-4.8.1)：无控制台、显式 JSON 深度和 UTF-8 传输。
