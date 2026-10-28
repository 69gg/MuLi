# 明确收藏来源的原文比较核心

仅比较两份当前仍保存的 `attachment:` 来源。独立附件代表独立版本；来源目录不构成全历史版本库，旧摘录不能推定完整旧稿。本增量不自动关联摘录、不找回原文件、不做语义差异或后台轮询。

`source_comparison.compare_sources()` 接收双方完整来源 ID、各自记忆来源 revision、可选游标、冻结 `ComparisonPolicy` 与调用者 `check`。入口可共用该只读服务，不要求记忆管线启动或 `memory.enabled=true`。输入 SQL 先限制正文/元数据，再验证 `source_digest` 与 `restore_attachment_source`。原附件摘要、文档阅读 revision 与记忆来源 revision 分别返回，不能互换。

有效策略复用 `memory.max_source_chars`、`min(limits.max_file_bytes, document.max_extracted_bytes)`、`limits.max_search_entries`（最大行数）、`limits.max_output_chars` 与 `document.timeout_seconds`。根集成的 `check` 须在开始与 worker 返回后核对原请求、维护/关闭状态和这些当前有效策略；服务随后重新核对双方正文/来源，拒绝遗忘或变动后的旧结果。

行由 LF 分隔，保留 CR、大小写、空白、UTF-8 字节位置及末行是否换行。固定的 stdlib-only worker 通过当前解释器的隔离模式运行，由 `run_process` 负责 stdin JSON、输出额度、期限、取消及进程回收。行数先受限，再用 `SequenceMatcher(..., autojunk=False)` 获取有界操作表；只逐块构造当前输出页，不构造全部 diff 正文。单个块放不下时明确失败，不截断或跳过变化。

结果为 `equal/changed`、左右完整身份/提取范围、`items`、`has_more` 和 `next_cursor`；每块含操作类型、左右原文、字节区间及 `memory_source` 参数。空侧只有零长度锚点，核上下文时省略 `length_bytes`。游标以每个 Store 的内存密钥认证，绑定资料库、双来源/版本、方向、算法与策略，防止伪造终点。正常重开或进程重启后游标失效，从首页重新比较；不存正文缓存、不修改持久格式。

核心回归覆盖中英文/emoji 字节复读、增删改、相同正文、重开、文档三类版本、逐页完整性、伪造/跨库/换方向/换策略游标、SQL 输入门、来源损坏和 await 后变化，以及真实子进程超时、取消、输出超额后的回收。工具/API/前端授权和用户流程由根集成验证；本文件不代表检查已经通过。

前端每块左右入口复用现有来源原文 reader，采用已验证的来源 ID、revision 与 UTF-8 字节锚点，不按字符索引或减字节寻找前页，也不以整个差异长度扩大单页。空侧保留位置说明，EOF/空全文不伪造读取回执；可从头重读。返回仅保存双方身份、当前比较游标和焦点标识，重新请求后聚焦对应入口，不缓存比较正文或 DOM；关闭返回最初目录 trigger。原文视图同时跟踪双方来源，任一侧变化即取消读取并清空返回上下文，旧入口和迟到结果不能恢复正文。

可选 `view=overview` 复用完整、有界 opcode，返回无正文坐标目录；默认 `changes` 的字段和游标 binding 保持原合同。概览独立绑定视图及合同版本，仅首页附全局 `totals`：三类变化组数、总组数、左侧移除和右侧加入的整行数及字节数。替换为一组，双方各计完整跨度；不是字符级最小改动或语义重要性。每次仍执行完整匹配，不承诺减少 CPU。

概览侧包含零基、左闭右开 `start_line` / `end_line`、原字节坐标及 reader 参数，不 join 正文。服务用原文字节的 LF 边界验证行端点映射、非重叠顺序、操作类型和精确 reader 参数；全局计数校验整数、分项和、来源上界及首页当前页下界，单页时还须等于全部条目之和，不在主进程重新匹配。身份、首页计数、条目与预留续查游标共同受原输出额度限制；概览也不得跳过放不下的条目。

两种视图共用主助手、同根队友和 API 的 admission、策略与来源重核、关闭回收。目录读完只表示坐标已列完，需按选中组的结束字节限制原文续读，不能拿全文 reader 的 `has_more` 判断组完成。空输入的统计由 worker 定义，不扩大现有收藏入口对非空有效正文的要求。
