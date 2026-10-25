# 2.42：队友资料显式交接

状态：已正式发布。最终精确签名 `ed2b267` 的候选/main各十项及发行十三项全部成功，PyPI/GitHub两构件大小与SHA-256一致。上一片 2.41 的候选/main绿色与发行UIA超时分别保留，见[对应审查](review-v2.41.md)。本版包含其未发行的资料收件目录；不覆盖原签名标签，也不将旧失败改写成成功。

## 行为与范围

主统筹可把主人上传或移动渠道送来的资料，通过现有 `spawn_agent`、`send_message`、`send_input` 的 `attachment_ids` 显式交给选定队友；认证 Team 消息 API 复用同一校验与邮箱。引用上限沿既有 32 条，每次全部验证本地 ID、元数据和当前普通文件后，和文本及派工对象同一事务保存。没有父历史扫描、正文自动读取、附件复制、模型下载、新数据库或 ACL。

队内来源仍为 agent，附件内容不会变成新的主人授权。队友在现有安全边界取补充，不取消正在进行的流式或工具；新移动输入不改变派工冻结的 ReplyOrigin。文件、图像和音频继续通过既有工具读取，长转写沿原 transcript revision 与字节游标续读。

压缩后的 `conversation_read`、正文页与网页归档完整返回原消息的结构化引用，缺字段为 `[]`。保留通用信封原先的有限 opaque 字符串和重复顺序，只有新本地 Team/Web 入口要求小写十六进制、唯一且文件可用。引用不能按可选字段省略；不完整 SQL 字节投影或最终 JSON 超额明确 `output_budget`，损坏形状/编码为 `content_unavailable`。持久格式、主配置和 DDL 不变。

使用与设计分别见[资料交接](teammate-attachments.md)和[设计依据](teammate-attachments-design.md)。附件库原本按实例全局读取，显式交接不构成按队友隔离的文件权限。引用被接纳不保证文件永久存在、不变或已由模型读过。

## 独立审查与修复

先复核原有通用信封，撤回把所有持久历史追溯收紧为 32 位十六进制的初稿；保留旧 opaque/重复引用。独立只读后审发现损坏 SQLite JSON 可包含转义孤立 surrogate，解码后通过字符串 shape，却在 HTTP UTF-8 编码或工具结果保存时抛出裸异常。增加逐引用严格编码检查，并用 raw SQL 注入、真实有界投影与三个读取入口验证固定错误。

另保留 `RecursionError` 防御捕获；当前 Python 3.13.11/SQLite 3.50.4 的默认解码器在 SQLite 允许的 900–1000 层探针成功解码，1001 层 SQLite 已拒绝，没有冒称当前默认解码器已复现递归错误。900 层非字符串数组仍按原信封 shape 返回固定损坏错误，不替换 scanner 或调整递归限制制造成功。

根复核入队前无新增 await、引用和派工对象同事务、工具 caller 与目的地不接受模型覆盖、SQL 先限定 session/seq 再有界投影，未发现剩余必须修复的 P1/P2。真实 Runtime 场景没有替换 submit、Team.send、附件读取 handler 或报告入库。

## 本地验证

全部使用统一 `check_local.py` 串行守卫，单次 1 GiB、共享 2 GiB、swap 0、共享单 CPU；只使用临时文件/SQLite、固定 FakeClient 和 HTTP Mock，不操作真实账号、麦克风或桌面。

实现者基础派工、补充、认证 API 与旧行为 54 项通过；独立完整场景及历史/压缩 75 项、相关 SQLite/归档/回放 77 项通过。损坏编码修复后的历史/SQLite/Runtime 67 项通过，Ruff、格式、生产类型检查均退出 0；这些集合有重叠，不累加为独立总数。

根对冻结生产及提示词整合检查十文件 **146 passed**，pytest 2.67 秒，守卫 3.551 秒、峰值 190.5 MiB、swap 0、实际退出 0。覆盖 Web 上传派工、签名移动 webhook 补充与重复事件、ASR 工具批阻塞期间不插入、批后完整转写续读、原 Web 报告；wake=false 邮箱跨重开、真实压缩摘要故意不含 ID 后按原 seq 找回；主 vision 和独立 vision 两种实际 ModelClient HTTP 请求路径。

后续全量回归、构件、签名、精确候选/main/发行和元数据结果在实际完成后记录，不以本节定向通过代替。

冻结源码整合后的全量后端覆盖 **210 个文件，5,132 passed / 11 skipped**。四个互不重叠的守卫串行执行，每个守卫内部仍至多32文件一批：

| 文件范围 | pytest 完成数 | 守卫时间 | 峰值内存 |
| --- | --- | --- | --- |
| 1–64 | 850 + 783 passed，1 skipped | 82.551 秒 | 639.6 MiB |
| 65–128 | 978 + 717 passed | 134.860 秒 | 606.8 MiB |
| 129–192 | 688 + 722 passed | 35.622 秒 | 751.5 MiB |
| 193–210 | 394 passed，10 skipped | 6.624 秒 | 331.5 MiB |

四守卫均实际正常退出0、swap0，未增加时间、内存或并发上限；平台跳过不算原生实测。只保留一条既有畸形 MCP policy 夹具的 Pydantic 序列化 warning。全仓 Ruff、368 文件格式、146 生产文件 strict mypy、离线83包锁校验和差异空白检查共用30秒守卫通过，8.049秒、392.1 MiB。前端文件未改动，沿用上一精确代码281项通过的证据；远端 quality 仍会独立执行前端检查。

首次误用 `--no-build-isolation`，此 worktree 解释器没有 hatchling，未构建；随后两项依赖构件的检查因目录为空失败，不计为验证通过。改用仓库既有 `uv build --offline` 与缓存的隔离构建环境，wheel/sdist 成功，1.293秒、100.3 MiB；未安装或下载新依赖。随后仅在临时目录 offline/no-deps 安装本地单wheel，源码目录外导入、兼容声明、静态资源/API/提示词/桌面默认关闭及任务控制/lifespan冒烟通过，1.256秒、98.8 MiB；发行版本/声明/changelog/两包一致校验168毫秒、19.6 MiB通过。全部守卫实际退出0、swap0。之后的依赖检查以构建成功为前提逐项进行，不将空构件检查或本地构建当成远端发行完成。

## 首轮候选与回退核验修正

PR49 的精确签名 `c1f8bbec7df001ea84fc6a4e4095bdb9261ab6d6`，候选 run `40009696868` 十项九成功一失败；唯一失败为 Ubuntu Python 3.13 shard 2 `120606586618` 的原 20 秒真实 CLI 回退案例。控制器在19,810毫秒完成 resolved，持久状态 rolled_back/released，最后健康读取超时，不能因状态已落盘而计为验收通过。该轮两个 Windows、macOS、其余 Linux、quality 与 MCP 均成功；失败记录保留，不盲重跑。

旧日志表明 controller/selected 文件核验占 `_check` 累计壁钟约95%；base 与 controller 相同的回退步骤重复核验累计约3.077秒。这是旧分项数据，不是新实现节省时间的实测。最小修正仅在一次同步 `_check`、完整 `SlotIdentity` 相等时复用刚完成的 controller 证明，不跨调用缓存，不按共同解释器合并，不删除效果前/await 后 guard，也不提高任何期限。实现者与另一 Sol 分别复审，未发现 P1/P2，细节及红绿记录见[计时记录](managed-recovery-timing-design.md)。

根随后在原期限内检查完整 `test_managed_backend.py` 与 `test_managed_process_recovery.py`，**66 passed**，pytest59.71秒、guard60.386秒、214.2MiB、swap0、实际exit0。包含真实 CLI 子进程、临时数据库、合成 manager 的切换与 target 拒绝回退，不操作用户真实服务。旧三例1红2绿与修正后14项定向结果不与该集合累加。构件重新构建及下一精确候选仍须完成；本地通过不证明远端性能收益或原生 systemd 三事务成功。

修正生产后重新离线构建 `/tmp/muli-v242-fixed-dist` 的 wheel/sdist，1.297秒、96.2MiB；临时 offline/no-deps 安装及源码外生命周期冒烟1.221秒、98.4MiB；版本/声明/changelog/两包校验175毫秒、19.9MiB。全仓 Ruff、368文件格式、146生产源 strict mypy 再次通过。每条均单独30秒守卫、实际exit0、swap0，不增加依赖或下载；下一精确候选仍未验收。

## Windows 首请求诊断

独立后续调查确认 2.41 失败只有 utility_ready，下一段先读取 stdin 完整行、解码 JSON 并校验信封，之后才是 probe_received 和 UIA 初始化；没有 COM 或程序集卡住的证据。本片仅补 input_waiting、input_line_received、request_decoded 三个首请求一次性固定点，不加入时间代理、重试、预热、额外 reader 或新的期限。

专用固定投影 18 项、原 pending/host 46 项串行守卫通过，Ruff、格式及限定类型检查通过，最高 314.2 MiB、swap 0。mypy 首次因 helper 模块双名拒绝后，使用其既有包根路径显式检查通过，未改源码掩盖错误。包内 11 行标记 LF 共 361 bytes、CRLF 共 372 bytes，沿原 2048-byte stderr 前缀及总预算；逐字节截断、前置未知行占满前缀、乱序/重复和旧八阶段兼容有纯回归。它只证明完整固定行被宿主观察到，不能证明阶段耗时、READY 或根因已定位；真实 Windows 新诊断尚待远端验收。根静态复核三个点的位置、一次性门及原读/解码/校验顺序保持，未发现 P1/P2。

## 发版闭合

最终精确签名 `ed2b26703380a1250ce2c8ecb5b9c14fc37e307f` 的[候选40013510247](https://github.com/69gg/MuLi/actions/runs/40013510247)与[main40014738737](https://github.com/69gg/MuLi/actions/runs/40014738737)各十项全部成功，[发行40014739124](https://github.com/69gg/MuLi/actions/runs/40014739124)十三项全部成功，含两组原生Windows验证、prepare、publish与GitHub Release。原20秒回退检查未提高期限。main与签名标签`v2.42.0`非force原子推送，PR49原提交合入；签名tag对象`947f17db0e93bd5209709fef84bbb866285d96de`在本地及GitHub API相同，指向上述提交。本地GPG校验为Good signature。

只读取有界PyPI/GitHub元数据，没有下载发行文件，核对[PyPI](https://pypi.org/project/muli-agent/2.42.0/)与[GitHub Release](https://github.com/69gg/MuLi/releases/tag/v2.42.0)均为正式版且两构件完全一致：

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `muli_agent-2.42.0-py3-none-any.whl` | 800351 | `20f54ab91580020ce3d1c73ef0293cab6ba15780cf9f7207316b3bfc96f7bf7e` |
| `muli_agent-2.42.0.tar.gz` | 4144356 | `add7365d06db0809348e63d83f6e28dbf5d59559bb9bc10118f3d6f9bef4ecbc` |

首个元数据脚本误将guard stdout一起重定向进JSON，解析失败；改为guard内部只捕获gh输出后，脚本又错误要求release的`target_commitish`必须是SHA，而实际该字段是main。最终以真实annotated tag对象验证提交，再比较名称、版本、两文件数、大小和摘要，guard75毫秒、16.1MiB、swap0、exit0；不把release分支标签当作精确源证明。上述两次校验脚本错误没有改动发行构件或标签，最终不依赖它们的失败结果作发布声明。

## 未验收边界

固定 FakeClient 证明宿主信封、上下文、工具执行与路由，不证明自然模型稳定选择正确资料；有限音频/PNG 签名样本不证明编解码质量。真实 OneBot/微信/Telegram/QQ 账号、网络中断和外部送达仍按平台另验。多队友反序、锁等待期间撤销、出站 unknown 的额外组合目前沿用既有专项证据，没有冒称本片完整新组合已验。

Linux 原生服务三事务及 Windows UIA 间歇超时继续独立调查；本片离线绿色不等同这些问题已解决。
