# 无障碍电脑操作

2.4 完善 Linux AT-SPI 的应用和窗口定位。电脑能力默认关闭，使用已安装的系统 Python GI，不在 MuLi 环境下载或编译 GTK。macOS AX、Windows UIA 的节点操作仍未实现；各平台截图、录屏和后台服务的范围见[平台能力](platforms.md)。本轮测试只使用假无障碍对象与临时进程，不操作开发机桌面。

## 选择读取范围

调用 `computer_targets({})` 获取桌面直接应用。应用条目给出临时 `application_id` 和 `target_id`；传 `application_id` 再调用 `computer_targets`，读取那个应用的直接窗口或容器。发现只访问这一层，不预先扫描每个应用的所有后代。应用本身也能作为目标，兼容没有标准顶层窗口结构的程序。

`computer_snapshot({"target_id": "本次发现返回的标识"})` 从选定应用或窗口读取子树；不传 `target_id` 时保留原先的桌面快照语义。目标必须来自当前宿主缓存，模型不能自行指定进程、D-Bus 路径或节点下标。新发现替换旧发现集合；过期、未知或变化的目标明确失败，不按名字寻找替代项，也不退回整个桌面。

快照返回 `snapshot_id` 和节点 `id`。后续 `computer_action` 的动作必须来自该节点公开的 `actions`，`computer_type` 用于设置节点的**完整文本**。原生定位信息留在宿主内，模型只使用临时标识。一次操作消费一次快照；需要下一次操作时重新读取。

节点以 `parent_id` 保留公开的父子关系，`depth` 相对于所选根计数。目标根没有父节点，`parent_observed=true`；祖先未能读取时为 `parent_id=null, parent_observed=false`，不能当成平级根节点。条目按父节点先于子节点排序，预算裁剪后不保留悬空父引用，可区分不同面板中的同名控件。

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
    "backend": "atspi",
    "python_path": "/usr/bin/python3",
    "max_nodes": 300,
    "operation_timeout_seconds": 12,
    "snapshot_ttl_seconds": 60
  }
}
```

`operation_timeout_seconds` 是一次 helper 的总期限，大于 0、至多 120 秒；`snapshot_ttl_seconds` 同时约束快照与发现引用，大于 0、至多 300 秒。两者必须是有限数值。输出预算仍使用现有 `limits.max_output_chars`，无需再设置另一套预算。旧配置缺少这些字段时使用原来的 12 秒操作期限和 60 秒快照有效期。

## 依据与验证边界

实现沿用 [AT-SPI 节点接口](https://docs.gtk.org/atspi2/class.Accessible.html)、[D-Bus 原生身份协议](https://gnome.pages.gitlab.gnome.org/at-spi2-core/devel-docs/doc-org.a11y.atspi.Socket.html)及[节点状态定义](https://docs.gtk.org/atspi2/enum.StateType.html)。本轮不引入常驻桌面观察进程、坐标输入、OCR 或跨平台原生引用服务。

Fake GI 可验证遍历、身份变化、错误和预算；核心跨平台 CI 可验证宿主协议。它们不能替代真实 Arch 桌面上的 GTK、Qt、Electron 应用验收，也不能证明 macOS 或 Windows 无障碍后端可用。实测状态随[版本审查](review-v2.4.md)更新。
