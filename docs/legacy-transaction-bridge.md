# 失败迁入后的原服务事务

这是 V2 整合中的内部文件、Store 和 writer 证据边界；用户管理宿主负责认证进程、
持有配置锁并发布逻辑 permit。

迁入 managed 模式失败并恢复 legacy 入口后，原服务仍需能够修改配置或重启。`prepare_legacy_after_managed` 为此创建一个新的 V1 generation，不改写已完成的 V2 历史。调用宿主必须先认证当前服务进程；函数自行核验实际 writer lease、同一 Store/main session、精确终态投影回执、完整格式标记、保留安装及当前解释器。

只接受 base 为 legacy 的 aborted 或 rolled_back。unit 必须已经恢复到冻结的 base 字节，selector 必须缺席，两个 role 的历史快照均须完整。精确终态投影后允许的热配置不会被旧快照撤销：函数有界读取当前配置并通过 Settings 校验，将这些实际字节冻结为新 V1 base；restart 的 target 必须等于这个新 base。准备快照前后都确认本次冻结的配置摘要，过程中再发生变化则保留旧 state 并拒绝桥接。新记录使用当前 V1 安装清单身份，不把 V2 slot 摘要混作 V1 安装摘要。准备文件和数据库复核完毕，最后才替换同一个 state.json；上一代仍保留。不恢复旧数据库，也不执行系统服务动作。

`confirm_legacy_after_managed` 供宿主修复“state 已写而调用返回丢失”的逻辑绑定。
它与准备共用同一套旧 Store/精确回执/历史快照/安装证据校验，要求调用方先退出
poison 控制锁，在 fresh control 上取得耐久屏障。随后严格匹配本次 generation、
request/action、manifest 身份、origin boot、deadline 和本次 base/target；新 V1 base
快照、state 摘要与当前实际配置还须等于宿主冻结的授权，拒绝三者一致改写后的另一
base。已耐久发布的 installation.json 也必须等于本次 manifest。
只读确认不会覆盖账本或重开新代；外部控制者合法推进到原实例的停止观察窗口可以
续接，目标决定或其它实例阶段则拒绝。持久目录同步失败时，可见 JSON 不构成授权。

原边界的 17 项回归及协议、日志、控制器、probe 组合的 253 项检查已通过。新增回归覆盖已投影后的热配置分别继续 restart/configure、缺少投影时拒绝、restart 不能撤销热配置，以及准备中配置变化不覆盖旧记录。本轮本文件 25 项回归与 adapter、managed boot、维护门合计 106 项通过（7.38 秒、峰值 116.7 MiB），两份源码 mypy 和四份 Python 文件 Ruff/格式检查通过；最高峰值 124.8 MiB，swap 为 0。所有检查经过统一资源守卫并串行运行。这些结果只证明内部边界，完整 A/B 入口仍需运行时整合验证。

后续丢失返回确认片的 adapter/bridge 组 53 项通过（13.49 秒、峰值 97.9 MiB），
两份源码 mypy、四份 Python 文件 Ruff 通过，最高峰值 124.8 MiB、swap 0。
它验证内部同步确认与资源归属，不把尚未执行的完整宿主回归计为通过。
base 授权尾差另验 adapter 29 项及新增准备前漂移 1 项，均通过；两源码 mypy
和相关 Ruff 通过，最高峰值 124.6 MiB、swap 0。未为该尾差重跑整库。
