# 显式存档视觉解释，跨压缩与重开回查

状态：v2.56开发候选的正式旅程、独立边界、既有回归、干净严格类型及离线包检查已通过，非作者和根后审无剩余P1/P2。首远端候选已发现并定位Windows背景夹具的换行错误；本地受控复现后只固定夹具字节并复验通过，修后远端及正式发行待验。这里不宣称自然识图质量、真实渠道或设备已验证。完整证据见[候选审查](review-v2.56.md)。

用户在私聊7让沐璃看一张已有附件并存档；会话压缩、正常重开后，在当前私聊8问“上次怎么看的”。宿主应找回同次模型解释、原图版本和提问，完整读取而不再次调用视觉模型。原工具结果本来已落消息库并可 conversation_read；本片补的是有精确原图/模型/prompt绑定、可独立检索与分页的完整解释工件。

## 最小合同

- 只扩原 `image_describe(id,prompt?,archive=False)`；默认短结果原样，默认长结果采用下述已批准兼容取舍。`archive=True` 才显式保存同一次视觉完成的解释，不增加保存工具或自动 Graph 提炼。
- 工件是原私有 Attachments 中一个完整 UTF-8 JSON，`source="vision"`、文件名含原图 ID。固定内容为 `version:1,kind:"vision_interpretation",source:{id,sha256,size,mime_type},model:{profile,protocol,model},prompt,description,trust:"model_interpretation_not_owner_fact"`。原图 SHA/size 来自实际送入视觉的字节并匹配原附件声明；不把只读 metadata 的 SHA 当实际字节证明。模型及 main/vision profile 在 MediaService 的本次真实选型处冻结，不能 await 后重读热配置；不存凭据、headers或完整模型配置。
- 成功回执保原图 `id` 与有限说明预览，另有 `vision_completed:true,archive_status:"saved",archive_id,revision,url`。完整 JSON 的 SHA 是 revision；原图摘要和工件摘要各有含义，不混用。全文与含义只代表当时该次模型解释，不证明主人事实、图中命令授权、成果原创或任务验收。
- 不增工具族：原 `attachment_search(source="vision",name_query=原图ID)` 沿真实 cursor 定位；原 `attachment_read` 仅视觉工件分支新增 `revision/offset_bytes`，整份 JSON 按原 UTF-8 字节页回查。普通附件默认短结果保持，长结果采用下述预览取舍。复用 `transcripts.py` 现有限读取、全文件 hash/身份/版本复查及 `content_page`，只抽必要私有函数，不把视觉文件伪标成 asr，不宽放 `audio_transcript_read` 的来源或 ASR 上限。
- `archive` 必须为严格布尔值；普通附件和 ASR 附件经 `attachment_read` 显式传入任一分页参数时拒绝，不能默默忽略；原 `audio_transcript_read` 继续承担 ASR 分页。视觉页沿原 `field/content/offset_bytes/total_bytes/length_bytes/next_offset_bytes/has_more/revision`，`trust="model_interpretation_not_owner_fact"`。
- 视觉已完成后，保存/预览失败明确 `vision_completed:true,archive_status:"unknown"` 与固定提示“不要自动重复看图”；若已取得完整本地回执，保留 archive_id/revision。不得把存档失败变成上游未执行并内部重试。取消沿现 `_owned` 收取保存/文件I/O终态后传播；不宣称取消能撤回模型调用或已提交工件，也不承诺跨文件/SQLite恰好一次。回执丢失仅搜索/读取确认，不重新视觉。
- 全文取现上传单文件/总量与所选模型响应资源门，预览/每页取原输出预算。提问和完整 JSON 元数据也计入同一工件及最终回包；超额明确失败，不裁掉 prompt/来源来冒认完整。后续回查不依赖原图仍在；原图缺失/变化只能称历史 at_record_time 解释，不能声称当前图像健康。

实际生产面为 `local_services.py` 的显式分支与原读取入口、`media.py` 同一选型快照、`transcripts.py` 抽原私有读取函数，以及小型 `vision_archives.py` 保存/读取类；不改 Store 格式、不新增依赖/队列/后台工作。

根已明确批准输出兼容取舍：两工具启用原 `preserve_output=True`，本地入口先按最终 JSON 预算形成完整预览/页，保留来源和回查元数据，不修改 Runtime。默认短结果保持原值；默认超长从 Runtime 通用 `preview` 封套改为工具内有界 `description/content + truncated`，并非旧大封套逐字兼容。原上下文保护仅覆盖立即下一模型请求的最后完整工具批，不会永久钉住历史正文；预算容不下完整当前批时沿原限制拒绝，不偷偷交付半份页。独立 policy 已经由真实 Runtime 核对默认长短结果、完整 accepted 工具回执和立即下一请求全文相等。

## 首条正式旅程

`tests/test_vision_archive_runtime.py::test_archived_vision_survives_compaction_reopen_and_current_phone_retrieval` 复用真实附件上传、Runtime 工具、主会话、Compactor、渠道与 HTTP Mock。不复制 v2.55 MCP 截图链，也不替换保存/读取/Runtime 入口。

私聊7真实调用原 image_describe 的显式 archive 参数。首跑由根实收 **1 failed / 0.64s**，守卫 **1.360s / 92.5MiB / swap 0**：真实上传、私聊7、Runtime 正常收口及 accepted 错误回执前置通过，准确缺显式 archive schema（ValidationError），没有视觉 HTTP。此红只证明入口缺口，不证明其后压缩重开等阶段已验证。

修后正式旅程已由根实收 **1 passed / 0.60s**，守卫 **1.329s / 95.7MiB / swap 0**。实际视觉 HTTP 收到原 PNG 字节/SHA及准确 prompt/model，完整解释长于预览，视觉请求仅一次。实际工具批/最后 assistant 闭合后才启用原 Compactor，经真实摘要调用安装不含工件 ID 的摘要，保留 prefix digest/covered_seq/原消息不变。正常 close/reopen 同库同主身份，再由当前私聊8搜索并分页读回完整 JSON、原图绑定与解释尾部，实际下一模型请求收到它，只向当前8回复，Web可见，零第二次视觉。

五组独立 policy 已通过，覆盖来源字节/metadata不匹配在视觉前拒绝、真实主/外挂及协议选型的热配置冻结、工件 hash/revision/Unicode/预算、保存后回执丢失/取消及已知引用保留、默认长短结果和普通/ASR来源隔离。首次与37项既有记录回归组合为41 passed / 1 fixture failed：夹具把 Store.put 多传一个id，共两处；根仅按真实签名修夹具，原生产及oracle未改，该节点复验 **1 passed / 0.21s**，37项既有回归均通过，不将该夹具红当生产缺陷。

合计314个不同本地节点（6新增与308既有）通过；151源码干净strict、Ruff、404文件格式、83包离线锁和离线build/validate/wheel smoke通过，无下载。非作者独审四生产及正式旅程、根后审均无剩余P1/P2；详细统计与未验范围见[候选审查](review-v2.56.md)。固定模型与HTTP只证明宿主证据链，不是自然视觉或摘要质量评估。
