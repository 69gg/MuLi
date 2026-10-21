# Linux 本地保留安装的托管切换

状态：2.22.0 已完成本地整合、独立审查、跨平台 CI 和正式发布；尚未在用户真实服务上执行。这里说明公开入口的合同，验证与发行记录集中在[2.22 审查](review-v2.22.md)。

托管模式保留一个固定 controller 和独立启动器，在原管理 journal 中记录当前安装、候选安装和恢复步骤。程序选择可以回退，数据库继续使用同一份最新数据，不用旧数据库备份覆盖切换后新增的收件、审批或投递记录。

首次迁入使用 `self_adopt`，候选必须就是当前运行的完整保留安装。以后用 `self_switch` 选择另一个已存在的本地安装。两个工具都接受以下三个绝对路径：

```json
{
  "prefix": "/path/to/retained-environment",
  "python": "/path/to/retained-environment/bin/python",
  "package_root": "/path/to/retained-environment/lib/python3.13/site-packages/muli"
}
```

路径示例不是默认目录，必须按实际安装提供；不能把可执行文件的真实链接目标代替环境内的词法 Python 入口。接口不创建环境、不下载依赖、不执行候选来猜测兼容性。包必须提供真实兼容声明，并通过当前七领域格式、解释器、包文件、标准 Linux 用户服务和实际进程观察验证。历史无认证格式的库不会自动补证或迁移。

`self_switch` 与 `self_update` 一样需要真实用户批准；approve 也不替代候选兼容验证。普通原位 `self_update` 的原有格式限制继续有效，不能通过它覆盖托管环境。迁入后的普通 `service install` 不能覆盖稳定服务入口。

管理 API 复用 `POST /api/management/transactions`：

| action | 接受内容 | 行为 |
| --- | --- | --- |
| `adopt` | `candidate` 三路径 | 当前安装迁入托管，保留原配置 |
| `switch` | `candidate` 三路径 | 切到另一兼容本地安装，保留配置原始字节 |
| `configure` | `changes` | 当前安装使用候选配置执行恢复事务 |
| `restart` | 无候选、无配置变更 | 当前安装按恢复事务重启 |

非 `configure` 操作不接受配置变更；其它操作也不能夹带候选路径。未知字段、相对路径、缺少字段会在排队前拒绝。HTTP 入口沿用已有管理认证与同源限制，模型工具沿用权限门。

排队成功只代表输入与启动所有权初检通过，状态为 `pending`。待工作空闲后，准备器统一核实际安装、当前文件、数据库完整结构与格式，再持久化事务。准备状态已写入后，即使 helper 的观察超时，也保留原代并通过 `management recover` 接续，不另起事务或续期。

恢复结果先原子写入实际数据库，再解除配置等待。普通热配置可以继续使用；下一代捕获实际当前配置，旧健康凭据不能报告变更后的文件仍符合旧快照。失败迁入回到原入口后仍能继续配置和重启，原数据库句柄与 writer lease 保持到所有写入者关闭。

监听地址是一个例外：改变 `server.host` 或 `server.port` 必须使用 `configure` 恢复重启事务。`self_configure` 会自动选择 `restart_transaction`；HTTP 使用上表的事务入口。直接 `PATCH /api/settings` 会在关闭组件或写入配置前返回 `409 listener_restart_required`。密码、Cookie 和允许的 Host 等其它配置仍沿用热配置；保存新地址不会让当前监听 socket 自动迁移。

管理入口保留本次启动的实际监听地址，排队和准备写入前都核对它与当前配置是否一致。如果其它入口已经改写了监听地址，返回 `transaction_listener_changed`，不生成新计划或事务。控制面复用现有回环 HTTP 合同：`localhost`、回环 IP 及通配监听地址受支持，`0.0.0.0` 和 `::` 分别使用 IPv4、IPv6 回环访问；具体 LAN IP 会以 `transaction_unsupported`、原因 `transaction_http_not_loopback` 提前拒绝。当前与候选配置都需满足该合同。成功配置事务分别冻结旧地址供原进程排空、新地址供候选进程健康检查，不以新配置推断旧 socket 的位置。

查询入口仍为 `self_status`、`GET /api/management/transactions/current` 和 `MuLi management status`。恢复当前代可调用无参数的 `self_recover`，或以空 JSON 对象 `{}` 请求 `POST /api/management/transactions/recover`。两者统一复用当前宿主的恢复流程：先重新确认可能丢失返回的宿主绑定并投影已有结果，再按现有规则确认 helper。它们不另建事务、不续期，也不重试结果未知的业务效果；返回 helper 已接受或控制者正忙不代表切换已经完成。

HTTP 恢复入口在被动或暂停状态仍可达，但继续要求原有 owner 登录、同源、Host 和 JSON 边界；不接受路径、代号或其它参数，且不会放开其它业务写入口。`MuLi management recover` 交由固定 controller；单独启动 helper 不代表运行宿主内存中尚待确认的丢返回绑定已经确认。如果当前宿主仍在运行，应先使用上述宿主恢复入口。当前 selector 或候选程序坏掉时，CLI 仍不依靠候选程序执行恢复。未决状态拒绝普通管理命令，保留诊断和恢复入口。

HTTP 恢复请求持有现有配置锁直到恢复返回；应用关闭会等待已进入的恢复完成，之后才关闭 Store 与 journal。关闭已经开始时，排队恢复取得锁后返回 `503 transaction_unavailable`，原因 `service_closing`，不会调用恢复流程。该锁不要求普通执行准入，因此暂停时的恢复能力不变。

结果沿原计划保存的发令来源回送；结果已保存、进入主会话注意队列和外部渠道实际送达是不同阶段。没有新建会话或重新生成主身份，也不会自动重放结果未知的外部副作用。

本片优先 Linux，要求本地受支持文件系统、标准同用户 systemd 服务与 cgroup 观察。离线候选准备及可选单 wheel 在线获取沿同一托管链接入，使用与验证边界分别见[候选管理](candidate-host-design.md)和[有界在线材料](bounded-online-candidate-design.md)；本节的切换回归不证明真实下载或安装成功。跨格式迁移、真实停机时长及 macOS/Windows 的同等切换能力仍不在当前验收范围。

监听入口的专用回归 `tests/test_listener_configuration.py` 共 15 项通过，包含真实临时 Store/文件准备的旧、新端口快照和最后一次进程观察后漂移拒绝；HTTP 入口使用进程内 ASGI，systemd/proc 与文件系统支持面使用已有 fixture 的假边界，不访问用户服务。守卫内测试峰值 124.1 MiB、swap 0，相关 Ruff/格式及四源码文件严格 mypy 通过；这不等于真实守护进程的端口迁移验收。

恢复公开入口的 `tests/test_management_recovery_entry.py` 共 19 项通过：真实 owner 登录、同源/Host/JSON 拒绝、暂停时仅精确路径放行、工具直接复用恢复及失败传播。恢复函数在本组中使用可观察替身，验证入口分派，不将其计为 helper 真实启动或丢返回绑定确认的完整验收。测试峰值 108.4 MiB、swap 0，四文件 Ruff/格式及三源码文件严格 mypy 通过。

另补两项关闭竞态回归：真实 `app.state.close()` 等待恢复 await 且 Store 保持可读，以及关闭后排队恢复拒绝；两项已纳入 9 文件 84 项组合回归通过，`tests/test_management_recovery_entry.py` 共 21 项。组合测试耗时 5.90 秒，守卫服务耗时 7.428 秒，峰值 290.6 MiB、swap 0；这仍是临时资料与进程内 HTTP 验收，不代表真实用户服务恢复。
