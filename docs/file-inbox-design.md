# 资料收件目录：实现与验收合同

2026-10-24。用户持续自主迭代授权下提出的下一场景：主人明确指定一个目录，资料到达后沐璃及时提醒，主人随后可让她查看与整理。已有文件管理、系统注意事项、默认路由和出站账本继续复用；不监控整个 Home，不让模型定时检查目录。

## 首片范围与实现选择

后续项目关联候选（WIP）：可空 `project_id` 与独立 `file_inbox_project/current` binding 同 SQLite 事务保存；canonical proof 可带严格的 `{project_id,binding_revision}`，任务 revision/epoch 是消费时另取的有界快照，不进入不可变来源证明。项目批固定标准 Web bound，generic 的签名和字段保持原状。Attention 在最终锁内拒绝任务快照变化，至多立即重建一次，再等真实 bus revision；终止/解绑跳过而不回落 generic。任务引用仅核有限结构，不在宿主加载其正文或逐个解引用。项目 marker 与 prompt 共用原 Attention 字符预算，不能容下单条则保留 cursor 并明确预算失败。

该增量尚不能启用或发行：objects/ingress 新合同的真实格式准入与现有数据安全迁移是前置，不能只给新实例启用或写假格式 2；标准 Web 路由值不足以保证旧 Runtime 忽略项目 marker 后不会混入手机输入。现有收件流程保持格式 1，完整的项目执行段、迁移与回退验收均待统一验证。详见 [项目复核方案](proactive-project-inbox-proposal.md) 与 [使用边界](file-inbox.md)。

本方案由项目作出的选择：先支持 Linux 的一个现存目录，只看直属普通文件，默认关闭，不递归。目录必须在现有 workspace_roots 内；开启不扩大已有文件权限。自然语言修改使用主会话专属的 file_inbox_configure，认证管理入口为 GET/PUT /api/file-inbox/settings；沿现有配置锁、维护准入及生命周期只重配本来源，不增加前端常驻面板。macOS、Windows 明确不可用，待各自事件后端验收。

比较过三个方向：服务内 inotify 复用现有生命周期、事件循环和注意事项，适合首片；systemd.path 会额外增加单元安装、认证入站和重复激活，后续再评估；扩展 watcher 是目录指纹轮询，职责不同，不复用为资料事件后端。实现采用零新增依赖的 stdlib ctypes 适配，临时目录原生回归与整体应用验收分别提供证据，不以原型接口代替验收。

后审发现初稿将 file_inbox 放进主 Settings 会破坏同格式兼容：ConfigFile 完整序列化后，旧版本的未知字段拒绝会使其无法读取。实现改用现有 Store 中独立有版本的策略对象，不改变主配置、主配置快照或七领域合同。坏记录、未来 schema 和外层事务明确拒绝；完整替换先读取现值，省略字段恢复默认。此选择替代初稿的 self_configure 主配置字段，不建立虚拟字段或跨存储事务。

## 资料、权限与路由

新增固定 file-inbox 宿主生产者及 canonical 收件事实，沿现有 attention_source 适配器进入 AttentionInbox，不伪装成 system.log。收件对象、canonical 事件和首次冻结的 independent 路由同事务落盘，提交后才唤醒 EventBus；消费时重新验证事件 ID、来源对象、当前 scope、策略 revision 及启用状态，不能只信任同名总线事件。

继续使用 system:attention 与既有只读回合，不执行 owner hooks、不提炼主人身份或偏好。文件名与正文均不是主人指令；文件事件不授予移动、外发、团队调度或配置权限。通知只称“出现新的候选资料”，提供有界记录定位、类型/大小/观察时间；若展示文件名，须限制长度并标成外部数据，不默认注入正文或完整绝对路径。宿主不自动读取、解析或注入文件正文；沐璃仍可依现有 workspace、只读工具权限和来源策略按需读取，所得正文继续作为外部资料。这里不新增全面禁止 fs_read/fs_search 的限制。

来源证明独立承载类型、策略 revision 与 watcher 代次，不扩展冒用 chiliarch 的二元组。生产者提交事实和最终 Attention 接纳均在既有配置锁下重做来源复核，后者使用与提交相邻的 admission_guard；不能只在事件第一次进入队列时检查。

watcher 实例代次用于拒绝旧回调提交新事实；已提交 canonical 记录在重启后按持久来源身份、策略 revision 和保留状态验证，不因正常重启产生新 watcher 代次而失效。停用、换目录或重新授权仍按策略 revision 撤销，不能把运行时代次当作已提交资料的持久有效期。

主动提醒沿既有默认/明确记忆目的地与 outbox，首次持久化时冻结，发送复核保留撤销与 unknown 不自动重发。用户随后真实输入“查看并整理刚到的资料”时，按现有文件工具及 ask/auto/yolo 执行。首片不另做 PDF/OCR，也不把同盘移动说成跨盘归档。

## 事件与生命周期

- 从成功开启时刻开始接收，不扫描现有目录。CREATE/MOVED_TO 建候选；CLOSE_WRITE 只推进本代已见 CREATE 的候选，旧文件的一次修改不冒充新到达。
- CLOSE_WRITE 只是某次写打开被关闭，MOVED_TO 只是移入，两次大小稳定也不等于下载完成。候选可做有限合并与 stat，通知不得称“下载完成”。没有文件事件时没有目录扫描计时器或模型请求。
- 生产者自己的队列、合并窗口、候选数、持久保留和提醒频率分别有界，具体数值见下文；不能仅依赖 Attention 每批八条，否则文件风暴仍会导致多轮模型。溢出转一次固定 coverage_incomplete 并暂停，不做全目录自动补扫。
- 收到目录或祖先失效事件、复核发现身份改变或配置撤销后，不接受旧代新资料，不自动绑定原路径的新目录。首次明确配置时保存可验证的目录身份；重启逐段重新验证并匹配持久身份，否则记录 identity_changed、暂停至明确重新配置，不能由路径静默创建新身份。复用逐段 NOFOLLOW 打开原语；inotify_add_watch 无 dir_fd，以 /proc/self/fd 绑定持有的已验证目录，并监听祖先自身和精确下一段 entry 的失效，不依赖单次 Path.resolve()。目录 FD/watch 随路径深度增加；不递归提交祖先兄弟项。设备/ inode 在不同机器或文件系统重建后可能失效，不能将其宣称为跨系统永久身份。
- watcher 实例代次、策略 revision、宿主 observation ID 和有限文件版本共同约束接纳；不只按路径去重。配置换代的迟到 callback 无效。inotify 不提供事件发起人，不能由文件名或内容推断 owner，也不能可靠区分本进程与外部写入。
- 正常关闭释放 FD/回调并结算已接纳资料；重启只恢复已提交记录与 Attention 去重。停机事件没有持久保证，不宣称补收。停用、换目录、重新启用推进策略 revision，单纯重启不制造新授权。

候选状态及下文预算共同构成实现合同：

| 观察 | 状态与处理 |
| --- | --- |
| CREATE | 建立本代候选，单独出现不触发模型；未关闭的候选到期淘汰并保留覆盖缺口状态。 |
| 本代 CREATE 后的 CLOSE_WRITE | 进入有限合并窗口，复核普通文件与身份后可提交“候选资料”；不称下载完成。 |
| MOVED_TO | 进入有限合并窗口，复核后可提交；不假定移动前已写完。 |
| 旧文件的 CLOSE_WRITE | 不建立新到达候选。 |
| 删除、移出、身份失效、换代 | 淘汰候选，迟到事件不得恢复；目录失效则停止本代。 |
| 内核/应用溢出或接纳限额耗尽 | 进入 coverage_incomplete，暂停或降级；同一持久缺口代次至多一次通知，不扫描补收。 |

候选数、持久记录数与每单位时间进入 Attention 的总量分别有界，合并单批不能替代总量限制。缺口通知的额度跨重启保留，通知失败、恢复或重启不能制造新缺口代次。风暴回归同时核对候选事实、额外缺口事实和实际 Runtime 模型请求总量。

## 实现选择与剩余边界

1. stdlib ctypes 调用 libc，显式参数/返回类型，nonblocking/CLOEXEC，单 inotify 实例；失败先关闭后通知，正常关闭不通知。不新增库。
2. 临时目录验证准确 FD 绑定、祖先移走再移回、已排队失效、迟到回调、符号链接拒绝及 FD 清理；不改恢复专用的 RW/持久文件系统白名单。
3. 收件事实、元数据按需定位与保留策略复用现有 Store/工具预算；列表与剪裁共同按同事务生成的 canonical event ID 降序，不用墙钟或普通对象隐式 rowid 作为持久业务次序。排序先限制对象类型/字节，再核对 JSON 与编号整数范围，损坏键置末；剪裁按 canonical event ID 主键删除，撤销按独立接纳标记精确定位，不加载历史提醒正文。不另建文件索引或正文库。
4. Linux 不可用、内核/应用溢出、目录失效和自身回调异常保留首次固定原因并产生一次缺口，不形成提醒失败再次提醒的反馈环。挂载覆盖、远端网络文件系统、真实桌面和外部渠道仍未实测。

这些实现选择不转为主人必须回答的问题。独立 builder check、实现后审与真实临时目录验证分别进行；发行和外部集成需另行证明。

## 生产实现合同（2026-10-24）

以下数值由项目在技术预检后选定：独立 FileInboxSettings 默认 disabled、path 为空；合并窗口默认 0.1 秒（0—5 秒），等待 CREATE 关闭的候选期限 30 秒（0.1—300 秒），候选默认 64 个（上限 256），保留元数据默认 128 条（上限 1024）。没有候选时不运行合并/到期计时器，不扫描目录。

提醒窗口默认 3600 秒（1—86400 秒），每窗口最多 8 条资料事实（可配置至 32）；另为同一策略保留至多一条缺口事实，该事实也可能引发一次模型提醒，必须计入风暴验收的总上限。窗口计数与缺口标识持久保存，重启不重置；达到限额或候选到期后保留 coverage_incomplete 并停止观察，明确停用再启用才重新授权。本片不后台自动恢复或自动补收。

原生 adapter 未交付批次累计读取达到 16 KiB 或处理达到 256 个原生事件，即保守记录覆盖缺口并停止本代，该批资料不交付。计入名称过滤前的祖先兄弟 entry，祖先高度活跃也可能暂停；这不是文件条数额度。start 必须在运行中的 asyncio loop 上调用；on_failure 在先关闭本代后至多调用一次，正常关闭不通知。只获取普通直属文件的名称、device/inode、大小及 mtime/ctime，不打开正文。

应用复用现有配置锁与 lifespan，独立 file_inbox_settings 对象保存 schema_version=1 的完整期望策略；file_inbox_policy 单例保存当前授权身份、revision、窗口及缺口，file_inbox_record 保存有限元数据、观察标识和首次冻结路由。canonical file_inbox.observed 事件、元数据及计数在同一 SQLite 事务提交，EventBus 只在提交后唤醒，伪造同名或重放不同 event ID 不构成资料事实。已提交记录的验证不依赖当代 watcher generation；最终接纳仍按配置锁复核当前期望策略、访问根、持久身份与策略 revision。

MuLi 的只读 file_inbox_list/file_inbox_get 及同源认证入口提供有限目录、按 ID 定位与当前路径；路径明确 current_file_verified=false，不保证当前仍是观察到的版本。主会话通知不默认包含完整绝对路径或正文。目录换代后的重新授权通过独立策略停用再启用完成。状态区分 desired/effective/settings_error，不把保存成功当作原生观察可用。合同由纯状态机、真实临时目录事件、认证 API 和独立 Runtime 请求验收共同核对；已发布状态以版本审查为准。

## 验收

只使用临时目录、临时 SQLite 和假模型，不读取或监听真实用户目录。默认零 watcher；真实临时目录创建/移入；旧文件编辑；持续写入；重复事件；目录替换和符号链接；重启后身份变化；配置撤销及最终接纳竞态；迟到回调；内核/应用溢出；文件风暴模型请求上限；伪造总线事件；提醒提交前宿主不读取正文、既有权限内按需读取可用、访问根外拒绝、外部资料无法获得写工具；重启已有记录不重复；关闭 FD 无泄漏。已有来源路由、忙时延后与出站未知结果合同须保留。

独立 builder check 已完成：目录身份跨重启、最终接纳复核、候选状态和模型总量限制纳入方案。审查中将“不默认注入”误解为“禁止按需读取”的建议经原始要求与现有权限实现复核后撤回。原生适配的 13 项回归及 Ruff、格式和 strict mypy 已通过资源守卫；内核溢出和截断读取通过边界注入验证，没有制造真实内核溢出。应用整合、后审修复和发行另行记录，不能仅据下述早期技术预检宣称已交付。

## Linux 技术预检（2026-10-24）

独立 6.1 sol 在本机统一 30 秒资源守卫内运行一个仅使用 TemporaryDirectory 的最小探针。退出码 0，运行 144 毫秒、CPU 107 毫秒、峰值 21.5 MiB、swap 0；没有安装依赖、启动系统服务或监听真实用户目录。根代理另静态阅读探针中的实际断言和清理路径。

1. stdlib ctypes 调用 libc 的 inotify_init1，显式声明参数/返回类型，设置 NONBLOCK/CLOEXEC。逐段 NOFOLLOW 打开并持有目录 FD，再以 `/proc/self/fd/<fd>` 配合 ONLYDIR 绑定 watch：即使原路径已经替换，仍只收到原 inode 的创建，不会静默监控替换目录。这个 proc magic-link 需要跟随，追加 DONT_FOLLOW 反而得到 ENOTDIR；不把重新 readlink 得到的字符串作为准确绑定。
2. 祖先目录移出、写入、再移回后，叶目录身份仍相同，且叶 watch 没有 MOVE_SELF；祖先自身 watch 有 MOVE_SELF，父目录有对应 entry 的 MOVED_FROM/TO。证明仅做最终 stat 或仅监听叶目录不足，尚未证明完整祖先链初始化在任意竞态下都正确。
3. rm_watch 后，之前排队的 CREATE/CLOSE_WRITE 仍能读出，随后才是 IGNORED。因此换代采用新的 inotify 实例并保留 generation gate，不能把删除 watch 当作清空已排队事件。
4. asyncio.add_reader 实际唤醒一次；remove_reader 后关闭 FD，没有迟到回调，FD 数量回到基线。探针对每次读取、总读取次数及等待期限均有限制。

生产适配优先考虑已证实的零新增依赖 ctypes 小模块；若改用轻量库，必须先复核其读取行为，不能将“库很小”当作回调读取有界的证明。沿路径链订阅有限祖先自身失效及下一段 entry 变化，准确过滤名称；在同一已读批次先处理失效/OVERFLOW，再考虑资料接纳，异步提交前仍复核代次与策略。安装完 watch 后重开比较身份并排空初始化失效事件，才确定开始观察的时刻。

本次未做挂载、卸载、内核溢出或持久化实验。inotify 不通知挂载覆盖现有目录，网络文件系统远端变化也可能无事件；现有路径复核不等于对并发 namespace 切换的原子证明。本功能不能承诺这些静默缺口可被完整检测或自动补收，不能据此次探针声称生产 watcher 或完整路径竞态已验收。

所有后续验证继续经 scripts/check_local.py，串行使用共享 2 GiB、单次 1 GiB、swap 0、单 CPU 与既有硬期限；没有证明目录绑定与事件缺口的结果，不宣称功能已交付。

依据：[inotify 事件与溢出](https://man7.org/linux/man-pages/man7/inotify.7.html)、[Python FD 就绪回调](https://docs.python.org/3/library/asyncio-eventloop.html#watching-file-descriptors)、[systemd.path 行为](https://www.freedesktop.org/software/systemd/man/latest/systemd.path.html)、[轻量库候选官方文档](https://inotify-simple.readthedocs.io/en/latest/)。调研通过 Context7 核对 Python 事件循环；该库未在 Context7 检索到，尚未安装或证明兼容。接口资料不是实际验收。
