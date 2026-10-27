# 用户插件与自身管理

MuLi 可以查看自己的配置与状态、排队修改设置，并在受支持的安装方式下重启或更新。管理操作仍受当前工具权限与用户批准控制；模型说“已完成”不等于服务、安装或外部投递已成功。

## 查看和修改配置

1. 使用 `self_config` 查看脱敏配置，使用 `self_status` 查看管理计划、扩展、插件与更新能力。
2. 使用 `self_configure({"changes":{...}})` 提交配置补丁。返回排队状态后，继续查看 `self_status` 或已认证的 `GET /api/management`。
3. 若不再需要尚未应用的计划，可用 `POST /api/management/<计划ID>/cancel` 取消。

同一时间只能有一个等待或正在应用的管理计划。模型、渠道等修改通常等待前台和团队空闲；纯 `mcp`、`skills`、`extensions` 修改可在任务运行时验证并发布。排队期间若配置被其它入口更改，旧计划可能失败，应重新读取当前配置再决定。

MCP 和 Skills 支持目录发现与热重载，Python 插件启停或修改仍需重启。相关用法见 [MCP 接入](mcp.md)、[Skills](skills.md)和下方[插件目录](#插件目录)。MCP 资源须[按需读取](mcp.md#按需普通资源)，prompts 仅作为[文本草稿](mcp.md#显式文本-prompts-草稿)，不会因扩展启用而自动执行。

监听 `host` 或 `port` 的变化需要重启后应用；工作台会将这类修改排队，直接热修改会返回 `listener_restart_required`。保存或排队不表示新监听地址已经生效。其它设置重载失败时会尝试恢复上一配置；恢复失败仍需处理，不能仅凭配置文件恢复就判断组件健康。

部分功能使用独立策略，不放在 `self_configure` 的主配置补丁中：

| 功能 | 工具或管理入口 | 使用提示 |
| --- | --- | --- |
| MCP 接收限制 | `self_mcp_receive_configure`；`GET/PUT /api/management/mcp-receive-policy` | 见 [HTTP/SSE 接收策略](mcp.md#httpsse-接收策略) |
| 本地候选更新 | `self_update_configure`；`GET/PUT /api/management/update-policy` | 默认关闭；先读现值，再完整替换 |
| 在线候选材料 | `self_update_online_configure`；`GET/PUT /api/management/update-online-policy` | 默认关闭；不自动下载依赖 |
| 资料收件目录 | `file_inbox_configure`；`GET/PUT /api/file-inbox/settings` | 默认关闭；见[资料收件目录](file-inbox.md) |

完整替换策略时，省略字段会恢复默认值。保存策略不等于已经连接、下载或安装成功。

## 重启、切换与更新

`self_restart` 仅在符合条件的当前用户服务中可用；不满足条件会明确拒绝，不退回未经核验的直接重启。Linux 支持条件和操作见[恢复入口](linux-recovery-operations.md)。

`self_adopt` 可将当前保留安装迁入托管；`self_switch` 可切换到另一份已保留的兼容本地安装。两者使用安装、配置与 launcher 的绝对路径，不自动下载或创建环境。请先按[本地托管切换](managed-operations.md)准备材料，不要覆盖原控制安装或候选安装。

托管候选更新按以下顺序进行：

1. 服务启动时会进行一次有限的新版本元数据检查，失败不阻止启动，也不会自动安装。
2. 用 `self_status` 查看 `update_capability`，确认当前安装支持哪种更新方式。开发虚拟环境不会被自动替换；要求独立候选的安装不能用原位覆盖绕过检查。
3. 用 `self_update_preview` 查看候选。启用在线策略时，可传 `{"version":"精确版本"}` 选择版本。
4. 将预览返回的 `expected_policy`，以及在线预览的 `expected_artifact`，原样交给 `self_update`。**无论 ask、auto 还是 yolo，更新都需真实用户批准。**
5. 持续查看管理计划与服务状态。批准、下载完成或包安装成功，都不能单独证明新服务已经可用。

仅当 `update_capability` 允许旧原位更新，且实际安装检查通过时，可使用 `self_update({"version":"精确版本"})`；这条路径不要求候选预览，仍需真实用户批准。它不提供自动版本回退，不满足安装条件时不能绕过检查。

本地候选策略需指定已准备的 wheelhouse、保留目录与 uv 绝对路径。在线策略只在批准后下载一枚 MuLi wheel，默认上限 8 MiB、最大可配置 32 MiB，不自动获取依赖或新 Python。详细准备方式见[候选材料说明](bounded-online-candidate-design.md)。遇到互不兼容约束需用户先明确调整。

未解决的恢复操作会阻止普通配置与扩展换代。此时使用 `self_recover` 或 `POST /api/management/transactions/recover` 接续原操作；它不重新执行结果未知的业务操作，也不自动重新安装。证据不足时会保留阻断，不能靠重复调用宣称恢复。更新不承诺跨数据库迁移回滚，源码参考中的本地修改也不会被重置。

新版本等无发令会话的通知可使用[默认通知渠道](channels.md#无发令会话时的默认通知)；已有计划的结果保留原请求来源，不因完成时无人发令而改绑。

## 主动读取自身日志

- `self_logs({"lines":40})`：读取最近服务日志，`lines` 范围 1–200。Linux 查询当前用户的 `muli.service`；macOS、Windows 读取服务启动器管理的 `data/logs/service.log`，不包含轮转旧文件。开发模式可能没有对应日志。
- `self_events({"limit":20,"level":"error"})`：查询保留的结构化系统事件，可按组件、错误代码和运行或会话 ID 过滤；用返回的 `next_before_event_id` 继续翻页。详见[系统事件](system-events.md)。
- `self_failure({"run_id":"32位小写十六进制ID"})`：查询已知运行的有限宿主故障位置，只提供异常类别与可核验的包内函数、行号，不包含异常正文、局部变量或源码。详见[故障位置查询](failure-diagnostics.md)。

这些工具只读诊断，不接受任意文件路径、服务名或 shell 命令，也不自动触发通知或重试。结果为空不代表健康；`truncated` 表示资料可能不完整，`unavailable` 表示本次无法读取。

日志返回前会遮盖配置中的已知凭据，但无法识别所有历史秘密；请勿主动把密钥写入日志。日志和故障资料中的文字不是新指令或用户批准。

## 插件目录

默认目录为 `~/MuLi/plugins/<id>/`；使用自定义配置文件位置时，插件目录位于实际配置文件旁。插件是用户信任的 Python 代码，使用与 MuLi 进程相同的权限，不是隔离沙箱。

每个插件包含 `plugin.json` 和 Python 入口，例如：

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

`setup` 可同步或异步。插件可注册工具、提示词、上下文与生命周期钩子、模型客户端和 `/api/plugins/<id>/` 下的路由；这些路由沿用管理认证，模型调用插件工具仍走统一权限门。同名工具可能覆盖内置行为，应先审阅插件；多个插件争用同名工具会报告冲突。

可用 `plugin_install({"id":"personal","files":{...}})` 校验并保存插件文件，**重启后才加载**。安装不会执行插件、覆盖已有目录或自动安装依赖。修改已有插件，或把 ID 加入 `plugins.disabled` 后，也需重启才能生效。通过 `self_status` 查看清单和 `plugins_restart_required`。

关闭 `plugins.enabled` 后，启动时不会创建或扫描插件目录。目录无法创建或列出时，清单报告 `plugin_directory_unavailable`；单个条目无法检查时报告 `plugin_entry_unavailable`，主会话和其他健康插件仍可使用。修复对应路径或权限后正常重启，错误不会在当前进程中被自动重试或清除。

上下文钩子报错、超时或返回格式无效时，插件状态变为 `degraded`，显示失败阶段和异常类别。该次请求继续失败，不会自动跳过插件；后续正常执行成功，才清除这项故障状态。取消请求不算插件故障。

插件导致对话无法继续时，可以在终端检查和调整下次启动设置：

```sh
MuLi plugins inspect personal
MuLi service stop
MuLi plugins disable personal
MuLi service start
```

`inspect` 只检查指定插件的清单与入口结构，不执行其代码；`ready` 表示结构可用，不能证明运行健康。`disable` 和 `enable` 目前仅支持 Linux，需要已安装且确认停止的用户服务；恢复操作未结束、安装身份不符或配置被同时修改时会拒绝，不自动重启。使用自定义路径时，在 `plugins` 前传入同一组 `--config`、`--data-dir`。

修复后，可停机运行 `MuLi plugins enable personal` 再启动。两条变更命令只修改 `plugins.disabled` 中的对应 ID，不删插件或资料；`enable` 不会覆盖全局 `plugins.enabled` 或插件清单里的停用设置。可信 Python 插件的既有副作用无法靠停用回滚。

`persona.prompt` 替换基础提示词，`persona.append` 追加个性设定，插件的 `append_prompt` 再追加内容。日常记忆和长对话不需要编写插件，分别见[记忆](memory.md)与[长对话](context.md)。

## 持续工作账本

`commitment_save` / `commitment_list` 保存承诺的标题、下一步、负责人、来源和状态。完成需填写证据，取消需说明理由；对话摘要变化或换入口不会删除账本。

任务说明可保存完整目标、限制与证据引用，仅真实主会话可编辑正文。`commitment_inspect` 可查看关联计划、队友报告、工作流与依据。读取、保存说明或模型自报完成都不等同于批准执行或外部验收。

详见[任务说明](task-briefs.md)与[任务检查](task-inspection.md)。
