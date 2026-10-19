# 2.5 原生电脑后端审查

状态：2.5.0 已发布。Windows 测试标识修复后的精确提交 `a5aca76a0c3a8a7273404cae34df5a6e366c42d5` 通过八项跨平台 CI；发布流水线十一项全部成功。真实 macOS 桌面验收仍未完成。

本轮为 macOS AX 增加按需原生 helper，持有真实 AXUIElementRef；Linux 继续单次 AT-SPI，不引入新的第三方依赖。进程协议、CF/AX 原生接口、运行器接线分别实现，再由独立 reviewer 审查资源回收、身份与错误语义。

设计前核对 Python 官方 asyncio/ctypes 文档和 Apple C API。原语反证验证了启动尚未返回时的取消窗口、持续输出总量与管道背压、高位指针 ABI、CF borrowed/Create/Copy 所有权、有界 UTF-16/数组及未知动作结果。它们只用于决定实现，不能代替正式产品测试。

合同评审把实际返回子项数量与本次请求量比较，避免正常预算裁剪被错误记作 provider 故障；宿主的第二层输出裁剪只让未公开引用不可寻址，保留量仍受预算约束，无需额外 ACK 协议。配置代用 Computer 对象隔离，执行归属用实际 session/run，避免重复引入配置代状态。

接线审查发现原生回收期间的再次取消可能跳过运行器终态落盘。修复将准确执行的回收作为强引用任务完成后，再落盘已有结果、未知工具配对、会话状态及映射清理；回收失败记录 `cleanup_error`，不覆盖原动作结果。新输入和任务暂停在 mutation IPC write 前再次同步复核，之后的取消不承诺回滚应用已经接受的动作。

第一轮宿主/进程集成已通过 1117 项 Python 测试（单进程 19.52 秒）和 93 项前端测试；全仓 Ruff、126 个 Python 文件格式检查、67 个源文件严格 mypy 通过。此时原生 helper 尚未合入，数字不是最终发行验收。

独立 reviewer 另运行 14 项关键反证，覆盖启动晚句柄、重复取消、背压、并发关闭、明确动作回执与回收失败、真实 Runtime 终态与 SQLite、owner 清理失败仍等待 probe；这些与正式回归部分重叠，不相加作为测试总数。

实际 JSONL 子进程与宿主/Runtime 的三条组合反证已纳入正式测试：应用→窗口→快照→单次动作、发出动作后丢失回执的 unknown，以及原生发送前最后门禁。另四条独立宿主门控也已正式化，验证探测启动未返回时的全局关闭、TTL 回收未完成时的单 helper 占位、迟到观察不能发布 ID 和外部执行不能消费别人的快照；每条失败路径均回收临时进程。

原生层后审查复现了保护子树展开问题：已读取 secure Subrole 后，PID 元数据失败让完整记录未返回，遍历错误分支却继续读取其 children。现在保护分类独立于完整记录保留；独立 reviewer 的原始反例与正式回归均通过，保护子树不会展开，普通元数据失败仍可继续有界观察。没有在真实桌面读取秘密内容。

最终本地验收：1223 项 Python 测试通过、1 项 macOS 系统 CF/ABI 冒烟在 Linux 跳过；93 项前端测试通过，全仓 Ruff、132 个 Python 文件格式与 68 个源码文件严格 mypy 通过。原生 helper 的真实 serve 循环通过系统 CF 函数替身接入实际 JSONL 子进程、lease 和宿主，覆盖窗口、快照、单次动作、保护子树、未知回执、权限撤销、两层 Unicode 预算与显式非保护文本；这不等同于真实桌面验收。

独立复审已放行，没有剩余确定阻塞项。最终 wheel/sdist 在离线模式构建并完成临时安装，验证 CLI 版本、资源、接口文档、提示词、AX 模块、电脑默认配置、任务控制和服务生命周期。待验证：远端跨平台矩阵及 PyPI/GitHub 发行物。取消仍会传递给运行器，未返回工具结果的尾部保守落盘为 unknown；本轮不保证取消期间保存每一份刚收到的原生回执，也不自动重试。

未验证：真实 macOS TCC 的终端/LaunchAgent/解释器归属、真实窗口及原生动作、Intel/Apple Silicon 外部应用对象的行为、任意应用保护声明的可靠性。不会在开发机采集屏幕、点击桌面、安装 SDK 或下载模型来替代受控验收。

## 首轮跨平台 CI 与 Windows 修复

[PR #8 的 CI 39315129745](https://github.com/69gg/MuLi/actions/runs/39315129745) 对应精确提交 `af106d8e26cf2b1adc08252e1f6a080b52f4efe7`。quality、mcp、Ubuntu 3.12/3.13 和 macOS 3.12/3.13 六项成功。Windows 3.12/3.13 都完成但失败；分别为 1189 passed、34 skipped、2 errors，耗时 215.63 秒和 372.82 秒，不能记为八项全绿。

两个错误都来自同一超长帧用例的 setup/teardown：pytest 将 65536 个 `x` 加换行的参数自动展开进 node ID，随后写入 `PYTEST_CURRENT_TEST`，超过 Windows 的 32767 字符环境变量上限。日志报 `ValueError: the environment variable is longer than 32767 characters`；该用例尚未进入 helper 断言，不是 AX 调用超时。

修复仅为这个参数设置 `oversized-frame` 显式标识，实际 65537 字节输入和原先协议拒绝断言全部保留。收集级反证在修复前测得最长 `PYTEST_CURRENT_TEST` 为 65636 字符，修复后为 131 字符，仍完整收集全部 11 个输入。正式 native helper、真实协议桥接及假 CF/native 桥接共 90 项通过、1 项既有 macOS 系统 CF 冒烟在 Linux 跳过（0.95 秒）；Ruff、格式与 diff 检查通过。没有增大超时、新增跳过或改变产品代码。Windows 的最终结果仍须以修复提交的新 CI 为准。

## 发行核验

[修复后 CI](https://github.com/69gg/MuLi/actions/runs/39316794183) 为 8/8 成功；[发布流程](https://github.com/69gg/MuLi/actions/runs/39317554749) 为 11/11 成功。签名标签 `v2.5.0` 指向上述精确提交。2026-10-20 核对 [PyPI](https://pypi.org/project/muli-agent/2.5.0/) 与 [GitHub Release](https://github.com/69gg/MuLi/releases/tag/v2.5.0) 的文件名、大小和 SHA-256，全部一致；仅读取元数据，未下载包。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| wheel | 454593 | `28d6eed383afce5f8d1c8dac9360d6c6f4380a70dd06c0428bbc3d9aaa5a07bd` |
| sdist | 2526009 | `94eae11cf5eddfe5f727dbcf0bda70d04aac8ea3ed597923ae3fe5e0f80a592a` |

macOS CI 的系统 CF/ABI 冒烟通过，不等同于 TCC、窗口和实际动作验收；Windows 测试不再因超长用例标识中断。前文“待验证”记录是当时阶段状态，最终发行结果以本节为准。
