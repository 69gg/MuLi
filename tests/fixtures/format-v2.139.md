# 渠道原生引用输入格式的旧 reader

`format_admission_v2_139.py` 与 `compatibility_v2_139.json` 原字节来自根指定的 Good 提交 `88340994f70fd0417868981dcda81667d8fa2f26`（包版本 2.139.0）。复制源为当时仍保留该格式定义的工作树 `/data0/MuLi/.local-check-artifacts/worktrees/workflow-sources` 中的 `src/muli/format_admission.py`、`src/muli/compatibility.json`；未从已修改的新格式文件生成或重写旧逻辑。本实现子 agent 未执行 Git 或签名验证，提交来源由根提供；根在统一守卫内核验了文件与原复制源逐字节相同、字节数及 SHA-256。

| 冻结文件 | 原路径 | 字节 | SHA-256 |
| --- | --- | --- | --- |
| `format_admission_v2_139.py` | `src/muli/format_admission.py` | 4803 | `123cc51450e3cc66a71fb06b2b2616e2f35eaf39843eb5ef14eb55813f419ec8` |
| `compatibility_v2_139.json` | `src/muli/compatibility.json` | 480 | `3e835cbdc9edc63d3109c23e240afce5e9e0d441e95f8afcfbc3eccf10ee1df8` |

两份 fixture 由文件字节复制产生，`.gitattributes` 精确禁用它们的换行转换。测试先校验 SHA-256，再通过 `importlib` 执行冻结的旧 `require_current_format()`；旧 consumer 的判断未在测试内重写。测试复用当前共享的格式模型及证据读取器，因此它证明实际旧准入源码会拒绝 ingress6，不等同安装旧 wheel 或完整运行旧发行环境。

旧完整合同依顺序 `database/objects/configuration/ingress/outbox/memory/install_profile` 为 `1/3/2/5/3/5/1`；新完整合同仅 ingress 改为 6，成为 `1/3/2/6/3/5/1`。旧 adapter 会抹去原生引用 A 与 B 的唯一区别，旧归档白名单也会丢失定位字段；新非空引用参与入站去重指纹，改变了 pending 输入的明确所指对象及恢复语义。因此即使表 DDL、outbox receipts 编码都相同，旧 reader 也不能安全消费新 pending，不能把它解释为可按最近上下文处理的普通输入。

真实冻结旧 admission 拒绝新 marker；新 admission 要求旧 ingress5 完整合同显式升级；受管同格式切换在两个方向均拒绝跨格式。既有停机 `upgrade-channel-ownership` 工具在 Linux/macOS 原支持范围内只推进精确 marker，逐字保留所有输入、归档、入站 receipt/fingerprint、outbox receipts 和 unknown 状态，不回填原生引用、不领取 pending、不恢复工作、不向平台发送。Windows 仍按既有策略拒绝该升级；Linux 上模拟 Darwin 分支不构成原生 macOS 证据。

实现子 agent 只静读、复制文件和编辑，未执行上述测试、hash、Git、网络或任何检查；本表字节证据由根在统一守卫核验后提供。完整 native-reference canonical/归档持久性和实际工具旅程由独立 ingress/读取路径测试覆盖，本文件仅定义格式边界与停机升级保留合同。
