# Linux 无障碍引用与执行生命周期

## 目标

Linux 是主要适配平台。主助手和队友共用电脑入口时，一轮执行不能使用或释放另一轮的观察；释放、结束和关闭后，旧读取也不能重新发布引用。随后在托管 CI 的私有桌面验证真实 GTK 控件，不能使用开发者桌面来替代验收。

当前 Linux `computer_release` 只返回成功，`release_execution` 和 `aclose` 只处理 AX/UIA；Linux 还没有消费构造时已有的 `owner` 与 `dispatch_guard`。修复复用这两个回调、现有观察代次和进程清理，不新增用户配置或原生依赖。

## 行为

- 状态探测无需执行身份，也不占用观察。Linux 发现、读取和动作要求真实 `(session_id, run_id)`；未启用、平台不可用的原错误优先保留。没有调用者的直接测试改为明确提供自己的身份，不引入匿名授权。
- 首次观察前登记持有者。另一执行得到 `desktop_busy`，不能更改现有引用。沿用 `snapshot_ttl_seconds` 作为持有期上限，不因读取延长；到期退役后可以重新观察，不续用旧标识。这一时长选择由本次实现采用，与原生 helper 的有限持有方式保持一致。
- 原持有者可以更新自己的观察。保持现有新发现、快照和单次动作的失效规则；检查捕获的执行、代次和期限，等待结束后不发布过期或已撤销结果。
- 只有持有者可显式释放。`release_execution` 按传入的准确执行比较并同步失效，不查询后来执行的身份；旧 finalizer 不得清除新执行的引用。关闭先撤销 Linux 引用并标记关闭，再等待其它清理；关闭后不再启动 helper。
- Linux 保持一次请求一个 helper。复用 `run_process` 的子进程拥有与清理，仅增加可选同步 `input_guard`：取得自有句柄后、写入首个 stdin 字节前恰好执行一次。默认调用路径不变；guard 拒绝时零请求字节并回收子进程，取消继续传播。
- 电脑请求在输入派发前核对捕获的引用、执行、代次和期限。与 AX/UIA 一样，仅 action/type 在此消费 Runtime 原有的 `computer` 风险派发 guard；targets/snapshot 保留只读语义，不增加写权限门，probe 不占用观察。前置拒绝保留 `not_started`；已经派发后的超时、丢回执和原生异常仍为 `unknown`，不重放，也不把接口接受当作实际业务完成。

## 有限实现范围

先完成 `computer.py` 的 Linux 生命周期与必要的 `process.py` 输入门，复用 AX/UIA 的身份回调及错误方式，保留其它平台行为。添加真实异步等待、两执行交接、迟到 finalizer、取消、过期、关闭和零字节派发负控。纯测试使用既有 Fake GI/runner，不加载本机 GI。

独立后审通过后，接入私有 D-Bus/Xvfb 的真实 GTK 窗口验收。窗口只包含自有按钮、普通与密码输入框，以窗口进程的独立回报核对点击计数和普通文本。通过公开 Computer 路径操作，测试侧额外核对缓存目标的真实 PID/bus 身份；不用标题寻找替代目标。完整约束见本轮独立方案，原生验收未执行前不宣称通过。

当前 AT-SPI 密码节点只禁止文本设置，不能宣称密码标签已隐藏或全部动作已禁止；这一缺口继续保留在后续修复范围，不能由绿色夹具掩盖。

## 验证与资源

所有本机检查由主 agent 串行经 `scripts/check_local.py` 执行，沿用合计 1 GiB、CPU 50%、swap 0 与单次 768 MiB/180 秒限制。先验证新负控，再运行电脑、进程及 Runtime 关联回归，同步使用文档。真实 GI、窗口与系统包只在明确启用的托管 Linux 隔离任务中使用；不安装本机依赖、不操作真实用户桌面，不增加既有产品超时、节点或输出预算。

## 官方依据

- [dbus-run-session](https://dbus.freedesktop.org/doc/dbus-run-session.1.html)：私有回归会话及总程序退出后的 bus 生命周期。
- [GTK 可访问对象](https://docs.gtk.org/gtk3/method.Widget.get_accessible.html)与[密码输入显示](https://docs.gtk.org/gtk3/method.Entry.set_visibility.html)。
- [AT-SPI 进程身份](https://gnome.pages.gitlab.gnome.org/gtk/atspi2/method.Accessible.get_process_id.html)。

Context7 查询因月配额耗尽未取得结果，本轮使用上述官方文档和现有实现；未据此宣称完成原生运行验证。
