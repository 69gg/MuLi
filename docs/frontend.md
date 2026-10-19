# 沐璃网页对话

界面使用原生 HTML、CSS 和 ES modules，无构建步骤、前端运行时依赖或 CDN。后端同源提供 `web/` 和 `/api/`；访问服务根路径即可使用。

## 界面与交互

默认界面只有一段持续的 MuLi 主对话，打开或刷新时通过 `/api/conversation` 恢复。主页面没有侧栏、新建或删除主会话按钮；左下两个小按钮分别切换深浅主题和进入设置。主题首次跟随系统，用户切换后仅在本机浏览器保存偏好，不保存消息或密钥。

设置以自然语言要求为入口，完整管理页面仍可直接使用：

- 设置：主、审阅、执行、视觉、压缩、记忆提取模型，协议和原生推理参数，称呼与提示词，权限与工具，Chiliarch / MCP，消息渠道，语音与采集，媒体与检索，插件、更新、管理计划，通知、限制、访问和用户服务。
- 工作流：模板、步骤、模型角色、依赖、真实运行结果和取消。
- 团队：按最近创建分页查看成员，可筛选进行中任务，发送协作消息、停止任务和查看子任务记录；已绑定任务说明的成员可按需查看完整目标、要求与修订。支持任务控制的后端还可在说明弹窗内暂停任务范围、追加要求并交主沐璃重新规划。实时刷新保留焦点和已展开的消息。
- 记忆：检索、内容与实体关系编辑、关系证据、来源、置信度及修改历史。
- 通知：收件箱、已读状态，以及一次或重复提醒的创建、改期、暂停和删除。
- 附件：本地文件库、图片和音视频预览、下载，以及将已有附件带入对话。
- 运行记录：搜索主对话和子任务记录，子任务只读查看；主对话保持唯一。

头像与空态插画分别为 `muli-avatar.svg` 和 `muli-empty.svg`，是本次原创的青蓝古风 Q 版 SVG；并非生图模型输出。旧宣传图片不再用于新界面。浅色为青瓷蓝，深色为夜青；共享文字、表面与状态色 token，正文和可用按钮在两种主题中均以 4.5:1 为最低对比度。装饰线条和禁用控件另按用途处理。

输入支持中文组合输入，`Enter` 发送、`Shift + Enter` 换行、`Ctrl/⌘ + K` 回到主对话。运行中仍可发送补充消息，停止按钮独立显示；收到的是排队确认时显示实际待处理数量。切换管理页面保留对话、输入草稿和 SSE，刷新页面恢复已持久化的消息、未完成回复、审批和队列；未发送的文字草稿不会跨刷新保存。

## 附件边界

选择文件后先上传原始字节到本地附件库，发送消息只携带 `attachment_ids`。前端不会读取文本全文、将文件转成 Base64 注入模型、自动转录或自动播放媒体。移除输入框内的附件只移除引用，不删除文件。图片是否发送到视觉模型由服务端及模型配置决定。

图片使用安全的栅格 MIME 白名单；音频和视频使用原生 `controls`、`preload="metadata"`，不自动播放。SVG、HTML、PDF 等文档保留下载入口，不作为同源活动文档嵌入。所有内容与下载 URL 都由附件 ID 构造，同接口返回的外部地址不会直接作为预览源。

2.1 增加默认关闭的点击录音按钮；开启网页语音并配置 ASR 后，可生成可编辑文字草稿，仍须点击发送才进入对话。录音期间的用户编辑会被保留，迟到文字放到独立草稿区。取消、离页及退出登录会回收录音和转写请求；音频保留在附件库。详见 [网页录音](voice.md#网页点击录音)。

语音与桌面采集配置位于设置分组中。页面只读取组件和后端状态，不会在进入分组时录音、截屏、录屏或播放声音。设备可见、后端存在与真实权限验证分别展示；缺少声学唤醒组件、ASR 或声纹参考时显示服务端原因，不把文本匹配描述为声学识别。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `web/index.html` | 主对话、设置管理导航、附件输入、登录门和原生对话框 |
| `web/styles.css` | 两种主题、响应式布局、可读文字和减少动画偏好 |
| `web/app.mjs` | DOM 交互、真实接口接线、主会话恢复与管理页面 |
| `web/api.mjs` | JSON / 原始文件请求、结构化错误和增量 SSE 解析 |
| `web/state.mjs` | 消息、队列、工具、审批和运行状态的回放归并 |
| `web/settings.mjs` | 设置草稿、字段差异、原子对象和凭据保护 |
| `web/management.mjs` | 渠道与维护标签、只读模块状态加载及独立错误处理 |
| `web/theme.mjs` | 系统主题、本地偏好和主题切换 |
| `web/attachments.mjs` | 安全媒体分类、同源附件 URL 与容量显示 |
| `web/team.mjs` | 分页请求代次、团队焦点与展开状态恢复 |
| `web/task-control.mjs` | 任务控制修订归并、草稿与明确重试状态；不操作 DOM 或发起网络请求 |
| `web/voice-input.mjs` | 显式录音、轨道回收、上传转写取消与草稿冲突处理 |
| `web/markdown.mjs` | 纯 DOM 的轻量 Markdown，原始 HTML 按文本显示 |
| `web/api-docs.html`、`web/api-docs.mjs` | 本地 OpenAPI 参考、搜索和 schema 引用跳转 |

## API 合同

所有接口同源、位于 `/api`。普通变更请求声明 `Content-Type: application/json`；文件上传使用原始 MIME。网页不持久化访问密码或 API Key。服务端负责 Cookie、同源请求、文件访问、模型权限和工具审批。

| 接口 | 用途 |
| --- | --- |
| `GET /auth/status`、`POST /auth/login`、`POST /auth/logout` | 访问状态，密码登录和退出 |
| `GET /conversation` | 幂等获取唯一主会话，返回 `{session,messages,events}` |
| `GET /status` | 模型、工具、集成、通知、版本和真实运行状态 |
| `GET /sessions`、`GET/PATCH/DELETE /sessions/:id` | 子任务记录、详情和已有会话管理 |
| `POST /sessions/:id/messages` | `{content,mode,attachment_ids}`，返回 `{run_id,session_id,message,queued?}` |
| `POST /sessions/:id/stop` | 请求停止当前任务 |
| `GET /events`、`GET /sessions/:id/events?after=cursor` | 全局 / 主会话 SSE，按最后事件 ID 接续 |
| `POST /approvals/:id` | `{decision:"approve"|"reject"}` |
| `POST /uploads` | 原始文件字节；`X-File-Name=encodeURIComponent(name)`，原始 MIME 作为 Content-Type |
| `GET /uploads` | `{items}`；项目含 `id,name,mime_type,size,url,preview_url,kind` |
| `GET /uploads/:id/content`、`GET /uploads/:id/download` | 本地内容预览 / 文件下载 |
| `GET /team?limit=50&offset=0&active_only=false` | `{members,messages,offset,limit,total,active_only,has_more}`；成员含限长消息预览，完整历史通过会话详情读取 |
| `POST /team/:id/messages`、`POST /team/:id/stop` | `{content}` 协作消息 / 停止成员任务 |
| `GET /commitments`、`GET /commitments/:id` | 有界工作账本索引 / 已认证的完整任务详情；不存在的 ID 返回 404 |
| `POST /commitments/:id/control` | `{action:"pause"\|"request_replan",expected_epoch,request_id,additional_requirements?}`；返回 `{receipt,current}`，409 使用 `detail:{code,message,current?}` |
| `GET/POST /memories`、`GET/PATCH/DELETE /memories/:id` | 记忆内容、关系、来源和置信度 |
| `GET /memories/graph`、`GET /memories/:id/revisions` | 关系证据与历史快照 |
| `GET/POST /workflows`、`DELETE /workflows/:id` | 工作流定义 |
| `POST /workflows/:id/run`、`POST /workflow-runs/:id/cancel` | 工作流运行和取消 |
| `GET /notifications`、`PATCH /notifications/:id` | 收件箱和 `{read:true}` |
| `GET/POST /reminders`、`PATCH/DELETE /reminders/:id` | Unix 秒提醒时间、内容、重复间隔和启用状态 |
| `GET/PATCH /settings` | 脱敏设置、仅保存已编辑字段 |
| `GET /channels` | 真实渠道状态、出站队列统计与最近发送记录 |
| `POST /channels/deliveries/:id/retry` | 失败记录重试；结果未知须明确确认后传 `{allow_unknown:true}` |
| `GET /voice`、`GET /capture` | 语音组件、设备、播放后端及桌面采集能力状态 |
| `POST /media/transcribe` | `{attachment_id}`；返回 `{text,attachment_id,draft:true}`，只接受已有音频附件，不提交主会话 |
| `GET /management` | 管理计划、插件清单、插件重启标记与已有版本检查结果 |
| `POST /integrations/chiliarch/pair` | `{code,name}` 配对 |
| `GET/POST /service` | 用户服务状态与安装、启动、重启、停止、启停自动启动 |
| `GET /export` | 全量 NDJSON 下载 |
| `GET /docs`、`GET /openapi.json` | 本地 API 参考与实际接口结构 |

密码门通过 HttpOnly / SameSite Cookie 访问服务。任何接口或 SSE 返回 401 时立即中止事件重连，显示密码输入；登录成功后重新恢复主对话。API 参考也仅加载同源文件，不依赖 Swagger CDN。

任务说明正文按文本展示，证据引用默认折叠。打开时按需读取当前保存的修订，不截去中段要求；旧任务没有说明时明确显示尚未登记。没有 `control` / `scope` 的 2.2 详情保持只读，不提供不能兑现的暂停按钮。关闭或打开其他对话框后，迟到的详情响应不会替换新内容；认证失效会关闭对话框并清除详情，确保密码门可访问。

任务控制沿用此弹窗，不增加首页面板。暂停必须收到宿主回执，模型文字和本地点击不会直接变成“已暂停”；已经进入工具的操作允许收尾。暂停范围使用服务端 `scope.member_count`，包含同一任务说明的全部绑定成员及其子任务，不从当前团队分页推断。存在活动工作流时显示 `workflow_active` 原因，暂停原子 409，不自动停止工作流，也不显示部分成功。暂停后可追加完整要求或留空交主沐璃重新规划；没有直接恢复按钮，关闭和刷新都不会解除暂停。

控制显示以 `current.revision` 单调归并，覆盖同 epoch 从暂停到批准的变化。紧凑写响应不含计划正文时保留已有正文，再按需 GET；只有完整详情自己的修订可标在要求正文上。后台更新不重建正在输入的补充要求，409 保留草稿并提示核对后再次明确提交；补充要求只追加，不提供删改旧约束的入口。SSE 的 `commitment.updated` 只使当前弹窗失效，不携带或拼接正文，也不轮询模型。

请求结果未知时保留原操作体及 `request_id`，用户重试复用相同内容，不自动换 ID 发第二次。`503 / model_unconfigured` 与 `429 / queue_full` 属于明确的事务准入失败：显示服务原因、结束本次提交并保留既有暂停及补充草稿；断线和其他无法确认提交结果的 5xx 仍按未知结果处理。同标签页的 `sessionStorage` 仅在操作已经提交、回执尚未确认期间保存该操作体，支持关窗和刷新后确认；成功或明确失败后移除，401 和退出登录清空。未提交的补充草稿只留在打开的弹窗中，关闭后不保存。该机制不保存密码或 API Key。方案确认与队友通知投递分开显示，界面不把“计划已确认”描述成全体已执行。

SSE 事件为 `{id,type,session_id,data,created_at}`。支持消息增量、思考、消息完成、工具开始 / 完成、审批请求 / 解决、运行开始 / 完成 / 失败 / 取消。持久收件使用 `message.queued {message}`，用户消息的 `delivery_status` 为 `pending` / `delivered`；快照中的 pending 行恢复待处理数量。前端同时兼容 `conversation.queued {message_id,run_id,source}` 和 `conversation.injected {message_ids,run_id}`。用户消息完成事件不会清除助手正在流出的片段；晚到的 pending 确认也不会把已送入的消息重新排队。全局事件更新通知、提醒、工作流、团队和附件列表。

`attention.queued {message}` 将后台资料纳入待处理队列；`principal:"system:attention"` 的内容显示为可展开的“系统资料”，不会冒充用户请求。`ingress.cancelled` 清除取消的待处理行和计数，保留已送入的消息与流式回复。历史回放维持同样的来源标识。

会话详情的 `messages` 是已完成消息的权威来源；回放仅重建工具、审批、运行状态、最新思考、未完成回复和待处理队列。事件 ID 去重防止重连重复 Token。消息中的 `attachments` 使用完整元数据；只有 `attachment_ids` 时从本地库缓存查找。

设置仅提交实际改动；空 API Key、访问密码和渠道密钥保留旧值，`clear_api_key:true` / `clear_password:true` / `clear_token:true` / `clear_webhook_secret:true` / `clear_client_secret:true` 才明确清除。`api_key_set`、`password_set`、`token_set`、`webhook_secret_set`、`client_secret_set` 等只读状态不会回传；`reasoning`、`thinking`、`extra_body`、`extra_headers` 为完整对象替换，互不生成隐式参数。执行、压缩与记忆提取模型未配置时复用主模型；审阅模型未配置时交由用户批准；主模型不支持图片时可配置独立视觉模型。

设置概览提供自然语言入口与检查配置、消息渠道、语音组件和更新的短示例。没有未保存编辑时，再次进入设置会读取最新配置，供对话完成配置后核对；有编辑时保留草稿。状态刷新不会清除草稿。渠道、语音与采集、扩展与维护分组可直接编辑常用字段，其余完整参数以折叠 JSON 按字段覆盖；删除 JSON 中的键保留原值，明确的空数组、空对象或清除开关按接口合同处理。独立模块读取失败不会阻止其余分组使用，也不会把未知状态显示为关闭或更新成功。

渠道分组管理 OneBot、Telegram、QQ 官方机器人和微信的用户 / 群组白名单、接收附件及独立凭据，展示持久发送记录中的失败或结果未知。失败记录可重试，结果未知时须再次确认，取消不发起请求。扩展与维护分组展示插件清单、待重启标记、已有更新检查和管理计划；读取页面不会触发网络更新检查、安装插件或执行重启。记忆处理、上下文压缩和主动关注可在媒体与检索分组的折叠 JSON 中调整。并行 Agent 数只约束资源并发，界面不添加任意人数上限。手机横向设置导航在切换后保持当前分组可见。

MCP JSON 仅在实际修改后提交完整 `mcp` 对象。服务端原子替换服务器集合，并保留原位未改的 `••••` 凭据。旧服务只返回 `configured` 标记时隐藏编辑器；标记不回传。用户服务重启 / 停止使用短确认并处理断线，收到提交确认后再读取实际状态。提醒由后端调度，网页不调用模型轮询时间。

## 验证

```sh
node --test tests/frontend/*.test.mjs
node --check web/app.mjs
```

当前 88 项无依赖测试覆盖：SSE Unicode 与任意分片、回放去重、运行中恢复、补充消息不截断回复、队列快照与实时注入、晚到确认、仅附件消息确认、审批 / 停止、失败保留、时间转换、链接过滤、原始文件字节上传、安全媒体类型与 URL、脱敏设置、密码更新、MCP 原子替换、双主题文字对比度、主题偏好和本地 OpenAPI 引用。新管理回归覆盖只读状态请求、单模块及全部失败、更新未知状态、渠道密钥保留 / 清除、伪状态字段过滤、高级参数草稿合并、独立记忆提取角色、重试确认门、系统资料身份和取消收件。测试不会连接真实模型。

任务控制新增 13 条纯状态回归，覆盖旧版只读、同 epoch 修订乱序、紧凑投影保留正文、纯暂停与重规划区分、后台更新保留草稿、结果未知时原 ID 和请求体恢复、明确准入失败保留暂停及草稿、未辨认的 5xx 保留原请求、旧回执不回滚新计划、409 原子失败、401 清理后拒绝迟到回执和错误回执。此处仅确认界面状态逻辑，后端门禁与真实浏览器控制验收待整合后执行，不以这些测试代替暂停生效证明。

本轮通过 IAB 在独立临时数据目录完成 1280 × 720 桌面及 390 × 844 手机验收：浅深主题与刷新保存、设置保存和恢复、管理页面导航、工作流创建与删除、原始文本 / 图片 / WAV / MP4 上传、图片解码和媒体控件、原始内容下载、仅附件消息、错误密码与登录 / 退出恢复。手机主对话和设置没有横向溢出。

使用本地协议测试模型验证了流式回复、Ask 批准 / 拒绝、工具结果、审批中补充两条消息、刷新后待处理数量恢复、送入后清零与停止。拒绝结果不含测试文件内容。验收期间修复了 pending 收件展示、事件合同兼容及晚到确认的计数回退问题。测试没有调用外部模型、安装服务或采集真实桌面 / 麦克风。

实际截图：[桌面浅色](screenshots/workbench-desktop.jpg)、[手机深色](screenshots/workbench-mobile.jpg)。截图中的 `local-ui-test` 是验收模型，不代表外部模型、Chiliarch、桌面操作、通知显示或系统服务安装已验收。当前 IAB 文件选择桥接未能触发含中文本地路径的 change 事件；改用 ASCII 测试文件路径后上传成功。中文上传名称另由真实 API 回归验证通过。

新增管理分组再次使用独立临时数据目录及本地假模型，通过 IAB 验证桌面和手机的浅深主题、分组切换、渠道与高级 JSON 一起保存、跨分组草稿保留、密钥保存后留空、记忆提取模型参数持久化、插件待重启提示和已完成管理计划。结果未知的假投递取消重发后保持原状；失败的假记录进入队列，所有渠道始终关闭。语音、桌面采集、主动关注、自动记忆提取与启动更新检查均保持关闭；未执行服务安装或更新。真实历史中的系统资料可单独展开，手机和桌面没有横向溢出，浏览器未记录控制台错误。两张主界面截图已更新至本轮已连接状态。

2.1 整合验收补充：团队 105 位成员分成 50/50/5 三页，进行中筛选返回 3 位；翻页保持按钮焦点，到末页落在标题，390×844 视口无横向溢出。网页录音状态机和转写接口新增假录音器、取消、限额和草稿冲突测试；浏览器验证默认禁用与独立设置入口，未申请麦克风权限。

2.2 任务说明验收：临时实例验证完整正文按文本显示、旧任务未登记提示、迟到响应隔离、401 关闭并清空详情、实时刷新后关闭对话框恢复到同一队友按钮，以及 390×844 手机尺寸无横向溢出；未启用模型或任何采集设备。详情 API 回归覆盖认证、404、长正文不截断及只读数据库不变。
