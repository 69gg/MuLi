# 按本次私聊来源回复成果文件

主助手已经能使用 `channel_send` 发送文件；现有入口需要模型填写渠道、收件人及可选路由。本片增加一个更窄的操作：主人在私聊索取队友成果时，主助手只选择已有附件，由宿主固定本轮真实来源，避免再次推断收件人。

## 最小合同

- 新工具 `channel_reply_files(attachment_ids, idempotency_key)` 仅有这两个必填参数，没有文字、渠道、收件人、路由或媒体模式。附件沿现有数量、字节、摘要及适配器限制，固定以文件发送。
- 工具仍为 `external`，保留原 ask / auto / yolo 审批与 Attention 只读过滤。仅当前真实主会话 driver 的工具执行可调用；核对当前 Task、run、PreparedRequest 和任务 epoch，拒绝保留 handler、复制 ContextVar 的其它 Task 或队友借用。
- 来源只取本轮 `PreparedRequest.reply_origin`。`ReplyOrigin.kind` 必须为 `channel`，其中真实 route 的 `kind` 必须为 `private`；Web、无来源和群聊明确拒绝，不猜最近联系人，不回退外部目的地。
- 复用 `FrozenRouting.bound` 与 `ChannelManager.enqueue`，完整保留账号、sender、target 和 reply_to。沿现有自动回复的持久来源证据启用投递前再次鉴权；这不代替工具的 external 审批，也不授予任意目的地。
- 宿主将准确来源 input_id 与调用方稳定 key 一起摘要为 event_id；同信封同 key 复用原 outbox，不同附件冲突。新的主人输入是新请求，不能用相同 key 误认旧投递。原 manager 完整路由参与幂等键的语义不变。
- 入队前核对当前配置、当前 manager 与附件引用；此时已经撤权、禁用、关闭或文件不可用则拒绝，不产生部分 outbox。已经持久入队之后的撤权沿原 dispatch 转为 blocked。
- 排队回执不等于已送达；结果未知沿原 outbox 保持 unknown，不自动重发。普通 `channel_send` 的参数、能力和返回合同不变。

附件存在及其副本摘要是宿主事实；“符合任务要求”仍须依据已有验收或模型判断。工具不把任意附件自动认证为本任务原创，也不改变 file_publish、任务 completed 或交付验收单语义。

## 第一阶段正式旅程

新增一条离线 sent 旅程，使用真实 ASGI、OneBot 入站适配器、默认 ChannelApplication / LocalServices、Runtime / Team 和临时 Store，仅平台 HTTP 与模型策略使用固定替身：

1. 手机 7 交代任务，主助手真实保存任务并派直接队友；队友经真实 fs_write → file_publish 得到附件，真实完成报告携带实际附件元数据。主助手从报告读取该引用，Web 下载副本并核 SHA。
2. 手机 8 的普通输入完成独立主运行。手机 7 再索取成果，准确观察该轮工具目录：通用 channel_send 已有，新 channel_reply_files 应可用；缺入口时正常结束主运行，外层在准确入口断言处取得红。
3. 实现后同一旅程继续调用新入口，经真实 outbox 和 OneBot upload_private_file，核 user_id=7、实际 base64 字节与 SHA、一次文件投递、8 无文件，且 Web 仍可下载原副本。

沿既有 16000 / 6000 / 3 预算及 3 / 4 秒有界等待，不替换默认生命周期，不手写工具回执、附件 ID 或报告。首条旅程验证 sent，第二条验证真实超时后的 unknown、正常重开和原信封重投；当前准入撤权等边界由独立负控核验。固定工具策略证明宿主合同，不代表自然模型、真实账号或真实平台已验证。

## 当前证据

根已实收首个准确功能红：正式单 sent 旅程 `1 failed in 0.67s`，守卫 `1.344s / 91.4MiB / swap 0`。真实 fs_write、file_publish、完成报告、Web 下载、手机 8 独立运行结束，以及手机 7 索取后的主运行正常结束均已成立；唯一失败在外层 `reply_files_available is True`，本轮真实工具目录中确无新入口。此红仅证明入口缺口，不代表外发成功。

最小生产候选仅修改 `src/muli/channel_application.py`，复用原工具 provider、附件校验、manager.enqueue 与固定来源 dispatch。根首轮六节点实收 `5 passed, 1 failed in 1.04s`，守卫 `1.905s / 112.6MiB / swap 0`：正式 sent 旅程完整通过，四项独立接纳负控通过；唯一队友负控在异步 launch 前读取空 tasks，属于夹具前置问题。独立作者仅按原 team.wait 加入 timeout=2 的真实终态等待，根单节点复验 `1 passed in 0.47s`，守卫 `1.151s / 87.6MiB / swap 0`；因此六个不同新增节点均已有通过终态，不重复累计失败后复验，也不把夹具问题说成生产故障。

根另已收相关六文件 `232 passed in 2.28s`，守卫 `2.983s / 155.8MiB / swap 0`；全 150 个源码严格类型检查通过，守卫 `8.284s / 417.5MiB / swap 0`。新增七项与这 232 项既有回归无重叠，功能相关合计 239 个不同节点通过。发布兼容166项随后通过，pytest0.97秒、守卫1.493秒、67MiB；合计405个不同本地节点，不重复计失败后复验。

第二条正式旅程根已实收 `1 passed in 0.64s`，守卫 `1.335s / 92.4MiB / swap 0`：同一真实主请求内两次相同 key 的工具调用返回原 outbox，真实附件上传只遇一次显式 MockTransport ReadTimeout，随后实际 channel_status 完整工具结果核对 unknown。正常关闭默认应用及 Runtime，原 Store 重开后重投准确原信封，持久消息、run、入站回执和 fingerprint 均保持不变，未新增运行或出站；再用另一私聊的真实只读状态运行与文本送达验证恢复后的处理进度，原文件仍 unknown 且一次 HTTP。另一次主人输入与同 key 的区分沿独立负控验证。共两条正式旅程加五项独立负控，七个不同新增节点均已有通过终态，未改生产、期限或预算来取得第二条通过。

unknown 只表示未获得确定投递结果，不能判断真实平台是否已经收到文件。一次离线 HTTP 调用和重开后不重发，不构成平台 exactly-once 或真实账号送达保证。

作者未运行检查。非作者分别完成生产接纳链及第二条正常重开旅程后审，无剩余P1/P2；Ruff通过，三个改动文件仅由根按原formatter整理，397文件格式复验及离线83包锁检查通过。离线wheel/sdist、元数据一致性及隔离wheel安装/API/lifespan检查均通过；精确候选最终十项与正式发行十三项通过，签名标签及GitHub/PyPI两包元数据核对闭合，首轮Windows启动前置超时的未知原因仍保留，精确证据见[发行审查](review-v2.53.md#发行闭合)。固定策略与离线平台不代表自然模型选择、真实账号或外部送达已验证。
