# 开发验证与资源保护

Linux 本地检查统一经过 `scripts/check_local.py`，使用已有 systemd 用户管理器和 cgroup v2，不安装运行库、不修改系统服务、不会配置开机自启。执行前在服务内部读取自身与共享 slice 的内核控制文件，再启动检查；环境不具备控制能力或任一限制缺失时拒绝执行。

| 范围 | 内存 | Swap | CPU | 进程与线程 | 时间 |
| --- | --- | --- | --- | --- | --- |
| 全部本地 agent/worktree 的检查合计 | 2 GiB | 0 | 一个逻辑 CPU 的等效时间预算 | 128 | — |
| 一次检查及其所有子进程 | 1 GiB | 0 | 受上述共享预算约束 | 128 | 运行预算 180 秒，终止宽限 2 秒 |

Node 额外限制 V8 堆为 256 MiB。前端预设按文件单并发，测试超时 15 秒；该超时不能替代进程组期限。禁止 core dump，避免失败生成大文件。普通测试结束、取消、超时和局部 OOM 均由独立 service 回收其子进程，不停止其它检查或 Codex。共享 slice 只有本次登录的运行时配置，不改变 `user.slice` 或其它应用的额度。

```sh
# 复用已经安装的依赖，不自动同步或下载
python scripts/check_local.py backend .venv/bin/python
python scripts/check_local.py frontend tests/frontend/api.test.mjs
python scripts/check_local.py run -- .venv/bin/python -m pytest -q tests/test_local_check.py
python scripts/check_local.py --seconds 30 run -- .venv/bin/ruff check .
```

其它 worktree 在其自身目录调用主仓库脚本的绝对路径；共享名称固定为 `muli-checks.slice`，每次 service 使用不同名称。脚本输出 service 名称、退出结果、运行时间、内存峰值与 swap 使用，测试失败不会变成成功。只有全部启动前检查通过后才会出现 `Kernel resource limits verified`。

`backend` 必须显式指定已有 Python 解释器，不能自动下载或创建环境。核验资源限制后，按当前仓库约定递归发现 `tests/` 下的 `test_*.py` 与 `*_test.py`，固定文件清单；每批最多 32 个文件，使用新 pytest 进程串行运行，避免一个 Python 进程长期保留全套测试对象。也可在解释器后指定测试文件；目录、pytest 选项和不存在的路径拒绝，不悄悄改跑全量。需要 `-k` 等筛选时仍使用 `run -- 解释器 -m pytest ...`。

所有批次在**同一个** service/cgroup 与总运行期限内，后续批次不重置 180 秒预算，不提高内存或 CPU 限额。第一批失败、进程被信号终止或资源守卫拒绝后停止，不重试或继续其它批；取消仍回收整个 service。新进程释放自身堆，并不承诺所有文件缓存立即归零或任意规模测试一定能在预算内通过。前端保留原验证方式，远端 CI 的分片见下文。

此入口以当时全部 125 个后端文件实际运行四批，3,034 项通过、1 项平台跳过，整个 service 55.278 秒、峰值 846.8 MiB、swap 0；这是单个 cgroup 的整组峰值，不与四个独立 service 的分批峰值混称。随后将目录发现移入资源核验之后，26 项守卫检查与 48 项已有库检查经真实 backend 入口同组通过（峰值 86.7 MiB），Ruff/格式及脚本严格 mypy 通过。小型临时 pytest 替身还验证了真实新 PID、参数顺序和第二批失败后不启动第三批；无需递归运行真实 pytest 或创建额外服务。

默认离线设置 `UV_OFFLINE=1`、`UV_NO_SYNC=1`，明确传入 PATH、当前源码路径、Node 堆及数值库单线程环境。其它临时环境不自动继承，确需变量可在受限命令中使用 `env NAME=value`，不得覆盖保护变量。它是开发执行规范，不是恶意代码沙箱，也不阻止用户主动绕开脚本；所有协作 agent 必须遵守仓库 AGENTS.md。

显式设置过的 `UV_CACHE_DIR` 会保留，用于复用已有包和构建缓存；`UV_PYTHON_DOWNLOADS=never` 禁止自动下载解释器。构建依赖不在现有缓存时明确失败，不改为在线下载。首次隔离打包发现遗漏该缓存目录后已补上白名单与回归，随后完全离线构建和安装验收通过。

优先小范围验证，串行执行较重检查；不要在失败后提高限额或循环重跑。先读取有限错误日志、审查代码和 fixture。比较 DOM 或循环引用对象的身份时断言 `actual === expected` 的布尔结果，避免诊断格式化遍历完整对象图。

macOS、Windows 和没有 systemd 的 Linux 可继续运行 MuLi 产品；此本地开发入口不声称具备这些平台的等价隔离。托管 CI 在独立运行器执行既有跨平台矩阵，保留 job 硬超时，前端单并发与 Node 堆限制；`faulthandler_timeout` 仅用于诊断，不是进程终止保证。

Linux CI 的每种 Python 版本使用两份不重叠的测试集合，macOS/Windows 继续各跑全套。
显式 `--muli-ci-shard=1` / `2` 只在 pytest 完成正常收集后，按排序后的文件路径
交错分组；同文件全部参数化用例和 fixture 生命周期保持在同一作业，不另写一套
文件发现规则。非法参数和空片失败，没有该选项时行为不变。两片合起来覆盖同一
收集集合，文件均分不保证耗时均分，所有平台输出 `--durations=20` 供后续定位。
每项仍为十分钟上限，矩阵最多六项并行；恢复证据名称含分片编号，避免覆盖。
当前 CI 共十项（八个测试作业、质量、MCP），发行复用它后共十三项；本地守卫与
后端分批入口不改变。微型真实 pytest 回归验证无参数全量、参数化并集/交集、
同文件归属、非法参数与空片，不运行本地完整测试集。

分片依据为 [pytest collection hook](https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_collection_modifyitems)
及 [GitHub matrix](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations)；
接口先经 Context7 检索，再核对已锁定 pytest 8.4.2 源码。

实现依据：[systemd 资源控制](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)、[临时服务](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)、[服务期限与 OOM 策略](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、[Node 测试运行器](https://nodejs.org/api/test.html)。
