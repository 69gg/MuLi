# MCP HTTP/SSE 接收预算

状态：本片已随 2.34.0 正式发布，精确候选/main 各八项与发行十一项 CI 成功，签名标签及两平台包元数据一致，见[2.34 审查](review-v2.34.md)。该版本没有资源 API、stdio 帧防护、SDK 升级或真实外部 MCP 验收。

## 配置与持久合同

只增加现有 Store 的私有对象 `kind=mcp_policy / id=receive-v1`，包装 `schema_version=1` 与完整策略；唯一字段 `max_message_bytes` 默认 1 MiB，最小 64 KiB、最大 4 MiB，全部使用命名协议常量。缺失采用默认值，坏对象、未知字段/版本和过大原记录明确失败，不覆盖或静默放宽。复用 `_read_object` 在 SQL 层限制原 JSON、`Store.event` 原子保存对象和有限 summary，拒绝外层事务、非 LEGACY autocommit 与空 isolation_level。

不修改 Settings、DDL、compatibility.json、主配置文件、selector 或管理 plan。沿用现有认证 API、工具权限、configuration_lock 和维护门；新增完整替换工具与 GET/PUT API，self_config 返回 desired/effective/pending。保存只请求已有 Extensions reload，不创建新队列、文件 writer 或轮询。通知丢返回不撤销已经提交的策略。

configuration/objects 合同 1 允许旧代码保留其不认识的对象；旧版 Store/恢复/投影按各自 kind 操作。旧 slot 不执行这个策略，因此回退后不能宣称仍有接收保护。这个对象也不授予连接地址或工具执行权限。

## 连接生命周期

限额冻结到确切 HTTP/SSE 连接，不能热改旧 admitted 调用。相同 raw 配置但限额变化也创建新 candidate；stdio 不因此重启。候选准备与最后无 await 发布段重读策略；失败保旧连接和旧限额，状态保留 desired/effective 的差异。策略请求强制已有 full reload；Skills-only 快速路径同时核策略，不能吞掉请求。

沿用当前 generation token、call lease、candidate 和 retirement：发布新代才撤销尚未进入的旧 handler，已接纳调用按原限额完成；退休未清完时拒绝下一次 prepare，因此最多两套连接，不增加后台回收队列。策略写入与 reload 请求同一同步段完成；重启从 Store 实际值重建。

## 接收边界

针对当前锁定 FastMCP 2.13.1、MCP 1.21.0、HTTPX 0.28.1、httpx-sse 0.4.3。通过 FastMCP 公开 httpx_client_factory 创建原标准 AsyncClient，再追加 HTTPX 公共 request/response hook，封装原 AsyncByteStream 并委托关闭。SDK 继续负责 JSON-RPC、认证、重定向、连接/读取超时及 session 生命周期，不复制协议栈、不全局打补丁。

JSON、错误/重定向/其它普通 body 按总原字节计费。只有成功且确实由 SDK 流式消费的 SSE 使用事件预算；旧 SSE transport 的 POST 为非流式 body，不能靠不断输出小事件绕过总量。SSE 在解码前计事件原字节，包括注释、未知字段和换行；正确处理 CR、LF、CRLF 及任意跨块边界，CRLF 不是两个空行，未终止长行同样受限。长期 GET 的累计字节不作单条消息大小；Content-Length 预拒只用于普通 body。SSE 只接纳 UTF-8。

请求固定 Accept-Encoding: identity；响应出现非 identity Content-Encoding 时，在 HTTPX 解压前失败。预算只保证交给解码器的 body 有界，不保证底层已分配的单个 transport chunk、全部连接合计 RSS、无限小事件的速率或任意自定义 transport 的分配行为。

## 失败与取消

接收 guard 在抛错前同步锁存首个固定失败码，不记录正文、URL、头或异常文本。MCP 1.21 的后台 GET 会吞异常，所以 prepare 的复用/准备后、publish 与 handler 最末无 await 门都核同一个 latch。尚未接纳的调用返回 not_executed；已经进入 SDK 而不能证明尚未 POST 的调用保留原失败/未知语义，不重放、不重新调用另一端点。已发布坏连接通过现有退休收尾，候选坏连接由原 discard 拥有。正常取消/关闭不伪装成预算失败。

固定失败原因沿 `MCPManager.status` 进入 `self_status.extensions.mcp` 与已有 API，不把外层 `loaded` 当作逐连接健康。SDK 的 debug 日志并未可靠承接统一错误通知；本片不调用 catalog-changed 来重连，不冒用 `extensions_watch` producer，不新增默认渠道通知。统一通知仍是独立后续范围。

## 验证计划与独立自审

仅流式 MockTransport、小型临时 Store 和真实 Runtime/假 provider；阻断真实网络和子进程。验证普通 body 精确边界、SSE 所有换行拆块/长行/长期流、压缩预拒与非流式 SSE body；后台 latch 后未派发拒绝、在途失败无重试、坏连接不复用/发布；同配置改限额新连接与旧调用 drain、两套上限、Skills-only 请求、策略漂移和保存失败保旧；工具/API认证、原配置字节不变、坏/过大对象与事务回滚。

自审：不把目录 JSON 汇总预算冒充 wire 预算；不在 setter 内重启 Runtime；不为 policy 引入第二个事务账本；不把已入 SDK 的异常归类为已证实未执行。本地结果和独立静态审查见下文；所有检查均先领取共享 guard，每次最长 30 秒。

## 本片验证

新增 `test_mcp_policy.py` 22 项、`test_mcp_receive.py` 31 项、`test_mcp_receive_integration.py` 15 项，合计 68 项，最终 1.52 秒通过、峰值 167.1 MiB、无 skip。临时 Store 的严格原记录读取、坏对象拒覆盖、原子事件和调用者事务保持；流式 MockTransport 的精确 JSON/SSE 事件边界（含分隔空行、CR/LF/CRLF 拆块）、未终止行、非流式 SSE POST/DELETE、压缩预拒与取消；真实锁定 SDK 的 JSON、GET 吞错和 streamable POST，以及真实 FastMCP 的初始化→目录→工具调用→后台失效门均实际执行。没有用 `Response(content=...)` 预缓冲替代这些接收测试。

另有两个不重叠关联组：MCP/generation/live、Extensions/watcher、Skills-live 六文件 129 项（2.88 秒、177.9 MiB）；Runtime/session、management/HTTP、system-error-origin、Skills 六文件 133 项（5.08 秒、172 MiB）。共 15 文件、330 项唯一用例；本片 10 个 Python 文件 Ruff/格式、7 个生产文件 mypy 通过。首轮静态仅修导入排序与 HTTPX 可空编码属性的显式拒绝；最终新增组的一个 Pydantic warning 来自刻意构造字符串 cap 的无效 `model_copy`，该输入仍被固定拒绝。整轮最高 198.8 MiB，swap 0；所有检查均经根 `check_local.py`，串行、每次 30 秒，未扩大任何上限。

原 MCP extras CI 显式增加三份新文件，不新增 job 或升级 SDK。可选 `httpx_sse` 仅在实际 decoder 测试局部加载，三份新文件的纯 JSON/Store/Runtime 测试在无 extras 的普通矩阵仍可收集；三个直接 SDK 用例和一个完整 FastMCP 用例按实际可选依赖跳过。根与 Windows reviewer 对产品、测试、CI 和文档的独立静态审查未发现确定 P1/P2。此处没有根整合、远端 CI、外部服务器、stdio 进程或真实模型验收。

依据：[HTTPX 0.28.1 hooks](https://github.com/encode/httpx/blob/0.28.1/docs/advanced/event-hooks.md)、[raw streaming](https://github.com/encode/httpx/blob/0.28.1/docs/async.md)；SDK 行为另以本地锁定版本源码核实，Context7 当前 main/v2 的原生 SSE 限额不能套用到 1.21。
