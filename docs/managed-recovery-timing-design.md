# 既有恢复夹具的文件核验分项计时

本片基于签名 `062302d14502a5035528955e75fb0926ce16bfdc`，只调整三个测试文件的观察接缝。主分支 Linux 3.13 shard 2 的 job `120465321425` 日志显示，回退在 19,870 毫秒已 resolved/released，随后真实 GET 耗尽原 20 秒；这不足以将根因归给文件核验。本片不修改生产文件、校验、缓存、期限、真实 CLI 或服务行为，也不延续 native 修复。

`ObservedBackend._check()` 仍恰好调用一次原 `_check()`。只在该同步调用期间，用自动恢复的局部 patch 包住两个现有函数查找点：`managed_deployment.verify_slot_files` 对应 `verify_anchor()` 内的 controller 文件核验，`managed_backend.verify_slot_files` 对应 role 非空时的 selected 文件核验。两个 wrapper 每次仍调用当前原函数一次，以 `time.monotonic()` 的 `try/finally` 分别累计调用数和秒数；原异常和取消继续传播，退出时恢复查找点。没有保存或复用核验结果。

每个 step 在原总 `_check` 计时之外增加 controller/selected 的调用数与毫秒数，其他 `_check` 耗时可从总毫秒减去两项毫秒得到。差额包含其他校验及观察开销；独立取整或饱和后不能当作精确 CPU 时间，计时本身也不构成归因证据。

stdout 继续最多 32 条 `controller-step=` 和四个既有握手，累计上限保持 4 KiB。每条紧凑 JSON 数组固定为十个整数：`[step, elapsed_ms, check_calls, check_ms, controller_calls, controller_ms, selected_calls, selected_ms, phase_index, intent_index]`。计数和毫秒继续夹在 0 至 1,000,000；末两项使用现有 `Phase/Intent` 的 `get_args()` 顺序，并由 worker 与 reader 共享同一映射，避免扩展后的最坏 32 条超限。reader 严格核类型、范围、顺序、32 步上限与枚举索引，诊断行不能代替真实握手。

父进程只在完整验证一条记录后将其加入消费摘要。原失败 note 增加这份至多 32 条的紧凑 `steps`；未知、畸形或未完整收到的行不进入摘要。checkpoint 失败仍失败，但异常消息不再包含原始 stdout/stderr；完整 note 仍受原独立 4 KiB 上限和 JSON fallback 约束。真实 GET 失败也会带上此前已消费的摘要。

验证仅运行纯替身与小型自有 pipe：原函数调用次数、重复调用不缓存、controller/selected/前置失败与取消、lookup 恢复、step 返回/异常、所有新增标量反例、最坏实际枚举索引和全部饱和整数的 32 条加四握手，以及完整摘要加 16 项 timeline、有限 state/http 的 note 字节上限。保留原 EOF、累计字节、未知帧、严格另一 reader 和 33 步拒绝例。未执行真实 CLI、恢复 case、服务或网络集成。

最终 53 项纯例通过（本诊断文件 50 项，加既有 failure note 三项），pytest 0.26 秒、guard 服务 0.885 秒、峰值 65.2 MiB/swap 0。末次将 backend 私有导入明确从模块字典取出并标注 callable 类型后，相关 8 项再次通过，不另计唯一数。三份 Python 的 Ruff、format 与 strict mypy 最终全部通过；静态首轮发现未使用导入、私有导入并非公开类型导出及测试 timeline 标注问题，逐项修正，没有忽略规则或改动生产导出。最终类型检查峰值 227.9 MiB，本批最高为首轮类型检查的 253.5 MiB，swap 均为 0。所有执行逐条经过主仓库绝对路径 guard、每条 30 秒串行，结束后已归还共享 guard；远端分项耗时尚未观察，不声称已证明性能根因或修复真实回退超时。

## 2.42 候选 CI 证据与同次核验去重

上述段落记录前一诊断片的范围与本地结果。后续候选 `c1f8bbe` 的 run `40009696868`、Ubuntu Python 3.13 shard 2 job `120606586618`，以及 native16 的另一 Ubuntu shard，均在原 20 秒窗口最后的 released 健康读取超时。候选控制器在 19,810 毫秒发出 resolved；另一日志为 19,889 毫秒。两次最终持久状态均为 rolled_back/released，但这不等于整个验收通过。

候选旧日志的 11 条 steps 累加为 211 次 `_check`、211 次 controller 与 135 次 selected 完整 slot 文件核验，合计 346 次。两项文件核验累计约 13.966 秒，占 `_check` 累计 14.674 秒约 95%。这些是分项壁钟毫秒的取整累计，不是 CPU 时间；47/37 等计数不是 open 系统调用计数。代码每次核验都会重新读取并哈希解释器、pyvenv、兼容声明和有限包目录，嵌套耐久确认 guard 放大了重复扫描次数。

最小生产修正仅限单次同步 `_check`：`verify_anchor` 已完整核验 controller，若 selected 的完整 `SlotIdentity` 与 controller 相等，不再重复核验同一安装。不同 slot 即使共享完全相同的解释器仍分别核验；不跨 `_check` 缓存，不按 resolved_python 合并，不删除任何效果前或 await 后的 guard，也不改变文件耐久确认、原 20 秒测试窗口、事务期限或回退保留时间。

旧候选日志中，回滚 base 与 controller 相同的步骤 5–10 共有 72 次 selected 核验，累计约 3.077 秒；这是旧日志中将被去除的重复工作量推算，不是修复后的实测收益，更不代表远端 CI 已通过。新增回归复用真实 slot 文件与完整哈希，验证值相等但不是同一 Python 对象的 slot 只扫描一次、不同 slot 共享解释器仍分别扫描，以及两次 `_check` 之间修改包文件仍被拒绝。真实进程、systemd 和远端耗时须另行验证。

本地红绿验证均经主仓库 `check_local.py --seconds 30` 串行执行：旧生产三项回归为 1 failed / 2 passed（0.84 秒，guard 1.346 秒、68.2 MiB）；最小修正后的新例及 guard/期限旧例 9 passed（4.59 秒），切换链、损坏 target 恢复和正逆序确认另 5 passed（14.04 秒）。这是 14 项不重叠定向回归，不是远端或真实进程耗时对比。生产源码 strict mypy、两份 Python 的 Ruff 与最终格式检查通过。额外将旧测试文件纳入 mypy 时，在本次未改的旧行及导入夹具报告 15 项类型诊断，本片未扩大修复。整轮峰值 266.5 MiB、swap 0；未运行真实恢复进程案例，也未重跑远端 CI。
