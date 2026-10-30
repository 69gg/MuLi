# outbox 格式 3 的前序证据

以下四份文件由 root 逐字取自冻结提交 `4f78404f800f69c994d9829c0e37787eb4ac3ec7`。它们是 MuLi 2.144.0 的准确源码证据，不是旧 wheel 安装或跨版本服务运行的证明。

| 文件 | 原路径 | 字节数 | SHA-256 |
| --- | --- | ---: | --- |
| `format_admission_v2_144.py` | `src/muli/format_admission.py` | 5712 | `9ae2afcaf299bdc050ca7fede70bb381a7771fe1ed1c06aae183aa26f729ea24` |
| `compatibility_v2_144.json` | `src/muli/compatibility.json` | 480 | `5196aaa4d165e9519a87fb287dbd067259e9c93b705d6e07457323d5ba1aa433` |
| `previous_outbox_manager_v2_144.py.txt` | `src/muli/channels/manager.py` | 54362 | `9632ebdf4d10e00f99b27ba497f30728cf258b99fcb60c7143df9f9437e8e48d` |
| `previous_channel_history_v2_144.py.txt` | `src/muli/channel_history.py` | 18607 | `d9eb1c6d11fbb9b404429940101a1e0ec06ab49cd33ae1fbdf4f34e467de1f46` |

准确前序为 `(database=1, objects=4, configuration=2, ingress=7, outbox=3, memory=5, install_profile=1)`。新合同只把 outbox 改为 4，使准确单条待发取消成为不会被重试或重新领取的持久终态；它不表示平台撤回或证明过去没有外发副作用。

格式回归执行冻结的实际准入，要求它在任何业务写入或恢复前拒绝新标记。随后刻意绕过旧准入，用冻结的旧 wait 观察一次终态谓词，并读取新 cancelled 记录，说明旧等待与历史读取不能解释该状态；此有限只读诊断不运行旧服务、不等待物理超时，也不改变投递行。旧 objects3→4 回归同样以这份实际前序保留其原有边界，不把新 outbox 能力补进旧消费者。

四份原件以 `-text` 保留准确字节，测试核对上述 SHA-256；不得格式化、修改或为适配新断言提升其读写能力。来源记录与静态说明不替代检查终态。
