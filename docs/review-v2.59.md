# v2.59 私聊工具审批候选审查

基线为已发行v2.58的签名提交 `6b25ea722aeb48a0641eb0b70cc2f2ef9427140d`，
其发行证据见[上一版审查](review-v2.58.md#发行闭合)。本地功能、质量及离线包验证已闭合，
签名候选 `185bfe15966c4bd830f7becf1ae34a46e038dabb` 已推送至
[PR #67](https://github.com/69gg/MuLi/pull/67)，首轮远端十一绿、一项失败，尚未正式发行。

## 产品行为与提交边界

已绑定Telegram或OneBot主人可在原始私聊纯文本中发送 `/审批`，有界查询当前真实
等待项；`/审批 完整ID`查看详情，`/批准 完整ID`或`/拒绝 完整ID`只决定该准确
live等待者。列表中的完整审批ID独占一行，并展示准确会话ID、运行ID和工具资料。
命令由宿主处理，不进入普通主人消息队列，不由模型解释批准意图。

复用原适配器的control_text/account证据、信封指纹、配置锁内当前授权复核以及
原渠道outbox。群消息、引用、转发、媒体衍生文本与非主人不提供审批权；旧信封重投
也须重新通过当前身份和配置检查。审批依然要求未完成future与持久pending同时成立，
不能用历史pending对象复活已停止、取消或重启后失去等待者的许可。

原Web resolver共用同步提交段：审批状态、resolved事件和手机入站回执在同一SQLite
事务保存，提交后、首次await前才唤醒原future。回执写失败整体回滚；提交后通知失败
保留已登记决定，原信封重投只修复通知和确认出站，不再次resolve。确认回发令私聊，
原任务成果仍沿原来源并在Web可见。批准不等于工具已执行或成功，原任务版本、新输入、
维护与执行末门保持；未知出站结果不会自动重发。

## 已收本地证据

正式目标先红时，真实phone7主人请求、Web批准既有spawn、继承ask模式的直接队友和
实际fs_write等待者均已成立：future未done、文件不存在、零tool.started。phone8原始
私聊通过可信控制来源，且没有新增主人输入或模型轮次；准确失败在回执state实际为
`mobile_task_control`，预期`mobile_approval`。1 failed，pytest0.57秒，守卫1.276秒、
105.2MiB、swap0。这是分派缺口，未将身份或夹具前置当作功能红。

| 检查 | 不同节点 | 测试耗时 | 守卫耗时 / 峰值内存 |
| --- | ---: | ---: | --- |
| 新正式旅程与五个边界 | 6 | 1.51秒 | 2.313秒 / 162MiB |
| 既有相关回归，10文件 | 237 | 5.86秒 | 7.515秒 / 339.2MiB |
| 发布/兼容/fresh/server/recovery，5文件 | 233 | 3.87秒 | 5.129秒 / 214.3MiB |
| CI文件分片回归 | 5 | 0.87秒 | 1.384秒 / 72.9MiB |
| 全部前端 | 297 | 2.913秒 | 2.987秒 / 95.5MiB |

以上均exit0、swap0，去重合计778个本地节点，不重复累计子集复验。
153源码clean/noincremental strict通过，守卫8.344秒、379.7MiB、swap0；Ruff通过。

正式旅程经真实Runtime、Team、OneBot webhook、ChannelApplication、权限future与
临时Store，核对完整ID详情、准确批准、accepted工具结果、真实文件写入一次、队友报告，
以及实际适配器向原phone7发送成果和Web可见。phone8只收到控制确认；同入站信封重投
保留原回执，零重复resolved、模型输入、工具效果或出站记录。

五边界节点验证真实SQLite回执失败的三物回滚、Web与手机两种先到顺序、原始来源拒绝
及锁内撤权、提交后notify故障重投、确认ReadTimeout后的unknown保留。Web/手机测试
证明先到决定生效、后到决定拒绝，不证明并发请求的任意调度。重开节点是在工具已完成、
确认出站unknown后正常关闭并重开同库；不代表未完成审批等待者重启续跑或强杀恢复。
unknown表示结果未知，不能据此声称平台未收到；新明确命令对旧ID的拒绝确认是独立出站。

C非作者生产审查与A非作者五边界审查均未发现剩余P1/P2。边界小夹具使用真实普通会话
和工具执行创建等待者，没有手造future、审批历史或回执；只有正式旅程验证完整队友交付。
以上使用固定模型策略、MockTransport与合成数据，不代表自然模型质量、真实TG/OneBot
账号送达或真实浏览器验收，未升级本机常驻服务。

## CI并行调整与待验证项

Windows复用现有按文件分片，每个Python版本分两份；macOS仍全量，Linux原样。
通用CI作业数由10变12，发行复用后由13变15；最大并行6、原10分钟期限、runner和
Node堆边界不变。五项本地真实pytest分片回归验证完整并集、空交集、文件参数不拆、
非法及空片拒绝。远端覆盖与耗时仍待验证，不承诺总runner成本下降。

最终本地发布门均已实收exit0、swap0：

- 412文件格式检查通过，守卫65毫秒、21.4MiB。
- 83包离线锁检查通过，守卫64毫秒、29.2MiB，无依赖下载。
- 离线构建通过，守卫1.441秒、98.7MiB。
- 发行内容校验通过，守卫233毫秒、19.1MiB；未覆写旧release-notes。
- 隔离离线wheel安装、导入、版本2.59、兼容声明、资产、API、提示词、桌面默认值、
  任务控制与lifespan烟测通过，守卫1.284秒、100.2MiB。

签名标签及正式发行仍待完成，不以本地通过替代，另记准确终态。
功能合同和先红边界见[手机审批方案](mobile-approvals-proposal.md)。

## 首次远端候选

[CI 40088379012](https://github.com/69gg/MuLi/actions/runs/40088379012) 终态failure：
十二作业中十一项success，唯一macOS 3.12作业 `120855843763` 为4068 passed、
1461 skipped、1 warning、1 error，pytest203.41秒。Linux双Python双片、Windows双Python
双片、macOS 3.13、quality与mcp均成功；本版六个新增手机审批节点通过。

旧 `test_session_recovery.py::test_tool_terminal_metadata_survives_result_truncation[result3-failed]`
在setup的 `create_app → prepare_channels → revalidate_format` 中触发原1秒资料格式
读取期限。有限观测为elapsed1004ms、begin1004ms、queries0、vm_steps0，随后首个
optional查询前检查报 `format_read_timeout`；现证据不足以区分BEGIN内部阻塞、系统调度
或其它原因，不能归因“runner慢”，也不能把它当成手机审批断言失败。

根在Linux沿原守卫精确复验该节点，1 passed、pytest0.45秒，守卫1.178秒、85.3MiB、swap0。
这没有复现或修复macOS根因。未提高期限、跳过读取或重新运行同一失败CI来替代诊断；
后续先核查读取链和足够的定位证据，当前保留发行门为未通过。

## 首轮失败后的有界诊断校准

本次仅补充格式读取的失败观测，没有证明或修复上述macOS根因。原`begin`阶段覆盖
完整`_rows(db, "BEGIN")`及进入下一阶段的间隙，不能把1004毫秒直接解释为SQLite执行
或锁等待。`failure_phase=optional`、`queries=0`证明BEGIN已正常返回，而首个optional
资料查询还未发出；`vm_steps=0`表示尚未累计到进度回调的100步间隔，不表示零VM指令。

失败note新增唯一固定对象`begin_call`，字段为`wall_ms`、`thread_cpu_ms`、
`return_gap_ms`和`returned`。墙钟覆盖原完整调用，包括Python游标创建、执行、取结果与
关闭；返回间隙记录调用完成到后续阶段切换之间的时间。本线程CPU只作可选诊断，函数
缺失、OSError或NotImplementedError时为null，不能伪记为有效的0。BEGIN异常仍按原
异常分类，不能标记returned成功。所有毫秒值非负且有界，整个note仍不超过1024字节，
不输出SQL、路径、数据库资料、原始异常或私有值。

接线保留原1秒期限、查询与VM预算、SQL和PRAGMA、事务及ROLLBACK清理，不新增查询、
重试或格式跳过。非作者只读后审未发现剩余P1/P2。诊断首目标已准确先红：真实BEGIN、
原超时和清理均已走完，在缺少新`begin_call`键处失败，1 failed、pytest0.13秒，
守卫0.896秒、60.9MiB、swap0；不是BEGIN或cleanup夹具前置失败。

五个新增参数节点只校准观测：两个受控时钟分别在真实BEGIN调用内、以及原调用成功
返回后推进1.004秒；另三个覆盖线程CPU函数缺失或上述两种不可用异常。校准要求原
format_read_timeout、queries0、真实BEGIN与ROLLBACK各一次、数据库事实不变，并区分
调用内墙钟与返回间隙；它们不是macOS故障复现，也不证明低CPU究竟来自阻塞、调度或
其它原因。原有BEGIN异常测试另核returned为false。诊断后的定向复验已实收：整个
format_evidence文件75个节点，加上述macOS失败节点在本机Linux的精确复验，共76 passed，
pytest0.59秒、守卫1.350秒、87.5MiB、swap0、exit0。其中5个为新增校准节点；Linux通过
不代表macOS根因已消除。

诊断后的相邻五文件再次233 passed（pytest4.16秒、守卫5.496秒、213.2MiB）；
clean/noincremental strict仍为153源文件通过（守卫8.604秒、378.8MiB）。Ruff通过，格式
首查仅两处测试排版，修正后412文件通过（66毫秒、23.5MiB）。离线重新构建通过
（1.564秒、99.5MiB），发行校验通过（190毫秒、19.3MiB），均swap0、exit0；旧notes
未覆写。原审批实现未改，首轮778节点保留为历史，不将相同节点重复累加为新覆盖。
隔离wheel离线安装、导入、兼容、资产、API、提示词、默认桌面关闭、任务控制和lifespan
复验通过（1.420秒、99.7MiB、swap0、exit0），版本2.59。新远端候选终态另行收取；
原读取期限与发行门保持。

时钟定义参考[Python3.12本线程CPU计时](https://docs.python.org/3.12/library/time.html#time.thread_time)，
事务语义参考[SQLite DEFERRED事务](https://www.sqlite.org/lang_transaction.html#deferred_immediate_and_exclusive_transactions)；
这些定义不代替本次实际耗时定位，远端发行门仍未放行。

## 诊断候选的远端结果

签名提交 `9bb5c2d4e68174608b5455648bff1a751ab6ff64` 的
[CI 40090387882](https://github.com/69gg/MuLi/actions/runs/40090387882) 已实收十二作业全部
success，包含此前失败的macOS3.12和完整其余平台、quality、mcp。此结果只证明该精确
候选本次检查通过，不倒推首次超时根因已经修复。首轮失败及受控诊断的限定保留。

随后同步当前发布操作文档的十二项/十五项数量，历史十项/十三项发行证据不改。

## 发行闭合

最终签名提交 `300b68f0066a082e6a6bdf196f31668289858ae0` 的
[CI 40091072098](https://github.com/69gg/MuLi/actions/runs/40091072098) 十二作业全部success。
有效GPG签名标签v2.59.0与主分支已原子推送；PR #67于2026-10-24T11:58:19Z合并。
[Release 40092008397](https://github.com/69gg/MuLi/actions/runs/40092008397) 十五作业全部success，
包含完整跨平台验证、内容校验、PyPI Trusted Publishing及GitHub Release。
同SHA的重复main CI `40092007557` 已终态cancelled，不记为通过。

[GitHub v2.59.0](https://github.com/69gg/MuLi/releases/tag/v2.59.0) 于
2026-10-24T12:07:55Z发布。根只读小体积GitHub/PyPI元数据，确认两平台构件名称、
大小、SHA-256一致且PyPI未yank，没有下载发行包：

| 构件 | 字节 | SHA-256 |
| --- | ---: | --- |
| muli_agent-2.59.0-py3-none-any.whl | 860694 | 9f5609afe619c9347a70009de87164903e998975047332d1e957ce5cf90ff36e |
| muli_agent-2.59.0.tar.gz | 4570947 | 0170f6fa9dacc094e1a57b7c3e9b1d7df7eae3dd2091b6f5035d410c785a8886 |

此记录写入后继候选，不改已冻结标签；首次macOS失败及未确定根因保留。
最终文档提交的CI与签名标签、正式发行仍待闭合，不以先前候选绿色替代。
