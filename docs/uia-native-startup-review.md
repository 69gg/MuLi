# Windows UIA 原生夹具启动诊断

状态：仅诊断和夹具生命周期改动，本地纯假流与最终精确候选的 Windows 验收已通过，
随 2.32.0 发布；旧超时的唯一原因仍未确定。
未延长 12 秒期限、预热、删除断言或增加跳过；没有修改产品 UIA 实现。

## 已观察的失败

第二轮候选为 `c60a77bb396d9ecfbc063284e1b71350c7e35891`，CI
`39557587927`，Windows 3.12 作业 `119080883490`。完整矩阵 7/8；该作业
`3150 passed, 1361 skipped, 1 error`，唯一错误为
`test_real_windows_system_module_probe_mta_and_owned_unicode_tree` 的 fixture setup。

`native_form` 在原 12 秒总期限内等待 `process.stdout.readline()`，最终由
`asyncio.timeout` 抛出 `TimeoutError`，尚未收到首次 Shown 的 JSON 回执，测试正文的
MTA probe、目标发现和树读取均未执行。旧夹具不消费 stderr，也没有启动阶段事实；
故不能据此确认模块加载、WinForms 构造、Shown 或其他阶段的具体原因。trace 中清理后
的 closed/EOF 状态不能反推超时当时的状态。第二轮其余两项原生用例和 Windows 3.13
通过；首轮 `6a8ad38` 的两组 Windows 三项原生用例均通过，二者不能混计。

## 本次诊断边界

`tests/helpers/uia_native_startup.py` 只供这份原生 CI 夹具使用，复用既有
`_OutputBudget`、`_settle_process` 与 `_wait_cleanup`。一个绝对启动 deadline 覆盖
创建进程和所有启动阶段；阶段不会续期。固定脚本只向 stderr 写白名单阶段，记录
脚本进入、系统模块加载、程序集加载、Form 构造、控件就绪、调用消息循环前，以及
HandleCreated / Load / Activated / Shown。重复阶段只记录首次。

stdout 的原就绪 JSON 校验保持不变；同时消费 stderr，两管道合计限 4096 字节。
不保留或公开原始 stderr、异常正文、路径、窗口名、PID、桌面正文或 PowerShell 源码。
阶段的 `elapsed_ms` 是宿主收到标记的相对时间，可能受管道缓冲和调度影响，不能当作
每个加载操作的精确耗时。标记也不证明原生读取主体已执行。

错误诊断在强拥有的直接进程清理和 reader 收尾之后附于原异常，完整 UTF-8 note
不超过 4096 字节。字段仅包括有限阶段、两管道字节/EOF、spawn 状态、清理前后
returncode、清理状态和 reader 完成/失败标志。迟到的 spawn 仍接管后回收，重复取消
不提前丢失拥有权。已就绪后 stderr reader 超预算也必须在 body 退出时失败并回收，
不能被 gather 当作成功吞掉。清理失败不覆盖已有的原始错误。

本片仍只确认自有直接进程的退出，不宣称回收系统 COM broker。新增并发读取能够避免
旧夹具长期完全不消费 stderr 的管道问题；尚无证据证明它是远端超时的原因，也不把
加入诊断表述为已修复该原因。

## 本地检查

全部通过根 `check_local.py --seconds 30 run` 串行执行，单次 1 GiB、合计 2 GiB、
swap 0、共享一逻辑 CPU 的上限不变。无 PowerShell、桌面或真实子进程实验；假流模块
显式禁止实际 `asyncio.create_subprocess_exec`。

- 新 9 项纯假流回归通过，0.11 秒、峰值 56.1 MiB：固定阶段脱敏、两管道总预算、
  已就绪后 reader 失败、单 deadline、EOF、迟到 spawn、重复取消、清理失败保留原异常。
- 既有宿主 27 项通过，3 项原生 Windows 在 Linux 按原门跳过；0.16 秒、59.1 MiB。
- 三份测试 Python 的 Ruff / 格式与 strict mypy 通过。mypy 使用
  `MYPYPATH=src:tests --explicit-package-bases`，最终峰值 187.4 MiB。
  首次 mypy 的模块名映射冲突和四处测试类型/import 边界已修正；整轮峰值 189 MiB，
  全部 swap 0。未将类型检查失败当作运行时或平台根因。

本地结果只覆盖诊断、预算和清理的确定性行为。随后精确签名提交
`b30fdd29982e4a7f18d3e85ec5acecfbaafb4f0d` 的候选 CI `39561080146`
八项全部通过，包含两组真实 Windows 原生检查；同一提交 main `39562088735`
八项与发行 `39562088504` 十一项也全部通过。签名及发行构件证据见
[2.32 审查](review-v2.32.md)，不以此次成功改写前轮失败事实。

## 官方依据

先通过 Context7 核对 WinForms / PowerShell 接口，再补核微软对应版本原文：

- [WinForms 事件顺序](https://learn.microsoft.com/en-us/dotnet/desktop/winforms/order-of-events-in-windows-forms)：HandleCreated、Load、Activated、Shown 是不同阶段。
- [Form.Shown](https://learn.microsoft.com/en-us/dotnet/api/system.windows.forms.form.shown?view=windowsdesktop-9.0)：Shown 对应首次显示，不是进程创建完成的信号。
- [Application.Run](https://learn.microsoft.com/en-us/dotnet/api/system.windows.forms.application.run?view=windowsdesktop-9.0)：启动当前线程消息循环；调用前标记不冒称已经进入循环。
- [Windows PowerShell 5.1 启动参数](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1)：原 NoProfile、NonInteractive、STA 与 File 参数保留，不修改执行策略。
