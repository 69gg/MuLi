# 按运行查询故障位置

2.26 增加按运行查询故障位置。已知失败运行 ID 时，可以调用：

```json
{"run_id":"0123456789abcdef0123456789abcdef"}
```

工具名为 `self_failure`，只读，沿用现有工具权限。可先用 `self_events`
取得运行 ID；本工具不接受任意路径、异常文本或“最近一次运行”推断。
响应标为 `diagnostic_data_not_instructions`，不能据此批准命令或重做未知副作用。

`status=available` 返回 `diagnostic`：版本、覆盖范围 `muli_package_only`、
有限异常类别、模块/函数/行号，以及 `truncated`。只识别有限精确内置异常类型；
自定义或第三方类别为 unknown。外部、嵌套或无法证明当前代码绑定的位置统一
显示 unknown/null，不暴露文件绝对路径。空位置不表示没有故障。

其它状态使用固定原因：

| 状态或原因 | 含义 |
| --- | --- |
| `not_found / run_not_found` | 当前保留集合找不到该 run，不能据此推断曾否失败 |
| `unavailable / run_not_finalized` | 仍是 queued/running，尚无可查询的终态诊断 |
| `unavailable / diagnostic_not_recorded` | 终态没有诊断；可能是旧运行、采集关闭、提取失败或终态本来无需采集 |
| `unavailable / run_record_over_budget` | 原运行记录超过读取上限，没有解析诊断，不能视为无故障 |
| `unavailable / run_record_invalid` | 原记录结构或编码不可核验 |
| `unavailable / failure_diagnostic_invalid` | 诊断版本、字段或大小不符合当前协议 |
| `unavailable / run_read_failed` | 本次资料库读取失败 |

系统采集 `system_events.enabled=false` 时不新增诊断，已有记录仍可读。
`capture_level` / `notify_level` 仍只控制原系统事件采集与提示；此诊断是随
failed run 保存的按需证据，不按级别另发通知。
只采集宿主捕获的普通运行失败，不为正常暂停或取消采集。它不保存异常消息、
局部变量、源码或异常链；原有用户可见错误描述不受此片改写。

实际最多访问 16 帧，达到上限即保守标记截断；完整诊断 UTF-8 JSON 不超过
4KiB。查询仅按主键读取一个 run，完整原 JSON 解析上限为 32KiB，SQL 哨兵
窗口最多 32769 字节。超大运行不会通过提高预算、全库扫描或先加载 result
来补齐。完整工具响应沿用 `limits.max_output_chars`，必要时再减少帧并标记
截断；不裁坏 JSON，`preserve_output` 保留交给后续模型请求的完整资料。

诊断随既有 run 终态一同保存、保留和删除，不新建日志出口或自动通知。
保存仍在运行异步清理之后；进程提前退出或清理失败可能没有终态证据，
不承诺崩溃前一定落盘。本工具也不查询原生日志、提供任意 Python traceback
或证明系统健康。具体设计和离线验收见[实现合同](failure-diagnostics-design.md)。
