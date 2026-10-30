# 主人问题答案格式的真实前序源码

本组 fixture 来自根 agent 指定并核验的已签名 Good 提交 `28cf2153b467bcf0e7f5a44a55eb6aa2f834918e`。其包版本及 compatibility manifest 的 release 字段是 `2.140.0`，当时是尚未发布的候选；这不是已安装旧 wheel、已发布基线或旧发行环境的证明。根 agent 通过该提交的 `git show` 读取原字节，在统一检查守卫内完成冻结和逐字节核验；本格式作者只静读、编辑，未运行 Git、hash、Python 或检查。

| 冻结文件 | 原路径 | 字节 | SHA-256 |
| --- | --- | --- | --- |
| `format_admission_v2_140.py` | `src/muli/format_admission.py` | 5104 | `5517cc6c614e3e5c884d9c2b1263e6d1d4191cea197aa7900052a17b455dd017` |
| `compatibility_v2_140.json` | `src/muli/compatibility.json` | 480 | `7607a0611d9f7a0e0bf6f79418766b3d14d2cdd4da13021990b3b0ce6ddbf19b` |
| `previous_input_consumer_v2_140.py.txt` | `src/muli/store.py` 中三个方法及明确的诊断 wrapper | 6162 | `2ab445fe2ce93e628717544308c84433cc298159e217ceb96b4bc1b0a9fdbf69` |

前两份是完整文件的原字节复制。第三份保留原缩进、装饰器和方法源码，外围仅加入模块说明、原方法所需 imports 和 `PreviousInputConsumer(Store)` 诊断类，不是完整旧 Store。原 `src/muli/store.py` 的 SHA-256 为 `67cd85fc6387d9c62025bafd1d6778e60daa17b1f29a51a1da4b8ee537eea19d`；根 agent 使用 AST 定位方法后截取包含换行的原始连续行，并确认提取字节未改。

| 原方法 | Good 提交中的原行号（含首尾） | 方法原字节 SHA-256 |
| --- | --- | --- |
| `claim_inputs` | 1913–1976 | `dbf5991490e8b5b308bfef05e91fc9a74f7ba495a6882a1e081fd24c1800bfb9` |
| `input_archive` | 1978–2021 | `6e00357fd43b5c9b1b1d2253a57ef2aa729a373a97a80196a9f2e90dd87cd844` |
| `_archive_input` | 2023–2036 | `4b80989de4f7348ba54a60dc682ea73d4924ddceb3fdf4748245500d8bb70b28` |

`.gitattributes` 为这三条精确路径禁用换行转换；旧方法 fixture 使用 `.py.txt` 后缀，避免 Python 格式器重排历史方法。测试先校验以上完整 fixture 的 SHA-256，再经 `SourceFileLoader` 执行冻结模块。旧 admission 仍复用当前共享的格式模型和证据类型；方法诊断 wrapper 仍使用当前 Store 的建库及通用 `get`、`_put` 等支撑。这些共享部分不能被描述为“整个旧 Store 未修改”，也不能据此声称已完整运行旧版。

正式准入测试执行实际旧 `require_current_format()`：精确旧完整合同 `database/objects/configuration/ingress/outbox/memory/install_profile = 1/3/2/6/3/5/1` 可读，新完整合同 `1/3/2/7/3/5/1` 在业务写入前被拒绝。当前 reader 对精确前序 ingress6 要求显式升级；受管 A/B 同格式切换在两个方向均拒绝跨 ingress 合同。即使人为同时声明读取 6 和 7，写入合同变化仍不被同格式切换接受。

方法级反证故意通过 `__new__` 绑定测试 SQLite 连接，绕过旧程序本应先执行的格式准入。两个同路由答案具有不同的 `owner_question_answer`，该宿主 marker 的固定字段是 `version=1`、`question_id`、`answer_revision`、`task_id`、`worker_id`、`delivery_key`。在不存在相应问题、答案、任务和 worker 绑定的诊断数据上，真实旧 `claim_inputs` 仍会一次领取两个输入，旧归档白名单丢弃 marker；当前无协调服务的 Store 则拒绝领取。这个受限反证解释了 ingress 提升的原因，不表示旧版能够正常启动 ingress7 数据库。完整的新问题绑定、修订、brief、epoch、worker、取消和领取原子性由协调服务及 Runtime 旅程测试覆盖。

停机升级继续复用 `upgrade-channel-ownership` 的既有独占、持久性、`BEGIN IMMEDIATE`、CAS marker、回滚和关闭边界，仅把精确完整 marker 推进至当前格式。Linux/macOS 各自保留原先允许的全部精确前序，并新增 ingress6 前序；升级器不补造问题、不推断聊天答案、不消费 pending、不改业务或配置行、不重放回执及 unknown 输出。Windows 和受管跨格式升级范围未扩大。Linux 上注入 Darwin 分支不构成原生 macOS 验证。

旧 `format-v2.139.md` 和 v2.139 fixture 保留其历史 5→6 证据；它们不能代替本组真实 6→7 前序。当前候选版本与以上冻结来源分开维护。格式作者未执行新增测试，测试结果由根 agent 在统一守卫内实际收取后单独报告。
