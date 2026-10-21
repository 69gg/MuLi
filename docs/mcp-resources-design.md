# MCP 普通资源的按需列表与读取

状态：实现、本地回归、独立后审与根整合已完成，精确远端 CI 待验，未连接外部 MCP 服务器。实现基线为签名 `423596819405cae37311b278cfc30c46454f811b`，已包含接收预算、故障通知与空游标兼容修复；根整合和发行结果见[2.36 审查](review-v2.36.md)。本片不扩展统一错误通知。

## 首片选择

为队友提供两个固定的宿主工具：`mcp_resources_list(server, cursor?)`、`mcp_resource_read(server, ref)`。只向已显式启用、初始化声明资源能力且具有接收 guard 的 HTTP/SSE 连接发送请求。按需发现普通资源，读取已发现引用的完整短文本；不在连接准备、提示词或后台通知中预取目录与正文。

第一片不接 URI 模板、补全、订阅、服务端 prompts、任意 URI 直读、blob 解码/保存、HTTP 下载或本机文件路径解释。stdio 工具原路径保持；它没有解码前帧预算，因此资源工具暂不接纳 stdio。文档不能把服务器宣称只读等价为本地 `risk=read`。

两种较小替代没有选：直接返回一页 SDK 结果会把跨调用累计预算与不透明游标状态交给调用者；反复 `resources/read` 后切片则没有服务端 offset/修订合同，不能声称读到了同一份正文。选择一次完整有界目录观察加本地元数据分页，正文一次完整交付或明确失败，不新增正文缓存。

## 已核实接口与实施前接缝

先用 Context7 查公开接口，再读根已有环境中的 FastMCP **2.13.1**、MCP **1.21.0** 源码。Context7 返回的 main/v2 自动分页、任务和新订阅接口不适用于这些锁定版本。下表 MuLi 行号与无条件工具发现描述属于实施前 `4235968` 基线；本片的实际接线和验证另列于末节。

| 证据 | 实际行为与接线要求 |
| --- | --- |
| [FastMCP 2.13.1 client.py:589–614](https://github.com/jlowin/fastmcp/blob/v2.13.1/src/fastmcp/client/client.py#L589) | `list_resources_mcp()` 无 cursor，直接调用一次 session；不能用它冒称完整目录。 |
| [SDK 1.21 session.py:180、236–262](https://github.com/modelcontextprotocol/python-sdk/blob/v1.21.0/src/mcp/client/session.py#L180) | 初始化后可读 `get_server_capabilities()`；分页使用 `session.list_resources(params=PaginatedRequestParams(cursor=...))`。 |
| [SDK session.py:302–311](https://github.com/modelcontextprotocol/python-sdk/blob/v1.21.0/src/mcp/client/session.py#L302)、[FastMCP client.py:647–667](https://github.com/jlowin/fastmcp/blob/v2.13.1/src/fastmcp/client/client.py#L647) | read 只发送 URI，返回完整 `ReadResourceResult`；没有 offset、范围或内容修订条件。适配器直接沿 session 发请求，不使用会记录 URI 的高层调试入口。 |
| [SDK types.py:288、435–554](https://github.com/modelcontextprotocol/python-sdk/blob/v1.21.0/src/mcp/types.py#L288) | 资源能力对象允许空对象；目录有 URI/元数据，读取内容为 text 或 blob，且 SDK 模型允许额外字段。不能把整个 model dump 当作可信宿主输出。 |
| `src/muli/tools/mcp.py:148、436、596–610` | 已有完整工具目录分页，但新连接无条件 `list_tools`。资源专用服务器需要按实际初始化的 tools capability 跳过不支持的 `tools/list`。 |
| `src/muli/tools/mcp.py:819–857、669–689` | 最后同步 generation/连接/latch 门、在途登记、期限和退休已有实现；资源操作沿同一所有者，不建第二套连接。 |
| `src/muli/runtime.py:1631–1667、1750–1785`、`src/muli/permissions.py:25–67` | 工具统一经过审批与最后请求门；`preserve_output` 让完整结果进入上下文，放不下时明确失败。不能仅在资源 handler 内核输出预算。 |

协议依据：[2025-06-18 resources](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)、[pagination](https://modelcontextprotocol.io/specification/2025-06-18/server/utilities/pagination)。资源能力以 `resources is not None` 判断，`resources={}` 合法，subscribe/listChanged 都不是必需条件。SDK 1.21 的 `Cursor=str`、`nextCursor: Cursor | None` 没有非空约束；缺失或 `None` 才终止，空字符串必须原样发给下一页。游标不能解析、修改或跨 session 持久化。

## 能力、工具与兼容

真实适配器只投影初始化结果中的 tools/resources 是否存在，不复制未知 capability 字段。未初始化不当成空目录。声明 resources 而没有 tools 的服务器不发 `tools/list`；合法空工具和空资源目录均可成功。capability 是接口声明，不是数据访问许可或健康证明。

保留当前 `MCPClient` 的工具调用契约。新增独立的可选资源适配接口，旧 tools-only fake/client 没有它时保持旧目录行为、不生成资源工具，不能用默认 capability 伪造支持。真实 FastMCP 适配器必须有明确初始化证明，不能套用该兼容回退。

全体资源服务器共用两个宿主工具，不为每个资源或服务器增加 schema。schema 的 server 枚举和处理函数捕获同一 generation 的确切连接映射；绝不在旧 handler 内按名字查找新端点。仅有 stdio 或 tools-only 服务器时不增加这两个工具。宿主名称与已有远端命名空间分开，仍走实际 registry 冲突检查。原 `max_mcp_tools` 保持远端工具数语义；固定两个宿主工具不是服务器目录条目，仍受 Runtime 的总上下文预算约束。

两个工具均为 `risk=external`，不采信服务器只读声明；main/reviewer/attention/disabled 与最后无工具轮沿原过滤。`preserve_output=True` 是宿主标记，不进入模型 schema。引用只是当前已发现项的定位信息，不能绕过审批、维护、暂停或调用代次门。

## 一次完整目录观察

无 cursor 的 list 是显式刷新：在连接原 timeout 的同一个绝对期限内跟随所有服务器页，先完整验证，再原子替换该服务器当前内存目录。按页重新给完整 timeout 不被允许。它只读取元数据，不取资源正文。

- 沿用当前有效 `MCPLimits.max_pages`（默认 8）作为本次资源目录页数上限、`max_catalog_bytes`（默认 256 KiB）作为本次全部目录页逻辑 JSON 总字节上限；分别计费，不冒充 wire 上限或原工具目录合计。新增说明这两个既有上限也限制显式资源目录操作，不添加 Settings 字段或新策略对象。
- 使用命名协议常量 `MAX_RESOURCE_ITEMS=256`、`MAX_RESOURCE_URI_BYTES=4096` 和 `MAX_RESOURCE_CONTENT_BLOCKS=16`，分别限制目录实际条目遍历、URI 和读取块数。这是首片协议边界，不接受环境变量扩大，不建立另一套配置平台；更小的既有目录/输出/接收预算先到即止。
- 在逐项处理前核实际列表长度；所有条目与页均计数，包括空页、重复 URI、未知字段和随后不展示的元数据。每页整个 SDK 结果（包含 extra/_meta）参与总逻辑 JSON 字节计费，之后仅保留允许字段；序列化失败归一固定码。该计费发生在 SDK 解码后，不能取代接收 guard。
- 重复 URI、重复 nextCursor、缺字段、类型错误、超页/条目/总字节预算均整体失败。空字符串是合法游标，第二次出现同一空字符串仍算分页停滞。达到上限但 `nextCursor is not None`，不能把前缀标成完整；空目录只有在实际资源列表为空且 `nextCursor is None` 时才完整成功。
- 名称、title、description、mimeType、size 仅作为有界服务端元数据。未知字段不传播或求值，不据此执行、抓取、生成工具或扩大权限；已被 SDK 接受的额外元数据不因名称新颖而单独拒绝。size 是估计，不是内容已读或安全证明。

一次失败、取消或超限不发布部分目录，也不冒用旧目录作为这次成功结果；旧完整目录可保留其原身份。接收 latch 已失败时旧目录同样不能再授权 RPC。

## 本地分页与引用寿命

每个当前连接最多保存一份完成的目录（仅已预算的投影元数据、确切 SDK URI 与有限引用表），每份最多 256 项/既有目录字节上限；当前配置最多 32 个服务器。新 candidate 不扫描资源目录，不复制这些缓存，旧 generation 不另留一套可用缓存。每连接最多一个目录刷新在途，第二个立即明确 busy，不新增等待队列。

目录得到宿主随机 catalog ID。仅为实际返回的条目发放 opaque ref，并返回有界宿主 cursor；continuation 只从这一目录切出完整条目，不发送 MCP 请求。cursor 绑定 server、当前 generation、catalog ID 与合法位置；只接受本进程实际发出的值，不允许客户端提交服务器原游标或自报累计页数。游标和引用总数最多与当前条目数同阶，不能因重复翻页累计增长。

每一页在旧 generation 的脱敏之后计算完整 JSON（包括所有 ref/cursor/状态）；尽可能放入原 `max_output_chars`，单条或空页包装都放不下则明确 output_budget，不截 URI、不静默跳过。续读不再次从服务端抓取，也不把本地 offset 发给服务端。

成功刷新替换旧目录时，旧 cursor/ref 失效；MCP 发布新 generation、删除/关闭、manager 关闭或进程重启时全部失效，即使连接被复用。Skills-only 独立发布不改变 MCP token，因此不额外失效。生命周期以上述当前代和最近目录为界，没有无限历史缓存或跨进程持久化。

目录是在若干响应中观察到的完整有界列表，不是服务端原子快照或正文修订。服务端在分页中变化但没有产生可检出的重复/异常时，宿主无法证明一致性；listChanged 不能提供缺失的 revision。首片不订阅或自动重取，需新目录由下一次明确 list 请求触发。

## 引用读取与非可信内容

read 只接受当前目录实际已展示项的 ref，并核 server、generation 和连接。SDK 已解析的 URI 由内部引用取出，保持 SDK 的 URI 语义；不能把模型传入路径、URL、旧 session 游标或错误返回里的 URI 升格成可读引用。URI 可用 file/https/自定义 scheme，但只回传给已经批准的原 MCP session；不调用本地 open/resolve、HTTP fetch 或 URI 模板展开。

读取仅一次请求，同一连接 timeout、不自动重试。先核 `contents` 长度，再逐项投影 text；允许标准的一次多文本/子资源响应，但其中的 URI 仍是数据，不签发新的可读引用。发现 blob、text/blob 混合形状或无法证明的内容类型，整个读取明确 unsupported/invalid，不返回伪装完整的文本子集，不解 base64。合法空文本与失败明确区分。

允许字段的字符串值与文本按旧 generation 进行已知密钥脱敏，宿主白名单字段名保持不变，即使密钥恰好为 `text`、`name` 或 `uri`。URI 的显示值与实际内部引用分开；不对 URI 用省略号截断后再请求。结果包含 `untrusted=true`，服务器 description/text/_meta/annotations 不成为系统指令、Skill、工具定义或权限。服务器自身能否真正只读、资源 ACL 是否正确仍属于配置服务器的信任边界，宿主 external 审批不能证明它。

完整读取结果必须同时满足当前接收预算、块数、输出字符预算与 Runtime 完整工具批次预算。文本超大时返回固定 output_budget；不制作任意正文 preview、自动读取其它资源或重复 read 做分页。`preserve_output` 经过实际 Runtime 打包后必须仍为 accepted/full；若完整批次无法进入下一模型请求，应明确停止，而不是只保护 handler 返回值。旧页后续可能沿既有上下文淘汰规则节选，不承诺永远保留全文。

## 调用租约与失败

资源操作复用确切 connection、generation、receive latch 和 active_calls/idle 所有权。最后同步门到登记 active_calls 之间无 await。拒绝旧 handler、旧引用或坏 latch 时是 not_executed；进入 SDK 后的错误、超时、取消不能被改称未发送，更不能自动重放，即使名义上是资源读取。

已接纳 read 允许在原连接完成，使用旧代脱敏与输出预算；新代不会接管它。已接纳目录观察也持原连接到收尾，但最后若 generation/目录发布条件已经失效，不发布缓存、不签发可用 ref，返回明确 stale 结果而非 not_executed。所有页共用一次期限。close/取消沿现有 SDK settle 与退休任务，不引入另一清理器、不强杀进程、不创建第三套连接。

目录协议/内容错误只使本次资源操作失败，不把 tools 能力或已发布工具假称坏；接收 guard 的连接级失败仍沿原 latch 同时关闭工具与资源的新派发。公开状态只含支持情况与固定原因，不回显原 cursor、URI、正文、头或远端异常。统一错误通知由独立片负责，此处不触发 catalog-changed 重连。

## 实现范围与门控

生产改动集中 `tools/mcp.py` 的真实适配器、资源状态/两个宿主工具、共用调用租约与发布失效；小 helper `tools/mcp_resources.py` 只容纳有界投影/分页值，不建 transport 或新生命周期。Runtime 复用已有 `preserve_output`，无需改动其实现。不改 Settings、DDL、policy 对象、管理 plan、模型 codec 或通知来源。

专用新测试先用旧实现证明“没有按需资源入口/资源专用服务器错误请求 tools/list”，再验证：

1. resources 缺失与 `{}`、tools-only 旧 fake、资源专用服务器、真正空目录；严格 extra 投影及未知元数据计费。
2. SDK 原 cursor（含空字符串）原样传递、仅 None 终结、重复 URI/游标（含重复空串）、空页带 cursor、最后一页刚好/超预算；失败或取消不发布前缀，不因多页续期。
3. 超过单模型页但未超总目录预算的列表，以宿主 cursor 完整拼回；每个 ID/URI 不变、不跳项。引用数有界，重复翻页不增长；刷新/换代/关闭使旧 ref 失效，Skills-only 不误撤销。
4. 真 Runtime/假 provider 的完整页与多块文本，经实际请求装配和三种 codec 保持 full；超批次预算明确失败且不发下一请求，宿主标记不进入 wire schema。
5. URI 只能来自已展示引用；file/custom scheme 不产生本机读或另一次 HTTP，正文指令不改变权限；blob/混合/未知形状明确拒绝，错误不返回假空文本。
6. 真实事件门控的审批等待后撤销、在途完成/退休、目录中途换代不发可用 ref、取消/超时不重放、旧秘密脱敏、receive latch 同时封锁 tool/resource。
7. 可选 MCP extras job 用锁定 SDK/FastMCP 与流式 MockTransport 验证实际 resources/list/read；普通无 extras 矩阵仍收集纯值、租约与 Runtime 测试。外部网络、真实 stdio 和下载均硬阻断。

首片不承诺服务端跨页或正文快照、任意恶意 transport 的整体 RSS、进程重启后旧引用恢复、模板/二进制兼容、旧 slot 执行资源防护或实际外部服务器验收。现有[研究](mcp-resources-research.md)保留其当时证据；[接收预算](mcp-wire-budget-design.md)是前置边界，不是资源功能已经完成的证明。

## 工具目录空游标的独立兼容修复

现有工具目录曾以 `not next_cursor` 拒绝合法空串。假客户端返回两页、首个游标为空时，旧实现稳定返回零工具；仅移除非空判断后完整发现两项。新增四个唯一 case，连同原分页/适配器回归共 **11 项唯一测试**通过（先跑的 5 项已包含其中）：空串原样交下一页及锁定 SDK 参数，重复空串仍停滞，页数、目录预算、失败保旧及候选关闭不变。

两份 Python 的 Ruff/format 与单源码 mypy 通过；pytest 峰 66.6 MiB、mypy 峰 177.8 MiB，swap 0。全部经根守卫、每条 30 秒串行执行，未运行真实 in-process MCP、stdio、网络或服务。这次历史验证只验收工具目录的游标谓词；资源首片的独立结果如下。

## 资源首片本地验证

资源专用假服务器的旧实现反例稳定失败：启动仍请求不支持的 `tools/list`，最终没有资源入口（1 failed，0.14 秒，峰 59.7 MiB）。最小接线后，三份新增测试共 **53 项**通过（1.99 秒，峰 110.9 MiB），没有 skip。它们包括锁定 FastMCP/SDK 的实际 HTTP、SSE 会话和流式 HTTPX MockTransport、真实 Runtime/假 provider 的三种 codec、完整页与读取、超上下文停止、外部审批、过滤、代次和旧连接所有权。未连接 socket 或实际 MCP 服务器。

独立审查提出短密钥与固定字段同名的反例：旧投影将 `text` 键一起脱敏，1 failed（0.15 秒，峰 61.2 MiB）。新增三参数回归将 `text/name/uri` 的宿主键、原始 URI 请求和正文值脱敏同时锁定；修复仅限新资源白名单投影，原通用工具结果脱敏不变。这三项已包含在上述 53 项中，首轮 50 项不重复计数。

既有回归分成三组：工具目录、代次、接收和策略 **93 项**（0.81 秒）；活跃换代、接收集成/通知、Extensions、Skills 活跃发布/完整资源页与 watcher **143 项**（3.61 秒）；Runtime、系统错误来源和管理 **55 项**（3.85 秒）。三组互不重叠，合计 **291 项**；加新增共 **344 项唯一测试、17 个测试文件**。既有真实 in-process MCP 用例显式 deselect 一项，不计通过数；所有执行均为 fake 或流式 MockTransport，无真实 stdio、外部网络、服务或原生操作。策略损坏样本触发的一个 Pydantic 序列化警告属于既有回归，不影响断言。

五份修改/新增 Python 的 Ruff 与格式检查、两份生产源码的严格 mypy 均通过。整轮 pytest 峰 **242.4 MiB**，mypy 峰 **173.7 MiB**，swap 0；每条检查经根 `check_local.py --seconds 30` 串行执行，沿用 1 GiB 单次、2 GiB 聚合和单 CPU 上限。未安装依赖。可选 SDK 用例需要已有 extras；普通无 extras 环境仍可收集纯假客户端与 Runtime 测试，远端选择由根整合 CI。

另一个纯探针核实了非法 Unicode 假设：锁定 SDK 的真实 `JSONRPCMessage.model_validate_json` 在接收入口即以 `json_invalid` 拒绝 lone surrogate，因此未据该假设改动产品或通用 codec。此探针不计入 344 项回归。当前结果不代替根整合、无 extras 类型检查、跨平台 CI 或用户服务器验收。
