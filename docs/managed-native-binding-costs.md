# Native 应用绑定成本诊断

本片只给 hosted Native 夹具补充输入体量与应用绑定成本的有限事实。产品认证、完整 A/B 包、真实目标拒绝与回退、三笔事务和原有 60/90/5 秒期限均不变。尚未进行本片的真实 systemd 实验或 hosted Native 验证；不能据此认定原 60 秒红灯已修复。既有失败记录继续有效。

## 实际输入

`describe()` 保留原先一次 `package_fingerprint` 已计算的 `files/size/digest`，准备阶段将三项写入 fixture。worker 在 A/B 原始 `register_local_slot()` 全量校验之后，仅投影其返回对象：

- `slot.digest` 与 `slot.package.{files,size,digest}`；
- `slot.resolved_python.identity.size`、解释器 digest；
- 原 `package_limits` 与 `python_bytes` 上限。

结果字段 `resources.binding_inputs` 使用版本 1。A/B 包指纹必须与 describe 的三项完全一致。缺字段或不一致只令可选诊断 `not_observed/unavailable`，不会替代原有包 digest 验证，也不会触发新一轮遍历、hash、下载或裁剪。解释器的实际 size 与 `python_bytes` 预算分别存放；本地 binary、压缩下载体积和预算值均不能冒充 hosted 的实际解释器体量。这里观察的是 Native 实际复制的完整安装包，不能将其称为正式 wheel 的测量。

## 实际调用与发布边界

生成的 serve 入口仍调用既有 `install_hook()`。它在原 `create_app()`、实际 Runtime 构造及其 `Maintenance` verifier 闭包绑定前，包装 canonical `verify_running_binding`。创建 app 后还核对 Runtime 的实际闭包确实包含该包装函数；已有外部 Runtime 或未绑定闭包不会导出零值成功。

每次包装只调用一次原函数，并原样传递其返回对象或异常对象，包括 `CancelledError`。`finally` 只累计内存数据，按实际 `generation/revision/role` 记录 `entered/returned/raised`、`perf_counter_ns` 墙钟与 `process_time_ns` CPU 累计值。每个进程最多 64 个位置；观察器不持有文件证明，不跨调用或 await 复用认证结果，不包装文件读取/hash 原语。

HTTP 请求结束后，仅在绑定位置变化时发布前缀，每个 serve 进程最多尝试 8 次。正常 ASGI `lifespan.shutdown.complete` 后最多再尝试一次终态；失败写入也消耗次数，不重试同一位置，不新增定时器或轮询。发布复用原有 32 KiB 有界原子私有 JSON 写法。单次绑定不写文件、不 fsync。

快照 `scope=prefix` 只说明已有调用的有限前缀：同一位置之后的重复调用、达到发布次数上限后的调用、未完成的 HTTP、强杀或尚未正常关闭的进程都可能未被持久化。`scope=final` 仅说明这个具体 serve 进程已正常结束；它不证明整轮 Native 事务完成。

`prior_publications` 独立记录此前已完成的发布次数、墙钟与 CPU 开销，绝不混入绑定计时。当前快照这次写入的完整耗时要到写完才知道，因此不包含在自身的 `prior_publications` 中；最后一次发布也没有后续快照来补报自身开销。不得把它解读为全部诊断开销，更不能将多进程 wall、CPU 或事务墙钟直接相加。

## 来源、覆盖与预算

每个文件绑定实际 UID、PID、`INVOCATION_ID`、permit boot 和 slot digest；boot 必须与 invocation 相同。文件名按 PID/invocation 分开，进程更替不会覆盖前两笔的历史。私有文件读取复用既有 no-follow、普通文件、单链接、owner/mode、前后身份与字节上限验证。

worker 只记忆已经由原 `origin_ready` 或最终 `projection` 的真实进程观察接受的 source。结算时最多读 4 个确定路径，并将每个 source 与原 timeline、原注册 slot 精确匹配；绑定行的 generation 必须属于本轮已观察记录，revision 不得高于该 generation 在原 timeline 中已观察的最大 revision，不要求每个中间 revision 都逐项出现。没有先取得来源证明就不读取成本文件。旧 source 只代表那个历史进程。

`resources.binding_costs` 使用版本 1；有来源的结果固定为 `partial/observed_sources_only`。没有进入既有独立观察边界的进程不在覆盖内，**尤其不包含被拒绝且未被该边界观察到的 target**。本片不为补齐它增加 probe/poll，也不宣称拿到了完整调用总数。每份快照独立核对来源、generation 和上述 revision 上限；缺快照、错误来源、未观察 generation、超出已观察上限的 revision、异常时钟、未绑定或过大文件仅令该 source 的成本降为 `not_observed`，其他已认证历史 source 继续保留。来源本身没有独立证明时不读取或导出该 source，均不提供伪造的零成本。

所有可选字段共用原 `result.json` 32 KiB 上限。新增诊断先降为 `output_budget`，必要时丢弃；原 timeline、失败、helper 证据和 cleanup 优先保留。字段完全缺失仍表示未观察到，不表示没有发生调用。成本快照不产生 readiness、控制权、事务成功或 cleanup 成功。

## 离线回归与待验证项

新增 `tests/test_managed_native_binding_costs.py`，覆盖生成 serve hook 命中真实 Runtime 闭包、独立调用的文件证明、原返回/异常 identity、成功激活仍验证四次、同一 receipt 的重复投影不新增绑定、计时失败、有限发布与正常终态、来源/权限/体积拒绝、多 source 中新 revision 快照不抹掉已认证历史、输入指纹复用和结果预算。测试使用原仓库的合成槽与临时数据库，并禁止真实子进程；没有触碰用户数据或桌面。

开发检查须通过 `scripts/check_local.py` 串行收取守卫终态。离线回归通过只证明观察器与控制流约束，不提供 hosted 成本或完整三事务达标证据。
