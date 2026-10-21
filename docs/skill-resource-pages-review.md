# Skills 资源页交付

本片修复 `skill_resource` 在工具直接返回完整 JSON 后，紧接的模型请求仍可能收到头尾节选的问题。长会话触发上下文打包时，普通工具正文先被节选，之后才移除旧轮次；原有资源分页测试只调用 handler，没有走实际 Runtime 请求链。

实现范围仅为该工具启用已有的宿主 `preserve_output` 标记。完整资源页在最新工具批次的下一次请求中保留，必要上下文确实无法容纳时沿用明确的 `ContextLimitError`。后续回答、输入或工具批次后，旧页仍可正常节选；不会永久固定所有读取历史，也不改变文件读取、权限、修订或分页预算。

## 协议与验收

通过 Context7 核对官方类型：[OpenAI Chat](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_tool_message_param.py) 使用 `role=tool`、`tool_call_id` 和字符串 `content`，[Responses](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_input_item_param.py) 使用 `function_call_output`、`call_id` 和字符串 `output`；[Anthropic](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/tool_result_block_param.py) 使用紧随工具调用的 `user` 消息中的 `tool_result`、`tool_use_id` 和字符串 `content`。现有 codecs 承担格式转换，保留标记仅属于宿主，不能进入工具 schema 或请求正文。

专用回归复用现有 Skills 临时目录及真实 Runtime/假 provider，覆盖三种协议中的完整分页与字符游标、同一资源修订连续拼接、两轮之间改文件的明确拒绝、无法容纳时不发出下一请求，以及完整持久结果和宿主标记不泄露。文件内容包含 UTF-8、引号、反斜杠与 CRLF；不访问外网、真实模型或用户目录。

## 实测与边界

旧实现的 Anthropic 单例稳定失败：真实 Runtime 第二次请求中的工具正文变为 `{\n[结果已节选；原始记录保存在会话]\n}`，无法作为完整资源页交付。该红例 1 项、0.26 秒，守卫峰值 74.2 MiB；没有把直接 handler 返回值误当成实际请求证据。

仅增加保留标记后，三种协议的新增 9 项通过（0.31 秒、72.3 MiB）；既有 `test_skills.py`、`test_skills_live.py`、`test_context.py`、`test_evidence_context.py`、`test_evidence_runtime_review.py` 合计 136 项通过（1.18 秒、106 MiB）。新组与既有组共 145 个唯一用例，旧红例已包含在新组中，不重复累计。两份 Python 文件的 Ruff/格式检查和 `skills.py` 严格类型检查通过，最高峰值 146.3 MiB、swap 0。每次检查独占同一共享守卫、期限 30 秒，未提高既有限额。

根独立静态审查无剩余 P1/P2。这里只验证离线 Runtime 到现有三种 codec 的交付、持久结果及错误边界，不声称已经调用真实模型或复验各平台文件系统；后续精确候选 CI 仍由整合分支执行。本片不改变热重载、读取权限、文件预算或默认通知路由。
