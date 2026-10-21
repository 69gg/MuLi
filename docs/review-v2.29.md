# 2.29 chiliarch 普通消息主动资料审查

已正式发布至 PyPI/GitHub：精确候选和 main 各八项、发行十一项全部通过，
原签名提交与两种构件元数据均已核实。2.28 的验证单独保留，不借用其结果。

## 合同与独立审查

默认关闭的本地策略使 `agent.message`、`collaboration.message` 的新资料
可以沿现有 Attention 进入持续主会话，等待当前工作自然结束；不新增模型
轮询、队列或数据库表，不自动回信 chiliarch。API 与主协调者工具使用同一
配置锁和权限门，scope/revision 由宿主提供，主 Settings 与配置格式不变。

接收时冻结独立默认路由；正文只是外部资料，不能成为主人授权、选择收件人
或获得写工具。来源适配按主键限量核对通知、原 inbox 前缀与宿主证明，
等待合并或配置锁后再次核对当前身份与策略。坏策略只拒绝本来源，不重置
未知对象、不拖住其它合法提醒。已接受事项保留历史身份，不宣称能撤回
所有旧入站或已经开始的发送。

原 `Store.ingest` 与外层通知事件分两次提交。作者用真实临时 SQLite 在旧
代码上复现通知事件写入异常未使 inbox/notice 回滚，再改为单一事务写入
inbox、notice、event；重复信封仅唤醒已保存事件，不重造资料。

父审查另发现旧 manager 在当前配置已换身份时，可能给旧 namespace 消息
生成新身份的通知 ID。修复让 hash 始终使用本次连接的 `receive_scope`，
当前策略仅决定证明是否有效；同协议 ID 在两个身份下的真实 SQLite 回归
确认通知和 inbox 均独立。两个源码审查均无剩余阻断。

## 作者验证

两个专用测试文件共 37 项通过（1.38 秒、147.7 MiB）；既有 integration
manager、chiliarch、attention、system events、proactive delivery 与应用
接线六文件共 134 项通过（1.75 秒、136.6 MiB），八文件不重叠，共 171 项。
首轮旧链事务红例峰值 74.8 MiB。新测试夹具曾修正来源字段、工具注册方式
及消费者初始唤醒与锁等待的因果门，没有为通过测试放宽生产预算或断言。

10 份 Python 的 Ruff/格式与 8 份修改源码严格 mypy 通过，类型检查峰值
174 MiB。全部串行使用共享 2 GiB、单次 1 GiB、单 CPU、30 秒守卫，swap 0。
没有真实 chiliarch、渠道、模型或用户服务连接；根分支整合与完整矩阵仍待
执行。这些证据不等于真实账号验收，也不计入尚未执行的 Windows Skills。

## 根分支整合与候选终检

签名实现 `5821d3f` 已整合为 `0336dce`，保留 2.28 的工作流来源与 AX 修复。
根分支重新验证上述八文件 171 项（2.76 秒、216.7 MiB），再独立验证
server、maintenance API、系统事件 Runtime、主动记忆及兼容/发行六文件
223 项（3.38 秒、200.3 MiB）。两组共十四个不同文件、394 项，包含作者
171 项，不重复累计。主配置字节、旧解析格式和关闭/维护边界均保留。

版本与锁文件、兼容声明同步为 2.29.0。全仓 Ruff、313 文件格式、132 源码
`mypy --no-incremental` 与 83 包离线锁核验通过，类型检查峰值 397.1 MiB；
全部 swap 0、原守卫串行执行，没有安装依赖。本版不改前端产品，完整网页、
打包及跨平台矩阵等待新候选 CI。

## 精确候选与发行启动

GPG 签名候选 `796bf311e4e183f50ab856a8586db1bab3c48a0e` 的
[CI 39546796431](https://github.com/69gg/MuLi/actions/runs/39546796431)
attempt 1 八项成功：quality、MCP 和 Linux/macOS/Windows × Python 3.12/3.13。
父审查与独立观察者分别核对精确 SHA 和完整八项结果，没有重跑掩盖失败。

[PR33](https://github.com/69gg/MuLi/pull/33) 于 2026-10-21T05:05:14Z 原提交
合入 main。签名标签 `v2.29.0` 与 main 一次非强制原子推送；本地标签验证为
有效 GPG 签名。独立 [main CI 39547575601](https://github.com/69gg/MuLi/actions/runs/39547575601)
与 [Release 39547575662](https://github.com/69gg/MuLi/actions/runs/39547575662)
尚待完成，现阶段不把标签存在视为 PyPI 已发布，也不把本版矩阵当作下一版
Windows Skills 原生句柄实现的验收。

## 发行后核验

上述 main 与 Release 均在 attempt 1 完成：main 八项、Release 的八项 verify
以及 prepare、publish、github-release 共十一项全部 success，精确 SHA 均为
`796bf311e4e183f50ab856a8586db1bab3c48a0e`。父审查另行核对两组完整作业。
远端 main 与标签解引用指向同一提交；标签对象
`1671e20df558518d3cb457a9cfa6fa503023b0c8` 及提交签名均 verified/valid。

[GitHub 发行](https://github.com/69gg/MuLi/releases/tag/v2.29.0) 于
2026-10-21T05:11:10Z（北京时间 13:11:10）发布，非草稿、非预发行。
独立观察者只读取 GitHub/PyPI 小型元数据，两个文件均未撤回，文件名、大小
及 SHA256 完全一致，没有下载构件：

| 文件 | 字节数 | SHA256 |
| --- | ---: | --- |
| `muli_agent-2.29.0-py3-none-any.whl` | 733393 | `da9a2b50aec6ffebb2cf24fa24c1bb9bc175b32fd177a10ab44ca93187bdef50` |
| `muli_agent-2.29.0.tar.gz` | 3762730 | `337c1a195b0e691b1a540e01f4bd63c8b055de182e1419cb9f40bf9cf25456f6` |

本版验证至此收口，不重复轮询或重跑；下一版 Windows Skills 和在线候选
获取分别使用自己的提交、审查及验收记录。
