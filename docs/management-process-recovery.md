# Linux 进程退出后的接管回归

这份回归只检验同版本恢复协议中的进程所有权边界，不运行真实服务。对应
`tests/test_management_process_recovery.py` 与固定角色 worker
`tests/helpers/management_recovery_worker.py`；生产实现没有因此修改。

## 当前验收范围

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

## 资源与隔离

仅 Linux 收集执行这一例，其他平台明确 skip。测试最多同时存在两个子进程，不产生孙进程；
每个 worker 的检查点等待最多 10 秒。父进程的进程阶段共用 15 秒 deadline，每个 worker
最多接收 4 KiB 合并输出；读取使用有界 `os.read`，不使用无界 `readline` 或输出收集。
finally 先定向 kill 所有仍存活的自建子进程，再分别用最多 2 秒 wait 回收、关闭管道。

子进程显式使用临时 `MULI_HOME`、仓库 `src` 和 UTF-8，清除继承的 `MULI_CONFIG`、
`MULI_DATA_DIR`，并清除 `PYTHONOPTIMIZE`，保留 worker 的断言。worker 只构造
Store/journal/controller，不构造 Runtime、读取默认扩展目录、运行服务 probe/backend 或
连接账号。配置与正文均为固定测试数据，检查点不输出 nonce。

本地验证仍必须经过共享 guard，例如：

```sh
python scripts/check_local.py --seconds 30 run -- python -m pytest -q tests/test_management_process_recovery.py
```

本片已通过上述 guard 的定向验证：1 项通过，pytest 用时 0.28 秒，service 总用时
0.742 秒，峰值内存 94.2 MiB、swap 0。两个新增 Python 文件的 Ruff、格式检查与严格
mypy 通过；未扩大到整库回归，也没有实际业务服务操作。

## 不能据此声称的能力

SIGKILL 与重开 WAL 验证进程崩溃后的已提交数据可读，不等同于断电耐久或磁盘硬件屏障
验收。本例不检验真实 systemd stop/start、InvocationID/cgroup、helper 启动与监督、
manifest/解释器安装核验、nonce HTTP 激活或最终成功投影，也不检验外部消息恰好一次送达。
这些边界仍由各自现有单元测试或后续独立验收承担，不能写成“真实服务恢复已验收”。
