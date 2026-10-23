# 既有恢复夹具的文件核验分项计时

本片基于签名 `062302d14502a5035528955e75fb0926ce16bfdc`，只调整三个测试文件的观察接缝。主分支 Linux 3.13 shard 2 的 job `120465321425` 日志显示，回退在 19,870 毫秒已 resolved/released，随后真实 GET 耗尽原 20 秒；这不足以将根因归给文件核验。本片不修改生产文件、校验、缓存、期限、真实 CLI 或服务行为，也不延续 native 修复。

`ObservedBackend._check()` 仍恰好调用一次原 `_check()`。只在该同步调用期间，用自动恢复的局部 patch 包住两个现有函数查找点：`managed_deployment.verify_slot_files` 对应 `verify_anchor()` 内的 controller 文件核验，`managed_backend.verify_slot_files` 对应 role 非空时的 selected 文件核验。两个 wrapper 每次仍调用当前原函数一次，以 `time.monotonic()` 的 `try/finally` 分别累计调用数和秒数；原异常和取消继续传播，退出时恢复查找点。没有保存或复用核验结果。

每个 step 在原总 `_check` 计时之外增加 controller/selected 的调用数与毫秒数，其他 `_check` 耗时可从总毫秒减去两项毫秒得到。差额包含其他校验及观察开销；独立取整或饱和后不能当作精确 CPU 时间，计时本身也不构成归因证据。

stdout 继续最多 32 条 `controller-step=` 和四个既有握手，累计上限保持 4 KiB。每条紧凑 JSON 数组固定为十个整数：`[step, elapsed_ms, check_calls, check_ms, controller_calls, controller_ms, selected_calls, selected_ms, phase_index, intent_index]`。计数和毫秒继续夹在 0 至 1,000,000；末两项使用现有 `Phase/Intent` 的 `get_args()` 顺序，并由 worker 与 reader 共享同一映射，避免扩展后的最坏 32 条超限。reader 严格核类型、范围、顺序、32 步上限与枚举索引，诊断行不能代替真实握手。

父进程只在完整验证一条记录后将其加入消费摘要。原失败 note 增加这份至多 32 条的紧凑 `steps`；未知、畸形或未完整收到的行不进入摘要。checkpoint 失败仍失败，但异常消息不再包含原始 stdout/stderr；完整 note 仍受原独立 4 KiB 上限和 JSON fallback 约束。真实 GET 失败也会带上此前已消费的摘要。

验证仅运行纯替身与小型自有 pipe：原函数调用次数、重复调用不缓存、controller/selected/前置失败与取消、lookup 恢复、step 返回/异常、所有新增标量反例、最坏实际枚举索引和全部饱和整数的 32 条加四握手，以及完整摘要加 16 项 timeline、有限 state/http 的 note 字节上限。保留原 EOF、累计字节、未知帧、严格另一 reader 和 33 步拒绝例。未执行真实 CLI、恢复 case、服务或网络集成。

最终 53 项纯例通过（本诊断文件 50 项，加既有 failure note 三项），pytest 0.26 秒、guard 服务 0.885 秒、峰值 65.2 MiB/swap 0。末次将 backend 私有导入明确从模块字典取出并标注 callable 类型后，相关 8 项再次通过，不另计唯一数。三份 Python 的 Ruff、format 与 strict mypy 最终全部通过；静态首轮发现未使用导入、私有导入并非公开类型导出及测试 timeline 标注问题，逐项修正，没有忽略规则或改动生产导出。最终类型检查峰值 227.9 MiB，本批最高为首轮类型检查的 253.5 MiB，swap 均为 0。所有执行逐条经过主仓库绝对路径 guard、每条 30 秒串行，结束后已归还共享 guard；远端分项耗时尚未观察，不声称已证明性能根因或修复真实回退超时。
