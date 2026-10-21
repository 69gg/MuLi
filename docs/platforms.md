# 平台服务与桌面采集

MuLi 只探测当前已安装的工具，不安装系统组件、下载模型或在导入模块时启动后台进程。以下适配通过生成内容与模拟进程测试；本次未实际安装平台服务，也未采集开发机桌面。

## 当前用户服务

Linux 同版本恢复自 2.19.0 起已接入 CLI 和管理 API，要求标准 systemd unit、真实进程/cgroup 与本地文件系统证明；普通前台运行和 macOS/Windows 不声称获得同等恢复能力。详见[恢复入口及验证范围](linux-recovery-operations.md)。

| 平台 | 后端 | 安装位置 | 日志 |
| --- | --- | --- | --- |
| Linux / Arch | systemd --user | $XDG_CONFIG_HOME/systemd/user/muli.service，默认 ~/.config/systemd/user | journalctl --user -u muli.service |
| macOS | launchd LaunchAgent | ~/Library/LaunchAgents/muli.agent.plist | 数据目录 logs/service.log |
| Windows | Task Scheduler，当前交互用户、最低权限 | 当前用户 MuLi 任务；描述文件位于数据目录 service/ | 数据目录 logs/service.log |

`muli service install` 只生成或注册服务，不启动；`enable` / `disable` 管理下一次登录的自动启动，`start` / `stop` / `restart` 管理当前进程。Linux 的 `enable` 不带 `--now`。macOS 的安装文件默认 RunAtLoad=false、KeepAlive=false；该适配不声明具备 systemd 的失败自动重启保障。

Windows 计划任务使用当前 Python 环境的 pythonw.exe，缺失时报告不支持，不回退到会弹出控制台的 python.exe。执行参数按 Windows 规则逐个编码，文件路径不会拼入 shell；任务不使用 SYSTEM、管理员 RunLevel 或保存密码。安装后的登录触发器默认关闭，显式 start 仍可启动。restart 会注册并运行一个独立的单次助手任务，由它结束主任务、等待停止、再启动主任务，最后删除助手；不会在主服务内部先结束自身再尝试执行启动。异常信息记录至 logs/restart.log。

macOS 与 Windows 使用本地日志包装器，不依赖终端重定向。每份日志最多 1 MiB，保留一个轮转文件。服务命令不隐式提升权限，系统工具缺失、用户会话不可用和查询超时会通过 status.reason 报告。Linux unit 默认保留 MemoryMax=512M、CPUQuota=50%、TasksMax=64，可用用户 unit override 调整。

Linux 同版本恢复使用独立的[安装 manifest](installation-manifest.md)：保留虚拟环境词法解释器入口，有限核对实际二进制、pyvenv、MuLi 包与标准 unit，不扫描完整依赖环境。它已接入开发候选的交接入口；原始服务命令仍只报告 accepted，并受未决事务与标准 unit 身份保护。自定义 unit/drop-in 不属于首期恢复合同，身份无法核验时拒绝自动控制。

## 截图与录屏

Linux 节点操作先使用 AT-SPI 获取无障碍树和节点身份；2.5 的实验性 macOS AX 使用单独的短期原生对象会话。Windows UIA 候选使用系统 PowerShell/.NET 的只读 MTA helper，只列顶层窗口、读取选定窗口，动作和输入不可用；首轮两组 Windows 原生 CI 已通过，第二轮 Windows 3.12 启动夹具超时，当前完整矩阵尚未通过，详见 [UIA 记录](windows-uia.md)。`computer_screenshot` / `computer_record` 是独立的图像采集工具，不要求节点后端可用，遵循 computer 风险门；采集结果不能提供绕过快照校验的点击坐标。配置 computer.enabled 与 computer.capture.enabled 后才可执行，二者默认关闭。图像采集能力与节点后端分别报告。

2.4 的 `computer_targets` 先列应用、再按应用列直接窗口；`computer_snapshot` 可指定发现所得目标，零参数保留桌面语义。省略原因和当前引用的有效期单独报告，使用方式与验证边界见[无障碍电脑操作](computer.md)。不完整树不能用来证明目标控件不存在。

macOS 的发现与默认快照只覆盖前台应用，显式窗口读取沿用相同工具。原生 helper 使用当前 MuLi Python 和系统 ApplicationServices/CoreFoundation，不安装 PyObjC；状态仅检查该 helper 的无提示信任结果。每次观察替换旧引用，操作后回收；引用不能在 agent 之间转交，需先释放再由接手者重新观察。真实 TCC、LaunchAgent 授权和应用动作尚未实测，详见 [2.5 审查](review-v2.5.md)。

| 平台 / 会话 | 截图候选 | 录屏候选 | 额外限制 |
| --- | --- | --- | --- |
| Linux Wayland | grim、gnome-screenshot | wf-recorder + timeout | grim / wf-recorder 依赖合成器提供相应协议；GNOME 等环境可能不支持 |
| Linux Portal（2.1 新增） | 显式 backend=portal | 不支持 | 复用系统 Python Gio；只读能力探测与实际授权分开，不自动降级 |
| Linux X11 | gnome-screenshot、scrot、ffmpeg x11grab | ffmpeg x11grab | 需要 DISPLAY；可用 capture.device 指定显示源 |
| macOS | screencapture | ffmpeg avfoundation | 录屏必须显式指定 capture.device，接受已确认的视频设备索引/名称或其 :none 形式；需要系统屏幕录制授权 |
| Windows | ffmpeg gdigrab | ffmpeg gdigrab | 默认 desktop；capture.device 可显式指定 gdigrab 输入源 |

capture.backend=auto 保留原有候选顺序，不自动选择 Portal；显式后端只用于它支持的操作，不偷偷降级到另一种采集。同步 status 只检查工具、环境及探测缓存，permission_verified 始终为 false。DBus 地址仅作为 portal 环境线索，不代表服务可用或已授权。

2.1 新增显式 Portal 截图，已通过私有 D-Bus 测试，未验证真实桌面授权。启用 computer.enabled、computer.capture.enabled，并显式选择 capture.backend=portal 后，使用 computer.python_path 对应的系统 Python Gio 请求截图；不依赖 AT-SPI 可用，不安装组件。系统缺少 GI 或 Portal 接口时明确失败。Portal 录屏、坐标操作和持久授权未实现。

`GET /api/capture` 与 `media_capabilities` 按需执行约 2 秒上限的异步只读探测，合并并发查询；同步 `Capture.status()` 不启动进程。成功缓存 60 秒、其他结果缓存 10 秒，`portal.state` 区分 unknown / ready / error，`stale` 表示缓存是否过期。未运行的服务保持 unknown，探测不激活服务；只有显式截图才允许正常激活 Portal。关闭或重载配置会取消并回收旧探测。

每次截图在固定 D-Bus 连接上预订阅 Response，校验实际返回 handle 和服务 owner；旧版路径、早到响应及版本 1/2/3 分别兼容。取消和交互超时尽力调用 Request.Close，不等待 Close 后不存在的 Response；helper 有短暂 TERM 清理宽限，随后强杀并回收。不能确认远端关闭时报告原因，不宣称对话框必已关闭。用户取消后不回退到其他截图工具。

Portal 返回的本地 file URI 经过控制字符、协议、路径及文件描述符检查，拒绝链接、FIFO、设备和非 PNG。只读有界复制到私人 captures/ 文件后进入附件库，不直接导入任意 URI，也不扩大文件工具工作区；Portal 原文件不删除。当前验证使用私有总线 fake 服务与极小 PNG fixture，没有调用真实 Portal。流程与测试边界见 [Portal 设计与实现记录](wayland-portal-design.md)。

macOS 的视频输入统一传为 `视频设备:none`，明确不打开 AVFoundation 音频设备；`0:0`、`default:default` 等显式选择音频的配置会在启动采集前拒绝。输出端的 `-an` 不能代替此输入约束。

配置字段为 enabled、backend、device、max_seconds、portal_timeout_seconds、fps、max_file_bytes、max_total_bytes。Portal 交互等待默认 60 秒，可设 1–300 秒；它与录屏时长独立，仍受外层运行取消和期限约束。默认录屏上限 30 秒、8 fps、单文件 32 MiB、目录 200 MiB。文件随机命名并保存在数据目录 captures/，不接受任意输出路径；结果验证为普通非空文件及 PNG / MP4 格式头，使用私人文件权限。录屏不捕获麦克风或系统音频。

所有进程复用可取消的 run_process，超时和输出都有上限。Wayland 使用独立 timeout 发送 SIGINT 完成封装，再用外层超时兜底；ffmpeg 自带时长和文件限制，采集期间仍持续检查目录与单文件配额。停止、异常、格式无效和超额都会删除未完成文件。已经完成的文件不会自动清理，以免删除用户需要的素材；满额后需显式清理。

## 参考依据

- [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html) 与 [systemd.syntax](https://www.freedesktop.org/software/systemd/man/latest/systemd.syntax.html)：用户服务、参数和转义。
- [Apple Launching Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)：LaunchAgent 与 ProgramArguments。
- [Microsoft Task Scheduler Schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema) 与 [schtasks create](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create)：XML 注册、当前用户任务与登录触发器。
- [FFmpeg Devices](https://ffmpeg.org/ffmpeg-devices.html)：x11grab、gdigrab、avfoundation 输入。
- [Arch wf-recorder 手册](https://man.archlinux.org/man/wf-recorder.1.en)：帧率、覆盖与中断结束录屏。
