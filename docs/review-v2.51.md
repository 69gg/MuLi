# MCP 搜索结果原文读取（2.51 审查与发行）

2026-10-24，本片补上“已配置 MCP 搜索返回资源链接，而普通资源目录为空时，队友仍可显式发现并读原文”的用户旅程。基线为已发行2.50精确签名提交 `d04ba0905e0292626403b61452a8dfca5abd3f83`，其发行证据另见[2.50审查](review-v2.50.md)。四发布元数据统一为2.51.0，精确签名候选、远端CI、正式发行及两处构件元数据核对均已完成，证据见下文；不回改旧标签。

## 实现与边界

- 生产仅改 `tools/mcp.py` 与 `tools/mcp_resources.py`。原工具完整成功结果中的 typed `resource_link` 可留下 `resource_links_available:true`，调用者再用 `mcp_resources_list(source="tool_result")` 显式展示引用，经原 `mcp_resource_read` 读取完整短文本。普通资源目录为空仍可工作，不新增远端工具、HTTP抓取、数据库或后台队列。
- 只认原SDK typed内容；普通文字、structured仿造、错误、截断或整组校验失败不授权。全部链接先按既有条目、UTF-8、URI和完整逻辑字节预算验证，未知字段也计费，不发布合法前缀。只有原完整展示页返回的ref才可读。
- 每连接只留一份有限结果集合及最新call token，新调用先撤销旧集合；较新失败/取消后，旧调用晚返不能复活。发布前同步复核原generation、准确connection、usable、receive latch和原绝对期限。MCP换代含复用连接均清理；Skills-only保持原合同。
- ref/cursor为带来源前缀的宿主不透明值，普通目录token保持。结果引用只支持文本read，含过期引用均在附件factory、reservation及远端RPC前拒绝save。已接纳read的旧代完成、external审批、主协调/reviewer/Attention/disabled过滤、HTTP/SSE和resources capability原门保持，stdio不新增资源能力。
- 元数据和原文始终是不可信外部资料，不成为指令、Skill、权限或工具定义。没有自动保存或发送文件，也不提高原上下文、输出、超时、pending或存储格式预算。

用法见[MCP说明](mcp.md#搜索结果资源链接)，准确合同、工具轮次和逐轮失败见[设计记录](mcp-result-resources-proposal.md)。

## 已收本地结果

以下均由根在共享资源守卫中串行收取真实终态；不同测试节点只计一次。

| 范围 | 实收结果 | 证明范围 |
| --- | --- | --- |
| 新正式旅程1项与独立策略5项 | 6 passed；pytest0.96秒，守卫1.782秒、114.6MiB | 真实ASGI/OneBot、Runtime/Team、锁定SDK/MockTransport搜索→展示→原文read→真实报告→原私聊一次/Web可见；五项有限来源/生命周期/保存/预算负控 |
| 既有MCP八文件 | 181 passed；pytest3.76秒，守卫4.658秒、169.6MiB | 原MCP相关合同回归；一个故意错误类型用例保留Pydantic serializer warning |
| 其它六个相关文件 | 131 passed；pytest3.20秒，守卫4.123秒、176.9MiB | Runtime及周边既有集成回归 |
| 全150源码strict | 通过；守卫8.429秒、417.4MiB | 严格类型检查 |
| 全仓Ruff | 通过；83毫秒、24.6MiB | 导入顺序检查无修改 |
| 393文件格式 | 通过；68毫秒、22.1MiB | 根仅统一排版两个文件，无语义修改 |

以上功能与相关回归为 **318个不同测试节点**。正式旅程中的多个工具步骤和五个策略节点内的正控不另计项数。A已完成两生产模块正式非作者后审，未发现剩余必须修复的P1/P2；独审本身不增加运行或外部集成证据。

## 历史失败

1. 首跑1 failed，pytest4.97秒、守卫5.744秒、134.2MiB、swap0；等待主安排回复超时，尚无搜索。新夹具误将真实spawn返回的 `id` 取为 `agent_id`，只修取值与有限诊断，原4秒等待和轮次不变。这是前置失败。
2. 第二跑1 failed，pytest4.89秒、守卫5.655秒、113.9MiB；主安排已完成，队友终态等待超时。夹具在默认auto下已经创建持久主会话，后改默认值没有更新它，队友继承auto而external调用需主人批准。只沿现认证PATCH显式设yolo并断继承，不伪造批准或降低生产风险。这仍是前置失败。
3. 第三跑1 failed，pytest0.89秒、守卫1.774秒、113MiB、swap0；真实搜索、SDK `ResourceLink`、accepted完整结果、队友报告及原私聊/Web均已成立，外层准确断言 `resource_links_available is True` 得到 `None`，旧typed块为 `omitted:true`。这是功能目标红，获准实施后上述六节点通过。

## 本地发布验证与外部边界

根随后在共享资源守卫内收取以下全部成功终态，无依赖或发行包下载：

| 检查 | 实收结果 |
| --- | --- |
| 发布/兼容166项 | 166 passed；pytest1.00秒，守卫1.564秒、67.3MiB |
| `uv lock --check --offline` | 83个包；守卫59毫秒、29.5MiB |
| 离线构建wheel与sdist | 输出 `/tmp/muli-v251-candidate-dist`；守卫1.342秒、98.3MiB |
| `release.py` v2.51一致性 | 守卫177毫秒、19.7MiB |
| `check_wheel` 隔离离线安装 | 守卫1.324秒、99.7MiB |

功能与相关回归318项加发布/兼容166项，去重合 **484个不同测试节点**。本地构建和安装通过不代表远端构件字节一致；以下发行核对比较的是GitHub与PyPI的实际远端构件。

## 发行闭合

根以有效GPG密钥签名候选 `c849a1f3afa738b8d236c05425bc84cce7ab2716`，
[PR59](https://github.com/69gg/MuLi/pull/59)于2026-10-24T06:10:53Z以同一提交进入main。
[候选CI40068709313](https://github.com/69gg/MuLi/actions/runs/40068709313)十项通过；
签名标签 `v2.51.0` 验签成功，核旧main准确为2.50且新标签不存在后原子推送main与标签。
重复main CI40069173250最终为cancelled，不充作成功。
[发行40069173567](https://github.com/69gg/MuLi/actions/runs/40069173567)精确对应同一提交，
十三项全部成功，包括prepare、PyPI publish与GitHub release。

[正式发行](https://github.com/69gg/MuLi/releases/tag/v2.51.0)于2026-10-24T06:19:53Z公开，
不是draft或prerelease。根只查询小元数据，未下载发行包；PyPI两文件均未yank，
大小与SHA-256逐项等于GitHub同名资产：

| 文件 | 字节 | SHA-256 |
| --- | ---: | --- |
| `muli_agent-2.51.0-py3-none-any.whl` | 836440 | `80cddd2de25d11faab8985bc67185f32bd0d902ca9ecaab1bbfd1668cfc4ef64` |
| `muli_agent-2.51.0.tar.gz` | 4406644 | `98f940e5c010262294eee5e0c833831ac0e179914d04d3afc3a4bf5ff7d9b1e5` |

覆盖边界：此版本远端MCP作业沿用旧显式文件表，未包含新资源链接的两份测试；
新增SDK旅程与策略的通过证据来自前述本地受保护运行。后续候选将MCP文件改为模式自动发现，
补上远端带SDK的覆盖；不能把十项或十三项全绿倒推成每个新增可选测试均在远端执行。

本地仅用临时配置/Store、固定工具策略、MockTransport及合成原文，无真实模型、账号、外部MCP或桌面操作。正式旅程固定yolo只证明宿主协议链，不证明auto自然批准或自然模型搜索质量；OneBot outbox也不等于真实平台送达。外部服务器的读取能力、ACL及内容质量仍须单独联调。
