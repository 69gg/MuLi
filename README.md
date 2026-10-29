<div align="center">

# 沐璃 · MuLi

本地数字生命，与你一起把事情做好。

[![CI](https://github.com/69gg/MuLi/actions/workflows/ci.yml/badge.svg)](https://github.com/69gg/MuLi/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-blue)](LICENSE)

</div>

MuLi 是面向个人电脑的常驻助手。你可以和她聊天、交办任务、管理文件，也可以从不同消息渠道继续同一段协作。沐璃以十八岁少女为形象，定位为数字生命与本地助手。

<picture>
  <source media="(prefers-reduced-motion: reduce)" srcset="https://raw.githubusercontent.com/69gg/MuLi/22433e03284bd8715972a3b71adeb21b1cf239c7/docs/screenshots/workbench-desktop.jpg">
  <img src="https://raw.githubusercontent.com/69gg/MuLi/22433e03284bd8715972a3b71adeb21b1cf239c7/docs/screenshots/workbench-demo.gif" alt="沐璃新版工作台演示：输入任务、工具调用、人工批准和流式答复" width="960">
</picture>

- **一起做事**：搜索资料、管理文件、阅读与收藏 PDF、DOCX 正文，[留存公开网页静态文本](docs/web-reading.md)并跨渠道续读，让多个 agent 分工、交流和复核。
- **记住重要的事**：后台整理偏好、事实与关系，可明确收藏已有完整语音转写或已捕获网页快照，日后按内容找回并核对出处。
- **随时联系**：网页之外，可选接入 Telegram、OneBot/NapCat、QQ 官方机器人、微信 iLink 和 chiliarch。
- **使用电脑**：按平台提供无障碍操作、截图与录屏，可收发文件、图片和语音。
- **按你的习惯扩展**：配置模型、MCP、Agent Skills 和用户插件，通过自然语言让沐璃协助设置。

## 开始使用

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。无需构建前端，默认安装不附带本地大模型。

```bash
uv tool install muli-agent
MuLi init
```

初始化会询问称呼、主模型、视觉能力、监听地址和密码，并尝试安装和启动当前用户的后台服务。默认网址为 **http://127.0.0.1:8765**；修改地址或端口后，以终端显示为准。命令也可以写作 `muli`。

首次初始化希望手动启动时，使用：

```bash
MuLi init --no-service
MuLi serve
```

已有配置时，直接运行 `MuLi serve`。初始化默认尝试下载只读源码参考，不需要时可加 `--no-source`。

当前源码使用 `memory=4`，可为资料关注保存登记轮的真实来源偏好；回访仍在 Web，外发须 AI 显式调用渠道工具并经过原审批。已有旧格式资料库须先完成[停机数据升级](docs/channel-ownership-format-upgrade.md)。非托管 Linux 支持指南列出的六种精确旧格式，macOS 仅支持完整 `memory=2/3` 前序合同；Windows、其它 macOS 旧格式和托管安装的旧库继续使用原版本，新装不受影响。

配置、聊天、附件、记忆、插件和日志默认保存在 **`~/MuLi`**。已有配置不会被初始化覆盖；需要自定义位置时，见[配置与维护](docs/operations.md)。

## 模型与权限

支持 OpenAI Chat Completions、Responses 和 Anthropic Messages。工作、审查、压缩和记忆提炼模型未单独配置时复用主模型；不能读图的主模型可搭配独立视觉模型。

最大输出 Token 默认不填写；使用 Anthropic Messages 时必填。Embedding 与 reranker 是可选的独立服务，用于记忆检索，在设置的“媒体与检索”中配置。详见[模型设置](docs/providers.md)。

| 权限模式 | 行为 |
| --- | --- |
| ask | 每次工具调用都询问你 |
| auto | 由独立 agent 审查，有风险时交给你决定 |
| yolo | 自动执行已启用的工具，仍遵守配置的资源限制 |

文件访问目录、工具开关和资源额度都可在设置中调整。Shell 和电脑操作使用当前系统用户的权限，文件目录设置不能代替系统隔离。

## 后台运行

优先适配 Arch Linux 和其他 Linux。Linux 使用 systemd 用户服务，macOS 使用 LaunchAgent，Windows 使用当前用户计划任务；通常随用户登录启动。桌面操作的可用能力因系统和应用而异，见[平台支持](docs/platforms.md)。

```bash
MuLi service status
MuLi service restart
MuLi doctor
```

也可在工作台设置中管理服务和查看日志。QQ 官方机器人与微信 iLink 需要可选依赖：

```bash
uv tool install 'muli-agent[channels]'
```

同时使用 MCP 时，可安装 `muli-agent[mcp,channels]`。安装后按[渠道说明](docs/channels.md)完成接入。

## 使用指南

- [聊天、审批与工作台](docs/frontend.md)
- [模型与连接参数](docs/providers.md)
- [记忆、检索与遗忘](docs/memory.md)
- [多 agent 协作](docs/workflows.md)
- [消息渠道与文件收发](docs/channels.md)
- [语音与唤醒](docs/voice.md)
- [提醒与主动事项](docs/attention.md)
- [MCP、Skills 与插件](docs/README.md#扩展与维护)
- [全部使用指南](docs/README.md)

[版本变更](CHANGELOG.md) · [开发与验证](docs/development.md) · [许可证](LICENSE)
