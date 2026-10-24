# 要求变更后的成果复核

2026-10-24，基于已发行2.48与现有任务/交付单。回答“要求变了，这份成果还能用在哪、还需补什么”。本片先写真实端到端目标红节点，根实收真实功能红后授权窄实现；当前生产候选待根验证与独审，不填写通过、版本或发行结论。2.49约定复盘仍是独立候选，不以其入口或目录名称推断已交付能力。

## 最小读取合同

扩展原 `commitment_inspect` 的 `section="delivery_review"`，使用已有 `record_id` 显式指定同任务的准确 delivery ID；不另造工具、持久ledger、语义diff、后台调度或自动重派。原 deliveries 目录与原单正文不变。新section只读一个当前任务与一张明确旧单，沿原会话范围/认证HTTP、字节门、revision/record_revision和UTF-8字节分页；不搜索“最近一单”、不扫描全部历史、不读附件或更新旧单。

`content` 是可完整续读的JSON：version、commitment_id、delivery_id；`recorded` 与 `current` 各有 task_revision、epoch、规范完整 brief；`changes` 只给 goal、constraints、evidence_refs 三个准确字段不相等布尔；保留旧 `host_facts`，将原模型判断明确置于 `historical_model_judgments`，`judgment_scope="recorded_requirements_only"`。分页外层保留原整单 current 布尔和 `file_verification="at_record_time"`。当前任务再变更使旧 revision 失效；record_revision核完整复核正文，不能把旧页与新要求拼接。

brief 使用现有严格格式。goal可按字段定位，constraints以各自版本的数组索引定位，保留顺序、重复、全文和evidence_refs。调序也算数组变化；重复文本不去重，不建立跨版本语义等价关系。changes只证明持久文字/数组变化，不证明不变文字仍合格，也不把旧索引的判断迁移到新索引。不存在控制字段时epoch沿原 `_state` 的0语义，不为读取补写控制状态。

两版brief引用均只作为规范持久定位/声明，不重新读取消息或附件正文，不证明来源现在仍存在或可信。仅此读取可在 `stored=True` 下复用validator的声明核对模式：仍严格核完整字段集合、UTF-8、kind/id/session_id/seq及精确类型，保留准确原值。创建、更新和默认调用继续解析并核查来源；未持久brief不能使用此模式。原对象字节门和双revision分页不变。

宿主只给上述事实：交付单的文件核验是登记时的副本/UTF-8/size/hash及准确执行关联，不证明新写、内容正确或满足新要求。模型必须另行说明可能复用部分、待核验部分和建议补做内容，明确这是模型判断，不代表宿主或主人验收。新读入口不改任务状态、审批、计划、派工、提醒、附件或出站权限；不因“建议补做”自动执行。

## 首轮真实红与验收

新独占文件 `tests/test_delivery_change_review.py` 的
`test_changed_requirements_review_reaches_model_after_real_delivery_and_reopen` 两参数：
`goal-and-constraints` 与 `reordered-duplicates`。使用真实默认应用/Runtime、主任务工具、直接队友、`fs_write→file_publish`、实际附件读取、完成报告和 `task_delivery_record`。主会话通过真实Web输入明确修改要求，正常关闭并重开原Store，再经原工具读取准确旧单。两个参数分别改变目标/限制，以及保留目标但调整有重复文本的限制顺序。

期望首红准确到既有工具的accepted“不支持新section”结果；模型接收到真实工具结果后正常结束，由外层断言暴露缺少结构化复核。没有未来API导入，不伪造publication、报告、消息或验收单。功能实现后必须证明完整旧/新brief、准确版本/索引、变化标记、原宿主事实及历史模型判断实际进入下一模型请求，并由该请求产生区分事实与判断的回复；任务、队友、旧单、附件与零文件外发保持。测试只用临时文件/SQLite与本地ASGI，原16000上下文、6000输出、3pending槽及既有3/4秒等待不提高。固定策略只验宿主数据流与权限，不是自然模型的复核质量评估。

根首次实收两参数均失败：pytest8.74秒、守卫9.512秒、142.1MiB，停在真实交付登记前置的4秒等待，尚未到新section，不能计作功能目标红。现仅保留 `goal-and-constraints` 参数供根定位，另一参数保留方案与分支、待同一前置闭合后恢复，避免重复相同误位失败。测试复用既有交付旅程的异常有限诊断，仅在原Timeout处读取实际run状态、failure_diagnostic异常类型、轮数及最后accepted工具结果；序列化整包不超过1024字节。原真实发布/登记前置、生产、预算和等待期限均不改变，也不因等待失败编造新wake或领养假材料。诊断重验尚待根收取。

根随后单参数实收仍为前置失败：pytest4.58秒、守卫5.268秒、108.3MiB；setup3轮、delivery2轮、worker3轮，真实副本读取已accepted，主run为TypeError失败。源码确认复用的 `call(identifier,name,**arguments)` 在位置参数调用ID之外，又收到工具JSON的 `identifier=task_id`，Fake组装下一调用时产生Python重复参数冲突，尚未调用登记handler。现登记、修改要求和复核三处均复用既有正式旅程的显式 `ToolCall(...,json.dumps(arguments))`，不改共享helper或生产，不修改真实登记参数。原误位失败保留；修后单参数功能红仍待根实收。

根随后实收真实功能红：单参数 pytest0.66秒、守卫1.378秒、110.7MiB；真实发布、验收登记、修改要求、正常关闭重开均已完成，准确到accepted的 `commitment_inspect` 返回 `{"error":"工具执行失败：ValidationError"}`，缺少新section正文。该红与前两次夹具前置失败分开记载，不推断生产交付/唤醒故障。

授权后的候选仅改 `task_inspection.py` 与 `commitments.py`，复用既有验收单point-read、scope、认证入口、savepoint与分页。恢复两个端到端参数，并在同一独占测试文件补有限reader反例：仅进度revision变化时三个标记全false；当前任务或旧单正文变化使续页拒绝；超预算/缺规范字段/布尔版本/坏UTF-8不返回复核；来源已删除仍保留准确声明且不读其正文；未持久模式、坏seq/未知引用字段/附件携带消息位置不能绕过validator。纯reader夹具明确不冒充真实完成/来源证明；实际完成证据仍来自上面的真实端到端旅程。

根已串行实收新文件14项全部通过：pytest1.01秒、守卫1.752秒、147.9MiB；旧 `test_commitment_management.py`、`test_task_inspection.py`、`test_task_inspection_api.py`、`test_task_delivery_evidence.py`、`test_task_delivery_limits.py` 五文件53项通过：pytest3.40秒、守卫4.627秒、269.3MiB。合计67个不同节点，不把前置误位红计为功能退化，也不据固定模型策略宣称自然模型判断质量通过。

Ruff首轮仅新测试的I001导入顺序未通过；根只修该导入块并格式化三个Python文件。随后全仓Ruff通过：守卫94毫秒、22MiB；387文件格式检查通过：守卫76毫秒、21.6MiB。完整149源码strict尚未执行，非作者生产审查仍待完成，不能由以上测试/格式结果推为已通过。当前保持未bump候选，之后先由根保存本片检查点并整合2.49基线，再做版本与整合检查；此处不声称发行完成。

本人未自行执行测试、格式化、Git或真实模型/渠道。正文放不下必须沿原分页，不能截成看似完整的复核；坏材料明确拒绝，不忽略坏项补取。首片不需要新依赖、Store格式、native服务或真实模型/渠道账户。
