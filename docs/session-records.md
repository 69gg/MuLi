# 完整运行记录标题目录

`SessionRecords` 为所有持久会话提供独立只读目录。查询在分页前匹配标题，可找到旧 `/api/sessions` 最近 200 条之外的记录；旧列表的用途、顺序和实时聊天恢复流程保持原样。

认证后的 `GET /api/session-records` 接受 `query`、`cursor`、`limit`，直接返回下述 JSON。HTTP 页预算取已配置输出上限与 32000 字符的较小值；不携带 cursor 表示从当前首页开始。它不修改旧 `/api/sessions` 的调用合同。

```python
from muli.session_records import SessionRecords

page = SessionRecords(store).read(
    query="原样标题片段",
    cursor=None,
    limit=50,
    max_output_chars=32000,
)
```

返回值直接作为完整 JSON 页使用，不再套外层对象：

```json
{
  "items": [
    {
      "id": "完整会话 ID",
      "title": "持久标题",
      "status": "idle",
      "created_at": 1790000000,
      "updated_at": 1790000300
    }
  ],
  "next_cursor": null,
  "has_more": false
}
```

`status` 只是已登记的会话状态，不表示任务成功；主任务结束、失败或停止后均可能回到 `idle`。目录不读取消息正文、附件、模型原生回放或事件，不调用模型，也不创建、唤醒或修改会话。打开某条记录后的旧消息及正文续读使用现有只读归档接口。

## 查询与稳定续页

`query` 默认空字符串，最多 120 个 Unicode 字符；保留前后空白，`%`、`_` 和 NUL 都是字面内容。SQLite 的 `lower` 仅提供 ASCII 大小写兼容，不承诺对其他文字执行 Unicode 大小写折叠。空查询包含缺少标题的旧记录；非空查询只匹配文本标题。

每页默认 50 条，允许 1–100 条。排序按创建时间降序，再按完整 ID 降序；更新标题、状态或 `updated_at` 不改变续页边界。创建时间仅接受有限、正数的 JSON integer/real。这是 MuLi 现代运行记录的时间语义：缺失、null、非正数、字符串、布尔值及无限数对外为 null，排序值为 0。`updated_at` 使用相同的有效性规则。原始数据不会被重写；标题与状态的空字符串原样返回，缺失或 null 返回 null，不把两者混为一种值。

将上一页 `next_cursor` 原样带回，同时保留原始查询；`has_more=false` 时游标为 null。游标绑定版本、资料库实例、原样查询以及实际返回末项的创建排序键和 ID；同一资料库重开后仍可续读，删除末项也不会使边界失效。SQLite 整数排序键保持整数精度，包括超过 JavaScript 安全整数范围的相邻 int64 值；调用方应把游标视为不透明字符串，不自行解析时间重建它。

核心入口在 Base64 解码、JSON 解析和目录查询前，用本次 `max_output_chars`
限制游标字符数；超限返回 `output_budget`，不创建解码副本或查询记录。
成功发出的游标已经包含在完整页预算中，因此同预算续读保持兼容。若中途
缩小预算使旧游标超限，可恢复原预算或从首页重读，不修改游标或跳过记录。

这是按当前事实续页的目录，不是跨请求快照。首次读取后新增的较新记录不会混入后续旧向页面，需要回首页查看；删除的记录不再返回。宿主通常不修改已创建记录的 `created_at`，本接口不保证插件任意修改创建键后的快照完整性。游标不是授权凭据，HTTP 入口仍须使用工作台现有认证。

## 完整预算与错误

预算按 `len(json.dumps(page, ensure_ascii=False))` 计算，包含列表、完整标题与 ID、游标、布尔值及全部外层字段。目录只返回连续的完整条目，不截断标题、状态或定位 ID，也不跳过超大条目去假装读取完成。页预算不足以加入下一条时，保留已返回的连续前缀及其真实游标；第一条仍放不下则明确失败。调用方可以提高预算或缩小标题查询，不能把错误当成空目录。HTTP 层可以设置比核心更小的预算及游标输入上限。

异常复用 `muli.evidence.EvidenceError(code, message)`：

| code | 含义 |
| --- | --- |
| `record_query_invalid` | 查询类型、Unicode 或长度无效 |
| `record_cursor_invalid` | 游标损坏、版本/资料库/原样查询不匹配，或排序键无效 |
| `record_limit_invalid` | 每页数量不是 1–100 的整数，布尔值也不接受 |
| `record_metadata_invalid` | 持久 ID 无效，或已登记标题、状态不是文本/null；不伪造替代值 |
| `output_budget` | 输入游标、第一条完整摘要及续页信息，或空页元数据不能装入当前预算 |
| `invalid_arguments` | 输出预算不是有效的正整数，或超出受支持的整数范围 |

## 资源边界

Store 通过 SQL 投影读取 ID、标题、状态的有界 BLOB 前缀和真实字节长度。只有确认完整后才严格解码 UTF-8；中文、emoji 和 NUL 不改变标题内容。未选中的 JSON 大字段不会进入 Python；SQLite 仍需要解析被访问的 JSON 行，标题包含查询也可能扫描大量候选，并不保证任意查询都只访问一页行数。

新增的单个部分表达式索引 `sessions_created_directory` 仅覆盖 session 的创建排序键与 ID；查询显式按原始 ID 排序，续页同时使用创建键上界和严格的二元边界。首次打开旧资料库时需要扫描现有对象并建立索引，其成本随资料库规模增加，之后复用索引，不删除历史。大量相同创建键（包括未知时间 0）仍可能增加候选检查，不宣称每页固定工作量。

本片只解决完整标题目录的可达性，不改变 `hydrateSession`、主对话首屏或全局 SSE 的历史回放，也不声称整个启动流程已有统一资源上限。
