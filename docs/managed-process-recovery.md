# 托管切换的真实进程验收

状态：两个 Linux 离线真实进程案例已通过受限检查，主体已通过独立静态审查，HTTP 总期限尾差已复验。产品入口来自签名接线 checkpoint `391df8316d21c003dfd32f012d765b9d9423bd5f`，并继承文件系统解析窄修 `759208809e0291ccdb0a55ab3220aa70ec4cf525`；本片仅增加两个测试案例、专用 worker 和本文，不增加运行入口或服务效果。

两例都从旧实例已经停止、同一个 protocol 2 journal 已持久到 `activating/install_target` 开始。fixture 使用真实 `prepare_managed` 和合法持久状态推进，但不声称覆盖公开 prepare、原进程的 drain 或从旧安装迁入。base/target 的配置 bytes 相同，安装 prefix 和包身份不同；目标切换必须实际进入 target 解释器，不能只比较配置摘要。

测试父进程复制有界的当前 MuLi 包到两个临时保留 prefix，以当前已安装的依赖目录供其运行，不安装依赖、不扫描整个 venv、不下载候选。各 prefix 的 `compatibility.json` 是这次同一 checkout 的测试声明，不是已发行包提供了该声明的证据。父进程先绑定一个临时 loopback socket；稳定 launcher 用真实 argv 做同 PID exec，进入实际 `-I -m muli` CLI、`open_boot_session`、`ManagedBootPermit` 和完整 `create_app`。安装、词法解释器、实际 prefix、当前包、文件系统、七领域格式、真实 SQLite WAL/FULL 和 writer lease 都使用产品校验。临时 prefix 的 test-only `sitecustomize` 注入 manager/probe 观察及已绑定 socket，不替代 permit、健康 proof、nonce 路由或 activation/projection 实现。

独立 controller 子进程持同一 journal 的真实 controller lock，调用实际 `ManagedTransactionController` 和 `ManagedLinuxBackend`。只有 systemd 命令接受、loaded unit/proc 归属和 cgroup 是受限假实现；probe 的 live PID、`/proc` argv、实际 executable 和 start ticks 仍取自真实子进程。配置、selector、unit 的正序安装/逆序还原、逐项耐久确认和 writer 锁检查均不另写测试算法。controller 通过固定的 start/stop checkpoint 交给 pytest 父进程创建或收取服务进程，不派生自己的子孙进程。

成功例先验证 target 以被动 Runtime 打开原 DB，错误 nonce 返回完整宿主的 401，正确 nonce 返回真实 paused proof。controller 持久 commit 后，经实际内部 HTTP activate 启动宿主组件，再通过 released proof 完成同代 resolved。两个后续 readiness 请求调用真实宿主投影入口，最终只生成一个固定 receipt 和一个 `management.completed` 事件。

失败例让 target 在真实被动 Runtime 打开原库后，持久写入晚于 prepare 的普通对象、pending ingress 和 unknown outbox。随后只为该 target 注册一个显式不支持能力，真实 readiness 路由拒绝，controller 进入唯一回退。父进程先停止并 wait target，再让实际 backend 逆序还原 base，启动 fresh base CLI；原库的最新业务对象、ingress、outbox、未知工具回执和原 routing 均须保留。fresh base 经真实 nonce、activation 和原子失败投影完成回退，不能恢复一份旧数据库副本。

所有外部能力关闭：主模型未配置，另有模型构造反证；更新检查、MCP、目录监听、skills、插件、记忆自动提取、注意事项、桌面通知、系统事件和真实渠道均不启用。ingress/outbox 是本地持久夹具，本片不声称测试了真实账号发送或 HTTP 入站授权。恢复用固定 controller/helper 的 systemd 启动与资源认证由对应独立测试覆盖，本片直接运行真实 controller/backend。

每例最多三个 fresh 子进程（controller、target、失败时的 base），同时最多一个服务子进程；稳定 launcher 的 exec 不增加进程。执行链共享父进程 20 秒单调时限，单次父回复最多 4 秒；HTTP 从发送、读取全部响应到关闭 stream 统一由 `asyncio.timeout(min(2, remaining(deadline)))` 限制。响应和 stdout/stderr 管道各受 4 KiB 上限约束。所有 owned children 在 `finally` 中先定向 kill，再逐一 wait，关闭管道和监听 socket；不依赖外层 cgroup 清理。fixture 在 checkout 同盘的私有临时目录内自动删除，避免把 tmpfs 假称为受支持的耐久事务文件系统。

这些案例不能证明真实 systemd/cgroup、helper 资源限制、origin drain、公开 prepare/adopt/repair、跨版本发行声明、完整依赖环境、断电恢复或任何真实外部集成。只有 Linux/POSIX 平台运行；其它平台明确跳过。

两个案例分别在共享 `check_local.py --seconds 30 run` 守卫中串行执行，单次 1 GiB、合计 2 GiB、swap 0、单 CPU。HTTP 总期限尾差后的成功切换为 `1 passed in 7.63s`，service 总时长 8.149 秒、峰值 172.8 MiB；目标拒绝后回退为 `1 passed in 12.94s`，service 总时长 13.451 秒、峰值 172.6 MiB。两份 Python 的 Ruff、格式检查和逐文件 strict mypy 均通过；mypy 使用 `MYPYPATH=src:tests` 明确解析产品和测试辅助模块，最终静态检查峰值 189.1 MiB。各检查均无 swap，未扩大原有时限或内存限额。

最初真实 CLI 启动被文件系统校验拒绝：本机 mountinfo 中无关 nsfs 的合法 mount root `net:[id]` 被当成绝对路径。该产品问题已有独立的有界反证及上述窄修，修复仅将 kernel 字段解码与实际 mountpoint 路径校验分开，仍按实际 `mnt_id`、挂载点和文件系统白名单核验；本片没有替换该校验为测试通过。完整宿主的错误 nonce 和显式不支持分别按现有 HTTP 合同断言 `401/detail=transaction_unauthorized` 和 `409/detail=maintenance_unsupported`。

实现前核对了 [Python Popen 的 pipesize/pass_fds 与超时清理合同](https://docs.python.org/3/library/subprocess.html#subprocess.Popen)、[Python isolated mode](https://docs.python.org/3/using/cmdline.html#cmdoption-I)、[Uvicorn programmatic server](https://www.uvicorn.org/#running-programmatically) 和 [HTTPX 异步 streaming](https://www.python-httpx.org/async/#streaming-responses)。[HTTPX read timeout 按数据块计算](https://www.python-httpx.org/advanced/timeouts/)，所以这里另加整段总期限。`-I` 会忽略 `PYTHON*` 环境，因此 fixture 明确依靠临时 prefix 自己的 `.pth`/`sitecustomize`，不依赖 `PYTHONPATH` 注入。
