# MCP 单个二进制资源保存为附件

状态：已在开发分支实现最小接缝并完成下述受限离线回归，待独立审查与整合；尚未发布或连接真实 MCP。设计基于已签 prompts 切片 `36b2c8b6f144a34b663972599beb8660b7c6a733`，根已整合为 `69935e4`。本地通过不等于外部集成已验收。

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

## 开发实现与受限验证记录

实现集中在 `tools/mcp.py`、`tools/mcp_resources.py`、`LocalServices` 与 Runtime。`ResourceSaveTarget` 仅携带原单文件上限与同步 preflight；preflight 在本地识别 MIME、计算摘要并检查完整回执后返回唯一 async 保存回调。工厂在操作入口预校验共享文件名、真实执行 owner、维护门、disabled 与原 PreparedRequest，随后无 await 进入原代 MCP lease。`attachments.py` 只抽取共用文件名函数，writer、DDL、Settings 和渠道逻辑未改；没有装配保存工厂时仍只有原资源工具。

保留的先红用例通过真实 Runtime 列目录取得 ref 后请求保存，旧实现精确返回“工具未启用”：1 failed、0.38 秒、峰值 79.2 MiB；不是 ImportError。实现后新 `test_mcp_resource_files.py` 首轮 42 项绿，最终补齐真实暂停 scope 与显式看图/假渠道交接后 **44 项通过，1.02 秒，峰值 102.2 MiB**。覆盖三 codec 的普通二进制/PNG/空 blob 完整回执、短秘密不改宿主键或摘要、非法内容/大小/回执恰好及差一字符、真实审批后换代/disabled、owner/维护/关闭/缺上下文、进入后网络或容量等待换代、取消/原绝对期限、保存器失败及 metadata 后 public 投影丢回执的固定 unknown。最后两项测试构造曾分别因缺少任务 brief、向冻结渠道 profile 赋值失败，均只修正夹具；生产代码没有因这些失败改动。

资源相关四文件含新增真实锁定 SDK 流式 HTTPX MockTransport→Runtime→Attachments 用例合计 **96 项通过，2.73 秒，峰值 131.2 MiB**；该集合包含上述首轮 42 项，不与最终 44 项重复累计。真实 SDK 用例验证一次 resources/read、原字节/SHA/MIME 与已关闭响应流，没有 socket 或真实 server。附件记录、LocalServices、媒体、扩展与 prompts 七文件另 **206 项通过，3.48 秒，峰值 156.1 MiB**。五个生产文件严格 mypy 通过（峰值 183.5 MiB）；七个 Python 文件 Ruff/format 通过。

全部执行都经过 `/data0/MuLi/scripts/check_local.py --seconds 30 run -- /data0/MuLi/.venv/bin/python …`，单次 1 GiB、共享 2 GiB、swap 0、CPU 1、tasks 128，输出均确认实终态与 swap 0。未下载、训练、压测、连接真实模型/MCP/渠道、操作本机服务或桌面、push、运行远端 CI 或改版本。这里的 vision 成功路径是有限方法替身，OneBot 是 MockTransport outbox；均不证明真实账号可用。本次实现验证当时尚未单独新增 file_io/metadata 各取消门、任意重复取消与保存回执后整批上下文不足用例；后续有限补片见下文，不将既有机制或其它用例的通过冒称这些场景已逐一验收。

冻结切片经独立 Sol 只读审查，追踪了源码、实际调用接线、测试与上述合同，未发现可证实的 P1/P2。独审没有执行检查，也没有连接真实 MCP，不增加任何运行验收证据。独审时 file_io/metadata 逐门取消、任意重复取消与保存回执后整体上下文不足尚未新增用例；独审结论不将这些场景认定为已验证。

## 有限测试补片

根只读复核后，仅对既有测试文件追加六项，不改生产。先读 `Attachments.file_io/save` 与 Runtime 的完整资源上下文不足回归，并经 Context7 核对 Python 3.13 的 `asyncio.shield`/线程交接合同；补片复用原真实 Runtime、Store、Attachments 和有限假资源客户端。

- 两项在原 `file_io` 的真实 `to_thread` worker 内，分别暂停实际临时文件 write 与 fsync；单次取消并发布删除连接的新代后，原 lease 和 reservation 仍保留、旧连接尚未关闭。放行 worker 执行真实操作后，调用仍抛 `CancelledError`，lease/reservation 归零、临时文件清除、metadata 未创建，仅一次远端读取。这证明该单次取消门会等待 I/O 收尾，不证明线程可被硬停止。
- 一项在真实 `Attachments.save` 完成文件和 metadata 提交后，用调用返回门暂停 LocalServices 接收回执；随后换代并单次取消，调用保留取消语义、原 lease 释放，既存文件原字节与 MCP metadata 均保留，没有 `tool.completed` 成功回执，也没有再次读取或保存。这是 writer 返回之后的 async 接缝，不声称可在同步 SQLite 提交指令中间暂停。
- 三项分别使用 OpenAI Chat、Responses、Anthropic 的真实 Runtime 与假 provider，在单批明确请求中成功保存八个小文件。八份完整回执及协议外壳放不进 4000 字符上下文时抛原 `ContextLimitError`，只发生一次模型请求；八个保存结果仍以 accepted/preserve_content 完整入库，文件、原字节、ID 和 SHA 全部保留，运行明确失败，不把已发生效果改称未执行。

六项定向首轮 **6 passed、44 deselected，0.47 秒，峰值 86 MiB**；格式化后整个文件 **50 passed，1.15 秒，峰值 105.4 MiB**，Ruff 检查通过。所有命令仍经上述 30 秒共享资源守卫串行执行并确认实终态，swap 0；本片只改测试和本设计，不因测试无生产修改而重复源码 mypy。没有 socket、真实 MCP/模型/渠道、CLI 或桌面操作，也没有 push、远端 CI、版本改动或子 agent。

这六项补齐了上述指定取消与整体上下文场景的有限运行证据，不改既有独审发生时的历史范围；任意重复取消仍明确未验。writer 的跨介质非事务边界、真实外部集成、真实 vision/渠道能力与线程硬停止均不获得额外承诺。
