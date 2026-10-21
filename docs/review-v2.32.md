# 2.32 Windows UIA 只读后端审查

状态：本地整合和首轮两组真实 Windows CI 已通过；第二轮已通过原 Linux
回退用例，但 Windows 3.12 的临时窗口启动超时；已补有界诊断，第三轮待验，尚未发布。这里只提供
顶层窗口发现、选定窗口树和释放引用，动作及输入明确不可用。

## 范围与审查

签名实现 `ec054b7fe41f255a0229a844275ead2d6e527624` 整合为 `39c9ad0`。
Windows 的既有 `auto` 后端选择 UIA，主 Settings 格式、资源限制和
AXHelperLease 协议保持；Linux AT-SPI 与 macOS AX 继续使用原路径。

固定系统 PowerShell 5.1 以 NoProfile / NonInteractive / MTA 和无控制台
启动，只按 `$PSHOME` 的系统路径加载 Utility 与已有 UIA 程序集，不源码
编译、安装依赖或修改执行策略。受限系统可能无法运行此桥，返回不可用；
模块探测成功不等于已经取得桌面访问能力。

helper 只保留同一进程中的短期原生窗口对象，核对 PID、启动时间和句柄；
宿主绑定调用者与代次，沿原绝对 TTL、输出预算和取消回收机制工作。
旧引用不按 RuntimeId、标题或 PID 重新搜索。密码节点不读 Name、Value
或 Text；逐项遍历，限制节点、深度、子项和完整 JSON。仅确认直接 owned
helper 已退出，不宣称系统 COM broker 或任意 provider 子树都已回收。

作者、独立 reviewer 和根审查已读产品、协议及原生测试，无剩余确定
P1/P2。原生 fixture 只在 Windows CI 创建自己的临时 WinForms 窗口，验证
系统模块路径、UTF-8/MTA、密码隐藏、旧引用拒绝和直接 helper 取消；桌面
不可用时要求失败，不静默跳过或改用 mock。Linux 的三个 skip 明确不是
这些行为的验收，原生阻塞 provider 和实际用户桌面仍不在本片证明范围。

## 本地验证

作者 186 项通过、3 项 Windows 用例在 Linux 跳过；六份 Python Ruff/格式、
两份源码严格 mypy 通过，最高 128.7 MiB。首轮 ctypes stub 类型问题修复
后重核通过；原 auto 后端测试仅同步 Windows 期望，禁用时不启动保持。

根分支同步版本、锁和兼容声明为 2.32.0：八文件 204 项通过、3 项平台
跳过（3.55 秒、77 MiB）；Runtime、扩展、兼容和发行四个不同文件另
224 项通过（3.11 秒、132.7 MiB），共十二文件 428 项，不与作者重复累计。
全仓 Ruff、323 文件格式、136 源码完整 mypy 与 83 包离线锁通过；最高
398.7 MiB，swap 0。

完整离线构建（92.9 MiB）及临时目录内、无依赖安装的发行包 smoke
（96.5 MiB）通过：安装后的 `uia_helper.ps1` 存在、位于安装目标内、UTF-8
可读，既有 API/资源/禁用桌面及生命周期验证通过。没有下载依赖或启动
PowerShell、真实桌面、真实用户服务。全部检查串行受单次 1 GiB、共享
2 GiB、单 CPU、swap 0 和 30 秒守卫约束；远端需验证精确候选的完整矩阵。

## 首轮完整矩阵

精确签名候选 `6a8ad38e7899b70cb34ac91f0b9a6570bcf1faa9` 的
[PR36](https://github.com/69gg/MuLi/pull/36) 对应
[CI 39554055600](https://github.com/69gg/MuLi/actions/runs/39554055600)，
七项成功、一项失败。Windows 3.12 / 3.13 作业 `119069563767` /
`119069563721` 均为 3151 项通过、1356 项跳过；三个 UIA 原生用例的
`win32 + GITHUB_ACTIONS` 门在这两组实际启用，没有缺桌面能力的动态跳过。

Ubuntu 3.13 作业 `119069563832` 为 4490 项通过、16 项跳过、一项失败：
`test_actual_cli_boot_activation_and_projection_preserve_latest_database`
的 `target-rejected-rollback` 已经过 start-target、stop-target、start-base，
基础服务 ready 和 paused 健康断言也已通过，但原 20 秒总期限内未收到
`controller-resolved`。此处尚未进入独立最终退出收尾；trace 中的 -9 来自
finally 清理，不能当成后台进程自行被杀的证据。

不修改产品或期限的单次受限诊断为 1 项通过、13.65 秒，服务 14.171 秒、
峰值 179.7 MiB、swap 0。base ready 为诊断起点后 11.552 秒，resolved 为
13.058 秒；两个临时 slot 各生成 119 个 pyc，确认 `-I` 忽略
`PYTHONDONTWRITEBYTECODE`。该次成功不能还原远端未保留的失败阶段，
也不能把额外字节码写入定为超时唯一根因。

签名窄修 `90f038d7de511f9d2825205d1b1e830d07bccafe` 整合为 `276c9ff`，
仅在最早测试 .pth 禁止后续包写 pyc，并为失败附最多 4 KiB 的固定阶段、
状态与 HTTP 计数。SQLite 诊断使用零 busy timeout，锁冲突立即退为不可用；
不输出私有正文或修改原异常。确定性隔离 Python 对照先红后绿；20 项单位
回归、真实切换 1 项（8.31 秒）和真实回退 1 项（13.78 秒）全部通过，两种
真实路径均证明专有 MuLi 包及 fixture 模块无 pyc。原产品、20 秒功能期限、
数据库/唯一回执与收尾断言保持不变。单文件 Ruff、格式与严格 mypy 通过，
本次最高 254.6 MiB、swap 0；根独立审查无剩余确定 P1/P2，不重复运行相同
真实案例。该修复确认消除额外写入，远端失败是否收敛须由新精确候选验证。

## 第二轮完整矩阵

签名候选 `c60a77bb396d9ecfbc063284e1b71350c7e35891` 的
[CI 39557587927](https://github.com/69gg/MuLi/actions/runs/39557587927)
attempt 1 于 `2026-10-21T06:54:09Z` 完成，仍为七项成功、一项失败。
原 Ubuntu 3.13 回退作业 `119080883351` 已成功；本次唯一失败是 Windows
3.12 作业 `119080883490`，3150 项通过、1361 项跳过、一个 setup error。

失败发生在 `native_form` 等待自有 WinForms 窗口的 Shown 回执，超过原
12 秒期限，尚未进入 Computer、probe、targets 或 snapshot。后两个原生
用例和 Windows 3.13 组均通过；现有日志没有窗口脚本阶段或 stderr 事实，
不能判定为 UIA 桥接错误，也不能单凭其它通过便归为运行器慢。下一修订
只补有界固定阶段与进程收尾诊断，保持产品/夹具期限与三个原生用例门槛。
这两轮失败分别保留，不以重跑替代具体定位。

签名诊断片 `498d452a44add7184ba8f157336be1df887be595` 整合为 `95a61df`：
只为自有窗口增加固定启动阶段、两管道合计 4096 字节预算和退出/清理证据。
原 12 秒、三个原生用例及产品保持。新增 9 项纯假流和既有 27 项宿主通过，
三个原生用例在 Linux 按原门跳过；三文件 Ruff、格式及严格 mypy 通过，
整轮最高 189 MiB、swap 0。根分支独立复审无确定 P1/P2，整合后的全仓
Ruff 与 325 文件格式检查通过，峰值 21.6 MiB。没有再次运行相同测试。
该片确认有界诊断与强拥有回收行为，不把新增并发读取说成已经定位旧超时
原因；真实脚本标记与完整新候选仍须远端验证，见[诊断记录](uia-native-startup-review.md)。
