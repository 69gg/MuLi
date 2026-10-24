# 主人插话后继续办理

当沐璃主助手等待工具批准时，你可以直接在 Web 或已绑定私聊发来新的要求。例如手机 7 请求发送说明、尚未批准时，在手机 8 说“先别发送，改为解释”。旧主审批会取消，发送记为未执行；同一个主运行接收新输入并回复手机 8，Web 保留完整过程。

这是 v2.63 开发候选。已经决定的审批不翻转；已经进入工具 handler 的操作先完成原收口，正在流式回答的模型保持当前请求。队友的独立审批仍可沿原 `/审批`、`/批准`、`/拒绝` 管理，不因主对话插话而取消。

## 提交与续办

Runtime 先在配置锁内复核宿主准入、校验信封、构造原 Conversation payload，再通过现有 `Store._enqueue_input` 去重和保存。只有真正新增、可信主人、pending 的主输入可以检查当前主运行的审批。

检查绑定稳定主 session、准确 run、非停止或已完成的真实 Task、持久 running 状态与原九字段 live 审批。Permissions 的易失索引仅定位该 session/run，不读取或撤销队友审批。输入、取消后的审批和 `approval.resolved` 事件在同一 SQLite 事务中提交，事件记录真实新 input ID。

提交后至首个 await 之间，以专用 `ApprovalSuperseded` 异常唤醒原等待者；它不是 Task cancellation。旧工具得到 `not_executed`，原 assistant/tool 配对、剩余尚未开始的调用、新输入 claim 和下一轮来源冻结沿既有路径完成。原流、driver、子 agent 和工作流生命周期保持。

事务写失败时，新输入、审批状态和事件一起回滚，原等待者仍 pending；提交后的通知失败不反转决定，完全相同信封重投只返回原持久输入。重复、外部或系统资料、附件中的指令、准入被撤销的信封不能撤销审批。尚未产生人工审批票的独立 auto 审查不在本次可唤醒范围内。

## 本地验证

正式入口为 [跨私聊真实旅程](../tests/test_owner_approval_steering_runtime.py)。实现前准确失败：手机 8 的真实入站、来源与渠道消费游标已核验后，旧审批仍是 pending，预期 cancelled；pytest 1 failed / 0.70 秒，守卫 1.391 秒、106.7 MiB、swap 0。未使用等待超时制造失败。

两生产实现后，该同一节点 1 passed / 0.58 秒，守卫 1.274 秒、105.5 MiB、swap 0。扩大同文件 7 个真实节点全部通过，pytest 0.60 秒、守卫 1.235 秒、109.1 MiB、swap 0，包含首节点，不重复计数。边界覆盖重复旧信封不撤后来票、非主人及最终准入失败、已决定与已进入 handler、在途模型流以及真实子 agent 审批独立。

[原子故障回归](../tests/test_owner_approval_steering_atomicity.py) 在真实审批写入和事件插入后抛错，检查完整回滚；另在确认已经提交且原 future 已唤醒的首次通知处抛错，检查完全相同信封重投、单次取消与零效果。该两节点与原 Conversation 完整 15 项合计 17 passed / 0.57 秒，守卫 1.212 秒、90.6 MiB、swap 0。

原 Conversation 的强制审批测试只移除“新输入后还须批准旧票”的过时步骤，保留 yolo 无法绕过 `requires_approval`、零 handler 与 `not_executed` 断言。非生产作者对实际两生产及新增原子测试独立后审未发现剩余 P1/P2。以上没有使用真实平台账号或自然模型，不证明外部送达、设备联调、SQLite COMMIT 结果未知或进程强杀后的效果恢复。

扩大26个完整渠道、审批、任务控制、队友和维护文件336 passed（pytest13.45秒，
守卫15.464秒、491.4MiB、swap0）；不重叠15个Runtime、Team、上下文、权限、恢复、
MCP、发行与格式文件480 passed（pytest6.58秒，守卫7.665秒、323.8MiB、swap0）。
共816个不同节点，前述小范围均包含其中，不重复累计；后一组只有故意构造坏类型的
MCP配置测试触发既有Pydantic序列化warning，没有新增失败。

Ruff通过；格式首查仅两个新测试排版，纯formatter后420文件格式通过；153源码
strict/no-incremental/MYPYPATH=src通过（守卫8.184秒、381.4MiB、swap0）。
离线锁核对83包通过，沿用已有CPython3.13.15，不下载或升级解释器。
离线构建、隔离wheel安装验收及v2.63.0发行元数据校验通过，构建守卫1.485秒、
102.3MiB、swap0，wheel验收1.265秒、100.6MiB、swap0；均未联网下载依赖。

v2.62同提交额外main CI的Windows旧队友进度测试失败没有被跳过：本轮保留原2秒
等待与全部断言，补固定阶段、实际模型轮数和四类有界事件计数，不加载消息正文。
受控校准完整委派真实资源回收后以Event保持driver收尾，准确观察真实run.completed
已产生但持久状态仍running，再释放原worker正常完成。它验证诊断的区分能力，
不代表已复现或修复远端根因，见[开发验证](development.md#队友进度等待的阶段诊断)。
准确旧失败与校准两节点2 passed（pytest2.57秒、守卫3.235秒、92.9MiB、swap0）；
扩大四个完整progress/cleanup/wait文件60 passed（pytest4.33秒、守卫5.353秒、
177.1MiB、swap0），与上述816节点不重叠，总计876不同节点，两节点包含在60内。
精确候选CI仍待完成；尚未正式发行。
