# 附件检索与分页

`attachment_search` 只检索已保存的附件目录元数据，允许找回最近 200 条之外的转写工件。它沿用 `read` 权限，不读取正文、调用 ASR 或确认文件存在；文件名与来源都是资料，不构成指令。旧 `attachment_list` 的数组返回格式、最近更新时间排序、默认 50 条与最多 200 条均保持不变。

## 使用方式

`attachment_search(source?, name_query="", cursor?, limit=20)` 返回 `items`、`next_cursor`、`has_more`、`scanned`。每页最多返回 100 项，每次处理最多 200 个相邻目录记录，额外读一条作为后续范围判断。

- `source` 区分大小写精确匹配；省略表示全部来源，空字符串只匹配空来源。支持渠道来源，不将来源限制为固定枚举。
- `name_query` 区分大小写、按原样字面包含匹配，不裁空格，`%`、`_` 没有通配含义。来源与文件名查询各限 120 个 Unicode 字符，拒绝无效 Unicode。
- `scanned` 是本次实际处理的记录数，包含不匹配项，不包含额外的后续判断记录。
- `has_more` 仅表示还有未扫描的附件范围，**不保证还有匹配项**。即使 `items=[]`，只要 `has_more=true` 就须回传 `next_cursor` 继续；游标始终前进。

ASR 已保存但回执丢失时，按 `source="asr", name_query="原音频ID"` 查询。工件名为 `转写-原音频ID.txt`。找到后，将目录项的 `id`、`sha256` 传给 `audio_transcript_read(id=..., revision=...)`；后续按其字节游标续读，不重新调用 ASR。

目录项仅返回 `id`、`name`、`source`、`mime_type`、`size`、`sha256`、`created_at` 和既有认证下载 `url`，不返回未知元数据字段或本地路径。目录记录可能对应已丢失或改变的文件；这里不做文件检查，正文读取入口仍须独立验证文件与版本。

v2.56候选增加显式视觉解释工件：`image_describe(..., archive=true)` 成功后保存为
`source="vision"`、名称含原图ID的私有JSON。回执丢失时只用
`attachment_search(source="vision", name_query=原图ID)` 找回，不自动重复看图。
将目录的工件ID及SHA传给 `attachment_read(id=..., revision=..., offset_bytes=0)`，
再沿返回的 `next_offset_bytes` 和同一revision读取完整提问、模型解释及原图版本。
只有视觉工件支持此入口的分页参数；普通附件和ASR传入这些参数会拒绝，ASR仍用原
`audio_transcript_read`。每页复核完整工件摘要、文件身份、元数据和当前大小上限；
原图已缺失时仍可回查历史解释，不证明当前原图健康，见[存档合同](vision-archive-proposal.md)。

## 游标和有界行为

使用既有 `(kind,id)` 主键按附件 ID 降序翻页，不依赖更新时间、OFFSET 或裸 rowid，不新增索引、数据库迁移或 Settings。ID 顺序不代表音频或工件的创建时间。SQLite 的[键值续页](https://www.sqlite.org/rowvalue.html)可直接定位后续范围；裸 [rowid](https://www.sqlite.org/rowidtable.html) 则可能在 VACUUM 后变化。

不透明游标绑定持久的资料库实例 ID、原始来源/文件名筛选摘要与最后消费的附件 ID。先将游标长度限制在 512 个字符内，再解码并严格校验。资料库重开、删除锚点、修改 `updated` 或调整页数/输出预算不会使已通过的 ID 再次出现。改变筛选或换资料库须从首页开始。

这是实时目录，不保持跨请求快照。新增 ID 落在已走过的范围时，要刷新首页才能看到；落在尚未扫描范围时，可在后续页出现。删除与新增不会把游标倒退或重新定位到锚点；持续新增情况下不承诺固定页数结束。

先用主键限定最多 201 行窗口，再在 Python 内筛选。SQL 每行只返回有界元数据 BLOB 前缀及原长度，避免为稀有匹配全库扫描或在 SQLite 内解析超大的整段 JSON。完整元数据在每行最多 `4 * limits.max_output_chars` 字节内才可解析；超预算或损坏记录不会被静默跳过，即使它可能不匹配筛选。已有可返回前缀时，超预算行留在下一页；单项仍不能容纳时明确 `output_budget`，不保证跨过坏记录找到后面的结果。

`items`、真实游标、`has_more` 与 `scanned` 按最终 JSON（含转义）共同装入当前 `limits.max_output_chars`。页内不截断 ID、名称或 SHA-256；第一条匹配项连同信封放不下时明确失败。工具标记 `preserve_output=True`，紧邻的模型请求保留完整 JSON 和续页引用；之后的上下文仍可能压缩，可重新检索目录找回。

## 验证边界

新增回归使用临时 Store、固定附件 ID、小文本与 HTTP MockTransport，覆盖超过 200 条后的旧 ASR 找回、空匹配页推进、删除锚点、两侧插入、相同 `updated`、重开/预算热缩、转义预算、坏游标、损坏元数据，以及真实 Runtime 对 JSON/游标的保留。端到端取消发生在 ASR 工件已提交后；之后只检索元数据并重开续读，ASR 总调用数为一次。

新增 37 项通过；既有 `test_audio_transcripts.py`、`test_session_records.py`、`test_local_services.py`、`test_auth_uploads.py` 四文件合计 98 项回归通过。三处源码 strict mypy、四处 Python Ruff/format 通过。首轮仅修正新测试的导入排序，产品回归无失败。

全部检查在目标 worktree 使用 `python /data0/MuLi/scripts/check_local.py --seconds 30 run -- /data0/MuLi/.venv/bin/python ...` 串行执行，峰值 159.8 MiB、swap 0，未提高限制。未运行真实 ASR、外部 HTTP、设备或安装过程；本机 Linux 回归不代表其它平台或真实服务验收。
