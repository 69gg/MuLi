# Team 后台汇报保留原始会话

状态：设计与实施切片。基线为 `9621ab4`；本片不修改版本号，不代表真实外部渠道已经验证。

主人从渠道 A 派出队友，随后在 WebUI 或渠道 B 继续交谈时，该队友的后台报告仍应回到 A。MuLi 保持一个持续主身份；改变聊天入口不改变既有任务的回复目的地。当前 `Runtime._delivery_routes` 只参考最近的主人消息，`agent`、`team_report` 和报告的 ingress 未保存任务来源，因而不能保证这一行为。

## 第一期合同

本片只实现 Team 原路汇报以及这一行为所需的队列和 outbox 状态。默认主动渠道、从记忆偏好选择目的地、Workflow 自动汇报均不包含在内。来源不明的旧任务、旧报告和直接宿主调用留在 WebUI；不能猜最近使用过的外部渠道。

复用现有 `PreparedRequest`、ingress、`agent`、`team_report`、归档消息及 `channel_outbox`。不增加另一个发送账本，不启动模型轮询，不把同一段 assistant 输出广播给多个原渠道。token delta 仍经现有 SSE 流送达 WebUI；外部渠道仍按完整消息投递。

### 宿主来源与权限分开

增加一个小型不可变 `ReplyOrigin` 值，持久形式为：

```json
{"kind":"channel","input_id":"已接收的主人输入ID","route":{"name":"telegram","account_id":"bot","kind":"private","target":"100","sender_id":"100"}}
```

`kind` 为 `channel`、`web` 或 `unattributed`；后两者没有外部 route。`input_id` 关联最初输入；多个输入只有完整目的地相同时才可合并处理，绑定仍保留具体输入 ID。route 只使用现有渠道字段及其有限 JSON 预算，不含正文、凭据或模型可编辑的路由建议。值在内存中使用冻结字段和规范 JSON 字符串，交付时解码为新对象，避免冻结 dataclass 仍持有可变字典的漏洞。

Conversation/Store 的输入路径仅从已验证的 owner 入站构造来源，或由宿主显式传入已捕获的 `ReplyOrigin`。owner ingress 保留既有序列化形式，从持久 source/principal/route/ID 推导；这样升级后重投旧事件不会因附加字段误报幂等冲突。新增来源参数不出现在 Team 工具的 JSON schema 中。任意队友正文、工具参数及外部资料都不能给自己写 owner 身份或来源。历史无字段的 owner 渠道输入可利用它本来就持久保存的 source/principal/route；没有这种原始证据的报告必须本地处理。

`PreparedRequest` 在任何 context hook、模型请求及其它 await 前冻结来源。其 `principal` 判定、`TaskSnapshot`、模式与工具授权继续独立工作；报告带着 A 的来源不等于报告成为主人指令。取消、暂停、未知副作用和旧 epoch 的权限边界均保持现有合同。

### 来源绑定到任务，而非最近聊天

主模型调用 `spawn_agent` 时从当前 `PreparedRequest` 捕获来源，随 session/agent/初始 ingress 一起提交。子队友直接继承父 agent 的持久绑定。队友完成及排队暂停时，结果与同一来源在现有 `team_report` 原子提交；报告投递父会话时，来源与 `team_report.status=delivered` 在同一个 ingress 事务保存。

`send_message`、`resume`、重启及重规划不会静默换绑原任务。后来的 B 渠道补充消息不能改写 A 的任务来源。此切片没有改绑工具；需要一个新任务来源时创建新队友。持久队友身份仍可继续此前工作，其报告保留首次任务来源。

### 队列在来源变化处分段

只给 `agent` 加字段仍不正确：现有 `Store.claim_inputs` 会一次取走多个来源的 pending 输入。A 的报告与 B 的消息混在一段模型回复中，无论选 A、选 B 或广播两边都会违反合同。

因此在现有有限 FIFO 候选窗口内，仅 claim 连续同一 `ReplyOrigin` 的前缀；不同来源仍为 pending，等下一个现有工具回放安全边界。相同原始输入派出的多个队友报告可以合并，不同来源不会进入同一个新输入批次。历史仍属于同一个持续主身份；本片承诺的是宿主对新一批回复的目的地归属，不声称模型会忘掉此前聊天资料。

每个 `PreparedRequest` 和该请求产生的完整/中断 assistant 消息固定一个来源。后来的排队消息不能改变已经开始流送的回复。不会重跑已执行工具；未开始调用仍按既有 pending-input/暂停规则处理。达到现有运行步数预算后，剩余输入继续留在持久队列，由既有 driver 调度，不增加无界循环。

### 送达和阻塞仍由既有 outbox 负责

只使用 assistant 消息的一个 `delivery_routes` 值和稳定消息 ID。已有 account/owner/group 校验在入队与实际发送前继续生效，不能用来源绕过已撤销授权。原渠道关闭、账号改变或路由失效时，不换发到默认或最新渠道。

事件消费者不能继续静默跳过 inactive route。为这类自动回复在同一 `channel_outbox` 中保存 `blocked` 状态、原因和稳定投递 ID，供现有渠道状态/投递接口查询；不进入自动发送查询，也不在配置再次启用时偷偷重发。只有原路由重新验证通过且主人明确重试，才沿既有手动重试路径继续。非法来源元数据留在本地，不把未验证的字典送给适配器。

已有 `sending -> unknown` 的重启/取消语义保持；同一 ID 再次消费不能把 `unknown`、`sent` 或 `blocked` 改回 pending。`team_report.delivered` 仅指父会话已持久收件，不等于外部渠道已送达。未知结果不自动重发，也不通过换幂等键规避。

## 写入点与兼容

不需要全库回填或启动扫描。新增写入在原有事务里附加有限字段；读到旧对象时使用本地来源。保留现有 schema，outbox 的状态字段原本就是文本。所有 outbox 失败/阻塞记录保留在现有有限管理查询中。撤回本片代码不会删除既有渠道账本；旧代码也不会主动选择 `blocked` 记录发送。

SQLite `Connection` 的 context manager 是提交/回滚边界，不提供嵌套事务；实现继续沿用 Store 的同步事务写入方法，不在新辅助函数里加会提前提交的嵌套 `with db`。`ContextVar` 的 task context 副本不使其保存的可变值自动不可变，因此来源值自身也必须冻结。[Python sqlite3](https://docs.python.org/3.13/library/sqlite3.html#how-to-use-the-connection-context-manager)、[contextvars](https://docs.python.org/3.13/library/contextvars.html)。以上 API 已通过 Context7 核实。

## 实施范围与验证

产品文件限于新 `src/muli/reply_origin.py`、`conversation.py`、`store.py`、`runtime.py`、`team.py`、`channels/manager.py`；`web/app.mjs` 和 `web/management.mjs` 仅为 `blocked` 补齐中文状态与既有显式重试入口。必要回归放新 `tests/test_report_origin.py`，以及现有 conversation/management 测试中与合同直接相关的部分；文档同步本设计、`docs/channels.md`、`docs/architecture.md` 和 README 相关段落。若实作证据要求额外文件，会先向根任务说明。

回归采用临时用户目录、离线假模型、假适配器、确定性事件门控和真实 Store 重开：

- A 派工后 WebUI/B 插入，模型 await 期间新输入不得改写当前回复或队友来源。
- 两个来源同批分段、同来源报告可合并，祖孙队友继承，busy send/resume 不改绑，旧无来源留本地。
- 报告的结果/来源与入队/交接原子性；Store 重开、重复事件消费不重复发送。
- owner/account/group 撤销及 disabled 渠道产生持久 blocked；启用后不自动重试，显式重试仍复核授权。
- outbox unknown 不自动重发；来源不改变 principal、TaskSnapshot、ask 模式或暂停守卫。
- 现有单请求 route 快照、Team 收件/暂停/恢复和渠道幂等回归保持。

全部运行通过统一 `scripts/check_local.py`，与其它 agent 串行协商，单次 1 GiB、共享 2 GiB、swap 0、共享 1 CPU。先完成相关小范围与静态检查，再交根任务整合；真实账号、平台回复时限、真实服务重启均不在本机实验范围。
