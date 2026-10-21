# AX 请求超时夹具的阶段边界

状态：2.32.1 独立测试修复，不改 AX 产品、期限或桌面权限；本地运行回归已完成，
远端 macOS 待精确候选复验。所有临时进程仅执行现有 Python JSON-lines helper，
不调用 Accessibility / UIA、系统桌面或 PowerShell。

## 失败证据

独立原生恢复 PR37 的旧基线 `54a91d6` 基于 2.31。
[CI 39561110110 的 macOS 3.13 作业 119092321951](https://github.com/69gg/MuLi/actions/runs/39561110110/job/119092321951)
为 `1 failed, 3131 passed, 1395 skipped`，129.39 秒。唯一失败是
`test_request_timeout_includes_response_and_reaps_process`：预期
`action_unknown_outcome/unknown`，实际为 `read_timeout`。其余七项作业通过，
失败不来自该 PR 的原生恢复改动。

旧测试从 cold spawn 之前就固定 150ms 请求 deadline，却无条件断言动作已经发送。
真实产品在启动前确定 `min(timeout, lease expiry, caller deadline)`，直到最终
write 才置 `_dispatched`；未派发的超时应为 `read_timeout/not_started`，已派发的
动作结果不明才是 `action_unknown_outcome/unknown`。旧断言混合了这两个阶段。
日志没有启动阶段用时，不能进一步断言某个 OS 调用或调度停顿单独耗尽了 150ms。

## 两条独立的证据

响应期用例先在原 lease 请求预算 2 秒内创建自己的真实临时 helper，然后才开始
原 150ms 响应测试。这仅选择测试阶段，不证明 cold spawn 能在 150ms 内完成。
此 lease 的局部 loop 代理只固定同步存活/最终发送检查看到的时钟；Future、Task、
实际 IPC、`asyncio.timeout_at` 和退出回收仍走真实事件循环，不修改全局时钟。
显式要求 `_count == 1`、`_dispatched`，且实际 stdin drain 已完成后，才断言
`action_unknown_outcome/unknown` 和原始 `TimeoutError` 原因。原 150ms 不续期。

新增冷启动用例复用已有 `LaunchGate`，让真实进程句柄在原 150ms deadline 过后、
清理已经开始时才返回。此时请求仍须保持拥有权，不能提前结束。释放 gate 后，
同一真实 child 必须被接管并确认退出，最终严格为 `read_timeout/not_started`、
零派发、零已发送请求，最终发送 fence 也不得调用。这条反证防止请求期限被移到
启动完成之后；不接受任意一种错误作为“都可以”。

原 serial 两请求同 PID、绝对 TTL、协议预算、并发关闭、重复取消和其他真实进程
回收测试均保留。没有增加 skip，没有扩大 150ms、2 秒请求配置或产品 TTL。

## 本地结果

经过共享 `check_local.py --seconds 30 run`，原单次 1 GiB / 共享 2 GiB、swap 0、
共享一逻辑 CPU 限额不变：

- 两阶段定向用例 `2 passed in 0.61s`，服务 1.084 秒，峰值 63.3 MiB。
- 整份 `tests/test_ax_lease.py` 为 `77 passed in 3.24s`，服务 3.696 秒，
  峰值 59 MiB；已包含前述两例，去重共 77 项。
- 单文件 Ruff / 格式检查通过。首轮显式使用 `MYPYPATH=src:tests` 的 strict mypy
  发现四处未改动代码的既有类型边界；同命令读取 `b30fdd2` 原文件也得到相同四错：
  关闭任务两个返回类型的分支推断，以及三处从 transport module 读取未显式导出的
  `_settle_process`。新增两例没有额外错误。随后仅在测试文件给关闭任务加显式类型，
  从原进程模块直接导入同一个 `_settle_process`，保留实际 monkeypatch 目标与运行语义。
  最终 Ruff / 格式和 strict mypy 均通过，mypy 峰值 138.5 MiB；此类型尾差后未重复
  77 项运行组。整轮最高峰 186.8 MiB，全部 swap 0。

上述本地证据不替代 macOS 远端复验，也不证明真实桌面动作已执行。

## 官方依据

经 Context7 核对 [asyncio.timeout_at](https://docs.python.org/3.13/library/asyncio-task.html#asyncio.timeout_at)
使用绝对事件循环时间，超时取消作用于 context 中的等待；
[StreamWriter.drain](https://docs.python.org/3.13/library/asyncio-stream.html#asyncio.StreamWriter.drain)
是流控等待，不能仅凭准备发送的 fence 就声称 drain 已完成。
