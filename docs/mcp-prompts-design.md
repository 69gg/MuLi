# MCP prompts 的显式文本草稿

状态：仅设计，基于 `72ee52be34a213fc4bb6f30d56c97a911785b633` 的资源首片；本文件不表示 prompts 已实现、已通过回归或已发行。本轮只读源码和官方资料，没有运行检查、服务器或安装依赖。

## 目标与接口

用户明确选择服务端模板后，允许会话或队友列出已配置服务器的 prompts，再按已展示引用与参数取回完整文本草稿。增加两个固定宿主工具：`mcp_prompts_list(server, cursor?)`、`mcp_prompt_get(server, ref, arguments?)`。均为 `risk=external`、`preserve_output=True`，复用当前审批、维护、暂停、main/reviewer/Attention/disabled 过滤与最终无工具轮；不为每个 prompt 动态生成工具。

成功 get 只返回带 `untrusted=true` 的完整工具资料：description 与有序 `messages[{role,text}]`。`role` 仅允许协议的 user/assistant 枚举，作为数据保留；不创建真实会话消息、不插入 system/user/assistant 上下文、不自动执行、不变成 Skill，也不修改系统提示词。接收工具资料不等于已经执行模板。没有预抓、自动补全、订阅、后台重取、slash-command UI 或新通知源。

## 官方与锁定版本证据

已先通过 Context7 查 [2025-06-18 prompts](https://modelcontextprotocol.io/specification/2025-06-18/server/prompts)，再核本地已锁 MCP **1.21.0** 与 FastMCP **2.13.1** 源码；不套用 main/v2 API。协议将 prompts 定位为用户可显式选择的能力，但不强制特定 UI。输入输出必须校验，不能把收到的 message 角色视为权限。

- [session.py:398–434](https://github.com/modelcontextprotocol/python-sdk/blob/v1.21.0/src/mcp/client/session.py#L398)：`list_prompts(params=PaginatedRequestParams(cursor=...))`、`get_prompt(name, arguments: dict[str,str] | None)` 可沿现有确切 session 调用；不用高层自动分页或额外客户端。
- [types.py:317–324、638–679、789–802](https://github.com/modelcontextprotocol/python-sdk/blob/v1.21.0/src/mcp/types.py#L317)：`prompts is not None` 才有能力，`{}` 有效；参数只有 name/description/required，没有 JSON Schema、默认值或类型转换合同。返回 message 的 content 可为 text、图像、音频、链接或嵌入资源，并允许未知字段。
- [分页协议](https://modelcontextprotocol.io/specification/2025-06-18/server/utilities/pagination)：cursor 不透明；缺失/None 终止，空字符串原样续页，重复空串同样算停滞。

## 目录、引用与参数

只接已启用、有接收 guard 且声明 prompts 的 HTTP/SSE 连接，stdio 不接。prompts-only 服务器跳过 tools/list；tools/resources/prompts 缺失互不伪装成支持。旧 tools-only fake/client 保持原 Protocol，新能力为可选接口；真实适配器仍必须有初始化证明。

无 cursor 的 list 在原连接的一次绝对 timeout 内完整观察全部页，再发布一份目录；本地 cursor 仅续读这份完成目录。沿用 max_pages、max_catalog_bytes，未知页字段和参数描述也参与完整 JSON 计费。拟复用现有 256 项目录上限，增加命名协议边界 `MAX_PROMPT_ARGUMENTS=32`；先核实际数组长度再逐项投影。只保留 name/title/description 与声明参数，重复 prompt name、重复参数名、非法 required/类型、停滞或超限均整体失败，不发布前缀。

每连接最多一份资源目录与一份 prompt 目录，各自沿用既有目录预算且不保留历史；新增占用明确上限为一份 prompt 目录、至多 256 个条目及同阶已发 ref/cursor。两种引用互不通用。仅实际完整展示过参数描述的 prompt 可 get；成功刷新、MCP 换代/复用、关闭和重启使旧引用失效，Skills-only 保持。缓存不按调用次数增长，不保存正文。目录不是服务端修订快照，服务端可在 get 前改变模板；本片不能声称获取了旧版本。

ref 内部绑定原 prompt name；显示名称/描述可以按旧代脱敏，绝不把脱敏后的名称发给 SDK。arguments 缺失与显式 `{}` 保持区别；值只接受字符串（允许空串），不做类型转换、模板展开或缺省填充。只接受所选 prompt 声明的参数名，required=True 必须存在；拒绝多余键。拟以 `MAX_PROMPT_ARGUMENT_BYTES=16384` 限制完整参数 JSON 的 UTF-8 字节，发送前验证无效 Unicode，不截断参数。该命名边界约束首片协议，不新增 Settings 或 policy。

参数键必须能精确往返；若已知密钥脱敏会改变某个参数名，首片选择整份目录明确失败，不泄露该键、不签发不可用参数合同，也不为此新建另一套参数引用系统。无参数或空参数名按锁定协议的实际类型处理，不能凭真值误判；固定宿主字段、ref、cursor 和经验证的 role 枚举不参与文本替换。

## 完整文本与调用结果

get 在同一连接上只请求一次，先核 messages 实际长度；拟用 `MAX_PROMPT_MESSAGES=16` 限制实际遍历。只接受所有 message 均为 text；image/audio/resource link/embedded resource、混合媒体或未知 content type 整份拒绝，不挑出文本冒充完整草稿。空 messages 与空文本可成功，不等价于错误。未知字段不传播、不执行、不触发 URI 读取。

原角色次序、完整文本和宿主标识须全部保留；只对允许的文本值按旧 generation 脱敏，再以实际 Runtime JSON 包装核旧代 max_output_chars。无法容纳明确失败，没有 preview、正文分页或再次 get。最新完整批次由 preserve_output 保护，完整结果放不进下一模型请求时停止，不发下一请求；较早批次仍沿原上下文淘汰规则。

复用最后无 await 的 generation/确切连接/latch 门和 active_calls。尚未派发的旧 handler/ref 明确 not_executed；已接纳 get 可用旧连接、旧密钥与旧限额完成。迟到目录不得发布新引用；已进入 SDK 的异常、超时、取消不改称未发送，不自动重放。接收失败仍封锁连接全部新派发，候选和退休沿现有所有者收尾。状态只返回支持与固定错误码，不回显参数、正文、prompt name 或服务器异常。

## 最小复用范围与验收

实现前再审以下小接缝，不复制第二份长生命周期函数，也不建立通用 RPC/插件框架：

1. `tools/mcp.py` 的真实 capability/适配器增加 prompts；共用 `_admit_call/_release_call`、候选/发布/退休与固定宿主工具注册，不另建 manager/连接。
2. 将 `ResourceCatalog` 的有限 ID/ref/cursor 值结构及完整目录的页数、总字节、一次 deadline、最后发布门提为小共享 helper；资源和 prompts 仅提供各自 fetch、投影和唯一键。保留明确的两类值与错误，不通过字典字段猜 RPC。现资源路径必须原样回归，避免为了复用引入策略类树。
3. 目录输出的完整条目装页与 JSON 预算复用当前资源接缝；新增纯值 prompt 参数/文本投影。Runtime、模型 codec、Settings、Store、文件 writer 和通知流程均不需改动。

旧实现红例应为 prompts-only 初始化无工具入口；绿色门控至少覆盖 `{}`/缺失能力、资源/工具组合、空目录、空与重复 cursor、总页/字节/条目/参数预算、未知字段、完整参数页才能签发 ref、required/多余键/空值/None 与 `{}` 原样请求。锁定 SDK 用流式 MockTransport 证明实际 list/get，无真实 MCP 或 stdio。

真实 Runtime/假 provider 再核三种 codec 的完整有序文本、role 不成为真实对话角色、混合媒体整体拒绝、无法容纳不发下一请求、审批后换代阻派发、在途旧结果/脱敏与退休、取消/失败不重试。现有 resource/MCP/过滤/最终空工具轮回归必须保留。旧 slot 没有这些入口，也不会执行新校验；不宣称回退后仍具备 prompts 保护。此文件只有方案，没有测试或外部服务器验收结论。
