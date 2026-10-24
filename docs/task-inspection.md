# 按需核查长期任务

沐璃可使用 `commitment_inspect` 回答“这件事做到哪了、依据是什么、谁还在处理”。管理客户端使用同一认证入口 `GET /api/commitments/{id}/inspection`。读取不选择重规划、不确认方案、不唤醒队友，也不调用模型或向量服务。

## 内容

| section | 内容 |
| --- | --- |
| `overview`（默认） | 有界任务元数据、控制摘要与可读取分段 |
| `task` | 当前完整持久任务 JSON，包含目标、限制、下一步及当前计划正文 |
| `agents` | 本任务及混合 Team/Workflow 后代的队友状态目录 |
| `reports` | 任务报告目录，包括尚未投递给父会话的报告 |
| `workflows` | 任务工作流运行，包括旧记录沿父链继承的运行 |
| `evidence` | 原任务说明的消息/附件引用及 `available/missing`，不因原文删除丢掉整个任务 |
| `memories` | 必须提供非空 `query`；本地关键词/图候选，`candidate_usage="unknown"` |
| `handoff`（开发中） | 必填 `agent_id`，指定队友的有限近期证据索引；不自动确认计划或接班 |
| `deliveries`（候选） | 显式登记的不可变验收单目录，分别呈现宿主文件事实和模型判断，并计算当前/历史状态 |

主助手填写 `identifier`；绑定队友可省略，只能读取当前任务。每次调用都按真实会话重新解析范围，复制主助手的工具 schema 或旧游标不能继承主身份。Web 使用既有管理认证，不接受请求提供的 agent/owner 身份。任务资料、报告、来源和记忆都不授予新权限。

## 分页

目录接受 `limit`（1–100，默认 20）和 `cursor`，返回完整 `items/has_more/next_cursor`。沿返回游标继续，不按全局列表截取后过滤。关联检查只读父链字段；目录不装载队友 prompt/result、工作流 nodes 或报告 content。某项及游标放不下时留给下一页，首项也放不下则明确返回 `output_budget`。

agents/reports/workflows/memories/deliveries 可用同一 section 和 `record_id` 读取一条完整持久 JSON；task 直接读正文。返回 `content/offset_bytes/next_offset_bytes/total_bytes/has_more`。续读同时回传 `revision`、`record_revision`、`next_offset_bytes`，可选 `length_bytes`；按 UTF-8 **字节**位置续读，拼接 content 后解析 JSON。整页元数据与正文共用 `max_output_chars`，保留给下一次模型请求，不能放下时明确拒绝。

正文仅证明 `source_completeness="stored_record_only"`：报告入库前可能已经限长，无法恢复此前丢掉的结果。消息继续用 `conversation_read` 的 session/message/seq 定位；附件使用既有附件工具。记忆及来源可用既有详情和 `memory_source_read` 核对，不能把“相关候选”改写为“任务曾依赖它”。

## 变化与纠正

`revision` 绑定资料库实例、完整当前任务、section、query；记忆段还绑定全局 memory revision。任务、更换查询、记忆更正或遗忘使旧页返回 `evidence_changed`（HTTP 409）。单条正文另用 `record_revision` 校验完整记录，状态或内容改变须重新读。已删记录明确缺失（HTTP 404），不从旧摘要复活。

目录标记 `consistency="live"`，按稳定 ID 翻页；活跃队友、报告和运行可能在页间变化。任务版本不是全部对象的历史快照；新增条目落在已读 ID 之前时须从首页查看。记忆只是现有 `max_candidates` 内的当前候选集，按 ID 展示，不声称列出所有相关记忆。

`deliveries` 的目录与正文另返回 `current`：任务 revision/epoch 或准确直接队友完成代次变化后为 false，原单仍可读。正文的 `brief/host_facts/model_judgments` 是登记时的冻结资料；`host_facts.verification="at_record_time"` 只表示当时实际核对过附件副本的完整 UTF-8 字节、size/sha256，不证明文件由该代新写出或内容正确。是否满足目标与限制明确保留为模型判断，未验项不能解释为宿主通过。普通查询不再次读取附件，也不把历史单恢复为当前。新段读取先过原始对象字节门，再沿相同输出预算分页，不扫完整历史。

该候选的验收单 ID 为 97 字符的不透明标识，按完整任务摘要前缀使用现有主键范围分页，不扫描其它任务全部验收单。客户端应原样回传 `record_id` 和游标，不按附件的 32 字符规则校验，也不从 ID 推断事实或权限。坏主键占用已选择的有限窗口，明确返回 `content_unavailable`，不跳过补取或改写记录。

纠正沿原入口：更正/遗忘记忆，或由主人暂停任务、追加要求并交沐璃重新规划。核查不触发 `commitment_read` 包装 handler 的重规划选择，更不能借旧页恢复暂停任务。读取使用可嵌套的只读 savepoint，保留调用方已有事务，不增加持久格式、缓存副本或后台轮询。显式读取单条正文仍需读取该持久记录，并不宣称是对任意巨大对象的流式存储接口。

新接办段使用独立的有限窗口与版本合同，不改变上述旧目录的 live 语义；详见[按最新证据接办](task-handoff.md)。设计见 [方案记录](task-inspection-proposal.md)。真实外部模型和用户渠道的使用效果需另行验收；离线 API、持久记录与工具回归记录在版本审查中。

显式验收工具与本轮候选验证状态见[任务交付验收单](task-delivery-evidence-proposal.md)。查询不登记新单，不改变普通 completed 或任务状态，不触发外发。
