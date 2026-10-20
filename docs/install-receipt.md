# 已核安装内的 receipt 观察

状态：Linux 内部读取接口和合成安装验证已实现，尚未接更新、CLI 或模型工具。它为后续 InstallProfile 提供固定来源的证据，不是完整解析上下文或升级许可；当前原位更新行为不变。

## 入口与来源

`read_install_receipt(manifest, *, uv_version, limits=IntentLimits())` 接收宿主已有的 `InstallationManifest`，返回冻结的 `InstallationReceipt`。`uv_version` 只选择已支持的 parser adapter，不证明 uv 二进制或 receipt 的历史 producer。

入口要求已有虚拟环境的 prefix 名为 `muli-agent`、区别于 base prefix、包含 pyvenv 证据，且当前 MuLi 包位于该 prefix 内。开发目录、外部包及其它布局明确不支持。随后复用 `InstallationManifest.verify()`，核对实际解释器、包、配置/数据路径和服务入口；只读取 prefix 下固定的 `uv-receipt.toml`。这个位置由 pinned [uv 工具实现](https://github.com/astral-sh/uv/blob/4ecd0eff2a81e3ffdcd7cfee10d08936e4747c17/crates/uv-tool/src/lib.rs)决定，不从 receipt 的 entrypoint 文本推导读取路径。

文件读取复用现有逐目录锚定和 `O_NOFOLLOW`/`O_NONBLOCK` 逻辑，要求当前用户拥有的普通文件；没有 mkdir、chmod、符号链接跟随、目录扫描或隐式缺库创建。读取上界沿用 `IntentLimits.max_receipt_bytes`，再交给[纯意图解析器](install-profile-design.md)。不执行 uv、Python 或 receipt 中的任何入口，不发现环境变量、用户配置、索引凭据或网络来源。

安装名、包内位置和 receipt 存在只缩小证据来源，不证明这是完整、未被手动修改的 uv 安装。manifest 当前也不认证全部依赖环境。完整宿主仍需另核 uv、resolver、Python 绑定、凭据引用和外部修改链。

## 变动检测与私有数据

读取前后各核一次安装身份，再次读取 receipt 并比较文件身份及 SHA-256。结果绑定完整 manifest digest、固定文件路径/身份/摘要、解析结果及本次预算。`snapshot.verify(manifest)` 重新执行同一观察：更换 manifest、修改程序/解释器、替换同内容文件、改变或删除 receipt 均拒绝继续使用旧意图。

这些是合作安装操作之间的重复观察，不是原子的文件系统快照，也不是防同权限恶意进程的沙箱；最后一次核验之后仍可发生变化。未来宿主必须持有相应安装排他权，在每次效果之前重新核验，不能保存一个 snapshot 后永久信任它。此读取片不创建或声称持有 uv 的工具锁。

结果状态只为 `receipt_observed`，内层仍保留 `context_required`。路径、摘要、解析意图均为宿主私有证据，默认 repr 隐藏，不直接送模型、事件或公开 API；序列化方法不是脱敏接口。错误只有固定 code，不包含文件路径、TOML 或底层异常文本。

## 验证

测试复用真实临时 `InstallationManifest` 文件夹具，组成 capture → read → parse → verify；未读取本机安装、用户 receipt 或任何凭据。覆盖原要求与 extras 保留、同内容替换、读取中替换、程序/解释器/manifest 改变、缺文件、链接/FIFO/目录、大小上限、坏 TOML，以及不支持的来源/adapter 在文件观察前拒绝。

统一 guard 下新增与既有 installation/parser 合计 186 项通过（0.34 秒、峰值 62 MiB、swap 0）；两文件 Ruff、格式及严格 mypy 通过，静态检查峰值 185.4 MiB。实际安装发现、uv 身份认证、完整上下文合并、候选安装及 A/B 切换尚未验收。
