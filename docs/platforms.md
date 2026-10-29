# 平台服务与桌面采集

MuLi 使用系统已有的服务管理器和采集工具，不自动安装系统组件或下载模型。平台状态表示当前可探测到的能力，不能代替系统授权和实际操作确认。

## 当前用户服务

先在当前用户环境配置并运行 MuLi，确认模型和所需渠道可用，再安装后台服务：

```bash
muli service print
muli service install
muli service start
muli service status
```

`print` 查看服务配置，`install` 生成或注册服务但不启动，`start` 启动当前进程。其它命令：

| 命令 | 用途 |
| --- | --- |
| `muli service stop` | 停止当前服务 |
| `muli service restart` | 重启服务 |
| `muli service enable` | 启用下一次登录自动启动 |
| `muli service disable` | 关闭登录自动启动，不等于停止当前进程 |

服务使用安装时选择的 Python 环境、配置与数据目录。凭据环境变量也必须对后台服务可见，不能只在临时终端里设置。命令不会自动提权；缺少系统工具或用户会话不可用时，查看 `status` 的原因。

同一数据库路径只供一个运行实例使用。后台服务运行时，再启动一份使用相同数据的 MuLi 会被拒绝；前台调试前先停止服务。正常退出或初始化失败完成清理后可以重新启动。升级前仍需退出旧版本，这项互斥不会替你停止旧进程，也不开放其他平台的旧库格式迁移。

| 平台 | 服务方式 | 安装位置 | 日志 |
| --- | --- | --- | --- |
| Linux / Arch | `systemd --user` | `$XDG_CONFIG_HOME/systemd/user/muli.service`，默认 `~/.config/systemd/user/muli.service` | `journalctl --user -u muli.service` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/muli.agent.plist` | 数据目录 `logs/service.log` |
| Windows | 当前交互用户的 Task Scheduler 任务 | 描述文件位于数据目录 `service/` | 数据目录 `logs/service.log` |

Linux 默认限制为 `MemoryMax=512M`、`CPUQuota=50%`、`TasksMax=64`，可通过用户 unit override 调整。`enable` 不会同时启动服务。受管理的恢复入口要求标准服务配置及可核验的安装；自定义 unit 或 drop-in 可能无法自动恢复，见 [Linux 恢复操作](linux-recovery-operations.md)。普通前台运行、macOS 和 Windows 不提供同等的 Linux 恢复保证。

macOS 安装后默认 `RunAtLoad=false`、`KeepAlive=false`，不承诺失败后自动重启。Windows 需要当前 Python 环境的 `pythonw.exe`，不回退到弹出控制台的 `python.exe`；任务不使用 SYSTEM、管理员权限或保存密码。Windows 重启通过一次性助手任务完成，异常可查看 `logs/restart.log`。macOS 和 Windows 服务日志每份最多 1 MiB，并保留一份 `logs/service.log.1` 轮转文件；可用 `self_logs({"segment":"previous"})` 按现有脱敏和输出额度读取，详见[自身日志](self-management.md#主动读取自身日志)。它不读取独立的 restart.log，也不增加日志留存量。

## 截图与录屏

节点操作使用当前平台的无障碍后端：Linux AT-SPI、macOS AX、Windows UIA。先用 `computer_targets` 发现目标，再用 `computer_snapshot` 读取节点，按当前快照提供的动作操作；不能把不完整树当作控件不存在的证明。支持范围、系统权限和引用有效期见[电脑操作](computer.md)。

`computer_screenshot` 和 `computer_record` 分别截图、录屏，与节点后端独立：节点不可用不一定影响采集，但图像也不能绕过节点快照校验去点击坐标。采集前需同时启用 `computer.enabled` 与 `computer.capture.enabled`，两者默认关闭。

例如启用自动选择的截图后端：

```json
{
  "computer": {
    "enabled": true,
    "capture": {
      "enabled": true,
      "backend": "auto"
    }
  }
}
```

### 平台所需工具

| 平台 / 会话 | 截图 | 录屏 | 需要注意 |
| --- | --- | --- | --- |
| Linux Wayland | `grim`、`gnome-screenshot` | `wf-recorder` 与 `timeout` | 合成器必须支持相应协议，GNOME 等环境未必支持 `grim` / `wf-recorder` |
| Linux Portal | 显式选择 `backend="portal"` | 不支持 | 需要 `computer.python_path` 对应系统 Python 的 Gio 与桌面 Portal |
| Linux X11 | `gnome-screenshot`、`scrot`、`ffmpeg x11grab` | `ffmpeg x11grab` | 需要 `DISPLAY`，可用 `capture.device` 指定显示源 |
| macOS | `screencapture` | `ffmpeg avfoundation` | 需要系统屏幕录制授权；录屏必须设置视频设备 |
| Windows | `ffmpeg gdigrab` | `ffmpeg gdigrab` | 默认源为 `desktop`，可用 `capture.device` 指定 |

`backend="auto"` 只从当前平台适用且已安装的工具中选择，不自动选择 Portal。显式指定后端后，不会失败再偷偷换用另一后端。macOS 的 `capture.device` 接受视频设备索引/名称或 `视频设备:none`；`0:0`、`default:default` 等选择音频输入的值会被拒绝。录屏不录麦克风或系统音频。

### Wayland Portal 截图

在不适用 `grim` 等工具的桌面上，可显式设置：

```json
{
  "computer": {
    "enabled": true,
    "python_path": "/usr/bin/python3",
    "capture": {
      "enabled": true,
      "backend": "portal",
      "portal_timeout_seconds": 60
    }
  }
}
```

能力检查不会代替用户授权；实际截图可能弹出系统选择或授权窗口。用户取消后不会回退到其它工具。关闭和超时会尽力取消请求，但无法确认时不会宣称系统对话框一定已关闭。Portal 录屏、持久授权和坐标操作不支持。

截图只把验证后的 PNG 副本保存到 MuLi 数据目录，不删除 Portal 原文件，也不因此扩大文件工具的工作区。缺少 Gio、Portal 接口或会话权限时会明确失败。设置页能力检查或 `media_capabilities` 可刷新状态，未探测或过期的状态不能当作已授权。

### 保存位置与配额

`computer.capture` 还可设置 `device`、`max_seconds`、`portal_timeout_seconds`、`fps`、`max_file_bytes`、`max_total_bytes`：

- 录屏默认上限 30 秒、8 fps，单文件最多 32 MiB，采集目录合计 200 MiB。
- Portal 授权等待默认 60 秒，可设 1–300 秒，与录屏时长独立。
- 文件随机命名，保存于数据目录 `captures/` 并进入附件库，不接受任意输出路径。
- 取消、失败、格式无效或超额的未完成文件会删除；已经完成的文件不会自动清理，满额后需明确清理。

系统权限、桌面会话和应用行为由实际部署环境决定。工具已安装或状态显示可用，不代表屏幕录制授权、无障碍授权或目标应用动作一定成功。
