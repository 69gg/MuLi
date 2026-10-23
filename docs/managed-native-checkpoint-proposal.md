# readiness 最后完整轮的小型失败回执（本地验证完成）

2026-10-24。根已独审方案和原提交/保存顺序，并通过唯一守卫收齐旧实现三项预期red：pytest 3 failed/0.12秒，守卫597毫秒/66.4 MiB/swap0/exit1；两项缺字段、一项严格五键拒绝，与预期一致。随后实现worker、协调器及新回归，已完成根整合与另一Sol独立后审，尚未新的远端原生验收。此前本地文档HEAD为`1304e1b`，真实第十八轮精确候选为`c0394b0`；不把本地实现或纯回归当成远端受测实现。

## 新事实与问题边界

根已核对 [CI 40022475250](https://github.com/69gg/MuLi/actions/runs/40022475250) 全终态为10成功/1 native失败，native job为120650223635。897字节 artifact12531416330 的SHA256为 `f80270b66ede2164eeda9b9d4e3af161414b113e8bef52e61c8ae205ff412a2f`，仅receipt410/result1103字节，通过完整schema；根取证守卫1.854秒/45.7 MiB/swap0。后取2418字节有限日志确认20:53:26Z deadline、20:53:29Z `native_service_cleanup_failed`，不能沿用第十七轮command_timeout。

result仍为manager_setup/setup_failed、0事务；manager_failure为deadline/manager_validation/query0/ok/used17512。后备status0/output_budget及其他not_started是外层整包拒绝默认，不能当作worker未执行或继承第十六轮状态。清理后receipt账户和组removed、mount0、environment/bootstrap已清空而manager_program仍在；它不证明prepare未创建，也不把账本清空替代正常cleanup验收。本子agent没有重复下载、解析ZIP或读取原始敏感正文。

`_SetupIO`计原始双pipe和文件字节；未返回计数时保守占满授额。`observe_setup`还计真实JSON，完整包可超过1024；第十八轮再次否证“去重后一定容得下诊断”，但没有被拒原包和分阶段计数，不能认定内部实际耗满哪一步。下一片先保留已付读取成本的事实，不增加启动变量或再作盲CI。

## 唯一授权改动

在`manager_state(manager-ready)`原轮询中，完成原两unit、loaded、invocation连续性、PID/stat startticks/cgroup连续性和允许状态门后，仅在尚未ready、准备进入原sleep的pending分支复制一份`last_completed_round`。提交点放在原有`remaining(deadline)`成功之后、原sleep之前，复用该次剩余时间计算，不添加新时钟读取或期限。ready分支的完整process/kernel/尾部复验未完成，不能提前作为新完整轮。只在已取得非零本轮invocation和PID/startticks绑定时发布observed；尚无满足条件的历史轮时为not_observed，后来的不完整/超范围轮不覆盖已保存轮。该轮的pending路径原本就是一次List和两次属性读取，再读一次stat；不再读proc、bus或账户，不加轮询、sleep或恢复查询。

失败时将该字段附在原五字段失败回执中；idle/ready成功格式不变。外层只扩展`manager_failure_facts`的严格嵌套白名单，复用`observe_manager`现有整个回包≤1024、used≤本次余额−1024和原期限门，以及先保存结果、后可选诊断的顺序。既有五字段历史失败回执继续可读；新worker失败回复必须显式携带observed或not_observed。后备失败保存仍携带同一manager_failure，不能覆盖原首因或已完整接受的轮事实。

## 精确 schema 与字节责任

`last_completed_round`仅有以下两种形状，不接受额外键：

```text
{"version":1,"code":"not_observed"}

{"version":1,"code":"observed","elapsed_ms":E,
 "runtime":{"active_state":A,"sub_state":S,"job":J},
 "manager":{"active_state":A,"sub_state":S,"job":J,
            "pid":P,"start_ticks":T,"invocation":I}}
```

- A仅为原门允许的`active`、`activating`、`inactive`；inactive要求同项J>0。S沿原`[a-z][a-z0-9-]{0,63}`，最多64个ASCII字符；不发Description、路径、原cgroup或任意状态正文。
- J是严格int，0≤J<2³¹；P是严格int，0<P<2³¹；I为非全零32位小写十六进制。均来自原本完成的`_manager_units`与连续性门，不以序号或输入选择其它unit。
- T来自本轮原有`_start_ticks(stat, pid)`。**现函数只保证非负，没有uint64上限**；投影另限严格int且0≤T<2⁶⁴，超界仅不更新checkpoint，不能改变原readiness结果或首因。相同规则适用于其他不能满足投影范围的值。
- E为该轮`observed_since - started`转换的整数毫秒；严格int且0≤E≤(HARNESS_SECONDS+CLEANUP_SECONDS)×1000，范围从现有常量推导。它是轮开始时间，不是失败时间或实时年龄；不读取新的时钟来源，不输出墙钟。

原失败包仍为`code/stage/query_status/query_code/used`，另加上述字段。CLI继续compact JSON UTF-8加一个newline，所有实际回包字节照计。三项red中的最大合法shape采用最长允许枚举、最长sub_state、最大合法数字及32位invocation验证**整个包**≤1024；不是给1024之外另开checkpoint额度。`before + worker_used + len(actual_reply)`必须≤32768，即使下个查询无返回、used保守等于原grant仍成立。原1024预留、约88秒观察、2.5秒回执、5秒cleanup/单命令及资源限制不变；完整setup JSON原有语义也不偷偷改为全局≤1024。

这里只能称最后完整观察轮。后续身份漂移、失败或超时仍保留原失败码，不能把历史轮称为到期快照、最终状态、同字段原子读取，或声称完成exe/四UID/notification前后门；pending轮没有这些事实。回执整体校验/替换落盘是已有原子文件提交路径，不保证磁盘错误时必能保存，也不提供新ready授权。

## 首轮三项 red 与实施后反例

以下三项已由根收齐旧实现真实 red，实施后保留原断言并通过，精确结果见末节：

1. `tests/test_managed_native_checkpoint.py::test_last_completed_round_survives_next_query_timeout_without_new_reads`：真实typed probe＋既有注入fixture，第一完整pending轮后第4次查询无返回。原used保守占满、deadline/query timeout保留；仍必须有第一轮checkpoint，恰4次命令/1次stat，无新补读，整个回包及累计不超限。
2. `tests/test_managed_native_checkpoint.py::test_first_incomplete_round_reports_not_observed_without_recovery_queries`：首轮Id错误，无proc读取，仍3次命令；原native_manager_reply保留，checkpoint明确not_observed，不合成历史轮或恢复查询。
3. `tests/test_managed_native_checkpoint.py::test_checkpoint_reply_fits_original_reserve_and_survives_exhausted_fallback`：最大字段实际JSON≤1024，协调器接纳完整失败包；grant耗满后无余额启动后备诊断，结果仍保留checkpoint和deadline首因、0事务，used＋实际回包不超32768。额外键拒绝。

新增纯fixture节点（根已串行验证，不代表真实服务验收）：

- `test_incomplete_identity_change_keeps_only_previous_complete_round`：下一轮invocation/PID/ticks/cgroup漂移仍保留首轮；恰两轮、六次查询，最多两次stat，无补读。
- `test_first_unbound_or_unprojectable_round_does_not_invent_a_checkpoint`：首次无PID、零/旧invocation或ticks超uint64，下一查询超时仍明确not_observed；第四次命令中断，无恢复查询。
- `test_checkpoint_nested_schema_rejects_unbounded_or_untyped_values`、`test_checkpoint_missing_or_extra_shape_is_rejected`、`test_checkpoint_inactive_requires_an_existing_job`：坏版本、bool、负数、越界、未知键/状态、长或含换行sub_state、缺失形状、零/非规范invocation拒绝；inactive只接纳仍有job。五键历史失败包继续通过。
- `test_projection_limits_leave_previous_round_and_input_untouched`、`test_later_complete_but_out_of_projection_range_round_keeps_history`：投影不改源值；第二完整轮时间越投影范围后不覆盖第一轮，也不替换下一查询原deadline首因。
- `test_success_shapes_and_full_ready_proof_are_unchanged`、`test_incomplete_ready_tail_does_not_publish_a_new_complete_round`：idle/ready成功键不变；ticks超投影范围仍能经原process/kernel/尾部门READY，kernel失败只能携带此前pending轮。
- `test_checkpoint_transport_preserves_original_packet_and_parent_deadline_gates`：以合法JSON尾部空白构造实际1024/1025字节，前者按真实字节计费，后者或到原父截止不接纳；不以只算字段长度代替真实传输门。
- `test_actual_oversized_fallback_json_does_not_replace_saved_checkpoint`：后备合法JSON加入尾部空白后实际超过1024，worker用满原grant使累计超32768，仍保存原manager_failure及其checkpoint，setup按原output_budget拒整包，0事务。
- `test_external_cancellation_still_propagates_without_a_synthetic_receipt`：外部取消仍传播，不能把它伪造为worker成功返回的失败包。只有原函数捕获并实际返回、且外层在原截止内严格接受的包，才保证走既有保存顺序；外部取消、丢包、父截止或写盘错误都不承诺新回执。

根的窄验证入口是 `tests/test_managed_native_checkpoint.py`；随后连同既有 `tests/test_managed_native_ci.py` 复验，再对两脚本及新测试做Ruff/format、两脚本strict mypy。每项通过主仓 `/data0/MuLi/scripts/check_local.py --seconds 30` 串行执行；本子agent不持槽、不启动这些检查。纯fixture仅证明新事实的保存与拒绝边界，不能据此宣称READY修复或自动触发第十九轮。

## 实际根验证与独立后审

实现后新文件**54 passed**（pytest0.18秒、守卫657毫秒/64.6 MiB），连同原native文件合计**560 passed**（1.53秒、2.111秒/115.6 MiB）。这些数量重叠，不相加作为不同用例；所有守卫30秒、swap0，收取真实exit0。

三份Python已格式化且Ruff通过。两脚本strict mypy首次未指定源码路径，在八处旧导入得到Any返回错误；保持源码及规则不变，显式指定本worktree的`MYPYPATH=src:tests`并用非增量检查后通过（5.292秒/284.6 MiB、exit0）。没有通过ignore或安装依赖消除错误。

另一Sol独立只读后审未发现P1/P2：核对真实typed调度和原命令/文件数、严格范围、原尾门、实际1024/1025字节、父截止、后备整包超额和历史保留，确认不是只验证投影函数自身。根也审查了原remaining之后的唯一冻结点及既有协调器先保存失败、后可选诊断顺序。没有本机真实账户、服务、挂载或网络实验；远端仍须取得新精确候选事实，不能据此宣称READY、三事务或正常cleanup已修复。
