# 配置、迁移与运维

## 配置入口

所有常规配置可以从工作台设置页面管理。高级配置为严格 JSON；未知键被拒绝。模型的 reasoning / extra_body / extra_headers 为对象，PATCH 时按整体替换；留空 api_key 保留原密钥，clear_api_key=true 显式清除。读取 API 返回 api_key_set / credential_set，不返回密钥。包含 token 的自定义请求头仅显示占位符。

协议基础地址需包含供应商所需版本前缀，例如 `https://provider.example/v1`；不要把 `/chat/completions`、`/responses`、`/messages` 当基础地址。具体字段参见 providers.md。环境变量名称由用户选择，默认不假定任何供应商。

搜索服务同样由用户指定，避免默认把个人查询发送到不明公共实例。SearXNG 需开放 JSON 格式；Brave base URL 为其 API 根地址，Tavily 为其服务根地址。

## 旧版本迁移

旧基线使用仓库根目录 config.json、history/dialog.json 和导入时启动的 MCP。新版本使用独立 XDG 配置与 SQLite 数据，不自动加载旧密钥、不自动启动旧 MCP 命令。迁移前保留这些文件。

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

操作路径是 snapshot → action/type → 再次 snapshot。快照具有有效期，操作校验进程及 D-Bus 对象身份，成功或失败都消耗快照。界面不支持的动作不会降级成任意坐标点击。图像、坐标、macOS AX 和 Windows UIA 留有后端扩展空间，当前不声明已实现。

## 数据与恢复

数据目录保存 muli.sqlite3（含 WAL）、文件回收站。备份运行中数据库应使用 SQLite backup API 或停服务后完整复制，不能只复制活跃数据库主文件。配置与数据库包含个人资料，勿提交 Git。

设置中的“导出个人数据”下载 NDJSON，包含会话、运行、记忆、关系和修订。导出逐批读取，不加载整个数据库，也不包含模型配置密钥；它是可读数据导出，不是跨表一致性备份，不包含文件回收站的实际文件。需要可恢复备份时仍使用 SQLite backup 或停服务复制，另行保存配置与回收站。

重启后，进行中的会话、审批和工作流标为中断。可回看已完成步骤，再显式发起新任务；不会自动重放文件写入或桌面点击。chiliarch 收件使用事务先落盘再 ACK，重复身份内容不一致会拒绝。

## 服务与故障定位

- `muli doctor`：版本、配置位置、模型配置状态、桌面与服务能力。
- `muli service status`：用户服务状态和最近日志。
- `/api/docs`：自动生成的管理接口文档。
- `journalctl --user -u muli.service -f`：服务日志。

管理服务只监听回环地址，拒绝跨源变更与不合法 Host；不开放公网。其他机器访问请采用明确设计的认证代理，当前版本不提供公网部署模式。

配置修改不能与执行中的任务交错；先停止任务再改执行参数。端口修改在下一次启动生效。桌面授权依赖用户桌面会话，不能用 root 服务替代。

## 主动提醒

通知页可以创建一次或重复提醒、查看下一次触发、停用和删除；对话工具也可创建提醒。存储使用 Unix 秒，界面按浏览器本地时区显示。重复间隔至少 60 秒。调度器等待最近到期时间或变更通知，空闲不请求模型；停机期间错过的多次重复提醒在恢复时合并为一次，再计算下一次。

提醒进入工作台通知列表与 SSE。Linux 可在设置中开启“桌面通知”，复用系统已安装的 `notify-send`，关闭网页后也能接收新提醒；默认关闭。桌面通知失败不影响持久记录，不补发启动前的历史弹窗，也不唤醒关机的电脑。详见 notifications.md。

## 资源与平台

默认最多两个模型执行并行；主会话未完成任务和工作流运行分别最多八个，排队时间计入超时。上限可从设置的执行预算修改；提高上限前应核对供应商配额。systemd 的默认资源保护为 MemoryMax=512M、CPUQuota=50%、TasksMax=64，可通过用户 unit override 调整。

服务与数据库操作支持 Python 3.12+。文件、配置统一使用 UTF-8；POSIX 私人权限位不等同于 Windows ACL 隔离。Linux AT-SPI 和 systemd 的集成测试之外，其他平台只测试核心功能，不声明已实现 AX、UIA 或平台后台服务安装器。
