# 可选 Linux 桌面通知

工作台的通知记录可以同时显示为 Linux 桌面通知。此功能默认关闭，只复用系统已安装的 `notify-send`，不安装 Python 包或系统组件。macOS、Windows 显示 `unsupported`，没有跨平台替代实现。

这里的桌面弹窗与[Attention 主会话说明](attention.md)、[无发令会话时默认渠道](channels.md#无发令会话时的默认通知)分别配置。默认渠道仍 WebUI，第一期只允许启动自主更新检查选默认；原任务提醒继承请求来源，旧通知保持本地。桌面 enabled/失败/已读不会更改渠道目的地、复位 blocked/unknown 或产生一次新的 fallback 发送。通知已保存与模型已整理、渠道 sent 是不同阶段。

在配置中启用：

```json
{
  "notifications": {
    "enabled": true,
    "timeout": 5,
    "max_queue": 32,
    "expire_ms": 8000
  }
}
```

`enabled` 必须是 JSON 布尔值；其他字段可省略使用示例默认值。`timeout` 是单次进程运行时限，允许大于零至 30 秒；`max_queue` 为 1–128 条待发送副本；`expire_ms` 为 1000–60000 毫秒的显示时间建议，桌面通知服务可能忽略该建议。发送不会等待用户关闭通知，也不添加可执行动作，并请求静音提示。

需要 MuLi 进程能连接当前用户的桌面通知服务。`muli doctor` 的 notifications 能力检测只检查 Linux 平台与 CLI 是否可用，不发送测试通知，也不保证 D-Bus 桌面会话可达。以用户服务运行时，应保留正确的图形会话环境；CLI 存在而发送失败时，状态会如实显示错误，不会尝试安装程序、修改系统服务或切换用户。

## 事件与生命周期

`DesktopNotifier(store, bus, config: DesktopNotifications)` 提供 `await start()`、`await close()` 和同步 `status()`；`notification_capability()` 为 CLI 检测提供无发送行为的能力信息。运行时负责在配置应用时创建实例，在提醒调度器启动前启动通知消费者，在重新配置与关闭数据库前先关闭旧实例。

启动时先记录当前 Store 最大事件游标，仅消费之后的 `notification.created`。提醒和助手通知的事件可以携带完整内容；chiliarch 的仅 ID 事件从持久化通知记录读取标题和正文。闲置时等待 EventBus 的 Condition，不调用模型、不定时轮询外部服务。

消费者和发送进程在独立后台任务运行。队列只保存有长度限制的文本，不会阻塞通知事件的生产者；队列满时丢弃新来的桌面副本并累加 `dropped`，工作台中的持久记录保留。关闭会取消正在运行的进程并丢弃待发送副本。重新启动从新的当前游标开始，不补发停机期间的旧桌面通知；服务恢复后仍可在工作台查看旧记录。

单次发送失败或超时仅更新通知模块状态，不重试该条副本，不阻止提醒落盘、chiliarch 消息接收或后续通知处理。后续发送成功恢复 `ready`。意外后台错误会停止该实例的两个后台任务，等待明确重新配置或重启恢复。

## 内容与状态边界

发送复用统一的 `run_process`：参数列表调用、没有 shell、用户内容放在 `--` 之后。标题默认最多 120 字符、正文最多 2000 字符，按最终命令参数计量；正文及标题的 XML 标记转义，控制字符移除，实体不会被截断。正文中的反斜杠额外转义，防止 `notify-send` 的 `g_strcompress` 将字面八进制转义还原为链接或图片标签。通知不加载内容中的本地图片或远程媒体。

构造参数 `max_title_chars`、`max_body_chars`、`process_output_bytes` 可供嵌入者调整，默认进程输出预算为 4096 字节；达到输出限制时，统一进程执行器停止子进程。输出与原始异常正文均不进入状态，避免泄露通知内容、环境信息或 D-Bus 地址。

`GET /api/status` 中的 `notifications` 包含配置开关、平台支持与 CLI 检测、`status`、`running`、`sending`、`queued`、`sent`、`failed`、`dropped` 和安全的 `last_error`。状态包括 `disabled`、`stopped`、`unsupported`、`unavailable`、`ready`、`error`。计数属于当前实例；`sent` 表示 CLI 成功返回，不代表桌面已经展示或用户已读。工作台记录的已读状态独立维护。

## 离线验证

`tests/test_notifications.py` 使用临时 SQLite、真实 EventBus 和模拟进程，验证启用配置、平台与 CLI 检测、启动游标、Condition 等待、仅 ID 事件、参数及转义、长度、队列、超时、失败恢复和停机取消。测试会阻止真实 `notify-send` 执行，不向本机桌面发送通知。

参数与转义依据：[libnotify 的 notify-send 官方实现](https://github.com/GNOME/libnotify/blob/master/tools/notify-send.c)、[GLib 命令行参数解析](https://docs.gtk.org/glib/goption.html)、[桌面通知规范](https://specifications.freedesktop.org/notification/latest-single/)。
