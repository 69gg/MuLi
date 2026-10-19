# 任务控制持久层（2.3 开发中）

本页描述 `muli.task_control` 的同步领域层。它已经保存 owner 操作、主助手确认和可靠通知所需事实；**单独安装这一层不会暂停模型或工具，也没有新增可用的 Web 按钮/API**。Runtime 的安全边界、团队调度、workflow 准入和认证端点必须接线并通过对应测试后，才能声明任务控制生效。

它复用 SQLite objects、events、ingress 和 message_order，不启动模型、后台 worker、网络请求或定时重试，不增加依赖。所有方法同步完成，事务内没有 await。宿主负责真实身份认证、`configuration_lock`、配置预算和提交后的事件通知/唤醒。

## 持久状态与范围

旧 commitment 没有 control 时按 epoch=approved_epoch=0 读取，不迁移重写全部历史。首次 pause 建立：

```json
{
  "epoch": 1,
  "approved_epoch": 0,
  "replan_request_id": null,
  "plan": null
}
```

epoch 大于 approved_epoch 表示未获当前计划确认。既有 commitment.revision 用于所有展示修订；进度编辑不改变 epoch，批准同一 epoch 仍推进 revision。纯 pause 清除 replan_request_id，不产生模型输入；没有当前真实 owner request_replan 回执，main 不能批准。

受控 commitment 的完整 brief 不再接受 generic `commitment_save` 替换。该入口仍可更新进度和证据描述，并完整保留 control；要求只能由 owner 控制入口追加。确认只写 plan，不改 goal/constraints/evidence_refs。原始要求不因压缩、计划文字或协作消息而撤销；本层不证明自然语言计划在语义上满足每条要求。

scope 包含同一 commitment 的所有根队友和后代。`resolve_scope(session_id)` 沿真实 agent/workflow 父链解析，workflow 节点通过 session.workflow_run_id 查运行，兼容没有显式 commitment_id 的旧 workflow。父链缺失、绑定冲突或环明确拒绝，不降级成无范围。

首版不实现活动 DAG 中途暂停：每次 pause 都自行在事务中查询持久 pending/queued/running workflow；宿主传入的 active_workflow_ids 仅补充仍在收尾的实际 handles，不能替代持久查询。该 scope 有活动 DAG 就返回 workflow_active，状态完全不变。当前 epoch 获批准后可以开始新 DAG；B 接线必须在首次 await 前持久登记继承绑定和活动运行，并在节点请求/工具调用上使用同一 scope/epoch 门禁。独立 DAG 不受无关任务控制。

## 同步接口

构造 `TaskControl(store, commitments)`，二者必须共用 Store。调用方只向真实已认证 owner 暴露 owner_control；Python 插件是用户信任的完整本地代码，本接口不是恶意插件沙箱。

| 方法 | 合同 |
| --- | --- |
| `owner_control(id, OwnerControlRequest, *, main_session_id, mode, model_configured, max_pending, active_workflow_ids=())` | pause/request_replan 短事务；返回 ControlResult；不通知或调用模型 |
| `current(id, *, active_workflow_ids=())` | `{id, revision, control, scope}` 紧凑投影；control 不含 plan 正文，正文保存在 canonical commitment |
| `execution_snapshot(session_id)` | 返回真实执行范围的不可变 TaskSnapshot；无绑定的持续 main 返回 None |
| `replan_snapshot(main_session_id, id)` | 只为实际持续 main 和有效 owner 重规划意图提供选中快照 |
| `is_current_replan(snapshot)` | 选中快照已获批准、被新的 pause/replan 替换或事实失效时返回 False；宿主必须清除临时选择 |
| `guard_execution(snapshot, *, session_id, risk)` | read 不受 epoch 暂停门禁阻断；副作用要求快照及当前 epoch 均已批准且相同；本方法不代替原有权限检查 |
| `approve_plan(snapshot, observed_epoch, plan, targets, *, caller_session_id, max_targets)` | 真实 main、宿主已展示快照、当前 owner 意图及 epoch 校验；确认与目标 outbox 同事务 |
| `pending_deliveries(limit, *, after_id=None)` | 按稳定 ID 有界分页；消费者可越过暂时满箱的目标，不能总从首项重试造成饥饿 |
| `deliver(id, *, max_pending)` | 同步检查并投递一条计划通知；返回 DeliveryResult，不自行启动任务 |
| `resume_notified_target(target_session_id)` | 只读核验同代次当前计划的真实通知；供 Runtime 在已判 paused 的完成边界决定是否立即排入新请求 |

`OwnerControlRequest(action, expected_epoch, request_id, additional_requirements="")` 只支持 pause/request_replan，expected_epoch 不接受 bool。request_id 使用现有入站 ID 长度边界。pause 不能夹带修改要求；request_replan 可追加一条完整非空要求或空文本重新规划。

`ControlConflict` 与 `ControlUnavailable` 都提供 `code` 和纯中文 `message`；str(exception) 同时包含 code，供日志使用。HTTP 层建议映射为 `detail={code,message,current?}`，不得把纯字符串错误误当控制已提交。

`TaskPaused` 是专用领域结果，包含 `observed_epoch`。Runtime 接线必须先给同一模型批次尚未执行的 call 写完整 not_executed results，再将 run/agent 停靠；不得被普通工具错误捕获后继续生成“成功”，也不得套用失败分支递归取消所有后代。

## 请求快照与权限

`TaskSnapshot` 保存 session_id、commitment_id、revision、epoch、approved_epoch、replan_request_id，以及不可变序列化的 brief/plan。`context()` 从同一快照输出完整说明和明确的非授权提示；不在 context hook await 后给旧正文换上新 epoch。

Runtime 必须区分 `PreparedRequest.execution_scope` 和 `selected_replan`。main 本身仍是跨任务协调者，选中一个暂停任务不能使主助手的确认工具和其他任务协调被通用执行门禁误拦。选中状态只供当前 main 请求；批准或 owner 意图失效后清除，避免旧的大 brief 占用无关主聊天预算。

执行请求在进入模型前和取得 capacity 后检查 scope；副作用工具在审批前检查一次，在所有 await（包含 tool.started 事件）之后、进入 handler 前再同步检查一次。旧请求的 call 即使晚于新 epoch 确认返回，也不能执行。`guard_execution(..., risk="write")` 可用于非只读模型轮次准入；主协调的 selected_replan 单独在 approve_plan 内验证。

scope binding、main 身份和请求快照均来自宿主。模型仅提供 observed_epoch，不能借猜中当前整数替换它实际看到的旧快照。ContextVar 的设置与 finally reset、真实 caller 校验、工具 schema 和 handler 二次防护由 Runtime/LocalServices 接线负责。

## 原子操作与幂等

控制事务使用 BEGIN IMMEDIATE，不允许嵌套在另一个提交事务中；失败完整回滚。顺序为真实宿主身份检查、规范化请求、回执查重、CAS/容量/预算检查、完整提交。回执命中早于 expected_epoch CAS，也早于新配置下的容量/模型可用性检查。

- pause：epoch+1、清 replan_request_id、回执、commitment_revision 与紧凑事件同事务。模型未配置、上下文预算热缩或旧证据文件已删都不妨碍已有 brief 任务的纯暂停。
- request_replan：要求当前已暂停；主模型已配置、完整上下文预算及 main ingress 容量允许后，追加要求并 epoch+1，将 owner 回执、修订、可恢复 main 输入和事件原子保存。输入的 route=null，不广播旧渠道；受保护 task_control 引用随 claim 保留。
- approve_plan：同一次 owner 请求的确定方案只确认一次；approved_epoch、plan、修订、确认回执与每个目标的 task_plan_delivery 同事务。targets 去重并受宿主配置预算约束，只能是同 scope 的未完成队友。

ControlResult 提供 receipt、current、created 和可选 ingress；`public()` 仅返回 `{receipt,current}`。旧 request_id 重试返回原始 receipt 和最新 current，不能把历史对象再写回。相同 ID 内容变化为 request_id_conflict。UI 以 current.revision 单调接收，不能只比较 epoch。

既有 `Store.enqueue_input` 仍保留其他调用者依赖的 duplicate objects 语义；控制使用无中间提交的 `_enqueue_input(..., apply_objects_on_duplicate=False)`，且先查控制回执。`_put/_event/_archive_input` 同样只在调用方事务内写入。所有公开事务完成后，宿主才 bus.notify、唤醒 main 或调度队友。

控制与确认复用 `commitment.updated` 事件，载荷只有 id/revision/epoch/approved_epoch/action/control_changed，不发送完整约束、plan 或 owner 附加文字。列表的 control 摘要先预留预算再加入标题预览，保持旧数据在热缩输出预算后仍可读取。

## 有界计划通知与停靠后的满箱

每个 outbox 保存目标 session、generation、completion_revision 与 expected_status。投递要求 scope/current approved epoch 一致，并符合以下之一：

1. 目标仍具有完全相同的代次、完成修订和状态。
2. 原 running/queued 目标在同 generation 停靠，当前为 paused、完成修订恰好 +1，且该唯一下一修订的持久 team.finished 确认 status=paused、generation 相同。

不使用 running agent 上可能尚未更新的 run_id。cancel 会消费完成修订，因此 queued→cancelled→queued、取消后再 paused、新代次或无完成记录的伪停靠均不能通过旧投递。

- 目标尚未 paused：有界写入既有 ingress，principal=system:task_control、route=null。满箱返回 pending、无写入，不循环重试。
- 目标已经 paused：原生调用应已由 Runtime 补齐，此时普通 pending 邮箱可能永远无法先腾空。对通过上述 CAS 的唯一通知，使用 `_archive_input` **原子存档真实消息、置 agent=queued、标 delivery=delivered**。不消费、删除或扩容原有 pending ingress，不新增等待协程。稍后 claim 的旧输入不改变 epoch 或批准事实；下一请求仍必须使用当前 pinned brief/plan。
- 新 epoch/取消/代次不匹配：标 superseded，不复活任务。

DeliveryResult 包含 state、session_id、changed、wake、ingress、message、path。path=archive 时有真实 message 存档；path=ingress 时有实际 ingress。重试已 delivered 的对象不再改 agent。宿主只在方法返回后依据 wake 调度，并区分“owner 请求已接收”“计划已确认”“通知已投递”“队友实际续办”，不能用 delivered 冒充已经执行。

另一个顺序是：目标仍 running/queued 时通知已投递，旧请求随后才被 epoch 门禁判为 paused。outbox 此时已 delivered，不能依赖 pending flusher 再唤醒。`resume_notified_target` 从当前批准事实推导唯一通知 ID，复用相同代次/完成修订校验，并核实 ingress 的真实 source、principal、route=null 与受保护 task_control 引用。若通知已被 claim，还同时检查历史消息与 ingress 的 input_seq、message seq、run_id 关联；普通输入、已取消 ingress、缺失档案或旧 epoch 不能成为续办凭据。查询使用确定 ID，不扫描全部历史。

正常调用位置是 **运行器已决定本代结果为 paused，而数据库 agent 仍为 running/queued、generation=gN、completion_revision=cR 时**。返回 True 后，宿主不得 await，立即复用一次 `_finish(..., requeue=True)`：在同一事务写 `team.finished(status=paused, completion_revision=cR+1)` 和 `agent(status=queued, completion_revision=cR+1)`。完整原生调用配对必须已在此前完成；下一次请求重新构造完整快照。这个 helper 只读，不递增修订、不建消息、不启动任务。它也能核验已持久停靠且唯一下一完成事件证明正确的状态，但正常完成路径不能先 finish 再二次 finish；已经 queued 的 cR+1 不会再次通过旧证明，避免重复续排。

消费者由启动、控制确认、输入空间释放和团队实际状态事件驱动；使用 limit/after_id 有界遍历。无状态变化时等待事件，不轮询模型或持续重试。接线层须拥有关闭/取消消费者的生命周期，不能通过取消消费者传播取消 owner/队友任务。

## 本层验证与尚待集成

`tests/test_task_control.py` 使用临时 SQLite 和故障注入验证：各写点完整回滚、真实 close/reopen、旧回执 CAS 顺序、控制说明保护、同 snapshot 约束与 epoch、主/子身份区别、活动/旧结构 DAG、满箱停靠、取消后同 generation 新意图拒绝、通知存档和排队的原子性、claim 前后的唯一续办证明、实际来源/档案关联、外部 scope 与已完成目标拒绝、输出预算热缩与不泄漏正文。

本层测试不调用真实模型或外部账号。模型流期间 pause、最后 await 后工具门禁、调用配对、Team wait/active/代次、DAG admission 竞争、API 认证和浏览器交互仍需 B 与服务端/前端集成验证。
