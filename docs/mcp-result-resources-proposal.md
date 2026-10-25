# MCP 搜索结果链接：显式发现与原文读取

状态：v2.51.0 已发行，正式旅程和五项独立负控均已通过，合相关旧回归及发布/兼容共484个不同节点；全150源码strict、Ruff/393文件格式检查通过，非作者后审无剩余必须修复P1/P2。四处发布元数据、离线发布验证、精确远端CI及GitHub/PyPI两构件核对已闭合，新增可选测试的远端覆盖边界见[2.51发行审查](review-v2.51.md#发行闭合)。基线 `d04ba0905e0292626403b61452a8dfca5abd3f83` 对应 v2.50 已正式发行，证据见[2.50发行审查](review-v2.50.md)。

主人在已绑定私聊交代“查找资料并读原文”，主助手把检索交给真实队友。已配置 MCP 的搜索工具可能返回合法 `resource_link`，而 `resources/list` 仍为空。队友需要先显式发现这次搜索的链接，再经原 MCP 会话读取短文本，把实际工具证据通过真实队友报告交回主助手；回复沿原私聊来源，Web 保留同一会话记录。

MCP 2025-06-18 的[官方 tools 规范](https://modelcontextprotocol.io/specification/2025-06-18/server/tools#resource-links)明确支持 typed `resource_link`，且它不保证出现在 `resources/list`。原文仍使用同服务器的[resources/read](https://modelcontextprotocol.io/specification/2025-06-18/server/resources#reading-resources)。服务器是否支持读取、资源是否可访问及正文是否符合已有短文本限制，由真实 capabilities 与受控读取结果决定，链接本身不证明这些事实。Context7 本轮返回 Monthly quota reached；根已核对这两份官方一手规范，未更换依赖或连接外部服务。

## 已有接缝与准确缺口

- 实施前 `src/muli/tools/mcp.py::_result` 只保留 text 和 embedded resource 的 text；typed `resource_link` 进入 `omitted:true` 分支，名称与 URI 丢失。`_call` 已有确切连接、独立 MCP generation、active call lease、超时和 receive latch。
- 实施前 `mcp_resources_list` 只观察普通资源目录，read 和 save 都只认普通目录实际展示的引用。`_directory_slice` 已在完整输出预算与期限通过后发 ref/cursor，引用是宿主 opaque token。
- `src/muli/tools/mcp_resources.py` 已有 `ResourceItem`、严格 URI/元数据白名单和完整短文本校验。原 MCP 页数、目录逻辑字节、条目数、URI、接收与输出预算可直接复用。

不另建数据库、通用工具 ledger、后台抓取或模型轮询；不改变主助手协调、reviewer、Attention、disabled tools 和 external 审批边界。首片不接 stdio 资源、不解 URI 为本机路径、不做任意 HTTP 抓取、不自动执行正文或发送文件。

## 最小接口与所有权

生产最多改 `src/muli/tools/mcp.py` 和 `src/muli/tools/mcp_resources.py`。搜索工具的完整正常结果可附 `resource_links_available:true`，仅表示当前调用留下可显式发现的有界链接集合，不直接授予 URI 读取权。没有合格集合时不增加该肯定字段；原普通工具结果形状保持。该标记只是历史观察，后续实际 list/read 的宿主检查才有执行权。

`mcp_resources_list({"server":"search","source":"tool_result"})` 从同一当前连接的最近一次成功工具调用中显式展示链接元数据、opaque ref 和可选本地 cursor。省略 `source` 沿原普通目录行为，可显式使用 `source:"catalog"`；两来源的集合、cursor、ref 分开，不合并普通目录，也不重新发 `resources/list`。返回的 `catalog_complete` 只表示这次工具结果的链接组完整通过预算，不能解释为完整服务器目录。目录展示沿 `_resource_fields` 脱敏，原 SDK URI 只私有保留供确切会话读取；不把显示脱敏值再次解析成请求地址。

随后仍调用 `mcp_resource_read({"server":"search","ref":"已展示引用"})`。宿主按引用的准确来源分派，必须已实际展示才可读，复用原完整短文本、外部风险、期限及 Runtime 完整工具回执合同。读取结果始终 `untrusted:true`；名称、description、annotations、URI、正文均是外部资料，不能成为指令、Skill、权限或工具定义。

本来源只开放文本 read。`mcp_resource_save` 在原远端 RPC 和附件 factory/reservation 之前拒绝 tool_result ref；其 `tool_result:` 前缀加原 Catalog opaque 摘要也让已过期引用无需历史表即可辨认来源，普通 32 位摘要 token 保持。ref/cursor 必须原样使用，不解析或猜造。普通目录显式二进制保存合同不变。HTTP/SSE 与 resources capability 的原门保留，不因工具返回链接就给无读取能力或 stdio 服务器增加入口。

每连接只保留一个有界工具结果链接集合和本次 call token。当前 generation 下实际接纳的新工具调用立即撤销该连接旧集合；失败、取消、无合格链接或截断不会回退到旧集合。B 较新调用失败或取消后，A 较早调用晚到不能重新填回链接。普通资源目录不受这次单来源撤销影响，不增加历史缓存或等待队列。

调用 await 后允许旧代普通文本按原合同完成；发布链接集合前还须同步复核准确 generation、当前连接身份、usable、receive latch、本次 call token 与绝对期限。MCP 发布换代即使复用同一连接也清集合与旧引用；删除、禁用、权限/配置代次变更、退休、关闭及重启同样失效。未发布候选不获引用权，独立 Skills-only 发布不额外撤销原 MCP token。不能让旧调用签发新代可用 ref。

## 整组接纳与资源边界

只从原 SDK 结果的 typed `content` 块提取 `type:"resource_link"`，成功调用才有资格；普通文字中的 URL、`structured_content` 仿造字典、错误结果、sanitize 后投影、preview 和截断结果都不授予链接。先核整组条目数量，再对整组原链接 JSON（含未知字段）计既有目录逻辑字节，严格核 UTF-8、URI 与元数据形状，全部通过后才保留私有 URI 和白名单展示字段；不能先丢未知字段来逃过预算，也不发布合法前缀。重复 URI、超量、坏形状或预算失败均不留下半份集合。

沿用现 `MAX_RESOURCE_ITEMS`、`MAX_RESOURCE_URI_BYTES`、`MCPLimits.max_catalog_bytes` 和原 `max_output_chars`；本地分页继续在完整输出容纳后才登记实际展示项 ref/cursor。原接收门发生在 SDK 解码前，整组逻辑预算发生在解码后，不能互相冒充。结果 marker 也参与原工具完整输出预算；加入 marker 后若只能生成 truncated wrapper，该结果不发布链接集合。仍只有每连接一份集合，不叠加全局缓存、第二套 Settings 或新的总预算。

## 先红正式旅程与后续门控

`tests/test_mcp_result_resources_runtime.py::test_search_resource_link_reaches_explicit_read_and_original_phone` 使用临时 Store、真实 ASGI/OneBot 入口、默认应用生命周期、真实 Runtime/Team、锁定 FastMCP/MCP SDK 和流式 HTTPX MockTransport。模型替身只选择固定工具，不直接写消息、报告、路由或工具回执，不证明自然模型质量。

手机 7 的真实 owner 输入触发主助手 spawn 直接队友；主运行先正常结束，随后放行队友。队友先实际调用普通资源目录并观察空目录，再调用已配置 MCP 搜索。MockTransport 的真实 JSON-RPC 返回合法 typed link；观察器仅委派原 SDK 调用并确认 SDK 已解码 `ResourceLink`，不替换 MCP client。旧实现下队友取得 accepted 完整搜索回执后正常结束，真实报告交回主助手，原私聊 outbox 与 Web 均实际可见，再由外层 `resource_links_available is True` 断言准确命中宿主丢失链接。缺新工具、导入失败、工具错误和超时都不是目标红。

修后正式旅程已验证成功分支：同一队友显式 `source:"tool_result"` 展示 ref，再经原 `mcp_resource_read` 真正读取正文；独立主运行只从真实队友报告得到原文，手机 7 的最终回报恰好一次，Web 可见。普通 `resources/list` 仍只一次且为空，远端搜索一次、原文读取一次，不能通过补进普通目录或替身预先塞正文过关。沿现旅程 16000 上下文字符、6000 工具输出字符、3 个 pending run；主模型至多 3 次、队友至多 5 次请求、每份消息历史至多 32 条，MCP timeout 2 秒、既有有界等待不加长。没有真实模型、账号、外部 MCP、桌面或网络送达实测。

必要负控由独立 `tests/test_mcp_result_links_policy.py` 覆盖宿主合同：普通文字/structured 仿造不授权；tool_result save 零 RPC/零附件预留；同连接换代后旧调用晚返不发布；较新取消后较早调用不复活；工具输出截断和整组目录字节超限不发部分 ref。这些有限替身负控不冒充锁定 SDK 旅程或自然模型端到端质量。

首跑由根实收 1 failed / 4.97 秒，守卫 5.744 秒、134.2 MiB、swap 0：等待主助手安排回复的原 4 秒超时，观测轮次为主 3 / 队友 1，尚无搜索结果，不能记为资源链接目标红。静态核对 `TeamManager.spawn` 及既有正式旅程后确认，它返回真实 agent 对象的 `id`，新夹具误取 `agent_id`；现只将取值改为 `id`，并保存实际已见 run 以便终态后仍能输出至多 1 KiB 的固定 status/exception_type/工具结果类型与 code 诊断，不打印完整消息、路径或错误正文。原等待、轮次、预算和目标断言未放宽。

第二跑由根实收 1 failed / 4.89 秒，守卫 5.655 秒、113.9 MiB：安排回复已通过，停在队友结束的原 4 秒等待，未到搜索目标断言，该等待当时缺诊断。源码确认 agent 的 `completed/failed` 终态合同与既有正式旅程一致，不能改取其它状态充数；另发现 `make_runtime` 在默认 auto 下已创建持久主会话，之后更改 `default_mode` 不会改该会话，手机 ingress 不传 mode，队友继承 auto，external MCP 调用仍需真实主人批准。只于手机输入前沿已有认证会话 PATCH 显式设置 yolo 并断真实返回及队友继承，未手造批准或修改权限实现；给原 worker 等待包同一至多 1 KiB 诊断，附 agent mode/status、report execution_status 与待批准数量。该轮属于前置失败，不能记为功能红；下一轮已实际走通此前门控。

第三跑由根实收准确目标红：1 failed / 0.89 秒，守卫 1.774 秒、113 MiB、swap 0。真实完整搜索、SDK `ResourceLink` 解码、accepted 完整工具回执、队友完成报告及原私聊/Web 前置均通过，外层准确在 `resource_links_available is True` 断言得到 `None`，旧 payload 中 typed link 为 `omitted:true`；不是缺工具、导入错误、审批或等待超时。

获准后实现只改 `tools/mcp.py` 与 `tools/mcp_resources.py`，复用原目录值、完整展示分页及文本 read。整组原 typed 内容计费后仅保留一份有限集合，新调用 token 和最终代次/连接/接收/期限检查防止晚返复活；来源前缀在 save 前拒绝。根实收六项新节点通过：pytest0.96秒，守卫1.782秒、114.6MiB；既有MCP八文件181项通过，pytest3.76秒、守卫4.658秒、169.6MiB；其它六文件131项通过，pytest3.20秒、守卫4.123秒、176.9MiB。三组去重合318个节点，不重复累计单旅程或策略正控。

全150源码strict通过，守卫8.429秒、417.4MiB；全仓Ruff通过（83毫秒、24.6MiB），393文件格式通过（68毫秒、22.1MiB），根仅统一排版两个文件、导入顺序检查无修改。既有MCP回归中的一条Pydantic serializer warning来自故意错误类型用例，未把它改为跳过。A完成两生产的正式非作者后审，未发现剩余必须修复P1/P2。四发布元数据统一2.51.0，七持久格式、依赖和预算保持。根随后实收发布/兼容166项通过，去重合484项；离线锁复核、wheel/sdist构建、发行一致性与隔离安装均通过，精确结果见审查记录。候选十项、发行十三项及两处远端构件元数据核对已闭合；SDK新增测试的本地与远端覆盖范围严格区分，见发行审查。作者未自行执行检查、格式化、Git 或网络。
