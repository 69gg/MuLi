# 资料收件目录

MuLi 可以在 Linux 上观察一个主人明确指定的现存目录，出现新候选资料时沿既有 Attention 与主动通知路由提醒。默认关闭；只观察直属普通文件，不递归、不扫描历史、不轮询目录、不自动读文件正文。文件名和内容始终是外部资料，不授予主人身份、配置、写文件、派遣或外发权限。

## 配置与使用

先通过现有主配置明确 `workspace_roots`，再通过自然语言 `file_inbox_configure` 或已认证的 `PUT /api/file-inbox/settings` 配置收件来源。例如：

```json
{"enabled":true,"path":"/已有工作区/资料收件"}
```

此入口完整替换来源策略，省略字段恢复默认；先用 `GET /api/file-inbox/settings` 或 `file_inbox_list` 读取现值再修改。策略是现有 SQLite 中的独立有版本对象，不属于 `/api/settings` 的主配置，不能交给 `self_configure`。保存不改变主配置文件或七领域格式合同，旧版本不识别此功能时不会因主配置多出字段而拒绝启动；旧版本也不提供文件收件观察。坏记录或未来策略版本明确拒绝，不静默重置。

路径不会扩大文件访问权限；目录及各级祖先必须能通过原生 NOFOLLOW 校验。目录不存在、符号链接、平台不支持或监视器不可用时，会暂停并报告固定状态。更换目录、调整该来源预算或改变访问根会推进持久授权 revision。仅修改其它设置、服务正常重启不会重新授权、扫描旧文件、刷新通知额度或清除覆盖缺口。来源配置保存后只重配本来源；主配置调整访问根时仍重新核验。

启用时保存目录 device/inode；重启发现目录身份不同会报 `identity_changed`，不会自动绑定替换目录。明确接受替换目录时，先保存 `enabled:false`，再保存 `enabled:true` 与完整目标策略，建立新授权。覆盖缺口同样需要这一明确重新启用流程；不自动补扫停机或暂停期间的文件。

通知只说明“新的候选资料”，提供记录 ID、观察时间、大小和长度受限的外部文件名，不表示下载完成。主人可随后说“查看并整理刚到的资料”：`file_inbox_list` 有界列出元数据，`file_inbox_get` 按 ID 定位路径；MuLi 可按现有文件权限使用 `fs_read` 等只读工具读取。正文不会因收件事件进入 SQLite 或默认提醒上下文。整理、移动、外发等操作继续由真实主人输入与既有 ask/auto/yolo 权限决定。

同源只读 API 复用现有认证与 Origin 边界：

- `GET /api/file-inbox?limit=20` 返回 `status` 和 `items`，limit 范围 1–100。
- `GET /api/file-inbox/{id}` 返回保留记录及候选路径，已撤销或不存在的记录返回 404。
- `GET/PUT /api/file-inbox/settings` 读取或完整替换来源策略；状态区分 `desired`、`effective` 与 `settings_error`，完整输入格式见现有 OpenAPI 文档。
- `GET /api/status` 包含 `file_inbox` 状态。

路径是观察时目录与文件名的定位信息，`current_file_verified:false` 明确表示未核验当前文件版本；文件可能已修改、删除或替换，后续 `fs_read` 读取当前文件并继续经过既有文件工具授权。API 不返回文件正文。

## 有界事件与持久状态

| 配置 | 默认 | 范围 |
| --- | ---: | ---: |
| `coalesce_seconds` | 0.1 秒 | 0–5 秒 |
| `candidate_timeout_seconds` | 30 秒 | 0.1–300 秒 |
| `max_candidates` | 64 | 1–256 |
| `max_records` | 128 | 1–1024 |
| `window_seconds` | 3600 秒 | 1–86400 秒 |
| `max_notices_per_window` | 8 | 1–32 |

CREATE 只建立本代候选；本代 CREATE 后 CLOSE_WRITE 或 MOVED_TO 才进入有限合并窗口。旧文件单独 CLOSE_WRITE 不构成新到达；删除/移出会淘汰候选。重复同版本元数据不会再次提交。无候选时没有目录扫描计时器或模型调用；宿主只有一个受限候选集合和一个驱动任务，不为每个文件创建任务。

候选到期、候选容量耗尽、内核/原生读取溢出、目录身份失败或每窗口提醒额度耗尽时，关闭监视器、保存一次覆盖缺口并暂停。每窗口最多 `max_notices_per_window` 个资料事实，同一持久授权至多另有一个缺口事实。因此默认单窗口至多八个资料事实加一个缺口事实；它们可能合并提醒，也可能分别导致模型调用，不能只用 Attention 单批大小证明总量有界。计数、窗口起点和缺口 ID 跨重启保留；触达限额后即使窗口时间过去，也不会自动恢复。

原生未交付批次累计读取量达到 16 KiB，或原生事件达到 256 个时，保守记录 `coverage_incomplete` 并停止本代，该批资料不交付。预算包括沿祖先链收到、最终被名称过滤的兄弟 entry 事件，因此祖先目录高度活跃时也可能触发暂停；这不是文件条数或模型调用次数的额度。收件元数据保留数受 `max_records` 限制；列表和保留裁剪统一按 canonical event ID 从新到旧排序，观察时间只用于展示，系统时钟并列或回拨不会让旧候选挤掉新候选或覆盖缺口。淘汰记录时按记录保存的 event ID 精确移除其 canonical 事件，不扫描全部历史事件。已经排队的提醒有独立小型接纳标记，元数据淘汰不会使配置撤销漏掉待处理提醒。正文与文件索引不入库。

原生适配使用一个 inotify 实例，逐段 NOFOLLOW 打开并持有目录链，通过 `/proc/self/fd/<fd>` 将 watch 绑定到已验证目录。目录 FD 和 watch 随路径深度增加；祖先自身失效与各父目录的下一段 entry 变化用于停止本代，不递归收集祖先目录中的其他资料。同步身份复核也检查已排队的失效事件，因此祖先移走再移回不能仅凭最终 inode 相同继续接纳。

canonical 收件事件、记录、首次冻结的独立通知路由及持久额度在同一 SQLite 事务提交，之后才唤醒 EventBus。Attention 同时检查 canonical event ID、持久来源身份、策略 revision、当前授权和保留记录，并在配置锁内最终复核。伪造同名事件或复制资料到另一个 event ID 不会获得来源证明。运行时 watcher generation 仅拒绝旧回调；正常重启不会据此删除已提交且尚未提醒的事实。

只取消此来源尚未 claimed 的提醒，不取消真实主人输入或其它来源批次。已开始的模型回合继续遵守既有只读 Attention 权限；冻结目的地的撤销与未知发送结果沿原有出站账本处理。

## 边界

只支持 Linux，本片没有 macOS/Windows 文件事件后端，非 Linux 返回 `platform_unsupported`。Linux 仍需可用的 `/proc/self/fd`、目录链访问权限及 inotify 系统资源，初始化不可用返回 `watch_unavailable`。运行中目录链失效为 `identity_changed`，内核溢出、读取问题或预算耗尽为 `coverage_incomplete`，消费回调异常为 `callback_failed`；保留首次原因并暂停。

inotify 不保证远端网络文件系统变更、挂载覆盖或停机事件的完整性；路径复核不是并发 namespace 切换的原子锁，也不会恢复丢失资料。保存 device/inode 不能被当作跨系统永久身份。真实桌面通知、外部渠道发送、网络文件系统和挂载变化须按各自集成另行验收。
