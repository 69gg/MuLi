# 配置、迁移与运维

## 配置入口

所有常规配置可以从工作台设置页面管理。高级配置为严格 JSON；未知键被拒绝。模型的 reasoning / extra_body / extra_headers 为对象，PATCH 时按整体替换；留空 api_key 保留原密钥，clear_api_key=true 显式清除。读取 API 返回 api_key_set / credential_set，不返回密钥。包含 token 的自定义请求头仅显示占位符。

协议基础地址需包含供应商所需版本前缀，例如 `https://provider.example/v1`；不要把 `/chat/completions`、`/responses`、`/messages` 当基础地址。具体字段参见 providers.md。环境变量名称由用户选择，默认不假定任何供应商。

搜索服务同样由用户指定，避免默认把个人查询发送到不明公共实例。SearXNG 需开放 JSON 格式；Brave base URL 为其 API 根地址，Tavily 为其服务根地址。

## 旧版本迁移

旧基线使用仓库根目录 config.json、history/dialog.json 和导入时启动的 MCP。v1.0.0 使用 XDG 配置与 SQLite 数据；当前版本默认集中到 ~/MuLi，不自动加载旧密钥、不自动启动旧 MCP 命令，也不自动搬移已有数据。可继续通过 MULI_CONFIG / MULI_DATA_DIR 指向旧位置，迁移前保留原文件。

可按以下对应关系在新工作台配置：

| 旧字段 | 新字段 |
| --- | --- |
| model_config.main_model.model_name | models.main.model |
| model_config.main_model.api_base_url | models.main.base_url |
| model_config.main_model.api_key | models.main.api_key 或 api_key_env |
| provider_type=openai/deepseek | protocol=openai_chat |
| model_config.json_model | 可用作 models.reviewer，需检查该模型支持普通 JSON 输出 |
| tools_api_config.web_search.base_url | search.base_url |

历史消息不自动转成可信长期记忆；有价值事实可通过记忆入口导入并标明来源。旧 Docker 工具不再默认启动容器，命令工具使用当前用户权限与运行预算。

## 无障碍电脑操作

Linux 需要桌面会话 D-Bus、AT-SPI、系统 Python GI。Arch Linux 使用系统发行的 python-gobject / at-spi2-core；不需要在 MuLi 虚拟环境编译 GTK。设置 computer.python_path 指向具有 GI 的系统 Python，然后启用电脑工具。先在状态页查看能力。

`computer.backend=auto` 按平台选择 Linux AT-SPI 或 macOS AX，不在失败后更换后端；旧配置中的显式 `atspi` 不迁移。macOS AX 使用当前 MuLi Python 的系统框架，`python_path` 不影响它。能力查询不会弹出辅助功能授权、扫描桌面或保留节点引用；实际授权归属与第三方应用行为仍需本机受控验收。队友不能借用另一个执行的原生引用：先 `computer_release`，再让接手者重新观察。`desktop_busy` 应交回统筹处理，不用模型循环等待。

`computer.operation_timeout_seconds` 控制单次无障碍 helper 的总期限，默认 12 秒，可配置为大于 0、至多 120 秒的有限数值；`computer.snapshot_ttl_seconds` 控制快照及目标发现引用的有效期，默认 60 秒，至多 300 秒。两者不接受布尔值、字符串或无穷大。旧配置无需修改，缺省时沿用原期限；修改配置不会延长已有操作凭据。

操作路径是 snapshot → action/type → 再次 snapshot。快照具有有效期，Linux 操作校验进程及 D-Bus 对象身份，成功或失败都消耗快照。界面不支持的动作不会降级成任意坐标点击。无障碍树不可用时可单独请求截图；截图不产生可执行节点身份，不能代替操作前的快照。实验性 macOS AX 使用归属于当前会话/执行代次的短期 helper 保留原生引用，动作或释放后须重新观察；已完成离线原生接口测试，但真实 TCC 授权和桌面动作尚未实测。Windows UIA 与任意坐标操作仍未实现，见[电脑操作合同](computer.md)。

桌面图像采集另由 computer.capture.enabled 开启，默认关闭。截图与有界录屏保存在数据目录 captures/，随后可作为本地附件使用；不自动上传、不录制声音。工具沿用 computer 风险及 ask/auto/yolo 权限模式。单文件默认 32 MiB、目录默认 200 MiB，默认录屏上限 30 秒、8 fps；超过配额或取消时终止采集进程并删除未完成文件。后端与授权边界见 [平台能力](platforms.md)。

2.1 起支持显式 computer.capture.backend=portal 的 Linux 截图，复用 computer.python_path 的系统 Gio，不要求 AT-SPI 可用。保持 auto 选择顺序；Portal 不提供录屏或失败降级。portal_timeout_seconds 单独控制交互等待，默认 60 秒；可在截图高级配置或管理 API 中调整。能力查询不触发截图授权，真正截图仍可能显示系统对话框；当前只完成私有总线模拟验证，未在真实桌面采集。

## 数据与恢复

数据目录保存 muli.sqlite3（含 WAL）、文件回收站、attachments/ 附件库，以及启用后生成的 captures/ 和 voice/。备份运行中数据库应使用 SQLite backup API 或停服务后完整复制，不能只复制活跃数据库主文件。配置与数据库包含个人资料，勿提交 Git。网页录音、截图、录屏、声纹参考与合成音频需要另行备份；NDJSON 数据导出不包含这些二进制文件。

新启动工厂独占创建的资料库在核心、记忆、渠道和主身份准备完整后写入格式标记。以后启动先核验其完整合同与身份，未知或损坏标记会明确失败，不静默迁移。历史未标记资料库保持原路径，不根据空文件或相似表结构自动补证。初始化中断留下的未标记库也不会在下次启动自动认证；保留现场供诊断，不要手工删除标记来绕过检查。构造锁只保护初始化，不能当作整个运行期的独占写锁，详见[格式准入](fresh-format-admission.md)。

已标记资料库目前不支持原位自动更新，因为当前更新器尚不能核验候选版本的完整格式声明。新版发现仍正常工作，`self_status` 和 `/api/management` 返回实际的 `update_capability`；用户批准不能代替兼容证据。旧未标记资料库继续使用原安装检查路径，见[自身管理](self-management.md)。

2.22 的本地候选入口在显式迁入后，由固定启动器和 controller 接续同一事务，保留最新数据库。该路径不下载、不创建候选环境；保持原控制安装完整，避免手动原位覆盖。操作合同与验收状态见[本地托管切换](managed-operations.md)。

设置中的“导出个人数据”下载 NDJSON，包含会话、运行、待处理输入、团队与管理账本、记忆原文、事实与关系证据、冲突、社区、修订、向量和渠道投递记录。导出逐批读取，不加载整个数据库，不包含配置文件或渠道协议凭据缓存；它是可读数据导出，不是跨表一致性备份，也不包含附件及文件回收站的实际文件。对话和工具结果本身可能包含用户曾发送的敏感内容。需要可恢复备份时仍使用 SQLite backup 或停服务复制，另行保存配置与本地文件。

重启后，残留的未完成运行、审批和工作流标为中断，对应会话回到 idle。恢复先按未完成状态筛选并分批处理，不受较新历史数量影响；已暂停和终态记录保留。可回看已完成步骤，再显式发起新任务；不会自动重放文件写入或桌面点击。chiliarch 收件使用事务先落盘再 ACK，重复身份内容不一致会拒绝。

网页当前运行与审批按真实执行槽、未结束 future 和持久状态一起核验，旧 started/requested 事件不重新产生执行或授权。恢复变更带持久失效事件，已有网页重新接通 SSE 也会结束旧运行；未确认的工具结果显示“结果未知”，需先核查实际状态。可见历史与字节预算边界见[会话恢复](session-recovery.md)。

2.9 为生命周期和精确父运行查询增加两个幂等 SQLite 索引，首次打开旧库需要扫描已有对象一次；后续恢复只取候选 ID，再逐条处理正文。它不删除历史、不重新提炼记忆、不启动模型。工作流设置中的活动入口独立分页，任何未完成执行都会阻止定义删除；仅有终态历史时可以删除定义，运行历史保留。

2.11 增加会话创建序的幂等 SQLite 索引，旧库首次打开会扫描已有对象建立索引，不改写旧记录或增加归档表。标题查询先筛完整集合再分页，仍需扫描候选标题；无全文索引或语义查询。页只保留连续完整摘要，超长旧标题无法放入预算时明确报错，不裁短 ID 或静默跳过。旧实时首屏与全局 SSE 的资源边界未在本片改变，见[记录目录](session-records.md)。

## 服务与故障定位

2.14 增加按 `ingress.run_id` 查询的幂等部分索引，首次打开历史库需扫描已有收件。它让很早的注意事项运行也能被精确识别，防止迟到错误再次唤醒模型；不删除历史或启动模型。系统日志只保留有限原记录及对应事件，不限制整个数据库大小，详见[系统事件](system-events.md)。

- `muli doctor`：版本、配置位置、模型配置状态、桌面与服务能力。
- `muli service status`：用户服务状态和最近日志。
- `muli management status`：只读查看现有恢复事务；不打开业务数据库或创建缺失目录。
- `self_recover` / `POST /api/management/transactions/recover`：先确认当前运行宿主，再接续现有控制者；不创建新代次或延长原期限。
- `muli management recover`：宿主不在运行时，可从固定控制安装接续现有控制者；不会自动确认其它运行宿主内存中的丢返回绑定。
- `/api/docs`：自动生成的管理接口文档。
- `journalctl --user -u muli.service -f`：服务日志。

服务管理按当前系统选择 systemd 用户服务、macOS LaunchAgent 或 Windows 当前用户计划任务。install 只安装，enable 只管理登录自动启动；start、stop、restart 分开执行。不会使用管理员权限或创建系统级服务。Windows 必须有当前 Python 环境对应的 pythonw.exe，避免弹出控制台；服务内重启由独立的一次性计划任务执行，避免结束发起者后无法再启动。macOS / Windows 日志保存至 logs/service.log，单份最多 1 MiB 并保留一个轮转文件。实际平台验证范围见 [平台能力](platforms.md)。

Linux 同版本恢复自 2.19.0 起已接入独立控制者、serve bootstrap 和管理事务入口。提交或 helper accepted 只表示请求已保存/提交，最终结果须以 journal 终态及业务投影为准。waiting 等待完整自然空闲，只有 drained 后才暂停新执行；现有 owner 不收尾时有限超时保留原服务。未决代次阻止原始服务控制旁路；安装 unit 与调用者的配置、数据、解释器不符时拒绝操作。验证范围、恢复命令与限制见[同版本恢复入口](linux-recovery-operations.md)，不把离线通过当成真实机器故障注入通过。

交互 `MuLi init` 在配置成功后依次请求 install、enable、start，报告已完成步骤；启动请求被接受不等于服务已健康。`--no-service` 或 `--defaults` 跳过这一步，开发环境建议使用 `MuLi init --no-source --no-service`。启动失败时配置仍保留，可查看 `MuLi service status` 或使用 `MuLi serve` 诊断。

管理服务默认监听回环地址。绑定非回环地址必须配置访问密码；登录使用 HttpOnly、SameSite=Strict cookie，API 拒绝跨源变更与不合法 Host。密码和监听地址可在初始化或设置中配置；外部访问仍需自行提供合适的 HTTPS 入口。

配置修改不能与执行中的任务交错；先停止任务再改执行参数。端口修改在下一次启动生效。桌面授权依赖用户桌面会话，不能用 root 服务替代。

## 主动提醒

通知页可以创建一次或重复提醒、查看下一次触发、停用和删除；对话工具也可创建提醒。存储使用 Unix 秒，界面按浏览器本地时区显示。重复间隔至少 60 秒。调度器等待最近到期时间或变更通知，空闲不请求模型；停机期间错过的多次重复提醒在恢复时合并为一次，再计算下一次。

提醒进入工作台通知列表与 SSE。Linux 可在设置中开启“桌面通知”，复用系统已安装的 `notify-send`，关闭网页后也能接收新提醒；默认关闭。桌面通知失败不影响持久记录，不补发启动前的历史弹窗，也不唤醒关机的电脑。详见 notifications.md。

`attention.enabled` 默认开启：新到期提醒、维护终态与发现新版在活动主任务完整结束后形成独立的只读回合，不中断正在进行的工具批次或渠道回复；有主模型配置时触发一次正常回复，可能产生模型费用。系统资料不是用户授权；回复保留在主会话，并按宿主冻结的原请求来源投递。2.16 起启动时自主发现新版可使用明确设置的默认通知渠道，其它未知来源及旧记录留在 WebUI，不广播到历史渠道。首次启用和重新启用跳过先前历史；没有模型配置时只持久保留一批，配置恢复后继续处理。事件消费、去重、队列背压与关闭行为见 [系统注意事项](attention.md)，外发设置与撤销见 [默认通知](channels.md#无发令会话时的默认通知)。

## 资源与平台

默认最多两个模型执行并行；主会话未完成任务和工作流运行分别最多八个，排队时间计入超时。上限可从设置的执行预算修改；提高上限前应核对供应商配额。systemd 的默认资源保护为 MemoryMax=512M、CPUQuota=50%、TasksMax=64，可通过用户 unit override 调整。

服务与数据库操作支持 Python 3.12+。文件、配置统一使用 UTF-8；POSIX 私人权限位不等同于 Windows ACL 隔离。平台服务、录屏和语音新增适配经过模拟测试，未在本次开发机实际安装服务、采集桌面或录制麦克风；不把能力探测等同于实际桌面授权成功。

语音默认关闭，需要录音工具、真正的声学唤醒桥接和已配置的 ASR。启用声纹验证后还需显式录制参考音频。唤醒后播放“请说”或短提示音；播放不可用会记录状态，不假称已经播放。配置、桥接格式、文件保留和身份边界见 [语音](voice.md)。

网页语音输入另由 `web_voice.enabled` 控制，默认关闭。它使用浏览器权限与现有 ASR，只生成待发送草稿，与后台唤醒独立；远程页面需 HTTPS。音频保存在附件库，取消转写或丢弃文字不会自动删除原音频。详见 [网页点击录音](voice.md#网页点击录音)。
