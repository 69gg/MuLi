# 2.33 MCP 活跃换代审查

状态：已正式发布。首次候选的两项 Windows 失败修复后，最终精确候选/main 各八项
及发行十一项全部成功，包含真实 Windows provider 验收；签名与包元数据见末节。
Linux 托管服务原生整链仍在独立 PR37，不能由这里的成功结果代替。
2.32.1 已发布标签保持不变。

## 用户结果与实现

主会话、队友或工作流持续运行时，纯扩展目录、API 和 agent 自配置可发布
新的 MCP 工具表。每轮模型请求冻结 schema 与 handler 配对；实际派发前再核
当前代和确切连接。尚未派发的旧调用返回未执行，已接纳调用持有旧连接与
旧脱敏/输出策略完成，最后一个调用释放后才关闭。新准备不会创建第三套连接。

Skills-only 保持独立，不撤销 MCP。完整配置、模型/渠道混合修改、维护事务
和已有 stdio 配置替换保留各自边界，不增加自动重试、安装或跨进程旧连接恢复。
详见[用户说明](mcp.md)与[设计及旧新合同表](mcp-live-generation-design.md)。
这里的 stdio 替换边界是 2.33 历史合同；现行同名候选热切换见
[同名 stdio 设计](superpowers/specs/2026-10-28-mcp-stdio-swap-design.md)。

签名实现 `e8abb2c2cb9af9f5cfacf13aff5739946863e9c0` 整合为 `c9ccd04`。
真实 Runtime/Team 的旧行为先以 `runtime_busy` 失败；作者随后完成 16 个文件
377 项唯一回归，其中新增 21 项。原 MCP 进程内 FastMCP 一例明确未在本机执行，
没有删除或放宽；真实可选 SDK 的验证由远端既有 MCP 作业负责。

根与独立 reviewer 审查生产链、schema/handler 配对、调用取消与强拥有回收、
旧密钥对象值及结构键脱敏、配置保存失败、管理队列顺序和 API 准入，没有剩余
确定 P1/P2。关联回归还发现并修复目录观察失败后同轮准备、状态查询额外读取
维护门及失效候选快速重试，原 watcher 和系统错误来源断言保留。

## 整合验证

根分支同步项目、运行时、锁与兼容声明为 2.33.0，更新使用说明和 Changelog。
八个关键扩展、Skills、管理及 API 文件共 143 项再次通过（7.01 秒、212.9 MiB）；
另两个兼容与发行规则文件 166 项通过（0.97 秒、69 MiB），根分支共十文件
309 项，不与作者的 377 项重复累计。

全仓 Ruff、当时 328 文件格式、136 源码完整 `mypy --no-incremental` 和 83 包
离线锁核验通过，最高峰 400 MiB、swap 0。每次使用共享守卫串行执行，单次
30 秒、1 GiB、共享 2 GiB、单 CPU；没有安装依赖、连接用户 MCP、访问真实模型
或运行本机用户服务效果。该片未改前端，跨平台全量与新包构建安装留给精确 CI。

资源目录与内容能力仅有[后续研究](mcp-resources-research.md)，尚未实现；
不会把工具输出截断当作传输在解码前已有字节限制。

## Windows 自有测试窗口

签名测试片 `48e61dd5bd8f9a77e283f82dfabf10bf8a9c7beb` 整合为 `3f876ee`。
2.32.1 main 的旧失败只出现进程创建返回，无首个脚本标记；本片移除被读目标的
额外 PowerShell/.NET/WinForms 启动依赖，改用标准库 Win32 自有窗口。产品仍由
真实 PowerShell MTA helper 读取，原 12 秒、4 KiB、密码 Name/value 与三项
原生断言保留。就绪消息必须实际经过窗口消息循环，指针宽度与部分失败清理
有纯夹具覆盖；隐藏前置 STATIC 提供密码 Name 的依据经微软原文复核。

四文件 47 项通过、3 项原生测试在 Linux 按原门跳过（0.22 秒、62.3 MiB）；
修改文件的 Ruff、格式与严格类型通过，整轮峰值 244.4 MiB、swap 0。
根与第二 reviewer 静态审查无确定 P1/P2；根不重复作者这组检查。合入后全仓
Ruff 与 330 文件格式再次通过，生产源码未变，不重复此前完整类型检查。

这些结果不证明 Windows provider、隐藏 label 的最终 Name 或真实消息循环已通过，
也不宣称查明系统 PowerShell 冷启动原因。新的 Windows 验收由本候选 CI 执行，
详细边界见[Win32 夹具审查](uia-native-win32-fixture-review.md)。Linux 真实服务
整链仍在独立 PR37，不包含于本候选。

## 首次候选与数据库扫描改进

PR39 候选 `753e0e50528091c55566c306f4aedac502387cbd` 的 CI
`39573510582` attempt 1 为 6/8：质量、MCP、双 Ubuntu 和双 macOS 成功，
Windows 3.12/3.13 的新目标已在约 79/95 毫秒进入消息循环，但就绪 PID 与
直接启动进程不一致。CPython Windows venv 启动器会另启实际解释器；后续修复
直接使用基础解释器，继续要求就绪 PID 严格等于直接子进程，不接受任意后代身份。
旧失败保留，不通过重跑覆盖。

独立 PR37 的 macOS 3.12 首次 Runtime 构造曾触发 `existing_store_read_budget`，
该码同时表示 VM 指令或墙钟预算耗尽，日志不足以区分。源码核对发现每个必需
对象都会重复扫描 `sqlite_schema`，因此改为一次参数化查询、逐行核对，仍保留
原 1 秒/20 万指令、单值/SQL 长度、读快照、完整 DDL 比较和所有失败恢复。
不根据这项优化声称已证实该次 CI 的具体预算原因。

小型真实 SQLite 库附加 100 张无关表，在 1 万指令预算内的回归先于旧代码失败；
修改后扩为 all/core/memory/channel 四组，并附一个超出 schema 文本额度的无关
view，均通过。既有缺表/约束/身份/超预算/事务测试保留；五个相关文件共 194 项
通过（2.34 秒、178.7 MiB），不与上面的 309 项重复合计。两文件 Ruff/格式和
源码严格类型通过（类型峰值 195.3 MiB）。独立复审无确定 P1/P2；未触及真实库。

Windows PID 修复签名 `e1abd65ef35ad55c53b5f9a816127b5f7bc76e5d` 整合为
`7ebc78b`。旧代码用真实 `_ready` 生产器和原 observer 的纯替身链先稳定失败，
修后目标直接启动 `sys._base_executable`，缺少有效绝对基础解释器则拒绝，未改
严格 PID 校验或退而接受任意子 PID。相关 52 项通过、3 项保持原 Linux 平台门
跳过（0.22 秒、58.4 MiB）；四文件静态通过，峰值 145.2 MiB。

PR37 Ubuntu 3.13 的合成恢复超时另发现夹具会在 standalone launcher 的 `.pth`
阶段提前导入完整服务，随后 exec 真实 CLI 又导入一次。签名修复
`87b33ae37101d94b5aaf792f6c5551de9f8278f7` 整合为 `cb64a00`，仅在真实完整
CLI argv 才安装测试 hook，恢复 launcher 在验证 slot 前不导入 slot 的边界。
旧 launcher 反例先失败；10 项纯 argv 回归通过（0.16 秒、61.3 MiB），单文件
Ruff/格式与严格类型通过（类型峰值 272.9 MiB），两位 reviewer 无阻断。
原 20 秒与真实 CLI、backend、hash、文件系统、数据库、nonce 及所有业务断言
均保留，未在本地跑服务子进程。实际启动收益与完整回退仍待新精确 CI，不将
既有超时全部归因于这一次重复导入。

上述三片合入后，全仓 Ruff 与 330 文件格式检查再次通过（22 MiB、swap 0）。
不重复作者已通过的纯测试；数据库生产源码已在其变更后单独完成严格类型检查。
以下最终候选继续运行全部八个原 CI 作业，没有以本地结果代替跨平台验收。

## 精确发行验收

最终签名提交 `1cf078dbd31c4f71054328de9cf02a46f2805d8f` 的
[候选 CI](https://github.com/69gg/MuLi/actions/runs/39576755760) attempt 1
八项全部成功。根独立核对全部 job 与 SHA 后，非强制原子推送 main 与签名标签
`v2.33.0`；[PR39](https://github.com/69gg/MuLi/pull/39) 于
2026-10-21 09:34:45 UTC 合入相同提交。标签对象
`79d5649382920deb2bcb04a6cd0f16cef37c437f` 指向该精确提交，提交与标签的
GPG 签名均通过核验。

相同 SHA 的 [main CI](https://github.com/69gg/MuLi/actions/runs/39578053330)
attempt 1 八项成功；[发行流水线](https://github.com/69gg/MuLi/actions/runs/39578052294)
attempt 1 十一项成功。三轮均包含 Ubuntu、macOS、Windows 的 Python 3.12/3.13
矩阵、质量与可选 MCP 作业，发行另含 prepare、publish、github-release。
这次真实 Windows CI 验证了保留原期限与断言的 Win32 目标/provider 用例；
Ubuntu CI 同时保留合成托管进程恢复测试。Linux 原生 systemd 整链仍在独立
PR37，不包含于这些成功作业。

[GitHub v2.33.0](https://github.com/69gg/MuLi/releases/tag/v2.33.0)（release
`419383359`）于 2026-10-21 09:41:29 UTC 发布，非草稿、非预发布。
根与独立 reviewer 只读取小型元数据，确认与
[PyPI 2.33.0](https://pypi.org/project/muli-agent/2.33.0/) 的两个包名称、大小及
SHA256 一致，PyPI 均未撤回；没有下载发行包。

| 文件 | 字节 | SHA256 |
| --- | ---: | --- |
| `muli_agent-2.33.0-py3-none-any.whl` | 759925 | `11882151d8f5e06d3e00a720c2e1359d1c49c980b2f3b4768b89a6d5d503ce59` |
| `muli_agent-2.33.0.tar.gz` | 3884302 | `bec340c1b8fa84442689b649686dcd6c6d9fe0221457729f71848160b4966388` |

首次候选的两项 Windows 失败继续保留，不以重跑或改标签覆盖。发行作为检查点，
后续接收限额与独立原生服务验收继续推进。
