# 失败迁入后的原服务事务

这是 V2 整合中的内部边界，尚未连接用户管理入口。

迁入 managed 模式失败并恢复 legacy 入口后，原服务仍需能够修改配置或重启。`prepare_legacy_after_managed` 为此创建一个新的 V1 generation，不改写已完成的 V2 历史。调用宿主必须先认证当前服务进程；函数自行核验实际 writer lease、同一 Store/main session、精确终态投影回执、完整格式标记、保留安装及当前解释器。

只接受 base 为 legacy 的 aborted 或 rolled_back。unit 必须已经恢复到冻结的 base 字节，selector 必须缺席，两个 role 的历史快照均须完整。精确终态投影后允许的热配置不会被旧快照撤销：函数有界读取当前配置并通过 Settings 校验，将这些实际字节冻结为新 V1 base；restart 的 target 必须等于这个新 base。准备快照前后都确认本次冻结的配置摘要，过程中再发生变化则保留旧 state 并拒绝桥接。新记录使用当前 V1 安装清单身份，不把 V2 slot 摘要混作 V1 安装摘要。准备文件和数据库复核完毕，最后才替换同一个 state.json；上一代仍保留。不恢复旧数据库，也不执行系统服务动作。

原边界的 17 项回归及协议、日志、控制器、probe 组合的 253 项检查已通过。新增回归覆盖已投影后的热配置分别继续 restart/configure、缺少投影时拒绝、restart 不能撤销热配置，以及准备中配置变化不覆盖旧记录。本轮本文件 25 项回归与 adapter、managed boot、维护门合计 106 项通过（7.38 秒、峰值 116.7 MiB），两份源码 mypy 和四份 Python 文件 Ruff/格式检查通过；最高峰值 124.8 MiB，swap 为 0。所有检查经过统一资源守卫并串行运行。这些结果只证明内部边界，完整 A/B 入口仍需运行时整合验证。
