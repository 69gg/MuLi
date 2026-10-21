# 可选 MCP 工具与资源

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

## 目录发现与活跃任务热重载

除原有内联 `mcp` 外，运行时读取 `MULI_HOME/mcp.d/*.json`，默认是 `~/MuLi/mcp.d/`。`extensions.mcp_directory` 可替换目录，空字符串使用默认路径。只扫描一级 `.json` 普通文件，不递归、不跟随分片符号链接。每个分片仍使用 `{"mcpServers": {...}}`；**目录分片省略 enabled 时默认启用**，显式 false 或 `disabled:true` 停用。原有内联配置省略 enabled 仍停用。

同一 JSON 对象的重复键、内联与分片之间或不同分片之间的同名服务器都会拒绝候选，状态说明冲突来源；没有按文件名覆盖的隐式优先级。读取前后核对已打开文件及路径身份，普通并发修改返回 `source_changed`，等待下一次稳定变更。该检查不构成对恶意本机程序的文件系统沙箱。

身份快照取路径的 `lstat()`；路径与打开句柄之间核对设备号、inode、大小和修改时间，句柄读取前后、路径读取前后各自额外核对 `ctime`。[Windows 的 `DirEntry.stat()`](https://docs.python.org/3.13/library/os.html#os.DirEntry.stat) 会把设备号与 inode 设为零，不能直接拿其缓存结果与 `fstat()` 比较；[CPython 的已知差异](https://github.com/python/cpython/issues/157671)还会让路径查询的 `ctime` 表示创建时间、句柄查询表示元数据变更时间，因此不跨 API 比较该字段。跨平台回归模拟零字段与不同但稳定的 `ctime`，并确认同一 API 读取期间的 `ctime` 变化仍被拒绝；不按平台跳过，也不移除非阻塞、符号链接和源变化核验。

初次启动保留 best-effort：失败来源或服务器记入状态，核心助手仍可启动，合法零工具服务器属于成功。后续变更必须通过全部启用服务器的连接、完整分页、名称、参数 schema 和总预算验证，才会发布整代；任一失败都保留当前工具、连接和 Skills 代。不会把前几页或前 100 个工具截断后当作完整目录成功。

分页游标按[协议](https://modelcontextprotocol.io/specification/2025-06-18/server/utilities/pagination)视为不透明字符串，包含合法空串，原样交给下一页；只有缺失或 `None` 结束。重复游标（含重复空串）仍明确停滞，页数及目录预算不变。

首次准备也核对读取前和发布前的同一来源指纹；等待 MCP 或 Skills 时发生编辑，会丢弃旧候选并让监听器重新准备，不能把新文件指纹记到旧工具代上。未发布的失败或取消清除观察状态，完整配置重建后的临时扫描错误恢复为原指纹时也能重试。坏 Skills 来源不会阻止仍可用的 MCP 配置与核心助手启动；失败状态不冒充目录已成功加载。详见[生命周期回归](extension-lifecycle-review.md)。

发现变化或收到 `notifications/tools/list_changed` 后先合并为一个候选请求。纯扩展候选可以在主会话、队友和工作流运行期间准备并发布；配置锁仍串行化准备、来源复核、保存和发布，最后复核到整组发布之间没有 await。未变服务器复用原连接；只为新增或改变的 HTTP/SSE 服务器建立候选连接。工具处理函数始终绑定它验证过的连接，发布时只替换 MCP 工具子集，不替换内置工具或其它 provider。

每轮实际模型请求开始前取得当前工具表，本轮 schema 与返回调用查同一份表。实际 MCP 发布会撤销旧代尚未进入 handler 的调用，包含仍在模型流、审批等待或同批前序工具等待中的调用；按原调用 ID 返回 `status:not_executed, code:generation_changed`，不会用旧名称投到新端点，也不自动重试。已经进入 handler 并登记为在途的调用保留原连接完成，结果继续使用旧代的密钥脱敏与输出预算；外部超时、取消和未知结果不被改称未执行。下一模型轮才使用新版 schema，主会话、reviewer、系统注意事项、禁用工具及最后无工具回合的限制保持不变。

仅 Skills 文档的自主目录变化继续沿[独立 Skills 发布路径](skills-live-design.md)生效；它不准备、重连、发布或退休 MCP，也不撤销已取得的 MCP handler。最后完整接受的 MCP 来源/配置/预算必须不变，且不能有未履行的手动重载或 catalog refresh；其它纯扩展变化走完整候选。该次 `last_apply` 明示 `scope:skills`、`mcp:unchanged`，扩展代次推进不代表 MCP 已重新连接。

已有 stdio 服务器的配置变化保守返回 `restart_required`，保持旧进程和旧配置；不会先关闭旧进程来试运行候选。删除服务器发布后先撤销新调用，已接纳调用结束后才关闭旧连接。要改变这类 stdio 配置，需要在停止或重启进程时应用源配置；窄管理接口不会提前保存无法发布的候选。

`PATCH /api/settings` 和 `self_configure` 的纯 `mcp`、`skills`、`extensions` 修改使用同一窄入口：先准备并验证全部候选，再原子保存配置，随后在没有 await 的步骤发布工具和 Skills 快照。保存失败或候选取消不发布，旧配置和旧 handler 继续可用。轮到的纯扩展计划无需等全局空闲；已有管理计划时仍须先完成或撤销它，纯扩展不越队。混合修改模型、渠道等其它配置仍走原有空闲与整体 reconfigure，暂不提供同等的候选保旧承诺。

发布后旧连接先等待已接纳调用归零，再由管理器强持有的退休任务关闭；发布与配置锁不等待这些调用或关闭完成。至多当前与一个候选或待退休连接集合。仍有待回收连接时，目录请求合并排队，PATCH 返回 `409 generation_draining` 且不保存，管理计划保持 pending；不创建第三套连接。真正关闭失败单独记为 retirement error，新代仍已生效，不声称回滚成功；失败引用继续保留，后续准备说明需要重启，由 shutdown 再尝试强制关闭客户端，不强杀进程、不重试工具执行。调用者的重复取消不会取消已拥有的清理任务。在途调用已有执行期限，归零后的关闭沿原 timeout；不承诺任意 SDK 都可在固定总时长内回收。

`GET /api/extensions`（以及 `/api/status`、`/api/management`、`self_status` 中的 extensions）返回代号、phase、指纹、分片来源、固定错误码、Skills 计数和关闭错误。`last_apply` 分别显示配置 unchanged/saved/save_failed、是否 published、旧连接退休 unneeded/draining/pending/complete/failed；`draining` 是仍有已接纳调用的旧连接数，`cleanup_pending` 包含待关闭和关闭失败，排队原因会区分回收等待与维护门。不会回显请求头、环境变量、URL、远端异常正文或 Skill 正文。`POST /api/extensions/reload` 接收 `{}`，返回 queued 状态，要求显式重查当前服务器目录；它不表示已换代。

排队原因取 watcher 已观察到的状态，查询本身不重新读取维护门。局部 Skills 候选失效或准入暂阻时，尚未消费的请求和来源保留到正常下一观察轮；新请求或显式唤醒仍可提前触发。目录观察失败时也不在同一轮再次准备候选。

默认每 2 秒轻扫描、500 毫秒合并变更，最多一个准备任务与一个 dirty 标记；相同失败指纹不会定时重连，只有源变化、新通知或显式 reload 才重试发现。工具执行本身仍不自动重试。关闭 watcher 后的迟到服务器通知不会恢复已删除连接。

目录与候选预算集中在 `Settings.extensions`，均有类型和上下限校验：

| 字段 | 默认值 | 含义 |
| --- | ---: | --- |
| max_entries | 256 | 目录一级条目总数 |
| max_mcp_fragments | 32 | JSON 分片数 |
| max_mcp_fragment_bytes | 65536 | 单分片源字节 |
| max_mcp_source_bytes | 262144 | 分片源总字节 |
| max_mcp_pages | 8 | 每服务器完整工具目录、每次显式资源目录各自的页数 |
| max_mcp_tools | 100 | 所有启用服务器的工具总数 |
| max_mcp_catalog_bytes | 262144 | 工具名、描述和参数 schema 总 JSON 字节；每次资源目录另按完整页 JSON 合计计费 |
| candidate_timeout_seconds | 30 | 整个候选准备时限；失败后独立清理另受连接时限约束 |
| scan_interval_seconds | 2 | 轻扫描间隔 |
| debounce_seconds | 0.5 | 变更合并窗口 |
| watch_enabled | true | 自动目录扫描；显式 reload 仍可用 |

服务器合计仍最多 32 个。Skills 的读取、目录和活跃正文预算独立放在 `Settings.skills`。目录预算是在 MCP SDK 解码单页后核对；下述 HTTP/SSE 接收策略在解码前独立检查 body/event，两者不能互相替代。没有跨进程持久化上一可用工具代，不替换已接纳调用的实际连接，也不支持任意 Python 插件热卸载。

## HTTP/SSE 接收策略

`self_mcp_receive_configure({"max_message_bytes":1048576})` 完整替换 HTTP/SSE 单条接收上限，沿用工具写权限；也可使用已认证的 `GET/PUT /api/management/mcp-receive-policy`。默认 1 MiB，允许 64 KiB–4 MiB 的整数，省略字段恢复默认。这是现有 Store 内的独立小策略，不属于主 Settings 或 `self_configure` 的补丁，不改变旧控制器读取的配置 JSON。损坏、未知版本或超预算的现存策略明确失败，不能静默覆盖为默认值。

保存只请求原扩展重载，不表示已生效。`self_config.mcp_receive_policy`、该 GET API 及扩展状态的 `receive_policy` 返回 `desired/effective/pending/error`；`effective=null` 表示尚无已发布的 MCP 代，而不是无限额。限额绑定到具体 HTTP/SSE 连接，即使服务器配置未变，修改限额也需准备新连接并发布代次；旧在途调用保留旧限额到完成，退休结束前不建第三套。候选失败保留旧连接和旧有效值，Skills-only 路径不能把策略请求当作已经完成。stdio 不因这项预算而重启，也不获得本片的字节防护。

普通 JSON、错误响应、重定向和其它非流式 body 按完整原字节计费。成功 SSE 长连接按事件计费，包含注释、未知字段和分隔空行；CR、LF、CRLF 与跨块边界保持同一协议含义，未终止长行也不能无限积累。长期 GET 的累计 body 不受单事件大小限制。锁定 SDK 的旧 SSE transport 使用普通 POST 接收响应，因此该 POST 即使声明 SSE 也必须服从总 body 上限；streamable HTTP 的流式 POST 才能按事件重置。SSE 仅接纳 UTF-8。请求使用 `Accept-Encoding: identity`，非 identity 压缩响应在 HTTPX 解压前拒绝；不通过解压后截断掩盖膨胀。

首次接收失败同步锁存固定原因，连接立即禁止新调用。后台 GET 的 SDK 异常可能被吞掉，固定原因仍可从 `self_status.extensions.mcp`、`/api/extensions` 的 `mcp` 或 `/api/status.mcp` 查询；外层 `phase=loaded` 只表示曾发布成功，不保证每条连接仍可用。尚未进入 handler 的调用返回 `not_executed` 和固定失败码；已经进入 SDK 的调用保持失败/未知语义，不重发。

当前已发布 HTTP/SSE 连接的首次接收失败另以 `mcp_connection` 进入[统一系统事项](mcp-receive-notification-design.md)，受现有采集/通知级别、Attention 总开关与有界队列控制。它是独立连接故障，首次持久化时冻结默认或有效显式记忆路由，不冒用当前请求来源或 `extensions_watch`。前台工具回执可能同时可见，不表示远端执行两次。候选、退休连接、stdio、取消及 Runtime 关闭期间不新增该通知；不自动重连、重放或授予 MCP 操作权限。队满、写失败和关机可能丢通知，固定 latch/status 仍可按需查。旧 slot 不具有这项新来源，回退期间不承诺补发。

预算约束的是交给 HTTPX/SDK 解码器的 body，不保证底层 transport 已分配的单个 chunk、全部连接合计 RSS 或无限小事件的速率。旧 slot 会保留但不执行这份私有策略，回退后不能声称仍受此防护。实现、锁定依赖与验证见[接收预算设计](mcp-wire-budget-design.md)。

## 按需普通资源

已启用的 HTTP/SSE 服务器在初始化中声明 `resources`（空对象 `{}` 也有效）后，提供两个固定宿主工具。`mcp_resources_list({"server":"remote_tools"})` 返回完整观察过的目录中的一页元数据、该页每项的 `ref` 和可选 `next_cursor`。将 `next_cursor` 原样作为 `cursor` 续页；续页只访问当前内存目录，不再向服务器请求。无 cursor 的调用表示重新完整观察目录。`mcp_resource_read({"server":"remote_tools","ref":"已展示引用"})` 只读取已展示项的完整短文本；不能直接提交 URI 或本机路径。

连接准备、提示词和后台不预抓资源目录或正文。仅声明资源的服务器不请求 `tools/list`；原 tools-only 客户端保持原行为。两个宿主工具都为 `risk=external`，主协调会话、reviewer、Attention 和禁用工具过滤保持，不能借服务器“只读”声明绕过审批。它们不占远端 `max_mcp_tools` 条目，仍受实际模型上下文预算限制。

一次目录观察在原连接 timeout 的同一绝对期限内完成全部服务器页。页数和完整页 JSON 总字节分别沿用上表预算，未知字段也计费；另限 256 项、URI 的 UTF-8 长度 4096 字节。空服务器游标合法，只有缺失或 `None` 结束；重复游标或 URI、超限、不完整均明确失败，不发布目录前缀。`catalog_complete=true` 表示服务器目录已完整观察，不表示这一宿主页展示了全部条目。只投影 name/URI/title/description/mimeType/size，未知扩展字段不传给模型。

每连接仅一份完成目录和一个在途刷新，第二个刷新返回 `resource_catalog_busy`。只有实际展示项的 ref 可读；重复本地翻页不无限增长引用表。成功刷新、MCP 换代（含连接复用）、删除/关闭和重启会使旧 ref/cursor 失效。Skills-only 独立发布不改变 MCP token，不额外撤销资源引用。目录不是服务器原子快照，也不证明正文有同一修订。

read 一次读取至多 16 个完整文本块。file/https/自定义 URI 只回传给原 MCP session，不在本机解释、打开、解析路径或额外 HTTP 抓取。blob、混合 text/blob 和无法证明的内容形状整体拒绝，不解码二进制、不返回文本子集。结果带 `untrusted=true`，元数据和正文都不能作为指令、Skill 或权限。URI 的显示值可脱敏，内部请求仍使用原 SDK URI，不请求被截短或脱敏后的地址。

元数据和文本的字符串值按旧代密钥脱敏，固定字段名和宿主引用保持；随后核完整 JSON 字符预算，不能容纳时明确 `resource_output_budget_exceeded`，不制作正文 preview。最新工具批次经 Runtime 保持完整；完整批次放不进下一请求时停止运行，不继续请求模型。较早批次仍遵循原上下文淘汰规则。取消、超时和已进入 SDK 的未知结果不自动重放；换代前已接纳的 read 可在旧连接完成，迟到目录不签发新代可用引用。接收 latch 同时封锁工具与资源。

本片不接 stdio 资源、模板、订阅或后台抓取，不新增配置/API/通知源。状态中 `resources_supported` 和固定 `resource_error_code` 不回显 URI、原服务器游标、正文或异常。实现与验收边界见[资源设计](mcp-resources-design.md)。

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

远端工具的文本与结构化结果遵守 `max_output_chars`，超限返回 `truncated:true` 和受限 preview。图片、音频、二进制资源只保留类型元数据，不把 base64 数据塞入模型上下文或自动下载媒体。服务器错误结果正文不回显；已配置 env/header 值以及 Bearer token 在正常文本中也会遮盖。服务器日志和 stdio stderr 不转发给模型或应用日志。上述工具结果的 preview 不适用于完整资源读取。

## 验证边界

`tests/test_mcp.py` 验证默认禁用、不自动下载、持久连接、命名隔离、统一外部风险、配置传递、错误遮盖、调用时限、取消和结果限制，以及完整分页、停滞游标、候选预算、失败保旧、复用连接归属、stdio 需重启、旧 handler 撤销和退休关闭失败。可选依赖存在时，原有进程内 FastMCP 用例验证完整协议与关闭生命周期。`tests/test_extensions.py` 覆盖目录规则、分片预算、源变化、配置锁、保存失败、准备期限、相同失败不循环重连和窄管理接线；真实队友报告在候选准备中唤醒离线主模型，也不阻止纯扩展发布。专用代次回归使用真实 Runtime/Team 与假 provider/client，覆盖旧批配对、审批撤销、下一轮 schema、在途回收、旧秘密及候选取消；准确结果与旧实现反例见[代次发布记录](mcp-live-generation-design.md)。没有连接用户的实际外部服务器，也没有下载服务器包。

`tests/conftest.py` 为每个测试隔离临时 `MULI_HOME`，并移除宿主的配置/数据目录环境覆盖；测试可自行覆盖这些变量来验证默认路径。没有显式配置目录的 Runtime 测试也不会扫描真实 `~/MuLi` 的扩展分片。

接收片新增三文件 68 项回归：真实 HTTPX 流式 MockTransport 验证解码前预算，锁定 SDK/FastMCP 验证后台 GET 吞异常后仍封锁派发与 streamable POST 事件计费；临时 Store/Runtime 验证完整替换、事务、代次与管理入口。含既有关联组共 15 文件 330 项唯一用例通过，准确分组与资源结果见[接收验证记录](mcp-wire-budget-design.md#本片验证)。可选依赖缺失时仅实际 SSE decoder/SDK 用例按需跳过，JSON/Store/Runtime 替身用例仍可收集；原 MCP extras CI 明确运行三份新文件。未连接实际外部 MCP 或运行 MCP stdio 程序。

接口依据：[FastMCP 客户端](https://gofastmcp.com/clients/client)、[FastMCP transport](https://gofastmcp.com/clients/transports)、[MCP 工具目录协议](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)。本地锁定 FastMCP 2.13.1 的 `list_tools()` 和 `list_tools_mcp()` 均只读取一页；实现按 MCP SDK 1.21.0 的 `session.list_tools(params=PaginatedRequestParams(cursor=...))` 完整跟随 nextCursor，没有套用当前主分支的自动分页语义。
