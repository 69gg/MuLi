# UIA 测试目标的独立 Win32 夹具

状态：从精确 `1cf156b7b498c394b4aa719e3c3391ddafaba003` 隔离实施。初次整合
`753e0e5` 的 CI 为六项成功、两项 Windows 失败，定位到虚拟环境启动器与实际目标的
PID 不同。直接基础解释器修正后的本地 52 项及静态已通过，新的 Windows 矩阵待验。
仅测试代码与文档，不改产品后端或已发布标签。

## 精确失败与可证实边界

2.32.1 main CI `39565663386`、Windows 3.12 作业 `119107199623` 为
`3169 passed, 1361 skipped, 1 error`。唯一错误是
`test_real_windows_system_module_probe_mta_and_owned_unicode_tree` 的 setup 超过原
12 秒，完整 main 为 7/8；同一提交的发行 `39565663502` 十一项通过。不能用发行成功
覆盖这次 main 失败，也不能把不同矩阵的成功项合成一次全绿。

已有 4 KiB 固定诊断只看到 `spawn_returned`（相对收到标记时间 0 ms），没有最早的
`script_entered`；stdout/stderr 均 0 字节且没有 EOF。清理前进程仍存活，之后准确
直接进程已回收、reader 已收尾，没有 reader 错误或输出超预算。这里尚未开始产品
MTA probe、目标发现或树读取，不能称为已定位 UIA、Form.Shown 或产品读取超时。

[CreateProcess 官方合同](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw)
说明返回时不保证新进程初始化完成；[PowerShell 启动说明](https://learn.microsoft.com/en-us/powershell/scripting/dev-cross-plat/performance/startup-performance)
区分进程创建、SessionState 初始化与 profile。Windows PowerShell 5.1 还要加载系统
.NET Framework，`-NoProfile` 仅排除 profile。当前证据不能区分引擎、策略检查、调度
或其它初始化阶段，也不能归因杀软、缓存或 CRL。没有清缓存、改执行策略或加期限。

## 最小改动

旧目标窗由第二个系统 PowerShell 的固定 WinForms 脚本创建；产品本身则使用独立
PowerShell MTA helper 读取它。本片只去除测试目标的额外 PS/.NET/WinForms 依赖：

- 自有 Python `-I -B` 子进程执行固定测试 helper，仅 hosted Windows CI 可创建窗口；
  没有 shell、外部命令、COM 注解组件、SDK、源编译或新依赖。
- 用 RegisterClassW/CreateWindowExW 创建自有离屏、不激活、无任务栏按钮的窗口；
  子控件都是系统 STATIC、EDIT、BUTTON，微软为标准控件提供真实 UIA provider。
- 自有 WM_APP 消息必须经过 GetMessage/DispatchMessage 循环，才输出原有
  `{pid,title}` 严格 UTF-8 JSON 就绪回执；并非创建 HWND 后立即冒称循环已经运行。
- WNDPROC 使用 stdcall；HWND、WPARAM、LPARAM、LRESULT 按指针宽度定义，UINT/DWORD
  固定 32 位；MSG 保留 lPrivate。GetMessage 的 -1 错误独立失败，不当普通消息派发。
- 窗口和控件只在所属子进程/线程创建、显示、销毁；退出注销自己的 class。父夹具继续
  原强拥有直接进程回收，重复取消、迟到 spawn、reader 失败和已有异常保留均不改。

原 12 秒包含实际子进程创建至就绪，阶段不续期；原 stdout/stderr 合计 4096 字节和
清理后固定诊断保留，只增 Win32 阶段白名单。没有预热产品 PowerShell，也没有额外
给产品启动预算。产品的固定系统路径、NoProfile、NonInteractive、MTA、Utility 模块
实际路径、CREATE_NO_WINDOW、Unicode、旧引用拒绝和取消回收三项原生测试仍在原处。

## 密码覆盖

可见 STATIC 保留原 Unicode/引号/反斜杠文本。密码 EDIT 仍含原
`private-password-value`，使用 ES_PASSWORD；紧邻前置隐藏 STATIC 含原
`private-password-label`。依据微软 [Win32 edit Name](https://learn.microsoft.com/en-us/accessibility-tools-docs/items/win32/edit_name)
合同，隐藏 label 不单独暴露 UIA，仍可成为后继 EDIT 的 Name，无需新增 IAccPropServices
或手写 COM。原 native 断言保留：必须确有 protected 节点且 Name 为空，两项私有哨兵
均不得出现在完整公开 JSON 中。

这项关联仍须 Windows 真 provider 验证。Python fake 回归只能证明按官方方式设置了
控件和样式，不能独立证明系统最终的 Name 值。既有受控 host 回归证明 protected +
非空 Name 回执拒绝；生产 PS 源码按 IsPassword 分支避免读取 Name，但没有把 host
回执测试称为动态 getter 访问证明。本片不增加另一套 PS AST 提取或 getter 测试。

## 验证安排与限制

初版在根明确交接共享 guard 后，经绝对路径 `check_local.py --seconds 30 run` 串行完成：

- 新 `test_uia_native_window.py` 的 11 项、既有启动/清理 9 项与 UIA host 27 项，
  合计 **47 passed、3 native Windows skipped**；0.22 秒、服务 0.731 秒、峰值
  62.3 MiB、swap 0。三个 skip 保持原 hosted Windows CI 门，不作为 Windows 验收。
- 四个修改 Python 的 Ruff/格式与限定 strict mypy 通过；mypy 使用
  `MYPYPATH=src:tests --explicit-package-bases --follow-imports=silent`，最终峰值
  147.7 MiB。首次 Ruff 指出常量 getattr，改为可缺省的 Windows 按需 loader；首次
  mypy 指出测试引用非显式导出的 `module.ctypes`，改直接已导入的同一个 ctypes。
  整轮最高 244.4 MiB、swap 0；这些静态错误不作为原生启动根因。

纯 API 测试硬拒绝真实 DLL/子进程，覆盖实际消息派发后就绪、Unicode/隐藏 label/
密码样式、pointer-sized 回调、GetMessage 错误、部分构造失败、回调异常和注册/
销毁顺序。共享资源限制仍为单次 1 GiB、合计 2 GiB、CPU 1、swap 0，每次 30 秒，
没有提高上限或绕过保护。

本轮未在开发机运行 PowerShell、创建桌面窗口或安装组件。新的真实 Windows 矩阵仍
待执行；去除测试目标的额外初始化依赖不等于确认或修复系统 PowerShell 冷启动根因，
也不保证任意机器的 Python/PowerShell 都能在 12 秒内准备好。

## 首个整合矩阵与 PID 修正

精确候选 `753e0e50528091c55566c306f4aedac502387cbd`、CI `39573510582` attempt 1
完整终态为 **6/8 成功**。质量、MCP、Ubuntu 3.12/3.13 与 macOS 3.12/3.13 成功；
Windows 3.13 作业 `119133264765` 和 Windows 3.12 作业 `119133264818` 均为
`3200 passed, 1361 skipped, 2 errors`，错误仅在前两项 native 的目标窗 setup。

这次固定诊断已依次到达 Python、class、window、controls、show 和实际
`message_loop_ready`；首例分别仅 95 ms 和 79 ms，第二例为 73 ms 和 62 ms。
错误来自原 `_read_ready` 的 `{pid: process.pid, title}` 严格比较，不是 12 秒超时、
输出超预算或未读 stderr。原强拥有清理均已 reap，第三项产品 helper 取消用例通过。

先经 Context7，再核固定 CPython v3.13.9 的
[venvlauncher.c](https://github.com/python/cpython/blob/v3.13.9/PC/venvlauncher.c)：
Windows venv 的 `python.exe` 用 CreateProcessW 启动真正解释器并等待其退出。
[getpath.py](https://github.com/python/cpython/blob/v3.13.9/Modules/getpath.py)
把真实基础入口和 venv 启动入口分开。因此 asyncio 持有的直接 launcher PID 与
目标回执中的 `os.getpid()` 不同，原夹具把两者当成同一进程的假设不成立。

本次仅让 **stdlib-only 测试目标** 在 Windows 直接使用当前 CPython 的
`sys._base_executable`，保留 `-I -B`；缺失或非绝对路径明确拒绝，不回退启动器。
这是当前 CPython 实现提供的内部信息，不声称为通用 Python 解释器的公共 API。
其它平台的 argv 保持原 `sys.executable`。严格 PID/标题回执、原三项 native 正文、
12 秒、4 KiB 及直接进程清理全部不变，也没有接受任意子 PID 或改动产品 PowerShell。

修正的纯端到端反例把标准 Win32 API double 实际消息派发产生的 `_ready` UTF-8
字节送入原 `observe_native_form`，只在 fake 启动边界区分 launcher 与 base PID。
旧码稳定在同一 `readiness did not match` 处失败（0.06 秒、54.2 MiB）；修后通过，
证明 producer/reader 的衔接，不冒称已在 Linux 执行 Windows redirector。

本轮再次按根独占 guard 串行受限检查：16 项目标 fixture、9 项 startup 和 27 项
host，合计 **52 passed、3 原 native Linux skipped**，0.22 秒、峰值 58.4 MiB；
四份测试 Python 的 Ruff/格式和限定 strict mypy 通过，整轮峰值 145.2 MiB、swap 0。
首轮静态仅发现 import 顺序和 fake Process 的显式 cast 边界，修后无产品语义变化；
端到端单例在该类型收尾后再通过 0.04 秒，已包含于上述 52 项，不重复累计。
测试替身没有启动目标子进程、加载 Windows DLL、运行 PowerShell 或创建窗口；真实
目标 PID、provider 和密码 Name 关联仍须由后续精确 Windows CI 证明。
失败旧 run 已闭合，不盲重跑。

## 最终 Windows 验收

精确签名 `1cf078dbd31c4f71054328de9cf02a46f2805d8f` 的候选
`39576755760`、main `39578053330` 和发行 `39578052294` 均为 attempt 1，
各自 Windows 3.12/3.13 作业全部成功，原三个 native 用例、严格 PID/标题、
12 秒、4 KiB 和直接子进程清理没有放宽。候选共 8/8、main 8/8、发行 11/11，
完整发行及两包元数据见[2.33 审查](review-v2.33.md#精确发行验收)。

这证明本片在这些 GitHub-hosted Windows 环境中的实际目标窗口、provider 与密码
Name 关联通过，不推断任意机器的启动上界，也不反向认定旧 PowerShell 冷启动的
内部原因。本机仍未创建桌面窗口；首个失败矩阵保持原记录。

## 接口依据

先经 Context7 查询 Win32 API，再对照微软逐项原文补足结构与返回值：

- [WNDCLASSW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-wndclassw)、[WNDPROC](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nc-winuser-wndproc) 与 [Windows 数据类型](https://learn.microsoft.com/en-us/windows/win32/winprog/windows-data-types)。
- [MSG](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-msg)、[GetMessageW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getmessagew) 与 [CreateWindowExW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-createwindowexw)。
- [标准控件 provider](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-providersoverview)、[ES_PASSWORD](https://learn.microsoft.com/en-us/windows/win32/controls/edit-control-styles) 和 [DestroyWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-destroywindow)。
