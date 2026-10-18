# 可选 MCP 工具

MCP 连接使用 FastMCP 2.x 的持久化异步客户端。核心安装不需要 MCP 库；只有显式启用服务器时才导入可选依赖。连接或列举工具失败只记入状态，不阻止本地助手启动。

配置沿用 `mcpServers` 格式，每个服务器增加明确的 `enabled` 开关：

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

只有 JSON 布尔值 `enabled:true` 才会连接；省略、false 均不启动，`disabled:true` 优先禁用。配置管理接口要求 enabled/disabled 为布尔值，字符串 true 会被拒绝；独立使用管理器时，非布尔 true 也不会触发启动。`stdio` 可省略 transport；URL 默认使用 streamable HTTP，兼容 `http` / `streamable-http`，旧式 SSE 使用 `sse`。HTTP 支持 headers，stdio 支持 command、args、env、cwd，不通过 shell 拼接命令。

## 配置编辑与密钥

配置管理仅接受根 `mcpServers`，最多 32 个服务器；单服务器只接受上述字段与 enabled/disabled/transport/timeout。timeout 为一天以内的有限正数，管理器进一步用全局时限收紧。URL 必须是 HTTP(S)，不允许内嵌账号、片段或空白。

`GET /api/settings` 保留 MCP 配置结构，将 env 和 headers 的全部值显示为 `••••`，URL 查询参数整体显示为 `?••••`。密钥建议放在 env 或 headers，避免放入可展示的 command、args、cwd 或服务器名称。

PATCH 中的 mcp 原子替换整个服务器配置；省略的服务器或密钥字段会被删除，`mcp: {}` 清空全部服务器。原位同服务器、同 env/header key 的 `••••` 会保留已有值；新增服务器或新增 key 使用占位符会被拒绝。URL 查询占位符只可在同服务器、同 endpoint 原位保留，修改 URL 后必须重新填写或删除查询参数。展示占位符本身不会被保存为真实密钥。

服务器程序必须已经可用。`npx` 只接受显式带 `--no-install` 或 `--offline` 的配置，`uvx` 必须带 `--offline`；管理器不会自动修改参数、下载包或安装服务器。启用 stdio 即允许启动用户配置的本机程序，该程序使用当前用户权限。

## 运行时接线

`MCPManager(config, timeout=30, max_output_chars=24000, max_tools=100)` 接收上例中的 `mcp` 对象：

- `await start()` 返回统一 `Tool` 列表，重复调用复用当前连接；每个外部工具都标记 `risk='external'`，由运行时权限门决定批准，不能采信服务器的只读声明绕过审批。
- `status()` 返回 name/status/tool_count/transport 和安全错误原因；不返回服务器 URL、命令参数、环境变量或请求头。
- `await close()` 退出客户端上下文并关闭 transport，清理持久连接和 stdio 子进程。关闭后应创建新管理器应用新配置。

工具名使用 `mcp_服务器_工具_短哈希`，符合模型函数名长度限制；即使原名称经过字符替换后相同，哈希仍区分来源。远端工具保持原参数 schema，本地权限门仍会进行参数校验。

全局 timeout 同时限制连接、工具列举和每次调用；单服务器 timeout 只能收紧它。调用不自动重试，以免外部操作重复。取消会传播到客户端；超时或异常只返回固定安全原因，不回显服务器异常正文。

文本与结构化结果遵守 `max_output_chars`，超限返回 `truncated:true` 和受限 preview。图片、音频、二进制资源只保留类型元数据，不把 base64 数据塞入模型上下文或自动下载媒体。服务器错误结果正文不回显；已配置 env/header 值以及 Bearer token 在正常文本中也会遮盖。服务器日志和 stdio stderr 不转发给模型或应用日志。

## 验证边界

`tests/test_mcp.py` 验证默认禁用、不自动下载、持久连接、命名隔离、统一外部风险、配置传递、错误遮盖、调用时限、取消和结果限制；可选依赖存在时，还使用真实 FastMCP 客户端连接进程内测试服务器验证完整协议与关闭生命周期。没有连接用户的实际外部服务器，也没有下载服务器包。

接口依据：[FastMCP 客户端](https://gofastmcp.com/clients/client)、[FastMCP transport](https://gofastmcp.com/clients/transports)，并按本地 FastMCP 2.13.1 的构造与调用签名核对。
