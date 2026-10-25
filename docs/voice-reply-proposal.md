# 本地声纹会话自动语音回复（v2.60 提案）

状态：正式目标已由根实收准确红（1 failed，pytest 3.58 秒；guard 4.240 秒、97.3 MiB、swap 0）。真实配置、PCM 注册/声纹检测接线、ASR HTTP、reminder_list 工具轮、主 run 最终答复与 Web 记录均已到达；当时只有“请说”一次合成/播放，没有最终答复 TTS。候选实现后，根已实收正式旅程及七项生命周期回归共8 passed（pytest 0.73 秒；guard 1.487 秒、97.6 MiB、swap 0、exit0），原失败断言现通过。

独立七边界、既有回归及前端去重共905个本地节点通过，153源码clean strict、415文件格式、Ruff、离线锁/构建/内容校验及隔离wheel烟测通过，见[候选审查](review-v2.60.md)。签名首候选已推送至PR #68，远端十一绿、一项旧AX租约浮点边界失败；窄修保留原期限并另取完整CI，正式发行门尚未闭合。未做真实设备/声纹算法效果验收。

## 用户合同

启用 `voice.enabled`、`voice.tts_enabled`、`voice.reply_enabled`，配置现有唤醒/声纹桥接、完成本地声纹注册并配置 ASR/TTS 后，本次真实 `VoiceWorker.listen_once()` 验证通过的输入进入同一个 MuLi；该输入实际所属主运行成功完成时，自动合成并尝试播放一次最终答复。原“请说”提示音、显式 `speech_synthesize` 保存附件和网页录音草稿行为保持。

仅本次本地声纹验证产生播放资格。手机、Web、ASR 文本、模型参数或伪造 `source="voice"` 不能取得资格；手机审批只是放行原工具，不更换答复来源，也不撤销原声纹会话。停用、停止、换代、新输入取代旧请求均撤票。未播放/失败/结果未知不重试；重开不恢复易失票据，不补播历史消息。

## 最小接线

1. **`PersonalIO.on_voice`：一个设备、一张易失票据。** 只有当前 worker 回调且本次 `speaker_verified is True` 才建票，保存该 worker、个人 I/O 换代号及 `submit()` 返回的真实入站 ID / `voice_reply_epoch`；该 epoch 必须在接纳锁内随真实入站冻结并返回，不能 submit 返回后再读当前代次。替换时取消旧等待任务。`on_voice` 随即返回，不把等待模型/审批放进 `VoiceWorker.listen_once` 的 `Admission.operation`。PersonalIO 拥有等待任务，关闭/重配置先失效并取消结算，再关闭 worker/media。
2. **`Runtime`：提供准确完成事实和撤票代次。** `submit()` 返回的 run 可能正在处理旧输入，不能直接使用。等待者按 ingress 唯一 ID 点读 `state/run_id`；pending 只等现有 `bus.changed`，在锁内用 revision 条件避免漏唤醒，不加计时轮询。delivered 后调用已有 `Runtime.wait(actual_run_id)`，该方法已 shield 主任务。成功 run 新增可选 `result_message_id: str | None`、`reply_origin: dict | None`；两者取实际最终 `PreparedRequest.reply_origin` 与最终 assistant 消息，绝不完成后重算最新用户。`run.completed` 同样携带这两个有限定位字段；旧 run 无字段即无自动播放资格。中间工具消息、流式片段、失败/暂停/取消、子任务和多来源工作流聚合结果不冒充单一最终语音答复。
3. **同步撤票、音频前末门。** Runtime 提供只读 `voice_reply_epoch`；真实新 owner 输入插入、实际创建的主人 task-control（含暂停）及显式主会话 stop 入口在首次 await 前递增。重复收据、队友报告、手机审批不递增；新 Attention 输入同样使旧本地票据失效。个人 I/O 换代和 `VoiceWorker.stop` 另有本实例生命周期代次。等待者醒来、TTS 前、TTS 返回后及调用播放器前均核票据仍当前、所有开关/worker/代次、真实成功 run 和最终 `reply_origin.input_id == ticket.input_id`。已启动的播放收到撤票时取消并结算原 player 子进程；已发出的声音无法撤回，不宣称硬实时静音。
4. **`VoiceWorker`：复用音频操作生命周期。** 新窄方法 `reply(text, *, admission_guard)` 在原 `_operation`、`_capture_lock` 内完成 TTS/保存/播放，复用 `MediaService.synthesize`、`LocalFiles` 和 `SystemPlayer.play`，按上述末门检查。等待阶段不占 Admission；实际音频阶段禁止本设备同时采集，避免把自己的答复重新录成唤醒。停止会取消原 `_operations`，不通过显式合成工具绕过此门。

Runtime 代次不产生新持久状态或授权；原入站/运行进度继续使用 `bus.changed`，Runtime 与 worker 的易失撤票事件在首次 await 前同步置位并换代。等待者先取得当前事件再同步核对票据，旧票据不能借新事件漏掉失效；音频任务在等待实际 player 时同样监听撤票，取消并结算原操作。每个临时 bus/Event 等待任务都由原 owned task 在 finally 取消并收取；显式 stop 即使已无 active run 也能撤票，不改变原关闭/通知顺序。不新增 scheduler、订阅表或模型轮询。任意票据检查/合成/播放失败只更新语音状态，不改已完成 run、不重投用户输入、不取消 shield 内的主运行。

## 音频、失败和资源

`VoiceWorker.status().last_reply` 使用固定状态（waiting / synthesizing / playing / played / cancelled / failed / unknown）及真实 input/run/message 定位；`played` 只表示播放器正常返回，不能证明人耳听见。合成保存复用既有 `artifact` 结构和 voice 私有目录，默认保留生成音频供核查，受原 `max_file_bytes/max_total_bytes` 限制；取消也不悄悄删除已保存结果。没有音频时不制造路径。失败沿原 `last_error` / `voice.status` 可见，播放已进入后取消或丢返回记 unknown，不补播。

文本不截断伪装完整答复：只接原最终输出预算内的非空答复，继续受现 Media HTTP、文件配额、播放器 20 秒及进程输出限制约束。每设备至多一个等待/播放任务；不下载模型、不添加依赖、不改权限模式、ReplyOrigin kind、存储格式或渠道外发。

## 先红正式旅程与边界

- 真 ASGI lifespan → 实际 PersonalIO / VoiceWorker → 真实 PCM 注册与本次唤醒/声纹验证 → 可控 ASR HTTP → 真实 Runtime（含工具轮）→ 最终答复 → 可控 TTS HTTP / player。分别核“请说”和最终答复，最终文本只合成/播放一次；原 Web 最终记录仍在。不能直接调用 on_voice 或手造 ingress/run 取代这条链。
- 工具或 TTS 屏障内接入真实手机/Web 新输入：旧票据失效，手机继续同一 MuLi且不本地朗读；队友报告不能抢旧来源，手机审批不误撤原票据。声纹拒绝、默认关闭均零最终 TTS。
- TTS 屏障期间 stop、配置关闭/换代或默认 close/reopen：释放屏障后旧音频不播放；保留真实 run/音频/失败事实且无补播。取消等待者不得取消 Runtime 主任务。真实录音、声纹误识别率、外部 TTS 与扬声器效果仍须单独设备验收，离线正式旅程不替代它们。
