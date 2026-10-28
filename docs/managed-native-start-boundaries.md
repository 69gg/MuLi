# 原生夹具的 start 正向边界

`start_target` / `start_base` journal intent 在调用 backend 前已持久化，不能证明
服务命令已经分派。仅原生测试 helper 的现有 cost hook 包装实际
`ManagedLinuxBackend.start` 与 `_command`，发布以下正向事实：

| kind | 事实 |
| --- | --- |
| `start_entered` | 精确 backend、record、role 的 start 已进入 |
| `start_command_entered` | 原 start 已到达 `_command("start")` 入口 |
| `start_command_returned` | 该命令正常返回；`--no-block` 不证明应用 readiness |

诊断 context 只绑定上述身份、当前原 start 任务与单调起点，在 finally 清除，不携带
文件或安全证明；同实例其它任务、其它 backend 及嵌套命令不记录为本次 start 命令。
原 start / command 各调用一次，返回、异常和取消继续交给原实现；只有命令正常返回
才写第三边界。观察器自己的时钟、快照、发布或读取失败可以造成缺样本，不能改变
真实操作的结果。取消和进程中断仍传播，并在 finally 清除 context；缺少任一边界都
不能推导为“没有执行”。

固定私有 `helper-start-checkpoint.json` 复用原 `publish` / `snapshot` / 有界文件读取，
每次覆盖，只在边界前进时尝试发布，每次 start 最多三次。每条包括固定 kind、严格
整数 revision、phase / intent / role、相对 start 的 elapsed_ms 和原累计成本。
原 `helper-costs-checkpoint.json` 的 `install_returned` 与完整 exit costs 不变。

Case 沿原 poll 读取此小文件，包括没有新 journal revision 的轮次；原最终收集路径
再读真实 journal 与此文件。不新增 bus、HTTP、sleep、reader task 或 readiness 条件。
只接受已由既有 helper 身份观察认证的 generation / helper / uid / pid / invocation
五元组，且 generation / helper 已登记、revision 不晚于真实 journal，并能匹配已记录
transaction 的 revision / phase / intent。允许组合仅为 activating / start_target / target
及 rolling_back / start_base / base；未知、未来 revision 和边界回退均不接受。

每代只缓存最新合法 `(revision, boundary)`，最多三代；同 revision elapsed 不得倒退。
source 撤销时移除对应 start 样本。最终可选 `resources.helper_start_samples` 使用精确
字段白名单与同样的身份、timeline 校验，旧结果没有该字段仍有效。输出仍受原
32768 字节限制；start 样本最后合并，协调器需要空间时先丢 start 样本，再沿原裁剪
顺序处理其它可选成本，不能用新样本挤掉结果、清理证明或已接受的成本。

固定文件的 write / fsync 和采集会消耗原墙钟预算。这是定位前置认证、命令 await
及后续 observation 区间的诊断实验，不表示性能改善或原生事务已通过。

离线回归使用真实小临时 journal、现有 slot / backend fixture 和受控命令 runner，
不启动 manager 或 subprocess。检查由主任务通过 `scripts/check_local.py` 串行执行；
本说明本身不声明离线或托管验收通过。

工作进程失败时，协调器只向 CI 日志转发已有 `native_failure_diagnostic:` 中
经公共验证器认可的一份包内安全位置。缺失、重复、超限或损坏的诊断均不转发，
也不回显原命令输出、异常正文或路径；原退出错误、收取及清理顺序保持不变。
`helper_exit` 是最近观测阶段标签，不能替代安全帧来指认真正抛错位置。

为兼容既有回执，helper 观察仍使用 `stage=helper_exit`；空 listing 仍保留
`state=collected`，但这两个历史标签都不能证明 helper 曾启动或已经退出。新增可选
`observation` 只表达原观察实际得到的事实：`never_observed` 表示精确 listing 为空，
且本轮 timeline 尚无同名 helper 的完整成功观察；`present` 表示原完整观察成功；
`absent_after_present` 表示完整观察之后再次取得精确空 listing。后者仍不证明成功
启动、健康或正常退出。旧回执缺少此字段仍有效；新字段只允许用于 helper 观察，
且必须与原 state 一致。判定只读取原有有界 timeline，不增加查询或建立、恢复 source。

`CASE_SECONDS=60` 是原生 worker 的整轮夹具预算：`Case` 从构造时起建立一个单调
deadline，`run()` 用同一个 `asyncio.timeout` 包围 `drive()` 的导入、初始服务启动、
adopt A、switch B、故意目标失败的 switch A，以及每笔完整投影。它不是每笔产品
事务的期限；产品 record/anchor 的 helper deadline 和 rollback reserve 独立保留。
最后阶段为 prepared 且 `never_observed` 时，只能报告本轮未观察到 helper，不能
据此指认前置身份拒绝或目标故障注入已经执行。

事务尚未 resolved 时，原 helper 配置、进程身份和来源撤销观察保持不变；包括 blocked
与 recovery_required。resolved 后只用原精确 unit listing 等待 helper 真正消失，存在
时继续原 poll，查询失败或无效时仍失败。只有成功解析的精确空 listing 才进入最终
成本收取与原 journal、运行角色、数据库和路由投影校验，不再读取退出期间的配置快照。
终态保留的 source 只是此前认证的历史身份，不能表示当前健康；缺失或已撤销的 source
不会从 listing、成本文件或 start 文件建立、恢复或重绑。预算、期限及 reader 数量不变。
