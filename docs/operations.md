# 配置、备份与维护

常用配置在工作台“设置”中管理，也可以请沐璃协助修改。`MuLi` 和 `muli` 是同一个命令。

## 启动与后台服务

首次运行使用 `MuLi init`。初始化会保存配置，默认尝试下载只读源码参考，再安装、启用并启动当前用户的后台服务。源码参考可用 `--no-source` 跳过；只想配置、不安装服务时使用 `--no-service`。已有配置不会被初始化覆盖。

已经配置好后，运行 `MuLi serve` 可在当前终端启动；不要与已运行的后台服务同时启动。访问终端显示的工作台地址，默认是 `http://127.0.0.1:8765`。前台运行时关闭该进程也会停止服务。

后台服务常用命令：

| 命令 | 用途 |
| --- | --- |
| `MuLi service status` | 查看是否安装、运行及最近日志 |
| `MuLi service install` | 安装服务，不自动启动 |
| `MuLi service enable` / `disable` | 开启或关闭登录后自动启动 |
| `MuLi service start` / `stop` | 启动或停止服务 |
| `MuLi service restart` | 重启服务 |
| `MuLi doctor` | 查看版本、配置位置、模型与本机能力 |

启动请求已提交不等于服务已经可用，仍需查看状态和实际访问结果。初始化的服务安装失败时，配置会保留，可以先用 `MuLi serve` 查看原因。

Linux 使用 systemd 用户服务，macOS 使用 LaunchAgent，Windows 使用当前用户计划任务，不创建管理员级系统服务。Windows 需要当前 Python 环境提供 `pythonw.exe`；缺少服务所需组件时会说明原因。详情见[平台支持](platforms.md)。

## 修改设置

模型连接、搜索、文件访问目录、工具开关和执行预算都可在设置中调整。模型基础地址需包含供应商要求的版本前缀，例如 `https://provider.example/v1`，不要填写完整的 `/chat/completions` 等接口地址。搜索服务需要自己配置；SearXNG 必须开启 JSON 输出。详见[模型设置](providers.md)。

保存时留空已有密钥通常表示保留原值；需要删除时使用明确的清除操作。界面只显示密钥是否已设置，不会读回明文。高级 JSON 配置不接受未知字段。

普通执行配置不能与正在运行的任务交错修改，先让任务结束或停止任务。MCP、Skills 等扩展的专用重载可在运行期间应用，具体规则见[MCP](mcp.md)和[Skills](skills.md)；用户插件启用状态需要重启。

插件目录或个别插件无法读取时，可继续使用主会话，并通过 `self_status` 查看失败清单。检查路径和权限后重启即可重新加载；禁用插件时不会创建或扫描插件目录。详见[插件目录](self-management.md#插件目录)。

[连续终端](shell-sessions.md)默认使用当前用户的系统 shell。服务重启后不会恢复旧终端的进程、目录或环境，也不会重放命令。

修改监听地址或端口需要按工作台提示完成配置重启。排队成功不表示新地址已经生效；重启完成后，使用新地址重新连接。

默认只监听本机。监听非回环地址必须设置访问密码；远程使用还需自行提供合适的 HTTPS 入口。网页麦克风在远程 HTTP 页面上通常不可用。不要在进程仍对外监听时通过清空密码来关闭认证。

## 数据放在哪里

默认主目录是 `~/MuLi`，其中 `~` 表示当前用户的主目录。

| 默认位置 | 内容 |
| --- | --- |
| `~/MuLi/config.json` | 配置，可能包含模型和渠道密钥 |
| `~/MuLi/plugins/`、`~/MuLi/prompts/` | 用户插件与自定义提示词 |
| `~/MuLi/source/` | 初始化可选下载的只读源码参考，不是当前安装程序 |
| `~/MuLi/data/muli.sqlite3` | 聊天、运行、任务、记忆、审批及投递记录 |
| `~/MuLi/data/attachments/` | 上传和保存的附件 |
| `~/MuLi/data/trash/` | 文件工具的可恢复回收站 |
| `~/MuLi/data/captures/`、`~/MuLi/data/voice/` | 启用后产生的截图、录屏与本地语音文件 |
| `~/MuLi/data/logs/service.log` | macOS、Windows 的服务日志；Linux 主要查看用户服务日志 |

模型使用环境变量读取的密钥不在配置文件中，恢复时还需重新提供对应环境变量。Shell 和电脑工具具有当前系统用户的能力，工作目录设置不是操作系统沙箱。

### 自定义位置

可使用 `MULI_HOME` 修改默认主目录；`MULI_CONFIG` 和 `MULI_DATA_DIR` 分别指定配置文件与数据目录。也可使用全局命令行参数，放在子命令之前：

```text
MuLi --config "/绝对路径/config.json" --data-dir "/绝对路径/data" serve
```

请将示例替换为本机实际路径。更换位置后，服务管理命令也要使用相同参数；已经安装的后台服务保存了安装时的绝对路径，修改终端环境变量不会自动更新它。托管安装的目录切换应按[本地托管操作](managed-operations.md)处理，不要直接覆盖正在使用的安装。

## 备份与恢复

需要可以恢复的备份时，最简单的方式是停服务后复制完整目录：

1. 让任务结束；若必须中断，先记下尚未确认的操作。使用 `MuLi service stop` 停止后台服务，前台运行则退出该进程，并确认已经停止。
2. 备份配置目录和整个数据目录。默认可复制完整的 `~/MuLi`，保留可能存在的数据库 `-wal`、`-shm` 文件以及附件、回收站和语音目录。
3. 另外备份工作目录中由沐璃创建或修改的文件。它们可能位于 `~/MuLi` 之外，不会随应用数据一起备份。
4. 恢复前保留当前现场；在服务停止时恢复同一份备份中的配置、数据库和文件，再用对应版本与路径启动。

运行中备份数据库应使用 SQLite backup 功能，不要只复制正在使用的 `muli.sqlite3` 主文件。配置、对话和工具结果可能包含私密信息，备份也需要按私人资料保存。

设置中的“导出个人数据”会下载 NDJSON，方便阅读聊天、任务、记忆和其它保留记录。它**不是可直接恢复的完整备份**：不包含配置、附件正文、回收站实际文件，也不保证不同数据表处于同一时刻。网页录音、截图、录屏和声纹参考音频都需另行保存。

重启后，未完成的即时运行、审批和工作流会显示为中断，已经暂停的任务仍保持暂停。旧审批不能继续批准，结果未知的文件写入、外发或桌面动作不会自动重做。先查看实际状态，再发起明确的后续请求；持久提醒和定时计划按各自规则继续检查，不等于重新执行旧工具。

## 从旧配置迁移

程序不会自动搬移旧仓库中的 `config.json`、`history/dialog.json` 或早期 XDG 目录，也不会自动加载旧密钥、运行旧 MCP 命令。已有兼容配置和数据可以通过上面的路径参数继续使用；搬移前先备份。

旧配置不能直接作为当前设置导入时，在新工作台重新填写。常见字段对应如下：

| 旧字段 | 当前配置 |
| --- | --- |
| `model_config.main_model.model_name` | `models.main.model` |
| `model_config.main_model.api_base_url` | `models.main.base_url` |
| `model_config.main_model.api_key` | `models.main.api_key` 或 `api_key_env` |
| `provider_type=openai/deepseek` | `protocol=openai_chat` |
| `model_config.json_model` | 可配置为 `models.reviewer`，需确认接口兼容 |
| `tools_api_config.web_search.base_url` | `search.base_url` |

旧聊天记录不自动转成可信记忆。需要保留的资料可通过[记忆入口](memory.md)或[附件收藏](attachment-memory.md)明确加入。

## 更新与故障排查

发现新版不代表当前安装可以直接原位更新。工作台会显示允许的更新方式；带格式标记的资料库需要核验兼容的托管候选，不能靠一次批准绕过。使用[自身管理](self-management.md)或[本地托管操作](managed-operations.md)的入口，并保留旧安装和备份。

若出现格式不兼容或标记损坏，先保留文件并查看诊断。不要删除格式标记、手改数据库版本或混用不同备份中的文件来强行启动。

| 问题 | 先做什么 |
| --- | --- |
| 工作台打不开 | 用 `MuLi service status` 看服务状态；必要时在终端运行 `MuLi serve` 查看启动错误，避免同时启动两个实例。 |
| 配置位置或能力不清楚 | 运行 `MuLi doctor`。能力可检测不等于桌面授权已经成功。 |
| Linux 服务异常 | 查看 `journalctl --user -u muli.service -f`；macOS、Windows 查看数据目录中的服务日志，需要保留的上一轮转文件时可请沐璃用 `self_logs({"segment":"previous"})` 读取，见[自身日志](self-management.md#主动读取自身日志)。 |
| 配置、更新或重启停在恢复阶段 | 先运行 `MuLi management status` 查看；按[恢复操作](linux-recovery-operations.md)接续。宿主不在运行时可用 `MuLi management recover`，不要反复新建相同操作。 |
| 某次任务失败 | 在工作台保留运行 ID，请沐璃查询故障位置；参见[故障诊断](failure-diagnostics.md)。 |

默认前台模型执行并发为 2，每次运行最长 600 秒、最多 16 次工具调用；可在执行预算中调整。systemd 用户服务另有默认的 512 MiB 内存、50% CPU 和 64 个任务限制。提高应用预算不会自动提高系统服务限制。

电脑操作、截图和录屏依赖当前桌面与授权；语音默认关闭，还需要相应录音、声学组件与 ASR/TTS 配置。按需阅读[电脑操作](computer.md)、[平台支持](platforms.md)、[语音](voice.md)、[提醒与主动事项](attention.md)及[桌面通知](notifications.md)。
