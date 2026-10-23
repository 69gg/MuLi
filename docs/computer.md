# 无障碍电脑操作

2.4 完善 Linux AT-SPI 的应用和窗口定位；2.5 的实验性 macOS AX 后端沿用相同工具名。电脑能力默认关闭。Linux 使用已安装的系统 Python GI，不在 MuLi 环境下载或编译 GTK；macOS 按需使用系统原生框架，无新增 Python 依赖。Windows UIA 提供顶层窗口与选定树，本片增加普通按钮的一次 Invoke，输入仍拒绝；新动作的真实 Windows 验收待验，见 [UIA 记录](windows-uia.md)。截图、录屏和后台服务的范围见[平台能力](platforms.md)。

## 选择读取范围

本节先说明 Linux；macOS 的前台范围与引用寿命见下节。

调用 `computer_targets({})` 获取桌面直接应用。应用条目给出临时 `application_id` 和 `target_id`；传 `application_id` 再调用 `computer_targets`，读取那个应用的直接窗口或容器。发现只访问这一层，不预先扫描每个应用的所有后代。应用本身也能作为目标，兼容没有标准顶层窗口结构的程序。

`computer_snapshot({"target_id": "本次发现返回的标识"})` 从选定应用或窗口读取子树；不传 `target_id` 时保留原先的桌面快照语义。目标必须来自当前宿主缓存，模型不能自行指定进程、D-Bus 路径或节点下标。新发现替换旧发现集合；过期、未知或变化的目标明确失败，不按名字寻找替代项，也不退回整个桌面。

快照返回 `snapshot_id` 和节点 `id`。后续 `computer_action` 的动作必须来自该节点公开的 `actions`，`computer_type` 用于设置节点的**完整文本**。原生定位信息留在宿主内，模型只使用临时标识。一次操作消费一次快照；需要下一次操作时重新读取。

节点以 `parent_id` 保留公开的父子关系，`depth` 相对于所选根计数。目标根没有父节点，`parent_observed=true`；祖先未能读取时为 `parent_id=null, parent_observed=false`，不能当成平级根节点。条目按父节点先于子节点排序，预算裁剪后不保留悬空父引用，可区分不同面板中的同名控件。

## macOS 原生引用

AX 的零参数发现和快照限定为当前接收键盘输入的应用，范围明确返回 `foreground_application`；选择当前 `application_id` 可列它的窗口，再用窗口 `target_id` 读取子树。不会枚举所有应用、点击坐标或在失败后切换后端。

原生对象由一个短期 helper 保留，使用当前 MuLi 的 `sys.executable`，不使用 Linux 的 `computer.python_path`。保留对象能避免把同名替代控件当作原目标；应用仍可能让原对象失效，因此派发前重新核对对象、当前状态及实际动作。每个新观察替换所有旧发现和快照引用。一次有效快照的操作尝试后，整组引用和 helper 都被回收；明确拒绝、未知结果与取消也不会复活凭据。

引用归属宿主提供的会话和执行代次，模型无法指定。其他执行立即收到 `desktop_busy`，无需等待有效期。让同一队友完成观察与操作；主沐璃若已观察，应先调用无参 `computer_release`，再由队友重新观察。释放是读取级工具，只结束当前调用者持有的 helper，重复释放没有副作用。结束、暂停、停止、配置重载和关闭也会回收准确的执行代次；旧执行不能关闭新执行的引用。

helper 最长寿命复用 `snapshot_ttl_seconds`，从创建计时，读取和状态查询不续期。每组最多 16 条请求，单请求最多 64 KiB，单响应最多 250000 字节，整个 helper 的 stdout/stderr 累计最多 1000000 字节；停止与回收不等待用户或模型。原生 API 自身可能先分配完整动作数组，不能把宿主预算解释成整个系统的绝对内存上限。

状态查询只在短进程中执行无提示的辅助功能信任检查，结果按成功 60 秒、其他 10 秒缓存，并合并并发查询。它不创建应用引用、不遍历桌面、不弹授权，也不证明所有应用可读；`session_accessible` 仍为未知。需要系统授权时根据本机 macOS 设置为实际运行 MuLi 的环境授权，MuLi 不自行更改 TCC；终端、LaunchAgent 和 Python 更新后的授权归属尚未实测。

首版不读取文本 Value 来拼名称。明确密码控件拒绝修改；文本控件保护语义未知时只读，不能把可选 `Subrole` 缺失理解成非密码。只有明确非保护且原生可写的文本控件才接受完整文本设置；普通按钮不因缺少可选子角色而全部禁用。部分应用的普通文本框可能因此暂不可编辑，这是能力边界，不通过重复尝试绕过。

## Windows 窗口与普通按钮

Windows UIA 的零参数发现只列直接顶层窗口，返回 `scope=desktop_windows`；快照必须
显式使用本次窗口 `target_id`，范围为 `window`。每个新观察消费旧发现，不接受自造
PID、句柄或 RuntimeId，不按标题回搜。密码节点不读 Name / Value；只为普通非密码、
启用且支持 InvokePattern 的 Button 提供 `actions=["invoke"]`。合法快照下的一次
动作尝试消费全部凭据，失败不自动重发；accepted 只表示接口正常返回，不等于业务
完成。`computer_type` 继续拒绝。它复用 macOS 的调用者与执行
代次、绝对 helper TTL、释放和关闭合同，但 Windows 只确认自有直接 helper 退出，
不宣称清理系统 COM broker。详见 [Windows UIA](windows-uia.md)。

## 不完整结果

树的宽度、深度、节点工作量、原生接口异常和最终输出预算都可能让观察不完整。`complete=false` 与兼容字段 `truncated=true` 明确表示存在省略，`omissions` 给出原因。直接子节点数量不是整棵省略子树的后代总数；无法证实数量时返回未知，不捏造准确计数。

节点预算按访问尝试计，桌面根、目标定位、失败和重复对象也消耗预算。无障碍路径最多 12 层，每个父节点最多访问 100 个子节点；定向根已有的路径长度同样占用深度额度。单节点动作超过 20 个、角色或动作名称过长时省略整个条目并报告 `output_budget`，不把截断的动作名当成可调用动作。未知原生状态使用 `null`，不伪装成确定的 false。

宿主限制 helper 响应最多 60000 字符（包含换行的 UTF-8 响应仍在 250000 字节进程总预算内），再按 `limits.max_output_chars` 核算交给模型的最终 JSON。两层都减少完整条目，保留可用标识及不完整提示，不把半段 JSON 交给模型。省略明细本身放不下时，`omissions_truncated` 与 `omission_reasons` 保留全部原因，公开数量退为未知，不能把保留明细的小计冒充总数。看到不完整结果时，不能据此断言某个控件不存在；可重新选择较小的应用或窗口范围。

## 有效期与权限

新快照尝试立即废弃旧操作凭据，即使新请求随后失败或被取消，也不会恢复旧凭据。动作仍需重新校验 D-Bus 连接名、对象路径、进程及节点状态；相同的显示名称不是相同节点的证据。快照标识不持久化，服务重启和配置重载后失效。

发现与快照使用原来的读取权限，节点动作和文本修改使用 `computer` 风险门。ask 每次调用询问；auto 的电脑动作交给用户；yolo 按用户选择自动执行。任务暂停与新输入继续遵循运行器的工具边界。已经进入执行的原生动作可能完成，取消进程不代表撤销界面变化。

错误包含稳定 `code`、中文 `message` 和 `outcome`。`not_started` 表示尚未派发动作；已经派发却无法确认结果时不能自动重发点击或文本设置。原生接口接受请求也不等于业务目的已经完成，需要下一次观察确认。状态页的能力诊断不承诺所有应用都支持无障碍，也不自动安装组件、触发截图或改变系统权限。

## 配置

```json
{
  "computer": {
    "enabled": false,
    "backend": "auto",
    "python_path": "/usr/bin/python3",
    "max_nodes": 300,
    "operation_timeout_seconds": 12,
    "snapshot_ttl_seconds": 60
  }
}
```

`operation_timeout_seconds` 是一次无障碍操作的期限，大于 0、至多 120 秒；`snapshot_ttl_seconds` 同时约束快照与发现引用，大于 0、至多 300 秒，AX 还用它限制整个原生对象会话的绝对寿命。两者必须是有限数值。输出预算仍使用现有 `limits.max_output_chars`，无需再设置另一套预算。旧配置缺少这些字段时使用原来的 12 秒操作期限和 60 秒快照有效期。

`backend=auto` 在 Linux 选择 AT-SPI、macOS 选择 AX、Windows 选择 UIA，其他平台明确报告不可用；显式 `atspi` 或 `ax` 不自动降级。未新增 `uia` 配置枚举，旧配置格式不变。旧配置中已有的 `atspi` 保持原值；macOS 用户需明确改为 `auto` 或 `ax`。Python 路径只影响 Linux 的 AT-SPI/Portal。所有平台仍默认 `enabled=false`。

## 依据与验证边界

Linux 实现沿用 [AT-SPI 节点接口](https://docs.gtk.org/atspi2/class.Accessible.html)、[D-Bus 原生身份协议](https://gnome.pages.gitlab.gnome.org/at-spi2-core/devel-docs/doc-org.a11y.atspi.Socket.html)及[节点状态定义](https://docs.gtk.org/atspi2/enum.StateType.html)。macOS 使用 [AXUIElement](https://developer.apple.com/documentation/applicationservices/axuielement_h) 与 [无提示信任检查](https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrustedwithoptions)。不引入桌面轮询、坐标输入或 OCR。

Fake GI/CF 可验证遍历、身份变化、错误和预算；核心跨平台 CI 可验证宿主协议，macOS 系统库检查只能证明 ABI/符号的相应部分。它们不能替代真实 Arch 桌面的 GTK、Qt、Electron 或 macOS TCC、窗口及动作验收。实测状态分别记录于 [2.4 审查](review-v2.4.md)及 [2.5 审查](review-v2.5.md)。
