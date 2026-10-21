# MCP 单个二进制资源保存为附件

状态：仅为下一片候选设计，基于已签 prompts 切片 `36b2c8b6f144a34b663972599beb8660b7c6a733`，根已整合为 `69935e4`。未修改生产代码、执行测试或连接实际 MCP；本文不是能力已交付的声明。

## 选择与协议依据

优先增加显式 `mcp_resource_save(server, ref, name)`：从已展示的普通资源引用取回一个完整 blob，保存到现有附件库，再由调用者另行选择 `image_describe` 或 `channel_send`。这直接补上图片和文件的可用入口，复用 `Attachments`、附件查询、认证下载和渠道 outbox，不建第二个附件引擎。

暂不接 RFC 6570 资源模板。锁定 SDK 有 `resources/templates/list` 与分页参数，但本仓库没有 URI-template expander；普通字符串替换不能覆盖 reserved、复合值和 explode 等语义。该选择依据[官方资源协议](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)与 [RFC 6570](https://www.rfc-editor.org/rfc/rfc6570)，不是协议禁止模板。

先通过 Context7 查证，再核 FastMCP **2.13.1**、MCP **1.21.0** 的实际代码：[session.py:302–315](https://github.com/modelcontextprotocol/python-sdk/blob/v1.21.0/src/mcp/client/session.py#L302) 的 read 仅发送 URI；[types.py:519–554](https://github.com/modelcontextprotocol/python-sdk/blob/v1.21.0/src/mcp/types.py#L519) 允许 text/blob 数组和额外字段，`blob: str` 本身不验证 base64。严格解码使用 Python 3.12/3.13 已有的 `b64decode(..., validate=True)`；以 [3.13 文档](https://docs.python.org/3.13/library/base64.html#base64.b64decode)为准，不套 Context7 main 中 3.15 才新增的参数，也不宣称验证了 canonical padding bits。

## 一个已接纳操作的权限边界

取回、校验和一次保存属于同一个明确的 `risk=external` 操作；描述和审批参数须说明它会进行远端请求与本地持久保存。现权限模型只有单个 risk，external 在 auto 模式也不因独立审查通过而自动放行；yolo 仍是现有显式策略。不得降为 read/write 或新造复合 risk。复用 reviewer、Attention、main 协调工具、disabled 和最终无工具轮的现有过滤，不额外授予主会话调用资格。

工具由应用组合层注入现有附件保存能力；MCPManager 不导入 Runtime、LocalServices 或 Attachments。可使用一个有类型的可选保存回调，未装配时不提供保存入口；回调只复用同一个 `Attachments.save(source="mcp")`。依赖随 Runtime 重新配置重新装配，不保留指向旧 Store 的回调，也不新建 manager 或 writer。

必须区分真实门的责任：`Runtime._tool` 已执行权限、PreparedRequest/暂停 epoch 与待处理输入检查；它并不等于 MCPManager 内有 owner/维护检查。保存入口还需由 Runtime 的窄宿主 guard 同步核 `_closing`、实际 run owner、维护 admission、当前 disabled 与原 PreparedRequest；MCPManager 的 `_admit_call` 只负责原 generation、确切 connection、receive latch 与在途登记。各门到进入该操作之间不增加 await，审批期间变化仍不得派发旧 handler，更不能按旧 server 名查找新端点。

**进入后按原代完成，不承诺途中撤销阻止写入。** 该合同与当前 MCP read、`file_publish`、TTS 一致：网络返回后或等待附件容量期间发生换代、禁用或暂停，并不形成第二次独立的保存审批。原 connection lease 持有到保存及其收尾结束，使用原代输出限额；不因新代出现而把回调或 URI 改接新对象。若要改成中途撤销，至少需要在容量等待后与 fsync 后/replace 前分别检查；只在调用 save 前检查一次并不能证明最终提交仍受当前代授权，本片不为此改造附件引擎。

## 校验、预算与本地效果

只向当前已启用、声明 resources 且有接收保护的 HTTP/SSE 原连接请求；stdio 和任意 URI 直读仍不接。ref 必须来自该代实际完整展示的普通资源目录。一次 `resources/read`，不用第二次读取拼接；URI 只是传给原 session 的标识，不据 `file:`/`https:` 等 scheme 打开本机文件或发另一次 HTTP。

先核实际 `contents` 长度恰为一，再核单块为合法 blob；有 text、混合、多块、未知形状或类型不符则整体失败。未知字段仍由原接收预算计费，不传播或执行。校验返回 URI 但不签发新引用；不要求服务端返回的子资源 URI 是本机路径。空 blob 沿现附件库可保存空文件的行为处理。

在任何临时文件、配额 reservation 或元数据写入之前，完成有界 ASCII/base64 检查、解码与实际长度检查；根据当前单文件限额先拒绝明显超大的编码输入，再核真实 decoded bytes。目录 size 仅为提示，不能代替实际长度。沿用连接的 `max_message_bytes`、一次绝对 timeout 及 uploads 的单文件/总配额/并发限制，不新增 Settings；base64 和 JSON 外壳也消耗 wire 预算，不能承诺取满默认 32 MiB 附件上限。等待容量和写入不重置原操作期限；附件库在写入时仍按当前配额检查 reservation。

name 由调用者明确给出，沿 `Attachments.save` 原来的末段文件名提取、空白、UTF-8 240 字节及控制字符校验，可窄提取原校验供请求前使用，不另造命名规则。数据始终写宿主随机 ID 文件，名称不是路径。MIME 复用现 `detected_media`，不因远端声明或扩展名直接获得 inline/vision 权限；未知内容作为 octet-stream。二进制原字节不做字符串密钥替换，否则摘要和文件内容会被静默改变；回执不包含 blob、正文、原 URI、服务端未知字段或文件系统路径。

完整成功回执只保留固定宿主字段与 `attachment:{id,mime_type,size,sha256}`、`saved:true`、`untrusted:true`。先用实际 decoded bytes/header 算出大小、摘要和最终 MIME，并以相同长度的 32 位 ASCII ID 占位，按实际 Runtime `json.dumps(ensure_ascii=False)` 外壳预检原代工具输出预算；真实随机 ID 替换不改变长度。预算不足时无文件效果，不返回 preview 或截断 ID。宿主固定键和摘要不参与通用文本脱敏。

使用 `preserve_output=True`，实际 Runtime/三 codec 保持完整回执；若随后完整工具批次仍装不进模型上下文，沿现机制停止下一请求。该停止发生在工具效果之后，不能倒推保存未发生；不能用 handler 的输出预检冒称一定能完成后续上下文交付。

## 取消、失败与持久边界

`Attachments.save` 当前为同目录临时文件写入、文件 fsync、`os.replace`、`Store.put`、public 投影；有异常时尝试删除目标与临时文件。它不是文件系统与 SQLite 的跨介质事务，也没有新目录 fsync 证明。不得称 exactly-once、崩溃原子或“异常就一定没有保存”。尤其 metadata 已提交后丢返回、public 投影失败、取消或进程崩溃，可能留下元数据与文件不一致；本片不建立新账本或恢复线程来掩盖此边界。

SDK 一旦进入就不能把远端结果不明改称整项 not_executed；GET 已完成但校验拒绝时可明确 `saved:false`，表示本次尚未调用保存器。保存器一旦进入而没有完整回执，只给固定 unknown/不确定结果，不自动再次 get/save；取消仍保留取消语义。严格只触发一次保存调用，沿当前 file_io/Runtime owner 收尾，不声称线程 I/O 可硬停止或任意重复取消都已经验收。

落盘成功但回执丢失时，可用既有 `attachment_search(source="mcp", name_query=...)` 沿 cursor 查元数据，再显式读取/下载；名称不唯一、元数据不证明文件仍存在，也不能据此推断旧调用的精确结果。附件读取仍有路径/大小检查；渠道发送还会核摘要、格式、配额与目标绑定。保存本身不附加会话消息、不运行看图、不发送渠道；queued 也不等于 delivered。

## 最小接缝与验收

生产接缝仅围绕 `tools/mcp.py` 的确切代次/资源 ref/调用 lease、`tools/mcp_resources.py` 的单 blob 纯值校验、`LocalServices`/Runtime 的宿主回调装配与入口 guard。`Attachments.save`、附件元数据、媒体识别、认证下载、`ChannelManager` outbox 继续复用原实现；不新增 DDL、策略、管理事务、模板 expander、下载库或另一套附件缓存。原 `mcp_resource_read` 的完整文本和 blob unsupported 行为保持。

实施前至少证明以下受控红绿，不运行真实 MCP、stdio、渠道或模型：

1. 已展示 ref 的单 PNG/普通二进制可经锁定 SDK 流式 MockTransport 保存原字节，完整 SHA/ID 回执；资源专用、空 capability、短秘密固定键和原 text read 不回退。
2. ref/跨代/审批后替换/disabled/暂停 epoch/owner/维护入口拒绝时，SDK 和保存器均零调用；进入后换代或容量等待期间换代，原代最多一次保存并明确采用原合同，旧 lease 到收尾才释放。
3. 非法 base64、非 ASCII、双 text/blob、多块、错误类型、实际大小/总配额不足、回执刚好与差一字符，核无前置文件效果或原 reservation/临时文件清理；宣称很小的 size 不绕真实字节预算。
4. 真 Runtime/假 provider 的三 codec 完整回执与原风险/过滤；上下文不足不发下一模型请求，已完成文件效果不被抹成未执行。URI 为 file/custom/http 也无本机 open/额外 fetch。
5. 网络、容量、file_io 与 metadata 返回门控分别取消/超时，不自动重试；注入保存已提交后丢返回，固定不确定结果且没有第二次 get/save，不把残存对象当健康成功。重复取消仅按实际能证明的 owner 收尾范围记录。
6. 通过同一附件 ID 显式调用既有看图与 fake 渠道 outbox；无 vision、摘要改变、渠道不支持/超限均沿原错误，保存不触发自动识别或发送。

当前仅完成源码与官方合同核查；没有任何新增测试执行、真实附件保存、外部服务或发布验收。后续实现与独立复审需另行冻结范围，本设计不混入当前 prompts 交付。
