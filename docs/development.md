# 开发与验证

开发前阅读仓库的 `AGENTS.md`。Python 使用类型注释；先了解现有实现，再修改代码，并同步相关使用文档与必要回归。

## 准备环境

```bash
git clone https://github.com/69gg/MuLi.git
cd MuLi
uv sync --locked
uv run MuLi init --no-source --no-service
uv run MuLi serve
```

默认前端不需要 Node 构建；运行前端测试时需要已有 Node。不要为了验证自动下载本地模型、安装大型运行库，或操作真实用户数据和桌面。

## 本机检查入口

Linux 本地测试、检查、打包和实验统一经过 `scripts/check_local.py`。它要求 systemd 用户管理器与 cgroup v2，核验内核实际限制后才执行；无法确认限制时停止，不退回无保护运行。

| 范围 | 内存 | Swap | CPU | 进程与线程 | 期限 |
| --- | --- | --- | --- | --- | --- |
| 全部 agent 与 worktree 合计 | 1 GiB | 0 | 半个逻辑 CPU 的等效时间预算 | 96 | — |
| 单次检查及其子进程 | 768 MiB | 0 | 受共享预算限制 | 96 | 最长180秒 |

Node 堆另限192 MiB，前端按文件单并发。先运行相关小组，本地检查串行执行；超出限额时停止并缩小验证范围，不提高上限。取消、超时与局部内存超额会收口该检查的任务组；此入口是开发资源保护，不是恶意代码沙箱，也不提供跨平台等价隔离。

常用命令：

```bash
python scripts/check_local.py backend .venv/bin/python tests/test_runtime.py
python scripts/check_local.py frontend tests/frontend/api.test.mjs
python scripts/check_local.py run -- .venv/bin/ruff check .
python scripts/check_local.py run -- .venv/bin/ruff format --check .
python scripts/check_local.py run -- .venv/bin/mypy
python scripts/check_local.py run -- uv build --offline
python scripts/check_local.py run -- .venv/bin/python scripts/check_wheel.py
```

其它 worktree 从自己的工作目录调用主仓库脚本的绝对路径，并指定已有解释器。不得绕过守卫、提高限额求通过，或把工具等待时间当成硬超时。命令返回仍在运行时，收取最终退出结果后再启动下一项检查。

守卫统一将 `TMPDIR` 设置为脚本所属仓库的 `.local-check-artifacts/tmp`，覆盖继承的临时目录设置。跨 worktree 调用同一主仓库脚本时，共用该磁盘目录，执行工作目录仍是当前 worktree。入口创建的 `.local-check-artifacts` 和 `tmp` 必须是当前用户拥有的普通目录，权限为 `0700`；符号链接、其它所有者或非私有权限都会拒绝执行，不自动修正已有目录。

入口通过有界的 `findmnt -T 目录 -n -o FSTYPE` 核验临时目录所在文件系统，拒绝 `tmpfs`、`ramfs`、`devtmpfs`，也拒绝空白、多行、命令失败或工具不可用的结果，不退回 `/tmp`。临时数据保存在磁盘上，避免 RAM 文件系统残留持续占用共享 cgroup 内存预算；入口不自动删除工件，已有临时记录和冻结工件继续保留，迁移的旧路径也可保留为符号链接供追溯。

### 定向与全量验证

先运行相关小范围；通过后再按改动范围做整合检查。需要 pytest 的 `-k` 等参数时，通过 `run --` 调用已有解释器。

`backend` 后必须提供解释器，可再列出测试文件；不接受目录或 pytest 选项。不指定文件时发现全套后端测试，在同一个守卫内按最多32个文件串行分批；后续批次不会重置总期限，首批失败即停止。

默认设置 `UV_OFFLINE=1`、`UV_NO_SYNC=1`、`UV_PYTHON_DOWNLOADS=never`，复用现有环境和缓存，不自动安装依赖。需要额外变量时，在受限命令中显式使用 `env NAME=value`，不得覆盖保护变量。离线构建缺少依赖时应明确失败。

失败后先看有限日志和相关代码，不循环触发同一个失控用例。比较 DOM 或含循环引用对象的身份时，断言布尔结果，避免失败报告递归展开整个对象图。

### YAML 依赖的类型检查

PyYAML 当前没有类型声明；`pyproject.toml` 仅为 `yaml.*` 设置库专用 `ignore_missing_imports`，统一覆盖根模块及子模块，不在每个导入上重复行内 ignore。YAML 导入值仍按 `Any` 处理；业务代码的严格检查与 unused-ignore 检查继续生效。

`tests/test_typecheck_yaml.py` 使用实际项目配置和 YAML 导入，分别检查冷缓存、依赖类型变化后的暖缓存，并验证业务返回类型错误与多余 ignore 仍会报错。测试使用独立临时缓存，固定三次子进程检查，每次最多20秒，不安装依赖或重试。

## 托管 CI

队友命令续办的恢复回归需要一份独立的旧版源码。CI 通过 [checkout](https://github.com/actions/checkout) 在忽略目录内检出固定的 2.123.0 提交，并用 `MULI_PREVIOUS_SOURCE_ROOT` 指向它；恢复子进程会核对实际导入位置，分别验证旧版和候选版，不使用候选实现替代旧消费者。

本机运行 `tests/test_shell_continue_recovery.py` 时，在受限命令中设置 `env MULI_PREVIOUS_SOURCE_ROOT=/path/to/previous-checkout`，指向已有的固定旧版源码根目录。该夹具仅使用临时资料库，缺少旧源码时明确失败，不自动下载或跳过。其首批完整恢复旅程仅适用于 POSIX；Windows 仍运行独立的 Shell 观察者与既有恢复回归。

CI 共19项作业，包含16项后端测试分片、质量、MCP 与 Linux 原生作业，验证 Linux、macOS、Windows 与 Python 3.12/3.13。Linux 和 macOS 每个版本按测试文件分成两份不重叠集合，Windows 分成四份；全部分片合起来覆盖完整测试集合，同文件的参数化用例与夹具生命周期保持在一起。分组使用仓库相对文件名的稳定摘要，同一分片总数下新增文件不会重排已有文件；pytest 自身的发现与忽略规则保持生效，分片不保证耗时均分。正式发布复用这19项验证，并另运行准备、PyPI 发布与 GitHub Release，共22项作业。

测试矩阵最多并行6项，每项期限10分钟。四片减少平均执行文件量，完整收集开销仍保留；候选与正式发布须收齐相应全部作业的成功终态。

冻结的旧版兼容性夹具按原始字节核验；`.gitattributes` 对这些文件关闭文本转换，避免 Windows 的自动换行转换改变历史证据。新增此类夹具时，同步声明其检出规则，保留原始摘要断言。

分片只在指定 `--muli-ci-shard` 时启用，编号从1开始；`--muli-ci-shards` 默认2，可选2、3或4。编号超出总数或所选组没有用例时明确报错，不把覆盖缺口当作通过。

质量作业检查格式、类型、前端与发行包；可选集成作业另安装 MCP 和渠道依赖，覆盖默认环境可能跳过的 SDK 路径。每项保留 workflow 硬超时、有限并发和 Node 堆限制；诊断用的 `faulthandler_timeout` 不能代替终止期限。

Windows 托管矩阵通过 [MSYS2 官方 action](https://github.com/msys2/setup-msys2) 复用 runner 已有环境，安装 [UCRT64 Poppler](https://packages.msys2.org/packages/mingw-w64-ucrt-x86_64-poppler)，把其程序目录放到测试 PATH 前面并记录版本。这样真实 PDF 用例使用支持标准输入的转换器，避免命中 Git for Windows 附带的同名 Xpdf 程序。下载与安装仅发生在托管 runner；本机离线检查不安装转换器，也不改变真实文档用例的断言或可选依赖缺失行为。

Windows 原生无障碍测试仅在托管 CI 创建并操作自己的临时窗口，本机开发不读取或点击真实桌面。专用诊断工作流与完整跨平台 CI 的覆盖范围不同，不能相互代替。

测试应使用临时目录、假模型和受控 HTTP/SSE。协议回归通过不等于供应商、真实账号、桌面硬件或自然模型效果已经联调；在交付说明中准确标明未验证的部分。

候选下载期限回归分别核查请求前已过期、客户端构造耗尽期限和已拥有响应后的流内超时。流内用例仅控制模块局部时钟，保留真实的 `asyncio` 计时和原 0.01 秒预算，以实际进入、取消和关闭 body 的事实验证收口，避免把调度延迟误判为关闭失败。

专项测试如果用很小的上下文验证并发配额、压缩交接或完整协议回执，应显式配置精简的人格提示，保留原来的上下文和输出上限。这样默认人格增加正常使用指引时，不会提前耗尽专项夹具的预算；默认人格与自动记忆是否能共同工作，另用保留完整默认提示的真实请求旅程验证。

需要逐字核对的 UTF-8 文本夹具应通过 `write_bytes(text.encode("utf-8"))` 写入并用 `read_bytes()` 核对落盘字节，避免平台默认换行转换影响完整回执比较。

Portal 回归使用独立的临时 D-Bus 和合成图片。夹具按可用能力选择 Gio 的新对象注册入口，并兼容旧版；保留严格 stderr 检查，不通过过滤警告掩盖接口变化或真实错误。[Gio 注册接口](https://docs.gtk.org/gio/method.DBusConnection.register_object_with_closures2.html)

<a id="队友进度等待的阶段诊断"></a>

### 定位等待问题

队友完成事件、资源释放和最终状态保存可能处于不同阶段。诊断时用有界观察分别确认它们，不要仅凭一次等待超时就判断死锁，也不要靠放宽期限、跳过断言或重复运行掩盖问题。

## 提交与发布

保持提交范围清楚，避免混入本地配置、密钥、运行数据和生成草稿。使用仍有效的 GPG 密钥签名；密钥失效时先刷新，不默默降级为无签名提交。

使用文档描述最终功能和操作方法；版本变化写入 [CHANGELOG](../CHANGELOG.md)。调试流水、测试数字和临时审查过程放在开发记录或 PR 中，不追加到用户指南。

开发历史材料与技术合同见[开发资料归档](../devarchive/README.md)。

发布要求与 Trusted Publisher 配置见[发布说明](publishing.md)。

`pyproject.toml` 将 Hatchling 构建后端固定为精确版本，本地与 CI 使用同一版本，避免浮动后端改变 wheel 元数据及摘要。`scripts/release.py` 使用标准库核验实际 wheel 的唯一 `Generator` 与项目声明一致；缺失、重复、版本漂移或浮动依赖都会拒绝发布。升级后端时同时更新精确声明，并重新验证、构建和冻结产物。
