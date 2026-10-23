# 指定队友的接办证据（开发中）

版本号暂拟 2.43；实现、定向回归和首轮静态后审已完成，整合、构件及远端发行仍待完成，尚未发布。2.42 候选的独立 Linux 回退期限失败正在原分支修复；不能将本片定向绿色替代该发行门。

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
