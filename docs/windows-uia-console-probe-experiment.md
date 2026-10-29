# Windows UIA 控制台创建模式实验

这是隔离分支上的受控诊断实验，尚未证明启动失败的根因，也不是生产修复。
生产代码、PowerShell 脚本和普通 CI 的启动方式保持原样。

2026-10-29 的 B2 发布 run `40812013110` / job `123244282342`，在 Python 3.12 的
首个 no-walker 探测中等待原回执 12 秒超时。一次 write/drain 已返回，stderr 完整观察为
零字节、阶段为空，清理确认。相同 job 后续探测与 owned Unicode/MTA 树旅程通过。
这些事实没有证明 PowerShell 未执行脚本，也没有证明 CPU、Python 版本或 UIA 是根因。
[实际失败 job](https://github.com/69gg/MuLi/actions/runs/40812013110/job/123244282342)。

## 唯一行为变量

专用 `windows-native.yml` 在 push `codex/windows-native-*` 或手动选择
`probe-dependency` 时，运行 Python 3.12/3.13 × `create-no-window`/`default` 四个 job，
最多两个并行，每 job 仍为五分钟。每个 job 的两个原 no-walker 参数各调用一次原探测，
没有预热、第二次请求、失败重试或期限放宽。首个 `[None]` 是主要启动观测；后一个参数
不能当作同环境冷启动对照。

`MULI_TEST_UIA_PROBE_SPAWN_MODE` 只在该探测 step 设置。测试包装器精确匹配这一个
owned child 的 argv：`create-no-window` 保留 `CREATE_NO_WINDOW`，`default` 仅把它改为
`0`。固定诊断只输出模式与传给原 spawn 的 flags，不输出脚本或请求正文。未知模式
（包括空字符串）明确失败；未设置环境变量时不安装包装器。普通 CI 与手动 `all-native`
均不设置此参数，保持原行为。

系统 PowerShell 路径、`-NoLogo -NoProfile -NonInteractive -Mta -File`、脚本、三条 PIPE、
12 秒总期限、唯一请求与原清理路径都保留；原 dependency/MTA/no-window/module 及
no-tree-walker、closed/reaped/readers-settled 断言全部保留。包装器不涉及 `native_form`。

Windows 官方文档明确：`CREATE_NO_WINDOW` 不设置控制台句柄，默认创建模式与其不同；
标准 PIPE 句柄由另一项合同控制，不能因此声称原 PIPE 无效。
[Process Creation Flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)、
[STARTUPINFOW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-startupinfow)、
[Creation of a Console](https://learn.microsoft.com/en-us/windows/console/creation-of-a-console)。
`-File <path>` 与 `-File -` 的区别、MTA 与无 profile 行为按
[PowerShell 5.1 官方合同](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1)
保持，不改成 `-Command`，不修改执行策略。

## 判读与边界

若原模式失败而 `default` 的原断言全过，只能获得控制台创建模式相关线索；不同 VM 与
时段仍会混杂，不能宣布根治。`default` 仍失败、两边都通过或出现窗口断言失败，都必须
保留真实结果。`flags=0` 不保证 parent 实际拥有控制台，不应称为“已确保子进程有控制台”。
新的明确阶段只能缩小等待边界，不能替代原成功断言。

离线 fake 回归只验证包装器的精确作用域、三 PIPE/argv/期限不变、一次 spawn/请求、
异常身份及原清理，不运行 PowerShell 或桌面；远端实际矩阵另行记录，不能以 fake
通过冒充 Windows 探测已恢复。任何未来生产修改仍需审查无窗口合同与完整原生旅程。
