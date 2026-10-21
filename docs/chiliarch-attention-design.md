# chiliarch 普通消息的可选 Attention

本片只把 `agent.message`、`collaboration.message` 的有限正文作为外部资料提醒主人，不运行任务、不自动回复 chiliarch。原有 `task.dispatch`、ACK 和任务权限保持原合同。实现与专用回归已经完成，源码和关键回归经独立只读审查；真实服务验收仍未进行。

## 策略与兼容

默认关闭。固定 `objects(integration_attention_policy, chiliarch-v1)` 保存版本、开关、宿主当前配对 scope 与每次替换的新 revision；缺失或身份不匹配不启用，坏值或未来版本显示固定不可用原因且不能覆盖重置。主 `Settings`、`AttentionSettings.events` 的三个 Literal 值和配置 JSON 不变。旧控制器可继续读取主配置，只忽略新增对象和消息来源；这不等于旧版实现了此功能，也不改变七领域合同的既有权限、路由或 unknown 语义。

管理入口为 `GET/PUT /api/integrations/chiliarch/attention`。PUT 只接受严格布尔 `enabled`，scope/revision 由宿主注入。`chiliarch_attention_status` 为 read 工具，`chiliarch_attention_configure` 为 write 工具；配置工具限定主人主会话，复用既有审批、configuration_lock、关闭与维护门。HTTP 复用主人认证与同源 JSON 边界；被动实例可读策略，不能修改。策略成功替换后通知事件总线，主配置文件不变。保存不配对、不连接远端、不启动模型。总 `attention.enabled` 仍为总闸。

## 原子接收与有界来源

首次接收在同一 SQLite 事务中保存 inbox、notification 和 notification.created，复用 `Store._event`，不嵌套公开 `Store.event/put` 提前提交。事务成功后只 `bus.notify`；重复同 ID 同内容也可 notify，但不新建记录或事件，丢通知返回后重投可唤醒原事件。提交失败不 ACK。

仅策略在首次接收时有效且两类消息的 `payload.message` 是非空白字符串，才冻结有限宿主来源证明、策略 revision 和 `FrozenRouting.independent`。原 payload 完整保留，Attention 只获得有限节选；远端 metadata/wake/收件人建议不能修改策略或授权。通知 ID 始终使用本次接收的不可变 scope 与原 envelope ID 的稳定 hash，inbox 与协议 ID 不改，兼容长 Unicode ID。旧连接收尾时，即使 Settings 已换身份，也不会占用新身份的通知主键；身份不匹配仅使新提醒证明无效，不丢掉原身份收件。开启不回扫旧通知或补发授权。

来源适配器按固定主键、SQL length/CASE/substr 边界读取证明及正文，不先把完整原 payload 加载到 Python 再截取。策略对象最多 4096 字节，正文节选最多 `min(attention.max_chars, 4000)` 个字符，完整来源证明最多 32 KiB；原 inbox 的核验沿桥接默认 1 MiB 信封上界，canonical 通知另有固定有限上界。超限不授予提醒资格，不能因为大字段先把完整 JSON 搬到 Python。核对真实 inbox、canonical notification、宿主字段和当前策略。坏策略、畸形或过预算资料只禁用本 producer 的自动提醒，原收件箱、ACK 与其他提醒仍可用；原始异常正文或凭据不进入状态输出。

## 生命周期与路由

复用 Attention 的 cursor、receipt、批量/正文预算、自然空闲等待与 ask/read-only 权限，不新增表、队列、轮询或模型调用入口。逻辑去重键包含真实 scope 与 envelope ID，不按正文猜测重复。

coalesce/等待空闲/等待配置锁后，AdmissionGuard 在 configuration_lock 内从当前 Settings 重算 identity scope 并重读策略 revision，旧 manager 的 `_scope` 不能单独充当当前身份。配对或策略变化后未接纳旧消息失去资格。已接纳事项保留当时历史身份，不重新归属新身份；本片不撤销所有既有 ingress/outbox。

默认路由在首次接收时冻结，之后沿既有 `FrozenRouting` 的发送前撤销与记忆证据核验；不得从 chiliarch 消息推断地址。全局默认目的地变化只影响未来消息。

## 验收范围

1. 真实 SQLite 的事件写入失败全事务回滚；提交后通知失败再重投仍只一份通知/事件。
2. 真实 Attention 与假 provider 在自然空闲后唤醒一次；关闭、旧记录、重复消息不唤醒。
3. 等待期间换 scope/revision，旧资料不以新配对身份接纳；不同 scope 的同 ID 不冲突。
4. 长 Unicode ID、有限正文、只读工具及冻结默认路由；远端 wake/metadata 不授权。
5. 坏/未来策略不重置、不拖其他 reminder；管理入口真实鉴权、锁、关闭门及主配置 bytes 不变。

## 本地验证

旧实现的真实 SQLite 反例已稳定失败：事件写入拒绝触发器未被调用，入站及通知已提交。修复后 `tests/test_chiliarch_attention.py` 与 `tests/test_chiliarch_attention_management.py` 共 37 项通过（1.38 秒，守卫峰值 147.7 MiB、swap 0），包括事件失败整体回滚、通知丢返回后重投、自然空闲与只读工具、coalesce/配置锁期间策略失效、不同身份同 ID、坏或过大证明隔离，以及 HTTP 认证/同源/严格输入/关闭门。

既有 `test_integration_manager.py`、`test_chiliarch.py`、`test_attention.py`、`test_system_events.py`、`test_proactive_delivery.py`、`test_application_integrations.py` 六文件共 134 项通过（1.75 秒，峰值 136.6 MiB、swap 0），与新文件不重叠。8 个修改源码通过严格 mypy（峰值 174 MiB、swap 0），10 个 Python 文件通过 Ruff 与格式检查。全部检查经共享资源守卫串行执行，每次硬上限 30 秒；没有真实 chiliarch、账户、渠道外发或模型调用，本轮未执行远端平台矩阵。
