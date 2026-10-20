# 恢复事务的管理结果投影

状态：接线前的小片。`management_projection.py` 实现纯投影函数，尚未接入服务启动、管理状态入口或维护解除回调；不会停止或启动服务。journal 是恢复过程的权威，网页管理对象和通知只作投影。

`project_final(permit, store)` 只处理最新 resolved 的最终记录，不使用 `permit.record` 的启动缓存。`permit=None` 或尚无最终记录时返回 `None`。先确认真正持有目标 DB 的 writer lease，再以有限 `PRAGMA database_list` 核唯一 main 的实际路径，以 `runtime_state` 两个主键核 Store instance／主会话，并验证对应主会话对象已存在；不会调用可能建立主会话的 `Store.main_session()`。

SQLite 连接必须处于 WAL 和 `synchronous=FULL` 或 `EXTRA`；本模块只查询，不静默改动既有 Store。宿主在任何事务之前明确设置并核验 FULL，所有参与恢复的写入者都必须遵守。已有外层 `db.in_transaction` 明确拒绝；`autocommit=True` 或 `isolation_level=None` 等使现有 `Store.event` 不能合并多次写入的模式也明确 unsupported，不会替调用方提交。`read_projection(store)` 使用相同只读连接与身份检查，不把未提交或缺少持久保证的值当作覆盖旧 journal 的证据。

读取 resolved record 后同步 journal 父目录并重读，只有记录完全相同才开始投影。不能把 rename 后尚未确认落盘的 released 当成已完成；同步失败或 revision 变化不写 DB。committed 且 released／released_degraded 映射 completed；rolled_back 且已放行映射 failed，`result.restored_previous=true` 明确表示已恢复旧配置；aborted／cancelled 映射 cancelled，其他 aborted 映射 failed。blocked、recovery_required 或尚未放行不会发送 completed。

原管理计划在 prepare 时保存 `transaction.generation`。只接受同 request ID、同 action、同 generation 的计划；按主键和 journal 字节预算读取计划，不扫描管理历史。保留计划原有 `routing`，不从当前会话、默认地址或记忆重建来源。原工具消息和 unknown 收据不修改。最终计划保留原 metadata，更新状态、固定结果字段、完成时间和 `transaction` 的公共终态；`management.completed.data` 必须与这个最终计划完全一致，供现有 Attention canonical 逻辑验证。

单个 `Store.event` 事务同时写管理计划、固定对象 `kind=management_projection`／`id=transaction-v1` 的最新 `ProjectionReceipt` 与 `management.completed` 事件。receipt 只含固定 ID、generation／revision／Store instance；重复 exact receipt 直接返回，不增加通知。相同 generation 任意不同 revision 都拒绝，不回退或重发该代终态；只有验证过当前 record／计划的另一 generation 可以替换固定 receipt。提交或任一写入失败时三者一起回滚。

合法 successor 窗口是新 manifest B 已耐久、下一代 prepared 尚未落盘，journal 仍为已投影的旧 A。此时真正 DB／Store／main 和 stable resolved journal 均匹配，exact receipt 可以只读返回，不要求当前 permit.installation 仍等于旧 A；需要创建新投影时才严格要求 permit installation／service 与 record 相同。下一代 prepare 仅接受从真实 Store 读取的精确 receipt，不信任调用方口头声称“上一代已汇报”。

原配置快照清理放在投影之后的独立幂等步骤，不影响结果正确性。本片仅使用临时 Store／journal 与故障注入；后续由服务启动、管理状态读取和既有维护解除回调触发，不增加永久轮询。helper 不打开业务 Store 或写管理结果。发起工具须先完成持久回执，管理 worker 再 prepare；其实际 owner 清除前不能取得 drained，避免重启丢失自己的工具结果。

通过 Context7 核对 [Python sqlite3 事务接口](https://docs.python.org/3/library/sqlite3.html#how-to-use-the-connection-context-manager) 和 [SQLite synchronous／WAL 持久语义](https://www.sqlite.org/pragma.html#pragma_synchronous)。WAL 下 NORMAL 提交可在断电后回退；FULL 每次事务提交额外同步 WAL。这是依赖本地持久文件系统、合作进程与 SQLite VFS 正确实现 fsync 的保证，不对恶意同 uid 文件替换或不诚实存储设备作额外承诺。

受限离线验证：新投影回归与既有 maintenance／journal 回归合计 95 项通过，0.37 秒、峰值 67.6 MiB、swap 0；两份 Python Ruff／格式与单源码 mypy 通过，静态检查峰值 120.4 MiB。回归覆盖五种 resolved 结果与未放行状态、Attention 原路 canonical、实际 DB 和主身份、旧启动缓存、计划三键关联、journal 同步失败／读取变化、外层事务保持、NORMAL／OFF／DELETE／autocommit 模式拒绝、收据写入／事件插入／COMMIT 失败全回滚、同代 revision 冲突、跨代替换、manifest successor 的 exact receipt 重入，以及 unknown 工具消息不变。独立只读审查通过；未执行真实服务或 helper，也没有以单模块结果声称完整恢复闭环已交付。
