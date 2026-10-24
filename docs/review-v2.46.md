# 手机重规划回复与记忆证据评估（2.46.0）

2026-10-24，已正式发布。精确候选/main CI、GPG签名及PyPI/GitHub两处发行资产已验收，见下方[发版闭合](#发版闭合)。以下本地候选记录保留其当时证据与边界。

## 行为与边界

已绑定主人在Telegram/OneBot私聊发出 `/调整` 后，主助手对本次调整的方案或受阻说明回到发令私聊，并保留Web展示。Runtime在配置锁的最终准入后复用 `MobileControlSource` 校验构造可信 `ReplyOrigin`；随首次控制、回执和main ingress在原事务中保存。公开Web请求不能指定该来源，旧指纹不变，重复回执不补改历史。

同一主运行在工具批次后和无工具续轮中，根据真实有限claim列表选择来源。严格匹配的队内报告只补充证据；没有其它新触发时保留本次来源和主动通知选路。新主人输入或其它真实触发按其持久来源更新；首次准备、独立报告新运行、队友来源及报告metadata保持原规则。没有全局最近主人目的地或路由回写。发送前核对当前授权，撤销后blocked，unknown不自动重发。

本片只覆盖主助手重规划回复。队友后续真正完成、另起主运行时回发令手机，仍须下一片的持久计划来源关联；不会由本次离线绿色证明。真实账号送达、自然模型规划质量、真实桌面及原生系统服务恢复也未验收。数据库等七领域仍为格式1，handoff协议2，无格式迁移；项目复核与原生资源保护WIP不进入本版。

既有main同时包含26项固定记忆证据评估，覆盖时间、来源身份、未知投递、遗忘及更正与引文区别，并实际压缩、正常关闭重开。该测试侧扩充不改变生产记忆行为；基线PR53和main精确CI已各十项成功，不替代本候选自己的矩阵。

## 实际失败与修复

- 新四参数旅程首次停于测试helper的 `identifier` 参数冲突，不能算功能红；改为真实ToolCall后，四项均实际完成重开、读取、下一轮批准和原队友一次计划交付，最终手机outbox为0，取得目标红。修复后四项通过。
- 必要兼容/来源三项通过：真实旧无来源领域提交遇回执保存故障后的正式链路重投不补来源；工具和无工具两种边界从Attention切到新主人。测试没有手造旧批准或回执。新旅程另核同事件换另一owner冲突、不修改领域/ingress。
- 首次十五文件429 passed、2 failed。有限读取两临时Store证明原三条控制确认齐全，多出的是正确的自动说明；旧夹具混合计数超时。按真实outbox标记分类，分别精确核来源与唯一发送。
- 旧三参数复验又两项只因成功attempts被预期为1失败。按真实Manager成功清零合同修为0，HTTP recorder仍证明仅一次发送；最终三项通过。没有放宽超时或业务断言。

## 已收取的本机验证

检查由根经主仓 `scripts/check_local.py` 串行执行；共享2GiB、单次1GiB、swap0、单CPU等效预算和既有期限。临时数据、FakeClient、真实Runtime/ASGI/渠道应用及HTTP替身，不使用真实账号、模型、桌面或服务。

| 集合 | 实际结果 |
| --- | --- |
| 新四旅程 | 4 passed，pytest1.31秒，守卫2.152秒、124.3MiB、swap0、exit0 |
| 兼容/来源切换 | 3 passed，pytest0.27秒，守卫0.876秒、76MiB、swap0、exit0 |
| 原接办三参数修正后 | 3 passed，pytest1.03秒，守卫1.770秒、105.9MiB、swap0、exit0 |
| 十五相关测试文件整合 | 431 passed，pytest9.43秒，守卫10.794秒、363.3MiB、swap0、exit0 |
| 全147源码strict非增量mypy | 通过，守卫8.293秒、414MiB、swap0、exit0 |
| 全仓Ruff与格式 | 通过，380文件已格式化；最后四文件仅排版修正，无语义变更 |

这些集合重叠，不累加为新测试数或完整后端覆盖。本候选未重跑本机完整后端，跨平台完整集合交由精确远端矩阵另验，不沿用2.45的全量数。独立审查已覆盖生产、旅程、兼容与负控制，无剩余P1/P2；夹具分类和文档修订另经一次独立后审，无P1/P2。没有把独立审查记作额外执行测试。

## 离线构件准备

四处发行版本同步为2.46.0；数据库等格式仍为1、handoff仍为2。发行/兼容两文件 **166 passed**（pytest0.96秒，守卫1.452秒、68.7MiB、swap0、exit0）。`uv build --offline`从本地材料生成sdist并由它生成wheel（守卫1.343秒、97.4MiB），没有下载依赖或模型。

既有 `scripts/release.py` 以v2.46.0核对源码、兼容声明、CHANGELOG、wheel及sdist一致（174毫秒、19.6MiB）。`scripts/check_wheel.py`只把本地wheel无依赖离线安装到临时目录，验证包内导入、声明、静态资源、API文档、提示词、桌面默认关闭、任务控制、完整ASGI生命周期与CLI版本（1.242秒、100MiB）；均swap0、exit0。没有安装到用户工具环境或启动真实服务。远端发行仍须使用其精确质量作业测试的构件，不能以本地准备代替。

旅程与细节见[后续回复方案](mobile-task-control-proposal.md#手机重规划后续回复切片)、[接办旅程](task-handoff-journey.md)和[Runtime合同](task-control-runtime.md)。每次发行是检查点，下一片继续完整任务最终汇报及主动项目闭环。

## 发版闭合

精确提交 `5a5ae91be7dd86b9f68305c174469e319c66b8aa` 的[候选CI40050511098](https://github.com/69gg/MuLi/actions/runs/40050511098)与[main CI40051161988](https://github.com/69gg/MuLi/actions/runs/40051161988)各十项成功。[PR54](https://github.com/69gg/MuLi/pull/54)以原提交快进合入main，未重写签名；本地提交签名G，GitHub verification为valid。`v2.46.0`注释标签由有效密钥 `23C8AF06EF76B7295409E2AA738DBF23B05B37D4` 签名，`git verify-tag`确认Good signature。

[发行40051756850](https://github.com/69gg/MuLi/actions/runs/40051756850)精确head相同，十项verify及prepare、publish、github-release共十三项全部成功。[GitHub Release](https://github.com/69gg/MuLi/releases/tag/v2.46.0)于北京时间2026-10-24 10:01:04发布，非draft/prerelease；[PyPI 2.46.0](https://pypi.org/project/muli-agent/2.46.0/)两文件均未yank。仅读取小型元数据，未下载发行构件；两处名称、大小和SHA-256一致：

| 文件 | 字节 | SHA-256 |
| --- | ---: | --- |
| `muli_agent-2.46.0-py3-none-any.whl` | 816508 | `3d75ea84f16551f99473d3b9af7a0df8af7b409307ec9c9d3048fae8c8136be1` |
| `muli_agent-2.46.0.tar.gz` | 4297306 | `f37a698b0bf429bb809459d359404a6fa6ba4a4bad3fc180f2aa5d0201193d8a` |

首次PyPI条件投影未产出可接纳记录（exit4）；随后直接读取同一版本的有限字段，确认上述完整两文件和未yank状态，不推断首次未接纳的原因。正式发布仍只覆盖上文功能范围：后续队友完成的独立主运行回手机、真实平台送达及原生恢复另行验收。26项记忆评估已随本版发布，固定策略不能证明自然模型判断质量。
