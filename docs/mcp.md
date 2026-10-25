# 可选 MCP 工具与资源

MCP 用来连接外部工具服务器。核心聊天不依赖 MCP；需要接入时，安装带 `mcp` 可选依赖的 MuLi，例如首次安装使用 `uv tool install "muli-agent[mcp]"`。服务器程序须另行安装，MuLi 不会自动下载它。

## 接入服务器

在配置的 `mcp.mcpServers` 中添加服务器，将示例路径、地址和凭据改为实际值，确认后设置 `enabled:true`：

```json
{
  "mcp": {
    "mcpServers": {
      "local_tools": {
        "enabled": false,
        "command": "python3",
        "args": ["/path/to/installed/server.py"],
        "env": {},
        "cwd": "/path/to/workspace"
      },
      "remote_tools": {
        "enabled": false,
        "transport": "http",
        "url": "https://example.com/mcp",
        "headers": {},
        "timeout": 20
      }
    }
  }
}
```

- 本机 stdio 使用 `command`、`args`、`env`、`cwd`，可省略 `transport`。程序以当前用户权限运行，不是隔离沙箱。
- 远程连接使用 `http` 或 `streamable-http`；旧式 SSE 使用 `sse`。URL 必须为 HTTP(S)，不能嵌入账号、空白或片段；凭据放在 `headers`。
- 内联配置只有布尔值 `enabled:true` 才连接；省略或 `false` 均关闭，`disabled:true` 优先禁用。
- `timeout` 是有限正数，只能收紧全局调用时限。超时或结果未知时不会自动重做外部操作。
- `npx` 须显式携带 `--no-install` 或 `--offline`，`uvx` 须携带 `--offline`。MuLi 不会替你补参数或安装包。

工具在模型目录中显示为 `mcp_服务器_工具_短哈希`，沿用服务器的参数定义和 MuLi 的工具权限。服务器声明“只读”不能绕过审批。连接失败可从 `self_status` 查看，不阻止本地助手启动。

## 目录发现与活跃任务热重载

也可将服务器配置拆成 `~/MuLi/mcp.d/*.json`，每个文件内容为 `{"mcpServers": {...}}`。设置 `MULI_HOME` 后默认目录随之改变；`extensions.mcp_directory` 可指定其它目录。只读取一级普通 `.json` 文件，不递归、不跟随符号链接。

**目录分片省略 `enabled` 时默认启用，内联配置省略时默认关闭。** 同名服务器或重复 JSON 键会使候选配置失败，不按文件名覆盖。

默认每 2 秒发现变化，并合并短时间内的连续编辑。可设置 `extensions.watch_enabled:false` 关闭自动发现；手动重载使用已认证的 `POST /api/extensions/reload`，请求体为 `{}`。通过 `GET /api/extensions` 或 `self_status` 查看是否生效；`queued` 仅表示已排队。

HTTP/SSE 配置和 Skills 可以在任务运行时更新。候选加载失败时保留上一份可用配置。已有 stdio 服务器的配置变化需要重启，状态会提示 `restart_required`。只修改 `mcp`、`skills`、`extensions` 可走扩展热重载；混合修改模型、渠道等配置时仍需等待空闲。

换代后，尚未开始的旧工具调用会被拒绝，模型下一轮使用新目录；已经开始的调用可在旧连接上完成，不自动重试。旧连接尚在收尾时，新重载可能排队或返回 `generation_draining`。重启会重新连接服务器，不持久保留上一进程的工具引用。

常用扩展预算如下，均位于 `extensions`：

| 字段 | 默认值 |
| --- | --- |
| `max_mcp_fragments` | 32 个分片 |
| `max_mcp_fragment_bytes` / `max_mcp_source_bytes` | 单分片 64 KiB / 合计 256 KiB |
| `max_mcp_pages` | 每次完整目录观察最多 8 页 |
| `max_mcp_tools` | 所有服务器合计 100 个工具 |
| `max_mcp_catalog_bytes` | 目录 JSON 合计 256 KiB |
| `candidate_timeout_seconds` | 候选准备 30 秒 |

服务器合计最多 32 个；超限会明确失败，不把截断目录当作完整目录。

## HTTP/SSE 接收策略

使用 `self_mcp_receive_configure({"max_message_bytes":1048576})`，或已认证的 `GET/PUT /api/management/mcp-receive-policy` 设置单条接收上限。默认 1 MiB，允许 64 KiB–4 MiB 的整数。它是独立策略，不放在主配置或 `self_configure` 补丁中；保存为完整替换，省略字段恢复默认。

保存后需成功重载连接才生效。`self_config.mcp_receive_policy` 显示 `desired`、`effective`、`pending`、`error`；失败时保留旧有效值。普通响应按完整消息限制，SSE 长连接按单个事件限制；此设置不适用于 stdio，也不代表所有连接的总内存上限。

接收失败会封锁该连接的新调用，状态中可查询固定原因。MuLi 不自动重连或重放操作；修改配置或显式重载后再查看状态。通知是否出现还取决于系统事件与主动通知设置。

## 按需普通资源

已启用且声明资源能力的 HTTP/SSE 服务器可按需使用：

1. `mcp_resources_list({"server":"remote_tools"})` 列出资源及可用 `ref`。有 `next_cursor` 时，将其原样作为 `cursor` 继续翻页。
2. `mcp_resource_read({"server":"remote_tools","ref":"已展示引用"})` 读取完整短文本。不能直接传任意 URI 或本机路径。
3. 如需保存二进制资源，显式调用 `mcp_resource_save({"server":"remote_tools","ref":"已展示引用","name":"资源.bin"})`。只接受单个完整 blob，保存后返回附件 ID、类型、大小和 SHA-256。

目录和正文不会在连接时自动预取。文本读取不接受二进制或混合媒体；超出输出预算时明确拒绝，不以节选冒充全文。资源始终是外部资料，不能作为主人指令或工具批准。

保存沿用附件的单文件与总配额，不自动识图或发送。需要时再调用 `image_describe` 或 `channel_send`。若保存结果为 `unknown`，不要立即重复保存；可用 `attachment_search` 的 `source:"mcp"` 与文件名条件查找，再核对实际附件。

刷新目录、连接换代、关闭或重启会使旧引用和游标失效，须重新列出。URI 只交给原 MCP 服务器，不作为本机文件路径打开。当前不支持 stdio 资源、资源模板、订阅或后台抓取。

## 搜索结果资源链接

工具完整成功返回协议中的 `resource_link` 时，回执可显示 `resource_links_available:true`。调用 `mcp_resources_list({"server":"remote_tools","source":"tool_result"})` 查看本次链接，再将展示的 `ref` 交给 `mcp_resource_read`。

这与普通目录分开；普通文字 URL 不会自动变成可读资源。这类链接仅支持文本读取，不支持直接保存文件。每连接只保留最近一次工具调用的结果引用，新调用、换代、接收失败或关闭后失效。

## 工具直接返回的图片

HTTP/SSE 工具返回合格图片内容时，MuLi 展示宿主引用、MIME 和大小。显式调用 `mcp_resource_save` 保存附件后，才可继续用 `image_describe` 看图；只提供工具的服务器无需额外声明资源能力。

图片引用同样只属于该连接最近一次调用。不会自动识图，也不会把普通文本中的 base64、音频或其它二进制当作图片。识图需要主模型支持图片，或配置独立视觉模型。

## 显式文本 prompts 草稿

声明 prompts 能力的 HTTP/SSE 服务器可使用：

```text
mcp_prompts_list({"server":"remote_tools"})
mcp_prompt_get({"server":"remote_tools","ref":"已展示引用","arguments":{"code":"字符串"}})
```

先查看目录中完整参数，再按引用调用；必填参数须提供，值只能是字符串。参数最多 32 项、完整 JSON 最多 16 KiB。游标与引用不能和资源目录混用，换代后需重新获取。

结果最多包含 16 条有序文本草稿。草稿中的 user/assistant 角色仍是外部资料，不创建会话指令、不自动执行，也不变成 Skill。混合媒体和超预算全文会被拒绝。当前不支持 stdio prompts、补全或订阅。

## 配置编辑与密钥

`GET /api/settings` 中的 MCP `env`、`headers` 值显示为 `••••`，URL 查询参数也会遮盖。密钥应放在 `env` 或 `headers`，避免写入可展示的服务器名、命令参数或工作目录。

更新 `mcp` 会替换整个对象：省略的服务器和密钥字段会删除，`mcp:{}` 清空全部服务器。原服务器、原键上的 `••••` 可保留旧密钥；新增服务器或键不能用占位符代替真实值。更换 URL 后，查询参数须重新填写或删除。

工具结果会遮盖已配置的凭据；普通工具输出过长可能提供截断预览，完整资源读取则直接报超限。服务器错误正文和 stdio stderr 不转发给模型。仍应避免让服务器输出秘密，脱敏不能识别所有未知凭据。
