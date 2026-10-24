# readiness 最后完整轮的小型失败回执（第十九轮已取得历史轮，仍未 READY）

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
