# 配置、迁移与运维

## 配置入口

所有常规配置可以从工作台设置页面管理。高级配置为严格 JSON；未知键被拒绝。模型的 reasoning / extra_body / extra_headers 为对象，PATCH 时按整体替换；留空 api_key 保留原密钥，clear_api_key=true 显式清除。读取 API 返回 api_key_set / credential_set，不返回密钥。包含 token 的自定义请求头仅显示占位符。

协议基础地址需包含供应商所需版本前缀，例如 `https://provider.example/v1`；不要把 `/chat/completions`、`/responses`、`/messages` 当基础地址。具体字段参见 providers.md。环境变量名称由用户选择，默认不假定任何供应商。

搜索服务同样由用户指定，避免默认把个人查询发送到不明公共实例。SearXNG 需开放 JSON 格式；Brave base URL 为其 API 根地址，Tavily 为其服务根地址。

## 旧版本迁移

旧基线使用仓库根目录 config.json、history/dialog.json 和导入时启动的 MCP。v1.0.0 使用 XDG 配置与 SQLite 数据；当前版本默认集中到 ~/MuLi，不自动加载旧密钥、不自动启动旧 MCP 命令，也不自动搬移已有数据。可继续通过 MULI_CONFIG / MULI_DATA_DIR 指向旧位置，迁移前保留原文件。

可按以下对应关系在新工作台配置：

| 旧字段 | 新字段 |
| --- | --- |
| model_config.main_model.model_name | models.main.model |
| model_config.main_model.api_base_url | models.main.base_url |
| model_config.main_model.api_key | models.main.api_key 或 api_key_env |
| provider_type=openai/deepseek | protocol=openai_chat |
| model_config.json_model | 可用作 models.reviewer，需检查该模型支持普通 JSON 输出 |
| tools_api_config.web_search.base_url | search.base_url |

历史消息不自动转成可信长期记忆；有价值事实可通过记忆入口导入并标明来源。旧 Docker 工具不再默认启动容器，命令工具使用当前用户权限与运行预算。

## 无障碍电脑操作

Linux 需要桌面会话 D-Bus、AT-SPI、系统 Python GI。Arch Linux 使用系统发行的 python-gobject / at-spi2-core；不需要在 MuLi 虚拟环境编译 GTK。设置 computer.python_path 指向具有 GI 的系统 Python，然后启用电脑工具。先在状态页查看能力。

操作路径是 snapshot → action/type → 再次 snapshot。快照具有有效期，操作校验进程及 D-Bus 对象身份，成功或失败都消耗快照。界面不支持的动作不会降级成任意坐标点击。无障碍树不可用时可单独请求截图；截图不产生可执行节点身份，不能代替操作前的快照。macOS AX、Windows UIA 与任意坐标操作仍未实现。

桌面图像采集另由 computer.capture.enabled 开启，默认关闭。截图与有界录屏保存在数据目录 captures/，随后可作为本地附件使用；不自动上传、不录制声音。工具沿用 computer 风险及 ask/auto/yolo 权限模式。单文件默认 32 MiB、目录默认 200 MiB，默认录屏上限 30 秒、8 fps；超过配额或取消时终止采集进程并删除未完成文件。后端与授权边界见 [平台能力](platforms.md)。

## 数据与恢复

数据目录保存 muli.sqlite3（含 WAL）、文件回收站、uploads/ 附件库，以及启用后生成的 captures/ 和 voice/。备份运行中数据库应使用 SQLite backup API 或停服务后完整复制，不能只复制活跃数据库主文件。配置与数据库包含个人资料，勿提交 Git。截图、录屏、声纹参考与合成音频需要另行备份；NDJSON 数据导出不包含这些二进制文件。

设置中的“导出个人数据”下载 NDJSON，包含会话、运行、待处理输入、团队与管理账本、记忆原文、事实与关系证据、冲突、社区、修订、向量和渠道投递记录。导出逐批读取，不加载整个数据库，不包含配置文件或渠道协议凭据缓存；它是可读数据导出，不是跨表一致性备份，也不包含附件及文件回收站的实际文件。对话和工具结果本身可能包含用户曾发送的敏感内容。需要可恢复备份时仍使用 SQLite backup 或停服务复制，另行保存配置与本地文件。

重启后，进行中的会话、审批和工作流标为中断。可回看已完成步骤，再显式发起新任务；不会自动重放文件写入或桌面点击。chiliarch 收件使用事务先落盘再 ACK，重复身份内容不一致会拒绝。

## 服务与故障定位

- `muli doctor`：版本、配置位置、模型配置状态、桌面与服务能力。
- `muli service status`：用户服务状态和最近日志。
- `/api/docs`：自动生成的管理接口文档。
- `journalctl --user -u muli.service -f`：服务日志。

服务管理按当前系统选择 systemd 用户服务、macOS LaunchAgent 或 Windows 当前用户计划任务。install 只安装，enable 只管理登录自动启动；start、stop、restart 分开执行。不会使用管理员权限或创建系统级服务。Windows 必须有当前 Python 环境对应的 pythonw.exe，避免弹出控制台；服务内重启由独立的一次性计划任务执行，避免结束发起者后无法再启动。macOS / Windows 日志保存至 logs/service.log，单份最多 1 MiB 并保留一个轮转文件。实际平台验证范围见 [平台能力](platforms.md)。

交互 `MuLi init` 在配置成功后依次请求 install、enable、start，报告已完成步骤；启动请求被接受不等于服务已健康。`--no-service` 或 `--defaults` 跳过这一步，开发环境建议使用 `MuLi init --no-source --no-service`。启动失败时配置仍保留，可查看 `MuLi service status` 或使用 `MuLi serve` 诊断。

管理服务默认监听回环地址。绑定非回环地址必须配置访问密码；登录使用 HttpOnly、SameSite=Strict cookie，API 拒绝跨源变更与不合法 Host。密码和监听地址可在初始化或设置中配置；外部访问仍需自行提供合适的 HTTPS 入口。

配置修改不能与执行中的任务交错；先停止任务再改执行参数。端口修改在下一次启动生效。桌面授权依赖用户桌面会话，不能用 root 服务替代。

## 主动提醒

通知页可以创建一次或重复提醒、查看下一次触发、停用和删除；对话工具也可创建提醒。存储使用 Unix 秒，界面按浏览器本地时区显示。重复间隔至少 60 秒。调度器等待最近到期时间或变更通知，空闲不请求模型；停机期间错过的多次重复提醒在恢复时合并为一次，再计算下一次。

提醒进入工作台通知列表与 SSE。Linux 可在设置中开启“桌面通知”，复用系统已安装的 `notify-send`，关闭网页后也能接收新提醒；默认关闭。桌面通知失败不影响持久记录，不补发启动前的历史弹窗，也不唤醒关机的电脑。详见 notifications.md。

`attention.enabled` 默认开启：新到期提醒、维护终态与发现新版在活动主任务完整结束后形成独立的只读回合，不中断正在进行的工具批次或渠道回复；有主模型配置时触发一次正常回复，可能产生模型费用。系统资料不是用户授权，回复留在主会话，不广播到历史渠道。首次启用和重新启用跳过先前历史；没有模型配置时只持久保留一批，配置恢复后继续处理。事件消费、去重、队列背压与关闭行为见 [系统注意事项](attention.md)。

## 资源与平台

默认最多两个模型执行并行；主会话未完成任务和工作流运行分别最多八个，排队时间计入超时。上限可从设置的执行预算修改；提高上限前应核对供应商配额。systemd 的默认资源保护为 MemoryMax=512M、CPUQuota=50%、TasksMax=64，可通过用户 unit override 调整。

服务与数据库操作支持 Python 3.12+。文件、配置统一使用 UTF-8；POSIX 私人权限位不等同于 Windows ACL 隔离。平台服务、录屏和语音新增适配经过模拟测试，未在本次开发机实际安装服务、采集桌面或录制麦克风；不把能力探测等同于实际桌面授权成功。

语音默认关闭，需要录音工具、真正的声学唤醒桥接和已配置的 ASR。启用声纹验证后还需显式录制参考音频。唤醒后播放“请说”或短提示音；播放不可用会记录状态，不假称已经播放。配置、桥接格式、文件保留和身份边界见 [语音](voice.md)。
