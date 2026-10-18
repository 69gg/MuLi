<div align="center">

# 沐璃 · MuLi

本地数字生命，与你一起把事情做好。

[![CI](https://github.com/69gg/MuLi/actions/workflows/ci.yml/badge.svg)](https://github.com/69gg/MuLi/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-blue)](LICENSE)

</div>

MuLi 是面向个人电脑的常驻助手：聊天、管理文件、搜索资料、记忆偏好、接收任务，以及让多个 agent 分工和复核。主要适配 Arch Linux 和其他 Linux；网页与核心服务支持 macOS / Windows，桌面操作和用户服务的能力按平台显示。

角色形象是十八岁的少女，定位为数字生命与本地助手，不设定恋爱关系。青瓷蓝工作台将对话放在中心，执行轨迹、审批和设置按需展开。

![青瓷蓝工作台](docs/screenshots/workbench-desktop.jpg)

## 能力

| 领域 | 当前实现 |
| --- | --- |
| 模型 | OpenAI Chat Completions、Responses、Anthropic 原生协议；流式/非流式、推理参数、工具回放、重试预算 |
| 协作 | 有依赖的多 agent 工作流，独立会话、角色模型、并行限制、失败传播、取消和复核模板 |
| 权限 | ask 每个工具调用询问；auto 独立审查 agent，风险操作交给用户；yolo 自动执行 |
| 文件 | 目录、读取、写入、精确编辑、搜索、复制、移动、可恢复回收站 |
| 搜索 | SearXNG / Brave / Tavily 可配服务，有限大小网页读取与来源链接 |
| 记忆 | 全历史倒排索引、两跳关系检索、来源与置信度、修订历史和级联忘记 |
| 电脑 | Linux AT-SPI 无障碍树、节点动作与文本编辑；新鲜快照与节点身份校验 |
| 主动事件 | chiliarch 原生配对与 SSE 任务；持久提醒、可选 Linux 桌面通知，无 AI 轮询 |
| 扩展 | 可选 MCP stdio / HTTP / SSE 工具，共用权限与输出预算 |
| 长任务 | 工具结果节选、完整原生协议回放、有限排队；原始会话记录保留 |
| 管理 | 模型、权限、工具、目录、记忆、工作流、通知、服务状态与日志统一入口 |

## 启动

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。前端无需安装 Node 依赖或构建。默认运行不依赖 Docker、向量数据库或本地模型。

```bash
git clone https://github.com/69gg/MuLi.git
cd MuLi
uv sync --locked
uv run muli init
uv run muli serve
```

打开 **http://127.0.0.1:8765**，在设置中填写模型协议、基础地址和模型名称。主模型处理会话，审查模型使用独立的无工具上下文，工作模型未配置时复用主模型。API key 可从环境变量读取。

默认配置为 `$XDG_CONFIG_HOME/muli/config.json`（通常 `~/.config/muli/config.json`），数据为 `~/.local/share/muli/`。`MULI_CONFIG` / `MULI_DATA_DIR` 或 CLI `--config` / `--data-dir` 可覆盖位置。密钥不出现在配置读取 API；配置文件和数据库使用私人文件权限。

```bash
uv run muli --config /path/config.json --data-dir /path/data serve --port 8766
uv run muli doctor
```

模型连接尚未配置时，界面可用于管理，不会伪装产生模型回答。独立审查未配置或失败时，auto 转为人工审批。

## Linux 常驻服务

```bash
uv run muli service print
uv run muli service install
uv run muli service enable
uv run muli service start
uv run muli service status
journalctl --user -u muli.service -f
```

也可在工作台的系统设置中安装、启动、停止、重启和启用登录启动。服务属于当前用户，不需要 root；内存、CPU 和进程数有 systemd 上限。配置修改后重启服务才能应用监听端口变化。

用户服务通常随用户会话启动。需要退出登录后仍保持运行时，可自行按系统策略启用 user lingering；MuLi 不更改系统登录策略。macOS / Windows 可直接运行 `muli serve`，不伪装提供 systemd 或 AT-SPI。

## 权限与资源

所有模型发起的工具调用经过同一个权限门，子 agent 继承模式，不得自行提升。工作目录列表是文件工具的访问范围；yolo 也不会取消配置的预算和工具禁用状态。shell 与桌面操作具有当前用户能力，auto 会交给用户确认；路径范围不是操作系统沙箱。

文件操作默认限于用户主目录，可在设置里修改。文件读取/写入、网页下载、命令输出、工作流节点与并行数均有上限。删除使用回收站，跨文件系统回收拒绝隐式复制大目录。重启后未完成任务标为中断，不盲目重复可能已有副作用的操作。

## 进一步阅读

- [架构与执行边界](docs/architecture.md)
- [模型协议与参数](docs/providers.md)
- [多 agent 工作流](docs/workflows.md)
- [上下文与长任务](docs/context.md)
- [个人记忆设计](docs/memory.md)
- [MCP 扩展](docs/mcp.md)
- [桌面通知](docs/notifications.md)
- [chiliarch 接入](docs/chiliarch.md)
- [工作台交互](docs/frontend.md)
- [配置、迁移与运维](docs/operations.md)
- [迭代记录](docs/iterations.md)
- [版本变更](CHANGELOG.md)

## 开发与验证

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
node --test tests/frontend/*.test.mjs
uv build
uv run python scripts/check_wheel.py
```

测试使用临时目录、模拟 HTTP/SSE 与假模型，不使用真实模型额度、不点击真实桌面。CI 在 Linux、macOS、Windows 与 Python 3.12/3.13 上验证核心；操作系统特有测试按能力跳过。

协议测试通过不代表特定供应商已联调；AT-SPI 能否读取某个应用取决于桌面会话和应用的无障碍支持。参见各模块文档中的已验证边界。

旧版源代码保存在 Git 历史基线 `6e22bf3`。旧 `config.json`、历史数据及本地环境不会被启动程序自动覆盖。
