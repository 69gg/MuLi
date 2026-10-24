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

本节仅为方案，尚未编写或运行实现，也不授权native20。不改上述native19失败证据，不把本地586项或镜像清单当作现场READY证明。目标是在现有小实例观察中取得用户manager内部的两个有限事实，**替换**`internal=not_observed`的空位，不恢复旧大包、不追加第二次诊断。

**固定版本依据。** 根已读取上游[v255 `src/core/dbus.c`](https://github.com/systemd/systemd/blob/v255/src/core/dbus.c)的`bus_on_connection`/`bus_check_peercred`及[v255 `src/shared/bus-util.c`](https://github.com/systemd/systemd/blob/v255/src/shared/bus-util.c)167–185行：实际peer凭据为UID0或server自身有效UID才接纳。因此沿用现有真实root helper，不模拟UID、不设trusted、不修改manager认证。该文件217–249行有用户私有连接的new/address/start路径，但其后备连接不在本方案授权内。[v255 `sd-bus.h`](https://github.com/systemd/systemd/blob/v255/src/systemd/sd-bus.h)提供所需非变参API；以上是固定上游源码依据，不等同Ubuntu补丁版或现场连通实测。[peer模式文档](https://www.freedesktop.org/software/systemd/man/latest/sd_bus_set_bus_client.html)及[systemd1接口文档](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html)只作接口解释，latest页面不能替代v255证据。

**唯一连接与调用。** 在已有root helper两次proc stat/status/cgroup/exe读取之间，使用已安装libsystemd的薄FFI：`sd_bus_new`、固定私有socket地址、`sd_bus_set_bus_client(0)`、`sd_bus_start`。socket只能由已核账户UID派生，不能由调用者提供任意地址；缺库或连接失败明确not_observed。禁止open/default、Hello、订阅、自动fallback、CLI替代、LoadUnit/StartUnit或重试。仅发固定`Manager.GetUnit("basic.target")`，再读返回unit的`ActiveState`与同一manager的`NJobs`；GetUnit只查询已经loaded的unit，失败不加载它。用new_method_call/append_basic/read_basic构建及读取，严格核对返回signature和at_end，NJobs只接受`u`，ActiveState只转成有限枚举；不读取ListJobs、任意属性或错误正文。

**身份窗口。** 保持现有前后PID1 Unit/Service属性核对与两次proc四UID、exe/startticks/cgroup连续性门。只有实际exe为manager且四UID均为目标UID才进入peer读取；固定socket连接前后lstat的类型、owner、device/inode须一致。通过`sd_bus_get_fd`取得实际连接FD，核`SO_PEERCRED`的PID/UID确为已锚定manager PID/目标UID，不能用socket属主代替peer身份。调用后再次核socket及原后侧proc/PID1门，任一漂移整组internal事实作废。只证明该有限窗口，不把历史checkpoint、两个顺序标量或最终deadline称为原子同刻状态；basic active且NJobs0也不证明READY已经发出或被PID1接收。

**期限、分配与释放。** 不另开worker或cgroup；FFI在现有受限root helper内执行。每次`sd_bus_call(timeout_usec)`仅取同一个原绝对deadline的剩余额度，外层进程硬截止继续兜底，不为每次调用重置5秒。原5秒后备、cleanup与执行总期限均不扩容。每个bus/message/error及临时FD在全部失败/取消出口释放；借用的bus FD不能误关，若为取peercred复制FD则单独关闭。固定argtypes/restype，不用变参或将native错误串写入回包。libsystemd内部wire接收与malloc由现有真实cgroup上限约束，**不能宣称原32KiB管道/文件预算同时限制D-Bus库内部接收分配**。

**实际计费回执。** 原32768字节proc＋双pipe累计、512字节内层wire及1024字节整个外层回复继续不变，新增有限标量也须容入原包。入诊断前父已付B必须来自真实`setup_bytes`，raw grant为`32768−B−1024`，不从native19的worker.used17468推算。父在原结果中保存固定小型`accounting`：`parent_before`、`raw_grant`、`child_charged`、`reply_bytes`、`parent_after`和`settlement=validated|unvalidated|not_started`；数字仅取实际账本/实际wire，未知reply_bytes用null，不伪造零。合法同锚点、及时、有界回包才按真实used退差额，结算`parent_after=B+child_charged+reply_bytes`；未经验证不退款，丢回包继续按现有保守规则占满总额。未启动不制造子进程花费。该元数据同样受严格shape和整个1024字节保存投影门约束，不借新字段增加预留；它用于暴露现场是否有足够采集余额，不能保证未来一定取得内部事实。

**首片可证伪边界。** 离线fake缝隙须覆盖真实调用顺序与释放：仅两固定属性、无激活/回退；签名或尾部多值、仅peer PID/UID错、后socket/proc/PID1漂移均拒；剩余deadline耗尽、丢回包不退grant；实际512/513与1024/1025临界及最大accounting投影仍保住原checkpoint/首因。fake只证明协议编排和预算，不能证明FFI ABI、真实EXTERNAL握手或v255认证行为。实现获批并通过离线边界后，另行授权受控临时peer验证这些连接与凭据事实；不接真实用户服务、不扩大预算，也不据此跳过原READY尾门或直接启动native20。
