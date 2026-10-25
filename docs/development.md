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
| 全部 agent 与 worktree 合计 | 2 GiB | 0 | 一个逻辑 CPU 的等效时间预算 | 128 | — |
| 单次检查及其子进程 | 1 GiB | 0 | 受共享预算限制 | 128 | 最长180秒 |

Node 堆另限256 MiB，前端按文件单并发。取消、超时与局部内存超额会收口该检查的任务组；此入口是开发资源保护，不是恶意代码沙箱，也不提供跨平台等价隔离。

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

### 定向与全量验证

先运行相关小范围；通过后再按改动范围做整合检查。需要 pytest 的 `-k` 等参数时，通过 `run --` 调用已有解释器。

`backend` 后必须提供解释器，可再列出测试文件；不接受目录或 pytest 选项。不指定文件时发现全套后端测试，在同一个守卫内按最多32个文件串行分批；后续批次不会重置总期限，首批失败即停止。

默认设置 `UV_OFFLINE=1`、`UV_NO_SYNC=1`、`UV_PYTHON_DOWNLOADS=never`，复用现有环境和缓存，不自动安装依赖。需要额外变量时，在受限命令中显式使用 `env NAME=value`，不得覆盖保护变量。离线构建缺少依赖时应明确失败。

失败后先看有限日志和相关代码，不循环触发同一个失控用例。比较 DOM 或含循环引用对象的身份时，断言布尔结果，避免失败报告递归展开整个对象图。

## 托管 CI

CI 验证 Linux、macOS、Windows 与 Python 3.12/3.13。Linux 和 Windows 每个版本按测试文件分成两份不重叠集合，macOS 运行完整集合；同文件的参数化用例保持在一起。分片不保证耗时均分。

质量作业检查格式、类型、前端与发行包；可选集成作业另安装 MCP 和渠道依赖，覆盖默认环境可能跳过的 SDK 路径。每项保留 workflow 硬超时、有限并发和 Node 堆限制；诊断用的 `faulthandler_timeout` 不能代替终止期限。

Windows 原生无障碍测试仅在托管 CI 创建并操作自己的临时窗口，本机开发不读取或点击真实桌面。专用诊断工作流与完整跨平台 CI 的覆盖范围不同，不能相互代替。

测试应使用临时目录、假模型和受控 HTTP/SSE。协议回归通过不等于供应商、真实账号、桌面硬件或自然模型效果已经联调；在交付说明中准确标明未验证的部分。

<a id="队友进度等待的阶段诊断"></a>

### 定位等待问题

队友完成事件、资源释放和最终状态保存可能处于不同阶段。诊断时用有界观察分别确认它们，不要仅凭一次等待超时就判断死锁，也不要靠放宽期限、跳过断言或重复运行掩盖问题。

## 提交与发布

保持提交范围清楚，避免混入本地配置、密钥、运行数据和生成草稿。使用仍有效的 GPG 密钥签名；密钥失效时先刷新，不默默降级为无签名提交。

使用文档描述最终功能和操作方法；版本变化写入 [CHANGELOG](../CHANGELOG.md)。调试流水、测试数字和临时审查过程放在开发记录或 PR 中，不追加到用户指南。

发布要求与 Trusted Publisher 配置见[发布说明](publishing.md)。
