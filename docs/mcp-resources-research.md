# MCP 资源能力的后续调研

状态：资源能力仍未实现，也未连接外部服务器、安装依赖或下载资源。本文件保留初次调研时的源码与边界；活跃任务换代已完成，HTTP/SSE 接收保护已在独立切片提交，实际发行证据仍归各片记录。

已形成[普通资源按需列表/读取设计](mcp-resources-design.md)：首片拟限已启用且有接收 guard 的 HTTP/SSE，完整有界目录加有限内存分页，只读取已发现引用的完整短文本。设计方向经根静态评审认可；先等待统一通知片整合，再从其基线实施，不将本研究或设计当成功能交付。

## 已核实的差异

通过 Context7 查接口后，再核精确 [FastMCP 2.13.1 源码](https://github.com/jlowin/fastmcp/blob/v2.13.1/src/fastmcp/client/client.py)：
`list_resources_mcp()`、`list_resource_templates_mcp()` 的签名没有 cursor 参数，直接
调用底层 session；不能套用文档主分支的分页签名。后续如实现完整列表，应沿现有工具
目录的底层 SDK 分页方式，单独核对锁定 SDK 的方法，而不是假称高层已经取完全部页面。
`read_resource_mcp(uri)` 返回完整协议结果，`read_resource(uri)` 仅取其中 contents。

[MCP 2025-06-18 资源协议](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)
区分普通资源、URI 模板、资源列表变更和可选订阅；列表可分页，标准 read 请求仅携带 URI。
资源能力及其订阅支持由服务端声明。返回内容可以是文本或 blob；URI 也可能表示远端
文件，不能因为采用 `file:` 就由 MuLi 打开本机路径。

由这两项事实推断：对 SDK 返回值做截断不能证明响应在解析前有内存上限，也不能把
原文重新读取后分页伪称服务端提供稳定快照。当前 MCP 文档已明确单帧预解码限制；
新增内容读取不能隐去这项边界，更不能自动预取全部资源或 base64 媒体。

## 可复用的实现方向

如果推进下一片，先让队友按需列出服务端资源，沿同一外部风险门和调用代次租约执行。
目录和读取应保持为独立工具，不能塞进启动提示词。URI 留在服务端协议中，不转换成本机
文件访问或另一次 HTTP 下载；响应正文始终作为外部资料，不生成技能或系统提示权限。

资源请求应复用正在实现的确切连接、最后派发门和旧结果策略，避免新建第二套生命周期。
同名服务器替换后，旧引用必须要求重新发现；分页来源、当前代和已展示进度应明确，不能
悄悄跳过放不下的条目。未经读取的 size/mimeType 是服务器元数据，不是内容真实性证明。

首片不必同时加入 URI 模板展开、订阅、服务器提示词、自动保存媒体或后台抓取。先回答
传输预算问题，再选一项真正可验证的能力，比把这些接口全部开放后只裁剪模型输出更稳妥。
不为研究修改全局安装、升级可选 SDK 或增加本地依赖。

## 锁定 SDK 的进一步核对

`uv.lock` 实际锁定 MCP SDK **1.21.0**、HTTPX **0.28.1** 和 httpx-sse **0.4.3**。
Context7 当前检索返回了 SDK v2/main 文档，不能据此给此安装声明新的默认帧上限。
逐项对照精确 1.21.0 tag 后确认：

- [ClientSession](https://github.com/modelcontextprotocol/python-sdk/blob/v1.21.0/src/mcp/client/session.py)
  的 `list_resources` 与 `list_resource_templates` 支持
  `params=PaginatedRequestParams(cursor=...)`；旧 cursor 位置参数已标记弃用。
  可复用 MuLi 工具目录适配器的分页形态，而不是依赖 FastMCP 高层自动取完目录。
- [Streamable HTTP](https://github.com/modelcontextprotocol/python-sdk/blob/v1.21.0/src/mcp/client/streamable_http.py)
  的 JSON 路径先 `response.aread()` 再交给 Pydantic，SSE 路径先取得完整事件数据再解析。
  此版本暴露 `httpx_client_factory`，但该入口本身不是字节限制；对 POST、后台 GET 和
  恢复流以及压缩响应分别证明预算前，不能声称一个输出截断器保护了接收内存。
- [stdio](https://github.com/modelcontextprotocol/python-sdk/blob/v1.21.0/src/mcp/client/stdio/__init__.py)
  将文本块累积到换行后才解析 JSON；零容量对象流约束已解析消息交接，不限制前面的
  行缓冲。只在 session 返回后检查结果大小无法改变这个接收边界。

因此后续需要独立选择并验证传输接缝或 SDK 兼容升级，不能为资源接口悄悄替换 transport、
复制整套协议栈或把当前工具信任边界扩大为任意资源下载。以上仅为精确源码核对，没有
本地安装、运行 SDK、连接服务或变更本轮 2.33 的热重载实现。

## 实施前需要闭合

- 对锁定 MCP SDK 逐项核实 HTTP/SSE/stdio 的消息解码接缝，区分可强制的字节上限和
  仅事后限制；已有已信任连接策略仍应明确，不声称抵抗任意恶意巨型帧。
- 独立评审是否以资源目录起步，以及目录是否无需正文也能产生足够实际价值。
- 假 session 和已有可选依赖的进程内协议测试先证明分页停滞、重复 URI、超预算、换代、
  取消、文本/blob 区分与外部权限；任何真实协议验证均不访问用户服务器或自动下载内容。

以上是初次调研提出的门槛，不是后续发行承诺。HTTP/SSE 接收接缝的后续实现与独立验证见[接收预算设计](mcp-wire-budget-design.md)，stdio 的未终止行边界仍未闭合；资源功能的实际实施/验收按新设计单独进行。Linux 原生恢复验收保持独立。
