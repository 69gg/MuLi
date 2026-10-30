# 固定前序工作流消费者

主 agent 从签名核验为 Good 的 `5186d29`（2.138.0 前序候选）提取；精确完整提交、
原始文件摘要、Runtime 片段行号和共享依赖摘要见 `manifest.json`。

- `workflows.py` 保留完整原始模块。
- `runtime_drive.py.txt` 保留 `Runtime._drive` 含原缩进的连续原始行，仅加载时 dedent。
- `compatibility.json` 保留该候选的原始兼容声明。
- 三个原始 fixture 和 manifest 的摘要另由兼容性测试固定。

不要格式化或手工改写原始 fixture。共享当前 Store 等依赖的边界见
[`workflow-source-compatibility.md`](../../../docs/workflow-source-compatibility.md)。
该目录用于固定相关旧源码消费者回归，未安装运行完整旧发行包。
