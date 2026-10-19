# Wayland Portal 截图方案

**状态：下一版本候选实现，尚未发布，不属于 2.0 的能力声明。** 本开发树已实现显式 Portal 截图并通过私有 D-Bus fake 测试；未调用真实 Portal、打开授权对话框或采集桌面。实际平台限制见 `docs/platforms.md`；录屏与目标选择仍不在本轮范围。

## 范围与接入

首轮只增加 Linux 的显式 `computer.capture.backend="portal"` 截图后端，保留现有 `auto` 顺序。Portal 录屏需要另一套 ScreenCast / PipeWire 流程，不在本方案内；显式 Portal 录屏应返回不支持。用户取消 Portal 后不切换到其他截图后端。

复用 `Capture`、`LocalFiles`、`PersonalIO.publish()` 和现有 `computer` 权限门。`computer.enabled` 与 `computer.capture.enabled` 继续默认关闭；ask / auto / yolo 的判断不变，Portal 自身仍可要求用户授权。

只新增一个短生命周期的 `tools/portal_helper.py`，沿用 `Computer` 的系统 Python、stdin/stdout JSON 与子进程模式，不在 MuLi 虚拟环境安装 GI。helper 延迟导入 Gio / GLib，不导入 Atspi，因此截图不依赖无障碍树可用。`Capture` 通过构造参数接收现有 `computer.python_path`，不另设解释器配置、不直接走 `Computer.call()`。

helper 支持只读探测与一次截图两种操作；每次操作只创建一条 D-Bus 连接。截图由 `Capture` 在 `files.lock` 内分配私有 PNG 路径，使用 `LocalFiles.run_output()` 监测输出配额；helper 收到成功 URI 后只读校验并复制到这个指定文件，最后由既有 `artifact()` / `publish()` 进入附件库。JSON 只传有界元数据，不传整幅图片。

配置增量为 `capture.portal_timeout_seconds`：默认 60 秒，允许 1–300 秒，整体仍受外层运行期限与取消约束。helper 在连接与属性读取后扣除已用时间，再等待剩余交互期限；它不复用录屏 `max_seconds`。

## 异步探测与同步状态

`Capture.status()` 继续同步返回，不执行子进程、D-Bus 调用或 `asyncio.run()`。给实例增加小型探测缓存，并由一个异步 `refresh_portal()` 更新；同实例并发探测合并为一个任务。无需常驻轮询或新后台服务。

异步 `/api/capture` 与 `media_capabilities` 按需刷新；不在启动时预热，不起轮询。两个采集开关关闭或未选择 Portal 时不启动探测 helper。成功缓存 60 秒、其他结果缓存 10 秒，探测自身限时约 2 秒；显式截图在自身 helper 中重新确认版本，不因缓存失败永久禁用。查询取消不会取消共享探测；`PersonalIO.close()` / 配置重载取消并回收旧 Capture 的任务。

缓存区分 `unknown / ready / error`，记录 `version`、`available_targets`、内部探测时间与短原因，并通过 `stale` 表示过期。初始版本、能力为未知，不用假值 0 或 false 冒充探测结果。`portal.detected` 仍只表示环境线索，候选实现的 `implemented=true` 不代表真实桌面已验证。

探测只读取接口属性，不调用 `Screenshot` / `PickColor`。用禁止自动启动的调用检查现有服务；尚无 owner 只能说明“服务未运行，能力未确认”，不等于未安装。缺少 GI、会话总线不可连接、接口缺失和超时分别报告。解释器、总线地址或配置改变使缓存失效；实际调用的 owner 变化也使旧结果失效。

`permission_verified` 保持 false，不因探测或一次成功变为永久授权，也不把 `DBUS_SESSION_BUS_ADDRESS` 存在当作 Portal 可用的证明。

## 请求流程与版本兼容

Portal 使用 `org.freedesktop.portal.Desktop`、`/org/freedesktop/portal/desktop` 与 `org.freedesktop.portal.Screenshot`。当前官方接口为版本 3，但必须运行时读取 `version`。[Screenshot 接口](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Screenshot.html)

| 接口版本 | 本轮发送的选项 | 兼容行为 |
| --- | --- | --- |
| 1 | 唯一 `handle_token`、`modal` | 不发送 `interactive` 或 `target` |
| 2 | 上述选项及 `interactive=true` | 允许用户调整截图；不声称所有后端都能提供相同交互 |
| 3 | 默认仍同版本 2 | 首轮不新增目标选择控件；省略 `target` 保留兼容行为 |

若以后增加明确的截图目标，`target` 是 1、2、4、8 之一的**单值**，分别表示屏幕、窗口、区域、活动窗口；`AvailableTargets` 才是位图。只在版本 3 且支持对应目标时传入，不把不支持的窗口请求悄悄改为全屏。

无原生窗口句柄时传 `parent_window=""`，这是官方允许的用法。浏览器标签页或 DOM 标识不能当作 Wayland 原生窗口句柄。[窗口标识](https://flatpak.github.io/xdg-desktop-portal/docs/window-identifiers.html)

一次截图的顺序如下：

1. helper 通过 Gio 建立一条固定会话总线连接。显式截图允许正常激活 Portal，随后解析并固定服务的 unique owner，方法调用与信号过滤都绑定这个 owner；探测仍不激活服务。
2. 从这条连接取得调用方 unique name，去掉开头 `:` 并把 `.` 换成 `_`，作为 `SENDER`。为每次请求生成带 MuLi 前缀的随机合法 `handle_token`。
3. 预测 `/org/freedesktop/portal/desktop/request/SENDER/TOKEN`，**先订阅**该路径的 `org.freedesktop.portal.Request.Response`，再调用 `Screenshot`。所有回调在同一个 GLib main context 处理，避免跨线程状态竞态。
4. 使用明确的 Variant 类型：方法参数 `(sa{sv})`、返回 `(o)`、信号 `(ua{sv})`。返回 handle 必须校验，不能假定一定等于预测路径。
5. 处理 `Response`：0 才读取有界 `uri`；1 返回用户取消；2 返回失败。未知状态、缺少 URI、错误类型均不生成附件。
6. 退出时取消订阅并关闭连接；owner 更换或总线断开结束本次操作，不自动重发截图。

预订阅和 token 路径是 Portal 自 0.9 提供的模式。为兼容返回其他 handle 的旧实现，在方法返回前增加一个临时、严格匹配已固定服务 owner 与 Request.Response 接口的订阅，只缓存少量有界消息。返回真实 handle 后只接受该路径的消息，建立精确订阅并移除临时订阅。这样也覆盖信号先于方法返回的竞态；缓冲超限直接失败，不接受其他 sender 或其他请求结果。[Request 路径与响应](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Request.html)

Gio 信号回调属于订阅线程的 thread-default main context；订阅 well-known name 也不能省略 owner 检查。helper 只维护本次请求状态，不把请求路径跨连接复用。[Gio 信号订阅](https://docs.gtk.org/gio/method.DBusConnection.signal_subscribe.html)

## 超时、停止与清理

方法调用期限和等待用户响应的总期限分开：前者限制 D-Bus RPC，后者允许用户完成选择；整个过程仍受运行剩余预算约束。不能套用普通 D-Bus 调用约 25 秒的默认等待值来代替交互期限。[请求生命周期](https://flatpak.github.io/xdg-desktop-portal/docs/requests.html)

停止、总期限到达或 helper 收到 SIGTERM 时，尽力在**同一连接**上调用真实请求对象的 `Request.Close()`。正式方法是 **Close，不是 Cancel**；Close 后不会再发送 Response，因此清理只等待 Close 调用完成或短期限，不能继续等 Response。[Request.Close](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Request.html)

`GCancellable` 只取消本地调用等待，不能代替远端 Close。若方法尚未返回真实 handle，可先尝试预测路径并在短清理窗口接收迟到的方法结果；预测路径的 UnknownObject 不能当作已关闭真实请求。失败的 Close 退出进行中集合；随后方法确认真实 handle 时允许补发一次 Close，也覆盖方法结果先于失败回调到达的顺序。已确认 handle 上的失败不循环重试。不能确认清理时记录短原因，不宣称远端对话框必已关闭。[Gio 异步调用](https://docs.gtk.org/gio/method.DBusConnection.call.html)

`run_process()` 新增**默认关闭、仅 Portal 截图使用**的 `terminate_grace`：先 SIGTERM，屏蔽重复取消并给予约 2 秒清理，随后强杀并 wait。helper 的 Close 等待窗口为 1 秒，自身交互期限早于外层硬期限。终止期间有界丢弃管道输出，避免背压阻塞退出；其他调用保留默认立即终止。强杀只保证本地资源回收，不冒充 Portal 关闭确认。

状态机只允许一个终态：停止先发生就不得发布随后到达的成功响应；Close 与 Response 交错、重复信号、取消期间再次取消均不能重复写文件或重复生成附件。清理失败也必须回收进程、订阅与本地未完成文件。

## URI 与文件边界

只处理本次已校验成功响应的 URI，不提供“导入任意 URI”工具。允许 `file:`、空主机或 `localhost`、绝对路径；拒绝远程协议、userinfo、query、fragment、NUL 和无效转义。解码一次，不经过 shell，不下载文件。

Portal 可通过 Documents portal / FUSE 返回截图，不能把路径硬编码限制为 `/tmp`。这只是本次授权结果的专用导入通道，不扩大普通文件工具的 `workspace_roots`。[Screenshot 结果说明](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Screenshot.html)

helper 以只读描述符打开源文件，逐层用目录描述符与 `O_NOFOLLOW` 防止符号链接替换，末端使用 `O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW`，同一描述符 `fstat` 确认普通非空文件。目录、FIFO、设备及符号链接均拒绝；不使用“先 stat 再重新按路径打开”的检查方式。拒绝所有链接可能影响带链接的用户目录，这是首轮可解释的兼容取舍，须用 Documents/FUSE 路径样例验证。

按现有格式头规则检查 PNG，不引入图片解码库，也不宣称完成全面图像解码验证。源文件长度不能作为唯一限制：按小块读取，累计最多为单文件上限与目录剩余额度较小者，再多读一个字节判断超额；增长中的文件同样受限。源读取和复制都在可回收的 helper 内，慢速 FUSE 不阻塞主进程事件循环。

输出只能是 `Capture` 已分配的私人路径，禁止链接；`LocalFiles.run_output()` 继续监测单文件与目录配额，完成后 `validate()`，再进入附件库。附件库与采集目录各自执行现有配额，不承诺两份副本只计一次。失败或停止删除自己的未完成输出；不删除、修改 Portal 原文件，不额外撤销 Documents 授权。任何原始 URI 都不直接交给模型或渠道。

## 有界验证计划

不依赖真实桌面测试。`tests/test_portal.py` 覆盖路径预测、版本选项、URI 检查、缓存和终态竞态；模块导入不立即要求 GI。系统没有 GI 或 `dbus-daemon` 时，相关集成测试明确跳过，不安装依赖。

集成测试在独立子进程创建临时私有 D-Bus，使用显式配置且不加载系统服务激活目录。Gio fake 服务持有 `org.freedesktop.portal.Desktop`，导出 Screenshot、属性、Request.Close 与 Response，只写极小 PNG fixture。helper 只连接这个显式地址，fake owner 未就绪即失败，绝不回退用户总线或启动真实 Portal。若复用 `Gio.TestDBus`，它会修改进程环境，也必须限制在独立子进程内。[Gio.TestDBus](https://docs.gtk.org/gio/class.TestDBus.html)

| 专项 | 必须覆盖 |
| --- | --- |
| 版本与状态 | v1/v2/v3；无服务、缺 GI、探测超时；同步 status 不启动进程；并发刷新合并；禁用不探测 |
| 请求竞态 | 信号先于返回、正常顺序、旧 handle 不同、重复/伪造 sender 或 path、owner 重启 |
| 停止与期限 | 用户取消；无响应超时；Close 无 Response；返回 handle 前停止；请求对象延迟注册、预测 Close 失败后同 handle 确认；Close 失败与 handle 回调双向交错；TERM 宽限与最终强杀；停止后迟到成功不发布 |
| 文件接收 | 有效 PNG；Documents 样式路径；远程 URI、链接、FIFO、目录、空文件、坏头、超额和读取期间增长 |
| 集成收尾 | 配额已满、复制或发布失败；只删除自有未完成文件；无遗留进程和私有总线；权限门与附件行为保持 |

候选实现已通过私有总线测试，复核命令为 `PYTHONPATH=本工作树/src /data0/MuLi/.venv/bin/pytest -q tests/test_portal.py tests/test_capture.py tests/test_process_cleanup.py tests/test_process_grace.py`。测试只启动自有临时总线和 fake 服务，均在 fixture 退出时回收；输出洪流测试有独立进程 watchdog。

发布前仍应在用户明确授权下对 Arch 的实际 GNOME / KDE Portal 做有限人工验收。在实际桌面验证前，发行说明必须继续区分“实现并通过模拟测试”和“已验证该桌面授权与截图”；本轮未实施真实桌面验收。
