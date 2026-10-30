# readiness 最后完整轮的小型失败回执（第十九轮已取得历史轮，仍未 READY）

当前最新结果已推进至 Native48：第一代完整闭合，第二次排队与 prepare 成功，第二代在 helper_exit 触及总 60 秒期限，cleanup verified；本轮另有两个 Windows UIA 探针超时。最新有界计量候选与验收边界见末节。下列早期 readiness 记录保留当时事实。

2026-10-24。根已独审方案和原提交/保存顺序，并通过唯一守卫收齐旧实现三项预期red：pytest 3 failed/0.12秒，守卫597毫秒/66.4 MiB/swap0/exit1；两项缺字段、一项严格五键拒绝，与预期一致。随后实现worker、协调器及新回归，完成根整合与另一Sol独立后审。第十九轮精确候选`db22a5f`现已受测：小回执保住历史pending轮，manager仍未READY、真实事务仍为零，详见末节。此前本地文档HEAD为`1304e1b`，真实第十八轮精确候选为`c0394b0`；本地实现、纯回归与远端结果分别记录。

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

根的窄验证入口是 `tests/test_managed_native_checkpoint.py`；随后连同既有 `tests/test_managed_native_ci.py` 复验，再对两脚本及新测试做Ruff/format、两脚本strict mypy。每项通过主仓 `/data0/MuLi/scripts/check_local.py --seconds 30` 串行执行；本子agent不持槽、不启动这些检查。纯fixture仅证明新事实的保存与拒绝边界，不能据此宣称READY修复；随后授权执行的第十九轮另记于末节。

## 实际根验证与独立后审

实现后新文件**54 passed**（pytest0.18秒、守卫657毫秒/64.6 MiB），连同原native文件合计**560 passed**（1.53秒、2.111秒/115.6 MiB）。这些数量重叠，不相加作为不同用例；所有守卫30秒、swap0，收取真实exit0。

三份Python已格式化且Ruff通过。两脚本strict mypy首次未指定源码路径，在八处旧导入得到Any返回错误；保持源码及规则不变，显式指定本worktree的`MYPYPATH=src:tests`并用非增量检查后通过（5.292秒/284.6 MiB、exit0）。没有通过ignore或安装依赖消除错误。

另一Sol独立只读后审未发现P1/P2：核对真实typed调度和原命令/文件数、严格范围、原尾门、实际1024/1025字节、父截止、后备整包超额和历史保留，确认不是只验证投影函数自身。根也审查了原remaining之后的唯一冻结点及既有协调器先保存失败、后可选诊断顺序。以上没有本机真实账户、服务或挂载实验；当时尚待远端验收，不能据此宣称READY、三事务或正常cleanup已修复。

## 第十九轮实际结果与剩余缺证

候选`db22a5f`的[CI 40029000745](https://github.com/69gg/MuLi/actions/runs/40029000745)最终9项成功、2项失败；Linux [managed-native job120672256510](https://github.com/69gg/MuLi/actions/runs/40029000745/job/120672256510)仍为`native_deadline_exceeded`，随后`native_service_cleanup_failed`。根有界取得并验证artifact12533174463：ZIP **1027字节**，SHA256 `30e4af16c499f7960a2fb8956fa53b71221a4320ace3a8fdc9872af0422de02a`，严格两成员并通过协调器完整result/receipt校验；守卫exit0、104毫秒/19.6 MiB/swap0。本子agent只读根已验证的小result和有限日志，没有重复下载或运行校验。

- `manager_setup/setup_failed`、timeline0、generations0。`manager_failure=native_deadline_exceeded/manager_validation/query_status=0/query_code=ok/used=17468`；本次不是该查询已报告的输出上限或查询退出错误。最后完整轮为`observed/elapsed_ms=81770`：runtime `active/exited/job0`，manager `activating/start/job1759`，PID2305、start_ticks8630、invocation `f5287b597e4c43b39e8bd9ef9bf0fbad`。这是**历史轮开始时间及该轮已核pending事实**，不是到期快照；原pending睡眠或随后期限门仍可触发最终deadline。
- 此轮仅绑定user@的MainPID/stat/cgroup/invocation，未完成ready分支exe、四UID及尾门。receipt登记`/usr/lib/systemd/systemd`也不是本轮exe实测，更不是内部basic完成、进入事件循环或READY发送/接收证明。不能借第十六轮的process正向事实补齐本轮。
- 后备`diagnostic_status=0/diagnostic_code=output_budget`仍是**整个回包因累计字节门被拒绝**；默认`not_started/units=[]`不表示worker未执行。小checkpoint成功保留，不等于后备预算或READY已修复。receipt账户/组已removed、mount0、environment/bootstrap清空，只证明所记账资源走到回收；`resources.cleanup`缺失且服务收尾报错，不能称正常cleanup验收。

同job setup前缀明确为Ubuntu **24.04.5 LTS**、image `ubuntu-24.04/20261012.334.1`。其[精确镜像清单](https://github.com/actions/runner-images/blob/ubuntu24/20261012.334/images/ubuntu/Ubuntu2404-Readme.md)列systemd **255.4-1ubuntu8.17**、dbus **1.14.10-4ubuntu4.1**；这是镜像清单与setup身份对应，**不是该job现场包版本或unit正文实测**。上游v255只能用于有版本边界的源码推导，不能等同Ubuntu补丁后源码或本机Arch安装。

源码复核及唯一待审因果分叉见[设计记录第十九轮](managed-native-ci-design.md#第十九轮历史pending轮已保住ready仍未通过)：当前仍无法区分最终exec前、内部basic/job未完成、或READY发送/接收路径。下一步只考虑在原失败观察总量内替换重复负担，先同代确认实际exec层，再区分basic/内部job；可靠无激活读取通道及真实整包上限尚待核清。此记录不增加字段、轮询、启动变量、期限或预算，不实施该方案、不授权盲开第二十轮。

另一失败是Windows3.13原生UIA首probe的12秒超时：1 failed/3692 passed/2017 skipped，阶段到`uia_ready`、sent1、后cleanup confirmed。它不证明完整response已写出或被Python收到；旧后清理投影不能反推清理前进程/reader状态。该native分支尚无`41af626`同进程前后诊断，不借v2.43另一候选的CI或诊断改进宣称本轮Windows根因已修复。

## 第十九轮之后的最小观察合同（已授权实施，待根验证）

本次只读核对根已保存的 native19 result 和有限 setup 日志：仍为上述9绿2红、零事务；历史 pending 轮已保存，后备整包被拒。没有执行检查、系统命令实验或新CI。下面只建议先审定一个小型 **实际映像/凭据观察**；尚没有能满足原边界的内部 basic/pending-jobs 读取通道，不把整条 READY 链写成已可实施。

**先纠正通道归属。** 当前 `_SetupIO.query` 将 `SystemdProbe` 的 `busctl --user` 显式替换为 `--system`。直接用它查 `basic.target` 或 `ListJobs` 得到的是PID1的状态，不能归入该专用UID。现有代码没有 libsystemd/ctypes peer wrapper；既有 busctl 私有握手及 systemctl 用户连接回退的否决仍成立。缓存上游v255的 `manager_check_finished` 先要求 manager running，继而检查 basic active/reloading；jobs非空会返回，jobs空则进入 `manager_send_ready`。因此实际exe=manager本身仍不能证明进入这一检查、basic完成、队列为空或READY已发送/接收。镜像清单、socket存在、epoll_wait及缺少日志均不能补这些事实。内部层本片固定 `not_observed`；没有已审定的无激活连接、peer身份及接收/分配上限，就不发真实新轮。

**建议替换落点。** 仅在 manager-ready 已保存合法 `last_completed_round=observed` 后，`NativeCI.observe_setup` 将此次旧大包 `diagnose-setup` 替换为一次小型实例观察；无checkpoint或非此失败阶段保留旧分支，不从receipt的manager_program造锚点。小观察复用 `SystemdProbe._unit_path/_properties`、现有proc身份解析、`system_executables` 与窄root worker模式，删除这一分支的版本查询、runtime-dir重复快照、wait/environ/socket/notification/journal读取。不是在旧大包后再追加一次诊断。原manager-ready轮询、成功完整门、sleep、失败首因及先保存checkpoint的顺序不变。

窗口内先 `GetUnit(user@UID.service)`，再读 Unit(Id,InvocationID) 和 Service(MainPID,ControlGroup)，必须匹配历史锚点；调用一次固定root helper读 `/proc/PID` 的 stat/status/cgroup/exe 各两遍，最后再读上述Unit/Service两组属性。共至多6条内部命令（5条系统属性命令、1条root helper），不用用户bus，也不发start/reload/新账户操作。helper只接受既有类型的PID/UID/ticks/cgroup和deadline/allowance，不接受任意路径；复用受信脚本与既有系统映像发现，不硬编码runner映像路径。两侧PID/startticks/invocation、cgroup、实际exe和四个UID须稳定；UID只投影 `all_expected/mismatch`，exe只投影 `manager/executor/other`，不导出实际路径或四个数值。exe=executor仅说明窗口内尚是executor映像；other/mismatch不猜PAM或哪一步失败。完整窗口后才发布observed；单位或proc身份漂移、截断、未知格式只发布not_observed，不能用“前半段相同”通过。此窗口发生在历史轮之后，不能把两者称同一时刻或到期原子快照。

**固定小回包与计费。** 独立有限variant由 `setup_facts` 接纳，旧完整setup仍可读；`fixture_result` 对其与manager_failure锚点做交叉核验，`save_setup_result` 保留原首因与历史轮。小回包键固定为 `version/code/reason/uid/pid/start_ticks/invocation/exe/uids/internal/used`：version是严格int 1；code为observed/not_observed；observed仅reason=none、exe=manager/executor/other、uids=all_expected/mismatch；not_observed仅reason=timeout/output_budget/identity_changed/invalid_reply/unavailable且exe/uids均为unavailable。uid/pid是严格int且0<值<2³¹，ticks是严格uint64，invocation始终为非零32位小写hex；两种code均保留已接受的历史锚点，不合成空PID或空invocation。used为严格int且0≤值≤32768−1024；internal固定not_observed。没有合法checkpoint时走原分支，所以此variant不设no_checkpoint。全部为ASCII白名单，最大值按紧凑JSON及newline保守界小于300字节，fixture同时实际序列化证实；整个外层诊断CLI回包上限仍1024，不能只算新增字段，也不把磁盘上含首因的完整result另限为1024。root helper固定仅 `code/reason/exe/uids/used` 五键，同一code/reason/标签枚举，used限于其原始读取授权，不输出异常正文，继续使用原512字节内层wire门。

设入诊断前已计费B、原总量T=32768，则A=T−B、内层读取授权L=A−1024；A≤1024就不启动后备。所有命令两管道、proc实际读取和root helper wire仍向L累计；root子授权按现有context做法先占用再仅凭严格回包used退差额，丢返回/异常不回收授权。worker顶层即使内层耗尽或末次属性读取失败，也须返回固定小型not_observed，而非整包丢回默认not_started。故 `B + worker_used + len(actual_reply) ≤ B + (A−1024) + 300 ≤ T`；最后仍按实际整包字节与父deadline核验。不得以该公式证明现场一定有足够**采集**余额：两遍status等实际字节可能大于L，此时只能明确output_budget。除非后续能给出剩余采集余额的证据，不为小包通过就盲开下一轮；不挪用2.5秒receipt时间做新观察，不增加原5秒后备/cleanup或约88秒绝对期限，不减少现有ready证明来腾字节。

**仅五组待授权纯反例。** 复用 `tests/test_managed_native_checkpoint.py` 和既有managed-native fake：①同锚点实际manager/四UID匹配，后续原查询耗尽也保留小事实和原首因；②只把实际exe改为executor，或四UID中仅一项不符，必须得到对应有限标签、不能从启动登记判manager/匹配；③只改后侧invocation或startticks，完整窗口必须拒绝且不补读重试；④内层占满原grant及最大合法小包/实际1024与1025字节，前者按used+真实JSON计费，超限/父截止仍拒，旧checkpoint不丢；⑤fake PID1的basic active/jobs空，仍必须internal=not_observed，并断言没有用户连接、没有误用系统查询当用户内部状态。另以同一夹具确认idle/ready成功shape与原命令数不变，不把纯fixture绿写成真实READY修复。

这只让下一证据从“整个后备包被拒”收敛到“同代实际映像/凭据已观察，或明确是哪一个有限门未取得”。若根要求一次取得内部basic及pending jobs，则当前方案**不足以授权该真实试验**：应先独立审定读取通道及硬接收边界，而不是给现有system bus换个查询名、重写D-Bus协议或把无日志当阴性结果。上述合同已获准推进下述纯fixture及实施；它不授权native20。

### exe小片的首轮旧实现red（根已收终态）

根已接受上述最小exe/四UID合同。首轮red冻结点仅新 `tests/test_managed_native_instance.py` 和本文改动，生产未动；根已真实运行收齐5 failed/pytest 0.14秒，守卫635毫秒/66.6 MiB/swap0/exit1，一项仍走diagnose-setup、一项拒新variant、三项缺窄helper。五个节点复用既有Commands/context_reader并禁止真实子进程：

- `test_failed_bound_manager_replaces_large_fallback_and_charges_actual_packet_once`：经真实协调器prepare及observe_setup，旧实现仍调用diagnose-setup，必须red；新合同只调用一次diagnose-manager-instance，并把授权占满的used与实际小包各计一次，保留首因/0事务。
- `test_compact_setup_requires_exact_same_instance_and_never_invents_internal_state`：完整fixture_result应接受新小variant、拒额外键/类型错/锚点不符/内部伪READY；旧实现严格旧setup拒新包，必须red。最大字段真实JSON同时断言小于300字节。
- `test_instance_helper_reads_actual_image_and_all_four_uids_without_environment[executor]`、`[one-uid-mismatch]`、`[ticks-drift]`：缺少窄helper为旧红；随后必须实际走已有proc解析的注入缝隙，分别验证executor、仅一个UID不符、后侧ticks漂移拒绝。保留两遍固定读取、实际字节计费、内层≤512及无env/路径泄漏断言，不用一份预设“好结果”代替helper执行。

根取得上述真实旧红后授权生产及其余期限/丢返回/后Unit漂移反例。当前实施范围仍只有两脚本、新独立测试、本文；另经根单独批准，仅将旧checkpoint测试的合法锚点耗尽fallback断言由diagnostic_code迁为code/reason，保留历史checkpoint、首因、0命令及计费原断言。未自行检查、Git、联网或启动任何真实服务；以下是待根实际验证的实施边界。

### exe小片实施冻结与窄验证入口（待根实测）

- `NativeCI.observe_setup` 只对合法observed checkpoint提前分流并return；不再跟一次旧diagnose-setup。`observe_manager_instance` 复用原5秒与execute_deadline的较早者，父余额≤1024或已到期不启动命令，只保存output_budget/timeout小包。原observe_manager先保存首因及checkpoint的路径、manager-idle/ready成功门和无锚点旧诊断不变。
- worker `diagnose_manager_instance` 最多五条原SystemdProbe系统bus查询加一次窄root子命令。root helper `read_manager_instance` 两遍stat/status/cgroup/exe，复用_start_ticks/_cgroup/_read_regular及受信映像发现；既查全部四UID值也比较两遍值，稳定不符是mismatch，只有中途漂移才是identity_changed。两侧Unit/Service属性、进程startticks/所属cgroup/实际exe/四UID及账户身份均须连续，完整门后才发布observed。内部basic/jobs无观察，固定not_observed。
- `_SetupIO.instance` 先占原raw grant；两管道内层wire实际计费且≤512，固定五键、去重JSON、严格used与标签验证完成后，仅退raw grant减actual used一次。丢回包/命令异常保守耗满原worker grant；已收到但语义不合法的包不退raw grant，已知wire仍计费。父侧同样先占raw grant，收到合法、同锚点、及时且≤1024整个CLI回复后，才以真实used＋真实wire替代占用；父call丢失返回或在内部拒绝过期回复时，保守占满原32768总量。过期/超限/身份错只保存not_observed，不覆盖首因。
- 输入/协议违反硬上限时不能发布observed。实际run_process仍硬限内层wire；注入夹具故意返回超额wire会被拒绝并耗满原grant，外层not_observed的used钳到原grant，不能输出更大的数字令小包再次整包拒绝。此失败包不声称额外字节曾在真实硬门内合法接收。

新独立测试的主要节点：`test_compact_parent_accepts_only_bounded_timely_same_instance_reply`（实际1024/1025、父截止、丢回复、坏used、异锚点）；`test_compact_worker_binds_both_bus_sides_without_pid1_internal_inference`（正向窗口、后invocation/PID/cgroup漂移、内层丢返回、真实typed ProcessOutputLimitError分类、PID1伪basic/jobs正向陷阱）；`test_inner_reply_never_refunds_an_unvalidated_raw_grant`（耗满raw grant、513字节内层wire、重复键、未知UID正文）；`test_no_bound_checkpoint_keeps_existing_large_diagnostic_contract`（原五键与not_observed锚点）；root helper另补实际exe和单UID中途漂移，不能从稳定初始值宣称完整窗口。新文件当前26个参数节点；旧成功门继续由原managed-native整组回归验证，本片没有改它们。

根可先通过主仓守卫串行运行新独立文件及 `tests/test_managed_native_checkpoint.py`，再与原native及四项OutputBudget整合；随后两脚本strict mypy、所改Python的Ruff/format。所有命令仍须主仓绝对路径check_local.py、每项≤30秒，只有真实终态才记录通过。纯fixture绿仍不能证明现场余额足以完成两遍status、manager进入内部初始化或真实READY；没有安全的user内部观察通道，本文不授权native20。

### 根验证检查点（2026-10-24）

独立Sol冻结后复审确认无剩余P1/P2。期间发现真实`ProcessOutputLimitError`未按output_budget分类的窄P2，已复用现有异常类型并补真实异常类注入；没有扩大wire或原授权。根首个新instance＋旧checkpoint检查为79 passed/1 failed，唯一失败是旧oversized测试仍用合法observed锚点却要求走diagnose-setup。该分支已被新小包明确替换，不能伪称原实现仍该走旧命令。

根将此旧节点明确命名legacy_fallback、只把其checkpoint改为真实合法not_observed，使它继续验旧大包1025字节、原超额计费、历史/首因保留和零事务。新26参数的合法observed覆盖新小包1024/1025及同样保留边界；另一Sol读回确认两分支没有混淆。修正后两文件80 passed（pytest0.24秒、守卫735毫秒、64MiB/swap0）；三文件完整native整合586 passed（pytest1.65秒、守卫2.250秒、120.8MiB/swap0）；独立OutputBudget四项passed、八项deselected（守卫511毫秒、53.6MiB/swap0），均实际exit0。586包含80，不重复累计。

严格类型初查未指定源码路径，将原导入推为Any并报十项；显式MYPYPATH后仅剩新variant的setup可能为None这一真实收窄缺口。根只在原predicate前增加等价的isinstance(dict)门，未放宽验证，另一Sol读回无P1/P2。最终`MYPYPATH=src:tests`、strict/no-incremental/explicit-package-bases两脚本通过，守卫5.348秒、285.1MiB/swap0；改后完整三文件586项再验通过（pytest1.67秒、守卫2.283秒、120.8MiB/swap0），不与此前结果重复累计。

四份改动Python最终Ruff和格式检查均通过；根统一格式化三份、另一份原样。以上全部在主仓check_local.py原30秒守卫串行运行，实际exit0，未触发原生服务、账户或真实桌面效果，无网络或下载。本片不是READY或cleanup修复，也未启动native20。

## 待审增量：同一实例窗口内的 direct-peer 标量读取

本节的独立helper已完成离线检查点；窗口集成与父级结算候选见末节，尚待根验证，也不授权native20。不改上述native19失败证据，不把本地586项或镜像清单当作现场READY证明。目标是在现有小实例观察中取得用户manager内部的两个有限事实，**替换**`internal=not_observed`的空位，不恢复旧大包、不追加第二次诊断。

**固定版本依据。** 根已读取上游[v255 `src/core/dbus.c`](https://github.com/systemd/systemd/blob/v255/src/core/dbus.c)的`bus_on_connection`/`bus_check_peercred`及[v255 `src/shared/bus-util.c`](https://github.com/systemd/systemd/blob/v255/src/shared/bus-util.c)167–185行：实际peer凭据为UID0或server自身有效UID才接纳。因此沿用现有真实root helper，不模拟UID、不设trusted、不修改manager认证。该文件217–249行有用户私有连接的new/address/start路径，但其后备连接不在本方案授权内。[v255 `sd-bus.h`](https://github.com/systemd/systemd/blob/v255/src/systemd/sd-bus.h)提供所需非变参API；以上是固定上游源码依据，不等同Ubuntu补丁版或现场连通实测。[peer模式文档](https://www.freedesktop.org/software/systemd/man/latest/sd_bus_set_bus_client.html)及[systemd1接口文档](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html)只作接口解释，latest页面不能替代v255证据。

**唯一连接与调用。** 在已有root helper两次proc stat/status/cgroup/exe读取之间，使用已安装libsystemd的薄FFI：`sd_bus_new`、固定私有socket地址、`sd_bus_set_bus_client(0)`、`sd_bus_start`。socket只能由已核账户UID派生，不能由调用者提供任意地址；缺库或连接失败明确not_observed。禁止open/default、Hello、订阅、自动fallback、CLI替代、LoadUnit/StartUnit或重试。仅发固定`Manager.GetUnit("basic.target")`，再读返回unit的`ActiveState`与同一manager的`NJobs`；GetUnit只查询已经loaded的unit，失败不加载它。用new_method_call/append_basic/read_basic构建及读取，严格核对返回signature和at_end，NJobs只接受`u`，ActiveState只转成有限枚举；不读取ListJobs、任意属性或错误正文。

**身份窗口。** 保持现有前后PID1 Unit/Service属性核对与两次proc四UID、exe/startticks/cgroup连续性门。只有实际exe为manager且四UID均为目标UID才进入peer读取；固定socket连接前后lstat的类型、owner、device/inode须一致。通过`sd_bus_get_fd`取得实际连接FD，核`SO_PEERCRED`的PID/UID确为已锚定manager PID/目标UID，不能用socket属主代替peer身份。调用后再次核socket及原后侧proc/PID1门，任一漂移整组internal事实作废。只证明该有限窗口，不把历史checkpoint、两个顺序标量或最终deadline称为原子同刻状态；basic active且NJobs0也不证明READY已经发出或被PID1接收。

**期限、分配与释放。** 不另开worker或cgroup；FFI在现有受限root helper内执行。每次`sd_bus_call(timeout_usec)`仅取同一个原绝对deadline的剩余额度，外层进程硬截止继续兜底，不为每次调用重置5秒。原5秒后备、cleanup与执行总期限均不扩容。每个bus/message/error及临时FD在全部失败/取消出口释放；借用的bus FD不能误关，若为取peercred复制FD则单独关闭。固定argtypes/restype，不用变参或将native错误串写入回包。libsystemd内部wire接收与malloc由现有真实cgroup上限约束，**不能宣称原32KiB管道/文件预算同时限制D-Bus库内部接收分配**。

**实际计费回执。** 原32768字节proc＋双pipe累计、512字节内层wire及1024字节整个外层回复继续不变，新增有限标量也须容入原包。入诊断前父已付B必须来自真实`setup_bytes`，raw grant为`max(0,32768−B−1024)`，不从native19的worker.used17468推算。父在原结果`resources.setup_accounting`中保存固定小型结算：`parent_before`、`raw_grant`、`child_charged`、`reply_bytes`、`parent_after`和`settlement=validated|unvalidated|not_started`；数字仅取实际账本/实际wire，未知reply_bytes用null，不伪造零。合法同锚点、及时、有界回包才按真实used退差额，结算`parent_after=B+child_charged+reply_bytes`；未经验证不退款，丢回包继续按现有保守规则占满总额。未启动不制造子进程花费。结算不进入worker wire；父对紧凑`{setup,setup_accounting}`实际序列化执行原1024字节保存投影门，最终result仍受原32768字节总门约束。原wire的填充照实计费，保存投影不复制填充。装不下时仅保留固定not_observed和结算/首因，不增加预留；它用于暴露现场是否有足够采集余额，不能保证未来一定取得内部事实。

**首片可证伪边界。** 离线fake缝隙须覆盖真实调用顺序与释放：仅两固定属性、无激活/回退；签名或尾部多值、仅peer PID/UID错、后socket/proc/PID1漂移均拒；剩余deadline耗尽、丢回包不退grant；实际512/513与1024/1025临界及最大accounting投影仍保住原checkpoint/首因。fake只证明协议编排和预算，不能证明FFI ABI、真实EXTERNAL握手或v255认证行为。实现获批并通过离线边界后，另行授权受控临时peer验证这些连接与凭据事实；不接真实用户服务、不扩大预算，也不据此跳过原READY尾门或直接启动native20。

### direct-peer 首片：独立 helper 的离线检查点

根在签名检查点`47c5233`后授权只写独立`tests/helpers/managed_native_peer.py`及`tests/test_managed_native_peer.py`。测试先于helper写入，首次冻结时未执行，根后续实收记录见本节末。公开入口`read_manager_peer(pid, uid, deadline) -> PeerFacts`只接目标身份和原绝对截止，地址固定从目标UID派生；缺库固定unavailable，未引入依赖安装或CLI后备。返回仅含code/reason/basic_state/njobs，不含原生错误正文、路径或unit列表。

候选仅绑定显式非变参v255 API：固定GetUnit加两次Properties.Get，三条消息均关闭auto-start及interactive authorization；每次call取原deadline剩余值。借用字符串用void指针接收，在message存活期间逐字节限长查NUL后复制；append传char[]地址，read传const char**输出位置，不用c_char_p返回值自动展开，也不盲拷最大长度。所有请求/响应/error及bus均有finally释放，SO_PEERCRED使用fromfd复制的FD，只关闭复制件。socket前后有限身份和真实FD的PID/UID核验已留在入口；未来正式集成仍须置于现有两次proc与前后PID1窗口内，helper本身不能代替这些外层身份门或cgroup/进程硬截止。

离线测试用假C符号回填真实ctypes输出指针，核固定请求实参、实际标量来源、原截止递减、完整signature/variant/at_end、空/超长借用字符串、native错误及部分返回资源释放、socket/peer漂移、缺库和无回退；另核借用FD不被关闭。fixture禁止真实CDLL加载、socket和文件open。候选**未接入**read_manager_instance、内外wire/父accounting结算或READY流程，512/1024/32768集成边界仍是后续工作，不能据独立helper结果冒称已覆盖。此次未执行检查、真实FFI/私有socket、真实服务或新CI，不授权native20；ABI、EXTERNAL认证及真实受控临时peer仍未验。

根随后实际收齐首轮27 passed：pytest0.06秒、守卫570毫秒、57.2MiB/swap0、exit0。这只验证离线编排；假C符号不执行原生返回ABI。根按固定[v255 `man/sd_bus_error.xml`](https://raw.githubusercontent.com/systemd/systemd/v255/man/sd_bus_error.xml)核出`sd_bus_error_free`返回void，候选却声明为pointer；没有因为返回值被忽略就认为声明正确。新增独立符号声明回归检查全部19项argtypes/restype，保留bus/message unref的pointer返回及close的void返回。根实收该节点1 failed：pytest0.06秒、守卫560毫秒、57.1MiB/swap0、exit1，唯一失败为error_free的pointer不等于None，fixture已完整进入ABI声明断言。

据此授权仅将该绑定restype改为None，未改调用流程、资源释放或错误分类。根修后实收28 passed（pytest0.05秒；守卫530毫秒、54.9MiB/swap0、exit0）。Ruff通过；格式首检两文件exit1，根仅格式化这两文件后Ruff与格式均exit0。`MYPYPATH=src:tests`、strict/no-incremental限定新helper的类型检查通过（守卫715毫秒、71.9MiB/swap0、exit0）。28包含前述27，不重复累计。根独立源码复核与固定v255声明核对未见剩余具体P1/P2；这些证据仍不等于真实FFI、peer认证、集成wire/父层结算或READY实测，未启动native20。

### 窗口集成与父级结算离线检查点

根在签名`0cdb78f`后先实际执行独立17节点：10 failed、7 passed（pytest0.21秒；守卫708毫秒、72.9MiB/swap0、exit1）。四个身份窗口分支未调用peer、最大合法internal被旧shape拒绝、五个父级结算分支缺字段；它们已进入真实reader/parent账本，未因夹具错误提前失败。据此授权集成；本节尚不记修复后通过。

`read_manager_instance`仅在第一次实际exe为已核manager、四UID全部匹配且proc额度尚余时调用独立helper；返回放在原`internal`位置。随后完整复核第二次proc，再由原`diagnose_manager_instance`复核第二次PID1 Unit/Service、账户及原绝对截止。任一后身份漂移撤销整组，internal恢复not_observed，原始tag/部分内部事实不保留；executor或UID不匹配不打开peer。旧inner五键和outer的`internal="not_observed"`仍兼容。内部仅接受精确四字段、固定状态/有限错误及uint32 NJobs，不把它当READY证据。

### native20 后的有界运行 job 投影（离线检查已完成）

native20 已实际取得同实例用户manager私有peer的 `basic=inactive、NJobs=5`，PID1仍为 `activating/start`，没有进入正式事务；这证明本次固定peer读取成功，不证明READY或完整清理成功。v255在basic已到达或启动jobs清空后发送READY；当前事实更支持启动job未完成，尚未定位具体阻塞单元。自建target虽有`DefaultDependencies=no`，它要求的basic仍拉入sockets/timers/paths，不能称发行版启动图已隔离。本节不改变此前native19、native20原始失败和cleanup失败事实。

复用PID1已有`Service.StatusText`：v255 `manager_print_jobs_in_progress`发送可选`(i of n) `前缀及`User job <ident>/<type> running (<time> / <limit>)...`。只在原小实例的第一次Service属性调用中合并此字段，原五次PID1查询、一次内层命令、身份窗口、5秒绝对截止及32768/512/1024字节门均保持；不追加轮询、ListJobs、通知、预热或重试，旧大包和notification固定标签合同不变。[v255实际formatstring](https://github.com/systemd/systemd/blob/v255/src/core/manager.c#L185-L253)、[固定job type表](https://github.com/systemd/systemd/blob/v255/src/core/job.c#L1492-L1503)、[basic依赖](https://github.com/systemd/systemd/blob/v255/units/user/basic.target)。

新版小实例始终携带`pending_job={code,identifier,type}`。仅完整匹配、ASCII且最多512字节的上述格式可产生`code=user_job`；identifier最多128字节，只接安全unit形状字符/十六进制转义及固定后缀，type只接v255固定job枚举。其余为empty/reached_basic/ready/other/unavailable，identifier与type均为空；不保存完整原文、任意描述、耗时或异常正文。identifier只是STATUS中的安全候选提示，可能来自Description，不能作为已验证unit ID。后侧PID1/进程身份漂移、内层失败或截止使整组不可用，不能保留先读到的job。旧无该字段artifact保持可读，当前collector回包必须是新版完整形状；超出原整个保存投影1024字节时继续按原规则失败关闭和保留首因，不扩大预算。

这是有限窗口中一个运行job的状态文本投影，不是完整依赖图、全部job清单、到期快照或已证实根因。v255的`unit_status_string(unit, NULL)`可能返回Description；如果Description恰为`dbus.socket`，它同样会成为identifier候选，不能反推出真实unit ID，后续定位必须另核实际单元身份。不符合安全形状的内容才返回other；不声称所有描述必定other。即使标签为ready也不是READY送达凭证，不改变成功准入。[v255实际ident选择](https://github.com/systemd/systemd/blob/v255/src/core/unit.c#L1299-L1327)。

根先实收现有`diagnose_manager_instance`两条目标红：单job及`(2 of 5)`前缀均在“未查询StatusText”断言失败，2 failed/26 deselected，pytest0.14秒，守卫632毫秒、66.7MiB/swap0、exit1。随后候选实现和负控已落：固定标签、坏格式/控制字符/未知type/超界、旧artifact与当前回包分离、后身份漂移、截止、真实wire计费及最大合法保存投影。作者未自行执行检查或真实服务；修复后检查结果待根记录，不能据纯回归宣称真实READY已修复或授权下一次远端native运行。

根随后实际收取instance+原CI两文件563 passed，pytest2.01秒、守卫2.664秒、139.2MiB/swap0/exit0；旧checkpoint/peer/peerintegration/resource四文件170 passed，pytest0.44秒、守卫990毫秒、73.8MiB/swap0/exit0。首次外部helper strict未带既有MYPYPATH，使旧导入变Any产生9处旧行no-any-return，不能据此改源码忽略。独立后审指出ident可能为Description，随后仅将新字段unit收窄为identifier，并补Description恰为unit形状的纯反例。最终六个native文件共734 passed（pytest2.29秒；守卫2.961秒、135.6MiB/swap0、exit0），包含此前检查，不重复累计；两脚本使用既有tests+src路径及explicit package bases的strict/no-incremental检查通过（守卫5.522秒、289.2MiB/swap0、exit0）。四份改动Python的Ruff通过，根仅统一三份排版。另一位未参与实现的Sol复核收窄后的投影，未见剩余P1/P2。

本片未改变原READY、清理、身份、执行期限或资源限额。native20的零事务、basic inactive/NJobs5及cleanup失败保持未关闭；离线回归不能证明现场READY已修复。下一次受限远端验证只用于取得新的判别信息，执行前仍须固定新签名head；本地不启动真实manager、FFI/socket、服务、账户或挂载。

正式`python -I /abs/managed_native_worker.py`的argv和隔离不改；脚本模式按固定同目录文件用importlib注册/装载peer，复用实际`__main__`中的worker异常与clock helper，不修改sys.path、不加载第二份worker。包模式保持相对导入。新增离线装载用例只执行脚本定义及受控loader，拦截真实peer，不是新进程`-I`或FFI实测。旧proc-only用例明确注入固定not_observed helper，以保留其原proc/漂移计费断言并禁止真实socket。

新增父级结算由真实调用前账本、实际raw grant和返回wire记录；校验grant公式、严格数值类型、三种settlement等式、setup.used一致性及紧凑保存上限。失联reply_bytes=null且总量饱和，超1024回复记录实际字节但不退款，0余额无命令。保留原checkpoint、首因和零事务；未改READY、cleanup或effects门。除原17节点外，补固定脚本装载及损坏结算反例，均仅写入待根执行。未运行检查、Git、网络、真实FFI/socket、服务或CI，不授权native20。

根随后在原30秒守卫中串行实收：新集成、独立peer和原instance三文件 **80 passed**（pytest0.35秒；守卫900毫秒、71.9MiB/swap0、exit0）；全部五个native文件整合 **640 passed**（pytest1.91秒；守卫2.593秒、133MiB/swap0、exit0）。640包含前述80，不重复累计。五文件完整集合为`test_managed_native_ci.py`、`test_managed_native_checkpoint.py`、`test_managed_native_instance.py`、`test_managed_native_peer.py`及`test_managed_native_peer_integration.py`。

首次Ruff指出两处导入排序，format check指出四文件排版；根仅执行对应导入整理及格式化。最终五个Python文件Ruff及format check均exit0；`MYPYPATH=src:tests`下两脚本和peer helper的strict/no-incremental检查exit0（守卫5.391秒、285.5MiB/swap0）。格式化后的640项整合结果即上文最终结果。另一位未参与实现的Sol只读后审loader、双身份窗口、真实计费及原限额，未见新增P1/P2，未将审查计作额外测试。

本检查点只完成离线窗口接线和父级结算；没有加载真实libsystemd、连接私有socket、创建原生服务或启动native20。真实ABI/EXTERNAL认证、现场READY与cleanup仍缺独立证据，native19失败保持未关闭。不得从640项通过推出这些外部行为已修复。

### native21 现场判别结果（失败保留）

G签名`a0c9771fc31555adec4ac62513457d0ba6dd7b7a`仅启动一次远端[CI40055483739](https://github.com/69gg/MuLi/actions/runs/40055483739)，原生job120757424198在02:46:57Z仍以`native_deadline_exceeded`结束，清理在02:47:01Z仍报`native_service_cleanup_failed`。零正式事务、无generation，不能记作READY或恢复成功。

实际artifact12545256391为974字节，SHA-256 `a453ac47590b2b0d095e5d17697aa87c34d91a11f4f277c009ac6435b3fd7b9c`。根只下载此小包，经原4096字节档案上限、精确两个成员、原解压读取限额、摘要及实际`fixture_result`/`Receipt.read`核验；本地没有启动真实manager或操作账户、挂载。原schema实际接受本次新投影：manager仍activating/start、basic inactive、NJobs=5；同实例内层查询成功，`pending_job={code:user_job,identifier:gpg-agent-ssh.socket,type:start}`。identifier仍只是状态文字候选，须另核真实unit/Description及依赖，不能仅据名字断言该单元已证实为根因。

本次parent账本19865→24729，raw grant11879，实际reply353字节、child charged4511，settlement validated；没有扩原查询、容量或截止。receipt记录临时用户和组已移除、mounts为0，但这不能替代失败的完整清理验收。后续先对照固定镜像的单元定义与当前fixture环境作可证伪修复，不重跑同一失败head、不延长READY或cleanup期限。

### native21 后的单 socket 隔离对照（已收目标红，候选待验证）

下一候选只在新建临时 UID 的私有 `.config/systemd/user` 中创建固定名
`gpg-agent-ssh.socket` 的零字节普通文件。v255 将空单元视为 masked；这是对现场
STATUS 候选的单变量 fixture 隔离，不是已经确认的产品 bug 修复，也不声称已取得
runner 安装单元的完整正文或确定阻塞发生于哪个 Exec 钩子。
保留原 basic/dbus 及其余 vendor 图、READY/事务门、查询次数、95秒/5秒期限和
32768/512/1024 字节限额。[v255 空单元规则](https://github.com/systemd/systemd/blob/v255/man/systemd.unit.xml#L239-L244)。

固定文件沿现有 bootstrap dirfd、NOFOLLOW、O_EXCL、完整目录身份与 root 所有权
核验创建，单独登记设备/inode；没有任意 unit/path 参数或 symlink 放宽。check/remove
须先核验两个已登记叶子的身份和精确正文，再删除任一叶子；空文件同 inode 内容变为非空
也拒绝。外来同名文件、换 inode、symlink、丢失 create 回包均不能领养或删除未知对象。
清理只删已登记叶子；原非空 foreign 树阻止后续卸载/删账户的门保持。
旧 receipt 缺少新身份字段时仍可读取并清理原目标，不能据此领养碰巧存在的 mask。

本轮先新增九个真实临时目录回归，调用原 `NativeCI.prepare`、实际
`bootstrap_files(create/check/remove)` 和持久 receipt，虚拟的仅是现有账户/属主及命令边界。
覆盖固定空叶子的创建/核验/清理、check/remove 下三类漂移、create 遇外来同名叶子、
create 成功但回包丢失。根实际收取九项全部失败、506 deselected，pytest1.19秒；
守卫1.743秒、102.7MiB/swap0、exit1。失败实际到达原bootstrap路径：不创建mask、
未拒漂移/foreign、remove先删target再因非空目录拒绝；不是未来API或导入失败。

据此已实现固定双叶子候选及独立`bootstrap_mask`身份，当前helper回包必须精确含mask，
旧receipt和旧两键manifest保持可读但不拥有mask。所有叶子先读验，并再次复核包含原先
不存在的叶子和目录链接后才删除；正文读取上限还受各固定预期正文长度约束。
补充旧receipt有/无外来mask、旧manifest、remove成功但回包丢失和坏身份schema回归；
原manifest与实际回复字节都继续计入原账本，空正文不增加正文读取额度。
生产/补充回归尚未由作者运行，等待根统一定向、完整native和严格类型检查。
没有启动真实服务、修改主机单元、发起新的 CI；后续单次远端对照仍须新签名 head、
完整离线通过及独立审查，只有真实 READY、正式事务和完整 cleanup 都通过才能记成功。

根统一实收目标选择 **10 passed / 512 deselected**（包含新增旧manifest节点；pytest0.51秒，守卫1.023秒、97.9MiB/swap0、exit0），随后完整六个native文件 **750 passed**（pytest2.11秒，守卫2.738秒、146.4MiB/swap0、exit0）。集合有重叠，不重复累计。首次两脚本strict检查指出删除循环复用已推断非空的leaf变量而接收Optional；根仅更名保存叶子的循环变量，不改行为。修后strict/no-incremental通过（守卫5.682秒、289MiB/swap0、exit0）。Ruff通过；格式首检一份测试排版未统一，根只格式化该文件后，三份改动Python的格式均通过。

另一位未参与本片实现的Sol独立只读后审创建、双叶先验、旧receipt/manifest和丢回包路径，未见确切P1/P2。上述均为离线临时文件和受控账户/命令边界；真实GnuPG单元正文和启动钩子的等待位置仍未取得。下一固定签名head只作一次单变量远端对照，保持原basic/dbus、READY和清理准入，不把mask提示当作已经修复生产根因。

### native22 单变量现场结果（仍未完成启动）

G签名 `0971afe1c29e7f432b8eff83ba9a13dda7d73904` 对应一次
[CI40056790988](https://github.com/69gg/MuLi/actions/runs/40056790988)。
原生 job120761422259 在03:07:22Z仍报 `native_deadline_exceeded`，
03:07:26Z清理仍报 `native_service_cleanup_failed`；零正式事务、零generation。
其它作业的结果不能替代此失败，也不能据临时账户已删除、mount计数为0宣称完整清理成功。

artifact12545886976为975字节，SHA-256
`b0b790fa9c75be28f3a2c288c5d9628f6c15fcedf714dd67e78291e14a223613`。
根以原4096字节ZIP限额、精确两成员、原解压上限和实际 `fixture_result` /
`Receipt.read` 验证，未在本机启动manager。最后完整轮 `elapsed_ms=81838`，
manager仍 `activating/start`，PID2484、start_ticks23639、invocation
`53b50b3d5a8a4a57a72bef50d5db314f`；双侧实际映像、四UID和实例身份一致。
私有peer读取为 `basic=inactive, NJobs=4`，STATUS候选变为
`pending_job={code:user_job,identifier:dbus.socket,type:start}`。
这说明单个GnuPG socket隔离改变了观察结果，尚不能证明剩余阻塞的根因。

原账本仍生效：parent19852→24686，raw grant11892，child used4489，实际reply345，
回包校验成功；原查询记录used17358、status0/code ok。
下一步须区分实际unit及控制子进程的环境、私有peer连接与回退，不能继续屏蔽dbus或整张启动图。
v255的 `bus_connect_user_systemd` 先连接 `XDG_RUNTIME_DIR/systemd/private`，
连接失败才回退普通user bus；`set-environment` 正常使用 `BUS_MANAGER`。
因此仅凭设置 `DBUS_SESSION_BUS_ADDRESS` 不能断言形成循环。
依据为 [v255 bus-util](https://github.com/systemd/systemd/blob/v255/src/shared/bus-util.c)
及 [v255 set-environment](https://github.com/systemd/systemd/blob/v255/src/systemctl/systemctl-set-environment.c)。
尚未取得runner实际安装unit正文或控制子进程证据，不增加期限或把STATUS字符串当READY。

该次CI终态为9项success、2项failure。另一失败是独立的Windows3.12
job120761422295：`test_real_windows_system_module_probe_mta_and_owned_unicode_tree`
在原12秒probe期限报 `read_timeout`，实收1 failed、3696 passed、2207 skipped（390.47秒）。
有限诊断为stages空、sequence unobserved、stderr未封顶、sent1；lease已关闭、
进程nonzero、reader已收尾且cleanup confirmed。没有首个脚本阶段证据，尚不能定位
PowerShell开始前的等待或归因主机负载；不跳过该项、不提高期限、不将九项成功冒充全绿。
这一Windows失败与Linux零事务/清理失败分别保留，不声称本次socket隔离已经修复任一方。

### native22 后：固定 dbus.socket 控制子进程判别（离线实现候选，待统一复验）

根先实收既有正式 peer 入口两条目标红：2 failed（pytest 0.41秒，守卫933毫秒，
98MiB、swap0、exit1）；实际仍查询 basic.target/ActiveState，与所需固定 dbus 三调用
不同，不是缺 API/import。随后授权本节窄实现；作者未运行检查、真实 FFI/socket 或
新 CI，当前候选待根统一复验。STATUS 已提示 dbus.socket，
下一采样固定查询这个已加载单元，不把 STATUS identifier 当作真实 unitID。原
`read_manager_peer(pid, uid, deadline)` 三次调用 **整体替换**：
`GetUnit(basic.target) → ActiveState → Manager.NJobs` 改为
`GetUnit(dbus.socket) → Unit.SubState → Socket.ControlPID`。不调用 LoadUnit、
ListJobs、订阅或自动激活；三条消息仍 auto_start=0、interactive=0，单个显式
private 地址、bus_client=0、FD 的真实 SO_PEERCRED 及 socket 前后身份不变。
依据为 [v255 Socket ControlPID/钩子属性](https://github.com/systemd/systemd/blob/v255/src/core/dbus-socket.c#L99-L115)。

新 peer 四字段为 `code/reason/sub_state/control_pid`，替换旧 basic_state/njobs；
SubState 只允许固定 socket 枚举：dead、start-pre、start-chown、start-post、listening、
running、stop-pre、stop-pre-sigterm、stop-pre-sigkill、stop-post、final-sigterm、
final-sigkill、failed、cleaning。PID 为严格非 bool 整数 `0 <= pid < 2**31`；
失败时两个值均为 null。旧 basic/jobs 形状仅 artifact readback 可读，当前 collector
不得将其冒充新版成功结果，也不生成同时携带两套概况的大包。

worker 在原 manager 两次 proc 及前后 PID1 身份窗口内，用真实 ControlPID 读取
子进程两次 stat/status/cgroup/exe，夹内只读一次 environ；全部读取及映像路径字节
沿同一原 raw grant 累计，任何单读超额/身份漂移/绝对截止使整组失效、不退 grant。
子 PID 必须与 manager 不同，四 UID 同目标 UID，cgroup 在该固定 user@ 实例下；
前后 start_ticks/四 UID/cgroup/exe 必须一致。仅将 exe 归为固定 systemctl/other，
不导出原始 argv、路径、环境或异常正文。环境只分类 XDG_RUNTIME_DIR 与
SYSTEMCTL_FORCE_BUS；其余环境字节仍计费但不解释/输出。两键重复均固定 duplicate；
runtime 为 matches/missing/other，force_bus 按 v255 布尔语义为 on/off/missing/other。
缺文件/读取失败不冒充 missing；完整环境不能在额度内读取则整组 not_observed。

`internal.control` 固定六字段：code、start_ticks、exe、uids、runtime_env、force_bus；
观测成功 code=observed，UID 分类为 all_expected；ControlPID=0 时 code=absent，
其余值 null/unavailable，完全不读子进程。start-pre/post 与非零 PID 只证明采样时
存在控制子进程；PID0只表示当时未观察到，不能证明从未运行钩子或根因位于 bind。
ControlPID 为该次 Get 的采样值，不声称此后始终是 manager 当前控制 PID；两次
proc 只能绑定所读进程本身，不能替代额外的持续父子关系观察。
root peer 成功不证明子进程的实际同 UID private 认证或 fallback 已成功。

原五次 PID1 查询、一次内层命令、5秒绝对截止、32768总 proc/pipe预算及512 inner /
1024 outer保存门保持。原 parent before/grant/charged/reply_bytes/after/settlement
取实际 wire 和账本，丢回复占用原 grant、不退款；新字段替换旧概况而非累加。
不得以新增诊断重置 deadline、扩大回包或削弱 READY/cleanup 成功门。

新 `test_managed_native_socket_control.py` 仅驱动既有 peer/instance 正式入口：固定三
调用、PID0反证、start-post 两侧真实 proc 数据、两键分类、子/manager漂移、期限与
超额撤销及512/1024边界；无未来 API/import 红，不执行真实 FFI/socket/proc。
补充反例核两个环境键重复、大小写布尔语义与ControlPID等于manager时拒绝；
原 peer 的完整 signature/variant/尾部、认证/socket漂移、截止及释放断言保留。
父 validator 仅 readback 接受旧 basic/jobs，当前收集路径必须使用新 exact 形状；
最大 ControlPID/start_ticks 与固定最长 SubState 仍进入原512内层/1024保存门，
原实际 wire 长度和 parent账本回归沿新包形状验证，未取消超额门。
共享旧可执行文件标签仍只有manager/executor；新systemctl分类仅用于本段子进程，
不扩展旧通用诊断枚举。作者尚未检查，后续还须实收外层 PID1漂移与
parent真实计费回归；离线通过不等于 READY 或完整清理修复。

runner 实际 unit 正文和 drop-in 的固定白名单读取另列未实施：会增加文件字节与
身份读取，须先从原预算中明确替换/分配。此片不做该采样、不假定 Ubuntu 当前
dbus.socket 必有某条 ExecStartPost，也不继续 mask dbus 或其它 vendor 单元。

根实际复验新socket-control/peer/integration/instance四文件 **132 passed**
（pytest0.81秒，守卫1.734秒、76.7MiB），原CI/checkpoint/resource三文件
**638 passed**（pytest1.87秒，守卫2.496秒、138.1MiB），互不重叠共770项，均swap0/exit0。
三脚本strict首检遗漏既有源码搜索路径，产生9处旧导入Any返回诊断；不改源码或规则，
显式 `MYPYPATH=src:tests --explicit-package-bases` 非增量检查通过
（守卫5.804秒、290.8MiB、swap0）。Ruff通过；格式首检六文件未统一，根只做排版后
全仓Ruff及378文件格式检查通过。根作为非作者另读实际三调用、uint32属性、
禁自动启动/交互授权、manager/子PID窗口、额度撤销和当前/历史schema分界，未见确切P1/P2。
这些是离线证据，不能代替同UID认证、现场可用余额、READY、正式事务或完整cleanup验收。
下一次远端只针对新的固定G签名head做一次判别，不重跑已知失败的0971候选。

### native23：固定 socket 控制进程已观察，仍未进入真实事务

根实际核定本轮候选head短标识为`81da`，CI
[40059336595](https://github.com/69gg/MuLi/actions/runs/40059336595)最终11项为
**10 success、1 managed-native failure**；native job为`120769236075`。
macOS/Windows的Python3.12与3.13本轮均成功；native22的Windows超时历史仍保留，
不能将这次通过写成已经定位或修复原超时。

根唯一守卫已对artifact`12546503280`执行有界读取、typed schema与SHA核验：ZIP
1345字节，SHA256为`f941c52e3056ee04e3e32b10682022f234b24bf5339408aacb8705752020ec84`，
本地证据为`/tmp/muli-native23-evidence.zip`。以下是根验证后提供的固定允许投影；
作者本次未另行解析ZIP、导出原环境或运行本机系统效果。

- manager为UID999、PID2306、startticks3054、invocation
  `4ec6a1f6feb34dd889cc9a6b974e0204`，PID1状态为activating/start，elapsed81716ms。
- 固定private peer读取成功：dbus.socket为start-post，ControlPID2319；该控制进程
  两侧proc身份接受，startticks3062、exe=systemctl、四UID均为expected，
  runtime_env=other、force_bus=missing，userjob为dbus.socket/start。
- 父级账本为before19895、after31619、grant11849、child charged11237、reply487；
  used17402、queryok。这里是实际父级grant/charged/wire账本，不将child原始used
  或reply重复计费，也不把诊断返回视为manager readiness通过。
- timeline0、generations0。receipt的setup_phase仍为manager_show_before，
  user_created=true、user_removed=false、group_removed=false、mounts3。
  native失败仍为deadline，cleanup为native_command_timeout；不能声称READY、
  事务、回退或cleanup成功，不能用清理未完成的receipt推断新的业务运行。

`runtime_env=other`的比较对象准确为`/run/user/999`：worker只在控制进程唯一的
XDG_RUNTIME_DIR不等于该值时输出other，并不与fixture home、卷路径或runner当前
环境比较；空字符串也归other。manager实例drop-in与harness客户端本来都显式设置
这个规范runtime目录。本次新事实定位到控制进程初始环境的偏离，不证明其实际argv、
目标socket、当前getenv值或等待原因；以前轮次manager初始环境正确不能替代本轮采样。

已缓存固定v255官方源码给出的解释边界是：用户manager先复制自身环境，但初始jobs
前的environment generators可修改其transient_environment，之后还会与client_environment
合并供子服务使用；manager自己的private监听则根据自身secure_getenv的runtime目录
建路径。因此private peer可用与控制子进程runtime偏离可以并存，尚不能归因于某个
generator、客户端或vendor单元配置。
[用户环境及生成器](https://github.com/systemd/systemd/blob/v255/src/core/manager.c#L651)、
[private监听](https://github.com/systemd/systemd/blob/v255/src/core/dbus.c#L948)。

下一最小采样仅为方案：复用同一次已计费的完整environ读取，增加固定
DBUS_SESSION_BUS_ADDRESS的expected/missing/other/duplicate分类，原始值不外传，
不新增proc文件、peer消息、循环、期限或额度，仍经512/1024及总字节门拒绝超额。
v255用户bus入口先看该地址、缺失才按XDG_RUNTIME_DIR派生；但当前尚无systemctl
argv/实际transport证据，不能据此断言它已经选择哪条连接路径。
[地址选择](https://github.com/systemd/systemd/blob/v255/src/libsystemd/sd-bus/sd-bus.c#L1336)。
该方案尚未实施或授权远端新轮次；不maskdbus、不覆盖vendor单元、不延长原期限，
当前诊断正向事实不替代真实服务与完整cleanup验收。

### Native24 合并采样候选：同窗命令与环境（待根复验）

根先在唯一守卫实收新增15个目标节点全部失败：pytest0.30秒、守卫798毫秒、
峰值76.2MiB、swap0。失败分别位于旧control形状、实际未读取cmdline以及current
validator不识新形状，未用缺API/import制造红。随后授权本节候选；作者没有运行
检查、格式化、Git、真实proc/FFI/socket或新CI。

原控制进程environ完整读取复用一次，仅多解释DBUS_SESSION_BUS_ADDRESS；新control
严格六键为code/start_ticks/exe/uids/env/cmd。env恰好三字符，依次表示runtime目录、
显式userbus地址和SYSTEMCTL_FORCE_BUS。前两位固定e/m/o/d，即规范预期值、缺失、
其他值、重复键；第三位为1/0/m/o/d，即v255布尔真/假、缺失、其他值、重复键。
runtime规范值仍为`/run/user/<UID>`，bus规范值为`unix:path=/run/user/<UID>/bus`。
absent或not_observed的control只允许env=`---`、cmd=unavailable以及原null/占位字段，
不能用占位码冒充observed。原值不导出，未知环境项仍丢弃且其字节仍计费。

在原子进程before/after proc绑定之间只增加一次cmdline读取，接受正文最多512字节。
复用原O_NOFOLLOW普通文件读取器；该读取器可能多取一字节判断超长，因此先从当前
raw余额保留这一探测字节，再将读取限额裁至min(512,余额−1)。成功按实际返回字节
计费，读失败、坏格式、期限不足或任一身份后核失败仍保守消耗原grant、不退款，
不将溢出探测挪到原grant之外，也不在失败后追加第二次读取。

cmdline必须非空、完整NUL结尾且无空argv。仅在实际exe已归为受信systemctl时，
匹配完整四参数形状：argv0 basename为systemctl，后面精确为`--user`、
`set-environment`、单个DBUS_SESSION_BUS_ADDRESS赋值；该赋值等于规范bus地址才
输出set_bus，其他赋值为set_other。其余完整命令为other；other映像即使伪装同样
argv也只能是other。参数额外、不同选项或其他完整形状不推断‘未执行过hook’；
截断、空argv或超长使整组not_observed。既不输出原argv/路径/环境，也不根据固定
命令标签宣称已经连接了哪个socket或解释其阻塞根因。

新六键替换两旧长环境键，不叠加字段。当前worker与外层collector仅接新形状；
历史basic_state/njobs和native23的runtime_env/force_bus控制形状仅允许artifact回读，
不能作为本轮collector成功回包。peer代码及其三次只读消息不改；原五次PID1查询、
一次inner命令、5秒绝对截止、512 inner、1024 outer、32768总额、parent真实
grant/charged/wire结算、READY和cleanup门均不改。native23的612字节raw余量与
487字节旧回包只是历史现场，不能保证新现场仍有足够空间；额度不足就保留真实拒绝。

原15项红测保留。候选另补19个参数节点，覆盖坏env/cmd/类型/字段及映像组合、
完整实际raw收费恰好可容与缺最后映像空间、溢出探测字节在原grant内、cmdline期间
截止和other映像不误认hook。原current oracle只随已批准形状迁移，两种历史形状的
回读接受/current拒绝仍分别断言；原子/manager漂移、无ControlPID、超额、父账本
和512/1024最大数值边界继续保留。候选统一待根实际回归与非作者后审；没有新轮次
运行结果，不能写成READY、三事务、回退或cleanup已经修复。

### Native24 离线验收

根唯一守卫实际收取socket/control、peer、peer integration及instance四文件166节点
通过（pytest0.47秒、守卫997毫秒、75.9MiB、swap0）；原CI、checkpoint与资源包络
三文件638节点通过（pytest1.85秒、守卫2.460秒、135.9MiB）。两组共804个不同节点，
没有扩大期限或跳过原失败。三脚本严格类型检查明确使用MYPYPATH=src:tests及
explicit-package-bases，通过于守卫5.401秒、289.6MiB。全仓Ruff通过；格式首验
三文件不合规，仅格式化这三文件后，全仓Ruff和378文件格式检查通过。

非作者Sol后审未发现具体P1/P2：新增读取和溢出探测仍在原grant内，身份后核失败
整组撤回，不输出原始命令，current与历史回读严格分离。结果仅为离线受控采样及
协议/计账回归，尚无Native24真实runner结果，不能替代Native23仍失败的启动与
cleanup证据，也不证明新增观察一定能解释或修复阻塞。

### Native24 现场事实与单变量 runtime 隔离候选（仅测试及方案）

根实际核定精确head `52079de` 的CI
[40061782017](https://github.com/69gg/MuLi/actions/runs/40061782017)最终为
**8 success、3 failure**，失败为Windows3.12、Windows3.13和managed-native，
不能把其余平台结果写作本轮全绿。Windows3.12精确日志为1 failed、3696 passed、
2261 skipped；UIA首probe阶段unobserved、cleanupconfirmed，原问题未关闭。
这里没有推断Windows3.13的具体失败机制，也不将Windows失败归因于Linux隔离配置。

managed-native job `120776658463` 于04:18:03至04:19:31运行，仍为
`native_deadline_exceeded`，cleanup仍报`native_service_cleanup_failed`。
根已用原typed parser核验1008字节artifact，本地
`/tmp/muli-native24-evidence.zip`，SHA256为
`379da7c55e1d9add258aa8ade0a348d00861d8db6325d2d6916601c0eea2c4d6`。
receipt的user/group removed均为true、mounts为0；这只证明该精确账本的回收结果，
不能覆盖cleanup job仍失败的事实。

manager仍为activating/start，PID2293、startticks3029、invocation
`9b5d9d03f26041c585337f9e06b03adb`，最后完整轮elapsed81704ms。
固定peer观察到dbus.socket为start-post、ControlPID2305。两侧proc接受的control为
`exe=systemctl,uids=all_expected,start_ticks=3037,env=oem,cmd=set_bus`：
完整四参数命令确为`systemctl --user set-environment`加规范userbus地址赋值；
唯一runtime值不等于`/run/user/999`，显式bus地址符合规范，force键缺失。
raw11306、grant11893、reply471、parent31628，小于原32768总额；
timeline和generations均为0。未证明actual transport、runtime的具体原值、
偏差来源或真实READY；不能仅据other归因environment generator。

下一候选只给该新UID的私有用户配置增加固定
`dbus.socket.d/muli-runtime.conf`，正文恰为：

```ini
[Socket]
Environment="XDG_RUNTIME_DIR=/run/user/<实际UID>"
```

实际UID从已核账户材料生成，不接收任意unit/path/value参数。仅规范该socket执行钩子
的runtime环境；不改vendor正文、ExecStartPost、DBUS地址、force、target、其它vendor图，
不再mask单元，也不禁用全部generator。根已核固定v255
[socket执行钩子](https://github.com/systemd/systemd/blob/v255/man/systemd.socket.xml)、
[Socket段执行环境](https://github.com/systemd/systemd/blob/v255/man/systemd.exec.xml)
和[执行环境合并顺序](https://github.com/systemd/systemd/blob/v255/src/core/exec-invoke.c#L4114-L4119)：
manager环境在前、unit的Environment在后。这是可证伪的fixture normalize实验，
不是已确认的生产根因修复。

准备复用真实bootstrap的dirfd/NOFOLLOW/O_EXCL、前后目录及叶身份、root文件所有权和
原计费。目录身份与文件身份分别持久为`bootstrap_socket_directory`、
`bootstrap_socket_file`；旧receipt缺字段默认无所有权，旧manifest不授权占有外来树。
同名外来目录/叶不adopt，create丢回包不追认未知inode。check/remove先验证所有已拥有
target、mask、drop-in和目录，再允许任何删除；新drop-in目录内异物、已记账材料替换或
内容改变时保留全部叶及receipt，
不继续卸载或删除账户。只有精确拥有且为空的新增目录可移除。
新增manifest、正文、复验及回包字节仍记原32768总额，沿原执行/cleanup绝对期限裁剪，
不增查询、诊断字段、采样轮次、512/1024门或资源限额。

本轮仅新增真实`NativeCI.prepare/bootstrap_files/check/cleanup`目标回归：创建固定叶及
持久身份后可check/remove；check和remove遇实际inode替换均在删除任何旧叶前拒绝。
账户/属主与系统命令仍由既有fixture替身承接，真实临时目录、helper及receipt链不替换；
旧实现预期在缺少固定叶、忽略未确认树或过早删除target处失败，不以缺API/import制造红。
作者未运行检查，目标红与生产实现均待根下一步授权和实收。

已有采样足以判别，无需新增日志：规范runtime实际生效后仍同hook阻塞，则该偏差不是
充分解释；runtime仍other则不能称实验有效。如果正常启动使失败后采样不再执行，
以真实READY、正式事务、回退及完整cleanup结果判定，不要求强造一条`eem`诊断。
下一远端轮次只有完整离线验证和非作者后审后才由根决定，当前没有触发新CI。

#### 目标红实收与窄实现候选（待根验证）

根唯一守卫已真实收取上述3节点全部失败、522 deselected：pytest0.63秒、
守卫1.171秒、102.1MiB、swap0、exit1。三个失败分别是正式create缺少固定叶、
check忽略未确认inode，以及remove先删除target后才因非空目录失败。不是缺API或
import，也不把fixture失败算作READY结果。

获准后候选仅改worker、coordinator及同一纯fixture测试文件：新manifest显式携带
`socket_directory/socket_file`身份，create才可创建该固定私有树；旧两键/三键manifest
仍可读取但不拥有新树，发现同名外来树立即拒绝。coordinator保留原5项目录链上限，
新目录/叶身份独立记录；旧两代receipt缺字段默认None，半套身份或坏类型拒绝。
所有已知叶与新目录完成读取、完整身份复核后才进入remove；新目录只接受固定单叶，
异物不清除。正常remove丢返回可仅按既有身份确认已缺失材料，create丢返回不追认。
新目录之外既有foreign.service保留/报非空合同不变，不扩大清理授权。

必要负控制覆盖文件/目录inode替换、正文修改、文件和目录symlink、外来同名目录、
旧receipt无所有权、旧manifest拒绝领养，以及新正文实际计入原额度、差1字节拒绝。
原丢create/remove回包回归同步断言新叶与身份；原精确收费回归增加该固定正文和新manifest
的真实字节。未增加诊断查询、轮次、期限或原输出限额，也没有真实socket/FFI/manager操作。
本次作者仅静态回读，未执行测试、检查、格式或Git；候选已冻结交根统一验证和独立后审，
没有授权或触发新的远端轮次，READY根因和完整cleanup仍未关闭。

#### bootstrap 原始读取的增长探测字节（候选窄修）

根先实收59个bootstrap节点通过（pytest0.80秒、守卫1.356秒、115.5MiB、swap0），
随后非作者补真实`_read_regular`增长反例：正文恰好耗尽grant时，同inode在初次fstat后、
实际read前增长，读取器的`limit+1`探测使actual_raw366大于grant365。
该唯一新节点实际1 failed，pytest0.49秒、守卫1.006秒、104MiB、swap0；
原错误返回与保守计账没有阻止实际raw读取越1字节，因此不能仅凭`used=grant`判安全。

窄修只在bootstrap每次叶读取前从当前原剩余额度预留1字节，已知固定正文连同探测
不能容下就提前报output_budget；传给共享读取器的限额也至多为剩余额度减1。
成功时仍只按实际返回正文计费，未使用的探测预留不重复扣账；读取器原有前后身份、
长度和读后增长拒绝不改，任何读取失败仍按原grant全额保守记账。
因此恰好只够正文而不够探测的grant会提前拒绝，不宣称修复后该边界仍执行了增长读取。
共享`linux_transaction`、测试oracle、原32768总额、512/1024门、期限与其它Native25合同
均未改变。作者未执行任何检查；该两文件候选已冻结，待根验证，不新增远端轮次授权。

根随后仅对原增长反例实际复验：1 passed，pytest0.12秒、守卫662毫秒、67.6MiB、
swap0。该结果只闭合bootstrap raw探测越1字节回归，不代表真实READY或cleanup问题已解决。

根继续沿相同守卫收取bootstrap小集60 passed、485 deselected（pytest0.45秒、
守卫968毫秒、79.5MiB、swap0）；完整七个native相关文件827 passed（pytest2.39秒、
守卫3.060秒、154.5MiB、swap0）。60项是827项的子集，不重复累计。
随后对coordinator、worker、peer三个文件执行原严格检查：
`env MYPYPATH=src:tests python -m mypy --strict --no-incremental --explicit-package-bases`
通过（守卫5.758秒、289.9MiB、swap0）。全仓Ruff通过；两文件仅排版后，
全仓378文件格式检查通过（守卫68毫秒、21.7MiB、swap0）。
非作者审查除已修raw+1外未见新增阻断问题；
这组离线结果不替代下一次隔离runner上的真实启动、事务与完整cleanup验收。

### Native25：READY 后的交易进程环境边界（仅目标红，生产未改）

根已验证精确候选 `088c2c7` 的 CI `40064351476`、native job `120784343681`：
运行在 04:57:18–04:57:21 失败，cleanup 步骤仍失败，不能沿用此前约 82 秒的 deadline
解释。792 字节证据包 `/tmp/muli-native25-evidence.zip` 的 SHA-256 为
`325f70acb0d580366f8a5d5c0c1333ad6eaf0631dbdec0cba2d9dc7bf2a01f56`；
根已通过既有成员、Receipt 和 `fixture_result` 严格校验。

本轮 manager readiness 为 240ms、`late_ready=false`；实际交易 worker 已进入 `import`，
但固定环境门报告 `native_environment_mismatch`：UID、HOME 和 user-bus 地址匹配，
`config_source=explicit`，XDG config/runtime 均不匹配。timeline 只有 elapsed 0 的 import，
generations 为空，正式事务仍为零。原 harness 256MiB/CPU1/32 tasks 与 manager
1GiB/CPU1/128 tasks 均保持。receipt 最后为 `manager_show_after`，登记 manager 为
`/usr/lib/systemd/systemd`，user/group removed 为 true、mounts 为 0；这不覆盖
`resources.cleanup=failed`，不能称完整清理已验收。

源码区分启动客户端和被启动进程：`user_arguments` 的 `env -i` 只规范调用
`systemd-run` 的客户端；真实 transient unit、后续正式应用和 helper 仍继承 user manager
的环境。当前事实不证明偏差由某个 generator 引入，也没有导出偏差原值。仅在两个
transient argv 增加环境不能覆盖后续正式服务，不能视为完整修复。

根批准的下一单变量仅针对这个新 UID 的 fixture manager：READY、原私有材料与身份守卫
之后，任何 transient/application 启动之前，实际 dispatch 一次固定
`systemctl --user set-environment XDG_CONFIG_HOME=<owned home/.config> XDG_RUNTIME_DIR=/run/user/<实际UID>`。
只写这两个既定值，不改 HOME、DBUS、force、vendor unit、控制命令、生成器、READY 或
worker/app 的环境门。这是新增的一次有界写操作，必须诚实计入原执行绝对 deadline 和
32768 输出额度；不声称总调用数未增，不增加原时间、内存、任务或输出上限。

先行两个纯回归走真实 `NativeCI.prepare/execute` 与既有 `Commands`：第一要求实际准确
新 UID dispatch 发生在 READY 和材料复验之后、首 transient 之前；第二让受控 manager 的
两键最初不符，只由实际收到的固定 `set-environment` 命令改变其状态，再交原
`worker.require_environment`，要求从拒绝到 drive/cleanup 均可接纳。只改父客户端环境
不能让该测试通过。账户、manager 和系统命令仍由离线替身承接，未运行真实 systemd、
FFI、私有 socket 或 generator；不宣称这证明真实继承、正式事务或 cleanup 已修复。
本轮只有测试和方案已写，作者未执行检查，生产待根实收目标红后另行授权；没有新 CI 授权。

### Native26：固定两键 manager normalize 候选（生产已冻结，待根复验）

Native25 精确 CI `40064351476` 最终为 10 个 job 成功、managed-native 一个失败；
Windows3.12/3.13 本轮实绿，不覆盖 Native24 两个 Windows 失败的历史。
根随后实际收取上述两项目标红：2 failed，pytest0.85秒、守卫1.410秒、105.7MiB、
swap0。一项准确失败于未 dispatch normalize，另一项实际到原 worker 环境门，
以 `native_environment_mismatch` 拒绝；不是缺 API 或导入失败。

授权后的窄实现仅在原 prepare 的真实 READY、manager kernel limits 和 bootstrap
复核之后，再复验原 manager 私有 drop-in，并用已核新账户和 owned home 生成两键。
固定 `systemctl --user set-environment` 实际以该 UID 调用，先于首个 transient；
不接受父环境或任意键值注入，不触碰 worker、app 或 cleanup 原有环境门。
这是一次新增命令，沿原 execute 绝对截止裁剪至原5秒命令上限；未增查询或另起期限。

新增命令不用先抛非零状态的 `checked/as_user` 包装：先计原始 stdout/stderr 合并回包，
再判状态。真实默认 runner 仅此处接收原 setup 余额减1的输出限额，为原余额内的
溢出探测留位；旧调用的默认32768门不变。成功按实际字节记账，非零也计回包，
超额或无回包保守耗尽余额、不退款；不足以保留探测时不 dispatch。
离线注入 runner 只在返回后核额度，不宣称它具备真实进程读取硬门。

必要负控覆盖非零、零/非零超额、丢回包、无余额、未确认 drop-in、READY 后真实
私有环境/bootstrap/资源材料改变时不 normalize 或启动 transient。原先两个核心
行为 oracle 保留，其中准确命令断言还在外来父 XDG 环境下运行。
作者未运行检查、格式、真实服务或新 CI；候选等待根统一离线复验与非作者后审。
READY240ms 是 Native25 已观察到的进展，正式事务和完整 cleanup 仍未验收，
本候选不证明环境偏差来源或真实继承已修复。

根首轮实际收取新增11项全部通过（pytest0.52秒、守卫1.065秒、102MiB）；
完整七文件为837 passed、1 failed（pytest2.48秒、守卫3.146秒、159.1MiB）。
唯一失败是旧启动前观察 oracle 把全部 manager 私有环境读取都限制为一次；新增
READY 后、normalize 写入前的所有权复核是本片已授权的新观察，不能删除来迁就旧断言。
测试现按真实 reload/READY/normalize 命令边界区分：启动前仍恰好一次，位于 install
之后、reload 之前；READY 之后、normalize 之前另恰好一次，二者各自实际正文长度均计入
原 setup 字节账本，不能有其它未分类读取，也不放大32768额度。只改测试和本说明，
生产仍保持冻结；该 oracle 修订待根复验。

### Native26 最终离线验证与独立后审

根对上述唯一修订 oracle 实际复验为1 passed。因此完整七文件首跑的837个通过节点，
加该唯一失败节点修订后的通过，共覆盖838个不同节点；**不是一次838项全量通过**，
新增11项是其中子集，不另行累计。生产未为该旧断言删减复核或改变预算。

coordinator、worker、peer 三脚本沿原显式 `MYPYPATH=src:tests`、
`--strict --no-incremental --explicit-package-bases` 检查通过，守卫5.340秒、
290.2MiB。根最后仅排版两个文件；随后全仓Ruff通过（83毫秒、25.8MiB），
378文件格式检查通过（87毫秒、21.9MiB）。

D 非作者正式后审未发现剩余P1/P2：新增写入只在READY后针对该新UID执行一次固定
两XDG键的 `set-environment`，保留原绝对deadline、原输出记账和worker/app/cleanup
环境门。以上均为离线受控验证；真实manager环境继承、正式事务和完整cleanup仍待
新的精确远端CI，不能由Native25的READY进展或这些离线绿推为已修复。
作者未执行检查、格式、Git、网络或真实服务；本次仅追加文档，源码与测试保持冻结。

### Native26 精确远端进展与下一失败边界

根对G签候选 `df0982e5a7e5fda398d1b4139859db9dfa733250` 的单次CI
`40066211245` 收取 managed-native 失败证据；其它job当时仍在运行，
此处不提前给完整矩阵终态。artifact `12549356834` 为814字节，本地
`/tmp/muli-native26-evidence.zip`，SHA-256为
`fdab36dbbee3c6d67a94ea8e7e0b67d43d3e41763bfea03c75296f0dd809f9f0`，
已由根按原 `fixture_result` 与 `Receipt.read` 严格校验。

本轮真实worker的HOME、UID、bus、config与runtime环境门全部为true，manager
ready260ms、late=false；harness仍256MiB/CPU1/32tasks/swap0，manager仍
1GiB/CPU1/128tasks/swap0。cleanup为verified，receipt的user/group均已移除、
mounts为0。这次环境门与完整cleanup已实际推进，不再沿用Native25的环境拒绝和
cleanup失败描述，但不据此证明环境偏差的历史来源。

正式事务仍未开始：completed=false、generations为空，code为 `FileExistsError`，
最后stage为 `slots_verified`；timeline依次为import0、imports_verified753ms、
slots_verified882ms。下一步只读定位该stage之后的实际创建路径，尚未授权生产修复；
不增加日志、原始路径输出、期限或额度，也不把本轮环境和cleanup通过写成事务成功。

### Native27：复用已拥有 bootstrap 目录的准确红绿

源码定位到真实 `Case.drive`：标准 `UserService.path` 已要求为私有
`home/.config/systemd/user/muli.service`，其父目录早已由原bootstrap的
dirfd/NOFOLLOW/no-adopt创建并记录身份。原drive在写unit前却再次执行无exist_ok的
mkdir，因此正常抵达此处会对已有目录抛 `FileExistsError`。

新增单项回归复用真实prepare/bootstrap create/check与receipt、已有真实retained-slot
材料helper及原 `Case.drive` 注册、配置和服务render/write/chmod路径。只替代当前进程
布局/账户环境、临时cgroup与文件系统认证边界、完全不bind的端口替身和首条外部命令；
屏障在 `manager_start` 的daemon-reload调用前核准确unit正文、0600权限、原父目录
inode和bootstrap target未变，然后终止，不执行systemctl、HTTP或正式事务。

根实收该selector准确红：1 failed，pytest1.01秒、守卫1.670秒、115.2MiB，
实际失败为 `Case.drive:3676` 对同一owned目录抛 `FileExistsError`，不是夹具前置。
根随后仅删除这一次重复mkdir；同一selector转为1 passed，pytest0.50秒、
守卫1.106秒、89.5MiB。原bootstrap创建的no-adopt、目录与叶身份门均不变，
原准确路径require、service render/write/chmod、启动余量检查与首manager命令顺序不变；
不以exist_ok放宽外来目录接纳，不增诊断、查询、额度或期限。

只读复核该一行窄修及首命令屏障未见剩余具体P1/P2。完整七文件回归仍待根收取，
本项绿只证明复用已拥有目录能走到首条manager命令之前；Native26真实artifact中的
环境全true、cleanup verified事实保留，正式事务仍为零，不能写成Native27远端成功。
作者未自行运行检查、Git、网络或真实服务；本次仅追加文档，生产与测试由根冻结验证。

根随后实际收取完整七文件839 passed：pytest2.64秒、守卫3.271秒、159.2MiB、
swap0；上述新增单项包含其中，不重复累计。三脚本沿原显式包路径、strict、
no-incremental及explicit-package-bases通过（守卫5.358秒、290.4MiB）；
全仓Ruff通过（76毫秒、26MiB），378文件格式检查通过（70毫秒、26.1MiB），
新增测试格式无需改变。此为Native27离线完整相关回归，未替代下一精确远端运行的
正式启动、事务和cleanup结果；文档再次冻结，作者未自行执行检查。

### Native27 真实首代事务失败与 Native28 最小首因证据候选

根对G签 `cb6c7c248434e4aebc2dc5abc2f4712b69753d56` 的单次CI
`40066956416` 收取 managed-native job `120792123122` 失败证据。
artifact `12549756223` 为1630字节，SHA-256为
`686d6c7238888e2117eb043f34804c36052416bb04e2ba1cf062a8193213710f`，
已由根按原typed parser校验。环境门全true、manager ready266ms；本轮实际到达
origin_ready5049ms、identity_frozen5054ms和adopt准备5083ms，generation1随后经历
prepared6463ms、draining13262ms、activating18362ms、rolling_back22183ms及
recovery_required23222ms。最终code为 `native_transaction_blocked`，completed=false；
helper_exit23247ms记录helper PID2611、Result=success、exit_status=0。
驱动正常返回恢复态不等于事务成功；尚无已完成代次，也未进入后续switch B/A验收。

本轮cleanup仍失败，receipt的user_removed/group_removed均false、mounts为3，不能
沿用Native26的cleanup verified。源码另确认fixture写入的 `muli.service` 没有对应
精准清理步骤，可能触发原bootstrap非空保守门；这与本轮事务首因独立，尚未由该
artifact证明它是本轮cleanup唯一首因，本节不修改删除授权。

现有revision观察已真实读取完整typed record，但timeline只保留phase/activation等
旧白名单：既丢掉首 `failure_code` 与最终 `code`，也可能把同phase不同intent的两个
revision按相同facts合并。Native28候选仅在这次既有record读取的原transaction行增加
`revision`、`intent`、`code`、`failure_code`、`rollback_attempted`；直接复用原严格类型
和有限枚举，不输出health、challenge、原始路径、异常串或完整journal。不增加journal
读取、查询、采样轮、原时间或字节额度，仍受64行及原32KiB实际序列化门约束。
`backend_failed`仍是受控归类码，不凭它推断systemctl的具体失败原因。

新单项 `tests/test_managed_native_transaction.py::
test_actual_transaction_preserves_first_and_final_failure_revisions` 复用真实retained-slot、
anchor与快照发布夹具、真实TransactionJournal和 `advance_managed`：有限HTTP替身依次
推进activating/stop_base、activating/install_target、rolling_back/stop_target首因
deadline_exceeded、recovery_required最终backend_failed。实际 `Case.transaction` 读取
这些已落盘revision并触发原 `native_transaction_blocked`，然后核原result.json同时保留
四次revision、intent和两个不同失败码，而非仅检查未来schema字段存在。外部HTTP及
helper进程观察为离线替身，不执行systemd、进程或真实私有socket，不宣称完整托管旅程。
作者仅新增此测试与本说明，尚未运行检查、格式、Git或网络；生产未改，等待根收取真实红。

根随后收取上述唯一selector真实红：1 failed，pytest0.34秒、守卫884毫秒、
64.4MiB、swap0；实际 `Case.transaction` 已读取四个正式revision，但首两次同phase、
不同intent被旧note去重，落盘少一行，尚未到后续字段断言。此为行为缺口，不是
missing API、导入或未来schema前置失败。Native27整轮CI现已completed/failure，
准确头仍为 `cb6c7c248434e4aebc2dc5abc2f4712b69753d56`：其它10个job通过，
唯一managed-native因正式事务及cleanup失败，不能描述成其它10项也失败。

根授权后的候选实现只在同一原revision note补上上述五字段；worker复用原
`Code/Intent` Literal约束，coordinator以相同有限枚举校验，revision为正的有界精确int、
rollback_attempted为精确bool，code/failure_code只接受原Code或null。出现新字段须为
transaction行的完整五字段组；旧artifact不含该组仍按原形状接纳，不补假值，不从
字符串推断历史revision。实际同phase不同intent现在因revision字段而保留为不同观察。

真实Journal目标回归追加原 `fixture_result` 收集入口的落盘回读，另补旧时间线形状
不变、bool冒充int/越界revision、非枚举intent/code、任意失败正文对象、非bool回滚标记、
部分组及错误stage拒绝的纯边界回归。原64行、32KiB门与期限不变，无新journal读取、
查询或health。候选尚待根统一定向及相关整合验证；作者未运行检查、Git或网络，
不据诊断字段实作宣称adopt首因已查明或cleanup已修复。

根随后实际完成Native28离线验证：新增文件10 passed，pytest0.33秒、守卫875毫秒、
72.1MiB；原七文件839 passed，pytest2.68秒、守卫3.321秒、159.2MiB。两组共849个
不同节点，不是一次849项全量运行，新增目标selector包含在10项中，不另行累计。

coordinator、worker、peer三脚本strict通过（守卫5.481秒、290.2MiB）；全仓Ruff通过
（74毫秒、24.3MiB），379文件格式检查通过（62毫秒、21.5MiB）。根仅格式化一个
文件，无语义改动；并独立逐项核对新白名单与原 `Intent/Code` 枚举完全一致，原deadline、
读取次数及行数/字节门保持。A随后完成正式非作者只读后审，未发现剩余必须修复的
P1/P2：确认五字段成组、精确类型及枚举、新旧回执兼容，直接复用原已读record，
没有新增命令、读取或期限。
以上离线绿只验证固定首因证据的保留与边界，不证明Native27真实adopt失败根因已解决，
也不包含cleanup修复或新远端事务验收。本次仅追加文档并冻结，作者未改生产/测试，
未执行检查、Git或网络。

### Native28 现场首因边界与 Native29 最小异常位置提案

根对精确头 `0b2a3dd16e4f2a0af157f2b7a26ea71306765620` 收取并严格校验
artifact `12549314435`（1730字节，SHA-256
`b6e95867b963ecec8564a8b419c3326ae90e873d232547bf58de56a4fc44ff09`）。
环境全true，origin_ready6286ms；rev1 prepared7792ms、rev2 draining14991ms、
rev3 activating/stop_base20688ms、rev4 rolling_back/stop_target24938ms、
rev5 recovery_required25983ms。后两次code和failure_code均为backend_failed。
helper在15019ms为active/exit0；这不是事务成功。cleanup仍失败，receipt三mount及
账户未移除。准确结论仅是stop_base位置失败、尚未进入install_target：通用controller
在执行stop之前仍先调用observe，现有证据不能区分二者，更不能证明systemctl stop失败。

最小候选复用 `src/muli/failure_diagnostics.py::capture_failure`，不新造异常原因枚举。
在 `management_controller._Controller._call` 原异常改写前捕获原exc，以固定前缀输出
一个有限JSON envelope：version、generation、revision、intent及原五键diagnostic；
固定MESSAGE前缀为 `muli.controller_failure.v1 `。
保留原backend_failed/deadline_exceeded、clock_invalid直传和CancelledError语义；
capture、序列化或日志写入失败均局部隔离，不改变任何事务决策。每个controller实例
仅首次backend failure尝试一次，先标记已尝试；回滚后错不覆盖、不重试capture。
不输出str/repr、异常参数、locals、cause/context、路径、nonce或HTTP正文。
使用标准固定logger，无exc_info/stack_info；独立helper的日志配置须确保该固定消息
能到原stderr/journal，而不是假定Runtime私有logger可用于helper。

原payload最多16帧/4096字节，仅保package-owned module/function/line；
ObservationError与TransactionError精确类型不在原表，返回unknown是正常结果。
这仍可定位受控raise，但16帧按外向内截断、locals/lambda未知帧及下层异常包装意味着
不保证每次取得最深raise或真实外部命令原因。reader通过现模块新增极小公开validator
复用原 `_validated`，不从外部直接导入私有函数、不另复制schema；实现前须保留
isolated脚本的固定模块装载边界，不能为复用而放宽sys.path或制造通用诊断框架。

谁读/何时读：worker只从 `Case.helper_empty` 原已取得且核过name/argv/cgroup的snapshot
冻结非零PID与invocation，关联当前generation；不增加probe。没有可信快照、早collect或
同代身份漂移则not_observed，不补查询或按名称捞旧日志。root coordinator在现
collect_result之后、cleanup停manager之前，最多新增一次有界journal读取；不由隔离UID
worker读系统journal，不使用 `journalctl --user`。固定查询同时匹配可信 `_UID`、`_PID`、
`_SYSTEMD_USER_UNIT`、`_SYSTEMD_INVOCATION_ID`，只取所需固定JSON字段；MESSAGE须匹配
固定前缀、完整envelope、受信代次及其已观察revision/intent和公开validator。
任意其它消息不导出。只保留该controller实例的首条，不用回滚诊断覆盖它。
可信字段缺失、超额、超时或坏日志均只使诊断不可用，不改变原code与cleanup结果。

字段与匹配依据是systemd255官方
[journal-fields](https://www.freedesktop.org/software/systemd/man/255/systemd.journal-fields.html)
和 [journalctl](https://www.freedesktop.org/software/systemd/man/255/journalctl.html)：
不同字段AND，可信invocation对应unit运行周期；非下划线用户字段INVOCATION_ID不能替代。
文档不证明本轮runner必定提供这些字段，真实缺失必须如实not_observed。

这明确新增一次journal读取，不再声称查询数不变；没有新增DBus probe、重试、启动或
期限。journal实际stdout/stderr、JSON envelope及最终保存投影均计入原剩余字节，
保原绝对deadline、进程资源、64条timeline、32KiB总门和receipt/result两个artifact成员。
4096字节是diagnostic上限，不是另发额度；剩余不足必须不读/不接纳，不能取消原失败
或截坏JSON后强行采信。安全诊断放原result的严格optional resources字段，不加journal
持久字段或修改Code枚举；旧artifact缺该字段仍兼容，新增绑定材料不算事务授权。

预计生产接缝仅原failure_diagnostics的公开校验、management_controller原catch处、
worker既有helper快照绑定、coordinator一次rootjournal读取及fixture_result严格投影。
先红应走真实Journal/ManagedTransactionController.step和真实package内受控raise：
原backend_failed/回滚决定仍成立，但缺应有的安全诊断；再核observe与stop位置可区别、
异常正文不泄漏、诊断自身失败不遮原错。journal解析另沿真实有限回包入口做身份/前缀/
预算负控，不以函数存在或未来schema缺字段作为唯一红。提案首次冻结时仅改文档，
未运行检查、Git、网络或服务；尚无Native29/真实异常位置验收。

根随后核Native28整轮远端终态：其它10个job成功，唯一managed-native的run与cleanup
两个step失败；这不改变上述准确事务与清理边界。Native29提案前审后进一步收窄为
每controller实例最多一条首失败诊断，尚无实现或新远端运行。

随后仅新增 `tests/test_managed_controller_failure_diagnostic.py::
test_real_stop_failure_keeps_decision_and_safe_original_package_location`：复用真实Host的
快照/Journal/advance与ManagedLinuxBackend，经真实controller.step和真实包内_command
抛ObservationError，只有manager证据、HTTP及外部command返回为离线替身。先核准确
stop argv确已到达、原backend_failed与回滚决定不变，再期待首条安全位置诊断且无命令
正文泄漏。未替换capture/logger，不导入未来API；作者未运行，生产未动，待根收真实红。

根实收此唯一目标真实红：1 failed，pytest0.49秒、守卫1.054秒、64.6MiB、swap0。
实际observe通过、准确stop seam到达一次，原rolling_back/stop_target、两backend_failed、
rollback_attempted及同deadline均已通过；唯一缺口是应有一条诊断实际为零。

授权后的首批核心候选仅修改failure_diagnostics和management_controller：公开校验函数
委托原_validated，不另造schema；原_call改写前捕获原exc，首attempt标记先于capture，
固定logger输出安全envelope，取消与clock_invalid不诊断，诊断自身普通异常被局部隔离。
本批源码静态读回后冻结，原目标测试不改；尚待根实测，不提前写绿。CI收集接缝尚未实现。

根首复验仍为1 failed（pytest0.47秒、守卫1.135秒、63.9MiB）：诊断已出现且controller帧
可定位，但真实backend两帧为unknown，原目标位置断言未放宽。源码确认其继承标准
AbstractAsyncContextManager，位置门原只接纳精确type而遗漏标准ABCMeta。候选仅额外
接纳精确abc.ABCMeta，仍核原module/globals/FunctionType/code身份；未知元类及恶意
ABCMeta子类继续拒绝并补两负控，另独立断言真实backend确为该标准元类。
同时将read_failure独用的query依赖移入该函数，Store仅TYPE_CHECKING，使同一原文件
可由固定-I脚本纯stdlib装载公开validator；不改sys.path或复制schema。再次静态冻结，
原目标oracle不变，尚待根复验，CI收集仍未落盘。

根随后实收核心及全部旧failure diagnostics共41 passed（pytest0.99秒、守卫1.739秒、
94.3MiB、swap0），原真实stop位置oracle与精确ABCMeta正控均通过。中间一次为
2 failed/39 passed（pytest1.01秒、守卫1.660秒、96.6MiB）：仅两恶意元类负控的
monkeypatch.setattr自身先访问替身__class__，未进入collector；根只改为对原模块
vars的setitem，不改负控断言或生产。该夹具失败与collector缺口分开记录，不抹历史。

CI收集候选现已落盘并静态读回：worker在原helper_empty快照观察内冻结有限
helper_source，关联已有generation/helper，正PID与非零invocation、原argv和实际UID
cgroup成员须通过；同代PID/invocation/argv/cgroup漂移撤销且不重新领养，已collect时
仅保留此前可信快照。没有新增snapshot/probe。root coordinator仅在原execute finally
collect_result后尝试一次；无source、UID/代次不符、无余额或截止即不查询。
固定root journalctl不加--user，四可信字段AND，并固定前缀grep及最多两行哨兵；
完整MESSAGE仅通过同一公开validator和已观察generation/revision/intent后导出首条。
stderr/stdout仍走原合并pipe的真实总字节门，读取grant为原剩额减一（给增长探测留位）；
所有返回状态先收费，丢回包耗尽原grant且不退款。保存仍受原32KiB门；放不下时退为
有限not_observed，连该摘要也无空间则保留原已存result，不抹首因。

诊断只放原result.resources的两个严格optional字段helper_source/controller_failure，
旧artifact缺字段照旧接纳，仍是两个artifact成员。cleanup独立coordinator回读原result
时复核并保留同源首诊断，不重新读journal。固定源文件importlib装载仅复用同一公开
validator，校验缓存module身份/路径，不改sys.path、无新muli导入依赖或schema复制。

新增离线收集回归沿真实controller原包内raise和真实capture生成MESSAGE；helper快照、
journal回包与account边界为明确离线替身。覆盖root只读一次及实际raw收费、四trusted
字段和envelope篡改拒绝、无余额/无绑定/撤销/截止零查询、丢回包/超额/失败不退费、
cleanup重开保留首条、helper身份漂移与collected边界、固定validator装载；另补core的
capture/logger失败只尝试一次且不改回滚，以及取消/clock_invalid不诊断。作者未执行
检查、格式、Git、网络或服务；上述CI接缝和新增负控仍待根统一验证，非真实journal验收。

最终静态读回又将cleanup保留条件收紧为完整helper_source相等；即使generation相同，
PID/UID/helper/invocation改变也不能把旧首诊断改绑到新来源，补独立重开反例。
离线parent夹具显式设置临时Receipt供原volume路径计算，不把缺receipt当功能红。
本批收集接缝与负控现语义冻结，仍未执行作者检查；前述41绿仅核心及旧collector范围，
不代表新增journal收集测试已通过，也不代表真实runner能提供所需trusted字段。

根随后运行四个原controller/backend文件时，30秒守卫在约99个点后终止，无summary、
无OOM；其间误启动另一strict守卫短暂争用CPU，不能据此把失败直接归因竞争。
只读参数展开将第99/100项定位到原config-prefix的错误/取消两参；根单独在15秒守卫下
实收2 passed（pytest5.29秒、守卫5.885秒、CPU5.848秒、64.6MiB），两call约2.37/2.39秒，
无异常环路。两项直接调用backend.install，不经过新增controller诊断路径；其原真实
slot哈希/CAS和文件同步成本保留。后续整合应按原30秒拆互不重叠组，不提高限额；
这次分组终止仍保留为未完成整组检查，不能当成四文件通过。

根随后按互不重叠组收取全部实际终态：两controller共86 passed（pytest0.66秒、
守卫1.194秒、61.9MiB）；backend前12 passed（12.96秒、13.487秒、65.9MiB）；
backend后20 passed（22.88秒、23.428秒、67.7MiB）；linux45 passed（0.32秒、
0.868秒、63.1MiB）。加已单独验证的config-prefix两参，共165个不同节点全绿，
未提高原30秒上限，也未重跑整组。真实hash/CAS使这些分组的CPU总计约44秒，
原整组本身超出30秒；旧并发只会额外增加墙钟，不能称其为唯一原因。上述绿仍不包含
新journal收集文件与新增core负控，须由根另行按有限小组验证。

根接着实收三个诊断文件共70 passed（pytest10.85秒、守卫11.523秒、100.1MiB、
swap0）：新journal收集25项、controller诊断6项、原failure diagnostics39项，
以本次实际collection为准。加上述165个不同旧节点，当前235个不同节点已绿；
此前41项属于本次70项子集，不另累计。原七个native fixture文件的本次影响回归、
最终静态检查及非作者后审尚待根收取；生产继续冻结，真实helper journal和Native29
远端事务首因仍未验，不因离线收集绿而宣称已取得现场异常位置。

根随后实收原七个native文件的本次影响回归：ci/checkpoint/resource三个文件
673 passed（pytest2.43秒、守卫3.063秒、152.7MiB）；peer/peer-integration/instance/
socket-control四文件166 passed（pytest0.37秒、守卫0.882秒、75.9MiB）。两组839个
不同旧节点，加前述235项，当前合计1074个不同节点本地绿，不再累计其子集。
生产和测试保持冻结，最终静态/格式及非作者审查待根收取，没有重测整组或运行服务。

三脚本类型检查沿既有命令形状，在此worktree为工作目录，经主仓30秒守卫运行：
`env MYPYPATH=src:tests /data0/MuLi/.venv/bin/python -m mypy --strict --no-incremental --explicit-package-bases scripts/check_managed_native_ci.py tests/helpers/managed_native_worker.py tests/helpers/managed_native_peer.py`。
显式源码根与package-bases用于保持原真实import类型和避免helper重复模块发现，
不以修改忽略规则遮盖导入Any；本段仅提供既有检查入口，不提前记本轮类型结果。

根最终实收三脚本严格检查通过（上述MYPYPATH与explicit-package-bases，守卫5.587秒、
288MiB）；此树全部146份源码strict通过（8.221秒、419.2MiB），未把其它树的源码数
套用于本树。全仓Ruff通过（68毫秒、24.3MiB）；根仅整理一份测试的import并格式化
三文件，无语义改变，最终381文件format检查通过（66毫秒、21.3MiB）。
另一位未参与实现的Sol完成全链只读后审，逐项核对可信字段、原预算、cleanup保留和
固定loader，未发现剩余P1/P2；该审查不另计测试节点，也不替代真实journal采集。

本地候选现保持冻结，下一步由根签名保存、推送后，仅针对该精确新head启动一次CI。
尚无Native29远端结果；能否取得真实helper的可信journal字段、安全首异常位置，
以及正式三事务与完整cleanup是否通过，均须按新CI实际产物确认，不预写成功或根因。

### Native29现场与Native30有限拒绝分类候选

根已对精确签名head `3174ae2bbd461ef3eb24d85446f0be87f03e4258` 的
run `40071674265`、managed-native job `120806256473` 取得并严格校验artifact
`12551716614`：1812字节，SHA256
`dc998929db457c291d763893044fd4299f715ae7decdc20fb5411b7d891abd17`。
本条记录时其它job尚未全部终态，不据此判断整轮其它平台结果。现场仍在首adopt的
rev3 activating/stop_base之后进入rev4 rolling_back/stop_target，继而rev5
recovery_required，两次失败均为backend_failed。helper_source已核UID999、PID2831、
invocation `00dff10ef9254ce9bab79133d7575cda`；但controller_failure实际仅为
`{code:not_observed,reason:no_known_event}`，没有取得首异常位置，不能判断是否已进入
systemctl stop。cleanup用户/组未删除、仍有三mount，完整清理仍失败。

静态确认现采集同时在journal查询和最终解析处核四个可信字段；查询先筛掉缺少或
不匹配unit/invocation的行，而解析又将空回包、元数据拒绝、安全payload及timeline
匹配失败折叠为同一no_known_event。因此该结果既不证明logger没有运行，也不证明
真实字段一定归属helper。独立helper的logger未见被Runtime私有logger的NullHandler
过滤；没有现场证据据此调整日志路由或降低身份门。

Native30首片仅替换同一次journal查询的检索预过滤：保留可信_UID、_PID和固定prefix，
移除查询参数中的unit/invocation约束；输出仍为原五字段、最多两行。最终observed
仍逐行要求原四可信字段全部准确、原helper_source、generation/revision/intent和
同一安全diagnostic validator通过，不能把不同拒绝行的身份拼接，也不能从查询命中
推断unit归属。单条严格有效行仍可采信，两个不同有效payload仍拒绝。

not_observed只新增三个固定阶段结果：成功零行的empty_reply、无可采信完整身份的
metadata_rejected、已有完整准确身份但payload/前缀/代次或timeline不符的
payload_rejected；混合行按最远可信阶段归类，不按最后一行覆盖。旧no_known_event
保留artifact回读兼容。当前坏JSON/UTF8、重复键的原行为是跳过该行，未必返回
invalid_reply；本片不借分类改变单有效行加坏行仍可采信的合同，仅坏行时可保留
no_known_event，不冒称是空回包。整包非法或超两行的原invalid_reply门、非零/丢回复
的unavailable及超额output_budget门继续适用。所有分类
仅描述本次有限采集，不能排除PID重用或tail两行挤占，不能宣称已定位日志缺失根因。
不导出元数据实际值、MESSAGE正文、异常文本或路径，不增加probe、重试、启动或
读取次数；原绝对deadline、allowance减一探针、返回先计费、失联不退款、cleanup
同源保留、64行timeline和32KiB总门全部保留。

本次先仅修改原 `tests/test_managed_native_controller_failure.py`：原真实controller
raise/capture生成的有效MESSAGE正控保留，查询替身精确要求仅移除两个检索条件；
新增沿原parent实际采集/保存入口的空回包、缺invocation、坏安全payload三参及
两条拒绝行不得拼接身份的负控，原四身份与五payload篡改断言改为准确拒绝分类。
没有新增未来API或伪造异常诊断。生产未改，作者未运行检查、格式、Git、网络或服务；
这些目标仍待根收取真实红，不预写通过或授权下一远端轮次。

根随后实收精确14个目标节点均红、15 deselected：pytest4.89秒、守卫5.462秒、
70.4MiB、swap0。原九个parser篡改及两行身份拼接负控准确落在no_known_event与
新拒绝分类差异；四个parent采集节点因旧查询仍带四字段AND，触发新的精确query
断言，按原失联回包保守记账耗尽32768字节。这是预期的查询接缝红，不是现场实际
读超额，也不是新增API或导入失败。

授权后的生产窄改仅 `scripts/check_managed_native_ci.py`：原一次query移除unit与
invocation检索参数，原五输出字段与最终四可信字段逐值门不变；typed reader接纳
三个固定新reason，旧no_known_event仍读。空行集合为empty_reply；成功JSON行未过
原完整元数据门为metadata_rejected；通过身份门后前缀、安全payload或既有timeline
失败为payload_rejected，两行取最远可信阶段。原坏JSON/UTF8或重复键继续跳过，
不撤销另一个严格有效行；纯坏行仍可no_known_event，两个冲突的有效payload仍
invalid_reply。没有改变原源码异常、helper绑定、query次数、计费、deadline、
cleanup或artifact预算。源码静态读回后再次冻结；作者未执行检查、格式、Git、网络
或服务，新分类及原相关回归待根验证，仍未取得Native29现场首因。

根随后实收本文件全部29 passed（pytest9.55秒、守卫10.086秒、73.8MiB、swap0）；
原七个native文件839 passed（pytest2.63秒、守卫3.352秒、158.9MiB、swap0），
共868个不同节点本地绿，不重复累计其中14个目标节点。另一位未参与实现的Sol
完成正式只读后审，确认四可信字段及source/timeline/安全validator采信门、原收费、
截止和cleanup未放松，单有效行加坏行、冲突有效行与最远可信阶段语义保持，未发现
剩余P1/P2。三脚本strict随后通过，守卫5.369秒、288.2MiB；两变更Python文件
Ruff通过。格式检查仅发现测试中两处换行，根按原formatter整理后两文件复验通过，
没有语义改动，未因此重复累加测试节点。

本片源码、测试及文档再次冻结；以上离线绿和后审不改变Native29真实失败、未取得
首异常位置与清理未通过的事实。远端Native30尚未启动，只有根完成剩余验证、签名
推送后才可针对精确新head单次运行，当前不预写现场分类、根因或事务修复结果。

Native30随后以精确签名head `48d3821692614a504b48bcd6c20b110272aa264a` 在
run `40073002299` 终态失败：managed-native job `120810303629` 仍失败，另外Linux
3.12与3.13的shard2均在同一真实进程旅程
`test_actual_cli_boot_activation_and_projection_preserve_latest_database[target-rejected-rollback]`
失败。两份固定摘要都处于rev6 rolling_back/stop_target、activation pending、
backend_failed，分别尚余约16.3与14.2秒，不是20秒整体deadline耗尽。

源码与实际有限output确认该回归的直接原因是夹具协议冲突：此测试将driver的stderr
合并到stdout，而原diagnostic_checkpoint只允许固定checkpoint和controller-step行；
Native29新增的安全controller warning进入同一pipe，先于step与stop-target被当作
unexpected checkpoint frame拒绝。Linux3.12日志尾明确已有真实stop-target；3.13
日志截断尾已包含安全诊断与step2，不从截断文本额外推定其完整后续输出。这里的
backend_failed本就是测试预设target拒绝后进入回滚的结果，不能据此改生产停止逻辑。

根用原rollback精确selector本机实收相同真实红：1 failed，pytest6.24秒、守卫6.898秒、
179.4MiB、swap0，诊断仍为rev6 stop_target/backend_failed且剩余14376毫秒。
授权的唯一候选代码改动在 `tests/helpers/managed_recovery_worker.py::controller`
入口，局部为固定muli.management_controller logger添加NullHandler并关闭传播。
只此独立测试driver的可选日志不再混入握手pipe；真实controller仍执行原capture、
首attempt与事务决定，不修改生产logger、parser、服务子角色、20秒、4KiB或三进程门。
原formal验证的是实际launcher/CLI/nonce/Runtime回滚与最新数据保留，安全诊断本身
仍由原专属controller/capture回归验收，不以此测试隔离替代现场日志采集。
作者未运行检查、格式、Git、网络或服务，源码静态读回后冻结，success与rollback
两formal的修后结果待根收取，不预写通过。

本次native artifact则是独立失败：根严格typed校验1813字节、SHA256
`1c3d9a3e6071d781bc1dd438c6d65c47dfa335e2acb0c02cc4786f2c1ec7d0d6`，
controller_failure实际为metadata_rejected，原最终四可信门仍保留；现场首stop_base
仍backend_failed，cleanup仍三mount。该阶段分类不证明具体缺失字段或元数据归属
原因，测试driver隔离不会修复真实native首因或清理，不据此盲重跑同一CI。

根随后签名提交 `48d3821692614a504b48bcd6c20b110272aa264a`（Good）并推送，
只运行一次精确CI [40073002299](https://github.com/69gg/MuLi/actions/runs/40073002299)，
最终failure。原生作业 `120810303629` 的1813字节artifact `12552131810` 已完整经过
当前typed reader和Receipt校验，SHA256为
`1c3d9a3e6071d781bc1dd438c6d65c47dfa335e2acb0c02cc4786f2c1ec7d0d6`。
`controller_failure` 为 `not_observed/metadata_rejected`：成功读取到未满足最终完整
可信元数据门的候选行，不导出其原字段或MESSAGE，仍不能采信原异常位置或断言
哪一字段缺失。此次helper代次 `39713028392a34976c604c79a50869ed`，UID999、PID2642、
invocation `86c5ae9a039a46fdac628783460950e3`；manager ready307毫秒、六环境核对全真。
首adopt仍依次rev3 activating/stop_base、rev4 rolling_back/stop_target/backend_failed、
rev5 recovery_required；helper退出不等于事务成功。清理仍三mount且user/group未移除。

同一CI两个Linux shard2也出现必须诊断的回归：Python3.12作业 `120810303557` 和
3.13作业 `120810303625` 均只有
`test_actual_cli_boot_activation_and_projection_preserve_latest_database[target-rejected-rollback]`
失败，各2751 passed、18 skipped、3270 deselected。有限现场摘要在target被拒后
停于wait:stop-target，rev6 rolling_back/stop_target、backend_failed，尚余16.346及
14.215秒，不是整体deadline耗尽。不能把两个确定失败称为偶发或以延长期限跳过；
下一步先定位该旧真实进程用例的具体故障接缝，不对相同head盲重跑或宣称全CI绿。

根随后对上述测试driver的定向logger隔离候选收取原success及rollback两条formal，
实际2 passed（pytest20.01秒、守卫20.542秒、180.8MiB、swap0）；未改变原生产、
20秒单case、4KiB及三进程限制。本机此时尚未提交，不能把这两条通过当作真实native
metadata_rejected或首stop_base故障已修。

### Native31：同一有限回包内的固定元数据状态提案与先红

原Native30只定位到metadata_rejected，尚不知道哪一trusted字段缺失或不等。
下一片不增加查询、probe、读取行数、deadline或字节额度：只在原两行解析中，
为最终not_observed/metadata_rejected结果附optional `metadata`，包含1至2条
按原回包顺序的固定摘要，每条严格为uid、pid、unit、invocation、shape五键。
四身份状态仅missing/mismatch/matches：原key缺失是missing；存在但类型非字符串
或不等于原可信source是mismatch；原字符串逐值相等是matches。shape为严格bool，
仅说明原整行字段集合与全部值类型门是否通过，避免四身份匹配但MESSAGE缺失被
误解成整行通过。没有任何实际字段值、长度、hash、MESSAGE、路径或异常正文。

摘要只解释拒绝，不参与采信或跨行拼接。最终observed仍要求完整同一行的原四可信
字段、helper_source、generation/revision/intent、安全payload及原timeline门通过；
单有效行加坏行仍可observed，两个冲突有效payload仍拒绝。坏UTF8/JSON/重复键
继续原跳过，不生成摘要，纯坏行仍可no_known_event；只有最终metadata_rejected
可带该字段，payload_rejected或observed不带。旧两键metadata_rejected继续回读。
新reader须核exact结果键/1至2条/exact五键/四有限enum/严格bool，摘要放不下仍
原output_budget或保留原result，不扩大32KiB最终门，也不增加授信材料。

本轮先仅在原controller_failure测试文件增加五个目标节点：真实controller/capture
MESSAGE经原parent采集保存入口，分别删unit、删invocation、改invocation、删MESSAGE，
以及两行各自身份不符但可互补的反例。原单次query与实际raw收费、首失败及timeline
均继续核验，期待只保存固定状态且不晋级observed。生产未改，作者未执行检查、
格式、Git、网络或服务；这些目标仍待根取得真实红，没有新远端轮次授权或根因结论。

根随后实收这五个目标全部真实红：pytest1.89秒、守卫2.415秒、69.7MiB、swap0；
原query、raw收费及保存均已通过，准确缺口是原parser只保code/reason而没有metadata。
授权后的生产仍仅修改 `scripts/check_managed_native_ci.py`，在原两行decode内产生
固定状态摘要，并在原typed reader核上述exact形状；不增加取数或改变最终采信门。
仅最终metadata_rejected附摘要，observed或其它reason仍原形状，旧两键拒绝仍读。

原相关断言仅按精确新摘要同步，没有允许任意extra；另补reader的非法enum、非bool
shape、零/三行、错误reason、行/结果extra拒绝及旧形状兼容，保单有效行加坏JSON的
两个顺序均可observed、两个不同严格有效diagnostic仍invalid_reply。新增摘要不能
成为事务授权或说明具体外部错误。作者未执行检查、格式、Git、网络或服务；本轮
生产、测试及文档静态读回后冻结，修后回归和非作者后审待根收取，不预写现场结果。

根随后实收controller文件45 passed（pytest14.84秒、守卫15.377秒、76MiB、swap0）；
原七个native文件839 passed（pytest2.59秒、守卫3.233秒、159.1MiB、swap0）。加上
前述测试driver隔离的两条formal，共886个不同节点本地绿，不重复累计其中五个先红
目标。三脚本严格类型检查通过，守卫5.332秒、287.4MiB、swap0；变更的三个Python
文件Ruff通过。根只整理script三处纯格式，再对三个文件复验format通过，没有语义
改动，也未因此重复累计测试。D完成正式非作者只读后审，未发现剩余P1/P2。

本片再次冻结。以上离线结果不改变Native30现场metadata_rejected、原事务与cleanup
仍失败且首因未得的事实；Native31精确新head尚未签名、尚未运行CI，现场结果待验。

### Native32：五个消费字段与JSON格式化阈值的目标回归

Native31随后在精确head `8f7e8a7` 的CI `40074892152` 终态：十个通用作业全绿，
两Linux shard的测试driver握手回归已恢复；唯一managed-native作业 `120816031280`
仍失败。根严格校验artifact `12552404045`，1840字节，SHA256
`deb78abf5e2166bfade9ae04357eb05557f1f0f6e386ac2066b9a7b439ccc57c`。
controller_failure仍为not_observed/metadata_rejected，但唯一摘要行四个可信身份
uid/pid/unit/invocation全部matches，shape=false。当前仍首stop_base失败后进入
recovery_required，cleanup保留三mount；未取得首异常位置，不能说事务或清理修好。

此结果只把拒绝定位到整行shape，不证明现场是额外metadata、MESSAGE缺失或非字符串。
当前reader把journalctl整行当作自有exact schema，并要求未消费的附带值也是字符串；
这超出了真正证据的五个字段。拟仅要求五个消费字段存在且为字符串，保留四可信身份
逐值相等、MESSAGE固定前缀、安全validator、同代revision/intent及timeline全部原门。
其它formatter字段不参与授权、不进入artifact；原重复键、两行、raw收费与总预算不变。

根已核官方[journalctl JSON输出说明](https://freedesktop.org/software/systemd/man/devel/journalctl.html)：
单值超过4096字节时默认可输出null，`--all`保留完整值。项目validator限制diagnostic
本体不超过4096字节，并未把其外层generation/revision/intent envelope及固定前缀算进
这一上限，因而合法MESSAGE仍可超过该阈值。拟在原唯一查询加`--all`，不是额外查询
或额外额度；实际完整JSON仍计原剩余32KiB，原deadline、最多两行和失联不退款保留。

本轮先仅新增原controller_failure文件的两条目标回归：第一沿真实controller/capture
产生的安全消息，经原parent采集保存入口，附加不消费的JSON metadata，期待同一安全
诊断且不导出额外值；第二保留原真实代次/timeline，明确构造经原validator认可、本体
仍在4096内而完整MESSAGE超过4096的有限诊断，查询替身严格模拟默认null及`--all`
完整回包，核同一次查询、实际wire收费、同deadline和原保存总门。后者是格式化边界
回归，不声称该合成帧列表是真实现场trace，也不推定Native31现场就发生了截值。
生产未改，作者未运行检查、格式、Git、网络或真实服务；两条目标待根收取真实红，
未授权新远端轮次，不预写修复通过。

根随后实收上述两条目标均真实红：pytest0.87秒、守卫1.423秒、69.6MiB、swap0。
原expected observed、diagnostic本体不超过4096而完整MESSAGE超过4096的前置，以及
原once与实际wire收费均通过，分别准确落在额外formatter metadata误拒与缺`--all`。
授权后的唯一生产改动在 `scripts/check_managed_native_ci.py`：shape只核五个消费字段
存在且为字符串，未消费的formatter键不再参与shape且不导出；原四可信值逐项相等、
prefix/envelope/安全validator/timeline与重复键门不变。原同一次查询增加`--all`，
没有新增查询、probe、重试或额度，raw上限与绝对deadline不变。

生产、测试和文档静态读回后冻结；作者未运行检查、格式、Git、网络或真实服务。
根修后实收原文件47项全部通过，pytest15.50秒、守卫16.045秒、75.6MiB、swap0；
相关原生七文件839项通过，pytest2.61秒、守卫3.262秒、159.1MiB、swap0。
本片合886个不同本地节点，未将Native31两条未重跑的真实CLI旅程累计进来。
三个原生脚本strict通过，守卫5.317秒、288.4MiB；Ruff通过。格式检查仅发现新增
测试的一个列表推导换行，由根沿原formatter整理，无语义变化。根非作者读回生产差异
与两条目标测试，原四身份、payload、timeline、重复键、实际raw收费及deadline门保持，
未发现剩余P1/P2；未重跑全应用源码strict，因为本片无应用源码改动。
Native31的shape=false现场分叉仍未知，此修复不证明已取得首stop_base异常位置，
也不改变真实事务与cleanup失败状态；新的精确远端结果另行补录。

### Native33：仅区分已读GetUnit非零回包的受控raise位置

Native32的精确CI `40076165153` 已取得首异常位置，managed-native作业
`120819856934` 仍失败。根严格校验artifact `12552992110`，2032字节，SHA256
`5215a0b9daafe8d384ab5b8278587098713aae184c25c75a7dd326ce71409748`。
controller_failure为observed，generation `034ee2e861820077ccd665550391c04d`、revision3、
intent stop_base；安全帧依次定位到 `_Controller._call`、`ManagedLinuxBackend.observe`、
`_probe`、`ManagedSystemdProbe.read_managed`、`SystemdProbe._unit_path`、`_command`。
最后一处是busctl GetUnit非零回包的原raise，证明失败的那一次step未进入其后面的
systemctl stop调用；不能证明此前同一revision的step没有成功stop。环境六项
仍为true；事务后续recovery_required，cleanup仍保留三mount，未完成。此处不预写该CI
其它作业终态，也不能倒推Native31究竟因哪一种JSON形状被拒。

现 `run_process` 已返回exit_code/output/stderr/truncated；probe使用merge_stderr=False，
stdout与stderr仍共用原单次OutputBudget，signal由子进程returncode负数表示，无需再读
日志或增加外部调用。v255官方
[Manager.GetUnit实现](https://github.com/systemd/systemd/blob/v255/src/core/dbus-manager.c#L449-L451)
对未找到的名称设置BUS_ERROR_NO_SUCH_UNIT，文案为`Unit %s not loaded.`；
[busctl call实现](https://github.com/systemd/systemd/blob/v255/src/busctl/busctl.c#L1901-L1903)
在sd_bus_call失败时添加`Call failed: `。本片拟仅在原消费接缝，把固定GetUnit
`muli.service`且完整stderr严格等于`Call failed: Unit muli.service not loaded.\n`的
形状、严格负整数exit_code、其它非零三类放到不同受控raise行，仍抛相同
ObservationError("systemd_observation_unavailable")。不做strip、子串、语言或ANSI
猜测；其它目标、前后缀、多行、非字符串等均不能被认作该固定NoSuchUnit形状。
这是受控CLI文本形状分类，不把stderr升级为认证过的D-Bus错误名。

原安全帧可直接区分这三个源码位置，不新增Code、持久schema、日志字段或raw错误；
controller仍作相同backend_failed与rollback，首次诊断、四可信身份、timeline、安全
validator、原deadline、输出额度与原查询数均不变。未完成回包的异常继续原路径，
不猜测exit_code。没有额外probe、重试或真实服务操作。

现有正控已缩小窗口：同一helper每轮先用同一个observed probe/runner成功核自身
GetUnit及进程身份；controller在draining轮必须成功read_managed且观察到drained，才
写下一版activating/stop_base。现场revision3因此证明此前同一服务已被成功观察，
不能解释为本轮从未加载过服务，或整个helper期间用户bus始终不可用；但仍未证明
下一轮非零是NoSuchUnit、signal、认证错误或其它，也不证明特定环境变量或unit退出
是根因。新目标只为下一现场作这一有限分叉，不据此修改环境或停止逻辑。

先仅在 `tests/test_managed_controller_failure_diagnostic.py` 增加一个目标节点，复用
真实Host/Journal/ManagedTransactionController，并将probe换为正式ManagedSystemdProbe；
仅其进程runner返回上述三类正式回包，精确核同一个busctl GetUnit参数。每类都应只
调用一次、未进入systemctl stop、保同一backend_failed/rollback/deadline与原安全
payload且不导出stderr；最后要求原SystemdProbe._command安全帧有三个不同正行号，
不硬编码未来源码行。当前三类都落同一raise，预期目标红在此行为缺口，非缺API或
schema。生产未改；作者未运行检查、格式、Git、网络CLI或真实服务，待根取得实红后
才实施，不授权新远端轮次。

根补齐Native32终态：十个通用作业全绿，managed-native失败，不能把本片诊断采集
成功称为正式事务或cleanup已修复。Native33上述目标已实收真实红：1 failed，
pytest0.60秒、守卫1.134秒、63.6MiB、swap0；三个回包均已走真实controller/probe，
原决策与trace前置全部通过，末尾三个位置仍同为原285行，准确缺少有限分类。

授权后只改 `src/muli/platforms/linux_transaction.py` 原非零回包消费分支：严格int
负值先走signal位置；只有严格正int、固定GetUnit参数、truncated严格False、stderr
严格str且全等官方固定文案才走NoSuchUnit形状位置；其它全部保原位置。三个位置
均抛同样的ObservationError和同一固定内容，不修改外部命令、环境、回滚或采集协议。
固定目标使用原UNIT常量，未新增任意可配置unit或通用错误匹配设施。

原目标不变；同一测试文件另补三组有限负控，分别核近似stderr（无换行、空白、ANSI、
额外行、其它unit）、非固定调用（其它unit、LoadUnit、非call）、不完整/非正式回包
（截断标记为True/缺失/0、bytes stderr、bool或字符串exit_code）。它们必须落回
同一个other受控位置，不被晋级；比较安全frame的正行号而不硬编码未来行数，原
异常类型及固定内容仍相同。作者未运行检查、格式、Git、网络或真实服务，生产、
测试与本节静态读回后冻结，修后结果待根收取；尚无Native33现场结果或根因结论。

Native33本地修后完整诊断文件10 passed（pytest2.91秒、守卫3.463秒、66.1MiB）；
原Linux transaction、managed probe、controller三文件109 passed（pytest2.98秒、
守卫3.564秒、67.8MiB），合119个不同节点。146个应用源码strict、no-incremental
mypy通过（8.466秒、414.2MiB）；两改动Python文件Ruff及格式通过，全部swap0。
非作者独审未发现明确P1/P2。未把Native32未重跑的886节点算入该片，也未操作本机服务。

Native33现场随后在精确head `9bc9e18661f6610798620f3f848027ce69e77b3c` 的CI
`40077754116` 取得分类，managed-native作业 `120824708334` 仍失败。根严格校验
artifact `12553278523`，2031字节，SHA256
`e90eb30a96268aaf22d500351ff354152e2eaf31fa9379ae9aab1a0509cabc0b`。
controller_failure仍为observed，generation `8fa7d7dc4c24f2f7af2b9e73d5c8c569`、
revision3、stop_base，精确源码_command第298行：固定GetUnit(muli.service)、正式
未截断回包的完整NoSuchUnit文案形状，非signal或other。可信helper PID2629、UID999、
invocation `8dc69120eb8243a9a724f5036c229ddb`，环境六项true、manager ready274ms；
时间线draining13806ms、stop_base19089ms、rolling_back23032ms、recovery_required24075ms。
真实事务与cleanup仍失败，未从这个文本形状推导出已认证的D-Bus错误名。

因果纠偏：上述时间线只记录generation/revision位置，不记录每一次step。
`_Controller._step`在stop命令成功后仍返回原stop_base record，`drive_loop`看到record
未变才sleep再执行下一step；因此先成功stop、inactive unit被GC、同revision下一次
GetUnit报未加载，完全符合现有证据。此前“观察先于stop”的结论只能限于失败的那次
step，不能排除前次同revision的真实停止效果。没有证据应继续修改fixture环境。

### Native34：合法inactive unit的正式观察（先目标回归）

Context7本次resolve已实际返回monthly quota reached，未重复请求。固定v255源码：
[unit_may_gc](https://github.com/systemd/systemd/blob/v255/src/core/unit.c#L401-L467)
允许无作业、无bus引用且无活进程的inactive unit被卸载；
[GetUnit与LoadUnit](https://github.com/systemd/systemd/blob/v255/src/core/dbus-manager.c#L424-L467)
区分“只找已loaded”与“必要时加载描述”，二者不等于StartUnit。原probe每个RPC均用
短命busctl进程，单独LoadUnit后再读不能保证对象一直驻留。v255的
[Unit/Service对象fallback](https://github.com/systemd/systemd/blob/v255/src/core/dbus.c#L253-L315)
对规范对象路径的每次属性请求调用manager_load_unit_from_dbus_path；描述可在各次
RPC内重新加载，无须根据NoSuchUnit虚构stopped快照。

根选择更完整且更简单的范围：仅 `ManagedSystemdProbe.read_managed` 对原固定UNIT
统一按规范对象Properties路径读取，不设phase白名单；通用SystemdProbe.read和helper
继续原GetUnit。这覆盖stop后的观察以及install后start_target、restore后start_base的
合法inactive窗口，减少一次RPC而不增加loop。规范路径从原UNIT按官方字节escape规则
派生，依据固定v255的
[sd_bus_path_encode规则](https://github.com/systemd/systemd/blob/v255/man/sd_bus_path_encode.xml#L62-L90)
把非对象标识字节编码为下划线加十六进制，不接受任意单位，不新增注册框架、依赖或
错误fallback。原Id/LoadState/fragment/
argv/dropins、磁盘双读、needs_reload和各phase判断、PID/job、writerlease、cgroup空门
全保留；missing、failed load、外来材料绝不能直接当停止或获得安装权限。

先写一个真实二步目标：真实Host/Journal/controller/backend和正式ManagedSystemdProbe，
仅外部busctl/systemctl回包与proc文件由临时fixture提供。第一step成功观察running并
执行一次正式effects.stop，返回同一stop_base revision；替身随后模拟inactive对象GC：
GetUnit未加载，但规范对象Properties可加载合法磁盘描述并返回inactive/PID0/job0。
第二step应进入install_target且总共只stop一次；旧实现应准确在第二step GetUnit失败，
进入rolling_back。不能以未来API/import为红，不能绕真实writerlease或磁盘材料。
作者未改生产、未执行检查/格式/Git/网络CLI或真实服务；目标待根取得实红后再实施。

准确目标为
`tests/test_managed_stopped_unit_observation.py::test_successful_stop_then_collected_unit_advances_same_revision`。
它复用既有Host的真实磁盘快照、flock和writerlease；临时proc取原Host进程身份构造，
bus替身复用现FakeBus合法属性，而非替换read_managed返回值。第一步正式stop命令
缝隙调用原Host.command释放实际writerlease后才模拟GC，准确保留同一revision；第二步
必须通过原snapshot验证并进入install_target，不能只是省略GetUnit而直接制造停止。
本节点静态读回后冻结，尚未执行；原生产文件未改，修后负控在根收红后继续补。

根随后实收此节点准确真红：1 failed，pytest0.59秒、守卫1.189秒、64.2MiB、swap0。
第一step的正式controller/effects.stop已成功返回同revision3，实际writerlease释放、
只stop一次等前置均通过；第二step在GetUnit严格NoSuchUnit原298行失败，转rolling_back
而未进入install_target。该回归证实本仓合法停止后的观察缺口；并不把fake GC回包称为
对Native33远端每个隐藏step的直接观测。

授权后的唯一生产修改为 `ManagedSystemdProbe.read_managed` 的路径入口：从原UNIT
以有限ASCII byte escape得到规范对象路径，直接执行原_snapshot属性查询，保留原
所有材料、状态和双读判断。通用read、helper、原33非零回包分类均未改；没有添加
LoadUnit、StartUnit、额外查询、等待或重试，不能把属性可读等同于进程健康。
新测试同文件补坏loaded ExecStart、实际populated cgroup、实际writerlease busy三条
负控，必须无安装/启动命令、磁盘unit不变，分别保持拒绝或原停止意图等待。

旧Native33分类测试原先假设managed probe必经GetUnit，该入口已被正当替换，不能
为它加回生产backdoor。该测试迁到通用SystemdProbe.read的真实GetUnit路径，继续
严格核三个正式回包、固定argv、原ObservationError与安全帧三位置、不泄stderr。
它现只证明通用读取的有限分类，不再冒称这三类经managed controller观察；此前
真实controller/effects.stop诊断原测试完全保留，新二步旅程承担managed生命周期。
生产、两个测试文件与文档静态读回后冻结；作者未运行检查/格式/Git/网络或真实服务，
修后回归与非作者后审待根收取，没有Native34远端结果，不预写事务或cleanup恢复。

根随后收取Native33远端终态：十个通用作业全部通过，managed-native仍失败，首异常
分类成功不代表事务或cleanup成功。此为上一片结果，不计入Native34本地覆盖。

Native34修后两文件共14 passed（pytest3.48秒、守卫4.011秒、68.3MiB），含原二步
目标和三个安全负控。原Linux transaction、managed probe、controller共109 passed
（pytest2.81秒、守卫3.366秒、65.9MiB）；另选原backend完整切换、写入前停止与所有权
门、外来进程和不可重试启动四组13 passed、21 deselected（pytest12.32秒、守卫
12.965秒、66.9MiB），没有把未选节点称为通过。本片合136个不同节点。
146源码strict、no-incremental mypy通过（8.396秒、414.3MiB）；改动Python Ruff通过，
仅一新测试文件纯格式整理。非作者独审未发现剩余P1/P2，确认安全门和原通用GetUnit
诊断仍有效。全部root串行守卫、swap0，无资源上限修改或真实本机服务操作；下一
精确隔离CI仍待运行，未预写事务或cleanup已恢复。

### Native35：官方空InvocationID的停止态观察（先目标回归）

Native34精确签名head `59495171c4372fff6308d7cac925a9b0006b84a3` 的CI
`40078985786`，managed-native job `120828380913` 再次失败。根已严格校验
artifact `12553384920`（2025字节，SHA-256
`e74d42221207be6af4d9f66db8404441726b84b6ea032f82a814a5dd5e0038f1`）。
本次安全帧已推进到 `ManagedSystemdProbe.read_managed` →
`SystemdProbe._snapshot` 的 `invalid_invocation`，仍在revision3的stop_base；
不再是GetUnit失败。其它作业此刻尚未全终态，正式事务及cleanup仍未恢复。

本次Context7 resolve实际返回monthly quota reached，随后核对固定v255 primary：
[InvocationID属性注册](https://github.com/systemd/systemd/blob/v255/src/core/dbus-unit.c#L876)
直接使用 `bus_property_get_id128`；该
[getter](https://github.com/systemd/systemd/blob/v255/src/shared/bus-get-properties.c#L52-L65)
对零ID返回空 `ay`，非零ID才返回16字节。原停止态fixture使用16个零字节，未覆盖
官方空数组编码；现场的invalid_invocation本身仍不是导出原始属性值的证据。

先仅修改原二步真实controller/probe回归的停止后属性为 `InvocationID=("ay", [])`。
首step仍须真实stop成功、返回同revision并释放实际writerlease；第二step仍须通过原
材料/双读/cgroup门而进入install_target，且总共只stop一次。目标仍为
`tests/test_managed_stopped_unit_observation.py::test_successful_stop_then_collected_unit_advances_same_revision`。
生产保持不变，待根先收准确目标红。拟定最小规则仅对真正inactive/failed、PID0、
job0的空ID使用内部原32零hex表示absence；不放宽其它长度、类型或非法bytes，不给
running、helper、health凭空创造invocation身份，不新增schema、RPC或时间预算。

根随后实收该目标准确红：1 failed，pytest0.52秒、守卫1.047秒、65.1MiB、swap0。
首step真实stop、writerlease释放和同revision均通过；第二step进入rolling_back而非
install_target，安全帧为原_snapshot第324行，与34现场位置一致，但不据此倒推现场
原值已被观察为 `[]`。

获准后的唯一生产修改在原 `SystemdProbe._snapshot`：原严格Job/MainPID解析提前
复用；空list只在inactive/failed、严格整数PID0和严格Job `(0, "/")` 时归一为原有
32零hex。非空仍必须恰16个严格整数byte；bool、负byte、超255、其它长度和bytes对象
均拒绝。它只表达absence，不证明cgroup空或writerlease可用；原材料、磁盘/进程双读、
phase、cgroup和lease门不变。running服务的空ID仍被snapshot拒绝；inactive helper
即便得到absence，也被原read_helper运行布局门拒绝，不能成为健康或boot身份。

保留原真实二步formal和三个停止安全负控；补通用read的inactive/failed空ID正控、
active/activating/PID非零/job非零拒绝，解码后snapshot边界的非法类型/byte/长度拒绝，
以及原helper、managed健康入口的无ID拒绝。bytes对象仅用于解码后纯边界，未宣称
它能由JSON线协议产生。生产与测试只静态读回，作者未执行检查/格式/Git/真实服务；
修后结果待根收取和非作者独审。

Native34最终除managed-native外，Linux3.12和3.13 shard1也各有两个失败，均为
`test_managed_bootstrap.py::test_fresh_actual_service_identity_precedes_configuration_and_sqlite`
的旧绝对RPC次数 `4 == 5` 断言。3.12实收3363 passed、15 skipped、两fail，161.12秒；
3.13实收3363 passed、15 skipped、两fail，240.76秒；余八个作业通过，不称十通用
全绿。根独立将该fixture改为准确核两遍Unit/Service固定path属性顺序，保入场前
writerlease、配置和SQLite原断言：两节点先红2 failed（pytest0.20秒、守卫765毫秒、
68.1MiB），窄修后2 passed（pytest0.22秒、守卫850毫秒、64.2MiB），均swap0。
该更改不涉及生产，本片同行提交；原34远端失败历史保留。

Native35根随后实收精准六个selectors共19 passed（pytest0.78秒、守卫1.303秒、
66.5MiB）；七整文件stopped observation、Linux transaction、helper、managed probe、
bootstrap、managed controller和controller failure diagnostic共214个不同节点通过
（pytest6.82秒、守卫7.361秒、87.2MiB）。前述19项和bootstrap两项均在214项内，
不重复累计。整合首命令误写一个不存在的测试文件，0项、exit4；未改生产，按实际
文件名更正后才取得上述整组结果。146源码clean strict通过（8.041秒、413.2MiB）；
全Ruff通过（64毫秒、21.8MiB），382文件格式检查通过（65毫秒、22.1MiB）。所有
实际检查由根串行守卫完成、swap0；作者未自行执行。

非作者A独立审查生产、18个新增参数节点、原formal及根的bootstrap断言修订，未见
剩余P1/P2；根也完成整合审查。当前仅本地候选闭合，尚未提交或取得Native35远端
结果，不称正式事务恢复；已知cleanup所有权P2仍未修，Native34现场原始InvocationID
值仍未导出，官方空数组规则与离线反例不代替该现场原值证据。源码和测试继续冻结。

### Native36：隔离helper只观测成本（先短合同与目标回归）

Native35的CI `40079919254` 最终completed/failure：十个通用作业全success，
仅managed-native job `120831141125` 失败。根严格校验artifact `12555011711`
（1903字节，SHA-256 `e6580c4f1fa882abd5ede1e80e70644ac76e17bd4c8f74455460724f19c3ff2e`）。
本轮已真实通过adopt：origin ready5800毫秒、prepared7446、draining14891、stop_base
20486、install_target25559、start_target38898、verifying48924、committed51122、
released57336、新boot精确projection58751，随后prepare switch58752、Case60001
TimeoutError。真实target PID2853、新boot `d5833f80dbe8439797b40cf4b1df4806`；
不再是_snapshot失败，但不能把adopt成功称三次事务全部完成。controller failure此时
not_observed/unavailable，cleanup仍三mount、用户/组未删除，不能称清理成功。

先不凭上述阶段差值归因，也不做碎片微优化。仅沿既有复制runtime的fixture.pth/
`_muli_native_ci_hook`增加独立helper观测分支，与serve工厂hook分离：严格匹配
`sys.orig_argv` 的私有a或b解释器、`-I -m muli.management_helper`及固定本root journal；
复用真实fixture UID/root、journal generation和派生helper名，绑定本进程PID及非零
INVOCATION_ID。不修改生产包文件、controller决策、任何proof/guard、60秒及原资源门。

只包原 `ManagedLinuxBackend._check`、`SystemdProbe._command`、
`ManagedLinuxBackend._request` 三个函数，分别计checks/bus/http的count和累计total_ms；
前者同步核包/anchor，后两者为原async命令及HTTP proof等待。调用原函数并原样返回
或重新传播原异常，perf_counter仅在finally累加，统计失败不得改变原控制流。三类
没有互相嵌套调用，不另计整observe，避免把包含成本相加。它们不是整个事务的完整
耗时分解，也不能从某类等待时间推断外部命令的CPU耗时。

进程内累加，atexit最多一次向本fixture根固定 `helper-costs.json` 原子写入私有有界
JSON；每次函数调用不写文件。固定exact shape为version1、code=observed、generation、
helper、uid、pid、invocation，以及checks/bus/http各exact `{count,total_ms}`，数值
严格非bool非负64位整数，无argv/路径/message/环境/正文。文件写入不计入这三类
函数耗时，仍消耗原真实整体期限；没有新RPC、模型、poll、重试或额外额度。被杀、
尚未退出、写入失败或文件/身份无效均无可用数据，不能冒充三项零值。

root原collect接缝只接受严格类型/字节门并与本次独立已核helper_source精确匹配的
统计（generation/helper/UID/PID/invocation）；旧文件、任意同名JSON不得给出observed。
加入resources.helper_costs仍须原最终32KiB门；旧结果缺字段仍兼容，不补假零。
统计不参与任何效果授权。当前先写一个小目标：实际NativeCI.prepare生成的hook在
受控helper orig_argv下装载，复用真实临时Host/Journal及原backend._check，确认一次
实际hash成功、一次真实stale_revision拒绝都保持原语义；显式触发截获的atexit回调
才出现准确计数文件。无未来API/import前置红，不宣称真实-I进程、systemd或现场
性能已验证。目标为
`tests/test_managed_native_helper_costs.py::test_generated_helper_hook_measures_real_checks_without_changing_their_result`。
根已实收该准确目标1 failed（pytest0.38秒、守卫1.033秒、65.4MiB、swap0）：
实际prepare生成hook、真实_check成功与stale_revision拒绝前置均通过，唯一失败为
atexit callbacks实际0，缺少观测注册。随后根授权fixture两脚本最小实现，不改生产包。

候选现已接入独立helper分支：初始化全部在局部受控try内，失败不阻止原helper；
三函数的返回、异常和取消均原样转发，计时自身失败只撤统计，atexit先标已尝试、
最多一次写入固定私有原子文件。原serve工厂分支保持不变。因为volume已交新UID且
0700，runner不另起sudo读统计：原同UID worker在drive成功或异常结算时一次读取
NOFOLLOW普通文件，核owner、mode、nlink、字节及前后文件身份，与原helper_source
五项逐值匹配，沿原result.json的单次cat导出；仍受原execute绝对截止，不新建期限。
父fixture_result复用同一严格统计validator并再核helper_source及本轮history。
不存在、未退出、被强杀、坏JSON/重复键、身份或文件漂移都不能输出假零。
加入统计若使原最终投影超过32KiB，仅撤统计为固定not_observed/output_budget；
连该短标记也放不下则省略可选字段，原其它proof完整保留、旧缺字段结果仍可读。
policy回归另覆盖身份五项不符、布尔/负值/额外字段/超64位/缺项、缺文件/软链/
公开权限/超额/坏JSON、原32KiB临界不裁proof及初始化拒绝保持原函数。

根串行实收新增20项首组19 passed/1夹具失败：预算夹具的64行固定填充实际仍能容下
完整统计，没有达到所声称的字节边界。仅将夹具改为在原合法字段/100字符上限内
填充两列，并先断言实际字节贴近原32768上限；生产预算与裁剪逻辑不改。
新20项随后全通过（pytest0.33秒、守卫0.884秒、67.5MiB、swap0），与九组既有
native fixture回归合计916个不同节点通过（pytest18.26秒、守卫18.925秒、
168.7MiB、swap0）。Ruff发现的常量setattr及fixture导入整理后，新20项再过；
全Ruff与383文件格式通过。生产包未改，不重复跑上一片已过的146源码strict。
非作者及根后审未发现剩余P1/P2；没有本机真实systemd、用户或桌面效果。
精确签名候选的远端native现场及统计仍待收取，不预写性能归因或整套成功。
修后只静态读回，待根目标转绿、相关整合、strict和非作者审；作者未执行检查、
格式、Git、网络或真实服务，不预写现场性能结果。

已知cleanup P2另行保留，不在36实施。现有fixture hook也可在未来仅观察
ConfigurationFile.replace的真实返回snapshot，继承初次O_EXCL的当前unit birth；
confirm不能领养，丢回包或凭据写失败必须保留fail。此可能路径无需改生产源码，
但其发布/清理负控尚未实施，本片统计不能冒称清理所有权已经解决。

### Native37：纠正成本hook的正式V2启动准入（目标先红）

Native36 `40082042192` 的精确候选 `7a3d5a7`，managed-native job
`120837419796` 失败；根严格核验artifact `12555253653`（1919字节，SHA-256
`371b259c9522ab9ee220e839417e05c5b8d8db44c11ce9b7dee85b193a54d636`）。
helper_costs为not_observed/unavailable，没有成本数据。adopt仍已committed49294毫秒、
released55330、精确projection56604（PID2779、新boot
`47da42b121e24941995e38a5664b37f8`），prepare switch56605后于59636毫秒Timeout；
cleanup仍三mount、用户与组未删除，不称三次事务或清理成功。

源码确定的观测接缝错误：正式V2的 `managed_helper_arguments` 返回
`-I -m muli.managed_host --root <本journal> run`，而生成hook及其初始化器只接受
旧V1的 `muli.management_helper`。真实V2不会注册统计回调。旧离线目标手拼同一V1
argv，未覆盖真正被测入口；已有20项绿不能证明V2统计已可用，也不能据空数据归因主成本。

目标改为消费真实Host的anchor/controller，经正式 `managed_helper_arguments` 生成
完整orig_argv，进入实际prepare产生的hook文本，再保持原真实_check成功、stale_revision
拒绝、一次atexit注册/写入及五项身份断言：
`tests/test_managed_native_helper_costs.py::test_generated_managed_helper_hook_measures_real_checks_without_changing_their_result`。
根已实收准确目标首红：1 failed（pytest0.31秒、守卫0.856秒、65.8MiB、swap0）；
正式argv、实际prepare生成hook及两次真实_check前置均通过，唯一失败为callbacks0。
据此只修改两fixture脚本：生成hook保留原V1/serve分支，另分派V2模块；初始化器仅增
本私有解释器、固定journal、准确 `--root ... run` 的完整argv形状，后续fixture/root/
record/五项identity及32KiB门均不变。四项小负控要求错root、recover、缺run及多参数
在访问fixture前拒绝，不能注册回调或替换原方法。生产包、原期限及资源门不改。
根随后核Native36为completed/failure，十项通用作业全success，仅managed-native失败；
保留上述空统计，不把一般CI绿误算作现场成功。37新24项全通过（pytest0.37秒、守卫
0.932秒、74.5MiB、swap0）；合既有coordinator文件共581个不同节点通过（pytest
2.29秒、守卫2.950秒、142.1MiB、swap0），不与36的916重复累计。全Ruff、383文件
格式及非作者后审通过，无剩余P1/P2。仅fixture改动，生产strict沿前片已过结果，未重复。
37真实候选 `c5b5a5c` 的 CI `40082934586`，managed-native job `120840018838`
失败。根安全核验artifact `12556325393`：ZIP 2093字节，SHA-256
`78f93d99d544577fc5659c841a8cb4a26f0a18ea2e5371c2801ff9532114d1a6`。
adopt于44730毫秒released、45679毫秒helper collected、45697毫秒精确projection；
switch于48468毫秒prepared、54263毫秒draining、58655毫秒stop_base，60000毫秒Timeout。
成本回执为not_observed/identity_changed；当前helper_source已是第二代
`4f5c88d76faff4799d8a6aef1ccec813`、PID2833、UID999、invocation
`52bb85ef8d16483086cbaa0c44cb0156`。没有可采信的成本指标，不据此归因主成本，
也不称完整事务、回退或cleanup通过。

### Native38：helper退出后立即保留本代成本（已取得现场成本，完整验收未通过）

源码显示成本文件仅在helper的atexit回调中替换，而原collector只在整个case最终一次
读取；第二代开始时当前helper_source已更新，第一代完成时没有采集。这能解释为何
最终可能拿第一代文件与第二代身份比较，但现场只有整体identity_changed，未导出文件
内的generation，故“确为第一代旧文件”仍是代码推导，不能冒称已观测事实。

本候选只在transaction resolved分支原helper_empty返回True后、下一代prepare前，
复用当前已核五键source，一次读取固定成本文件，先存入Case私有内存缓存；此时不向主结果
增加或修改字段，不能抢占后续transaction note空间并改变原首因。只在原finally采集位置，
将缓存按最终完整JSON的实际余额合入新可选resources.helper_cost_history。
缓存与最终history均
至多3项，每项严格为`{source,costs}`：source沿原五键身份校验，generation/helper须命中
原generations，costs沿原validator并必须observed；generation不可重复。只有本次已核
completed_generation与source一致才保留，缺源或漂移不从文件反推身份。当前helper_source、
controller_failure及原finally当前采集保持，不为历史成本回绑当前代。旧artifact无新字段仍
接受；新history的额外字段、坏类型、重复代次、未观测伪指标或外代均拒绝。

新增本地读取不增加RPC、轮询、等待或截止时间；原每次读取、原32KiB最终JSON及首因
保持。候选完整JSON（包括当前成本与新增history）先过原上限才保留新项，不够保留原历史，
仅沿旧output_budget缺证路径，不裁切身份或主结果。没有改生产_check、性能路径、cleanup
所有权或hook安装；清理独立候选另行审查，不把本片作为恢复成功证据。

独立成本fixture补了第一代完成保留→第二代在途旧文件拒绝但历史不丢→第二代完整退出
追加、重复采集不覆盖、缺源/错PID/错invocation/错完成代次、严格artifact反例与原32KiB
临界结果不变；还覆盖已到期不再打开文件、仍可合入此前完成代缓存。建议根运行
`tests/test_managed_native_helper_costs.py`及既有coordinator组；
作者未自行运行检查；根已实收成本文件38项通过（pytest0.79秒，守卫1.375秒、111.5MiB、swap0），
该本地阶段尚未取得真实helper成本；随后远端现场见下节，不由离线通过推定整套成功。

### 独立候选：fixture 的 app unit 出生凭据与清理

已知cleanup缺口独立处理：`Case.drive` 在私有home创建的 `muli.service` 没有出生凭据，
bootstrap清理不能仅凭文件内容、相同UID或存在而删除；前述远端三mount与用户/组未删除
仍是未完成清理，不能由成本统计或adopt已release推成成功。

本候选不改生产包。新增固定fixture模块 `tests/helpers/managed_native_unit.py`，由原prepare
复制进隔离volume；Case初次只用O_EXCL创建固定app叶，持久记录root/父目录与叶的
设备号、inode、大小、mtime/ctime和摘要。原V2 helper的精确orig_argv及私有fixture核验后，
生成hook才安装观察器；它仅包该固定路径的真正 `ConfigurationFile.replace`，原参数、guard、
返回值与异常不变。旧birth精确匹配且原replace成功返回新snapshot，才用原有界CAS继承当前
身份。confirm、外部换代及失败不能补领养；外来同UID同内容文件也不能重新成为owned。

仅fixture根另有固定O_EXCL pending护栏，先持久建立，再执行出生或被观察的CAS；新birth
真正持久返回后才清护栏。这覆盖receipt rename后fsync失败仍可见新JSON的窗口：取消、
强杀或任意未确认写入留下护栏，后续清理拒绝，不能从可见内容猜成功。观察器自身失败
不替换原unit操作的结果；护栏不在产品schema、原Receipt或artifact内，也不作为授权来源。

分离cleanup进程沿原helper/app stop、PID/job/cgroup门，额外持有真实journal controller锁与
writer lease，再以固定路径dirfd、NOFOLLOW、父目录及当前birth双核删除唯一app叶并fsync。
未loaded的unit沿原成功exact列表absence门，不将初始化空串冒称已观察到cgroup为空；不新增RPC。
任目录/叶/symlink换代、失效凭据、pending或未获真实journal/lease均保留失败；不删除任意
邻接文件、不递归清目录。成功unlink丢回包可在同一父目录识别已缺失；不重建、不领养。
没有journal却已留下app叶的早期失败仍保留，候选不承诺该类cleanup一定成功。

专用离线回归 `tests/test_managed_native_unit_birth.py` 的四组入口：真实bootstrap/Case.drive
到首manager屏障的O_EXCL出生；prepare生成正式V2 hook→真实CAS连续替换→真实Case.cleanup
与journal/lease删除；同内容新inode、symlink、父目录换代拒删；真正发布/receipt fsync失败、
取消、confirm不改原结果且不补ownership。系统进程、probe和cgroup仍是显式离线边界替身，
不称真实systemd清理已验。复用的旧首manager测试只将合成UID与临时文件真实owner对齐，
保留原路径、render、目录inode和首命令断言。原期限、资源/输出门、控制器决定及生产
ConfigurationFile均不改；新增固定小文件IO计入原墙钟，不加预算。

根已实收出生/清理专用9项通过（pytest1.74秒，守卫2.349秒、88.5MiB、swap0）。
既有coordinator首轮在172项通过后，旧timeline专用夹具缺新锁接口而失败；仅完善该夹具
的lock/lease与固定删除观察边界，保留原timeline/result断言，真实删除仍由专用回归覆盖。
三文件最终整合604个不同节点通过（pytest4.10秒，守卫4.761秒、186MiB、swap0）。
非作者只读后审未见剩余P1/P2；未以这些离线结果替代真实systemd/最终清理验收。

### Native38现场与Native39：仅合并两处同步组件门

根已安全核验精确候选 `c4feac5` 的 dispatch `40084916206`，artifact
`12556841931`：ZIP 2005字节，SHA-256
`d99c456c8bad3060923561e0eda490819dae7ad541c54d0a084de48847f4c4ca`。
adopt代 `d642919f66cb6e949847082207309988` 的历史与当前成本均为observed，准确匹配
PID2562、UID999、invocation `bc1ff49d60c6402e9039a92c88a0d2e5`：完整_check共168次、
26579毫秒，bus共286次、4675毫秒，HTTP共12次、1354毫秒。adopt于56002毫秒released、
57403毫秒完成projection；switch于57404毫秒进入prepare，60002毫秒Timeout，未观察到
其prepared事务。十项通用CI最终全部通过，仅原生job `120845765680` 失败；其日志首报
`native_command_failed`，独立cleanup报 `native_bootstrap_not_empty`。cleanup仍有三mount，
用户/组未移除，不能称完整原生事务或清理通过。
这些同代指标显示完整_check是已计量类别中的主要成本，不等同于
全部墙钟归因，也不预言下一候选节省的毫秒数。

Native39仅修改 `ManagedLinuxBackend._confirm` 与 `install` 的组件循环，删除各轮
外层重复guard。两处到下层首guard之间仍有只读文件IO、解析与目录fd打开，不能称
“没有IO”；其间没有await，不缓存或跨await复用任何身份、材料或截止证明。
`ConfigurationFile.confirm/replace` 与 `SelectorStore.confirm/replace` 原首guard、
写入/同步前与CAS后的原核验均保留，循环后的完整确认、await后probe、最终proof、
lease、cgroup及原deadline不变。start/stop其它路径不改，不引入skip开关。
fixture出生观察器的pending建立时点由独立作者另行收紧到被包装原guard首次成功后，
不以新增完整_check抵消本片去重；该fixture接缝须由其专用回归核验。

根实收准确目标
`tests/test_managed_backend_guard_budget.py::test_confirm_uses_eight_real_checks_without_duplicate_component_guards`
首红：1 failed（pytest0.52秒，守卫1.061秒、64MiB、swap0）。真实confirm与磁盘
前置均通过，实际11次完整_check，预期8次，多出三次。候选随后仅删除上述两处
循环外guard；install真实计数及在真实组件入口使revision、slot、lease或cgroup失效的
零效果反例由独立测试作者补齐。原截止与跨await保护沿既有回归核验。
候选尚待根串行检查；没有新远端执行或性能改善/功能成功声明，
原输出、资源及所有期限上限均未提高。

根随后收取正式默认switch安装，完整_check准确43→34，包含两次真实确认；与confirm
11→8、四项组件入口漂移共六节点最终通过（pytest3.04秒，守卫3.581秒、63.6MiB、swap0）。
首轮计数两项已通过，四负控均拒绝且磁盘不变，但错误地要求任何fsync都为零，因原
`JournalControl.read_current` 必需的目录durability读屏障而失败（4 failed/2 passed，
守卫3.706秒、65.8MiB）。只修测试分类：严格允许并要求原控制目录唯一fd/dev/inode
屏障，任何组件同步或额外同步仍失败，生产读屏障没有删。负控对cgroup失效保留
此前合法配置前缀，不谎称整个install未写入。

出生hook整组11项通过（pytest1.93秒，守卫2.586秒、88.2MiB），新增两项分别证明首
guard拒绝零pending/零发布及原异常身份不变、真实CAS多次guard只准备一次birth。
后端、控制器、host wiring、事务文件、coordinator及成本七文件732项整合通过
（pytest52.89秒，守卫53.594秒、172.8MiB），共749个不同本地节点，均swap0。
非作者分别核两loop生产、真实漂移负控及birth钩子，未发现剩余P1/P2。
Ruff已通过；格式首查只发现新测试一处纯排版，已格式化，最终386文件格式检查通过。
禁用增量缓存的严格类型检查146源文件通过（守卫7.902秒、372.7MiB、swap0）。
上述本地证明不代替Native39真实systemd性能、三事务与清理验收。

### Native39真实验收：清理闭合，三事务仍未闭合

精确签名候选 `9dfee5247d1c34c891c97aa8dd6ed5a6f891aa53` 的
[CI 40086602632](https://github.com/69gg/MuLi/actions/runs/40086602632) 十项通用检查全部通过，
仅原生job `120850722108` 失败。严格typed回读artifact `12557216865`，ZIP1892字节，
SHA-256 `21adf42567d42c9e125fce5cb047e90047dd3907bfd5d84a1904599cacb0e1ca`，
仍为 `completed=false/TimeoutError/stage=helper_exit`，原60秒总门未提高。

adopt代 `fe159a013df0e5caa26393bbb7d634d1` 的完整历史成本为observed，准确匹配
PID2383/UID999/invocation `1db8066c5db64c38a0623309560a3371`：checks153次、16438毫秒，
bus286次、3660毫秒，HTTP12次、1081毫秒。adopt在38127毫秒released、39070毫秒projection；
switch在39072毫秒prepare，代 `b0078440461f8d0ead3a05c34c86768a` 于41721毫秒prepared、
56278毫秒install_target，60000毫秒截止。第二代当前成本因identity_changed不可用，
不能把第一代历史当作第二代成本，也不按跨runner墙钟差直接宣称加速百分比。

本轮首次真实取得 `resources.cleanup=verified`，receipt的mounts为空、user_removed及
group_removed均为true，证明该专用fixture账户、组与已记录挂载回收闭合；不扩展为真实
用户部署恢复或三事务业务成功。暂不引入按mtime/identity复用解释器摘要的缓存：现场尚未
细分binary成本，且重复真实内容核验与元数据缓存并非同等证明。

### 下一窄片：成功安装直接返回严格确认，保留原启动核验

首轮仅新增 `tests/test_managed_install_confirmation.py` 两个目标节点后，根实际收取
2 failed（pytest3.00秒，守卫3.543秒、64.7MiB、swap0）：两者真实安装均已完成，
准确在返回None而非BindingConfirmation处红，非fixture/import问题。随后授权本窄片
生产候选及必要负控；候选尚待根检查，作者未自行运行。复用原backend的真实slot/anchor、journal、三文件CAS、writer lease与
cgroup文件；仅systemd/process证据及命令执行沿既有Host最小替身，不声称真实服务已验。
Native39上节现场保持原样，不将本设计当作已实现或已加速。

重复接缝是：`ManagedLinuxBackend.install` 已在原lease与空cgroup下执行完整确认、
probe/必要reload、再次确认；控制器成功后仍返回旧record。驱动因未推进还会等待一次，
下轮完整helper核验后，`observe` 又确认三文件并再次probe，才构造安装确认；随后原
`start` 仍独立完整核验。正常停止分支这一轮observe静态包含14次完整_check与12次应用
Unit/Service RPC；这里只是源码调用量推导，未测收益，不保证60秒三事务通过。

候选让正式managed安装返回准确 `BindingConfirmation`：position、role、configuration、
selector（允许确证缺失）、unit和loaded_unit必须与本次材料及实际loaded证据完全一致。
仅成功末尾可返回；不合成None、历史或部分安装确认。控制器经原截止重核、精确CAS后
持久化start intent并沿原start执行，省掉上述重复observe。启动自身的15次完整_check、
两次probe、全部真实内容hash、lease/空cgroup、loaded-unit、原效果前后门不得减少。
不得跨await复用二进制摘要或用返回值替代start校验，不改变普通V1或持久格式/预算。

当前两个首目标依次为：真实安装返回严格确认后，经正式CAS进入原start且保留其准确
15-check/两probe；真实ManagedTransactionController单步安装完成后，不再另做一轮
observe而推进并执行原start。两者均委派真实backend，未用未来缺失API/import制造红。
shared policy仅增加可选安装观察返回，managed由完整匹配的严格BindingConfirmation
构造停态安装证明，V1仍返回None、沿原重入观察路径。正式managed_host把同一个
verify_helper传给续行；回调仍经原_call及原绝对期限约束，完成后再核截止/CAS，不能
顺便删除原下一效果前的helper认证。直接构造driver可不提供回调，不冒充认证了helper。
当前Host测试不证明真实helper/PID/cgroup资源认证已贯通。

同一专用文件现补续行helper拒绝/取消、revision CAS/slot/unit/cgroup漂移及原截止失效；
六个确认字段逐项漂移不得进入helper续行或start；安装回包丢失/取消后新driver重新
观察，不假定start已发，回退保首因；未知start正常运行或已停止两种重开均不重复命令，
停止分支仍ambiguous。这些重开只重建controller，另由既有host/真正锁重开组整合验证。
新增 `tests/test_managed_host_install_confirmation.py` 沿实际run_managed_helper、真实
journal/anchor/slot材料和原verify_helper，使用既有OS-facing probe与应用效果替身：
安装后helper PID或kernel-limit证据失效必须先持久失败且零start；不能略过续行认证
直到下一普通loop才发现。它是正式host接线负控，不称真实systemd资源认证验收。
作者冻结后由根串行核验；新增21节点整组在原30秒门内硬超时，只有17个进度点，无
pytest终态，不能记为通过。未提高时限，随后拆成完整不重叠五批、每批仍30秒：

| 范围 | 节点 | pytest / 守卫耗时 | 峰值内存 |
| --- | ---: | --- | --- |
| 真实安装与单步续行 | 2 | 4.27 / 4.840秒 | 64.6MiB |
| 续行拒绝、取消、漂移与截止 | 7 | 10.83 / 11.446秒 | 65.4MiB |
| 严格确认六字段漂移 | 6 | 9.21 / 9.768秒 | 65.2MiB |
| 安装丢回包及未知start重开 | 4 | 10.17 / 10.779秒 | 64.8MiB |
| 正式host认证接线 | 2 | 2.59 / 3.176秒 | 65.2MiB |

五批均exit0、swap0，共21个不同节点。后端、V1/V2控制器、host wiring、事务文件、
helper成本与guard预算七个相邻文件另189 passed（pytest41.34秒，守卫42.093秒、
100.9MiB、swap0），不借此前Native39的749节点重复累计本候选覆盖。
非作者只读生产后审未发现明确P1/P2：原helper认证、截止重核、精确CAS及原start完整
门保留，V1仍为None旧路径，丢失安装返回和未知start不重复发命令。
正式host、shared事务记录、Linux控制器、helper、legacy host、停态观察、原生失败与
maintenance另八文件218 passed（pytest37.53秒，守卫38.311秒、110.7MiB、swap0）。
与上述组无重复，共428个不同本地节点通过。Ruff通过；格式首查只有两新测试排版，
修正后388文件格式通过（守卫64毫秒、21.4MiB）；146源文件clean/noincremental strict
通过（守卫9.192秒、373.9MiB、swap0、exit0）。新签名候选及真实原生CI仍待完成；
源码推导的节省调用量不代替实际systemd收益或三事务60秒门验收。

### Native40真实结果：局部请求超时与清理失败分别保留

签名提交 `afa37ac75c3280a47c50f882b0328d71ce606f6f` 的
[CI 40091142140](https://github.com/69gg/MuLi/actions/runs/40091142140) 已终态failure：
十项通用检查全部success，唯一managed-native作业 `120863893749` 失败。
artifact `12559147404` 仅1998字节，SHA-256
`eebb80ff2e1021392cdbbf016526ce28996860010ec598b166235666332974ee`；
根已核对摘要、ZIP成员及原typed receipt/result，不执行归档内容。

本轮adopt代 `55164643457f607e94eec7a3efe0c3de` 已于48916毫秒released、
50142毫秒projection；switch于50144毫秒开始prepare，53096毫秒记录prepare后
以 `TimeoutError/completed=false` 结束。此时尚未到原60秒总截止；
`Case.http` 另有原2秒单请求期限，现有材料不能断言POST未受理或已经原子受理。
不得把这次失败写成总门耗尽，也不根据跨runner墙钟差声称优化收益。
准确adopt helper为PID2662/UID999/invocation
`be36396930b4430c80358f81b38afd14`，历史成本checks139次/20428毫秒、
bus274次/4293毫秒、HTTP12次/1378毫秒；没有第二代helper成本证据。

后续独立cleanup步骤实际报 `native_bootstrap_not_empty` 并exit1；
receipt仍有3个挂载，user_removed与group_removed均false，未取得
`resources.cleanup=verified`。这与Native39的成功清理是两次不同运行，不能借用旧结论。
现场是托管runner的专用fixture，不是本机真实用户服务；未进行本机挂载、账户或服务操作。

源码独审另发现可独立验证的cleanup缺口：成功stop后再次通过GetUnit取应用路径，
合法单元GC会阻断后面的真实birth删除；现有夹具固定保持loaded，未覆盖该窗口。
该反例尚不能证明本轮现场就是GC。下一片先新增离线真实birth/Case.cleanup反例，
再复用固定应用单元的规范Properties对象路径，保留fragment、PID/job、空cgroup、
真实journal/control/writer lease及birth门；不把NoSuchUnit直接解释为安全空态，
不递归删除未知目录、不增加任何期限。主prepare超时仍是独立未闭合项。

该GC反例已由根准确先红：真实birth、cleanup、typed listing/Properties、stop与临时
cgroup事件均已通过前置，停后的GetUnit按规范NoSuchUnit失败，1 failed、pytest0.39秒，
守卫956毫秒、84.9MiB、swap0。首次误将selector传给仅接受文件的backend入口，被守卫
125拒绝且未运行pytest；随后使用受保护的run -m pytest取得上述实际红，不记拒绝为测试。

窄修只在fixture的两次应用快照中复用已有规范_UNIT_PATH，停后再次核对同一fragment；
helper的一般查询、成功stop、PID/job/空cgroup、journal锁/writer lease及birth删除门保持。
原正控与Properties不可用、fragment漂移、非空cgroup、PID和job五类负控共6 passed
（pytest1.13秒，守卫1.645秒、75.5MiB、swap0、exit0）。负控保留owned叶的字节/inode
和出生回执；全部保留外来叶、仅stop一次。根非作者完整差异审查未发现明确P1/P2。
这证明离线反例已修，不代表本轮现场归因、prepare超时或真实清理已验收。

根随后沿原30秒守卫运行birth、原生coordinator、checkpoint及资源envelope四个完整文件，
690 passed（pytest4.90秒，守卫5.564秒、170.4MiB、swap0、exit0），包含上述6节点，
不重复累加。Ruff通过，格式首查仅新测试一处列表排版，修正后全仓388文件格式通过。
本片没有src生产变更，不借用前片146源码严格类型结果声称本片另跑过。

### Native41：真实清理闭合，switch请求仍超时

签名提交 `eeb7d7625dc8cceeaddf51a946bc5173cd940829` 的
[CI 40093367567](https://github.com/69gg/MuLi/actions/runs/40093367567) 已终态failure：
十项通用检查全部success，唯一managed-native作业 `120870459301` 失败。
本轮原生artifact `12559543794` 为1685字节，SHA-256
`1a62fe292780005beec044c0c81940c44313ec0e0f9fbd0462b1026a46c1d369`；
根已核对摘要、精确两成员及typed receipt/result，不执行归档内容。
本轮实际取得 `resources.cleanup=verified`、mounts为空、user_removed及group_removed
均true；只证明该专用fixture清理完成，不将Native40旧现场倒写为成功。

主结果仍为 `TimeoutError/completed=false/stage=prepare`：adopt代
`1e911ddb3344093e3d13126b0fb706bc` 于50433毫秒released、51852毫秒projection，
switch于51853毫秒开始prepare，54826毫秒结束。仍早于60秒总门；原2秒HTTP局部门
保持，不能从timeline判定请求是否已原子接纳，不盲重投。准确adopt helper为
PID2842/UID999/invocation `9093100ca8c04022a6a4a5ee08c93bc9`，checks139次/21917毫秒、
bus274次/4466毫秒、HTTP12次/1490毫秒；不以跨runner时差声称加速。

下一窄片仅将switch候选的纯文件注册卸载到受控线程，来源、SQLite、配置锁和发布门
仍由原事件循环拥有。原2秒请求与60秒总门不增加，也不承诺单个卸载消除主超时。
根已沿真实adopt/released后switch的正式准备路径取得响应性首红：prepared、generation、
目标摘要和journal均通过前置，但注册尚未返回时独立主loop协程无法前进，准确为
1 failed、pytest1.58秒，守卫2.548秒、72.6MiB、swap0。其余取消/重复取消与候选、
源unit、原plan漂移负控已冻结，尚待生产实现后的根检查。

根在授权最窄生产修正后实收上述完整新文件6 passed（pytest0.58秒，守卫1.149秒、
76.6MiB、swap0、exit0）。switch只对三个Path和原PackageLimits执行file-only线程任务；
shield保留任务引用，取消和重复取消仍等待真实线程结算、取回异常后传播原取消，配置锁
不提前释放。adopt、后续原进程观察、来源与候选全hash、SQLite、CAS及发布门保持。
非生产作者对实际代码及测试只读后审无明确P1/P2，不能以方案审替代实际代码审。

六个完整准备、运行绑定、自管理与候选文件整合131 passed（pytest12.69秒，守卫13.593秒、
186.4MiB、swap0、exit0），包含上述6项，不重复累加。Ruff和389文件格式通过。
首次strict命令启用explicit-package-bases却漏配MYPYPATH，源码导入被当作Any并报311项；
未改生产压制错误，按源码根补 `MYPYPATH=src` 后，146源文件strict/no-incremental通过
（守卫7.898秒、374MiB、swap0、exit0）。本片未改变依赖或发行版本，新的精确签名
候选及真实原生CI另行验证；这些本地结果仍不证明原2秒请求或60秒三事务已完成。

### Native42真实结果：已进入第二代，仍在总截止失败

GPG签名提交 `7867dafccd2acea7eb854d690835fd50b2800caf` 的
[CI 40095097180](https://github.com/69gg/MuLi/actions/runs/40095097180) 中，
已终态failure，十项通用检查全部success，唯一managed-native作业 `120875563257` 失败。
根已核artifact `12560184685`：ZIP1893字节，
SHA-256 `d5d1c78238e8103812a1f8405c40d1770e7ecc5d0cf2d884437f65860d415b5a`，
精确两成员与原typed receipt/result均有效，不执行归档内容。

本轮cleanup为verified、mounts为空、user_removed与group_removed均true。
主结果仍TimeoutError/completed=false，但本次stage为helper_exit，总截止60000毫秒，
不再是前两轮的switch HTTP prepare局部失败：adopt于40284毫秒released、41442毫秒
projection；switch于41443毫秒prepare，第二代 `4438839d4b7972ebb2b0c29131e87aad`
于44101毫秒prepared、49896毫秒draining、54391毫秒stop_base、58204毫秒install_target，
60000毫秒结束。没有完成第二代启动验证或第三事务，不能作为完整原生恢复通过证据。

第一代准确PID2340/UID999/invocation `8f059d535c434848ae79e540d1ddeafc` 的成本为
checks139次/17351毫秒、bus274次/3526毫秒、HTTP12次/1217毫秒；第二代当前成本为
identity_changed不可用，不将第一代历史当作第二代成本。线程卸载的离线响应性反例已闭合，
本轮现场也实际走过原prepare；跨runner墙钟差不能独立归因于该改动或折算加速百分比。
下一片先只读审查原helper/backend的重复工作，保持完整hash、lease、效果前后门和原期限。

### Native43：合并已有selector确认的同步重复入口校验

根沿真实Host、正式install_target和target确认取得准确首红：原_check逐次完整委派，
磁盘身份/字节、事务record与零命令前置均成立，只因实际8次而要求7次失败。
1 failed、pytest1.80秒，守卫2.321秒、63.9MiB、swap0、exit1。
窄修仅在非空SelectorSnapshot时省去SelectorStore.confirm的外层guard；其前置_expect
为有界同步只读，没有await或写入，随后ConfigurationFile.confirm仍在任何fsync前执行
原完整guard。底层第二guard、inode双核、文件与父目录fsync和后读保持，缺叶分支
仍先原guard再目录耐久确认。真实revision与writer lease inode漂移在固定底层入口注入，
继续原确认与原guard，只准原journal控制fd的读耐久屏障，组件身份/字节不变、零命令。

完整新文件4 passed（pytest4.45秒，守卫4.966秒、63.3MiB、swap0、exit0）。非生产作者
对实际一处生产差异与低层调用后审未发现明确P1/P2。这不是原生三事务的60秒验收。
扩大六文件时60秒守卫终止（60.171秒、75.3MiB、swap0、exit143）；没有pytest总结，
不将部分progress当作完整通过。旧guard次数测试相应更新为非空selector确认7次、正常
install31次，其中probe的6次保持、target由28变25；None分支不全局减计数。
后续按完整文件拆分复验，不提高原守卫或事务期限，不跳过安全负控。

拆分后backend完整文件34 passed（pytest31.45秒，守卫32.081秒、72.6MiB、swap0）；
旧guard预算与新selector两完整文件10 passed（pytest7.51秒，守卫8.106秒、65.7MiB、swap0）。
安装确认、事务和底层文件三完整文件首验97 passed、2 failed（pytest31.05秒，
守卫31.621秒、69.8MiB、swap0）：两项准确失败为start的旧15次计数，实际为14。
start仍完整委派两probe的4次None检查与10次target门；仅同一selector确认少1次同步重复。
对应断言仅将15改14、target11改10，保持None4、probe2、认证、命令、record和漂移负控。
三文件再验99 passed（pytest30.83秒，守卫31.396秒、69.6MiB、swap0、exit0）。
与上述两组无重叠，共143个不同本地节点通过；不把此前部分progress、首失败或子集再累加。
Ruff通过；格式首查仅新测试一处排版，修正后390文件格式通过。146源文件strict、
no-incremental且MYPYPATH=src通过（守卫8.193秒、374MiB、swap0、exit0）。
本片无依赖、发行版本或持久格式变化；新的精确签名候选与原生CI仍需另外验证。

### Native43真实结果：第二代进入start，三事务总门仍未通过

GPG签名提交 `b1dc06ccfeecd3834d1142d1e0e71c19c0eaec10` 的
[CI 40097893114](https://github.com/69gg/MuLi/actions/runs/40097893114) 已终态failure：
十项通用检查全部success，唯一managed-native作业 `120883875588` 失败。
artifact `12562145130` 为1915字节，SHA-256
`c891664ab7878e8d7af39f6df1d3042a2564c286d39f35ee4149cc5b585fbe99`；
根只下载该小体积证据，核摘要、精确两成员及typed receipt/result，不执行归档内容。
本轮cleanup为verified、mounts为空、user_removed和group_removed均true。

adopt代 `9207c55dc3649ded134d2213bdb2a132` 于35932毫秒released、36872毫秒projection；
switch于36873毫秒开始，第二代 `beda6448b3eeac41d4254aa207094046` 于39131毫秒prepared、
44086毫秒draining、47875毫秒stop_base、51281毫秒install_target、59220毫秒start_target，
总60001毫秒以TimeoutError/completed=false/stage=helper_exit结束。未完成第二代启动
验证或第三事务，不能把合法前缀和清理成功当作完整原生恢复通过。

第一代准确PID2335/UID999/invocation `b02db620c3044954a060c529c1c54b29` 的成本为
checks135次/13444毫秒、bus274次/3818毫秒、HTTP12次/805毫秒。第二代准确PID2724、
invocation `7e1f5c1648ee461d86e9d934fd618a5d` 的当前成本仍identity_changed不可用；
不将第一代成本借为第二代，不按跨runner墙钟差归因或计算加速比例。下一步先审现有
身份有效观察边界能否保存准确第二代成本，未凭猜测引入跨校验缓存或扩大原截止。

### Native44候选：准确helper的安装返回快照

源码核验发现现成本hook只在atexit发布完整生命周期统计。尚未退出的第二代没有该文件，
final collector以第二代身份拒绝第一代文件是正确行为；identity_changed也涵盖不合合同
的形状，不能只据这个标签断定现场原文件内容。只提前读取或放松身份门不能补上数据。

本片仅改CI fixture，不改生产包、期限、依赖或实际效果。生成hook完整委派真实install，
成功返回且BindingConfirmation.position准确后，每helper至多尝试发布一次固定私人
helper-costs-checkpoint.json。原异常、取消和返回保留，观察失败不改原成功结果；
checkpoint含严格sample(kind=install_returned、准确revision)与原五身份/三类累计成本，
明确只是安装返回时的partial，不表示该进程读取时仍存活或完整生命周期成本。

Case在原fresh-position→helper_empty身份核验之后读取，必须已有准确本代非零PID与
invocation来源；未曾观测、异代、已invalid或文件漂移不能从checkpoint自证来源。
复用原安全FD、大小/权限/单链接、前后完整身份与重复JSON键检查；sample revision不能
晚于当前record。最多缓存三代，完整结果最终仅在原32KiB余额内加helper_cost_samples，
原当前helper_costs、完整helper_cost_history、identity_changed与事务控制保持独立。
没有新增RPC、轮询、systemd命令或放宽60秒门。

正式先红为真实生成hook、switch Host.backend.install、原Case身份核验与严格reader：
install和原确认完整成功，最终旧文件仍被准确拒绝，固定checkpoint却缺失；
1 failed / pytest1.47秒，守卫2.023秒、69.9MiB、swap0、exit1。不是导入失败或超时。
实现后8个目标节点通过（pytest2.45秒，守卫2.984秒、75.7MiB、swap0）；
完整helper_costs文件46 passed（pytest2.57秒，守卫3.096秒、69.4MiB、swap0）。
真实Case.transaction的fresh-position分支已在离线probe/HTTP下自动消费实际hook文件，
不以手填resources代替接线；安装失败原异常实例、缺失/失效/漂移来源、坏sample、
完整成本与partial分离、旧32KiB边界均保留。8项包含在46项内，不重复累计。

非生产作者读实际两脚本后未发现明确P1/P2。本轮只证明离线接线与合同；精确签名候选
及真实CI仍待执行，尚无第二代现场partial，更不表示原生三事务或超时根因已修复。

根补审发现coordinator在worker最终结果之后还会合并manager/controller事实，新增partial
可能因此挤掉旧完整成本。正式collect_result近限回归先取得1 failed（pytest0.26秒，
守卫0.806秒、76.1MiB、swap0、exit1）：输入与合并后的准确预期均通过严格reader前置，
实际保留partial却缩掉旧helper_costs，整体不等；不是超时。窄修在合并完成后、原完整成本
压缩之前，按原32KiB门从尾裁去最多三个新增partial，空则省略该字段。相同节点随后
1 passed（pytest0.17秒，守卫0.711秒、72.1MiB、swap0、exit0），保留原完整成本/history、
manager事实、首code、timeline和generations；实际差异只读后审未发现明确P1/P2。

随后七个完整fixture文件统一实收790 passed（pytest22.88秒，守卫23.524秒、
CPU23.343秒、185.8MiB、swap0、exit0），包含上述节点，不再累加先前子集。
最终两脚本及测试的Ruff通过，三个文件格式检查通过；提交和dispatch尚未完成。
真实第二代partial采样与完整三事务仍待新的精确候选CI验证，
不将本地绿倒写为现场超时已修复。

### Native44真实结果：第一代partial已取得，第二代尚未安装

准确签名候选 `fdb0b95da2ae2e3501c8ad65e0dc3b43e0062e39` 的
[CI 40102230148](https://github.com/69gg/MuLi/actions/runs/40102230148) 已终态：
十个通用作业通过，唯一managed-native作业 `120896568215` 失败。
根已严格核验artifact `12563236559`：
ZIP1944字节，SHA-256 `7e87ebf77454740cb555d856327beedf1d60450f5affc77efee96501c3bc1c6e`，
精确两成员与typed receipt/result有效。本轮cleanup为verified、mounts为空，user_removed
及group_removed均true；上段候选待验证状态保留为当时记录。

第一代 `18ec5be744937a3c185c11747a9cc7de` 的准确helper为PID3038/UID999/invocation
`352ec2647ee64cf6acbeedd5cdb43334`，完整成本checks135次/17156毫秒、bus274次/4501毫秒、
HTTP12次/1400毫秒。实际取得的install_returned partial属于这一第一代，revision4：
checks73次/9105毫秒、bus139次/2495毫秒、HTTP4次/434毫秒；不称已取得第二代采样。
第一代于42228毫秒released、43483毫秒projection。

第二代 `cf0176289845078c7d36c63092be063d` 的准确helper为PID3438/invocation
`4620e84d226945a789570c0719f42d8a`，于46284毫秒prepared、52336毫秒draining、57135毫秒
stop_base，60001毫秒以TimeoutError/stage=helper_exit结束，尚未到install。因此只有第一代
partial，第二代当前成本仍identity_changed不可用。完整三事务未通过，不能把第一代
采样或清理成功当作超时修复，也不按跨runner墙钟差计算优化收益。

### Native45：整合后临时 helper 终态观察失败

原 PR #37 已同步至签名候选 `dd0c122c9603ce4faa287d7e4f06216f474b7b20`，
整合 v2.70 并消除合并冲突。[CI 40180376486](https://github.com/69gg/MuLi/actions/runs/40180376486)
的原生作业 `121129961274` 失败：第一代在 42397 毫秒 released，43327 毫秒
以 `ObservationError` / `helper_exit` 结束，没有最终投影或第二代。此次不是超时。
artifact `12593636794` 的 ZIP 为 1662 字节，SHA-256
`11f64b0de3fff7d8fb92aae245540a702637eeccac9a749d6fae70e177a7d417`；
两成员、receipt 与 result 已严格核验。cleanup verified，用户和组已移除，mounts 为空。

代码和时间线将失败缩小到 helper 退出观察。原记录没有底层异常帧，不能唯一认定现场原因。
新增真实 Case + SystemdProbe typed 回归证实：临时 helper 可以在精确列表查询、
GetUnit、Unit/Service 属性读取之间被回收。旧实现取得 8 failed / 2 passed；其中三个
成功场景分别在原 RPC 位置抛出 ObservationError，格式与 argv 身份负控继续通过。

窄修仅处理固定 RPC 不可用错误：再作一次精确同名查询，成功空列表才记录 collected。
非空、失败、坏格式或错名保留原错误；取消正常传播；snapshot 校验和身份门不降级，
已撤销的历史 source 不因随后回收而恢复。不改生产 probe、期限或资源限制。
修后新增 11 项及相关四文件共 115 项通过，随后完整 18 文件 1020 项通过
（64.30 秒、219.1 MiB、swap 0）。另复用已有安全诊断，只在原失败 stderr 输出一次
有界的产品代码帧，不输出异常正文、真实路径、局部变量或异常链；提取失败不覆盖首因，
不新增结果字段。原生完整流程仍待新的精确候选验证。

### Native46：第一代闭合，第二代启动仍触及总期限

签名候选 `92d80dfb8361f8f2100508e9e2b66cbb7940744e` 的
[CI 40182350177](https://github.com/69gg/MuLi/actions/runs/40182350177) 已终态：
十二项通用作业通过，唯一原生作业 `121135856312` 失败。
artifact `12594606836` 为 2004 字节，SHA-256
`8edb07635c66144372377cba66ad4afe99ac9d97a93c6f6de386c0255b8bf55c`；
两个成员与完整 typed result/receipt 已核。cleanup verified，用户、组已移除，mounts 为空。

第一代在 32104 毫秒 released，32994 毫秒 helper collected，33010 毫秒最终投影通过。
第二代在 35237 毫秒 prepared、45976 毫秒 install_target、53280 毫秒 start_target，
总 60000 毫秒以 `TimeoutError` / `helper_exit` 结束，第三代未开始。
第一代完整成本为 checks135/13995毫秒、bus274/3067毫秒、HTTP12/971毫秒；
第二代准确安装返回 partial 为 checks73/10178毫秒、bus139/1518毫秒、HTTP4/293毫秒。
不将 partial 当完整生命周期，不按不同 runner 的墙钟差计算优化收益。

本轮没有再次出现 Native45 的终态观察错误，但三事务仍未通过。worker 的有界失败帧
尚被父协调器的非零命令处理隐藏，不能声称已在远端日志取得该诊断。
离线真实 backend 小范围 profile 将主要检查成本指向反复读取、哈希同一 Python 二进制；
下一片仅评估单次同步检查内的重复校验，不延长身份凭据寿命或扩大原期限。

### Native47 候选：同步检查内去重

本片只去除两处相邻同步工作。backend 在 controller 已完整认证后，给随后独立校验的
role 传这一次 Python 指纹；第二槽仍独立打开真实 FD，核路径、完整身份、链接链和
自身字节限额。仅全部相同才省第二遍读取/SHA，其余包文件仍分别校验；无凭据入口
继续完整哈希，凭据不跨调用或等待。helper 循环在等待后的完整认证之后，用原
`control.read_current()` 比较进度，保留锁、poison、fsync 和 V2 类型门；控制步骤、
每次等待两侧认证及真实效果门不变。

真实 backend 首红实收读取 131592 字节而非 65796 字节，两个槽均实际打开文件；
另一个真实 helper 首红准确落在调度读取多出一次完整 `_record` 和 Python 校验。
修后 backend/launcher/probe 五文件 100 项通过，helper/host 五文件 73 项通过。
安装、启动与回退十三文件首查 315 通过、2 失败，原因是离线成本测量 wrapper
未转交新关键字参数；修正测量接线后两项真实 CLI、启动、最终投影和拒绝目标回退
均通过。以上共 490 个不同节点，重跑与首红不重复累计。新增边界包括 ctime/inode
漂移、链接替换、独立包损坏、不同字节限额、下次重哈希、真实缺失/V1 记录拒绝。

155 个产品源码严格类型、Ruff 与格式检查通过；两片均由非作者只读后审，未发现
P1/P2。这里只证明同步重复工作已去除及离线边界保持，不按本地墙钟承诺完整三事务
能在 60 秒完成。真实原生 CI 仍待精确签名候选验证，原失败和期限保持不变。

### Native47真实结果：第二次排队请求局部超时

准确候选 `575402b` 的 [CI 40184138517](https://github.com/69gg/MuLi/actions/runs/40184138517)
原生作业失败。第一代 adopt、release、helper collected 与最终投影均已完成；
47.917 秒开始第二次 POST 排队请求，50.947 秒以 `TimeoutError` 结束，未达到总
60 秒期限，且没有第二代记录。原 HTTP 客户端仍配置 `HTTP_SECONDS=2`；观察到的
约 3 秒墙钟间隔不是配置期限变更。cleanup verified，不把清理成功当作三事务通过。

该 POST 只进入 `Management.stage` 持久排队、唤醒及通知，不在 route 内等待 prepare
完成；后台管理任务随后在同一事件循环执行 prepare。同步文件校验可能延迟 ASGI
发送响应，是待验证假说。超时既不证明第二请求未受理，也不证明已完成 prepare，
不能据此盲重投有副作用的 POST，或认定唯一根因已经找到。

本轮另有六个平台分片失败，不是仅原生作业失败。根核对 Ubuntu 3.12 shard1 的
`test_file_observation_preserves_original_calls_failures_and_lookup_restoration`：
`False/True × none/selected` 四节点的旧 `selected` stub 不接收 `verified_python`，
与上一片测量 wrapper 新增的转交参数不匹配，实际抛出 `TypeError`。
根本地守卫收齐该测试八参数中的 4 failed / 4 passed；同批 distinct slot 独立新 boot
用例通过，合计 9 节点为 4 failed / 5 passed（pytest 0.49 秒、75.8 MiB、swap 0）。
窄修仅为 stub 补显式可选 `FileFingerprint` 参数，并以身份断言核实非空指纹完整转交；
原异常实例、调用次数、计时和 lookup 恢复断言保留，已包含在下述修后整合中。
Native46 的十二项通用作业通过不作为本轮结果，测试接线失败也不冒充原生超时根因。

### Native48候选：source 同步认证去重

仅在 `_source` 已通过 `verify_anchor` 完整文件认证，且 source 与 controller 的完整
`SlotIdentity` 相等时，复用这次文件认证并调用已有 `verify_current_process`；不同 slot
与无 anchor 分支仍用原 `verify_current_slot`。每次新的 source 读取重新认证，不保留
跨调用或跨 await 缓存，当前进程身份门与其它来源、文件、事务门不变。

管理层 `verify_source` 只删除紧接 `read_lineage` 前重复的 `current.verify()`；
`read_lineage` 内首末验证、实际来源比较与 `candidate_guard` 全部保留。
两个真实目标先红由根合并实收 2 failed（pytest 0.46 秒、守卫 1.077 秒、77.5 MiB、
swap 0）：source 实际读取 74 字节而期望 37 字节，lineage 实际验证 5 次而期望 4 次。
这证明同步重复工作存在，不证明它就是远端 POST 超时的唯一原因。

修后八文件整合 161 项通过，包含 source、lineage、切换准备、异步门、候选捕获与
清理、恢复诊断；pytest 32.88 秒，守卫峰值 150.3 MiB、swap 0。distinct slot 的
新夹具起初因复用前次 boot 被真实健康证明拒绝，已通过给原完成 helper 传入独立 boot
修正，不改健康门。生产两处窄修经非作者只读后审，未发现 P1/P2。

上段本地验收先于下述真实 CI。没有调整 HTTP 2 秒、总 60 秒或资源上限，没有新增
skip，不以本片宣称完整原生恢复或超时已解决。

### Native48真实结果：第二次排队成功，仍触及总期限

准确候选 `96bb3ec` 的 [CI 40185853686](https://github.com/69gg/MuLi/actions/runs/40185853686)
已终态。原生作业 `121146354226` 失败；十一项通用作业成功，Windows 3.12 shard2
另有两个既有 UIA 真机探针超时。两者保持原 12 秒期限且 cleanup confirmed：一个
阶段未观察到、stderr 为空，另一个到达 `uia_initializing`。不能将它们归因于 source
去重，也不能将本轮记为只有原生作业失败。

第一代最终投影在 38590 毫秒完成。第二次 POST 于 38592 毫秒发起，40431 毫秒已
prepared，45835 毫秒 draining，49736 毫秒 stop_base，52943 毫秒 install_target；
60001 毫秒以 `TimeoutError` / `helper_exit` 结束，第三事务未开始。cleanup verified。
本轮没有再现 Native47 的局部 POST 超时，但这不证明此前超时的唯一原因。

第一代完整成本为 checks135 次 / 17645 毫秒、bus274 次 / 3552 毫秒、HTTP12 次 /
1346 毫秒。第二代 install-returned partial 位于 revision4：checks73 次 / 9542 毫秒、
bus139 次 / 2006 毫秒、HTTP4 次 / 388 毫秒。partial 不是完整生命周期；不同 runner
的墙钟结果不作为提速率。

### 后续候选：补齐 host 认证与检查阶段的有界成本口径

现有 `checks` 只包装 `ManagedLinuxBackend._check`，没有统计
`managed_host._record` 内完整 anchor/controller/Python 文件认证。因而 Native48 的
17645 毫秒不是全部哈希耗时，不能据此继续猜测某个效果门的收益。

根已取得准确首红：1 failed，pytest 0.41 秒、71.4 MiB、swap 0。真实 `_record` 文件
认证与原 backend 成功/失败前置均通过，唯一失败是回执缺少 `host_record`，不是模拟
成本值或缺少调用。候选只增加独立 `host_record` 次数/累计毫秒，以及 backend checks
按固定 phase/intent 的有限分桶。计时覆盖整个原函数，不能冒称纯 SHA 时间或整机峰值。

成本回执 version2 严格新增这两项，version1 仍可读取。分桶只使用事务协议的固定枚举，
拒绝未知标签、额外字段、重复分桶、无效整数和总数不一致；coordinator reader 仍仅依赖
stdlib，不导入完整产品包。原 helper 身份绑定、最多三代历史、32 KiB 总输出、原 deadline
和先保存功能事实的顺序不变，成本装不下时不能挤掉原事实。

hook 每次完整委派原方法一次，真实异常和取消继续传播；仅复用原 install-returned
快照与 atexit 输出，不新增 RPC、轮询或逐次写盘。新测试包含真实 `_record` 的异常/取消
原实例保留、旧版兼容、严格坏值与原字节边界。本地成本回归 65 项及其余十一文件
原生回归 926 项通过；两次守卫峰值分别为 87.4 MiB、203 MiB，swap 均为 0。
源码后审未发现新增控制流或放宽校验；远端三事务和性能问题仍未闭合。

### 启动指令前的相邻效果核验

`start` 在最后一次真实探测返回后，先完成 writer-held guard，再退出 writer lease，
紧接效果检查后才发送启动指令。这段同步过程可借用刚完成 guard 的 Python 与包内容
证明，复用现有完整指纹机制；效果检查仍独立打开文件、核对目录、权限、链接、ctime、
记录版本与期限。writer 退出失败时不执行后续检查，身份漂移时重新读取并拒绝损坏内容。
证明在发送指令前丢弃，不跨此前 probe 的 await，也不沿用至下一次启动。

离线首红的 target/base 两例均保留 14 次完整检查、独立打开与 stat/目录遍历，以及
成功启动的原断言，最后才因效果检查重复读取完整包/Python 字节而失败。后续验证须
确认仅省去这次内容重读，等待后仍完整读取，并覆盖释放 writer 期间的文件及记录变化。
这只是一处重复工作的消除，不证明原 60 秒内的三事务验收已通过；期限和资源上限不变。

### 运行中宿主的同步绑定内容证明

运行中宿主的健康、排空、恢复和激活请求，以及首次终态投影，都会经过实际
`verify_running_binding`。原路径先完整认证固定 controller，再完整认证当前安装；
共享真实 Python 时重复读取整个解释器，同槽时还重复读取完整包。这里只复用既有
启动路径的证明参数，不新增缓存、发行 profile 或外部库。

根已收取新回归的准确首红：真实 Host、permit、临时 Store、私有 slot/anchor、
无写入、两次独立打开、完整目录遍历前置全部通过，最后 Python 实际读取
131592 字节而目标为 65796 字节；1 failed，pytest 0.77 秒，守卫 2.070 秒、
峰值 100.5 MiB、swap 0、最终 exit 1。原红日志单独保留，不覆盖或改写。

候选每次绑定新建局部包证明，由 `verify_anchor` 完整认证 controller 后产出；
selected 仍从实际私有元数据读取，独立打开全部文件并遍历完整包树，使用自身预算、
链接链、完整文件身份及兼容声明检查，再核当前进程。不同完整槽身份不能借包证明；
同内容但不同包根仍分别读取，相同内容但不同解释器文件也仍分别读取。
证明不保存在 permit 或对象中，不跨调用、await 或 exec；原 legacy capture、
配置/selector/unit、原 Store/格式、writer lease、首末 journal/fsync 与投影 CAS 保留。

新增回归包括实际文件读取与打开/遍历计数、相同槽与不同内容/根/解释器/预算范围、
controller 校验后的文件变异、下一次绑定确实重读修改、当前进程漂移，以及真实
激活回调等待后拒绝放行。修后整合与质量结果待根在统一守卫中串行实收；此时不能
由字节计数或源码调用量推断完整三事务能过原 60 秒门，也没有新的 Native 通过证据。
