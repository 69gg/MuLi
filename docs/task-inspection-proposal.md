# 按需核查长期任务（2.39.0 已实现）

2026-10-23 的方案，已完成独立代码审查并随 2.39.0 发布。精确发行核验与保留的 main 失败见[审查记录](review-v2.39.md)。Linux 恢复验收独立推进，不以此功能替代服务验收。

## 已有基础

- `Commitments` 已保存目标、限制、下一步、修订和消息/附件证据位置；`commitment_read` 已有完整说明分页。`evidence_refs` 只允许消息与附件，不能把检索到的记忆冒称已采用的证据。
- `TaskControl.resolve_scope()` 沿 Team/Workflow 继承任务绑定；现有详情 API 返回当前 control 与 scope。暂停、补充要求和主助手确认已有 epoch/CAS 与稳定请求 ID。
- Team 有持久成员、运行及报告；Workflow run 已存任务绑定。应按现有任务关联查询，不能先截取全局成员列表再过滤，否则可能遗漏同任务成员。
- `Memory.search()` 是本地关键词/图查询；异步记忆流水线与模型提炼另行运行。Memory 有 revision、来源核查、更正与遗忘入口。首片不调用模型、embedding/reranker 或重建记忆。

## 一个窄入口

候选是只读 `commitment_inspect` 与同源认证 GET，复用现有任务范围解析。主助手按既有权限选择任务，绑定队友只看其范围；读取不改变主身份或回复渠道。

返回当前目标/计划定位、状态/下一步、control epoch、有限成员与执行报告定位、消息/附件依据定位。完整说明和大正文继续走既有分页入口；每项记录完整，超限明确分页、缺失或拒绝，不用静默节选冒充完整依据。

可选显式本地记忆查询单独展示为“检索候选，是否曾采用未知”，保留记忆 ID、revision 与核查入口，不自动改写 `evidence_refs`。不开启查询时不以最近记忆充数。用户补充的要求和资料仍按原权限区分，不因出现在核查结果中成为新授权。

分页固定任务修订、control epoch 和相关记忆 revision；变化时明确失效或要求重读。活跃报告的新增/更新须单独标明观察窗口，不能由这些三个 token 宣称获得跨全部对象的强一致历史快照。具体游标合同在实现前完成独立审查；不新增持久副本、任务账本或后台轮询服务。

纠正沿原入口：记忆使用已有编辑/忘记能力，任务要求使用暂停后的 `request_replan` 与主助手确认。核查视图没有新的写权限，不唤醒队友、不启动模型、不自动确认计划，也不将缺失报告推断为完成。

## 两项关键验收

1. 读取期间任务修订、epoch 或记忆变化/遗忘，旧页不冒充当前事实；失效、已删除依据、未完成报告和正文省略分别明确。固定证据与相关候选始终区分。
2. Web 主人和绑定队友获得一致定位及各自原有权限，查看为零业务写入、零模型调用、零唤醒；纠正后仍经原确认门，不能借读取或旧页恢复副作用。

## 首片实现合同

以下是实现者决定，复用现有 Python/SQLite/FastAPI 和工具注册，不增加依赖、持久格式或前端面板：

- `commitment_inspect` 与 `GET /api/commitments/{id}/inspection` 共用读取服务。`section` 为 `overview/task/agents/reports/workflows/evidence/memories`；默认 overview 给出任务元数据、控制摘要和各段入口。
- `task` 返回当前完整持久任务 JSON 的 UTF-8 字节页，包含 brief 和 plan；与 `commitment_read` 的历史 brief 页不同。不可调用会选择重规划的包装 handler。
- agents/reports/workflows 在 SQL 中先限定任务及混合父链，再按 ID keyset 读目录；列表不装载 prompt/result/nodes/content。可用 `record_id` 从同入口读取一条完整持久 JSON 的字节页，包括尚未投递的报告。正文只证明已保存内容，不能恢复报告生成前已丢弃的结果。
- `revision` 绑定资料库、任务内容、section、query 以及记忆段的 memory revision。目录 cursor 绑定该 revision；正文续读另带 `record_revision`，记录改变即失效。每页重做权限与范围核对。列表是 live 观察，新增/更改可能发生在页间，不能称作完整历史快照。
- evidence 逐项列出原 brief 的消息/附件定位及 available/missing，不因引用删除而丢失整个任务视图。原文继续走既有来源读取；不恢复已删除内容。
- memories 必须提供非空 query，只调用现有本地 Memory.search 的有界候选集，按 ID 分页，明确是相关候选、是否曾采用未知。记录正文可按 ID 读取，来源核查继续走现有 memory_source_read；不注入最近记忆或调用 embedding/模型。
- 所有 JSON 元数据和正文共用现有 max_output_chars。无法完整容纳一项时留给下一页；连首项也放不下就明确失败。正文复用 content_page 的 UTF-8 与必须推进约束。读取在一个可嵌套的只读 savepoint 内完成，不提交外层事务。

首片不新增纠正写入口；主人仍通过现有记忆更正/遗忘、任务暂停/重规划来纠正。独立审查确认没有需主人再决策的阻断，用户既有 YOLO 授权继续有效。
