# 沐璃网页工作台

工作台使用原生 HTML、CSS 与 ES modules，无 Node 运行时依赖、构建步骤或 CDN 资源。后端同源提供 `web/` 静态文件与 `/api/` 接口；从服务根路径访问 `index.html`，模块和图片使用相对路径加载。

## 界面

- **会话**：持久化会话列表、重命名、删除、流式消息、可折叠思考过程、工具轨迹、审批、停止任务与 Ask / Auto / Yolo 模式。
- **工作流**：从模板或空白创建多步骤任务，配置模型角色及依赖，查看真实运行节点、关联会话、结果，取消运行或删除定义。
- **记忆**：检索、新增、编辑、删除，管理实体、关系、来源与置信度；关系图可查看证据，详情可展开修改历史。
- **通知**：收件箱、未读计数、标为已读、关联会话；提醒支持一次或重复、暂停、启用、改期和删除。
- **设置**：主模型 / 审阅模型 / 执行模型，三种协议，权限目录、联网搜索、电脑操作、Chiliarch 配对、MCP 配置、运行与队列限制、桌面通知和 Linux 用户服务。
- **API 参考**：`/api/docs` 使用本地静态文件读取 `/api/openapi.json`，支持接口检索、参数、请求 / 响应结构和本地 `$ref` 跳转，不依赖 CDN。

起始页图片由仓库已有的 `img/standby.png` 压缩得到，没有添加宣传文字。配色统一为浅青瓷蓝与深青墨；移动端导航和运行轨迹使用抽屉，支持减少动画的系统偏好。

正文、辅助文字、占位提示与可用按钮采用至少 4.5:1 的对比度。文字色 token 有自动测试约束；图标、边框和禁用控件另按用途处理。移动端关闭的导航设为 `inert`，原生对话框保留焦点约束，模型与子视图可用方向键切换。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `web/index.html` | 页面结构、导航、输入框和可访问性入口 |
| `web/styles.css` | 视觉系统、响应式布局与克制的过渡动画 |
| `web/app.mjs` | DOM 交互与接口接线 |
| `web/api.mjs` | JSON 请求、结构化错误与增量 SSE 解析 |
| `web/state.mjs` | 会话事件归并、回放去重、模式与显示辅助函数 |
| `web/settings.mjs` | 设置草稿、按字段更新和密钥保护 |
| `web/markdown.mjs` | 安全的轻量 Markdown 展示 |
| `web/api-docs.html`、`web/api-docs.mjs` | 纯本地 API 参考与 OpenAPI 结构展示 |

## API 合同

所有接口均位于同源 `/api` 下。变更请求声明 `Content-Type: application/json`。网页不存储 API Key，不将密钥写入浏览器持久化存储；已保存的密钥由服务端脱敏返回。

| 接口 | 用途 |
| --- | --- |
| `GET /status` | 模型、工具、电脑操作、Chiliarch、MCP、通知、版本和运行状态 |
| `GET/POST /sessions` | 列出 / 创建会话 |
| `GET/PATCH/DELETE /sessions/:id` | 消息及历史事件、标题 / 模式、删除 |
| `POST /sessions/:id/messages` | `{content, mode}`，返回 `{run_id, session_id, message}` |
| `POST /sessions/:id/stop` | 请求停止当前任务 |
| `GET /events` | 全局 SSE，更新会话和通知 |
| `GET /sessions/:id/events?after=cursor` | 会话 SSE，断线后按事件 ID 接续 |
| `POST /approvals/:id` | `{decision: "approve"\|"reject"}` |
| `GET/POST /memories`、`GET/PATCH/DELETE /memories/:id` | 记忆检索、详情与管理 |
| `GET /memories/graph`、`GET /memories/:id/revisions` | 实体关系与历史快照 |
| `GET/POST /workflows`、`DELETE /workflows/:id` | 工作流列表、创建与删除 |
| `POST /workflows/:id/run`、`POST /workflow-runs/:id/cancel` | 工作流运行与取消 |
| `GET /notifications`、`PATCH /notifications/:id` | 通知及 `{read: true}` |
| `GET/POST /reminders`、`PATCH/DELETE /reminders/:id` | 提醒时间、内容、重复间隔与启用状态 |
| `GET/PATCH /settings` | 读取脱敏配置、保存已编辑字段 |
| `POST /integrations/chiliarch/pair` | `{code, name}` 配对 |
| `GET/POST /service` | 状态、安装、启动、重启、停止、启用 / 关闭自动启动 |
| `GET /export` | 流式下载完整 NDJSON 数据 |
| `GET /docs`、`GET /openapi.json` | 本地 API 参考与当前服务接口描述 |

SSE 事件结构为 `{id, type, session_id, data, created_at}`。支持 `run.started`、`message.delta`、`reasoning.delta`、`message.completed`、`tool.started`、`tool.completed`、`approval.requested`、`approval.resolved`、`run.completed`、`run.failed`、`run.cancelled`。全局事件另处理 `workflow.updated`、`notification.created` 和 `reminder.updated`。会话详情中的 `messages` 是已完成消息的权威来源；回放重建工具、审批、运行状态、最新思考内容和尚未完成的回复，不重复已保存的回复。事件 ID 去重避免重连后重复显示 Token。

设置保存时仅发送改动字段。空 API Key 保留已有值，显式 `clear_api_key: true` 才清除。`reasoning`、`extra_body` 与 `extra_headers` 作为完整对象更新，后端应支持清空对象，并保留未被显式修改的脱敏请求头凭据。

MCP 通过连接设置的 JSON 编辑器管理标准 `mcpServers` 对象；只提交实际改动后的完整 `mcp`。后端原子替换配置，删除服务器 / 字段和提交 `{}` 均会生效。`env`、`headers` 和 URL 查询参数的 `••••` 由服务端保留旧值；新增字段必须填写实际值。旧服务只返回 `configured` 标记时隐藏编辑器，标记不会回传。仅 `enabled: true` 启用服务器，未启用的配置不会启动进程或发起远程连接。

执行模型未配置时复用主模型；工具审阅模型未配置时交由用户批准。用户服务操作收到的是提交确认，界面随后读取实际状态；重启 / 停止使用短确认并处理连接中断。桌面通知默认关闭，是否可用和最近错误来自服务端状态。

提醒使用本地 `datetime-local` 输入，向接口发送 Unix 秒；重复间隔使用秒。到期通知和计数来自后端事件，网页不调用模型轮询时间。

## 使用与验证

后端启动后直接访问工作台地址。前端未预置演示会话、工具结果、通知或模型状态；服务断开和模型未配置分别显示真实状态。输入框内的草稿在视图切换和暂时断线时保留，刷新页面不会保存草稿。

快捷键：`Enter` 发送、`Shift + Enter` 换行、`Ctrl/⌘ + K` 新会话。中文输入法组合输入期间按回车不会发送。

从仓库根目录运行无依赖的前端测试：

```sh
node --test tests/frontend/*.test.mjs
```

测试覆盖 SSE 分片 / Unicode、回放去重、运行中恢复与取消后的部分输出、乐观消息确认、审批与停止状态、错误保留、秒级时间戳、提醒时间转换、链接协议过滤、脱敏设置更新、MCP 原子替换、文字对比度和 OpenAPI 引用 / 参数合并。测试不会调用外部模型。

2026-10-18 至 19 日，使用 Codex 内置浏览器连接临时实际服务完成桌面 1280×720、手机 390×844 验收：设置保存、会话改名 / 删除、记忆新增 / 编辑 / 检索 / 关系证据 / 修订 / 删除、模板创建工作流与未配置模型的真实运行错误、提醒创建 / 暂停 / 改期 / 到期通知 / 已读 / 删除、禁用 MCP 配置保存 / 清空和数据导出均已核验。`/api/docs` 的接口检索、参数、请求结构与引用跳转也已在实际路由验收，手机页面无横向溢出。

另外通过本地协议测试服务完成流式说明 → 文件读取审批 → 批准 / 拒绝 → 最终回复的端到端验收。批准后的工具结果含临时测试文件内容；拒绝后的工具结果仅含拒绝错误，回复不含文件内容。刷新并重新打开待审批会话后，历史文本与审批正确恢复。验收会话已删除，临时模型配置已恢复为空。没有执行用户服务变更、启用 MCP、发送桌面通知或调用外部提供商。

前端共 23 项自动测试通过；实测首屏、设置、已完成聊天和 API 参考的可读文字样本最低对比度为 4.84:1，控制台未记录警告或错误。

视觉验收截图：[桌面](screenshots/workbench-desktop.jpg)、[手机](screenshots/workbench-mobile.jpg)。两者均展示实际已连接但未配置模型的状态，无预置演示数据。

Markdown 展示支持段落、标题、列表、引用、代码块、行内代码、粗体与 HTTP(S) / 邮件链接。模型返回的 HTML 始终按文本处理；不加载远程图片或执行原始 HTML。
