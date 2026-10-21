# Agent Skills

MuLi 从 `~/MuLi/skills/<name>/SKILL.md` 发现指导。设置 `MULI_HOME` 时，默认目录随之改变；`skills.directory` 可指定另一个根目录。目录不存在表示没有安装 Skill，不影响普通聊天。核心包提供 YAML 解析，不要求安装 MCP extra。

例如将以下文件保存为 `~/MuLi/skills/review-notes/SKILL.md`：

```markdown
---
name: review-notes
description: 检查用户提供的学习笔记是否遗漏论据、前提和来源。
---
先保留笔记的原结论，再指出缺少证据或前提的段落。
需要示例时，按需读取 references/examples.md；不要把示例当作用户事实。
```

名称须与目录相同，长度 1–64，只用小写字母、数字和单连字符，首尾不能是连字符。`description` 为非空字符串，最多 1,024 字符。支持标准 YAML 多行字符串；frontmatter 须以顶格 `---` 开始和结束。建议使用临时文件后原子替换，避免编辑过程中发布半份指导。

文件安装后由扩展运行时自动发现。仅 Skills 文件变化、已接受 MCP 来源和扩展预算不变且没有完整重载请求时，可以在任务活跃期间独立发布 Skills；已物化的当前请求保持原正文，后续请求使用新目录并核验活跃修订。MCP、混合扩展目录、纯扩展设置、手动重载和 catalog refresh 走完整候选，也可在任务期间发布；模型、渠道等其它配置仍等全局空闲，`queued` 表示尚未加载。非法 YAML、入口过多、预算超限或半写会拒绝候选，已经接受的目录和完整正文继续服务。首次加载失败时没有 Skills-only 基线，后续必须先完整接受扩展候选。独立 Skills 发布不撤销 MCP 调用；完整 MCP 发布的旧调用及连接收尾见 [MCP 活跃换代](mcp-live-generation-design.md)。Skills 自身合同见 [Skills 活跃发布](skills-live-design.md)及[扩展热重载设计](extensions-reload-design.md)。

主会话或队友准备上下文时，发现已激活项被删除或修订变化，会持久清除该项并给出重新读取提示。之后恢复完全相同的旧文件也须重新调用 `skill_read`，其它有效指导仍保留；预算不足导致的暂停不会清除合法正文。未曾准备上下文的会话不会被全库扫描撤销，因此没有被该会话观察到的中间删除/恢复不改变其同修订激活。具体回归见[生命周期修复](extension-lifecycle-review.md)。

模型先获得有界的 `{name, description, location, revision}` 目录，正文不会全部加入每个请求。`skill_read(name, revision)` 激活指定修订，回执只含 `activated`、正文 hash 和 `delivery:next_model_context`，完整正文在下一次模型上下文中注入一次。正文完整交付沿用 `limits.max_output_chars`，还须容纳在活跃集合及完整请求预算内，超限返回 `skill_too_large`、`active_skill_budget` 或 `active_context_budget`，不交付半套指导。激活前会计入本次真实请求已有的其它固定资料；下一请求再次核验完整预算。目录修订不符返回 `skill_changed`，需要重新查看当前目录；发布了变更或删除后，旧正文不会自动替换成另一版。

已激活指导按会话保存，在上下文压缩或进程重新启动后可再次注入。重复读取同一 Skill 不叠加，也不会为新 Skill 静默挤掉旧指导。`skill_forget(name)` 清除指定项，不传名称则全部清除。若后续预算缩小或固定资料挤占空间，完整持久正文保留，但本轮明确 `guidance_status:suspended`，不会注入部分指导或声称已经应用；模型可继续调用清除工具，或修正预算再读。目录本身放不下时也明确暂停并留下有限恢复提示。首期 worker 结束不会立即删除其活跃记录，后续读取时才检查修订、失效或清除；这是每会话有限状态，不是全库会话数量上限。任意超大新输入等其它既有必要消息仍可能超过总预算，这不等同于 Skills 能保证所有请求可发送。

`skill_resource(name, revision, path)` 按需读取包内 UTF-8 文本，`offset` 是字符位置。返回 `revision`、`next_offset` 和 `truncated`；续读必须携带该资源的 `resource_revision`，文件改变后返回 `resource_changed`，避免把不同版本拼在一起。正文快照固定的是 `SKILL.md`，资源在读取时另取版本，并非整个资源包的永久快照。资源须为包内普通文件，拒绝绝对路径、父目录跳转、符号链接和非普通文件；异步读取后若该 Skill 已更换也拒绝旧结果。发布时完整相等的 Skill 复用原对象，无关 Skill 更新不使其误报换代；包目录身份变化即使正文 hash 相同也不复用。首期不自动执行 `scripts`，也不扫描整个资源树。读取仍受现有 `limits.max_file_bytes` 和包含完整 JSON 信封的 `limits.max_output_chars` 限制。

最新工具批次中的资源页在紧接的模型请求里保持完整，避免上下文压缩把正文节选后仍让模型沿原游标续读。必要上下文确实无法容纳整页时，明确报上下文预算错误，不发送半页；已完成读取的完整结果仍保存在会话中。后续回答、输入或工具批次后，旧页可正常节选，不会永久占用上下文。验证范围见[资源页交付审查](skill-resource-pages-review.md)。

源和资源通过绑定目录句柄逐段读取，祖先目录被替换为符号链接不能转向包外文件。POSIX 路径要求 `dir_fd`、`O_DIRECTORY`、`O_NOFOLLOW`。2.30 已发布的 Windows 适配仅支持普通绝对盘符路径上的本地 NTFS：从原生句柄核对精确 GUID 卷根，后续扫描、打开和复核均相对已持有的目录句柄，不跟随 junction 或其它重解析点；UNC、设备路径、ADS、SUBST 子目录及其它文件系统明确不可用。缺少必需 API 时状态为 `secure_file_reads:false`；路径或文件系统不支持时拒绝该来源并报告 `secure_read_unavailable`，不回退到普通全路径读取。空目录或关闭 Skills 仍正常，核心聊天不依赖这些平台能力。Windows 3.12/3.13 托管 CI 已通过真实临时 NTFS 竞争用例，前两次夹具失败及最终发行证据保留在 [2.30 审查](review-v2.30.md)；具体合同与边界见 [Windows Skills 句柄读取](windows-skills-design.md)。

MuLi 接受并验证 `license`、`compatibility`、字符串映射 `metadata` 与实验性字符串 `allowed-tools`，首期不把这些可选字段作为模型目录的一部分，也不实现 Claude Code hooks、subagent 或脚本自动执行。未知宿主字段只报告固定警告码。`allowed-tools` 不授予权限；Skill 是工作指导，不能替代主人指令或工具批准。所有读取工具沿用 MuLi 的统一权限门。目录来自用户受信任的本地文件，不是执行恶意本地代码的沙箱。

配置中的 `skills.enabled` 默认为 `true`，`directory` 默认为空，即上述默认目录。其它默认预算如下；修改配置经过既有校验和扩展发布流程，不会因超限自动扩大。

| 配置字段 | 默认值 |
| --- | --- |
| `max_entries` / `max_skills` | 一级入口 256 / Skill 64 |
| `max_source_bytes` / `max_total_source_bytes` | 单源 256 KiB / 合计 2 MiB |
| `max_frontmatter_bytes` | 16 KiB |
| `max_yaml_depth` / `max_yaml_nodes` | 8 / 256，拒绝别名、多文档和重复键 |
| `max_catalog_chars` | 目录完整 JSON 12,000 字符 |
| `max_active_skills` / `max_active_chars` | 每会话 4 项 / 完整活跃集合 JSON 24,000 字符 |

格式依据 [Agent Skills 规范](https://agentskills.io/specification) 和[渐进加载集成建议](https://agentskills.io/client-implementation/adding-skills-support)。MuLi 的目录位置、预算、会话状态和工具接口是宿主约定。离线回归使用临时目录和无模型的宿主替身，覆盖 YAML 边界、失败保旧、会话恢复、完整输出预算和资源续读；不等于已经验证真实第三方 Skill、全部平台文件系统竞争行为或包内脚本。
