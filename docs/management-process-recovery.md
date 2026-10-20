# Linux 进程退出后的接管回归

这份回归检验同版本恢复协议中的进程所有权与激活重观察边界，不运行真实业务服务。对应
`tests/test_management_process_recovery.py`、`tests/test_management_activation_recovery.py`
及各自的固定角色 worker；生产实现没有因此修改。

## 案例1：writer 退出后的保守接管

测试通过两个独立解释器进程和临时目录完成以下门控流程，不依靠 sleep 猜测顺序：

1. owner 取得真实 writer lease，打开 `Store(durable=True)`，保留 WAL；建立真实 Store/main
   身份，再持有真实 journal controller 锁、写入管理 plan 和一条已提交的 pending 入站。
   `owner-ready` 只在 SQLite 提交、prepared record 和双锁都已成立后发出。
2. 独立 controller 从磁盘打开 journal，确认 controller/writer 两锁都 busy，随后关闭这些
   观察句柄并等待固定放行字节。父进程只针对自己创建的 owner 发送 SIGKILL，确认已 wait
   回收后才放行 controller。两个子进程都使用 fresh exec、`close_fds=True`，不通过 fork
   继承锁或共享 FakeService 内存。
3. controller 重新打开 journal 并取得原 controller 锁。有限 fake backend 只报告“宿主已
   停止”，实际配置摘要来自临时文件，writer 可用性必须通过取得真实 lease 证明；所有
   drain/resume/stop/install/start/activate 方法都记录失败并立即拒绝，不含 systemd、HTTP
   或服务启动器。
4. 真 `TransactionController` 将 prepared 收敛为
   `recovery_required / foreign_state`。重复 step 不再观察或产生效果；generation、身份、
   nonce、快照、原 deadline 保持，revision 只增加一次。
5. 两个子进程退出后，在真实 writer lease 下重新打开 SQLite，核对原 Store/main、管理 plan
   和 pending 入站仍存在，固定锁 inode 未被替换，没有最终投影 receipt 或
   `management.completed`。缺少宿主健康证明时不会把“取得锁”当成“恢复成功”。

## 案例2：激活调用者退出后的只读重观察

这一例只覆盖已经进入 target 验证的激活交接，不重演服务切换。父进程在临时 SQLite、
真实 writer lease 和 journal controller 锁下保存 restart plan、原渠道 `FrozenRouting`
与一条既有 unknown 工具收据，再用固定合法状态转换把 journal 置于 verifying；base 与
target 是同一份临时配置，没有 drain、stop、install 或 start 操作。

1. fresh host 取得真实 `BootPermit`，构造 `Runtime` 与 `Store(durable=True)` 并持续持有
   writer lease。最小 loopback FastAPI 直接安装现有 `install_maintenance_routes`；真实
   `Maintenance` 的激活回调只计数，不调用 `Runtime.startup` 或启动集成。配置关闭扩展
   监听、MCP、skills、更新、通知、注意事项和记忆后台工作，模型构造器也直接拒绝。
2. fresh C1 取得真实 controller 锁，先以错误 nonce 请求真实 readiness 路由并确认 403。
   随后正确 nonce 的 HTTP GET 返回宿主的实际 paused proof，真实 Controller 写入
   committed/pending 决定，再 POST activate。收到实际 released proof 后，C1 在
   Controller 的调用尚未返回时发检查点并有限等待；父进程确认 journal 仍是
   committed/pending、旧 proof 仍 paused，才对自己创建的 C1 发送 SIGKILL 并 wait。
3. fresh C2 从磁盘重新打开 journal，独立取得原 controller 锁，并通过真实 lease 冲突
   确认 host 仍占有 writer。它只用正确 nonce GET 当前 readiness，一步把 journal 记为
   released；再次 step 是纯幂等返回。C2 的 activate 与所有服务效果方法都直接拒绝，
   host 同时核对总 POST 数和激活回调数都为一，不能靠共享 FakeService 状态冒充恢复。
4. 父进程以固定单字节放行 host。host 在原 permit/Store 上调用 `project_final` 两次，
   确认第二次没有新增事件。host 正常关 Store 后才释放 writer；父进程重新取得 lease、
   打开 SQLite，核对固定 receipt 与 `management.completed` 各一份，完成事件与最终
   plan 一致、原 routing 和 unknown 收据保持。generation、challenge、原 deadline
   不变；接管只增加一条 released revision，没有重新发放期限。

## 资源与隔离

仅 Linux 执行这两例，其他平台明确 skip。案例1总计两个 fresh 子进程；案例2总计三个，
顺序为 host+C1、host+C2，因此每例最多同时存在两个子进程，不产生孙进程。
每个 worker 的检查点等待最多 10 秒。每例父进程的进程阶段共用 15 秒 deadline，每个 worker
最多接收 4 KiB 合并输出；读取使用有界 `os.read`，不使用无界 `readline` 或输出收集。
finally 先定向 kill 所有仍存活的自建子进程，再分别用最多 2 秒 wait 回收、关闭管道。

案例2只继承父进程预先绑定 `127.0.0.1:0` 的单个 socket，不继承 journal/lease 描述符；
其他子进程保持 `close_fds=True`。HTTP 禁用环境代理，每个请求总期限 2 秒、响应最多
4 KiB，分块读取；控制器整体 action 仍有独立上限，且服从原 journal deadline。错误
nonce 不输出 nonce 或完整响应。Uvicorn 关闭等待最多 2 秒；若测试失败，父进程仍定向
kill/wait 自建 host，不连接或操作用户实际服务。

子进程显式使用临时 `MULI_HOME`、仓库 `src` 和 UTF-8，清除继承的 `MULI_CONFIG`、
`MULI_DATA_DIR`，并清除 `PYTHONOPTIMIZE`，保留 worker 的断言。worker 只构造
Store/journal/controller；案例2额外构造未启动的 Runtime 和最小内部路由宿主，不运行服务
probe/backend、默认扩展扫描或连接账号。配置与正文均为固定测试数据，检查点不输出 nonce。

本地验证仍必须经过共享 guard，例如：

```sh
python scripts/check_local.py --seconds 30 run -- python -m pytest -q tests/test_management_process_recovery.py
python scripts/check_local.py --seconds 30 run -- python -m pytest -q tests/test_management_activation_recovery.py
```

案例1已通过上述 guard 的定向验证：1 项通过，pytest 用时 0.28 秒，service 总用时
0.742 秒，峰值内存 94.2 MiB、swap 0。两个新增 Python 文件的 Ruff、格式检查与严格
mypy 通过；未扩大到整库回归，也没有实际业务服务操作。

案例2也已通过 guard 定向验证：1 项通过，pytest 用时 1.60 秒，service 总用时
2.069 秒，峰值内存 155.2 MiB、swap 0。对应两个 Python 文件的 Ruff、格式与严格
mypy 通过；没有扩大进程阶段 15 秒或 guard 30 秒上限，没有运行整库或真实服务。

## 不能据此声称的能力

SIGKILL 与重开 WAL 验证进程崩溃后的已提交数据可读，不等同于断电耐久或磁盘硬件屏障
验收。案例1不检验真实 systemd stop/start、InvocationID/cgroup、helper 启动与监督、
manifest/解释器安装核验、nonce HTTP 激活或最终成功投影，也不检验外部消息恰好一次送达。
案例2额外覆盖真实 loopback nonce 路由、独立控制器的激活重观察与最终投影幂等；它的
服务 running 状态和安装身份仍是有限 fixture，不代表真实 systemd 探测、完整服务组件
激活、全 WebUI 中间件与外部账号投递已验证，也没有注入 SQLite 投影事务中途退出。
这些边界仍由各自现有单元测试或后续独立验收承担，不能写成“真实服务恢复已验收”。
