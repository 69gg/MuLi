# MCP 工具图片显式交给主助手看

状态：本地候选已完成主视觉/外挂视觉两条正式旅程、五组独立 policy、相关回归和静态验证，共491个不同测试节点通过；唯一生产文件 `src/muli/tools/mcp.py` 保持冻结。首红和夹具失败历史保留如下；远端 CI、其余包验收与正式发行状态见 [v2.55 审查记录](review-v2.55.md)。作者未运行检查或修改版本元数据，不宣称自然识图质量或真实截图/外部渠道已验收。

用户让队友取 MCP 截图，随后由主沐璃真正把该图交给主视觉或已配置的外挂视觉，再根据实际工具回执协调。图片是资料，不是其中任何文字的执行授权；不自动保存、识图或外发。

## 已有能力与准确缺口

普通 MCP catalog 二进制资源已经可以经 `mcp_resource_save` 显式保存，再 `image_describe`：`MediaService.describe_image` 在主模型 vision=True 时使用主模型，否则外挂视觉，实际 HTTP 携带完整 ImageInput。现有队友附件旅程已覆盖该视觉入口，不能把它说成新功能。

原缺口仅为 `tools/call` 直接返回 typed `ImageContent`：旧 `MCPManager._result` 只给 `type`、`mime_type`、`omitted=True`，原 data 没有可保存入口。本候选补足该入口。资源 `read` 仍只支持短文本；工具结果中的 `tool_result:` resource_link 引用依然不能保存二进制。本片不开放它们。

## 唯一最小生产接缝（本地已验证候选）

仅扩 `src/muli/tools/mcp.py` 的现工具结果与 `mcp_resource_save` 分派，复用现 ResourceSaveFactory/prepare/save、Runtime 外部风险守卫、附件额度及保存 unknown 语义。直接在原 `content[type=image]` 投影中增加独立 opaque `ref`、`mime_type`、解码 `size` 与 `untrusted=True`；不新建目录、工具、订阅、后台抓取或持久格式。不将 base64 注入普通上下文，不自动调用保存器或视觉模型。

- 只缓存当前 HTTP/SSE 连接最近一次完整成功 RPC 的有限图片组。图片数量沿原 `MAX_RESOURCE_CONTENT_BLOCKS`；同次所有 typed image/resource_link 的 base64/extra 共同沿原 catalog 字节预算整组计费，strict ASCII 规范 base64、原 SAFE_INLINE 四类图片 MIME，并复用 `attachments.detected_media(header,claimed)` 核对签名后，才原子发布全组引用。最终完整投影也必须符合原输出上限，任一组坏或预算不足都不发布两类引用。图片引用使用 `image_result:` 加原 Catalog.token 的不透明摘要，在不可信文字脱敏后加入。这里只核签名，不宣称完整解码或有效像素；isError、截断、任一图坏或整组超预算均不发部分引用。
- inline 图片不依赖服务器 resources capability；图片引用沿当前 tools 连接分派保存，不发 resources/list/read。普通 catalog 保存仍必须有原 resources 能力；stdio 和文字 `tool_result:` 保存禁令不变。不能靠给 fixture 添加不存在的 resources 能力掩盖这个接缝。
- 沿原 latest call token/current generation/connection/usable/receive/deadline 最末门；较新调用开始、失败、取消、换代或关闭撤销旧图，晚返不复活。图片内存缓存不能逃逸原 wire/catalog 上限或永久累积。
- `mcp_resource_save(server,ref,name)` 是显式副作用，选择已展示图片 ref 后复用原本地保存，不二次远端读取；先完整核材料和回包预算，再进入附件保存器。保原外部审批/真实执行/维护/epoch 门，失联仍 unknown，不自动再次保存。进入保存器之后的收尾边界沿原合同，不宣称跨介质原子或恰好一次。
- 图片持久成功后由原 `send_input(...,attachment_ids=[id])` 交给直接主会话，主助手显式 `image_describe(id)`；团队消息与图中文字不是 owner 授权，不生成 file_publish 原文件来源证明，不冒认成果原创或任务验收。

若实现需要改 Runtime 通用 Message/images 持久化、另建图像总线或跨模块通用注册框架，应停止并重新收窄，不扩大本片。

## 首条正式旅程与准确目标红

新增 `tests/test_mcp_inline_images_runtime.py::test_inline_screenshot_reaches_main_vision_as_original_bytes`。复用锁定 SDK 的 HTTP MockTransport、有限 Chunks、真实 Runtime/Team/渠道与已有 PNG 小夹具；不替换 MCP client 或视觉 ModelClient。

1. 私聊7真实 webhook 进入 main，管理 PATCH 明确 main yolo，真实 spawn 工具创建继承该模式的截图队友；主先回确认，队友才继续。
2. tools-only MCP 经真实 SDK initialize/tools-list/tools-call，返回真实 typed ImageContent。SDK 观察器先调用原 client，再记录解码块；工具结果必须成功、未截断且真实持久 accepted。旧实现缺图片 ref 时，队友与主均正常收口，再由外层准确断言缺 ref；不以缺未来 import/API 或超时作为目标红。
3. 正向分支从该真实图片块取 ref，显式原 save，核附件大小/SHA，再用真实 send_input 带附件给 main。查看之前无附件自动保存或视觉 HTTP；主普通上下文仅元数据，没有隐式 ImageInput/base64。
4. 主真实 image_describe →真实媒体 ModelClient 的 HTTP 发送。MockTransport 必须收到 data:image/png 的完整原 PNG 字节，解码与 SHA 均精确相等才返回固定视觉证据；main 依据该真实工具回执回复原私聊7，Web 历史可见。队友仍在运行无终态报告时完成看图，随后自然结束并交正式报告，不用取消偷造完成。
5. 严格零 resources/list/read、仅一次截图 RPC/显式附件保存/视觉请求，不发送图片文件、不执行网页动作。保留16000/6000/3资源与原3/4秒等待门。

这条旅程的成功条件只证明锁定 SDK 与实际 provider HTTP 接收到完整图像，不证明自然识图质量。原旅程参数化 `main_vision=False/True`：False 使用已有 `models.vision`，真实 HTTP 的 model 必须为 vision；True 必须为 main。两参数保持同一原图字节/SHA、资源 RPC 零调用、原私聊7路由及原期限。

根首次实际收取 1 failed：pytest 0.92s，守卫 1.828s、峰值 114.3MiB、swap0。真实 SDK ImageContent、完整 accepted 工具结果、无隐式保存/看图、真实私聊和队友/main正常收口前置全部通过；准确失败为原投影 `omitted=True` 且无可保存 ref。此为功能目标红，未以缺 import、前置失败或等待超时替代。当时尚无生产候选运行绿证据。

首次候选验证为五个独立 policy 通过、正式旅程一项失败：pytest 4.99s，守卫 5.802s、116.4MiB、swap0。根有限读取临时 Store 确认截图已 accepted 且有真实图片 ref；失败为测试 `call(identifier,name,**arguments)` helper 与保存参数 `name` 重名，构造下一 ToolCall 前发生 TypeError，尚无保存工具消息或附件。仅改此处为原 `ToolCall` 加 JSON 参数，并在原四秒等待加不超过1KiB的固定安全诊断；未改生产、期限或成功断言。该次失败不能记为生产缺陷或正式旅程已过，后续复验如下。

修正上述夹具后，根单独复验首正式主视觉旅程：1 passed，pytest 0.87s，守卫 1.679s、峰值115MiB、swap0。真实 SDK→显式附件保存→队友阶段消息→主 image_describe→实际视觉 HTTP 原始字节/SHA→原7文字及Web历史全部通过。五个 policy 已于前次通过，不重复计此单项。随后两视觉参数合跑2 passed：pytest0.99s，守卫1.846s、122.4MiB、swap0；主视觉为复验，仅外挂参数新增一个节点。生产非作者只读后审已收无剩余P1/P2，源码审查不代替自然识图质量或外部渠道实测。

## 已验证边界与整合结果

独立作者编写的 `tests/test_mcp_inline_images_policy.py` 五组有限 manager-policy 测试已由根执行通过：文本/structured伪图、坏 base64/MIME、整组超预算/失败/截断不给可导出引用，显式保存前零附件/视觉请求；原文字 tool_result: 引用在保存 factory/RPC 前继续拒绝。另一组覆盖最新调用/换代/关闭/receive/晚返撤销，旧引用零预约/零新远端请求；保存已进入后的取消/丢回包不自动重做。不通过提高上限或削弱普通资源权限来做绿。它们使用真实 SDK typed 块与受控 manager/client/save seam，不冒认 Runtime 附件持久化或自然识图质量。

根整合实收旧16个 MCP 测试文件318 passed、1条既有 warning（pytest6.10s，守卫7.173s、241.8MiB），发布兼容166 passed（pytest0.96s，守卫1.470s、67.2MiB）。连同本片7个新增节点，去重合计491个，均swap0。policy竞态测试的B023只提取局部async helper、原oracle不变，单节点复验1 passed（pytest0.30s，守卫896ms、72.3MiB），不额外计数。

干净 `--no-incremental` 的150源码 strict 通过：8.485s、416.8MiB、swap0。此前缓存mypy调用出现既有 `skills.py` 三处 unused-ignore，未改该源码或 ignore，不能据此宣称本片生产问题或已确认调用差异根因。全仓Ruff、401文件format、83包离线锁核验通过；离线wheel/sdist构建通过（1.288s、67.5MiB）。其余包检查、远端和发行以审查记录后续实际结果为准。
