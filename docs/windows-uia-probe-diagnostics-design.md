# Windows UIA probe 的固定阶段证据

状态：原八阶段及后续三个输入阶段的纯回归与静态检查已完成；2.43 候选的 Windows
3.12 首 probe 仍超时且没有可见阶段。本文末新增同次首 probe 观察，源码与纯回归已
编写，等待根统一检查与独立后审；真实 Windows 新观察待验。
独立于已冻结的 Invoke 片；
不改变已发布标签，不把增加诊断写成已查明或修复启动超时。

## 已知事实与目标

2.36 发行 run `39596574355` 的 Windows 3.12 job `119210252707`，
`test_real_windows_system_module_probe_mta_and_owned_unicode_tree` 在首个独立
`once=True` probe 等待 `_pending` 时超时。目标 Win32 窗口已经就绪；PS helper
已返回进程句柄，宿主已经写入并 drain 请求。现有日志不能区分脚本启动、系统模块
导入、UIA 程序集初始化与结果写回阶段，也不能据此认定 provider 或执行环境故障。

`AXHelperLease.request` 的同一绝对 operation deadline 包含 spawn、发送和响应；
此 probe 未传额外 deadline，没有重复扣除两次 12 秒的证据。stdout/stderr 已并发
读取并共享 `MAX_SESSION_BYTES=1000000`；stderr 仅保留前
`MAX_STDERR_BYTES=2048` 字节。目标窗夹具另有 4 KiB 预算，不应混为同一捕获器。
根已请求一次同 SHA 稳定性复验，reusable verify 实际展开新矩阵；无论结果如何，
复验成功都不能替代根因证据，本设计不请求额外重跑。

目标是在下一次失败中区分已经经过的固定阶段，并确认准确 helper 是否回收。
原 12 秒、TTL、所有原生断言、异常 outcome、未知动作不重放与强拥有清理保持不变。

## 选择与修改范围

只修改 CI 外层 spawn 记录，看不到被测脚本内部；复制或注入脚本则不再验证包内原件。
因此建议随包 `tools/uia_helper.ps1` 仅增加固定 stderr 标记，CI 测试负责安全投影。
不增加 Settings、环境开关、CLI 参数、文件 writer、日志框架或新的读取协程；不预热
PowerShell，不替换 MTA，不改执行策略，不派生其他命令，也不读取新的 provider 属性。

预计文件为该 PS 资源、`tests/test_uia_native.py` 的首个 probe 失败路径、新小型
`tests/helpers/uia_probe_diagnostics.py` 与专用纯测试，以及 Windows UIA 用户文档。
复用现有 lease 的 stderr 前缀、进程句柄、reader 与清理所有者；不改共享 AX 协议。

## 固定阶段与一次性规则

每行只允许 ASCII 协议 `muli-uia-stage=1:<stage>` 加换行，不含时间、ID、参数或正文。
每个 helper 用固定十一位状态保存已尝试阶段，每位先置位再尝试写出；每阶段最多一次，
整生命周期最多十一行，LF 合计 361 字节、CRLF 合计 372 字节，均不超过 512 字节，
不因重复 probe 或其他请求增长。

| stage | 紧邻的代码位置与能够证明的事实 |
| --- | --- |
| `script_entered` | 固定偏好与小标记函数可用后、UTF-8 writer 构造前；已执行到脚本主体 |
| `utility_importing` | 原固定 `$PSHOME` manifest 导入前 |
| `utility_ready` | 原导入及实际 module.Path 检查全部通过后 |
| `input_waiting` | 首个请求进入原 `Read-BoundedLine` 前；不证明其后的读取已完成 |
| `input_line_received` | 首次有界读取返回非 null 值，即已取得 LF 结束的一整行；不证明 JSON 合法 |
| `request_decoded` | 首次原 `ConvertFrom-Json` 调用返回后；尚未通过版本、ID 与 limits 校验 |
| `probe_received` | 首个 probe 的原帧、版本与 limits 校验通过后，初始化前 |
| `uia_initializing` | 首次 `Initialize-Uia` 的版本/MTA/程序集检查开始前 |
| `uia_ready` | 原 GAC 检查通过并设置 ready 后 |
| `probe_encoded` | 首个 probe 的原成功 `Encode-Result` 返回后，原末次 deadline 检查前 |
| `probe_write_returned` | 该成功 probe 的原 `$writer.WriteLine` 返回后；不是宿主已收到的证明 |

三个 input 阶段仅位于 `requestCount=0` 分支，不能在解码前声称输入就是 probe。
前三级和 UIA 初始化各只尝试一次；probe 的三级仅跟踪首个合法 probe，失败也不为
后续 probe 重置。非 probe 可能先完成 UIA 初始化，因此一般会话不要求十一级全序；
本次 CI 的新建 once probe 才核上述顺序。错误回执不冒充成功 encoded/write 阶段。

标记通过 `[Console]::Error.WriteLine` 与 Flush 写出；仅固定字符串，整个写入置于
best-effort try/catch，不使用 Write-Error 或异常格式化，不输出 `$Error`。
标记仍由现有 stderr reader 全量计费，不增加 2048 字节保留量或 1 MB 会话上限。
诊断不设独立期限；发生在操作内的开销继续计入原期限。stdout 保持原 JSON-lines
和原 AutoFlush 行为，不为阶段观察额外重写、确认或发送任何业务帧。

## 仅 CI 失败时导出

首个 probe 用例保留原异常对象，沿既有 owner 收尾后再附加至多 4 KiB 的固定 JSON
note；不得新增等待期限、跳过失败、改变异常 code/outcome 或让诊断/收尾异常覆盖
原异常。成功路径的清理失败仍是失败；取消仍传播。生产工具/API 不展示 stderr。

投影只读取现有 2048 字节前缀，按完整 LF/CRLF 行精确匹配十一个固定标记；未知原文、
不完整行和非 ASCII 内容不导出。重复或乱序标为 `stage_sequence=invalid`，不据此
推断业务成功。至多十一个枚举及固定标量：`stderr_prefix_capped`、发送计数 0/1、
lease.closed、process 存在、退出类别 running/zero/nonzero/unavailable、readers 是否
全部终态、cleanup confirmed/unconfirmed。reader 终态不等于已读到 EOF；前缀达到
上限只表示可能截断，不声称完整诊断。无路径、PID、nonce、环境、provider 名称、
请求/响应正文、原始异常、stderr 片段或任意远端字段。

缺少最早标记只能说明没有观察到：进程初始化、整份脚本解析、执行策略、输出传输
或标记自身失败仍不可区分。仅有 importing/initializing 也不能断言它们永久停滞。
write_returned 存在而宿主仍无回执可缩小下一轮传输/解码调查范围，不证明宿主接收。
这些标记不参与授权、动作 outcome、重试或恢复决策。

## 验收与官方依据

先用纯替身核十一行及字节上界、重复 probe 不新增标记（静态协议检查）、任意分块与
CRLF/半行、未知/重复/乱序输入、2048 字节截断、完整 JSON note 预算。假 lease
事件门控证明 note 在自有清理后生成、原异常身份/outcome 和取消保留、清理失败不
伪称回收；既有 reader 继续证明 stderr 全部计费。禁止在 Linux 启动 PS 或桌面。
真实 Windows CI 保留现有 probe/三项只读与新增 Invoke 断言；失败 note 来自实际包内
脚本，不将 best-effort 标记缺失变为新的成功条件。仍核直接 helper 已退出，不宣称
回收系统 COM broker 或实际根因已修复；脚本语法、真实阶段输出及一次性行为待远端。

## 本片反例与验证记录

旧调用反例直接进入首个真实 native 测试函数，仅 Computer/lease 是有限替身且硬拒
实际 spawn。事件门控等待旧 finally 的 aclose，确认原 read_timeout/not_started 原样
传播、owned cleanup 已完成，但原异常的 note 为空；1 failed / 0.12 秒，守卫服务
596 毫秒、峰 57.2 MiB、swap 0。未通过缺失新模块的 ImportError 制造红例，脚本没有
阶段标记仅作为源码事实。

修后新文件 13 项通过（0.13 秒、峰 56.7 MiB），覆盖完整固定行与未知正文分离、
半行/CRLF/重复/乱序、2048 字节截断、清理后才附 note、异常身份/outcome、取消
等待收尾、投影与清理失败保原 fault，以及成功路径的清理失败仍传播。既有
`test_uia_computer.py`、`test_uia_native_fixture.py` 两文件 42 项通过（0.17 秒、
58.4 MiB），合计 55 唯一，没有执行 hosted native 用例或真实子进程。
三份 Python Ruff/format-check 通过；首轮仅两条 import 排序，已按规则修复。
新 helper strict mypy 首次报导入边界的 Any 返回，显式标注已知 dict 结果后通过；
首轮 67.9 MiB、修后 65.9 MiB。该注解不改变运行行为，整轮最高 67.9 MiB、swap 0，
全部检查仍经原 `check_local.py --seconds 30` 串行执行。

脚本阶段调用点仅做源静态约束，没有本机解析或执行 PowerShell，不把它称为真实
一次性运行证明。真实 Windows 仍需验证固定脚本语法、stderr 阶段和原 provider/按钮
断言；独立静态实现后审未发现确定 P1/P2。增加观测不等于已经修复旧 probe 超时。

已先查询 Context7 的 PowerShell 文档；其结果主要指向新版本，5.1 合同以微软固定
版本原文为准：[powershell.exe 5.1](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1)
说明 NoProfile/NonInteractive/MTA/File；[Console.Error](https://learn.microsoft.com/en-us/dotnet/api/system.console.error?view=netframework-4.8.1)
是标准 stderr 的 TextWriter；[StreamWriter.AutoFlush](https://learn.microsoft.com/en-us/dotnet/api/system.io.streamwriter.autoflush?view=netframework-4.8.1)
不等于刷新 encoder 或接收方确认。固定 ASCII 标记显式 Flush，仅作为已达代码位置的
有限观察，不是 Windows 启动阶段有时限保证的官方承诺。

后续旧候选 `691ea5c` 的 `39979235619` Windows 3.13 四项 native 失败，包括最小
owned-pending 脚本未见读后标记。新增独立的
[同进程判别合同](windows-uia-pending-observation-design.md)，只观察第四例原进程的
宿主输入与清理边界，当时没有改变原八阶段协议、产品桥接或原有事实记录；本次输入
阶段的扩展单独记录如下，不把旧结果改写成新十一阶段已实测。

## 2.41 首请求输入缺口（新增诊断）

精确 `b89a3dc8838b4e6e9c0cd73cc5bcff6821f87ba1` 的候选 `40003241223` 与
main `40004424238` 各十项成功；发行 `40004425177` 的 Windows 3.13 job
`120588588362` 仅该 probe 失败，调用 12.44 秒。发布步骤没有执行。
原 note 只观察到 `script_entered/utility_importing/utility_ready`，序列有效，
`sent=1`，并确认 helper/readers 清理完成。清理后 nonzero 不是自然退出原因的证明。

`utility_ready` 之后仍要执行原同步有界输入读取、JSON 解码、版本/ID/limits 校验，
才会到 `probe_received` 与 `uia_initializing`。现有证据没有显示进入 UIA 初始化，
不能把失败归因于 COM、程序集加载或 JSON 解码，也没有阶段时间可划分各段耗时。
[Console.In](https://learn.microsoft.com/en-us/dotnet/api/system.console.in?view=netframework-4.8.1)
的读取会同步等待操作完成；PowerShell 5.1 的
[ConvertFrom-Json](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/convertfrom-json?view=powershell-5.1)
把输入字符串转换为对象，其返回不代表本协议校验通过。这些接口事实不是本次超时的
因果证明；缺失 best-effort 标记也不能绝对证明代码位置未到达。

本次只在原调用两侧增加表中的三个固定标记，不增读取、重试、宿主计时代理或正文
输出。原八个调用点不移动；投影按实际首请求顺序识别十一名称，仍可识别旧八阶段
记录。2048 字节前缀、1 MB 会话总额、4 KiB note 与原 12 秒均不变。最坏情况下
未知 stderr 可占满前缀，所有新标记均不可见；只匹配前缀内完整行，半行不导出，
已有阶段缺失不标为乱序，也不因截断推断任何后续阶段。此 CI 全新 helper 的首个
once probe 按表中顺序尝试标记，其任一完整子序列仍有效；重复或逆序的已知行仍为
invalid。此判断不推广到先执行其他命令、再发 probe 的一般会话。

本次专用文件 18 项通过（原 13 项与新增 5 项，0.14 秒），新增覆盖原源码中首请求
读取/解码两侧的调用位置及门控、旧八阶段兼容、输入阶段重复/逆序、每一字节截断。
CRLF 全序列的 0..372 个前缀逐一验证，并以未知完整行挤占 2048 字节保留量，使
可见标记分别只剩相同前缀；之后完整但超出前缀的标记不可泄入结果。没有任意 stderr
时，全十一行可容纳于前缀；未知 stderr 存在时不保证标记全部可见。

既有 `test_uia_pending_observation.py` 19 项与 `test_uia_computer.py` 27 项共
46 项通过（0.20 秒），本次合计 64 个不同用例，无真实进程、PowerShell 或桌面。
两份 Python Ruff/format-check 与限定 strict mypy 通过；格式首轮仅一处换行，按规则
修复。mypy 首轮因测试 helper 的双模块名拒绝检查，在守卫内显式传 `MYPYPATH=src:tests`
并采用 `--explicit-package-bases` 后两文件通过，没有为类型检查改运行代码。
pytest 峰 61.8 MiB，整轮峰 314.2 MiB、swap 0，每条均由原 30 秒守卫串行执行。
实际 11 行及 LF/CRLF 字节长度也经同一守卫按脚本原字符串计算。

以上只证明纯投影/源静态边界；根与独立集成 agent 后审未发现可验证 P1/P2，真实 Windows 新阶段待验。
没有 PowerShell 语法或实际阶段输出的本地证明，也不把增加三个标记写成已修复超时。

## 2.43 同次首 probe 观察缺口

精确 `e664b2ed9e5aed7c34a87f637b8b965d6ef2e659` 的候选 CI `40019282611`
最终八项成功、两项 Windows 失败。3.12 job `120639317991` 的三个自有窗口正文
超时，`3742 passed, 1457 skipped, 3 failed`；首 probe 的完整 traceback 停在原
`AXHelperLease.request` 等待 `_pending`，证明该次原 spawn、write 和 drain 已返回。
note 为 `stages=[]/stage_sequence=unobserved`、`sent=1`，仅确认收尾后 lease、
直接进程和 readers 终态；nonzero 不能反推清理前已经自然退出。

本次与 2.41 的 `utility_ready` 事实不同。最早标记在脚本主体执行后才尝试写出，且为
best effort；空阶段不能证明脚本未进入，更不能归因 Python 版本、COM、JSON 解码、
执行策略或调度。旧投影没有 stderr 字节数，零字节、未知短行和半行可给出相同 note。
原 `_settle_process` 先取消 readers，再丢弃管道余量；清理后前缀不能当作完整 stderr。

### 最小实现与投影

只给测试侧增加 `ProbeHelperLease`，复用 `PendingHelperLease` 已有的原 stdin 包装和
第一次 `_retire` 前快照。首 native 用例在无 await 的构造范围内临时替换既有 UIA
工厂的 lease 类，仍由原 `_new_lease(once=True)` 构造完整 argv；随后立即恢复工厂。
包内 PowerShell 原件、参数、一次 spawn/write/drain、12 秒总期限、TTL、强拥有清理
与所有原生断言均保留。第四例仍使用原 pending 观察；产品 AXLease 和 PS 源码不改。

原 `uia_probe_diagnostic=` 保留既有字段，增加 `before_cleanup/after_cleanup` 两份
有限快照。前者在父 `_retire` 改状态和调度原清理任务之前无 await 保存，后者在原
owner 收尾后读取；投影或时钟失败仍不替换原错误对象、outcome 或取消。原错存在时
收尾错误不覆盖它；成功路径收尾失败仍传播。每条 note 仍限 4096 ASCII bytes。

两份快照复用固定 spawn/lease/exit 枚举、process 存在、readers 全部终态、0/1 发送
计数、原输入包装和 write/drain 布尔量。只增加保留前缀的 `stderr_bytes`、原
`stderr_prefix_capped`、完整白名单 `script_stages/stage_sequence`，以及固定分类：
`empty` 表示前缀零字节；`known_stage` 表示至少一完整白名单行；`unrecognized`
涵盖未知内容、非 ASCII 和半行，不输出或进一步识别这些内容。`known_stage` 仍可能
乱序，由原序列字段表达；capped 只表示可能截断，readers settled 不等于 pipe EOF。

相对耗时仅观察同一 lease 的原边界：`spawn` 从原 `_start` 前至其返回，`send` 从
该返回至原 drain 返回，`response` 从 drain 返回至第一次退休，`cleanup` 从原退休
至原 `_finish` 结束。当前 `phase` 另允许 `not_started/settled`；`settled` 只表示
观察时钟冻结，清理是否成功仍看原 `cleanup` 与进程/readers 状态，不将失败收尾改为
成功。总 `elapsed_ms` 与四个 `phase_ms` 分别向下取整，负量归零、上限为
`2**31-1`；阶段分别取整之和可小于总量。没有墙上时间、绝对时间、PID、路径、
argv、环境、原 stderr、异常或响应正文。所有操作内的采样开销仍计入原期限。

这些量可以区分清理前活着/已退出、零前缀/未知非空前缀，以及原预算主要在哪个宿主
边界消耗，不能据此区分底层 I/O 与调度或证明任何加载根因。没有额外进程、marker、
读取协程、预热、重试、策略设置、第二份超时或 CI 调度。

### 纯回归与独立失败边界

根先在原 30 秒统一守卫运行两个反例：清理前仍运行且 stderr 零字节，与清理前已经
退出且 stderr 含未知短内容。二者先验证原 `AXLeaseError` 身份/outcome、同一 owned
process、一次发送和一次清理，再要求新前后事实；旧投影均在 `before_cleanup`
缺失处失败。实际 `2 failed`、pytest 0.07 秒，守卫 590 毫秒、峰值 60.1 MiB、swap 0。

实现后的测试复用原 pending fake harness，另覆盖完整白名单、半行/未知/前缀上限、
晚 spawn 后准确接管、取消对象、reader 取消而无 EOF、相对毫秒取整/饱和/冻结、
投影/时钟/收尾故障不覆盖原错，以及首 native 工厂 argv/once 与一次生命周期。
根先在两个诊断文件完成53项，再整合native fixture/window/buttons/computer/invoke共
七文件**150 passed**，pytest0.56秒、guard1.160秒、73.3MiB/swap0。限定strict mypy
指出fake Process身份断言和模块私有绑定的类型表达，保持真实`is`语义修正；四文件
最终strict mypy、Ruff与格式通过，类型检查608毫秒、242.3MiB。最后工厂命名空间
表达修正后单项再验1 passed，guard728毫秒、65.3MiB。各步30秒守卫、实际exit0，
没有在开发机执行真实子进程、PowerShell、COM或桌面。独立最终只读后审确认类型
表达和工厂恢复断言没有弱化，未发现剩余P1/P2；真实Windows仍待新精确候选验收。

同轮 3.13 job `120639318011` 则为独立的 Runtime 构造失败，UIA 正文通过；
`test_channel_configuration_revocation_rejects_waiting_owner` 在首行 `make_runtime`
内的第一次 optional 格式查询返回后触发一秒期限，尚未执行配置撤销主体。
`format_read_observation` 为 `elapsed_ms=2255`、setup 366 ms、optional 1888 ms、
queries 1、vm_steps 0；`3744 passed, 1457 skipped, 1 failed`。零 progress 回调不证明
没有执行 SQLite；该失败仍如实保留。本片不改格式分类或预算，不用假时钟、skip 或
后续不同候选成功掩盖此真实失败，也不把新增观察称为已修复 3.12 启动超时。
