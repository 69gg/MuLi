# 用户插件与自身管理

MuLi 可以通过工具管理自己。`self_config` 返回脱敏完整配置，`self_configure` 校验补丁并写入私人待应用快照，在前台和团队空闲后应用。队列有配置指纹，期间其它入口修改配置会使旧计划失败，避免覆盖新设置。`self_status` 与 `/api/management` 可查看进度，待应用计划可由管理 API 取消。

配置文件原子替换；运行时重新载入失败时恢复原配置。监听 host/port 和插件启用状态需要重启进程，API 不声称已更换监听 socket。非回环进程不能通过“改为回环并清密码”的热修改提前关闭实际仍暴露的认证边界。`self_restart` 先校验配置，等待当前工作完成后调用用户服务。

每次服务启动仅检查一次有界 PyPI 元数据，失败不阻止启动。发现新版本写入通知与主会话上下文。`self_update` 无论 ask/auto/yolo 都经真实用户审批，随后只更新 uv tool 安装；开发虚拟环境不会被替换。更新不下载新 Python，不沿用代理环境；源码参考有本地修改时保留，不重置。包安装成功但服务重启失败会保留明确失败记录，不报告完整更新成功。自动版本回退尚未实现，不承诺跨数据库迁移回滚。

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
