# 托管切换的真实进程验收

状态：两个 Linux 离线真实进程案例已通过受限检查，主体已通过独立静态审查，HTTP 总期限尾差已复验。2.22 随包声明替换原夹具声明后的两例也已复验通过；实际发行构件另由打包检查核验。产品入口来自签名接线 checkpoint `391df8316d21c003dfd32f012d765b9d9423bd5f`，并继承文件系统解析窄修 `759208809e0291ccdb0a55ab3220aa70ec4cf525`；本片不增加运行入口或服务效果。

两例都从旧实例已经停止、同一个 protocol 2 journal 已持久到 `activating/install_target` 开始。fixture 使用真实 `prepare_managed` 和合法持久状态推进，但不声称覆盖公开 prepare、原进程的 drain 或从旧安装迁入。base/target 的配置 bytes 相同，安装 prefix 和包身份不同；目标切换必须实际进入 target 解释器，不能只比较配置摘要。

测试父进程复制有界的当前 MuLi 包到两个临时保留 prefix，以当前已安装的依赖目录供其运行，不安装依赖、不扫描整个 venv、不下载候选。各 prefix 的 `compatibility.json` 直接来自当前包的真实静态资产，测试不生成或改写它；发行包是否确实包含相同资产由 [wheel smoke 与发布检查](upgrade-compatibility.md#随包声明与发行检查)另行验证，这个进程测试不代替已发布构件验证。父进程先绑定一个临时 loopback socket；稳定 launcher 用真实 argv 做同 PID exec，进入实际 `-I -m muli` CLI、`open_boot_session`、`ManagedBootPermit` 和完整 `create_app`。安装、词法解释器、实际 prefix、当前包、文件系统、七领域格式、真实 SQLite WAL/FULL 和 writer lease 都使用产品校验。临时 prefix 的 `fixture.pth` 在依赖与 helper 路径行之后，显式导入唯一命名的 test-only `_muli_managed_process_fixture_hook`；该模块仍只为 base/target 角色注入 manager/probe 观察及已绑定 socket，不替代 permit、健康 proof、nonce 路由或 activation/projection 实现。

独立 controller 子进程持同一 journal 的真实 controller lock，调用实际 `ManagedTransactionController` 和 `ManagedLinuxBackend`。只有 systemd 命令接受、loaded unit/proc 归属和 cgroup 是受限假实现；probe 的 live PID、`/proc` argv、实际 executable 和 start ticks 仍取自真实子进程。配置、selector、unit 的正序安装/逆序还原、逐项耐久确认和 writer 锁检查均不另写测试算法。controller 通过固定的 start/stop checkpoint 交给 pytest 父进程创建或收取服务进程，不派生自己的子孙进程。

成功例先验证 target 以被动 Runtime 打开原 DB，错误 nonce 返回完整宿主的 401，正确 nonce 返回真实 paused proof。controller 持久 commit 后，经实际内部 HTTP activate 启动宿主组件，再通过 released proof 完成同代 resolved。两个后续 readiness 请求调用真实宿主投影入口，最终只生成一个固定 receipt 和一个 `management.completed` 事件。

失败例让 target 在真实被动 Runtime 打开原库后，持久写入晚于 prepare 的普通对象、pending ingress 和 unknown outbox。随后只为该 target 注册一个显式不支持能力，真实 readiness 路由拒绝，controller 进入唯一回退。父进程先停止并 wait target，再让实际 backend 逆序还原 base，启动 fresh base CLI；原库的最新业务对象、ingress、outbox、未知工具回执和原 routing 均须保留。fresh base 经真实 nonce、activation 和原子失败投影完成回退，不能恢复一份旧数据库副本。

所有外部能力关闭：主模型未配置，另有模型构造反证；更新检查、MCP、目录监听、skills、插件、记忆自动提取、注意事项、桌面通知、系统事件和真实渠道均不启用。ingress/outbox 是本地持久夹具，本片不声称测试了真实账号发送或 HTTP 入站授权。恢复用固定 controller/helper 的 systemd 启动与资源认证由对应独立测试覆盖，本片直接运行真实 controller/backend。

每例最多三个 fresh 子进程（controller、target、失败时的 base），同时最多一个服务子进程；稳定 launcher 的 exec 不增加进程。执行链共享父进程 20 秒单调时限，单次父回复最多 4 秒；HTTP 从发送、读取全部响应到关闭 stream 统一由 `asyncio.timeout(min(2, remaining(deadline)))` 限制。响应和 stdout/stderr 管道各受 4 KiB 上限约束。所有 owned children 在 `finally` 中先定向 kill，再逐一 wait，关闭管道和监听 socket；不依赖外层 cgroup 清理。fixture 只在 checkout 与解析后的系统临时目录两个候选父位置中，按顺序使用真实 `verify_local_filesystems` 选择通过者，再创建自动删除的私有临时目录。只有明确的 `filesystem_unsupported` 或 `filesystem_durability_unsupported` 才尝试下一位置；观察错误立即传播，两处均不支持则以 `managed_fixture_no_supported_filesystem` 失败，不跳过。实际 boot 继续复核子目录及原文件系统合同，不把 tmpfs 或不安全挂载假称为受支持存储。

这些案例不能证明真实 systemd/cgroup、helper 资源限制、origin drain、公开 prepare/adopt/repair、跨版本发行声明、完整依赖环境、断电恢复或任何真实外部集成。只有 Linux/POSIX 平台运行；其它平台明确跳过。

两个案例分别在共享 `check_local.py --seconds 30 run` 守卫中串行执行，单次 1 GiB、合计 2 GiB、swap 0、单 CPU。HTTP 总期限尾差后的原始成功切换为 `1 passed in 7.63s`，目标拒绝后回退为 `1 passed in 12.94s`；两份 Python 的 Ruff、格式检查和逐文件 strict mypy 均通过。

2026-10-21，改用真实源资产后的成功切换为 `1 passed in 7.95s`，service 总时长 8.456 秒、峰值 173.4 MiB；目标拒绝后回退为 `1 passed in 13.15s`，service 总时长 13.650 秒、峰值 172.5 MiB。本轮五份发行及测试 Python 的 Ruff、格式和 strict mypy 均通过；mypy 使用 `MYPYPATH=src:tests` 明确解析产品和测试辅助模块，最终峰值 186.9 MiB。各检查均无 swap，未扩大原有时限或内存限额，未运行真实服务管理命令。

最初真实 CLI 启动被文件系统校验拒绝：本机 mountinfo 中无关 nsfs 的合法 mount root `net:[id]` 被当成绝对路径。该产品问题已有独立的有界反证及上述窄修，修复仅将 kernel 字段解码与实际 mountpoint 路径校验分开，仍按实际 `mnt_id`、挂载点和文件系统白名单核验；本片没有替换该校验为测试通过。完整宿主的错误 nonce 和显式不支持分别按现有 HTTP 合同断言 `401/detail=transaction_unauthorized` 和 `409/detail=maintenance_unsupported`。

Ubuntu CI 暴露了两层不同原因。3.12 的公共 `sitecustomize` 名称碰撞使原临时同名钩子未执行；3.13 已执行钩子，但 checkout 的实际文件系统观测被现有耐久性门以 `filesystem_durability_unsupported` 拒绝。当前两例都会先通过排序靠前的临时 `.pth`，把公共名称置为无操作测试模块，再由 `fixture.pth` 显式导入唯一钩子；服务侧断言并持久记录这个无操作模块确实存在，父进程逐角色核验。择址仅在两个真实父位置间选择已通过原验证者，不修改产品门、挂载、系统 Python 或用户环境。独立择址单位例用 fake 观察覆盖首位不支持、全拒绝和观察错误传播，不计为真实文件系统验收；Ubuntu CI 是否通过仍需远端复验。

该名称碰撞先在本地以无操作模块占位稳定复现：旧钩子的成功切换例在 5.14 秒内以 `managed_boot_unsupported` 失败。唯一钩子及真实择址修复后的最终检查仍逐例经过 30 秒守卫：成功切换 `1 passed in 7.77s`，service 8.303 秒、峰值 173.5M；目标拒绝后回退 `1 passed in 13.24s`，service 13.735 秒、峰值 172.8M。另有 5 项 fake 择址单位例通过（0.13 秒），不与两例真实进程证据混计。两份 Python 的 Ruff/格式与逐文件 strict mypy 通过，整轮最高峰 273.8M 来自 mypy，swap 0；独立只读尾审无阻断。上述数字没有扩大原全链时限或资源限额，也不代表远端 CI 已经通过。

实现前核对了 [Python Popen 的 pipesize/pass_fds 与超时清理合同](https://docs.python.org/3/library/subprocess.html#subprocess.Popen)、[Python isolated mode](https://docs.python.org/3/using/cmdline.html#cmdoption-I)、[Python site 的 .pth 与 sitecustomize 顺序](https://docs.python.org/3/library/site.html)、[Uvicorn programmatic server](https://www.uvicorn.org/#running-programmatically) 和 [HTTPX 异步 streaming](https://www.python-httpx.org/async/#streaming-responses)。[HTTPX read timeout 按数据块计算](https://www.python-httpx.org/advanced/timeouts/)，所以这里另加整段总期限。`-I` 会忽略 `PYTHON*` 环境，但不禁用 `site`；fixture 因此依靠临时 prefix 自己的 `.pth` 显式导入，不依赖 `PYTHONPATH` 或公共 `sitecustomize` 名称。
