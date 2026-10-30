# 固定前序工作流消费者

主 agent 从签名核验为 Good 的 `5186d29`（2.138.0 前序候选）提取；精确完整提交、
原始文件摘要、Runtime 片段行号和共享依赖摘要见 `manifest.json`。

- `workflows.py` 保留完整原始模块。
- `runtime_drive.py.txt` 保留 `Runtime._drive` 含原缩进的连续原始行，历史加载方式仅做 dedent。
- `compatibility.json` 保留该候选的原始兼容声明。
- 三个原始 fixture 和 manifest 的摘要另由兼容性测试固定。

不要格式化或手工改写原始 fixture，也不要刷新 manifest 或固定摘要来声称当前依赖仍与
旧版本逐字共享。manifest 的共享 Store 及 Runtime 方法摘要属于 2.139 / ingress5 的
历史同格式证明；2.140 已增加原生引用和 Store helper，并使用 ingress6，当前不再以该
历史摘要放行旧工作流执行器。

当前兼容性回归保留这四个文件的原始摘要校验，实跑当前执行器的完整来源旅程及正常重开，
再让固定的 2.139 / ingress5 admission 读取真实 running DAG 的完整 SQLite 备份。
预期结果是业务恢复前以 `store_format_unsupported` 拒绝，全部业务对象、输入账本、配置和
outbox 原字节不变；unknown 仍为第 1 次尝试，且不恢复、不调用模型、不发送。

完整边界见 [`workflow-source-compatibility.md`](../../../docs/workflow-source-compatibility.md)。
本目录保留相关旧源码证据，当前测试未安装运行完整旧发行包，也不声称旧执行器可在
ingress6 上恢复。
