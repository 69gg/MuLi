# 已核安装内的 receipt 观察

状态：Linux 内部读取接口和合成安装验证已实现；2.23 开发片补充当前 managed slot 入口和可供候选宿主复用的有界观察缝，尚未接候选准备的完整运行链，不单独发版。它为后续 InstallProfile 提供固定来源的证据，不是完整解析上下文或升级许可；既有原位更新能力门不变。

## 入口与来源

`read_install_receipt(manifest, *, uv_version, limits=None)` 接收宿主已有的 V1 `InstallationManifest`，返回冻结的 `InstallationReceipt`。未给 limits 时使用 `IntentLimits()`。`uv_version` 只选择明确支持的 0.13.0 / 0.13.1 parser adapter，不证明 uv 二进制或 receipt 的历史 producer。

入口要求已有虚拟环境的 prefix 名为 `muli-agent`、区别于 base prefix、包含 pyvenv 证据，且当前 MuLi 包位于该 prefix 内。开发目录、外部包及其它布局明确不支持。随后复用 `InstallationManifest.verify()`，核对实际解释器、包、配置/数据路径和服务入口；只读取 prefix 下固定的 `uv-receipt.toml`。这个位置由 pinned [uv 工具实现](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-tool/src/lib.rs)决定，不从 receipt 的 entrypoint 文本推导读取路径。

文件读取复用现有逐目录锚定和 `O_NOFOLLOW`/`O_NONBLOCK` 逻辑，要求当前用户拥有的普通文件；没有 mkdir、chmod、符号链接跟随、目录扫描或隐式缺库创建。读取上界沿用 `IntentLimits.max_receipt_bytes`，再交给[纯意图解析器](install-profile-design.md)。不执行 uv、Python 或 receipt 中的任何入口，不发现环境变量、用户配置、索引凭据或网络来源。

安装名、包内位置和 receipt 存在只缩小证据来源，不证明这是完整、未被手动修改的 uv 安装。manifest 当前也不认证全部依赖环境。完整宿主仍需另核 uv、resolver、Python 绑定、凭据引用和外部修改链。

### 当前 managed slot

`read_managed_install_receipt(slot: SlotIdentity, *, uv_version, limits=None) -> ManagedInstallationReceipt` 读取 `slot.prefix / "uv-receipt.toml"`，前后调用真实 `verify_current_slot(slot)`。它核当前进程的词法解释器、prefix、实际 MuLi 包及 slot 文件身份，不能用共享基础 Python 或静态登记代替当前进程；prefix 可为既有受保留的自定义名称。结果的 `installation` 是 slot digest，绝不伪造 V1 manifest，也不读取固定 controller 的旧 receipt。`.verify(slot)` 必须仍是该当前 slot，不能拿未运行的候选代替。

### 候选宿主的有界观察缝

`observe_install_receipt(path: Path, *, uv_version, verify: Callable[[], None], limits=None) -> ReceiptObservation` 复用同一套普通文件读取、纯 parse 和双观察，必传 typed callback，无默认空核验。候选尚非当前进程，宿主应固定 candidate prefix 下的 receipt，并在 callback 中核静态 slot、预检证据和当前部署 binding；接收 callback 本身不认证调用来源。返回值不含安装身份，只含 `status="receipt_observed"`、`receipt: FileFingerprint`、`parsed: ParsedInstallIntent`、`limits: IntentLimits`。V1 / 当前 slot wrapper 各自在此缝之外固定来源，再追加相应安装 digest；候选宿主不能将这个通用观察结果冒称当前进程证明。

该缝前后各调用一次 callback，receipt 再次读取并比较；callback 的普通 I/O/值错误归一为 `receipt_binding_changed`，不回显错误文本。它不持有安装排他锁，也不去观察 receipt 的 entrypoint、Python 请求或 `find-links` 指向的目录。`file:///` 只是 parser 的有限语法证据，wheelhouse 所有权、来源、内容、路径效果和完整原意图→本次部署 receipt 链仍由候选宿主核验。

## 变动检测与私有数据

读取前后各核一次安装身份，再次读取 receipt 并比较文件身份及 SHA-256。结果绑定 manifest 或当前 slot digest、固定文件路径/身份/摘要、解析结果及本次预算。`snapshot.verify(manifest)` / `snapshot.verify(slot)` 用原 adapter 和原预算重新执行同一观察：更换身份、修改程序/解释器、替换同内容文件、改变或删除 receipt 均拒绝继续使用旧意图；0.13.1 结果不退回 0.13.0 重验。

这些是合作安装操作之间的重复观察，不是原子的文件系统快照，也不是防同权限恶意进程的沙箱；最后一次核验之后仍可发生变化。未来宿主必须持有相应安装排他权，在每次效果之前重新核验，不能保存一个 snapshot 后永久信任它。此读取片不创建或声称持有 uv 的工具锁。

结果状态只为 `receipt_observed`，内层仍保留 `context_required`。路径、摘要、解析意图均为宿主私有证据，默认 repr 隐藏，不直接送模型、事件或公开 API；序列化方法不是脱敏接口。错误只有固定 code，不包含文件路径、TOML 或底层异常文本。

## 验证

测试复用真实临时 `InstallationManifest` 文件夹具，组成 capture → read → parse → verify；未读取本机安装、用户 receipt 或任何凭据。覆盖原要求与 extras 保留、同内容替换、读取中替换、程序/解释器/manifest 改变、缺文件、链接/FIFO/目录、大小上限、坏 TOML，以及不支持的来源/adapter 在文件观察前拒绝。

统一 guard 下新增与既有 installation/parser 合计 186 项通过（0.34 秒、峰值 62 MiB、swap 0）；两文件 Ruff、格式及严格 mypy 通过，静态检查峰值 185.4 MiB。实际安装发现、uv 身份认证、完整上下文合并、候选安装及 A/B 切换尚未验收。

以上为原 V1 读取片历史验证。2.23 增量回归使用真实临时 slot 文件、实际 `verify_current_slot` / `verify_slot_files` 与替代的进程路径元数据，不执行候选解释器；覆盖当前 slot 与 controller 来源分离、当前身份变化、receipt 同内容替换、核验中替换、非当前候选拒绝及有界 observation callback 的失败。新的 parser 样本据已完成 uv 0.13.1 微型实验转写，私有路径和工具名已替换，非原始 receipt，也非 MuLi 安装验收。完整候选宿主接线仍属下一纵向切片。

2.23 共享 guard 定向组（parser / 两 reader / installation）共 224 项通过，用时 0.54 秒、峰值 62.4 MiB、swap 0；4 个 Python 文件 Ruff / 格式、两源码严格 mypy 通过，后者峰值 194.2 MiB、swap 0。守卫每次上限 30 秒，未增预算、未进行安装或下载。
