# 沐璃网页对话

界面使用原生 HTML、CSS 和 ES modules，无构建步骤、前端运行时依赖或 CDN。后端同源提供 `web/` 和 `/api/`；访问服务根路径即可使用。

## 界面与交互

默认界面只有一段持续的 MuLi 主对话，打开或刷新时通过 `/api/conversation` 恢复。主页面没有侧栏、新建或删除主会话按钮；左下两个小按钮分别切换深浅主题和进入设置。主题首次跟随系统，用户切换后仅在本机浏览器保存偏好，不保存消息或密钥。

设置以自然语言要求为入口，完整管理页面仍可直接使用：

- 设置：主、审阅、执行、视觉、压缩模型，协议和原生推理参数，称呼与提示词，权限与工具，Chiliarch / MCP，媒体与检索服务，通知、限制、访问和 Linux 用户服务。
- 工作流：模板、步骤、模型角色、依赖、真实运行结果和取消。
- 团队：真实协作成员与任务，发送协作消息、停止任务和查看子任务记录。
- 记忆：检索、内容与实体关系编辑、关系证据、来源、置信度及修改历史。
- 通知：收件箱、已读状态，以及一次或重复提醒的创建、改期、暂停和删除。
- 附件：本地文件库、图片和音视频预览、下载，以及将已有附件带入对话。
- 运行记录：搜索主对话和子任务记录，子任务只读查看；主对话保持唯一。

头像与空态插画分别为 `muli-avatar.svg` 和 `muli-empty.svg`，是本次原创的青蓝古风 Q 版 SVG；并非生图模型输出。旧宣传图片不再用于新界面。浅色为青瓷蓝，深色为夜青；共享文字、表面与状态色 token，正文和可用按钮在两种主题中均以 4.5:1 为最低对比度。装饰线条和禁用控件另按用途处理。

输入支持中文组合输入，`Enter` 发送、`Shift + Enter` 换行、`Ctrl/⌘ + K` 回到主对话。运行中仍可发送补充消息，停止按钮独立显示；收到的是排队确认时显示实际待处理数量。切换管理页面保留对话、输入草稿和 SSE，刷新页面恢复已持久化的消息、未完成回复、审批和队列；未发送的文字草稿不会跨刷新保存。

## 附件边界

选择文件后先上传原始字节到本地附件库，发送消息只携带 `attachment_ids`。前端不会读取文本全文、将文件转成 Base64 注入模型、自动转录或自动播放媒体。移除输入框内的附件只移除引用，不删除文件。图片是否发送到视觉模型由服务端及模型配置决定。

图片使用安全的栅格 MIME 白名单；音频和视频使用原生 `controls`、`preload="metadata"`，不自动播放。SVG、HTML、PDF 等文档保留下载入口，不作为同源活动文档嵌入。所有内容与下载 URL 都由附件 ID 构造，同接口返回的外部地址不会直接作为预览源。后续录音、语音识别和朗读控件可在真实服务接口齐备后接入。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `web/index.html` | 主对话、设置管理导航、附件输入、登录门和原生对话框 |
| `web/styles.css` | 两种主题、响应式布局、可读文字和减少动画偏好 |
| `web/app.mjs` | DOM 交互、真实接口接线、主会话恢复与管理页面 |
| `web/api.mjs` | JSON / 原始文件请求、结构化错误和增量 SSE 解析 |
| `web/state.mjs` | 消息、队列、工具、审批和运行状态的回放归并 |
| `web/settings.mjs` | 设置草稿、字段差异、原子对象和凭据保护 |
| `web/theme.mjs` | 系统主题、本地偏好和主题切换 |
| `web/attachments.mjs` | 安全媒体分类、同源附件 URL 与容量显示 |
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
| `GET /team` | `{members,messages}`，成员含 ID、名称、角色、状态、任务、父成员和会话 ID |
| `POST /team/:id/messages`、`POST /team/:id/stop` | `{content}` 协作消息 / 停止成员任务 |
| `GET/POST /memories`、`GET/PATCH/DELETE /memories/:id` | 记忆内容、关系、来源和置信度 |
| `GET /memories/graph`、`GET /memories/:id/revisions` | 关系证据与历史快照 |
| `GET/POST /workflows`、`DELETE /workflows/:id` | 工作流定义 |
| `POST /workflows/:id/run`、`POST /workflow-runs/:id/cancel` | 工作流运行和取消 |
| `GET /notifications`、`PATCH /notifications/:id` | 收件箱和 `{read:true}` |
| `GET/POST /reminders`、`PATCH/DELETE /reminders/:id` | Unix 秒提醒时间、内容、重复间隔和启用状态 |
| `GET/PATCH /settings` | 脱敏设置、仅保存已编辑字段 |
| `POST /integrations/chiliarch/pair` | `{code,name}` 配对 |
| `GET/POST /service` | 用户服务状态与安装、启动、重启、停止、启停自动启动 |
| `GET /export` | 全量 NDJSON 下载 |
| `GET /docs`、`GET /openapi.json` | 本地 API 参考与实际接口结构 |

密码门通过 HttpOnly / SameSite Cookie 访问服务。任何接口或 SSE 返回 401 时立即中止事件重连，显示密码输入；登录成功后重新恢复主对话。API 参考也仅加载同源文件，不依赖 Swagger CDN。

SSE 事件为 `{id,type,session_id,data,created_at}`。支持消息增量、思考、消息完成、工具开始 / 完成、审批请求 / 解决、运行开始 / 完成 / 失败 / 取消。持久收件使用 `message.queued {message}`，用户消息的 `delivery_status` 为 `pending` / `delivered`；快照中的 pending 行恢复待处理数量。前端同时兼容 `conversation.queued {message_id,run_id,source}` 和 `conversation.injected {message_ids,run_id}`。用户消息完成事件不会清除助手正在流出的片段；晚到的 pending 确认也不会把已送入的消息重新排队。全局事件更新通知、提醒、工作流、团队和附件列表。

会话详情的 `messages` 是已完成消息的权威来源；回放仅重建工具、审批、运行状态、最新思考、未完成回复和待处理队列。事件 ID 去重防止重连重复 Token。消息中的 `attachments` 使用完整元数据；只有 `attachment_ids` 时从本地库缓存查找。

设置仅提交实际改动；空 API Key / 访问密码保留旧值，`clear_api_key:true` / `clear_password:true` 才清除。脱敏标记不会回传；`reasoning`、`thinking`、`extra_body`、`extra_headers` 为完整对象替换，互不生成隐式参数。执行与压缩模型未配置时复用主模型；审阅模型未配置时交由用户批准；主模型不支持图片时可配置独立视觉模型。

MCP JSON 仅在实际修改后提交完整 `mcp` 对象。服务端原子替换服务器集合，并保留原位未改的 `••••` 凭据。旧服务只返回 `configured` 标记时隐藏编辑器；标记不回传。用户服务重启 / 停止使用短确认并处理断线，收到提交确认后再读取实际状态。提醒由后端调度，网页不调用模型轮询时间。

## 验证

```sh
node --test tests/frontend/*.test.mjs
node --check web/app.mjs
```

当前 33 项无依赖测试覆盖：SSE Unicode 与任意分片、回放去重、运行中恢复、补充消息不截断回复、队列快照与实时注入、晚到确认、仅附件消息确认、审批 / 停止、失败保留、时间转换、链接过滤、原始文件字节上传、安全媒体类型与 URL、脱敏设置、密码更新、MCP 原子替换、双主题文字对比度、主题偏好和本地 OpenAPI 引用。测试不会连接真实模型。

本轮通过 IAB 在独立临时数据目录完成 1280 × 720 桌面及 390 × 844 手机验收：浅深主题与刷新保存、设置保存和恢复、管理页面导航、工作流创建与删除、原始文本 / 图片 / WAV / MP4 上传、图片解码和媒体控件、原始内容下载、仅附件消息、错误密码与登录 / 退出恢复。手机主对话和设置没有横向溢出。

使用本地协议测试模型验证了流式回复、Ask 批准 / 拒绝、工具结果、审批中补充两条消息、刷新后待处理数量恢复、送入后清零与停止。拒绝结果不含测试文件内容。验收期间修复了 pending 收件展示、事件合同兼容及晚到确认的计数回退问题。测试没有调用外部模型、安装服务或采集真实桌面 / 麦克风。

实际截图：[桌面浅色](screenshots/workbench-desktop.jpg)、[手机深色](screenshots/workbench-mobile.jpg)。截图中的 `local-ui-test` 是验收模型，不代表外部模型、Chiliarch、桌面操作、通知显示或系统服务安装已验收。当前 IAB 文件选择桥接未能触发含中文本地路径的 change 事件；改用 ASCII 测试文件路径后上传成功。中文上传名称另由真实 API 回归验证通过。
