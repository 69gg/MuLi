# 有界在线候选材料

本片只自动取得指定版本的一枚 `muli-agent` universal wheel。依赖仍来自主人明确配置并已批准的本地 wheelhouse；后续 uv 始终 `--offline --no-index`，使用每次 attempt 的私有缓存。缺依赖会失败并保留当前安装，不宣称任意版本的完整在线更新。

在线策略是 Store 中独立、严格且有界的对象，默认关闭，不扩展主 Settings 或固定恢复控制器的格式合同。来源为匿名 HTTPS 的版本 JSON 入口及明确允许的构件 origin；不支持凭据、重定向、HTTP 内容压缩或自动重试。HTTPX 使用 `trust_env=False`，不采用其环境代理设置，但不能保证绕过系统 TUN、网关或其它网络路径。HTTP body 按实际读取累计；这不是 TCP/TLS 流量或计费配额。下载默认 8 MiB、最大 32 MiB，并受原 wheel/总材料上限约束；元数据大小和文件数也可配置，期限和磁盘阈值复用候选预算。

`self_update_preview({"version":"精确版本"})` 在实际当前安装、格式、原安装意图、本地材料和策略核验后，返回既有 `expected_policy` 与完整 `expected_artifact`：来源、精确版本、URL、filename、size、SHA256、yanked、Requires-Python、在线策略 revision 和当前候选 policy revision；后者也绑定实际安装、binding、原意图、本地材料与完整格式摘要。预览仅读取小 JSON，不下载 wheel，不证明候选已兼容。该工具使用现有 `preserve_output` 合同：整份结果放不下时明确拒绝交付，不把截坏 JSON 当作下一轮审批事实。`self_update` 的原有真实用户审批展示这份参数；handler 重取同版本有限元数据并精确比较，模型提交的字段只是前置条件。事实变化必须重新预览及批准。

首次持久管理计划仍为 `action=switch, operation=update`，先保存批准的构件事实，再开始下载。worker 仅消费该计划，不重新选择版本或文件。整个 attempt 从分配目录前到下载、四步离线准备共用一个绝对期限，客户端构造也计入；网络关闭另用既有 `stop_seconds` 的同一个绝对收尾期限覆盖 response 和 client，重复取消仍等待同一 owned task。关闭失败不允许材料 accepted 或启动 uv，不重置 attempt 的期限。所用 HTTPX/AnyIO 关闭响应取消；此边界不是强行终止任意吞取消的第三方 transport。`.part` 从首字节纳入既有 owned 磁盘观察；取消时关闭网络及文件后才结束 owned worker。下载文件不覆盖原 wheelhouse、当前/控制器/上一可用 slot；大小和 hash 一致、实际 ZIP 结构、METADATA 身份/版本/Requires-Python 及包内 `muli/compatibility.json` 七领域声明全部核验后，才以不可覆盖的方式发布到私有材料目录。此时仍未执行候选代码。

下载的 pending/accepted 状态保存在同一个管理 plan，文件先 fsync，再确认材料状态；回执保存与更新计划最终收尾均拒绝调用者活跃事务及不支持的 SQLite 自动提交模式，不抢先提交外层业务。收尾拒绝发生在删除配置快照或写事件之前，原持久计划与调用者事务保持不变，执行占用仍释放。提交成功而返回丢失时保留真实 plan 附件，不用旧内存覆盖。重启不续期、不恢复下载、不重装：无进程效果的未完成准备终止为 interrupted 并保留材料；已有进程 pending 则沿现有 cleanup recovery，已 prepared 则沿原 V2 事务恢复。`.part` 的存在不是接受或重试授权。

实际兼容判定复用当前 Store 的完整格式证据、当前实际 slot 声明及 `assess_compatibility(..., handoff_protocol=2)`。不以 PyPI 项目 JSON 的 info 声明代替 wheel 内 METADATA，不进行格式迁移，不修改 selector/controller。原离线入口和 lineage 保留，下载文件的最终指纹与原材料摘要共同纳入原候选材料证据。在线路径在下载前拒绝本地同项目、同目标版本的任何竞争 wheel（包括其它 build/tag），防止 uv 选择批准事实之外的构件；原依赖、下载 wheel 的压缩/声明解压字节与成员数按同一材料预算累计，空间预留也包含下载大小。

源码接缝为独立在线策略/元数据模块、单文件下载模块、候选 attempt 的共同期限与预执行材料接纳，以及 SelfManagement/既有认证管理 API 的窄接入。不增加通用 resolver、第二账本、凭据平台或全局 cache 适配器。

依据：[PyPI 版本 JSON](https://docs.pypi.org/api/json/)、[HTTPX 原始流](https://www.python-httpx.org/quickstart/#streaming-responses)、[环境变量隔离](https://www.python-httpx.org/environment_variables/)、[uv cache](https://docs.astral.sh/uv/concepts/cache/)。HTTPX 的流与显式关闭合同已通过 Context7 及本机已安装 HTTPX/httpcore/AnyIO 源码核对。

离线回归分别验证客户端构造耗尽绝对期限，以及已进入响应后的真实计时器取消和关闭，避免将启动开销误当成响应停滞。关闭失败、重复取消和持久收尾失败均须保留原错误及所有权；不延长产品期限或让清理成功代替材料接纳。

网络使用 MockTransport 和小型合成 wheel，Store、文件身份与管理来源核验使用真实临时文件和 SQLite；所有候选进程默认硬拒意外 spawn。已覆盖事实漂移、同版本竞争材料、原始错误 body 限额、关闭失败与重复取消、兼容拒绝、提交丢返回及重启不重装。没有真实构件下载、uv 安装、系统服务切换或发布平台矩阵验收，不把这些回归称为完整在线更新成功。
