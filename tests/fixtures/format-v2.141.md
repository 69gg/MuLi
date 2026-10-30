# 对象格式 3 的前序证据

以下文件逐字取自带有效 GPG 签名的提交 `0c630d4696913850750b3d08126ba30d1552496c`。它是 MuLi 2.141.0 的源码候选，不是已经发布或安装的旧 wheel。

| 文件 | 原路径 | 字节数 | SHA-256 |
| --- | --- | ---: | --- |
| `format_admission_v2_141.py` | `src/muli/format_admission.py` | 5399 | `aa73f4d7ee66bb0ad5ca25f7c7c5ddbb0f8d1e714ca2c2fb802320dd1d4f09ce` |
| `compatibility_v2_141.json` | `src/muli/compatibility.json` | 480 | `8bab43aa882602e1d97d2b9700ee9dd977726a2d6a793cbb6cd80afa765e514b` |
| `previous_compaction_v2_141.py.txt` | `src/muli/compaction.py` | 17101 | `d12889bb347e072ae552c686714b22f5ea68581623a3050f5958ca9fcf4a7904` |

前序状态为 `(database=1, objects=3, configuration=2, ingress=7, outbox=3, memory=5, install_profile=1)`。新格式只将 `objects` 提升为 4；旧程序须在业务读写前拒绝新标记。

准入回归加载冻结的实际前序验证器。压缩诊断先验证旧准入确实拒绝，再刻意绕过该门，将冻结的旧 `Compactor.project` 绑定到真实新 Store 的有限只读适配层，解释它为何不能保留新的多输入锚点语义。这不是旧程序正常启动、旧 wheel 运行、跨版本服务切换或实际恢复的证明。原消息、快照和数据库内容不能由诊断修改。

这三份原件由精确 `-text` 属性保留字节，禁止格式化或为适配新断言修改。诊断测试与准入测试分别核验对应 SHA；本文件只记录来源，不替代执行终态。
