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

## 目录发现与空闲热重载

除原有内联 `mcp` 外，运行时读取 `MULI_HOME/mcp.d/*.json`，默认是 `~/MuLi/mcp.d/`。`extensions.mcp_directory` 可替换目录，空字符串使用默认路径。只扫描一级 `.json` 普通文件，不递归、不跟随分片符号链接。每个分片仍使用 `{"mcpServers": {...}}`；**目录分片省略 enabled 时默认启用**，显式 false 或 `disabled:true` 停用。原有内联配置省略 enabled 仍停用。

同一 JSON 对象的重复键、内联与分片之间或不同分片之间的同名服务器都会拒绝候选，状态说明冲突来源；没有按文件名覆盖的隐式优先级。读取前后核对已打开文件及路径身份，普通并发修改返回 `source_changed`，等待下一次稳定变更。该检查不构成对恶意本机程序的文件系统沙箱。

身份快照取路径的 `lstat()`；路径与打开句柄之间核对设备号、inode、大小和修改时间，句柄读取前后、路径读取前后各自额外核对 `ctime`。[Windows 的 `DirEntry.stat()`](https://docs.python.org/3.13/library/os.html#os.DirEntry.stat) 会把设备号与 inode 设为零，不能直接拿其缓存结果与 `fstat()` 比较；[CPython 的已知差异](https://github.com/python/cpython/issues/157671)还会让路径查询的 `ctime` 表示创建时间、句柄查询表示元数据变更时间，因此不跨 API 比较该字段。跨平台回归模拟零字段与不同但稳定的 `ctime`，并确认同一 API 读取期间的 `ctime` 变化仍被拒绝；不按平台跳过，也不移除非阻塞、符号链接和源变化核验。

初次启动保留 best-effort：失败来源或服务器记入状态，核心助手仍可启动，合法零工具服务器属于成功。后续变更必须通过全部启用服务器的连接、完整分页、名称、参数 schema 和总预算验证，才会发布整代；任一失败都保留当前工具、连接和 Skills 代。不会把前几页或前 100 个工具截断后当作完整目录成功。

首次准备也核对读取前和发布前的同一来源指纹；等待 MCP 或 Skills 时发生编辑，会丢弃旧候选并让监听器重新准备，不能把新文件指纹记到旧工具代上。未发布的失败或取消清除观察状态，完整配置重建后的临时扫描错误恢复为原指纹时也能重试。坏 Skills 来源不会阻止仍可用的 MCP 配置与核心助手启动；失败状态不冒充目录已成功加载。详见[生命周期回归](extension-lifecycle-review.md)。

发现变化或收到 `notifications/tools/list_changed` 后先合并为一个候选请求。主会话、queued/running agent 或未结束 workflow 忙碌时显示 queued；空闲后获取运行时配置锁，再检查真实活跃状态。队友报告等内部唤醒仍可在候选准备的等待期间启动主会话，因此在最后一次源核对之后、保存和发布之前再次同步检查 busy；发现新运行则丢弃候选，自动 reload 保留请求等待下次空闲。该检查到整组发布之间没有 await。未变服务器复用原连接；只为新增或改变的 HTTP/SSE 服务器建立候选连接。工具处理函数绑定已验证连接，发布时只替换 MCP 工具子集，不替换内置工具或其它 provider。

已有 stdio 服务器的配置变化保守返回 `restart_required`，保持旧进程和旧配置；不会先关闭旧进程来试运行候选。删除服务器仍可在空闲发布后关闭旧连接。要改变这类 stdio 配置，需要在停止或重启进程时应用源配置；窄管理接口不会提前保存无法发布的候选。

`PATCH /api/settings` 和 `self_configure` 的纯 `mcp`、`skills`、`extensions` 修改使用同一窄入口：先准备并验证全部候选，再原子保存配置，随后在没有 await 的步骤发布工具和 Skills 快照。保存失败丢弃候选，保留旧配置和旧能力。候选准备中出现新运行时，PATCH 返回 `409 runtime_busy` 且不保存；self_configure 保留 pending 计划及私有目标，等当前工作结束后重新准备。发布成功后才退休旧连接；旧连接关闭失败单独记为 retirement error，新代仍已生效，不声称回滚成功。退休任务由管理器强持有，调用者的重复取消不会取消正在关闭的连接；关闭失败的连接继续保留，由 shutdown 再尝试强制关闭客户端，不强杀进程、不重试工具执行。仍有未确认清理连接时拒绝新增候选并说明需要重启，避免重复积累连接。混合修改模型、渠道等其它配置仍走原有整体 reconfigure，暂不提供同等的候选保旧承诺。

`GET /api/extensions`（以及 `/api/status`、`/api/management`、`self_status` 中的 extensions）返回代号、phase、指纹、分片来源、固定错误码、Skills 计数和关闭错误。`last_apply` 分别显示配置 unchanged/saved/save_failed、是否 published、旧连接退休 unneeded/pending/complete/failed；不会回显请求头、环境变量、URL、远端异常正文或 Skill 正文。`POST /api/extensions/reload` 接收 `{}`，返回 queued 状态，要求显式重查当前服务器目录；它不表示已换代。

默认每 2 秒轻扫描、500 毫秒合并变更，最多一个准备任务与一个 dirty 标记；相同失败指纹不会定时重连，只有源变化、新通知或显式 reload 才重试发现。工具执行本身仍不自动重试。关闭 watcher 后的迟到服务器通知不会恢复已删除连接。

预算集中在 `Settings.extensions`，均有类型和上下限校验：

| 字段 | 默认值 | 含义 |
| --- | ---: | --- |
| max_entries | 256 | 目录一级条目总数 |
| max_mcp_fragments | 32 | JSON 分片数 |
| max_mcp_fragment_bytes | 65536 | 单分片源字节 |
| max_mcp_source_bytes | 262144 | 分片源总字节 |
| max_mcp_pages | 8 | 每服务器完整目录页数 |
| max_mcp_tools | 100 | 所有启用服务器的工具总数 |
| max_mcp_catalog_bytes | 262144 | 工具名、描述和参数 schema 的 JSON 总字节 |
| candidate_timeout_seconds | 30 | 整个候选准备时限；失败后独立清理另受连接时限约束 |
| scan_interval_seconds | 2 | 轻扫描间隔 |
| debounce_seconds | 0.5 | 变更合并窗口 |
| watch_enabled | true | 自动目录扫描；显式 reload 仍可用 |

服务器合计仍最多 32 个。Skills 的读取、目录和活跃正文预算独立放在 `Settings.skills`。目录预算是在 MCP SDK 解码单页后核对，不保证传输层巨型单帧的预解码内存上限；没有跨进程持久化上一可用工具代，也不支持执行中换代或任意 Python 插件热卸载。

## 配置编辑与密钥

整体重新配置也不能丢弃未确认关闭的 MCP 管理器。关闭后仍有待回收连接时抛固定 `connection_cleanup_pending`，保留清理引用；恢复原配置时若仍不能回收则继续明确失败，不创建新管理器或伪称全部组件已恢复。程序最终关闭也报告此状态；它不表示操作系统连接必然仍存活，只表示客户端未确认回收。

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

全局 timeout 同时限制连接、工具列举和每次调用；单服务器 timeout 只能收紧它，运行时另限制整个候选准备的总期限。调用不自动重试，以免外部操作重复。取消会传播到客户端；超时或异常只返回固定安全原因，不回显服务器异常正文。

文本与结构化结果遵守 `max_output_chars`，超限返回 `truncated:true` 和受限 preview。图片、音频、二进制资源只保留类型元数据，不把 base64 数据塞入模型上下文或自动下载媒体。服务器错误结果正文不回显；已配置 env/header 值以及 Bearer token 在正常文本中也会遮盖。服务器日志和 stdio stderr 不转发给模型或应用日志。

## 验证边界

`tests/test_mcp.py` 验证默认禁用、不自动下载、持久连接、命名隔离、统一外部风险、配置传递、错误遮盖、调用时限、取消和结果限制，以及完整分页、停滞游标、候选预算、失败保旧、复用连接归属、stdio 需重启、处理函数连接绑定和退休关闭失败。可选依赖存在时，还使用真实 FastMCP 客户端连接进程内测试服务器验证完整协议与关闭生命周期。`tests/test_extensions.py` 覆盖目录规则、分片预算、源变化、忙时排队、配置保存失败、准备期限、相同失败不循环重连和窄管理接线；门控真实队友报告在候选准备中唤醒离线主模型，核对 API 冲突、自动请求与管理计划重排，以及旧配置、工具与 Skills 代保留。没有连接用户的实际外部服务器，也没有下载服务器包。

`tests/conftest.py` 为每个测试隔离临时 `MULI_HOME`，并移除宿主的配置/数据目录环境覆盖；测试可自行覆盖这些变量来验证默认路径。没有显式配置目录的 Runtime 测试也不会扫描真实 `~/MuLi` 的扩展分片。

接口依据：[FastMCP 客户端](https://gofastmcp.com/clients/client)、[FastMCP transport](https://gofastmcp.com/clients/transports)、[MCP 工具目录协议](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)。本地锁定 FastMCP 2.13.1 的 `list_tools()` 和 `list_tools_mcp()` 均只读取一页；实现按 MCP SDK 1.21.0 的 `session.list_tools(params=PaginatedRequestParams(cursor=...))` 完整跟随 nextCursor，没有套用当前主分支的自动分页语义。
