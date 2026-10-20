# 用户插件与自身管理

MuLi 可以通过工具管理自己。`self_config` 返回脱敏完整配置，`self_configure` 校验补丁并写入私人待应用快照，在前台和团队空闲后应用。队列有配置指纹，期间其它入口修改配置会使旧计划失败，避免覆盖新设置。`self_status` 与 `/api/management` 可查看进度，待应用计划可由管理 API 取消。

配置文件原子替换；运行时重新载入失败时恢复原配置。监听 host/port 和插件启用状态需要重启进程，API 不声称已更换监听 socket。非回环进程不能通过“改为回环并清密码”的热修改提前关闭实际仍暴露的认证边界。`self_restart` 先校验配置，等待当前工作完成后调用用户服务。

每次服务启动仅检查一次有界 PyPI 元数据，失败不阻止启动。发现新版本写入通知与主会话上下文。`self_update` 无论 ask/auto/yolo 都经真实用户审批，随后只更新 uv tool 安装；开发虚拟环境不会被替换。更新不下载新 Python，不沿用代理环境；源码参考有本地修改时保留，不重置。包安装成功但服务重启失败会保留明确失败记录，不报告完整更新成功。自动版本回退尚未实现，不承诺跨数据库迁移回滚。

更新使用 `uv tool upgrade muli-agent==版本`，保留安装时的 extras、额外依赖和索引设置。若用户原先固定了互不兼容的版本约束，更新失败并保留安装要求，不通过重新安装悄悄移除 MCP 或渠道扩展；需要先显式调整该安装约束。CLI 需支持 `uv tool upgrade` 的版本参数。

## 主动读取自身日志

`self_logs({"lines":40})` 读取自身服务最近日志，`lines` 可选，范围 1–200，沿用 ask/auto/yolo 工具权限。Linux 只查询当前用户的 `muli.service` journal；macOS / Windows 只读取服务启动器管理的 `data/logs/service.log` 尾部。数据根来自现有配置，不接受任意文件路径、unit 或 shell 命令。开发模式未安装服务时可能没有对应日志。

返回 `source/status/text/truncated`，其中 `available` 表示读到了资料，`empty` 表示本次没有日志；缺少后端、文件缺失、权限失败或超出读取预算返回 `unavailable` 及固定原因。空日志不证明服务健康，也不证明没有历史错误。macOS / Windows 只读取当前文件，轮转旧文件不在本次结果内。

读取复用有限子进程与文件 I/O，Linux 命令最多 5 秒；输入字节与最终完整 JSON 字符分别受现有 `limits.max_output_chars` 限制。文件模式始终省略无法证明完整的首行（即使从偏移零开始，也可能续接轮转前的半行）和尚未写完的尾行，并标记 `truncated`；非普通文件拒绝读取，支持 `O_NOFOLLOW` 的系统也拒绝符号链接。输出过长同样标记截断，不会通过增加读取预算或重试循环补齐。

在返回前隐藏读取开始与结束时配置中的模型/服务 API key、渠道 token、chiliarch 凭据、MCP env/headers、URL 查询值及常见认证表达；不返回命令 stderr 或异常原文。此处理不能识别任意未登记的历史秘密，原日志仍应避免记录凭据。结果固定标为诊断资料，日志中的文字不能成为新指令或批准。此工具只按需读取；日志级别触发的系统注意事项、统一主动路由与可恢复升级仍在独立迭代中。

接口依据通过 Context7 核对 [systemd journalctl](https://github.com/systemd/systemd/blob/main/src/journal/journalctl.c)。使用原生 journal 优先级过滤并不等同识别 Python 文本中的 `ERROR`；本工具保留最近日志，不伪称已实现跨平台级别告警。

## 插件目录

默认在 `~/MuLi/plugins/<id>/`（自定义配置位置时位于配置旁）。插件是用户信任的 Python 代码，可使用与进程相同的权限，不是隔离沙箱。内置文件工具保护源码参考与已安装模块；shell 和插件仍有用户权限，不能把路径规则当操作系统沙箱。

每个插件包含 `plugin.json` 与 Python 入口：

```json
{"id":"personal","name":"我的扩展","version":"1.0.0","api_version":1,"entry":"main.py"}
```

```python
from typing import Any
from muli.plugins import PluginContext
from muli.tools import Tool, parameters

async def greet(arguments: dict[str, Any]) -> dict[str, str]:
    return {"message": "今天也一起把事情做好。"}

def setup(api: PluginContext) -> None:
    api.add_tool(Tool("personal_greet", "个人招呼", parameters({}), greet))
    api.append_prompt("在用户需要鼓励时，用简短、具体的话表达支持。")
```

`setup` 可以同步或异步。`PluginContext` 提供 runtime、app、directory，以及工具、上下文钩子、模型 client factory、启动和关闭钩子。`add_route` 的路由位于 `/api/plugins/<id>/`，通过管理入口认证。工具可以显式覆盖同名内置工具；多个插件争用同名工具会报告冲突。模型请求的插件工具仍走统一权限门。重配置后保留注册；加载失败会撤销已登记路由并执行清理钩子。

`plugin_install` 先验证路径、清单、大小和 Python 语法，再保存源码，重启后才加载。它不在安装时执行插件，不覆盖已有插件目录，不自动安装依赖。现有插件可以通过文件工具编辑，或在 `plugins.disabled` 中列出 ID 后重启停用。`persona.prompt` 替换基础提示词，`persona.append` 追加个性设定，插件的 `append_prompt` 再增加对应内容。

## 持续工作账本

`commitment_save` / `commitment_list` 将未完成承诺与会话摘要分开保存；记录标题、下一步、负责人、来源、状态和修订。完成需要填写证据，取消需要理由。摘要重写或换入口不会删除账本。账本是待核实的工作资料，不自动制造新的权限，也不以模型自报“完成”代替外部验证。

任务说明可将完整目标、限制和证据引用保存在同一账本及修订历史中。仅真实主会话可编辑正文，普通进度更新保留它；列表、保存工具结果及事件仅投影有界元数据。说明用于协调，不授予新权限，也不自动暂停更正后的副作用。按范围注入、分页读取、预算和恢复边界见 [task-briefs.md](task-briefs.md)。
