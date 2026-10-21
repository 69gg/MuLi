# 托管切换的真实进程验收

状态：两个 Linux 离线真实进程案例已通过受限检查，主体已通过独立静态审查，HTTP 总期限尾差已复验。2.22 随包声明替换原夹具声明后的两例也已复验通过；实际发行构件另由打包检查核验。产品入口来自签名接线 checkpoint `391df8316d21c003dfd32f012d765b9d9423bd5f`，并继承文件系统解析窄修 `759208809e0291ccdb0a55ab3220aa70ec4cf525`；本片不增加运行入口或服务效果。

两例都从旧实例已经停止、同一个 protocol 2 journal 已持久到 `activating/install_target` 开始。fixture 使用真实 `prepare_managed` 和合法持久状态推进，但不声称覆盖公开 prepare、原进程的 drain 或从旧安装迁入。base/target 的配置 bytes 相同，安装 prefix 和包身份不同；目标切换必须实际进入 target 解释器，不能只比较配置摘要。

测试父进程复制有界的当前 MuLi 包到两个临时保留 prefix，以当前已安装的依赖目录供其运行，不安装依赖、不扫描整个 venv、不下载候选。各 prefix 的 `compatibility.json` 直接来自当前包的真实静态资产，测试不生成或改写它；发行包是否确实包含相同资产由 [wheel smoke 与发布检查](upgrade-compatibility.md#随包声明与发行检查)另行验证，这个进程测试不代替已发布构件验证。父进程先绑定一个临时 loopback socket；稳定 launcher 用真实 argv 做同 PID exec，进入实际 `-I -m muli` CLI、`open_boot_session`、`ManagedBootPermit` 和完整 `create_app`。安装、词法解释器、实际 prefix、当前包、文件系统、七领域格式、真实 SQLite WAL/FULL 和 writer lease 都使用产品校验。临时 prefix 的 `fixture.pth` 在依赖与 helper 路径行之后，显式导入唯一命名的 test-only `_muli_managed_process_fixture_hook`；只有 base/target 角色且原始解释器参数精确为本 fixture 的 `-I -m muli … serve` 时，该模块才导入 worker 并注入 manager/probe 观察及已绑定 socket。稳定 launcher 阶段不提前导入 worker 或完整服务模块，早期禁 bytecode 和无操作公共 hook 仍保留；该钩子不替代 permit、健康 proof、nonce 路由或 activation/projection 实现。

独立 controller 子进程持同一 journal 的真实 controller lock，调用实际 `ManagedTransactionController` 和 `ManagedLinuxBackend`。只有 systemd 命令接受、loaded unit/proc 归属和 cgroup 是受限假实现；probe 的 live PID、`/proc` argv、实际 executable 和 start ticks 仍取自真实子进程。配置、selector、unit 的正序安装/逆序还原、逐项耐久确认和 writer 锁检查均不另写测试算法。controller 通过固定的 start/stop checkpoint 交给 pytest 父进程创建或收取服务进程，不派生自己的子孙进程。

成功例先验证 target 以被动 Runtime 打开原 DB，错误 nonce 返回完整宿主的 401，正确 nonce 返回真实 paused proof。controller 持久 commit 后，经实际内部 HTTP activate 启动宿主组件，再通过 released proof 完成同代 resolved。两个后续 readiness 请求调用真实宿主投影入口，最终只生成一个固定 receipt 和一个 `management.completed` 事件。

失败例让 target 在真实被动 Runtime 打开原库后，持久写入晚于 prepare 的普通对象、pending ingress 和 unknown outbox。随后只为该 target 注册一个显式不支持能力，真实 readiness 路由拒绝，controller 进入唯一回退。父进程先停止并 wait target，再让实际 backend 逆序还原 base，启动 fresh base CLI；原库的最新业务对象、ingress、outbox、未知工具回执和原 routing 均须保留。fresh base 经真实 nonce、activation 和原子失败投影完成回退，不能恢复一份旧数据库副本。

所有外部能力关闭：主模型未配置，另有模型构造反证；更新检查、MCP、目录监听、skills、插件、记忆自动提取、注意事项、桌面通知、系统事件和真实渠道均不启用。ingress/outbox 是本地持久夹具，本片不声称测试了真实账号发送或 HTTP 入站授权。恢复用固定 controller/helper 的 systemd 启动与资源认证由对应独立测试覆盖，本片直接运行真实 controller/backend。

每例最多三个 fresh 子进程（controller、target、失败时的 base），同时最多一个服务子进程；稳定 launcher 的 exec 不增加进程。功能执行链共享父进程 20 秒单调时限，单次父回复最多 4 秒；HTTP 从发送、读取全部响应到关闭 stream 统一由 `asyncio.timeout(min(2, remaining(deadline)))` 限制。响应和 stdout/stderr 管道各受 4 KiB 上限约束。仅在 controller resolved 和两次 released proof 完成后，最终服务退出使用既有 `CLEANUP_SECONDS=2` 的独立有界收尾预算；回退途中停止 target、controller 与 HTTP 等待仍受原功能期限约束。退出成功后继续核真实 writer lease、DB、业务状态与唯一投影，不把发送 terminate 当作退出。所有 owned children 在 `finally` 中先定向 kill，再逐一按既有预算 wait，关闭管道和监听 socket；不依赖外层 cgroup 清理。fixture 只在 checkout 与解析后的系统临时目录两个候选父位置中，按顺序使用真实 `verify_local_filesystems` 选择通过者，再创建自动删除的私有临时目录。只有明确的 `filesystem_unsupported` 或 `filesystem_durability_unsupported` 才尝试下一位置；观察错误立即传播，两处均不支持则以 `managed_fixture_no_supported_filesystem` 失败，不跳过。实际 boot 继续复核子目录及原文件系统合同，不把 tmpfs 或不安全挂载假称为受支持存储。

CI 还可显式指定 `MULI_MANAGED_TEST_ROOT`。存在该变量时，仅对给定绝对目录执行同一真实文件系统核验，失败立即传播，不回退到本地候选目录。Linux 托管 runner 在自己的临时目录创建一个 64 MiB 稀疏 ext4 镜像，以 `loop,nodev,nosuid,noatime` 挂载到唯一测试目录，再将卷根交 runner 用户并设为 0700。setup 输出精确挂载点、文件系统、选项及空间/inode 余量，真实核验后才开始测试；`always` 步骤按固定路径卸载，不依赖 setup 最后才导出的环境变量。卸载失败会使步骤失败，不使用强制、lazy 卸载或删除已挂载目录。这个夹具只供应实际可准入的临时卷，不证明 backing filesystem 或宿主存储能承受断电。本机受限检查不创建镜像、不挂载文件系统。

这些案例不能证明真实 systemd/cgroup、helper 资源限制、origin drain、公开 prepare/adopt/repair、跨版本发行声明、完整依赖环境、断电恢复或任何真实外部集成。只有 Linux/POSIX 平台运行；其它平台明确跳过。

两个案例分别在共享 `check_local.py --seconds 30 run` 守卫中串行执行，单次 1 GiB、合计 2 GiB、swap 0、单 CPU。HTTP 总期限尾差后的原始成功切换为 `1 passed in 7.63s`，目标拒绝后回退为 `1 passed in 12.94s`；两份 Python 的 Ruff、格式检查和逐文件 strict mypy 均通过。

2026-10-21，改用真实源资产后的成功切换为 `1 passed in 7.95s`，service 总时长 8.456 秒、峰值 173.4 MiB；目标拒绝后回退为 `1 passed in 13.15s`，service 总时长 13.650 秒、峰值 172.5 MiB。本轮五份发行及测试 Python 的 Ruff、格式和 strict mypy 均通过；mypy 使用 `MYPYPATH=src:tests` 明确解析产品和测试辅助模块，最终峰值 186.9 MiB。各检查均无 swap，未扩大原有时限或内存限额，未运行真实服务管理命令。

最初真实 CLI 启动被文件系统校验拒绝：本机 mountinfo 中无关 nsfs 的合法 mount root `net:[id]` 被当成绝对路径。该产品问题已有独立的有界反证及上述窄修，修复仅将 kernel 字段解码与实际 mountpoint 路径校验分开，仍按实际 `mnt_id`、挂载点和文件系统白名单核验；本片没有替换该校验为测试通过。完整宿主的错误 nonce 和显式不支持分别按现有 HTTP 合同断言 `401/detail=transaction_unauthorized` 和 `409/detail=maintenance_unsupported`。

Ubuntu CI 暴露了两层不同原因。3.12 的公共 `sitecustomize` 名称碰撞使原临时同名钩子未执行；3.13 已执行钩子，但 checkout 的实际文件系统观测被现有耐久性门以 `filesystem_durability_unsupported` 拒绝。当前两例都会先通过排序靠前的临时 `.pth`，把公共名称置为无操作测试模块，再由 `fixture.pth` 显式导入唯一钩子；服务侧断言并持久记录这个无操作模块确实存在，父进程逐角色核验。择址仅在两个真实父位置间选择已通过原验证者，不修改产品门、挂载、系统 Python 或用户环境。独立择址单位例用 fake 观察覆盖首位不支持、全拒绝和观察错误传播，不计为真实文件系统验收；Ubuntu CI 是否通过仍需远端复验。

该名称碰撞先在本地以无操作模块占位稳定复现：旧钩子的成功切换例在 5.14 秒内以 `managed_boot_unsupported` 失败。唯一钩子及真实择址修复后的最终检查仍逐例经过 30 秒守卫：成功切换 `1 passed in 7.77s`，service 8.303 秒、峰值 173.5M；目标拒绝后回退 `1 passed in 13.24s`，service 13.735 秒、峰值 172.8M。另有 5 项 fake 择址单位例通过（0.13 秒），不与两例真实进程证据混计。两份 Python 的 Ruff/格式与逐文件 strict mypy 通过，整轮最高峰 273.8M 来自 mypy，swap 0；独立只读尾审无阻断。上述数字没有扩大原全链时限或资源限额，也不代表远端 CI 已经通过。

后续 [main CI 39539335917](https://github.com/69gg/MuLi/actions/runs/39539335917) 的 Ubuntu 3.13 回退例已完成 controller resolved 和两次 released proof，但最终服务退出仍借用功能期限，当时仅剩约 32 毫秒，`Popen.wait` 因此超时。本轮只将这一步改用既有独立收尾预算，不改变 20 秒功能期限、中途停止 target 或任何恢复断言。4 项不启动进程、不睡眠的确定性回归通过（0.14 秒），覆盖旧剩余预算表达式必然超时、功能期限耗尽后的独立收尾、非预期退出码和收尾超时不续期。

本轮仅真实回退参数经 30 秒守卫复验，`1 passed in 14.86s`，service 总时长 15.378 秒、峰值 182.4M；退出后原 writer lease、最新 DB/ingress/outbox/routing 和唯一投影校验全部保留。单文件 Ruff、格式和 strict mypy 均通过，mypy 仍用 `MYPYPATH=src:tests`，峰值 264.4M；全部检查 swap 0。两文件独立静态审查通过；本轮没有重跑真实成功参数，远端 CI 仍待修复提交后的复验。

2.26 首次候选 CI 的两个 Windows 作业暴露参数装饰器提前访问 `signal.SIGKILL` 的收集错误；模块执行期 skip 不能阻止该访问。参数改为退出类别，Linux 信号延后到测试函数体，仍核非零退出与被杀两种失败。受限子进程删除该常量后导入旧代码稳定失败、修后通过；四项 Linux 确定性回归通过（0.17 秒、63.1 MiB、swap 0），静态与独审通过。这不是 Windows 本地验收，远端完整矩阵须在新提交重新验证。

实现前核对了 [Python Popen 的 pipesize/pass_fds 与超时清理合同](https://docs.python.org/3/library/subprocess.html#subprocess.Popen)、[Python isolated mode](https://docs.python.org/3/using/cmdline.html#cmdoption-I)、[Python site 的 .pth 与 sitecustomize 顺序](https://docs.python.org/3/library/site.html)、[Uvicorn programmatic server](https://www.uvicorn.org/#running-programmatically) 和 [HTTPX 异步 streaming](https://www.python-httpx.org/async/#streaming-responses)。[HTTPX read timeout 按数据块计算](https://www.python-httpx.org/advanced/timeouts/)，所以这里另加整段总期限。`-I` 会忽略 `PYTHON*` 环境，但不禁用 `site`；fixture 因此依靠临时 prefix 自己的 `.pth` 显式导入，不依赖 `PYTHONPATH` 或公共 `sitecustomize` 名称。CI 的独立卷使用 [GitHub 托管 Linux VM 的 sudo](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#administrative-privileges) 和 [util-linux 的 loop 文件挂载及自动分离](https://github.com/util-linux/util-linux/blob/master/sys-utils/mount.8.adoc#loop-device-support)，不改宿主现有挂载。

2.32 首次候选 `6a8ad38e7899b70cb34ac91f0b9a6570bcf1faa9` 的
[CI 39554055600](https://github.com/69gg/MuLi/actions/runs/39554055600) 为七项通过、一项失败；
唯一失败是 Ubuntu 3.13 的真实回退参数。target 已拒绝、退出，base 已启动并返回 paused，
但父进程在原 20 秒期限内未等到 `controller-resolved`。它没有走到最终服务收尾；
不能将此失败与前述 32 毫秒 cleanup 问题混同。日志中的 driver `-9` 是 finally 收取后的
进程状态，也不足以证明 driver 自行遭遇 SIGKILL。两个 Windows job 全部通过，包含三个
在 Windows Actions 启用的 UIA 原生用例；该失败未发生于 UIA 原生用例。

一次带临时阶段观测的本地原始回退例通过（13.65 秒，service 14.171 秒，CPU 13.113 秒，
峰值 179.7 MiB、swap 0），base ready 在约 11.55 秒，resolved 在约 13.06 秒。
两个临时 MuLi 包各生成了 119 个 `.pyc`：`-I` 同时隐含 `-E`，所以环境中的
`PYTHONDONTWRITEBYTECODE` 没有效果。这是已证实的测试额外写入；上述单次成功和远端
原有有限日志尚不能将其认定为远端超时的唯一根因，也不能证明控制器固定停在某一 phase。

当前仅把 `sys.dont_write_bytecode = True` 加入原本排序最早的测试专用 `.pth`，
在后续 fixture 模块及 MuLi 导入前关闭 bytecode 写入。真实两例都核验各自专用 MuLi 包
与专用 fixture 模块未生成 `.pyc`，不承诺 `.pth` 执行前的系统模块也无写入。两个微型
真实 `-I` 子进程作确定性对照：仅设环境变量仍产生缓存，采用该早期 hook 后不产生缓存；
旧 hook 的新断言先稳定失败（0.20 秒、64.6 MiB、swap 0）。不修改产品 fingerprint、
启动准入、原 20 秒期限、各阶段等待和最终业务断言。

失败时原异常附一条完整 JSON 的 `managed_recovery_diagnostic` note，总 UTF-8 不超过
既有 4 KiB 预算。它保留最多 16 个固定阶段名和毫秒时刻、当前剩余期限、journal 的
revision/phase/intent/activation/code，以及 base/target 的 HTTP posts/started/allowed
固定标量。SQLite 按两个固定主键读取至多 4097 字节哨兵，不加载整库或输出正文；
只读连接的 busy timeout 为零，锁冲突记为 unavailable，不等待业务 writer。
坏记录、缺失证据和超预算同样使用固定状态；不输出私有路径、完整 journal/数据库或异常
正文，不改变原异常及 finally 的 owned 子进程回收，也不新增持久诊断文件或扫描框架。

该窄修的 20 项单位回归通过（含 5 项新增，0.19 秒、峰值 63.7 MiB），涵盖真实
`-I` 对照、固定标量白名单、超预算完整 JSON 和真实 SQLite exclusive lock 下的
nonblocking 读取。两个原真实参数分开经 30 秒守卫：成功切换 `1 passed in 8.31s`，
service 8.798 秒、CPU 8.369 秒、峰值 178.4 MiB；目标拒绝后回退
`1 passed in 13.78s`，service 14.273 秒、CPU 13.574 秒、峰值 171.7 MiB。
两例新增的专用包/fixture 模块无缓存断言均通过；全部 swap 0，未运行真实服务管理命令。
本轮数字不构成相同 runner 的性能对照，不声称该修复已解决远端唯一根因。
单文件 Ruff、格式检查和 strict mypy 均通过；mypy 使用 `MYPYPATH=src:tests`，
service 2.030 秒、峰值 254.6 MiB，swap 0。独立静态审查无阻断；远端新候选仍须重新验收。

PR37 第四轮精确候选 `e1bb47b` 的
[Ubuntu 3.13 作业 119129953012](https://github.com/69gg/MuLi/actions/runs/39572518905/job/119129953012)
再次在原 20 秒功能期限内未等到回退 resolved：target 启动请求在 6.504 秒、ready 在
8.929 秒，base 启动请求在 16.599 秒、ready 在 18.982 秒、paused 在 19.090 秒；
最终 journal 为 `rolling_back/verify_base`、activation pending、两个角色均未 POST activate。
它没有进入最终服务收尾。源码另证实一个夹具额外导入：原钩子仅检查 base/target 角色，
所以固定 controller Python 执行 standalone launcher 时已经导入完整 worker/server，
同 PID exec 真正 slot CLI 后又导入一遍；第一遍导入随后被 exec 丢弃。
当前只将钩子收紧为 [`sys.orig_argv`](https://docs.python.org/3/library/sys.html#sys.orig_argv)
的精确 CLI 参数后缀，不改产品 launcher、backend、包指纹校验、期限或任何恢复断言。
两段服务启动等待也包含真实 boot 工作，启动请求前还有实际 install/confirm；
现有日志不能分解这些成本，更不能将两次约 6.5 秒等待全部归因于重复导入。

最终 2.33 精确签名 `1cf078dbd31c4f71054328de9cf02a46f2805d8f` 的候选
`39576755760`、main `39578053330` 与发行 `39578052294` 均在 attempt 1
全部通过，包含 Ubuntu 3.12/3.13 原合成托管进程恢复测试；原 20 秒与业务断言
未放宽。完整证据见[2.33 审查](review-v2.33.md#精确发行验收)。这不等于独立
PR37 的原生 systemd 服务整链已通过，也不把旧超时全部归因于测试 hook。

直接执行同一生成钩子的纯 argv 反例在旧实现稳定失败：launcher 仍调用一次
`install_hooks`（0.19 秒、62.1 MiB）。修后 10 项通过（0.16 秒、61.3 MiB），
既断言 launcher/controller 不导入 worker，也覆盖 base/target 真 CLI、未知角色、错误配置
路径、缺少隔离参数、其它命令和额外参数；模块导入与安装函数均为受控纯替身，不启动子进程。
单文件 Ruff、格式和 strict mypy 通过，mypy 使用 `MYPYPATH=src:tests`，service 1.900 秒、
峰值 272.9 MiB，全部 swap 0。每项检查仍在原 30 秒资源守卫内完成。
本轮没有运行真实成功/回退参数；冷启动减幅与完整真实链须由新精确 CI 在原 20 秒内验收，
不以纯 argv 回归代替真实 CLI、nonce、DB 或进程收取证据。
