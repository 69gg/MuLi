# v2.60 本地声纹会话自动语音回复：候选验证记录

状态：本地功能、相关回归、质量与离线包门已闭合。签名候选
`b65ddf9fb817d3e77c801836696a4d64d050bff3` 已推送至
[PR #68](https://github.com/69gg/MuLi/pull/68)，首轮远端十一绿、一项失败；修后精确候选
十二项及正式发行十五项全绿，GitHub/PyPI两构件元数据已核对，正式发行见文末。
功能合同见 [voice-reply-proposal.md](voice-reply-proposal.md)。

## 已实收证据

- 正式目标首红：`tests/test_voice_reply_runtime.py` 单旅程 **1 failed，pytest 3.58 秒；guard 4.240 秒、97.3 MiB、swap 0**。真实 ASGI 配置、PCM 注册及本次声纹检测接线、ASR HTTP、`reminder_list` 完整工具轮、主运行最终答复和 Web 历史均已通过前置；实际只有“请说”的一次合成/播放，最终答复没有进入 TTS。此为最终语音回答缺口，非配置、导入或模型等待失败。
- 实现后根代理实收同一正式旅程及 `tests/test_voice_reply_lifecycle.py` 七项生命周期回归，合计 **8 passed，pytest 0.73 秒；guard 1.487 秒、97.6 MiB、swap 0、exit 0**。正式旅程包含受控 ASR/TTS/OneBot HTTP 与真实 Runtime、VoiceWorker、PersonalIO 生命周期；固定模型策略只证明协议、来源与生命周期合同。
- 根代理随后实收独立边界文件 `tests/test_voice_reply_boundaries.py` 七个不同节点 **7 passed，pytest 1.51 秒；guard 2.478 秒、127.8 MiB、swap 0、exit 0**。与上组不重叠，当前共15个新增本地节点通过，不将重复复验累加为新覆盖。
- 五个私有执行成功stub夹具及既有语音、媒体、Runtime共八文件 **207 passed，pytest 6.53 秒；guard 7.821 秒、217.2 MiB、swap 0、exit 0**。仅将旧成功stub返回值改为新的typed执行结果，原内容、失败stub、断言与时序门不变；生产未加入plain-str兼容分支。一次先行命令误列不存在的personal_io测试文件，被守卫以125拒绝、未执行pytest；核对实际文件后运行本组，不把拒绝记成测试通过。
- 服务、持续入站、任务控制/手机审批、接办、系统事件/维护、网页/渠道语音、压缩和初始化共十四文件 **170 passed，pytest 6.60 秒；guard 8.251 秒、343.8 MiB、swap 0、exit 0**。与前两组不重叠，后端当前共392个不同节点通过。
- 发布、兼容、fresh格式及文档配置四文件另 **216 passed，pytest2.06秒；guard2.829秒、112MiB、swap0、exit0**。全部前端 **297 passed，2.978秒；guard3.079秒、95.1MiB、swap0、exit0**。五组去重共905个本地节点，不重复累计子集复验。
- Ruff首查仅三处import排版，修正后全量通过；415文件格式通过（guard69毫秒、24.3MiB）。153源码clean/noincremental strict通过（guard8.636秒、379.3MiB、swap0、exit0）。
- 83包离线锁检查通过（guard68毫秒、29.5MiB）；离线构建通过（1.518秒、98.4MiB）；发行内容校验通过（204毫秒、19.1MiB），未覆写旧release-notes。
- 隔离wheel离线安装、导入、兼容、资产、API、提示词、默认桌面关闭、任务控制及lifespan烟测通过（1.427秒、99.8MiB），版本2.60，均swap0、exit0。签名候选、远端CI及正式发行另取精确终态，不以本地通过替代。

## 独立边界的范围

七项使用真实 ASGI lifespan、设置 HTTP、显式声纹注册、`listen_once()`、PCM/ASR/TTS HTTP 和实际持久入站/主运行，不直接调用 `on_voice`，不手造入站、运行或完成回执：

1. 默认未设置 `reply_enabled`：已验证输入仍生成文字答复，只有原“请说”提示音。
2. 本次真实采集未配置声纹桥接：事件明确 `speaker_verified=False`，实际入站隔离，零主运行和最终音频。
3. 最终 TTS HTTP 正在等待时，真实 Web 主人新输入撤销旧票据。
4. 主运行已经成功完成、TTS 尚未返回时，真实主会话 stop 仍撤销旧票据；原完成事实保持。
5. 设置 HTTP 禁用语音并正常重配置：旧 worker 关闭，旧 TTS 被取消，新实例不能恢复旧票据。
6. 主模型 stream 正在等待时关闭 PersonalIO：语音等待者收口，shield 内的真实主运行不被取消，释放后可正常完成。
7. 受控播放器已接收最终音频并开始处理，随后丢失结果通道：记 `unknown`、保留真实音频文件；正常应用关闭后用同一 Store 重开不重新合成/播放。重开时受控唤醒后端不可用，防止新采集与历史补播混淆；此项不代表进程强杀恢复，也不把所有确定播放失败一概定义为 unknown。

复用正式旅程已有的 `FakeRecorder`、`FakeDetector`、`FakePlayer`、`FakeClient/make_runtime` 与三秒有界 settle helper。受控事件只在媒体 HTTP、主模型 stream 或播放器边界设置；保持每次最多 32 条历史、16000/6000 上下文与输出预算、3 个 pending slot 及原三秒等待门，不增加资源预算。

## 非生产作者源码后审

对 `config.py`、`runtime.py`、`personal_io.py`、`voice.py` 及原服务重配置/关闭、媒体合成链只读复核，当前未发现明确 P1/P2：

- 资格取当前 worker 的真实验证事件及接纳锁内返回的入站 ID/代次；重复回执、未验证输入及手机审批不会借用历史语音来源。
- 等待者按准确入站点读实际 run，等待原 `Runtime.wait()`；只接受成功主运行实际最终 assistant 消息和冻结的 `reply_origin.input_id`。工具中间轮、失败/取消/暂停、旧 run 和多来源工作流聚合不能冒充本次最终答复。
- Runtime/worker 的同步撤票 Event 与代次配对，等待 bus、模型、TTS 或播放器时均可结算旧操作；音频实际保存/播放仍经过原 Admission、采集锁、文件额度和播放器生命周期。
- PersonalIO 先撤票并收取等待子任务，再关闭设备/媒体；取消 `Runtime.wait()` 的外层等待者保留其已有 shield，不改变主运行结果。票据只在内存中存在，正常重开没有历史补播入口。

另一非测试作者已只读审查独立七边界，未发现明确P1/P2：私有任务字段只作结算屏障，核心结果取真实HTTP、播放器和持久事实；未手造票据或运行来替代入站。此后审不把生命周期测试视为模型或硬件验收。真实麦克风/扬声器、声纹算法识别率、自然模型表现、付费或外部账号服务均未验证。`played` 只表示播放器正常返回，不能证明人耳已听见，已发出的声音也无法追回。

## 首次远端失败与原期限修复

精确首候选的[CI 40091932382](https://github.com/69gg/MuLi/actions/runs/40091932382)
终态failure，十二作业十一项success。唯一Windows3.12第二片作业 `120866219111`
为1943 passed、742 skipped、1 failed、1 warning，pytest172.46秒；失败为旧
`test_ax_lease.py::test_serial_requests_keep_one_process_and_return_only_result`，
实际helper frame的timeout_seconds为 `2.0000000000000284`，违反原 `<=2` 配置上界。
其余平台、quality和mcp通过；不能以十一绿替代发行门，也不归因语音断言。

根新增两个受控浮点时钟节点，沿真实AXHelperLease.request、JSON frame、匹配ID的
_response及原关闭回收，在二进制指数边界确定复现原加减舍入，实收2 failed
（pytest0.12秒，守卫622毫秒、64.2MiB、swap0）：2秒与0.1秒配置实际分别输出
2.0000001192092896与0.10000002384185791，未改真实事件循环时钟或扩大断言。
生产只将frame剩余时间截至配置timeout；原绝对operation_deadline、TTL、caller更早
deadline、timeout_at、写前末门及取消/unknown回收保持。整lease文件79 passed
（pytest3.13秒，守卫3.649秒、63.8MiB、swap0、exit0）。非作者源码与回归后审未发现
明确P1/P2，后续精确签名候选另取完整CI，不盲重跑原失败head。

窄修后的三文件语音正式/生命周期/独立边界复验15 passed（pytest1.66秒，守卫2.581秒、
140.4MiB）；与lease79节点不重叠，共94个本次后端节点，不把首候选905机械累计为新证据。
153源码clean/noincremental strict通过（7.930秒、380.2MiB），Ruff与全仓415文件格式通过
（格式76毫秒、21.8MiB）；83包离线锁70毫秒、29.2MiB。离线重建1.431秒、99.3MiB，
内容校验174毫秒、19.4MiB，隔离wheel离线安装/导入/兼容/资产/API/提示词/桌面默认值/
任务控制/lifespan烟测1.242秒、99.9MiB，版本2.60。均exit0、swap0，旧notes未覆写。

## 发行闭合

修后GPG签名提交 `64f9d9d074f5432923806b3b42de390c566ff223` 的
[候选CI 40093175960](https://github.com/69gg/MuLi/actions/runs/40093175960) 十二项全部success。
签名标签 `v2.60.0` 已本地verify-tag为Good signature，与main原子推送；
[正式发行40094164612](https://github.com/69gg/MuLi/actions/runs/40094164612) 十五项全部success。
main重复CI `40094164561` 已请求取消，不能将取消视为通过。

[GitHub Release](https://github.com/69gg/MuLi/releases/tag/v2.60.0) 已于
2026-10-24T12:38:49Z发布。根仅读取GitHub/PyPI有限JSON元数据，没有下载发行包；
核对两边文件名、大小、SHA-256与PyPI未yanked：

| 构件 | 字节 | SHA-256 |
| --- | ---: | --- |
| `muli_agent-2.60.0-py3-none-any.whl` | 863776 | `388d0e8cb444db21b892689ebc094b0f81406a29b9b3b4ff19939d9192d26af9` |
| `muli_agent-2.60.0.tar.gz` | 4591862 | `f1b3eba11e97aa250854cc6f2a035311193a41baa617826ffe9fa5b9f39ac779` |

发行不代表真实设备、声纹识别率、自然模型或外部账号联调已验收，首次失败记录保留。
