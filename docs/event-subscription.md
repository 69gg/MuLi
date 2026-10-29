# 事件订阅与恢复位置

## 服务端位置与 HTTP 游标

认证后的 `GET /api/ui/event-position` 只返回当前资料库的全局事件位置：

```json
{"after":"9007199254740993"}
```

`after` 始终是精确十进制字符串，空事件表返回 `"0"`。Store 复用最大事件主键查询，只读取整数位置，不加载事件 JSON，也不创建主会话、提交输入或启动模型。接口沿用管理 API 的认证与同源边界，响应带 `Cache-Control: no-store`。

调用方应先取得位置 P，再读取初始化事实，最后以 `after=P` 订阅全局 `/api/events`。这样 P 之后的持久变化仍可通过后继事件使摘要失效。P 是一次当前位置读取；它没有把多个 HTTP 事实请求变成同一事务快照，也不替代会话 hydrate 自己的恢复位置。

`GET /api/events` 与 `GET /api/sessions/{session_id}/events` 共用 HTTP 游标校验：

| 输入 | 有效范围与行为 | 无效响应 |
| --- | --- | --- |
| query `after` | FastAPI 整数参数，默认 0，范围为 0 至 SQLite signed int64 最大值 9223372036854775807 | 格式或范围无效返回 422 |
| 非空 `Last-Event-ID` | 独立解析整数，范围与 query 相同；空 header 沿用未提供语义 | 返回 400，`detail` 为“事件游标无效” |

两项分别通过校验后才取较大值，不能让一个合法输入掩盖另一个无效输入。负数和超界数不会被夹成合法位置；检查完成后才构造 `StreamingResponse`，无效输入不会进入 `EventBus.stream` 或 `Store.events`。合法位置即使高于当前最大事件 ID，仍保留等待后继事件的既有行为。内部 EventBus 消费者的合同没有变化。

位置字符串与外部 SSE 输入可以完整表达 int64 范围。主 `/conversation` 快照的事件 ID 和 `after` 也使用精确字符串；历史会话详情的旧格式没有在此改写。数据库替换、恢复后的实例 epoch、未来或过旧位置的显式重同步协议仍留待后续。

## 服务端验证边界

`tests/test_event_subscription_api.py` 使用临时 SQLite、真实 ASGI 路由、真实 EventBus 的有限帧读取及有限 SSE 替身，覆盖认证、no-store、空表、超过 JavaScript 安全整数范围及 int64 上界的精确位置、只读与不加载 payload、重开及删除末事件后的当前位置。真实 EventBus 回归验证先读 P、再持久写入事件，后继订阅只返回 P 之后的事件。两个 SSE 入口均覆盖格式、负值、超界、一个输入合法而另一个无效、空 header、默认位置和 query/header 最大值选择；拒绝路径明确断言没有调用流工厂或事件列表查询。

这些回归不连接真实模型、设备或外部服务，也不证明 bootstrap、活动正文、单条 SSE payload、长期浏览器 state/seen/DOM 的总资源有界。

## 网页连接与刷新

每次建立连接都创建新的 owner，替换时先取消旧 owner 和订阅。认证完成后先获取 P，再启动 status、conversation、sessions、notifications、uploads 事实读取。只有当前 owner 可以采用结果及启动流；全局流从 P 继续，会话流仍使用 conversation 提供的恢复游标。位置不可用时显示断连，不偷偷回退到全部历史。

主会话快照在一个无 await 步骤内采集 `live` 与全局 `after`，先由历史恢复文本，再用真实执行槽与有效审批 future 校正当前事实。会话流严格从该 `after` 继续，reducer 的快照下界只过滤后来的旧 SSE 帧，不过滤快照内部历史。启动时实际校正的状态与窄 `session.recovered` 同事务保存，已有页面跨服务重启仍可在原游标后结束旧运行和审批；详见[会话恢复技术归档](../devarchive/session-recovery-design.md)。

sessions 与 notifications 各保留一次在途读取和一个共享后继轮次。事件突发只把后继轮次标记为需要读取；显式调用得到覆盖自身触发的轮次结果，不等待未来事件安静下来。读取失败结束当前尝试，等待真正的新触发才重试。退出连接立即结算等待者并丢弃迟到结果，旧请求的 401 不退出新登录。

重连重新验证当前可见的工作流、通知、团队、记忆和上传页，以及打开的任务只读详情；编辑草稿保持，过时证据失效。记录目录仍存在时只标记更新，页面离开已经释放目录时恢复已提交的筛选条件；普通全局事件不重载用户正在浏览的页。pagehide 关闭订阅，持久页面返回、联网或重新可见时按当前连接状态恢复。

原始 SSE `id` 在当前帧存在时优先于 JSON envelope 的数字 ID；空字符串明确重置位置，完整的仅 ID 帧也更新下一次恢复位置，不生成业务事件。带 NUL 的 ID 忽略，未完成帧不推进外部恢复位置。旧 envelope-only 格式保留兼容。

归属范围是连接启动、订阅与其派生读取，不包含全部原有业务 mutation 或 settings 通用请求。长期主会话 snapshot 和浏览器缓存的总预算仍是后续独立切片。
