# 同一 owned-pending 进程的启动判别

状态：2026-10-24 先写合同后实现，测试侧实现、定向离线 fake 回归与静态检查通过；
独立 6.1 sol 后审未发现确定 P1/P2 或阻断；真实 Windows 验证仍待完成，不把观测
记为根因修复。

## 证据与边界

旧候选 `691ea5c` 的 CI `39979235619`、Windows 3.13 job `120501823249` 有四项
原生用例失败。首 probe 的固定阶段为空；第四项不调用 UIA/COM 的 owned-pending
脚本也未在原 12 秒内观察到读后标记。已有 `sent=1` 在 write/drain 前递增，不能证明
子进程已读取；清理后的非零退出也可能来自 terminate/kill，不能冒充自然退出。
本片基于任务核查候选 `340f882`，其后续 CI 结果也不能证明这次旧失败已修复。

只改第四项已有的测试脚本和其测试侧观察包装：仍启动一个 PowerShell、发送一次
原 probe、不增加查询或重试、不预热、不改 MTA/NoProfile/NonInteractive/File、执行
策略、产品 AXLease、原 12 秒及 TTL。仍验证取消后准确 owned helper 退出、reader
终态；不启动真实桌面或在本机执行 PowerShell。

## 同次固定协议

脚本在 ReadLine 前写 `muli-uia-pending=1:script_entered`，ReadLine 返回后写
`muli-uia-pending=1:input_received`，各一完整 ASCII LF/CRLF 行并 Flush，之后仍为
原固定 Sleep。标记总计不足 128 字节，继续由原 stderr reader 计入 1 MB 会话总量，
仅使用原 2048 字节前缀；没有额外 reader、文件或输出正文。诊断写入 best-effort，
不改变原 ReadLine/Sleep/取消合同；“读返回”不宣称原请求已通过业务协议校验。

测试侧 AXHelperLease 子类只观察原 `_start`、第一轮原 stdin write/drain 和第一次
`_retire`。stdin 包装委托原 write/drain/close/wait_closed，不另写、不另 drain、不替换
子进程，也不额外等待。每个入口记录 started/returned；spawn 只按原已有 task/handle
分为 not_started/pending/returned/failed/cancelled/unavailable。

第一次 `_retire` 调用父实现前，无 await 地保存 `before_cleanup`；原 owner 完成收尾后
读取 `after_cleanup`，两者不混用。快照只含固定 lease/exit/spawn 枚举、process 存在、
reader 终态、发送计数 0/1 或 unavailable、两个脚本阶段、阶段序列 valid/invalid/
unobserved、stderr 前缀字节数/是否可能截断；write/drain 的开始与返回为布尔值，
另标明包装是否安装。只观察这些量，不输出 argv、路径、PID、nonce、环境、任意
stderr、provider 信息、异常文本或响应数据。

失败仍抛原异常对象（包括取消）；原清理完成后才 best-effort 附
`uia_pending_diagnostic=` 的最多 4 KiB JSON note。投影失败不覆盖原错；原错存在时
清理错误不替换它；没有原错时清理错误仍传播。cleanup confirmed 仅在原 lease.closed、
进程已有退出码、原 readers 全部终态且外层收尾未抛错时成立，不宣称读到 EOF。

## 判读与验收

- spawn 未返回、已返回但未观察到 script_entered、观察到进入但未读返回，以及读后
  标记已到达分别报告；无标记仍不能单凭此区分宿主初始化、解析/策略及输出传输。
- drain returned 不等于子进程收到；input_received 不等于业务接受；清理后的非零
  不用作清理前自然退出证明。原异常和取消路径保持、一次 spawn/一次原 write/drain、
  纯 fake 覆盖阶段/半行/未知原文/上限、清理前后分离与诊断/清理失败。真实脚本和
  Windows 行为留待后续既有托管 CI，未增加 CI 或自行发起运行。

实现前查询 Context7 的 Python asyncio subprocess 与 PowerShell 文档。包装使用
`Process.stdin` 的 StreamWriter write/drain/close/wait_closed；不把 drain 文档解释为
接收方确认。PowerShell 的固定 Console.Error WriteLine/Flush 沿已有阶段片方式，
不引入新宿主参数或策略设置。

资料：[Python subprocess Process](https://docs.python.org/3/library/asyncio-subprocess.html#asyncio.subprocess.Process)
及 [StreamWriter.drain](https://docs.python.org/3/library/asyncio-stream.html#asyncio.StreamWriter.drain)；
PowerShell 5.1 与 Console.Error 的原版本依据沿
[既有 probe 文档](windows-uia-probe-diagnostics-design.md)，不将 Context7 返回的新版本
宿主文档冒充本次 5.1 参数改动依据。本片没有增加宿主参数。

## 本地验证记录

新增 `test_uia_pending_observation.py` 19 个纯 fake 节点，与既有 probe 13 项、
computer/fixture 42 项合计 74 项通过，pytest 0.26 秒，守卫服务 762 毫秒、峰值
64.5 MiB、swap 0。用 fake subprocess/settle 驱动原 AX request 生命周期，覆盖
spawn 未返回（之后晚返回准确回收）、drain 未返回、等待响应、取消同对象、原生
第四例接线、标记前缀/半行/未知正文/乱序/上限、清理前后分离、投影故障与清理
故障保留原错。没有运行 hosted native 用例、真实 PowerShell、COM 或桌面。

三份 Python 的最终 Ruff/format-check 通过，helper strict mypy 在
`MYPYPATH=src` 下通过。首轮 Ruff 报一处常量 getattr（B009），改为明确的测试侧
类型访问；首轮 mypy 未指定 worktree src 导致基类被当作 Any，修正检查路径后通过，
没有为此增加类型忽略。整轮峰值 94.6 MiB（首次 mypy）、swap 0。所有命令经主仓
`scripts/check_local.py --seconds 30 run --` 串行执行并收取真实退出码；这些本地结果
不证明 Windows 脚本执行、标记传输或旧启动超时已修复。

独立 `sol_inspection_tests` 对这六文件完成静态后审，结论为无确定 P1/P2、无阻断。
该结论与上述 74 项纯回归及整轮 94.6 MiB 峰值分别记录；后审未运行真实 Windows，
不把固定观测或后续不同候选的成功写作旧超时根因已查明、已修复或已经原生验收。
