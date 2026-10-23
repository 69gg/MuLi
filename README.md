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
| 协作 | 持续主身份、动态 spawn/send/wait/resume/stop 团队、可跨重启的增量结果游标、依赖工作流；历史成员无固定上限，执行有资源预算 |
| 权限 | ask 每个工具调用询问；auto 独立审查 agent，风险操作交给用户；yolo 自动执行 |
| 文件 | 目录、读取、写入、精确编辑、搜索、复制、移动、可恢复回收站；附件元数据检索与有界分页 |
| 搜索 | SearXNG / Brave / Tavily 可配服务，有限大小网页读取与来源链接 |
| 记忆 | 分层原文与后台提炼、关键词/embedding/关系图混合检索、reranker、多来源别名与实际召回路径、相关资料与侧写共同注入、原文分页核查、冲突修订、来源级忘记 |
| 电脑 | Linux AT-SPI 应用/窗口定位与节点操作；实验性 macOS AX 前台应用及原生引用；Windows UIA 顶层窗口/选定树、普通按钮一次 Invoke（本片动作 Windows 验收待验）；可选有界截图、录屏与统一附件引用 |
| 渠道 | 可选 OneBot/NapCat、Telegram、QQ 官方机器人和微信 iLink；同一主身份、Team / Workflow 后台报告保留原渠道、失效目的地持久阻塞与本地文件收发 |
| 语音 | 独立默认关闭的网页录音转写草稿、本地中文声学唤醒与声纹桥接、录音参考、OpenAI 兼容 ASR/TTS、完整转写留存与续读；不附带本地模型 |
| 主动事件 | chiliarch 原生配对与 SSE 任务、默认关闭的普通消息资料提醒；持久提醒、维护结果与新版通知进入主会话，独立事项可按默认或显式记忆绑定通知，可选 Linux 桌面通知，无 AI 轮询 |
| 扩展 | Agent Skills 渐进读取与文档热发布、运行期间换代的可选 MCP 工具、按需文本资源与显式单 blob 附件保存、显式文本模板、用户目录 Python 插件；工具、提示词、模型、路由和生命周期可扩展 |
| 长任务 | 完整标题目录与已保存正文分页续读、后台分层压缩、协议回放、持久收件队列、完整任务说明、明确暂停与补充要求后的重新规划 |
| 管理 | 模型、权限、工具、目录、记忆、工作流、通知、服务状态与日志统一入口；`self_logs` 读取自身日志，`self_events` 分页回查保留错误，`self_failure` 按运行定位故障；活动工作流独立分页与逐次停止 |

## 启动

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。前端无需安装 Node 依赖或构建。默认运行不依赖 Docker、向量数据库或本地模型。

```bash
uv tool install muli-agent
MuLi init
```

发行名称是 `muli-agent`，命令为 `MuLi` 或 `muli`。`init` 会询问称呼、主模型完整参数、视觉能力、监听地址和密码，然后安装当前用户的后台服务、启用登录启动并请求启动。可用 `--no-service` 跳过；`--defaults` 只创建默认配置。服务不可用时保留配置并给出 `MuLi serve` 前台启动方式。工作台默认 **http://127.0.0.1:8765**，沐璃会逐步协助配置其它能力。独立审查使用无工具上下文；工作、压缩和记忆提炼模型未单独配置时复用主模型。API key 可从环境变量读取。

默认所有内容集中在 `~/MuLi`：`config.json`、`data/`、`plugins/`、`prompts/`、`logs/` 和可选只读源码参考 `source/`。`MULI_HOME` 设置根目录，`MULI_CONFIG` / `MULI_DATA_DIR` 或 CLI `--config` / `--data-dir` 可单独覆盖。初始化不覆盖已有配置；旧路径不会被自动搬走。密钥不出现在配置读取 API；配置文件和数据库使用私人文件权限。

```bash
MuLi --config /path/config.json --data-dir /path/data serve --port 8766
MuLi doctor
```

模型连接尚未配置时，界面可用于管理，不会伪装产生模型回答。独立审查未配置或失败时，auto 转为人工审批。

QQ 官方机器人和微信 iLink 使用可选加密依赖：`uv tool install 'muli-agent[channels]'`；同时需要 MCP 时使用 `uv tool install 'muli-agent[mcp,channels]'`。默认安装不包含本地语音或 GPU 模型；平台接入方式见[渠道说明](docs/channels.md)。

没有发令会话的主动通知默认留 WebUI，可在“消息渠道 → 主动通知”一次设置外部默认。启动自主更新检查、扩展目录自主 watch 的新失败状态、MCP 当前 HTTP/SSE 连接首次接收失败可使用该默认；主会话当前请求失败和新任务提醒保存原请求来源，旧记录及其它日志留本地。MCP 故障不自动重连或重放；默认更改只影响新事项，撤销后的待发项 blocked，unknown 不自动重试。见[默认通知的配置与边界](docs/channels.md#无发令会话时的默认通知)。

## 后台服务

```bash
MuLi service print
MuLi service install
MuLi service enable
MuLi service start
MuLi service status
journalctl --user -u muli.service -f
```

也可在工作台的系统设置中安装、启动、停止、重启和启用登录启动。服务属于当前用户，不需要 root；内存、CPU 和进程数有 systemd 上限。配置修改后重启服务才能应用监听端口变化。

Linux 本地候选托管切换提供 `self_adopt`、`self_switch` 和 `self_recover`，保留最新数据库与原汇报来源；使用条件、验收状态和边界见[托管切换说明](docs/managed-operations.md)。

已托管的 Linux 安装可配置本地 wheelhouse，通过 `self_update_preview` 核验候选
来源，再经真实用户批准执行 `self_update`。准备进程有资源预算与持久恢复记录，
中断后由 `self_recover` 收尾；策略默认关闭，不自动下载依赖。另可启用独立在线策略，
预览精确版本并批准完整构件事实后，在预算内获取一枚 MuLi wheel，再走同一离线准备；
依赖仍需本地 wheelhouse，缺失时保留旧安装。配置、使用顺序及
尚未实测的真实系统恢复边界见[候选更新说明](docs/candidate-host-design.md)。

Linux 使用 systemd 用户服务；macOS 使用 LaunchAgent；Windows 使用当前用户计划任务及 `pythonw.exe`，不弹出命令窗口。它们通常随用户登录启动。Linux 退出登录后仍需运行时，可按系统策略启用 user lingering；MuLi 不更改系统登录策略。桌面权限与实际平台验证范围见[平台能力](docs/platforms.md)。

## 权限与资源

所有模型发起的工具调用经过同一个权限门，子 agent 继承模式，不得自行提升。工作目录列表是文件工具的访问范围；yolo 也不会取消配置的预算和工具禁用状态。shell 与桌面操作具有当前用户能力，auto 会交给用户确认；路径范围不是操作系统沙箱。

文件操作默认限于用户主目录，可在设置里修改。文件读取/写入、网页下载、命令输出、工作流节点与并行数均有上限。删除使用回收站，跨文件系统回收拒绝隐式复制大目录。重启后未完成任务标为中断，不盲目重复可能已有副作用的操作。

## 进一步阅读

- [架构与执行边界](docs/architecture.md)
- [模型协议与参数](docs/providers.md)
- [多 agent 工作流](docs/workflows.md)
- [工作流结果回原渠道](docs/workflow-origin-design.md)
- [任务暂停、补充要求与重新规划](docs/task-control.md)
- [上下文与长任务](docs/context.md)
- [个人记忆设计](docs/memory.md)
- [MCP 扩展](docs/mcp.md)
- [Agent Skills](docs/skills.md)
- [用户插件与自身管理](docs/self-management.md)
- [Linux 同版本恢复入口](docs/linux-recovery-operations.md)
- [新资料库格式准入](docs/fresh-format-admission.md)
- [压缩后的证据续读](docs/history-evidence.md)
- [查找全部运行记录](docs/session-records.md)
- [查看已保存的正文](docs/history-archive.md)
- [PyPI 与发布流程](docs/publishing.md)
- [桌面通知](docs/notifications.md)
- [系统注意事项与主会话唤醒](docs/attention.md)
- [系统日志事件与通知级别](docs/system-events.md)
- [按运行查询故障位置](docs/failure-diagnostics.md)
- [chiliarch 接入](docs/chiliarch.md)
- [可选通讯渠道与本地文件收发](docs/channels.md)
- [微信语音文字与原始录音](docs/weixin-voice-ingress.md)
- [跨平台服务、截图和录屏](docs/platforms.md)
- [应用、窗口与无障碍操作](docs/computer.md)
- [中文唤醒与声纹桥接](docs/voice.md)
- [完整语音转写与续读](docs/voice-transcripts-design.md)
- [附件检索与分页](docs/attachment-search.md)
- [工作台交互](docs/frontend.md)
- [事件订阅与恢复位置](docs/event-subscription.md)
- [会话恢复与当前执行](docs/session-recovery.md)
- [开发验证与资源保护](docs/development.md)
- [配置、迁移与运维](docs/operations.md)
- [迭代记录](docs/iterations.md)
- [版本变更](CHANGELOG.md)

## 开发与验证

```bash
git clone https://github.com/69gg/MuLi.git
cd MuLi
uv sync --locked
uv run MuLi init --no-source --no-service
uv run MuLi serve
# Linux 本地验证：先核验 cgroup 限制，再执行；复用已安装依赖，默认离线
python scripts/check_local.py backend .venv/bin/python
python scripts/check_local.py run -- .venv/bin/ruff check .
python scripts/check_local.py run -- .venv/bin/ruff format --check .
python scripts/check_local.py run -- .venv/bin/mypy
python scripts/check_local.py frontend
python scripts/check_local.py run -- uv build --offline
python scripts/check_local.py run -- .venv/bin/python scripts/check_wheel.py
```

本地检查入口覆盖子进程，并在执行前核验内核实际限额：单次 1 GiB、所有 worktree 合计 2 GiB、swap 0、共享一个逻辑 CPU 的等效时间预算、128 个任务、每次最长 180 秒。前端文件单并发，Node 堆另限 256 MiB；超时或局部 OOM 回收整组任务。没有 systemd 用户服务或 cgroup v2 时拒绝执行，不降级为无保护运行。使用方式和边界见[开发验证](docs/development.md)。

测试使用临时目录、模拟 HTTP/SSE 与假模型，不使用真实模型额度、不点击真实桌面。托管 CI 在 Linux、macOS、Windows 与 Python 3.12/3.13 上验证核心；操作系统特有测试按能力跳过。开发入口的 Linux 要求不改变产品的跨平台支持。

协议测试通过不代表特定供应商已联调；AT-SPI 能否读取某个应用取决于桌面会话和应用的无障碍支持。参见各模块文档中的已验证边界。

旧版源代码保存在 Git 历史基线 `6e22bf3`。旧 `config.json`、历史数据及本地环境不会被启动程序自动覆盖。
