# v2.55 MCP 工具图片审查

2026-10-24，基线是已签名推送的2.54候选 `2cc123f974e66bab1d66ffabbbfb5b23b1d7a21e`。
普通 MCP 目录图片原已可以显式保存并看图；本片补齐工具直接返回 typed ImageContent 的路径。
唯一生产文件为 `src/muli/tools/mcp.py`，复用原附件、Team 和实际视觉 provider，无新依赖或持久格式。

## 行为与独立审查

HTTP/SSE 完整成功的图片结果可携带 opaque `image_result:` 引用、核得的 MIME/大小及不可信标记。
图片与链接原 typed 字段及额外字段共享原目录字节预算；图片沿原16块上限、严格 canonical
ASCII/base64、四种原图片 MIME 与已有头部识别。签名检查不等于完整图片解码或像素有效性。
全部材料及完整输出通过后，在同一当前连接/代次/最近调用/接收状态/期限末门一次发布；
失败、截断、坏材料、超额或旧调用晚返均不发布部分引用。

仅 tools 能力的服务器可显式保存 inline 图片，不发 resources/list/read；普通目录能力、
stdio 及 tool_result 文本引用的二进制保存禁令保持。图片 ref 在宿主预检前准确核当前缓存，
之后复用原 factory/prepare/save、external 审批、真实运行守卫、附件额度和 lease；已进入保存器
后失联仍 unknown，不自动重存。图片缓存不持久，保存成功的附件沿原存储与显式视觉工具使用。

非作者已独立审查完整生产链，无剩余P1/P2。图片及图中文字不授予操作、外发或验收权限；
没有自动保存、自动看图、隐式图像上下文或文件外发。
另一位非作者单独后审两参数正式旅程，确认实际媒体 ModelClient 未被普通主流 FakeClient
替代，provider HTTP 完整核图像及模型选择，并核真实附件、运行中 progress 与原私聊/Web；
同样无剩余P1/P2。两项审查均未冒称固定视觉响应代表自然识图质量。

## 实际本地验证

- 首正式目标红：真实 SDK 已解出成功 ImageContent，工具结果完整 accepted，队友和主助手均正常
  收口，再准确发现原投影仅 omitted、无可保存 ref。1 failed，pytest0.92秒，守卫1.828秒、114.3MiB。
- 首实现后六项为5 passed / 1 failed，pytest4.99秒、守卫5.802秒、116.4MiB。临时 Store 的有限
  只读核查确认真实图片引用已产生；测试 helper 的 name 参数与保存文件名重名，构造 ToolCall 前
  抛 TypeError，尚无保存工具消息或附件。只改该处为原 ToolCall/JSON，不改生产、预算或原4秒门。
- 修后首主视觉旅程1 passed，pytest0.87秒、守卫1.679秒、115MiB。随后同流程参数化主视觉及
  外挂视觉，两参数2 passed，pytest0.99秒、守卫1.846秒、122.4MiB；旧主视觉复验不重复累计。
  真 tools-only SDK 返回原图，队友显式保存并带真实附件交给仍在运行的主助手；实际视觉 HTTP
  data URL 的解码字节及 SHA 均等于原 PNG，之后才返回固定视觉证据，主助手沿原私聊7协调，Web可见。
  查看前无自动保存或视觉请求，整个过程零资源 RPC、一次图像调用、一次保存、一次视觉请求。
- 五项独立 policy 全部通过，覆盖同组多图与图片/链接合计预算、恶意编码及伪图、来源保存限制、
  失败/取消/换代/接收/关闭晚返、秘密消毒碰撞、unknown零自动重入。Ruff发现竞态测试五处循环
  闭包捕获；仅提取局部 async 每轮 helper，保五节点与全部原断言。该节点复验1 passed，pytest0.30秒、
  守卫896毫秒、72.3MiB，没有用 noqa 或放松规则。
- 既有 MCP 全16文件318 passed、1个既有 Pydantic 非法类型夹具 warning，pytest6.10秒、
  守卫7.173秒、241.8MiB，无跳过。发布兼容166 passed，pytest0.96秒、守卫1.470秒、67.2MiB。
  两正式加五policy、旧318及发布166，合491个不同本地节点。
- 首带增量缓存的 mypy 报未改 skills.py 三处 unused-ignore；不改代码、依赖或检查规则，以
  `python -m mypy --no-incremental` 干净检查150源码全部通过，守卫8.485秒、416.8MiB。
  Ruff通过；三个改动文件沿原formatter仅整理格式，401文件格式检查通过；83包离线锁检查通过。

离线wheel/sdist构建通过，守卫1.288秒、67.5MiB；版本、兼容声明、changelog及两包一致性
检查通过，守卫180毫秒、19.2MiB，未覆盖旧release-notes。隔离离线安装及原import/API/
资产/生命周期检查通过，守卫1.295秒、100.2MiB，未下载依赖。

以上全部 root 串行经原守卫，单次1GiB、共享2GiB、swap0；未放宽限额或下载依赖。
锁定 SDK 与固定模型/HTTP 替身只证明实际传输和工具链，不代表自然识图质量、真实桌面截图、
外部服务器或账号联调。精确远端发行证据随后补录，当前未正式发行。
