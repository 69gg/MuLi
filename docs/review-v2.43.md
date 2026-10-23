# 指定队友的接办证据（开发中）

2.43候选已实现并完成多轮独立后审、损坏数据红绿修复、完整后端整合及离线构件验证；精确远端候选与发行待完成，尚未发布。2.42已修复独立Linux回退重复核验并正式发行，见[2.42闭合记录](review-v2.42.md#发版闭合)。

## 范围与实现

在既有 `commitment_inspect` 增加 `handoff` 段及 `agent_id`，认证管理 API 同步透传。一次只核查一个指定队友，返回当前任务控制版本、说明/当前报告/原消息的读取位置和完整可用附件引用，不默认读取正文、复制报告或检索记忆，不新增数据库、权限、自动接班或副作用重试。

目标和调用者父链都走有限解析，完整点查对象先经 SQL 字节门。消息先沿原始 `message_order_session` 索引取有限窗口，坏映射也占额度，不向更早有效消息持续补齐。revision 绑定任务、队友、父链、当前执行/报告、窗口内宿主消息版本及记忆；报告定位另携完整记录摘要，取包后改正文时拒绝旧定位。

队友 queued 时可能保留上一轮 run ID，明确标为历史，不借上轮成功报告证明本轮完成。所有副作用状态继续 unknown。新段仅为 `recent_window_only/live` 观察，旧 section 的 live 语义不变；完整说明、计划确认、派工和原路汇报均复用原入口。

## 独立审查

实施前发现旧 `_read` 完整任务加载和调用者 scope 解析会先于新段执行，均改为在旧读取前分支；调用者使用独立有限 callback。随后补齐 queued 旧 run 与真实 run/report 作用域验证，以及报告读取位置的完整版本。根复核原始有限映射、只读 savepoint、无 await 间隙、分页/版本和既有权限接线，尚未发现剩余 P1/P2；最终整合后仍须复审。

曾怀疑共享 `is_main` 调用 `main_session` 会提交外层事务；旧 2.42 的真实 Runtime 临时探针为 before=true、after=true、rollback 后无残留，守卫421毫秒、57.3 MiB、swap0，未复现。已有格式 admission 使该路径只读，因此不将其记作已复现 bug。共享身份谓词改为读取既有 marker 与 SQL kind，作为同义简化和预算收敛；不据此声称修复过真实提交缺陷。

新处理器与专用 scope callback 有界，不代表通用 Runtime 的 `execution_snapshot` / `guard_execution` 已具同等成本边界；其旧完整任务读取和父链解析另行审计。SQLite 仍需处理所选 JSON，不声称恒定 I/O；窗口外旧消息和绕过宿主 revision 的数据库修改不在本片版本保证内。

## 已完成验证

所有本机检查经主仓绝对路径 `check_local.py` 的30秒守卫串行运行，临时文件/数据库及明确 FakeClient/HTTP Mock，无真实账号、桌面或服务效果。

- 新单元及既有任务核查/控制/LocalServices，加完整 Runtime 旅程共144 passed，pytest1.27秒、峰124.8 MiB、swap0，实际exit0；此前139项基础集合与其重叠，不相加。四生产源限定strict mypy、Ruff和格式通过，最高267.7 MiB、swap0。
- 报告定位版本补充后，handoff+既有task_inspection 60 passed，pytest0.48秒，限定源码类型/格式检查通过，峰136.5 MiB、swap0；不把重叠集合计成新增总数。
- 首个 Runtime 旅程1 passed，pytest0.54秒，守卫1.219秒、90.8 MiB、swap0。先前 helper 参数名冲突和Web响应键错误仅修正测试。实际链为 Web附件派工、真实渠道工具的HTTP超时入账unknown、旧队友失败、主人更正记忆/重规划、新Web显式接办、新队友沿原seq读取附件、Web草稿报告，未知外发只尝试一次。来源及替身边界见[旅程](task-handoff-journey.md)。
- 根补管理API认证、跨任务拒绝、完整元数据、读取无副作用和消息改版409回归。首轮18 passed/1 failed，原因是新增夹具缺少任务说明的必填constraints，未到产品逻辑；并补齐旧简化bind夹具的session_id。修正后定向1 passed/17 deselected，守卫1.187秒、101.9 MiB、swap0，实际exit0。API与Runtime整合重验另行记录。

压缩/重开目前仍有先前附件与历史专项证据；本片新的完整组合尚未逐项重验，不将旧专项结果写成新的完整接办组合通过。自然模型决策质量、真实外部送达和副作用恢复未由这些离线结果证明。

根随后对冻结实现、管理API与Runtime旅程共六文件整合，**163 passed**，pytest2.21秒、guard3.089秒、206.5MiB、swap0、实际exit0；包含修正夹具后的完整API集合。全仓 Ruff、370文件格式与146生产源 strict mypy 均通过，最高341.2MiB、swap0。完整后端、对齐2.42修复后的版本及构件检查尚待完成，不以该定向绿色替代发行门。

最终独立后审发现原始 `message_order` 的非STRICT文本列可保存BLOB或非法UTF-8；旧投影把BLOB带入摘要时抛TypeError，坏TEXT在SQLite取行阶段抛OperationalError，空TEXT误标missing。四个真实raw SQL回归在旧实现全部失败（pytest0.28秒、guard778毫秒、70.4MiB、swap0、实际exit1），不是凭静态推断记作复现。修正仅在新窗口方法中先筛非空/有界TEXT、投影BLOB再严格UTF-8解码；坏项归一为unavailable并仍占原seq，不回补旧有效消息，也不收紧合法opaque引用。

修正后handoff+既有HistoryEvidence共98 passed，pytest0.55秒、guard1.085秒、93.4MiB；两文件Ruff/格式及store限定strict mypy通过，最高250.6MiB、swap0、实际exit0。另一Sol只读复审无P1/P2。不同坏ID归一为同一个不可用值，直接SQL绕过宿主revision的变更仍不在版本保证内；不为此无界读取或扫描。随后已签名保留功能checkpoint，并合入2.42同次核验修复，准备2.43元数据和完整整合；尚未推送或发行。

另一轮只读审查发现完整对象点查也存在驱动先于JSON解析解码TEXT的同类缺口。根最初用裸0x80注入，其中四种对象先被既有JSON表达式索引拒绝，该四项不算产品复现；将坏字节放入保留合法JSON形状的额外字符串后，任务、队友、目标/主会话、run和报告六例均在旧点查fetchone处实际失败（pytest0.38秒、75.5MiB、swap0、exit1）。SQL改为有界BLOB投影，再在固定错误处理内严格UTF-8解码。

独审继续指出 `json.loads(bytes)` 会自动识别UTF-16/32或接受BOM，可能扩大原JSON TEXT合同；三个编码反例在仅BLOB投影版本实际全部失败（pytest0.22秒、69.5MiB、swap0、exit1）。增加显式UTF-8字符串解码后，handoff、完整Runtime旅程、管理API、任务控制与HistoryEvidence五文件**207 passed**，pytest2.28秒、guard3.206秒、210.7MiB、swap0、exit0。固定错误与预算保留，不把字节投影当作自动修复其它编码；完整整合继续在最终生产冻结后执行。

2.42精确签名 `ed2b267` 的候选/main各十项及发行十三项全部成功，签名标签及PyPI/GitHub两构件核对一致，已正式发布。本分支保留其修复，不以接办离线绿色代替2.42发行结果。下一片记忆质量仅为[冲突共同呈现候选](memory-quality-next-proposal.md)，未实现或作为本版功能发布。

最终静态检查指出新增严格解码的可空SQL投影仍直接传入JSON解析器，运行时虽已有固定错误处理，类型合同不成立；改为显式拒绝None再解码。随后增量mypy出现未改Skills三处unused-ignore，未改源码或规则，完整`--no-incremental`检查146源通过，guard8.061秒、414MiB、swap0；Ruff/格式通过，相关五文件207项再验通过（pytest2.24秒、guard3.238秒、210.8MiB、swap0、exit0）。完整回归前两组已完成，再以该定向集合补验最后类型门；后两组按最终生产代码继续，后续总数不与重叠定向相加。

## 完整整合与构件

212文件分四守卫串行，每守卫内部仍至多32文件一批，共**5,191 passed / 11 skipped**；所有守卫实际exit0、swap0，没有提高资源/期限或测试并发。前两组与末次类型门的补验顺序如上，不把定向结果重复累计。

| 文件范围 | pytest通过 | 守卫时间 | 峰值内存 |
| --- | --- | --- | --- |
| 1–64 | 850 + 783，1 skipped | 83.394秒 | 650.4MiB |
| 65–128 | 981 + 717 | 127.181秒 | 605.2MiB |
| 129–192 | 688 + 732 | 36.277秒 | 761.8MiB |
| 193–212 | 440，10 skipped | 6.673秒 | 326.2MiB |

只保留既有畸形MCP policy夹具的Pydantic序列化warning；平台跳过不算原生实测。全仓Ruff、370文件格式、146生产源完整非增量strict mypy、离线83包锁检查通过。前端未修改，沿用上一相同代码的281项结果；远端quality仍会执行完整前端检查。

最终生产离线构建wheel/sdist成功（guard1.279秒、96.8MiB），只在临时目录offline/no-deps安装本地wheel，源码外资源、API/提示词、默认关闭的桌面及任务控制/lifespan冒烟通过（1.201秒、99.1MiB）。版本/声明/changelog/两包一致校验通过（169毫秒、19.6MiB）；均30秒守卫、实际exit0、swap0，没有下载新依赖。暂不等同远端发行或自然模型接办质量已验收。
