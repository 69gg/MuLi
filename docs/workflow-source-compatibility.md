# 工作流旧资料物化的前序消费者附录

本附录对应普通工作流资料物化。宿主把完整来源正文、准确身份和低信任说明写入普通
`node.prompt`；执行仍使用原有 `mode`、`parent_run_id`、`parent_session_id` 和
`reply_origin`。当前实现没有新增来源旁证 kind、执行状态或恢复队列。

## 固定范围

[`workflow_source_v2_138`](../tests/fixtures/workflow_source_v2_138/) 来自已核验 Good
签名的 `5186d29`（2.138.0 前序候选）。目录保存完整 `workflows.py`、Runtime
`_drive` 原行字节和兼容声明；完整提交、原文件摘要、片段行号及摘要在
`manifest.json` 中，测试另行固定四个 fixture 摘要。原缩进片段只在加载时 dedent。
这是固定前序相关源码消费者回归，未安装运行完整旧发行包，也不代表该前序候选的
完整跨平台 CI 通过。

旧 `WorkflowEngine` 包含原 `_prompt`、正常接纳/调度、`publish_results`、等待及
`_recover`；旧 Runtime `_drive` 保留父运行等待、全成功门、发布和 finalizer 全路径。
globals 取当前 Runtime 模块字典的副本，其中对象引用共享。当前 Store、EventBus、ReplyOrigin、
TaskSnapshot 所在四个模块与基线逐文件同字节；`run_step`、`_execute`、`_repair_tail`、
`_settle_shell`、`_stopping_session`、`_wake_main` 六个 Runtime 方法含装饰器按原字节
核验。其它 Runtime 组件、工具、渠道消费者及 provider 测试替身使用当前实现。

## 实证入口与边界

[`test_workflow_source_compatibility.py`](../tests/test_workflow_source_compatibility.py)
在两条现有完整 Runtime 旅程中绑定真实旧类和旧 `_drive`，复用真实当前工具写入来源与
普通新 DAG。断言覆盖失败源不发布部分成功、新草稿和评审实际执行、新父全成功后
发布、本轮 Web origin、旧成功节点未重做，以及正常重开。另一条旅程先由真实外发
HTTP 超时得到 `unknown`，再核查它在后续执行与重开中保持一次尝试。

测试在新普通节点已真实进入 `running` 并持久提交事件后备份临时 SQLite；原运行继续
正常完成。旧消费者重开备份只把未收口新运行及节点标为 `interrupted`，不启动模型、
工具或发布。旧来源定义、运行、父运行、节点会话和原 producing run 比较原始
`objects.data`；unknown outbox 比较全部原始列。渠道消费者实际越过新事件，验证无重放。
没有替换 `_execute`、`run_step`，没有手写 completed/unknown 记录。

覆盖仅限该固定前序的普通未绑定、非定时路径和 `yolo` 模式，不代表完整旧包、其它历史
版本、真实渠道或模型的验证。跨平台结果以对应版本的 CI 为准。冻结的旧 `_admit_start`
保留原接纳行为，本回归不验证它在 task spawn 失败时的修复。
