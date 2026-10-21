# 托管切换的 protocol 2 持久合同

V2 类型与纯校验已实现，通过独立审查与下述受限验证，并由启动、CLI、controller 和文件发布复用。类型校验不能独自授权安装；完整入口的整合状态见[本地托管切换](managed-operations.md)。V1 的 DTO、读取器与单安装健康判定不变。

## 同一个事务权威

`managed_transaction` 只提供 `TransactionRecordV2`、`ServiceIdentityV2`、`RoleBinding`、`HealthProofV2`、`TransitionV2`，以及纯 `parse_record_v2`、`prepare_record_v2`、`advance_record_v2`。没有另一个 journal、锁目录、文件替换器或 DB 连接。

宿主在现有 `state.json`、controller lock、writer lease 与私有目录下严格分派整数 protocol 1/2，使用原有文件写入与 fsync/poison 原语。V2 不接受 V1 JSON，V1 类型也不能解释 V2。未知、缺省或非整数 protocol 不得按旧模式尝试。`parse_record_v2` 先核既有 `JournalLimits.max_record_bytes`，再复用重复键和非有限 JSON 拒绝，失败仅返回固定原因。

纯函数收到的安装、声明、格式和投影都由宿主认证。`ProjectionReceipt` 形状相等不是证明；宿主必须在实际 Store 中读到精确终态回执，持有正确锁并核路径/身份后才传入。没有本模块替宿主完成的 lease 或耐久性捷径。

## 两个角色，各自冻结

`ServiceIdentityV2` 保存 `{deployment, store_instance, main_session, origin_boot}`。固定部署/launcher 身份与 slot 身份分开；slot 摘要不包含当前配置、unit、selector 或 boot，也不能复用包含旧 unit 的 V1 installation 摘要。

`base`、`target` 各自是 `RoleBinding`：

| 字段 | 含义 |
| --- | --- |
| `entry` | `legacy` 或 `managed`；缺少磁盘文件本身不能证明 legacy |
| `installation` | 宿主核验的 slot 身份摘要 |
| `compatibility` | 静态 `CompatibilityManifest` 的 canonical 语义摘要 |
| `configuration` | 现有 `SnapshotReference`，同时保存精确 bytes 与 canonical JSON 摘要和大小 |
| `selector` | managed 必须存在、legacy 必须为 null；仍使用 JSON `SnapshotReference` |
| `unit` | `RawSnapshotReference{digest,size}`，只引用精确 systemd 文本 bytes，不送 JSON 解析 |

`raw_snapshot(data, limit)` 接受非空 bytes 并核调用方预算与 4 MiB 上限，不解码或写文件。配置/selector 的快照继续复用 V1 JSON 规范化。`compatibility_digest`、`format_digest` 复用 `_snapshot(model.model_dump_json().encode(), 16384).semantic_digest`，不依赖字段顺序；原声明文件的 bytes 摘要另外纳入 slot 指纹，不能用语义相同掩盖构件改变。

`observed_format` 保存完整 `FormatState`。准备必须提供两份实际解析的兼容声明，核对两侧语义摘要，并执行现有 `assess_compatibility(..., handoff_protocol=2)`；七领域均须读取当前合同且写入同一合同、不需要迁移。版本号和用户批准均不能代替这个判定，更不能代替声明来源认证。

四种动作的允许变化：

- `adopt`：legacy → managed，同一 slot、声明和配置，只迁入托管入口；失败回退可以恢复无 selector 的 legacy 角色。
- `switch`：两侧 managed、不同 slot、同一稳定 unit；配置可相同，selector 与实际进程核验仍不可省略。
- `configure`：两侧 managed，只允许配置引用改变。
- `restart`：两侧 managed，完整 RoleBinding 相同。

新 request 只有在旧记录 resolved、精确 generation/revision/store 投影已提供且 Store/main 身份相同后才可准备。V1 未决不升级解释为 V2；V1 终态未投影也拒绝。重复 request 必须有相同动作、身份、两角色与格式；返回原记录、原 nonce、原 revision、原 deadline，不因重试续期。

## 阶段、角色与健康

复用 V1 的 phase/intent 边图及原期限、回退最多一次、先 committed 再激活、unknown 不自动重试的宿主语义。纯 advance 校验旧 revision、相邻 intent、健康及激活证明后，返回完整重新验证的新模型；不使用不校验更新内容的 `model_copy(update=...)`。[Pydantic 模型接口](https://docs.pydantic.dev/latest/concepts/models/#model-copy)

`boot_role` 仅在 `activating/start_target`、`verifying`、`committed` 为 target，在 `rolling_back/start_base|verify_base`、`rolled_back` 为 base，其余为空。原服务仍可提供 `role=origin` 的 readiness/drain 观察，但绝不作为 base/target 的健康证据。

`HealthProofV2` 显式包含整数 protocol 2、generation、challenge、boot、role、deployment、installation、compatibility、configuration、selector、unit、Store/main、format_digest、core_ready 和 admission。接受时全部匹配所选角色，boot 必须不同于 origin，challenge 必须是该代该角色的 nonce，不能笼统接受“两套安装之一”。legacy 回退 selector 必须为 null。paused 与 released/released_degraded 分开；blocked 激活后，同一旧 boot 的 paused 证明不能重开准入，必须由宿主观察新的实际 boot。

public 视图只给有限状态字段，不给 nonce、健康证明、安装或快照内容。纯函数不恢复配置、改变 selector、启动程序或重发 unknown。

## 宿主接线与验证边界

controller 必须同时检查配置、selector、unit 与实际安装；相同配置不能跳过程序切换。文件分别 CAS 和 file/parent 耐久确认，部分提交只允许本代冻结前缀；任何 foreign bytes 都拒绝覆盖。启动前还须核 stop、空 cgroup、writer lease，boot 时重新绑定真实安装/格式与 role；prepare 的观察不是永久许可。

稳定入口和 repair 先锚定 root，V2 未 resolved 时普通 CLI 拒绝，只保留稳定诊断/恢复；坏 selector/anchor 不得回退为未托管或重新 capture 故障安装。最终解释器选择、真实 exec 与 probe、V1/V2 journal 分派和终态投影由已接线的宿主闭环负责，验收状态见[本地托管切换](managed-operations.md)。本模块不下载、不登记候选，也不执行真实服务操作。

本片回归仅构造有限内存对象，覆盖四动作、JSON/原始 unit 引用、严格 protocol、同配置不同安装、完整 role 健康绑定、legacy 回退、激活再观察、精确投影和重试期限。`tests/test_managed_transaction.py` 的 61 项通过（0.14 秒，服务内存峰值 55.7 MiB）；两份 Python 文件 Ruff/格式检查、单源码严格 mypy 均通过，整轮最高内存 116.6 MiB、swap 为零。检查串行经过 `scripts/check_local.py`，每次外层期限 30 秒，单次 1 GiB、共享 2 GiB/单 CPU。真实子进程、系统服务与 A/B 切换证据不属于纯类型测试结果。
