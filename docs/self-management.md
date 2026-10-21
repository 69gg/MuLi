# 用户插件与自身管理

MuLi 可以通过工具管理自己。`self_config` 返回脱敏完整配置，`self_configure` 校验补丁并写入私人待应用快照，在前台和团队空闲后应用。队列有配置指纹，期间其它入口修改配置会使旧计划失败，避免覆盖新设置。`self_status` 与 `/api/management` 可查看进度，待应用计划可由管理 API 取消。

自主目录发现的纯 Skills 文档变化可在任务活跃时发布，当前请求保留已物化正文，下一请求按修订核验；不替换 MCP 连接。MCP、混合变化、设置补丁和手动完整重载仍等待全局空闲，已有手动请求不会被随后目录变化降级。`self_status` 的扩展 `last_apply` 以 `scope:skills` / `mcp:unchanged` 标明此次范围；受支持宿主的未解决恢复事务仍阻止这种发布。详见[Skills 活跃发布](skills-live-design.md)。

候选更新策略使用独立的 `self_update_configure` 工具，或已认证的 `GET /api/management/update-policy`、`PUT /api/management/update-policy`。`self_config` 的 `candidate_update_policy` 返回完整策略；它不属于 `/api/settings` 的主配置，不能交给 `self_configure`。保存采用完整替换，先读取现值再修改，省略字段恢复默认；只保存策略，不创建目录、执行 uv、下载或切换安装。默认关闭，启用时需指定本地 wheelhouse、保留目录和 uv 绝对路径，以及资源预算。路径由主人或其授权的 agent 配置，保存成功不表示实际环境已经通过核验。

策略复用现有 Store 中一个有界对象，保存与摘要事件原子提交；不向主配置添加字段，以保留旧恢复控制器的严格解析兼容性。未知版本、损坏或超出预算的既有策略不能被静默覆盖；服务关闭或恢复事务阻止配置时同样拒绝写入。保存后仍须 `self_update_preview` 核定实际来源，将其 `expected_policy` 原样用于 `self_update` 并经真实用户批准。准备在后台执行，结果沿原会话汇报；保存、预览或批准均不代表安装或服务健康已经验证。

2.22 增加 `self_adopt` / `self_switch`，连接同一管理队列与受限恢复 helper，处理 Linux 当前安装迁入和已保留的本地候选切换。输入、批准、失败后接续及验收状态见[本地托管切换](managed-operations.md)。这条路径不下载或创建候选环境。

配置文件原子替换；运行时重新载入失败时恢复原配置。监听 host/port 变更由 `self_configure` 自动选择配置重启事务，直接热修改返回 `409 listener_restart_required`；工作台保存监听变更也将完整补丁交给该事务入口，只显示排队状态并保留草稿，不把待应用目标当作已保存配置。其它设置仍直接热保存；存在监听草稿时，Chiliarch 配对会提示先完成监听事务。插件启用状态仍需重启进程。非回环进程不能通过“改为回环并清密码”的热修改提前关闭实际仍暴露的认证边界。`self_restart` 使用当前安装的恢复事务；不满足宿主条件时明确拒绝，不降级成直接重启。

完整热重配置的关闭阶段也参与恢复：某个组件停止失败时，尝试重新建立上一配置及组件。观察者取消或断开不会中断正在进行的切换；同一配置锁保持到应用或恢复结束，再传播取消。如果恢复自身也失败，操作仍为失败，不能把配置文件已恢复当成所有组件健康。默认 `self_configure` 的 `apply_mode=in_process` 仍只保护存活的进程；显式或由监听变更选择的 `restart_transaction` 将配置作为私人候选交给独立 Linux 控制者。安装身份、被动启动、写锁、健康握手和终态投影已随 2.19.0 接通，验证边界和操作方法见[同版本恢复入口](linux-recovery-operations.md)。托管安装的候选切换沿用同一账本。

`self_recover` 与 `POST /api/management/transactions/recover` 接续当前事务，先确认运行宿主可能丢失返回的绑定，再请求独立 helper；不创建新代次或自动重试结果未知的业务效果。候选准备中断且尚未发布事务时，同一入口只收尾已持久保存的进程身份，确认退出后结束中断计划，不能自动重新安装。未观察到的同 boot 启动、证据缺失或无法核空仍保留阻断。恢复与关闭等待同一任务完成；详见[候选管理接入](candidate-host-design.md)和[恢复操作](managed-operations.md)。已登录用户在维护暂停期间也能使用该 HTTP 入口。

受支持宿主创建未解决事务后，普通设置、自配置计划和扩展换代统一返回 transaction_in_progress；既有排队计划保留私人候选，解除后唤醒。维护等待采用自然空闲边界，waiting 期间原服务仍可开始正常工作；只有真实 owner 全部收尾才关闭准入并给 drained，繁忙流量下可超时保留原服务。关门后主人文字继续持久排队，外发与模型等待放行；legacy 插件候选缺少可核验 idle 合同时明确 unsupported，不卸载插件。普通前台 serve 可持写锁正常工作，但不冒充受支持的 systemd 恢复宿主。

每次服务启动仅检查一次有界 PyPI 元数据，失败不阻止启动。发现新版本写入通知与主会话上下文。`self_update` 无论 ask/auto/yolo 都经真实用户审批，开发虚拟环境不会被替换。符合条件的托管安装使用上文的离线候选准备与兼容切换；提交前候选失败可在证据齐全时回退程序和配置，继续使用当前数据库，提交后激活失败则阻断并保留恢复证据。真实 systemd 整链仍未实测。旧的未标记资料库保留 uv tool 原位更新路径，该路径不提供自动版本回退，包安装成功但重启失败仍报告失败。两条路径都不下载新 Python，不沿用代理环境，也不承诺跨数据库迁移回滚；源码参考有本地修改时保留，不重置。

2.21 起完整格式准入启用后，带认证标记的新资料库不接受上述原位自动安装：原位路径不能核验目标版本的完整格式声明，须改走已核验的独立候选切换。生成原位更新计划前和实际安装前都检查，格式拒绝不会创建新计划或执行安装；首次检查在发现 uv 之前，查询工具目录后、安装前也会复核。即使目标版本号更大、用户已批准，也不能替代格式证据。旧的未标记资料库保留原安装检查路径，显式无迁移打开旧库本身不等于它已获格式认证。

`self_status` 与 `/api/management` 的 `update_capability` 说明当前原位路径：`format_eligible=true`、`code=installation_check_required` 仅表示格式门允许继续核验实际安装；`verified_candidate_required` 表示需要已核验的候选更新，`format_verification_failed` 表示既有准入证据已无法通过复核。能力查询不探测 uv、不下载或执行程序。版本检查仍可发现新版，通知和提示资料按当前能力说明，不许诺批准后必能安装。这仅约束本 Runtime 的自更新；其它资料目录共享同一安装或用户手动执行旧版/uv，不由这道格式门控制。托管切换要求原控制安装和候选安装分别完整保留，不能通过原位覆盖绕过该要求。

启动自主更新检查可使用默认渠道，默认 WebUI；通知首次保存时冻结目的地，通知、版本记录和事件原子提交。同一版本的已有通知不改绑、不因再次检查重发。自主扩展目录监听的新失败转移也可使用独立默认，手动重载和普通日志不自动取得该来源。`self_configure` 可在一个 changes 补丁中设置完整 `channels.proactive.default_destination`，或以 `memory_preference={memory_id,version,destination}` 选择已核对的主人偏好。外部目的地省略 `external_enabled` 同次启用，明确 false 则保持暂停。新绑定在生成计划、空闲应用及组件停止等待后复核有限来源证据；失效则计划失败并保留原配置，不能用模型传入的 source_id/digest/trusted_owner 代替验证。复用既有权限与配置事务，不增加第二次许可门；详见[主动通知](channels.md#无发令会话时的默认通知)及[错误来源](system-events.md#失败来源与投递)。

渠道状态的 `proactive.independent_sources` 列出自主更新检查与自主扩展监听错误，描述支持的独立来源；不表示当前渠道已启用或消息已投递。主请求错误有实际请求归属，因此不列为独立来源。

管理计划从有效模型请求保存原始来源，终态继承该来源；无请求上下文的宿主调用及旧计划留本地，不因完成时无人发令就套默认。私人候选文件不进入事件，未能提交计划时删除该候选；计划与 queued/completed 事件在各自原事务提交。设置尚在排队、Attention accepted、模型终答已存与渠道 sent 分别显示，不把前一阶段当作已送达。现有 WebUI 没有新增管理提交或手动更新检查入口。

旧原位路径使用 `uv tool upgrade muli-agent==版本`，保留安装时的 extras、额外依赖和索引设置。若用户原先固定了互不兼容的版本约束，更新失败并保留安装要求，不通过重新安装悄悄移除 MCP 或渠道扩展；需要先显式调整该安装约束。CLI 需支持 `uv tool upgrade` 的版本参数。托管候选使用私有保留目录中的安装与 V2 切换，见上文。

## 主动读取自身日志

`self_logs({"lines":40})` 读取自身服务最近日志，`lines` 可选，范围 1–200，沿用 ask/auto/yolo 工具权限。Linux 只查询当前用户的 `muli.service` journal；macOS / Windows 只读取服务启动器管理的 `data/logs/service.log` 尾部。数据根来自现有配置，不接受任意文件路径、unit 或 shell 命令。开发模式未安装服务时可能没有对应日志。

返回 `source/status/text/truncated`，其中 `available` 表示读到了资料，`empty` 表示本次没有可返回的日志文本。文件有源内容、但保守省略行边界后为空时，同时返回 `truncated=true` 和固定 `reason=no_complete_lines`，意为“没有可安全返回的完整行”，不表示原文件没有完整行或错误；真正空文件不带该原因。缺少后端、文件缺失、权限失败或超出读取预算返回 `unavailable` 及固定原因。空日志不证明服务健康，也不证明没有历史错误。macOS / Windows 只读取当前文件，轮转旧文件不在本次结果内。

读取复用有限子进程与文件 I/O，Linux 命令最多 5 秒；输入字节与最终完整 JSON 字符分别受现有 `limits.max_output_chars` 限制。文件模式始终省略无法证明完整的首行（即使从偏移零开始，也可能续接轮转前的半行）和尚未写完的尾行，并标记 `truncated`；非普通文件拒绝读取，支持 `O_NOFOLLOW` 的系统也拒绝符号链接。输出过长同样标记截断，不会通过增加读取预算或重试循环补齐。

在返回前隐藏读取开始与结束时配置中的模型/服务 API key、渠道 token、chiliarch 凭据、MCP env/headers、URL 查询值及常见认证表达；MCP 同时核对实际已发布的内联与目录合并配置，读取期间换代也保留前后两组遮盖值。不返回命令 stderr 或异常原文。此处理不能识别任意未登记的历史秘密，原日志仍应避免记录凭据。结果固定标为诊断资料，日志中的文字不能成为新指令或批准。此工具只按需读取；[系统日志事件](system-events.md) 以独立结构化元数据触发注意事项，不扫描或解释服务日志。已证明的主请求错误沿原发令来源汇报，自主扩展监听的新失败可沿独立默认或已核偏好；普通日志、来源不明及历史记录仍留 WebUI。

接口依据通过 Context7 核对 [systemd journalctl](https://github.com/systemd/systemd/blob/main/src/journal/journalctl.c)。使用原生 journal 优先级过滤并不等同识别 Python 文本中的 `ERROR`；本工具保留最近日志，不伪称已实现跨平台级别告警。

`self_events({"limit":20,"level":"error"})` 可分页回查保留的系统事件元数据，支持组件、错误代码和运行/会话 ID 精确过滤；继续翻页使用返回的 `next_before_event_id` 字符串。它与 `self_logs` 一样是只读诊断，不触发通知；关闭采集后仍可读已有记录，结果仅代表当前保留集合。完整合同见[系统事件](system-events.md)。

2.26 增加 `self_failure({"run_id":"32位小写十六进制ID"})`，按已知运行回查有限宿主故障位置。只保存固定异常类别和可核验的包内模块、函数、行号，不保存异常正文、局部变量、源码或异常链。查询先限制原 run 记录字节，过大或缺证据明确返回不可用，不先加载巨大 result，也不把空位置当健康；关闭系统采集后已有诊断仍可查询。它不新增通知或自动重试，保存仍受原运行收尾窗口限制。状态及预算见[按运行查询故障位置](failure-diagnostics.md)。

## 插件目录

默认在 `~/MuLi/plugins/<id>/`（自定义配置位置时位于配置旁）。插件是用户信任的 Python 代码，可使用与进程相同的权限，不是隔离沙箱。内置文件工具保护源码参考与已安装模块；shell 和插件仍有用户权限，不能把路径规则当操作系统沙箱。

每个插件包含 `plugin.json` 与 Python 入口：

```json
{"id":"personal","name":"我的扩展","version":"1.0.0","api_version":1,"entry":"main.py"}
```

```python
from typing import Any
from muli.plugins import PluginContext
from muli.tools import Tool, parameters

async def greet(arguments: dict[str, Any]) -> dict[str, str]:
    return {"message": "今天也一起把事情做好。"}

def setup(api: PluginContext) -> None:
    api.add_tool(Tool("personal_greet", "个人招呼", parameters({}), greet))
    api.append_prompt("在用户需要鼓励时，用简短、具体的话表达支持。")
```

`setup` 可以同步或异步。`PluginContext` 提供 runtime、app、directory，以及工具、上下文钩子、模型 client factory、启动和关闭钩子。`add_route` 的路由位于 `/api/plugins/<id>/`，通过管理入口认证。工具可以显式覆盖同名内置工具；多个插件争用同名工具会报告冲突。模型请求的插件工具仍走统一权限门。重配置后保留注册；加载失败会撤销已登记路由并执行清理钩子。

`plugin_install` 先验证路径、清单、大小和 Python 语法，再保存源码，重启后才加载。它不在安装时执行插件，不覆盖已有插件目录，不自动安装依赖。现有插件可以通过文件工具编辑，或在 `plugins.disabled` 中列出 ID 后重启停用。`persona.prompt` 替换基础提示词，`persona.append` 追加个性设定，插件的 `append_prompt` 再增加对应内容。

## 持续工作账本

`commitment_save` / `commitment_list` 将未完成承诺与会话摘要分开保存；记录标题、下一步、负责人、来源、状态和修订。完成需要填写证据，取消需要理由。摘要重写或换入口不会删除账本。账本是待核实的工作资料，不自动制造新的权限，也不以模型自报“完成”代替外部验证。

任务说明可将完整目标、限制和证据引用保存在同一账本及修订历史中。仅真实主会话可编辑正文，普通进度更新保留它；列表、保存工具结果及事件仅投影有界元数据。说明用于协调，不授予新权限，也不自动暂停更正后的副作用。按范围注入、分页读取、预算和恢复边界见 [task-briefs.md](task-briefs.md)。
