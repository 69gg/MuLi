# Agent Skills

Skill 是可按需加载的工作指导，适合整理审阅方法、写作流程或项目约定。它不会自动执行脚本，也不能替代主人授权。

## 安装与启用

默认目录为 `~/MuLi/skills/<name>/SKILL.md`。设置 `MULI_HOME` 后默认目录随之改变；`skills.directory` 可指定其它根目录。`skills.enabled` 默认为 `true`。目录不存在时表示没有安装 Skill，不影响普通聊天，也不要求安装 MCP 可选依赖。

例如保存 `~/MuLi/skills/review-notes/SKILL.md`：

```markdown
---
name: review-notes
description: 检查用户提供的学习笔记是否遗漏论据、前提和来源。
---
先保留笔记的原结论，再指出缺少证据或前提的段落。
需要示例时，按需读取 references/examples.md；不要把示例当作用户事实。
```

`name` 须与目录名相同，长度 1–64，仅用小写字母、数字和单连字符，首尾不能是连字符。`description` 必填，最多 1,024 字符。文件顶部使用 `---` 包围 YAML，支持多行字符串，不接受重复键、别名或多个 YAML 文档。

也支持 `license`、`compatibility`、字符串映射 `metadata` 和字符串 `allowed-tools`；其中 `allowed-tools` 不授予工具权限。MuLi 不自动执行 Skill 中的 hooks、subagent 或 `scripts`。

## 在对话中使用

模型先看到名称、说明和修订目录，不会把全部正文加入每次请求。可以请 MuLi“使用 review-notes 检查这份笔记”：

- `skill_read(name, revision)` 读取并激活目录中指定的修订，完整指导进入下一次模型上下文。
- `skill_resource(name, revision, path)` 按需读取包内 UTF-8 文本，例如 `references/examples.md`。`offset` 为字符位置；续读使用返回的 `next_offset`，并携带原 `resource_revision`。
- `skill_forget(name)` 清除当前会话的一项指导；不传名称则清除全部。

活跃指导按会话保存，重启或上下文压缩后仍可使用；重复读取不会叠加。同一资源读取期间发生变化时需重新读取，不能拼接不同版本。资源必须是包内普通文件，不接受绝对路径、父目录跳转或符号链接。

Skill 的内容是工作资料。即使正文要求调用工具或执行脚本，也仍须遵守当前模式、工具权限与主人指令。

## 更新、重载与停用

修改文件后默认自动发现，也可在工作台 **设置 → 连接 → MCP 与 Skills** 点击“重新加载”，再用页面上方“刷新状态”查看加载结果与 Skill 数量。重载按已保存的配置处理，不保存当前草稿；排队不代表已生效，失败时保留当前可用扩展。也可通过已认证的 `POST /api/extensions/reload`（请求体 `{}`）请求重载，在 `self_status` 或 `GET /api/extensions` 查看结果。关闭 `extensions.watch_enabled` 可停用自动发现；关闭 `skills.enabled` 可停用 Skills。

合法文档变化可在任务运行时生效；当前已准备的请求保留原正文。会话下次准备上下文时，若发现活跃 Skill 已修改或删除，会移除旧指导并提示重新读取，不自动换成另一版。非法 YAML、超限或不完整文件不会替换上一份可用目录。编辑时建议先写临时文件，再完整替换。

指导须完整容纳在输出、活跃集合及模型上下文预算内，放不下时明确拒绝或暂停，不注入半份指导。暂停不等于删除，可清除不再需要的 Skill，或调整配置后重新读取。

## 容量与平台限制

以下字段位于 `skills`：

| 字段 | 默认值 |
| --- | --- |
| `max_entries` / `max_skills` | 一级入口 256 / Skill 64 |
| `max_source_bytes` / `max_total_source_bytes` | 单文件 256 KiB / 合计 2 MiB |
| `max_frontmatter_bytes` | 16 KiB |
| `max_yaml_depth` / `max_yaml_nodes` | 8 / 256 |
| `max_catalog_chars` | 目录 JSON 12,000 字符 |
| `max_active_skills` / `max_active_chars` | 每会话 4 项 / 活跃集合 JSON 24,000 字符 |

资源另受 `limits.max_file_bytes` 和 `limits.max_output_chars` 限制。Skill 目录来自用户信任的本地文件，不是恶意本机程序的沙箱。

Windows 仅支持普通绝对盘符路径上的本地 NTFS，不支持 UNC、设备路径、ADS、SUBST 子目录或重解析点。安全读取能力不可用时会明确拒绝该来源，核心聊天仍可使用。

文件格式可参考 [Agent Skills 规范](https://agentskills.io/specification)；MuLi 的目录、会话激活与工具接口以上述说明为准。
