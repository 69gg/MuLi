# Windows UIA 窗口与按钮

状态：Windows UIA 只读后端已随 2.32.0 发布。最终精确候选和 main 各八项、
发行十一项检查全部成功，包含 Windows 3.12/3.13 的三个真实原生用例。
前两轮失败仍保留在[发行审查](review-v2.32.md)，不反推旧夹具超时的唯一原因。
没有在开发机调用 PowerShell、读取桌面、安装组件或修改执行策略。

当前开发片增加普通按钮的一次 `invoke`，输入继续不可用；受限本地回归与静态检查
已通过，最终独立实现审查与真实 Windows 动作验收仍待完成。下面明确区分已发布只读首片的历史
证据和新能力，不能把历史只读矩阵当作按钮动作已经通过。详见[Invoke 设计](windows-uia-actions-design.md)。

2.32.1 后续 main 的 Windows 3.12 在测试目标窗夹具启动时再次超时，发行矩阵仍全部
通过。本轮仅把被读取的测试目标从额外 PowerShell/WinForms 进程改为 stdlib Win32
临时窗口；产品 PowerShell/MTA 桥接、12 秒期限和三项原生断言不变，尚待新矩阵验证。
首个整合 `753e0e5` 矩阵为 6/8：两项 Windows 在目标已就绪后的严格 PID 比较失败，
已定位 Windows venv 启动器与实际 Python 为不同进程。测试目标现直接使用 CPython
基础解释器，保持同 PID 校验；修后远端待验。
零阶段事实、这次不同的失败与覆盖边界见[目标窗夹具记录](uia-native-win32-fixture-review.md)。

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
- `computer_action({"snapshot_id":"当前快照","node_id":"按钮节点","action":"invoke"})`
  只接受该快照实际展示、明确非密码且启用的普通 Button；`computer_type` 继续拒绝。
  动作保留原 `computer` 风险门，发现、读取与释放沿用读取权限，不新增授权旁路。

每个新观察消费旧发现与快照。树只是当时的外部资料，不是原子桌面快照；名称本身
不能授权操作，只有本次公开快照中的临时标识与明确能力才可用于一次尝试。
UIA 的 Name 可能包含普通应用文字；首片不读取 ValuePattern / TextPattern。明确密码
节点先核 `IsPassword`，再决定是否读取 Name；密码节点返回空名称和 `protected=true`。
只有能取得 InvokePattern 的普通非密码、启用 Button 才提供 `actions=["invoke"]`、
`read_only=false`；其它节点保持 `read_only=true`、`actions=[]`。所有节点的
`editable=false`，不读 Value/Text，也不使用键盘、焦点、坐标或另一模式兜底。

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

新观察尝试、有效快照的一次操作尝试都在首个 await 前消费旧操作凭据。坏节点、
不支持操作、取消与失败不会复活旧凭据。宿主只缓存最终 JSON 中实际展示的节点；
helper 同样只保留已装箱节点的实际对象。最终 write 前复用 Runtime 的同步
dispatch_guard，重新检查真实执行、新输入、暂停与关闭。Invoke 前向原对象核密码、
Button 角色、启用、非保护名称、同 PID/窗口 stamp 及最多 12 层原窗口父链；
不使用 RuntimeId、标题或句柄寻找替代控件。

成功仅表示 `{"ok":true,"outcome":"accepted","effect_verified":false}`。进入原生
Invoke 后的异常、丢回执或超时均保守为 `action_unknown_outcome/unknown`；调用前拒绝
保留 `not_started`。取消不撤销已经发生的界面效果，清理失败不将 accepted/unknown
改成可重试错误。不自动重放，下一步需重新观察。

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

## 已发布只读首片的检查与后续记录

所有本地命令经 `scripts/check_local.py` 串行、每组 30 秒，单次 1 GiB / 共享 2 GiB、
swap 0 / 一逻辑 CPU 上限不变：

- 新宿主 fake 27 项通过；真实 Windows 文件在 Linux 明确跳过 3 项。
- 既有 AXComputer / Computer targets / integration 合计 83 项通过。
- 既有 lease 76 项通过；总计 186 项通过、3 项原生 Windows 跳过。
- 六份 Python 的 Ruff / 格式与两份源码 strict mypy 通过。峰值 128.7 MiB，swap 0。
  首轮 Linux ctypes stub 缺 WinDLL 类型，改为现有 Windows 适配器相同的按需 loader
  后通过；旧 auto Windows unavailable 预期只改为 uia，禁用不启动断言保留。

上述本地结果本身不证明 PowerShell 脚本语法、系统模块实际布局或 UIA 已工作。
`test_uia_native.py` 只在 hosted Windows CI 创建自己的临时标准 Win32 窗口；若未在
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

当前目标夹具不再需要独立 PowerShell/.NET/WinForms 启动。固定 Python `-I -B` 子进程
只创建自有窗口及 STATIC、密码 EDIT、BUTTON；消息循环实际处理自有就绪消息后才返回
原 PID/标题回执，继续服从原 12 秒启动总期限与合计 4 KiB 管道/诊断预算。隐藏的前置
STATIC 依微软标准控件合同提供密码 EDIT 的 Name，本身不作为 UIA 节点暴露；原私有
label/value 不泄露、protected 空 name 和只读断言保留。受控宿主测试另拒绝 protected
非空 name 回执；它不等于动态证明 PowerShell Name getter 从未调用。
新目标的真实 UIA 可枚举性、密码 Name 关联与无窗口启动仍须由 Windows CI 证明；
不能用 Linux fake API 结果替代，也不把去除额外依赖说成查明旧 PS 启动根因。
初版新纯 fixture 11 项、既有 startup 9 项及 host 27 项合计 47 项通过，原 native
3 项在 Linux 跳过，静态通过；随后首个整合矩阵的两个 Windows 作业在严格 PID 比较
失败。修正仅把 stdlib-only 目标入口改为 Windows CPython 的 `sys._base_executable`，
不接受子 PID；缺失/非绝对入口拒绝，产品 PS/MTA 与预算未改。纯消息派发→原就绪
reader 的端到端旧码红、修后绿；16 fixture + 9 startup + 27 host 共 52 项通过、
3 原 native Linux skip，四份 Python 静态通过，本轮最高 145.2 MiB、swap 0。
精确候选、job、历史首轮静态与远端待验边界见上述目标夹具记录。
这里的远端待验是该夹具修正时的状态；随后精确 `1cf078d` 的 2.33.0 候选、main
及发行矩阵均成功，已提供该只读夹具在双 Windows 矩阵的实际验收，不能用它替代
本次新增 Invoke 动作的验证。

## 本片 Invoke 验证

修改前两条真实宿主反例已失败，证明旧快照不能提供按钮能力且旧 UIA 错误转换丢失
unknown outcome；详见[设计中的红例记录](windows-uia-actions-design.md)。本地回归
仅使用假 lease、真实 Runtime/Store 和纯 Win32 调用替身，明确拒绝真实子进程。
专用 Invoke 29 项、宿主/启动/窗口夹具 63 项、既有 Computer/AX 纯回归 101 项通过，
合计 193 个不同用例（40 新增、153 既有）；专用首轮 28 项与后续 29 项重叠，
不重复累计。4 项 hosted Windows 原生用例在 Linux 按原门控跳过。
9 份 Python 的 Ruff/格式与两份源码 strict mypy 通过；pytest 峰值 67.2 MiB，
类型检查峰值 213.4 MiB，swap 0，每条命令仍由原 30 秒守卫串行执行。
最终实现独审与真实 Windows provider 验收待完成，没有运行开发机 PowerShell 或桌面。

原有三项 hosted Windows 原生用例与 12 秒启动、4 KiB 两管道/诊断预算保留；新用例
只操作自身临时窗的 BUTTON。标准控件的 `WM_COMMAND/BN_CLICKED` 必须同时匹配
原父 HWND、控制 ID 与按钮 HWND，才发送一次固定计数。helper 的 accepted 与该
计数分别断言并共用原操作期限。禁用按钮及密码无 Invoke，旧快照不能再次调用。
输出 reader 在 readiness 后继续消费同一 stdout，按钮事件仍计入原 4 KiB 总额，
重复或错误事件使夹具失败并强拥有回收。上述真实 Windows provider、动作和消息
关联仍待远端矩阵，不以 Linux fake 或只读历史通过替代。

## 后续 probe 阶段证据

2.36 发行的 Windows 3.12 job `119210252707` 曾在目标窗已经就绪后，独立 PS
probe 等待回执超时；现有日志不足以区分启动、模块导入、UIA 初始化与写回阶段。
本开发片仅给精确包内脚本增加每 helper 最多八行固定 stderr 标记，仍计入原会话
预算；CI 在自有 helper 收尾后，只附固定阶段和回收标量。没有扩大 12 秒、修改
MTA/执行策略、预热或自动重试，生产 API 不返回原始 stderr。缺少标记不等于确定
停滞原因，WriteLine 返回也不等于宿主已经收到。实现与真实 Windows 验证状态见
[阶段证据设计](windows-uia-probe-diagnostics-design.md)；不把观测片描述成根因修复。
本地仅运行 13 新增与 42 既有纯用例，三份 Python 静态及新 helper 类型检查通过，
峰值 67.9 MiB、swap 0；独立静态后审未发现确定 P1/P2，真实脚本阶段输出仍待验。

## 官方依据

Context7 先查 PowerShell / .NET 接口；模块自动加载问题无匹配时再核微软原文：

- [Windows PowerShell 5.1 启动参数](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1)：NoProfile、NonInteractive 与 MTA。
- [Import-Module](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/import-module?view=powershell-5.1) 与 [系统模块目录](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_psmodulepath?view=powershell-5.1)：显式路径导入，避免依赖用户模块搜索。
- [Add-Type](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/add-type?view=powershell-5.1)：AssemblyName 加载已有程序集与源码编译是不同参数集。
- [UIA 线程模型](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)、[TreeWalker](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/navigate-among-ui-automation-elements-with-treewalker) 与 [RuntimeId](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-getruntimeid)：独立 MTA、有限树遍历与非持久身份。
- [进程创建标志](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)、[ConvertTo-Json](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/convertto-json?view=powershell-5.1) 与 [Console 编码](https://learn.microsoft.com/en-us/dotnet/api/system.console.outputencoding?view=netframework-4.8.1)：无控制台、显式 JSON 深度和 UTF-8 传输。
