# 带证据的任务交付记录（2.48 审查与发行）

2026-10-24，2.48.0 已完成正式发行核对，精确源码为 `312f4abc1bc2e562829b64eaddece9fef6cb84dc`，结果见末节。非作者静态审查覆盖 `task_delivery.py`、`local_services.py`、`task_inspection.py`、`task_file_publication.py`，以及 Runtime 保存实际工具结果时的 publication 增量、正式旅程与 reader/limits 回归，未发现必须修复的 P1/P2。下述候选阶段本地验证与失败记录保留，发行闭合不替代真实外部账号联调。实现合同及先红历史见[交付证据方案](task-delivery-evidence-proposal.md)。

## 实际行为与权限边界

主 MuLi 可显式登记一个直接队友、一个准确完成代次和一个本地 UTF-8 附件副本的交付依据。单中冻结任务要求与版本，分开保存宿主文件事实和模型逐项判断；它不改变任务状态、计划批准、暂停、执行权限或出站授权，也不自动发送附件。

`host_facts` 只证明该次任务执行实际复制过这些字节，以及登记时完整本地副本的 UTF-8、大小和 SHA-256 核验，即 `at_record_time`。这不能证明文件由该执行新写出、内容正确、满足任务或主人已验收。`model_judgments` 是模型的判断，未验项目仍须标明 `unverified`，不升级为宿主或主人认可。查询中的 `current` 只核任务及准确执行版本，普通查询不重新读取文件。

发布证明从真实工具执行上下文、任务快照和直接父子关系取得来源，复制后再次复核。pending 不能用于登记；只有准确持久 assistant 调用、实际参数和完整 accepted 工具结果一致时，才把 recorded 证明与结果消息放在同一 `Store.put_many` 事务中。取消、unknown、截断、要求变化或提交失败不取得 recorded 证明；已经复制的附件可以保留，不领养旧附件或 pending 前缀补造成功。

登记工具只提供给主会话，handler 再核实际主运行和写入准入；模型不能提供宿主身份或文件事实。它交叉核对任务 revision/epoch、直接队友、完成 run、唯一准确 `team.finished`、报告、publication 和实际工具消息。附件在原有限窗口内完整读取，所有 await 均在写事务外；之后在 `BEGIN IMMEDIATE` 内同步复核身份及原持久证据，验收单、去重凭据和事件共同提交。相同去重键和参数返回原单且不重新读取附件，内容不同拒绝；任务变化后的原单可作为历史读取，不伪装成当前依据。

目录和正文复用原 `commitment_inspect` 及认证 inspection HTTP 入口。新单 ID 为准确任务 ID 的完整 SHA-256 前缀、冒号和原随机后缀，共 97 个 ASCII 字符，客户端应原样回传。目录沿现有 `(kind,id)` 主键做任务范围 keyset，再核真实 `commitment_id`；主键在 SQL 内先过原字节门，以 BLOB 跨入 Python 后严格解码。损坏行拒绝，不跳过补取或改写数据；不按全部交付记录扫描其它任务 JSON。不新增 Store 格式、索引、依赖、后台轮询或通用工具账本。

## 失败证据与已收验证

先行真实发布旅程曾在写入、复制、读取、完成与重开之后，因缺少准确 `task_file_publication` 关联取得功能红。显式登记 API 的首轮夹具调用参数重名导致等待超时，不能当成功能红；修正夹具后才准确到达工具未启用的目标红。独立负控还实际暴露过合法 surrogateescape 路径被可选证明破坏、超额宿主字段先跨入 Python、坏目录主键越过字节门及无关任务引起目录工作量增长。具体红/绿数与修正范围保留在方案文档，本审查不抹去失败历史。

以下结果由根通过 `scripts/check_local.py` 串行取得，审查者没有另跑检查：

| 范围 | 实际结果 |
| --- | --- |
| 发布、登记、正常重开、失败前缀与 reader/limits 三文件小集 | 15 passed；pytest 1.73 秒，守卫 2.665 秒、202.8 MiB、swap 0 |
| 新增同尺寸副本改字节、回执提交失败两参数 | 2 passed、9 deselected；pytest 0.82 秒，守卫 1.603 秒、140.9 MiB、swap 0 |
| 既有核查、认证 HTTP API、Runtime、LocalServices、队友附件及任务控制六文件 | 93 passed；pytest 2.60 秒，守卫 3.543 秒、205.3 MiB、swap 0 |
| 发行元数据与兼容声明两文件 | 166 passed；pytest 0.95 秒，守卫 1.452 秒、68 MiB、swap 0 |
| 全源码 strict 类型检查 | 实际 149 文件通过；守卫 8.603 秒、416 MiB、swap 0 |
| 全仓 Ruff、格式与离线锁文件核验 | Ruff 通过，386 文件格式通过，`uv lock --offline --check --no-python-downloads` 通过 |
| 离线 wheel/sdist 构建 | `uv build --offline --no-python-downloads` 通过；守卫 1.413 秒、105.8 MiB、swap 0 |
| 发行一致性与离线安装冒烟 | `scripts/release.py` 通过；`scripts/check_wheel.py` 的隔离安装、CLI 与本地 ASGI 冒烟通过，守卫 1.310 秒、103.2 MiB、swap 0 |

前四行互不重叠，共 276 个测试节点；其中首行包含九条正式发布/登记旅程及六项 publication/reader 边界。此前历史小集与这些结果有重叠，不再累计。固定模型策略、临时 SQLite/文件和本地 ASGI 验证的是执行关联、证据保存、准入及读取合同，不是自然模型判断质量或真实平台送达。

两项 `changed_copy/receipt_commit` 已取得上述实际通过终态：前者保持副本大小和持久元数据不变，只改变有效 UTF-8 字节，真实摘要核验拒绝登记；后者在单已写入当前事务后注入 receipt 写入失败，单、去重凭据与事件全部回滚。构建和安装均只使用现有本地运行时与依赖，不下载包或模型；构件位于临时目录，未修改真实用户数据。候选阶段本地结果不单独构成远端发行成功证明；后续实际发行终态另记如下。

## 正式发行闭合

根已核对精确 `312f4abc1bc2e562829b64eaddece9fef6cb84dc` 的 [Release 40062974570](https://github.com/69gg/MuLi/actions/runs/40062974570) 共 13/13 job success。[GitHub v2.48.0](https://github.com/69gg/MuLi/releases/tag/v2.48.0) 为 `draft=false`、`prerelease=false`，发布时刻 `2026-10-24T04:44:46Z`。PyPI 两个发行文件均未 yanked，逐文件 size 与 SHA-256 同 GitHub 构件完全一致：

| 构件 | 字节数 | SHA-256 |
| --- | --- | --- |
| wheel | 829646 | `1e42601b95e43778d2dee00f5fe55a926edc423a3a53186be53092ce288630a9` |
| sdist | 4351959 | `f73f0f57f4cb752f2feb5d613d4fdcb22c0053e277f38e23439960b125c67d38` |

本节仅在后续工作树记录已经取得的发行实证，没有改写已签名 tag，也没有据此新增真实模型、账号或平台送达的验收声明。

## 仍有边界

正常关闭重开覆盖的是已提交记录、准确重复和历史读取，不宣称任意崩溃后的执行恢复。旧代、孙代及其它坏证明由实际身份和有界证据门拒绝，但现小集没有逐一穷举全部组合；不把局部门禁等同于完整跨源端到端矩阵。现有旅程明确验证零文件出站，真正发送仍须走既有显式渠道工具并重新授权；撤销、unknown 不重发和真实渠道送达保持原独立合同。本版审查不开放 Project 格式迁移或原生服务恢复 WIP。
