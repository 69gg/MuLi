# AX 绝对 TTL 测试夹具修复

状态：2.28 独立 CI 修复，基线 `104c98c`；仅修改测试，不改变 AX 产品期限或后端行为。受限验证与独立实现审查通过，待整合和远端 CI 复验。

## 精确故障与范围

[main CI 39543241444 / macOS Python 3.13](https://github.com/69gg/MuLi/actions/runs/39543241444/job/119035539442) 唯一失败是 `test_absolute_ttl_early_callback_rechecks_and_does_not_renew`：早回调及原期限断言已通过，随后 `await item.request("snapshot", {})` 等待协议响应时被 `timeout_at(operation_deadline)` 取消，最终报告 `AXLeaseError: read_timeout`。该 job 为 2924 passed、1302 skipped、1 failed，109.79 秒。旧夹具的 `lease(ttl=0.2)` 使用真实 Python 子进程；请求在启动之前就以 `min(loop.time() + timeout, expires_at)` 固定期限，因此真实 spawn、IPC 和调度都占用这 200ms。日志没有足够证据进一步分解各阶段耗时。

该测试要证明的是“早回调重核、读取不续租、原绝对时间到期后拒绝复用”，不应同时要求本机或共享 runner 在 200ms 内启动解释器并完成 IPC。没有扩大 TTL、请求期限或 CI 超时，没有 skip、重跑碰运气，也没有修改 `src/muli/tools/ax_lease.py`。

## 确定性边界

保留 `ttl=0.2`，构造后先取消真实租约定时器。仅替换该 lease 的 loop 代理，让 `time` 和 `call_at` 可控；创建 Future、清理 Task 仍委托真实 loop，不修改全局时钟或 scheduler。受控 helper readiness、管道 drain 和匹配 request ID 的响应都立即完成且不让出 loop；真实 `request`、期限计算、JSON 协议解析、`_expire`、拒绝复用及关闭路径继续执行。

测试显式触发两次早回调，要求每次仍约到最初的 `expires_at`。两次读取继续保持同一期限，并验证发送给 helper 的剩余预算依次缩短为 TTL 的一半、四分之一。最后只推进受控时钟至原期限，不重写 `expires_at`，触发真实过期路径；新请求必须报 `lease_expired`、不能增加已发送请求数，且清理必须完成。

同文件的真实 `test_serial_requests_keep_one_process_and_return_only_result` 保留两个请求共享 PID、不同 request ID、期限不续及进程回收断言；真实 request timeout、launch cancellation 和并发关闭等测试也保持原样。它们与这个状态机单测分别提供传输和 TTL 证据。全部使用临时 Python helper，不调用 macOS Accessibility、TCC 或真实桌面，不能据此声称真实桌面动作已验收。

依据 [Python 的绝对 asyncio deadline](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout_at) 与 [延迟回调/事件循环时钟](https://docs.python.org/3/library/asyncio-eventloop.html#scheduling-delayed-callbacks)；文档通过 Context7 核对，使用本项目已有 pytest monkeypatch 与标准库 mock，无新增依赖。

## 验证

全部检查在明确交接的共享守卫下，以 `check_local.py --seconds 30` 串行执行，保持单次 1 GiB、共享 2 GiB、swap 0、共享单 CPU，未操作用户服务或桌面：

- 目标单例 1 passed，0.07 秒，峰 58.7 MiB。
- `tests/test_ax_lease.py` 整文件 76 passed，3.06 秒，峰 59.6 MiB；已包含上述单例，去重为 76 项。真实 serial、timeout、launch、关闭和协议异常测试均保持并通过。
- Ruff 与格式检查通过；该测试文件的 `mypy --follow-imports=silent` 通过，峰 137.9 MiB。首轮类型检查因 silent imports 将捕获的时钟值推为 Any，只补 `expiry/current` 的显式 float 注解后通过；首轮峰 164.4 MiB，没有更改运行语义或扩展源码范围。全部 swap 为 0。
- 父与独立 reviewer 静态审查均无阻断，`git diff --check` 通过。共享守卫已归还，无遗留 helper 或检查进程。

本地证据不代替 macOS runner 的远端 CI 复验，也不增加任何真实 AX/TCC/桌面验收声明。
