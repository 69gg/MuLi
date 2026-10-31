# 完整 ASR 转写工件

后续扩展：2.115 增加主人显式[收藏已有完整转写](attachment-memory.md#收藏已有完整转写)的独立写入口，使用 `memory=2` 来源合同。以下保留最初工件保存/阅读片的实施和验证记录，不把当时检查扩大为新收藏路径已验证。

本片让现有 `audio_transcribe` 在一次 ASR 调用后留存完整结果，继续使用 `read` 权限；不增加另一个保存工具、任意路径写能力或配置字段。网页录音草稿、后台唤醒、TTS 与渠道发送保持各自现有入口。

## 接口与复用

- `audio_transcribe(id, language?)` 的 `id` 仍是原音频 ID，短结果的 `text` 仍为全文。新增 `asr_completed`、`transcript_id`、`revision`、下载 `url`、UTF-8 字节游标和 `has_more`。长结果的 `text` 是预览，不把预览当全文。
- `audio_transcript_read(id, revision?, offset_bytes=0)` 的 `id` 是转写工件 ID。非零偏移必须携带上一页 `revision`；续读使用 `next_offset_bytes`，返回 `content`。偏移落在 UTF-8 字符中间、版本变化或文件变化均拒绝。
- 复用 `Attachments.save` 的私有目录、随机 ID、临时文件、文件同步、原子替换、附件元数据、并发与配额。转写文件固定命名为 `转写-原音频ID.txt`、来源为 `asr`，通过既有已认证附件下载读取；不接收保存路径、不添加第二账本。
- 复用 `content_page` 按最终 JSON 信封预算分页，两工具标记 `preserve_output=True`，使紧邻的模型请求保留完整引用。后续上下文仍可压缩，完整正文继续留在附件中。

## 边界与失败

全文 UTF-8 字节上限取当前 `media.asr.max_response_bytes` 与 `uploads.max_file_bytes` 的最小值，另受 `uploads.max_total_bytes` 与现有并发限制；预览/每页服从当前 `limits.max_output_chars`，不新增硬编码模型或服务参数。每页先以有限内存流式核对整个文件的大小与 SHA-256，再读取有限字节窗口，并复核文件及元数据变化。响应不包含本地绝对路径、凭据或配置。

ASR 完成后的保存或预览失败返回 `asr_completed=true` 与明确错误，不内部再次调用 ASR。保存成功后再次复核当前字节上限；等待保存期间限额缩小时，保留已存引用但明确报错，不把当前不可续读的工件说成成功预览。已保存但响应丢失时，用 `attachment_search(source="asr", name_query="原音频ID")` 沿 `next_cursor` 查找工件；空页的 `has_more=true` 仍须继续，不能只看最近一批的 `attachment_list`。目录中的 `id`、`sha256` 分别用作 `audio_transcript_read` 的 `id`、`revision`，无需再次付费转写。详见[附件检索与分页](attachment-search.md)。保存取消只向实际保存任务发送一次取消，重复取消先等待文件线程及清理结束再传播；不宣称可撤回已完成的上游调用或已提交的工件。

这里的 `read` 仍不允许任意业务文件写入。工具结果本来就由 Runtime 落库，本片仅将同一派生结果保存在受控私有附件库；转写内容继续标为资料，不能成为新的授权或系统指令。附件属于敏感本地数据，备份需包含附件目录与数据库；NDJSON 消息导出不包含文件正文。

## 验证与依据

临时 Store/附件和 HTTP MockTransport 的 24 项新增回归通过，以小文本及最小输出预算覆盖长转义、多字节分页、重开续读、版本/文件漂移、保存期间及读取前限额缩小、保存失败单次 ASR、已保存响应丢失、重复取消收尾和真实 Runtime 工具结果落库。测试硬拒实际 HTTP 传输与异步子进程启动。既有 `test_local_services.py`、`test_media.py`、`test_web_voice.py`、`test_auth_uploads.py` 合计 109 项通过；两处源码 strict mypy、三处 Python Ruff/format 通过。

全部检查在目标 worktree 使用 `python /data0/MuLi/scripts/check_local.py --seconds 30 run -- /data0/MuLi/.venv/bin/python ...` 串行执行，峰值 193.1 MiB、swap 0，未提高既有限制。首轮重开夹具漏传 `Store.open_runtime` 的必选 `default_mode`，补齐后该单例及全部新增组通过。没有真实 ASR、实际设备、下载或大音频；本机 Linux/fake 通过不代表 macOS/Windows 或真实服务验收。

依赖 API 已通过 Context7 核对 Python 官方 [二进制文件定位](https://docs.python.org/3.12/tutorial/inputoutput.html)、[hashlib](https://docs.python.org/3.12/library/hashlib.html)、[os 文件 API](https://docs.python.org/3.12/library/os.html) 与 [asyncio shield/to_thread](https://docs.python.org/3.12/library/asyncio-task.html)。不依赖仅 Unix 可用的 `pread`；防跟随/非阻塞标志只在平台提供时使用，并继续核对实际打开的普通文件及摘要。
