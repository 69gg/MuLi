# 2.34 MCP 接收保护审查

状态：已正式发布。最终精确候选/main 各八项与发行十一项全部成功，PR40 原提交
合入，签名标签及 PyPI/GitHub 两构件元数据一致；精确证据见末节。
真实外部 MCP、stdio 帧保护及 Linux 原生 systemd 整链不属于这里的成功证据。

## 用户结果与实现

HTTP/SSE 响应在解码前检查原始字节。普通 body 按总量、实际流式 SSE 按事件计费，
包括注释、未知字段与跨块 CR/LF/CRLF。非 identity 压缩响应及非 UTF-8 SSE 明确
拒绝；长连接不会因为累计接收正常小事件而耗尽单条消息预算。

默认每条 1 MiB，可用 `self_mcp_receive_configure` 或认证管理 API 在 64 KiB 至
4 MiB 间配置。策略保存在现有 Store 的独立对象中，不改变 Settings、DDL 或配置
合同；坏记录拒绝覆盖。期望值和已发布代的有效值分别显示，保存不代表立即生效。

限额绑定到具体连接，修改后沿原候选、换代和退休机制发布；在途调用保留原限额。
SDK 吞掉后台 GET 异常时，接收层仍同步锁存失败，阻止后续派发与复用；已派发请求
不自动重试。固定错误码进入 `self_status.extensions.mcp` 和扩展 API。
主动系统通知是独立后续工作，没有借目录变更回调触发重连或冒用已有日志来源。

作者签名 `ca439096b66293448f5afd7a7b37f39079558770` 整合为 `0bff414`。
根与独立 reviewer 核对 HTTPX 公共 hooks、锁定 SDK 的实际消费方式、候选发布门、
保存事务、关闭归属与认证 API，无剩余确定 P1/P2。CI 的既有 MCP extras 作业增加
三个新测试文件；可选 decoder 仅在相关测试局部加载，普通矩阵仍执行纯协议和配置回归。

## 验证与资源

作者 15 文件 330 项唯一用例通过，包含真实流式 MockTransport、锁定 SDK、FastMCP
及 Runtime；没有真实网络或子进程。该组详见[接收设计与验证](mcp-wire-budget-design.md)。

整合后根分支两组共十文件 333 项通过，不与作者结果重复累计：

- 新接收/策略/Runtime 与既有 Extensions/watcher/Skills 六文件 146 项，3.23 秒，215.5 MiB。
- 兼容、发行规则、管理与 HTTP 四文件 187 项，2.51 秒，139.6 MiB。

全仓 Ruff、335 文件格式、138 源码完整 `mypy --no-incremental` 及 83 包离线锁检查
通过；最高峰 400 MiB，swap 0。每项使用原共享资源守卫串行执行，单次 30 秒、
1 GiB、共享 2 GiB、单 CPU，没有放宽上限、安装依赖或操作本机服务。
首次测试命令误写 watcher 文件名，收集前退出、没有运行用例；更正为仓库已有路径后
执行上述六文件组。一个 Pydantic warning 来自刻意构造无效字符串限额，拒绝断言通过。

此片没有前端变更，完整跨平台测试、包构建和安装验证交由精确远端 CI。

## 保留边界

保护不涵盖 stdio 帧、底层已分配的单个 transport chunk、全部连接总 RSS 或小事件
速率。旧 slot 保留未知策略对象，但不会执行新接收限制。MCP 资源目录/内容工具和
后台故障主动通知仍在研究设计中，不作为已交付能力。

Linux 原生服务验收仍在独立 PR37。第六轮精确候选 `7349b47` 的 CI `39580845313`
attempt 1 八项成功、原生一项失败；原生回执停在 `manager_start_before`，用户 manager
仍为 `activating/start`，没有开始任何切换事务。有限进程诊断继续独立推进，不通过
放宽期限、重跑或本机实测掩盖失败。

## 首次 CI 的可选依赖类型边界

PR40 首次签名候选 `e5a715e012e1a383198507c61089636e8ee345d2` 的
CI `39583336308` attempt 1 最终七项成功、quality 一项失败；六平台/Python 组合
及可选 MCP 作业全部通过，quality 作业 `119166041143`
在严格类型检查报 `mcp.py:188 no-any-return`。普通 quality 不安装 MCP extras，
因此 SDK 工厂来自被忽略的缺失模块，其推断返回值为 Any；本地已装 extras 没有
触发这一分支。不是协议或运行时失败，也没有为了检查安装额外依赖。

根使用临时 mypy per-module `follow_imports=skip` 配置，将两个可选包替换为 Any，
在原守卫下复现相同错误（3.729 秒、204.9 MiB）。按锁定 SDK 已有返回合同，仅给
局部客户端补上 `httpx.AsyncClient` 注解后，同一反例通过（3.596 秒、204.6 MiB），
原正常依赖类型检查亦通过（5.900 秒、344.9 MiB）。这验证类型边界，不冒称已经
创建真正无 extras 的运行环境；干净安装仍由原 CI 验证。

生产行为、依赖、检查规则和 CI 作业均不变；该单纯类型注解无需另写镜像实现的
运行时测试。保留原 333 项回归与 SDK 覆盖，修改文件的 Ruff/格式复验通过。
第一次格式检查要求将新注解调用拆行，已按既有格式修正，不修改类型语义。

## 精确候选与发行门

签名修复 `e76226c216b91f404eb568bfb54cbb85772c6dc3` 的
[候选 CI](https://github.com/69gg/MuLi/actions/runs/39584434138) attempt 1 八项全部
成功，包含三平台 Python 3.12/3.13、质量与可选 MCP。根独立核全部 job、精确
提交、本地和 GitHub 签名、main 祖先及标签不存在后，非强制原子推送 main 与标签。
[PR40](https://github.com/69gg/MuLi/pull/40) 于 2026-10-21 10:33:07 UTC 合入同一
提交。签名标签 `v2.34.0` 对象 `9d4271955896cadefaead22d31185706e5bd009b`
指向该提交，本地与 GitHub 均确认 GPG 有效。首次七项成功、一项失败的记录保留。

同一 SHA 的 [main CI](https://github.com/69gg/MuLi/actions/runs/39585414183)
attempt 1 八项成功；[发行流水线](https://github.com/69gg/MuLi/actions/runs/39585416301)
attempt 1 十一项成功，包括重复完整矩阵、质量、MCP 和 prepare/publish/github-release。
根逐个核对全部作业的精确 SHA 与终态，不以候选检查替代发行验收。

[GitHub v2.34.0](https://github.com/69gg/MuLi/releases/tag/v2.34.0)（release `419435201`）
于 2026-10-21 10:41:11 UTC 发布，非草稿、非预发布。根读取小型元数据确认与
[PyPI 2.34.0](https://pypi.org/project/muli-agent/2.34.0/) 两包的名称、大小和 SHA256
一致，PyPI 均未撤回；没有下载或安装发行包。

| 文件 | 字节 | SHA256 |
| --- | ---: | --- |
| `muli_agent-2.34.0-py3-none-any.whl` | 764254 | `de9bf54533da4234e455d6081f00cb4dca957057a087661e0ee989872e79d7b4` |
| `muli_agent-2.34.0.tar.gz` | 3905102 | `5790e8c7cf66bf3cdf4e21247bd0557a2a4cb19ad8c1109a23c66b3a500b42bd` |

首次候选的类型检查失败保留，不移动或重写已发布标签。发布后继续统一通知、
资源能力设计及独立 Linux 原生验收，未将这些后续工作记作本版本已交付。
