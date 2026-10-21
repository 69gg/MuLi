# chiliarch 原生接入

MuLi 作为一个配对 agent 主动连接 chiliarch。后台通过 SSE 接收发送给此身份的消息，使用 POST 上报心跳与业务 ACK。网络等待和重连不会调用模型，chiliarch 服务端也无需反向连接 MuLi。

## 连接自我介绍（chiliarch 0.5.0）

chiliarch [0.5.0](https://pypi.org/project/chiliarch/0.5.0/) 已于 2026-10-20（北京时间）发布。[发布流水线](https://github.com/69gg/chiliarch/actions/runs/39295005766) 的六项作业通过；对应签名提交 `068a6b6` 与标签 `v0.5.0`。发布前完整 CI 覆盖 Linux/macOS/Windows 与 Python 3.11/3.12/3.13，PyPI 和 [GitHub Release](https://github.com/69gg/chiliarch/releases/tag/v0.5.0) 的两个发行包 SHA-256 一致。这证明发行与测试结果，不代表已替用户安装或完成真实配对。

每次连接和重连的首个心跳都会发送 `payload.metadata.introduction`，之后随正常心跳更新。介绍优先取 `persona.introduction`，为空时从 `persona.name` 与 `persona.user_name` 构造“我是……，……的私人数字助手”及统筹职责说明；不会额外调用模型。主 MuLi 可通过已有配置管理工具修改 persona.introduction，配置校验和重新连接流程沿用原机制。

介绍为最多 4000 字符的纯文本，不包含配置密钥或会话正文。chiliarch 保存并在 agent 管理界面和总管工具结果中展示，内容属于自行声明的资料，不能授予权限。软件 0.5.0 的协议版本仍为 `1.0`；配对请求格式不变，旧 chiliarch 服务端仍接受已有 metadata 扩展，旧客户端缺失字段仍合法。其他客户端可省略表示保留、传空串或 `null` 表示清除；MuLi 正常运行会持续上报当前人格介绍。

独立桥接器可设置 `ChiliarchConfig(..., introduction="...")`。连接自述、心跳与网络重连均为确定性后台操作，不使用 AI 轮询。

实现位于 `src/muli/integrations/chiliarch.py`，只依赖已有的 HTTPX 和 Python 标准库。HTTPX 的异步流使用 `AsyncClient.stream()` 上下文，退出后关闭响应；外部注入的 HTTPX client 仍由调用方负责关闭。[HTTPX 官方异步文档](https://github.com/encode/httpx/blob/master/docs/async.md)

## 接口与认证

下表依据 chiliarch 当前协议 1.0、`api.py`、`transport.py`、`auth.py`、`protocol/__init__.py` 和 `service.py` 核对。

| 用途 | 接口 | 认证与请求 |
| --- | --- | --- |
| 配对 | `POST /api/agent/pair` | 一次性配对码；JSON 为 `code`、`name`、`machine`、`capabilities` |
| 接收消息 | `GET /api/agent/events` | `Authorization: Bearer <agent credential>`；SSE |
| 上行与 ACK | `POST /api/agent/messages` | 同一 Bearer；完整协议信封；返回 `{ack, duplicate}` |
| 完整协议 Schema | `GET /api/protocol/schema` | 无须认证 |
| 管理端登录 | `POST /api/auth/login` | JSON `{password}`；返回 CSRF token，设置 `chiliarch_session` Cookie |
| 管理操作 Schema | `GET /api/operations` | 用户会话 Cookie |
| 管理操作 | `POST /api/operations/{name}` | 用户 Cookie + `X-CSRF-Token`；JSON `{arguments, idempotency_key}` |
| 总管会话事件 | `GET /api/chat/conversations/{conversation_id}/events?after=...` | 用户 Cookie；与 agent SSE 分开 |

配对成功返回 `{agent_id, credential, protocol_version: "1.0"}`。凭据只返回一次，应由 MuLi 配置存储保存并限制文件权限。`pair()` 不自动重试；请求断开时配对码可能已经被消费，不能假定未配对。

管理端 Cookie 不等于 agent credential。agent Bearer 不能用来调用管理端 HTTP 接口。生成配对码使用管理操作 `create_pairing_code`；其 `ttl_seconds` 默认为 900，允许 60–86400。管理员可通过原生 `revoke_agent` 操作吊销身份。

MuLi 当前桥接器只实现配对、agent SSE 与 agent POST。chiliarch 自身另有 `/api/agent/ws` 和 `/api/agent/poll`，桥接器不以 AI 轮询代替 SSE，也不自动切换到这些传输。配置地址可以带反向代理路径前缀；例如配置 `https://example.invalid/chiliarch` 后，请求路径为 `/chiliarch/api/agent/events`。

## 事件边界

agent SSE 使用 `event: message`，`id` 为协议信封 ID，`data` 为完整 JSON；注释行用于保活。`event: revoked` 表示凭据失效，桥接器立即停止并暴露错误。

信封包含：

```json
{
  "id": "stable-message-id",
  "type": "agent.message",
  "correlation_id": null,
  "protocol_version": "1.0",
  "timestamp": "2026-10-18T12:00:00Z",
  "expires_at": null,
  "payload": {"message": "任务已有进展", "metadata": {}}
}
```

可接收的原生消息包括：

| 类型 | 关键 payload 字段 |
| --- | --- |
| `task.dispatch` | `task_id`、`instruction`、`parameters`，可选 `workdir` |
| `task.cancel` | `task_id`、`reason` |
| `agent.message` | `message`、`metadata` |
| `request.resolved` | `request_id`、`status=approved/rejected/failed`、`result`、`error` |
| `collaboration.message` | `message_id`、来源与接收身份、`message`、`sequence`、可选任务/频道关联、`wake` |
| `native.response` | `interaction_id`、`task_id`、`status=answered/expired`、`answer` |
| `repository.command` | `command_id`、`action`、`arguments`；MuLi 未实现相应执行器时不能声明支持该能力 |

这个流只投递给 MuLi 身份。当前 chiliarch 没有通用的 `/external-events` 或 `/workflows` HTTP 接口，也没有向任意 agent 广播全平台事件的订阅 API。`operation.executed`、`task.updated` 等服务事件属于服务进程内 listener；总管会话 SSE 则是另一条 Cookie 认证的 UI 事件流。不要把 agent SSE 的连接成功描述为“已订阅全平台通知”。

## 桥接组件接口

```python
from collections.abc import Awaitable, Callable

from muli.integrations.chiliarch import (
    ChiliarchBridge,
    ChiliarchConfig,
    Envelope,
)


def make_bridge(
    base_url: str,
    credential: str,
    machine: str,
    persist_envelope: Callable[[Envelope], Awaitable[None]],
) -> ChiliarchBridge:
    config = ChiliarchConfig(
        base_url=base_url,
        credential=credential,
        machine=machine,
    )
    return ChiliarchBridge(config, persist_envelope)
```

- `await bridge.pair(code) -> PairingResult`：配对后更新该桥接器的内存配置，调用方另行保存 `agent_id` 与 `credential`。运行期间禁止换身份。
- `await bridge.run() -> None`：运行单个 SSE 接收循环及定时协议心跳。调用方保留 task 并观察其异常。
- `bridge.stop() -> None`：唤醒停止信号；随后等待运行 task 结束。HTTP 流和心跳均会清理。
- `await bridge.send(envelope) -> DeliveryReceipt`：发送一次完整上行信封，校验返回 ACK 是否匹配。不会自动重试业务副作用。
- `bridge.status`：`stopped/connecting/connected/retrying/error`；`last_error` 为不包含响应正文或凭据的简短错误。

`ChiliarchConfig` 字段：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `base_url` | 必填 | chiliarch HTTP(S) 地址，可带路径前缀 |
| `credential` | 空 | 已配对身份的 Bearer 凭据 |
| `agent_id` | 空 | 已配对身份 ID，供应用展示与关联 |
| `name` | `MuLi` | 配对显示名称 |
| `machine` | 空 | 配对时必填，由调用方提供机器标签 |
| `capabilities` | 空 tuple | 只声明已经实现的能力 |
| `heartbeat_seconds` | 15 | 协议心跳间隔，不调用 AI |
| `request_timeout_seconds` | 15 | POST 与建立连接的超时 |
| `stream_timeout_seconds` | 60 | SSE 连续无字节读取的超时 |
| `reconnect_initial_seconds` | 1 | 初始重连等待 |
| `reconnect_max_seconds` | 30 | 最大重连等待，包含有界抖动 |
| `max_message_bytes` | 1048576 | 上行信封及单个 SSE 帧的大小上限 |

网络中断、408/425/429 与服务端 5xx 会退避重连；401/403、协议错误、冲突、超限及其他非成功响应停止运行。不会跟随重定向传递凭据。SSE 数据按 LF/CRLF/CR 解析，支持分块 UTF-8、多行 data，并限制未结束行的累计大小。配对及上行 ACK 的 HTTP 响应也流式读取，不能超过 `max_message_bytes`。

## 与本地 Runtime 的接线

`src/muli/integrations/manager.py` 提供 `IntegrationManager(runtime)`：

- `await start()`：配置启用且已有凭据时启动 SSE、持久任务 worker 与 outbox sender。空闲等待 `asyncio.Event`，不会调用模型或轮询任务。
- `await close()`：停止接收、取消并等待本机执行器完成清理，再停止发送；未发出的业务回执保留在 SQLite。
- `await pair(code, name=None)`：调用配对接口，成功后将身份与凭据写入受限配置并启动连接。已有 chiliarch 任务运行时禁止更换身份；失败不自动重复消费配对码。
- `status()`：返回连接状态、队列及 outbox 数量、安全错误说明，不返回 credential。

应用生命周期调用 `start/close`；管理 API `POST /api/integrations/chiliarch/pair` 接收 `{code,name}`。配置的 `chiliarch` 段包括 `enabled/base_url/agent_id/credential/name/machine/max_queued_tasks`。`machine` 留空时使用本机主机名，任务排队上限默认 100；心跳和网络限制由桥接默认值控制。

接到 `task.dispatch` 后，原始信封、待执行记录和 `accepted` 回执在同一个 SQLite 事务中提交。任务执行前保存 `running` 与会话关联；终态及其回执也同事务提交。回执保存在 outbox，断网后按原 ID、timestamp 和 payload 重发；服务确认对应 ID 后才删除。

去重有两个层次：同身份的 envelope ID 必须内容一致；同一 task ID 的指令 payload 也必须一致。重复分派不会再运行。`task.cancel` 可先于分派到达，这时先保存取消记录，后续分派不会执行。执行中的取消会传递至本机任务，并在清理完成后记录终态；重复取消不能打断清理提交。

身份范围由服务地址和 agent ID 共同决定。换服务或重新配对为另一身份后，不执行旧身份的排队任务，也不把旧回执发送给新身份。重启时 `running/cancelling` 标为 `interrupted` 并上报，不重复可能已产生的副作用；尚未开始的 `queued` 任务可以继续。

外部任务使用本机 `default_mode`。远端 `parameters.mode` 仅为不可信资料，不能升级为 `yolo`。`workdir` 必须是本机允许目录中的现有绝对目录，仅作为任务资料传给模型，不修改进程 cwd，也不声称提供仓库沙箱。未派任务的 `agent.message/collaboration.message` 默认只进入收件箱，远端 `wake=true` 本身不授予提醒或模型权限。未实现的 `repository.command` 明确返回失败，不声明仓库命令能力。

## 普通消息提醒（默认关闭）

主人可用 `chiliarch_attention_status` 读取状态，以 `chiliarch_attention_configure({"enabled":true})` 开启当前配对身份以后新消息的提醒；关闭传 `false`。配置工具为 write 风险，只允许主人主会话调用，沿用正常审批。等价管理 API 为 `GET/PUT /api/integrations/chiliarch/attention`，PUT 的完整 JSON 仅为 `{"enabled":true}` 或 `{"enabled":false}`；沿用管理页登录、同源、JSON、配置锁及维护门。此操作只保存本地策略，不配对或连接服务；需要已经保存有效接入配置。

策略保存为独立 SQLite 对象，主 `Settings` 和 `attention.events` 三项不变。状态返回当前 scope、revision、实际开关和固定原因；身份变化后旧策略不自动应用于新身份，坏值或未知版本不会自动重置。旧版读取主配置仍兼容，但不会执行这项新提醒功能。

开启后，仅首次持久接收、正文为非空白字符串的两类普通消息可成为注意事项。inbox、通知和事件在同一事务中提交后才 ACK；重复只唤醒已有事件，不重建通知或重新授权。通知稳定 ID 绑定接收连接的真实 scope，同 ID 换身份也不会覆盖或冲突。开启不会补授权旧消息，远端 `wake/metadata` 不能改变策略或来源。

提醒复用 [Attention](attention.md) 的自然空闲等待、游标和去重凭据：外部正文明确标为资料，每条最多 4000 字符且服从当前正文预算，仅开放读取工具，不运行任务、不改配置、不自动回信 chiliarch。需要总 `attention.enabled` 开启；主模型未配置时仍可保留一批已接纳资料。首次接收时按 `channels.proactive` 冻结默认或显式记忆绑定的目的地，默认 WebUI；后续不从消息猜地址，也不会改发给新默认。已接纳资料保持原历史身份，不撤销所有既有 ingress/outbox。

来源核验使用固定主键与 SQL 字节上限，不先加载完整 payload 再截取。坏策略或过大证明只使本 producer 无法自动提醒，原入站 ACK 和其他提醒不受影响。实现、预算及本地验证边界见 [chiliarch 提醒设计](chiliarch-attention-design.md)。

## 持久化与安全重放

`on_envelope(envelope)` 的成功返回必须表示：

1. 在同一个 SQLite 事务中保存原始信封及待处理业务工作。
2. 按 `envelope.id` 去重，并检查同 ID 的内容一致；重复投递不能再次产生外部副作用。
3. 提交完成后才返回。callback 只负责持久接收，模型调用与工具执行由持久队列驱动。

桥接器在 callback 成功后发送 `ack`，其 `payload.message_id` 和 `correlation_id` 均关联原信封 ID。callback 失败或被取消不发送 ACK；下次投递仍使用原身份。ACK 失败也可能发生在本地提交之后，因此 callback 必须支持这种重投。

SSE `Last-Event-ID` 不是 chiliarch 的业务 ACK，桥接器不据此跳过消息。ACK 只代表持久接收，不代表任务已执行或已经通过审查。任务执行器必须另行发送 `task.status`，并区分 `accepted/running/completed/failed/cancelled/interrupted`。

上行业务同样应先保存完整 `Envelope.to_dict()` 与待发送状态，再调用 `send()`。连接异常意味着结果未知，应以相同 ID、timestamp 和 payload 重试，不能生成新 ID 重复副作用。`Envelope.from_dict()` 可恢复原信封。心跳和 ACK 是瞬时控制帧，不需要永久累积。

## 工作流对接

chiliarch 的工作流能力使用原生目标、任务与验收操作：

- `create_goal`：`title`、`objective`、`constraints`、`acceptance_criteria`，可选 `conversation_id`。
- `dispatch_task`：`agent_id`、`instruction`，可选 `goal_id`、`depends_on`、`acceptance_criteria`、`workdir`、`parameters`、`continue_task_id`。
- `review_task`：验收最新已执行完成的任务；前置任务通过验收后才释放依赖。
- `get_task/get_goal`：在事件指向具体结果或用户询问时读取事实；不需要让模型定时循环查询。

agent 身份申请操作时发送 `agent.request`：`payload={request_id, operation, arguments, message, metadata}`；结果从 `request.resolved` 返回。`request_id` 是稳定业务身份，与 envelope ID 分开。并非每个操作都会立即执行，权限与审批仍由 chiliarch 统一入口处理；请求已接收、审批已通过、执行已完成与结果已验收是不同状态。

MuLi 的 `ask/auto/yolo` 应在本地统一工具入口落实，不能让外部事件或下级 agent 绕过它。它与 chiliarch 的审批模式分别生效；尤其 MuLi 的 ask 是每次工具调用询问用户，而 chiliarch 当前 ask 只针对会改变状态的操作。

## 验证

`tests/test_chiliarch.py` 使用 HTTPX MockTransport 和临时 SQLite，覆盖配对格式、路径前缀、持久化早于 ACK、重复投递、存储失败后的重投、吊销与永久错误停止、禁止重定向泄露凭据、快速停止、流式帧解析与大小限制。测试不会连接或修改真实 chiliarch，也不会调用模型。

`tests/test_integration_manager.py` 使用假 Runtime 和假桥接器验证 task ID 去重、先取消后分派、重复取消清理、本机权限继承、原信封重发、重启不重做、身份切换隔离、过期/越界目录拒绝，以及通知不擅自唤醒模型。自述回归额外覆盖人格默认值、自定义文本、长度边界与每次重连上报。

0.5.0 联动开发另使用临时配置、临时 SQLite 和本机临时 TCP 服务完成 MuLi `IntegrationManager.pair()` → 真实 chiliarch 配对 → SSE 连接 → 首心跳自述持久化验收，全程禁止模型调用。chiliarch 端另覆盖 WS/SSE/长轮询及重连。用户实际部署的凭据、反向代理和生产任务尚未联调；这些隔离验收不会修改或启停用户服务。
