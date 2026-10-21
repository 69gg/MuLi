# 2.25 活跃任务中的 Skills 发布

2.25.0 已发布至 PyPI 和 GitHub，精确候选八项 CI 与发行十一项作业成功。
main CI 有一项恢复测试的最终进程收尾超时，未把该结果记成全部通过；
具体失败和后续处理见下。

## 功能与复用

自动目录发现的纯 Skills 文件变化可以在任务繁忙时发布。已经物化的请求
保留旧正文，旧修订读取明确拒绝，下一上下文按既有规则显示失效提醒。
完整相等的 Skill 对象继续复用，避免另一个包的更新误伤资源读取。

扩展运行时保留最近完整成功接受的配置与 MCP 来源指纹；只有证明 MCP、
Skills 设置和预算均未变化，且没有完整重载或 catalog 请求时，才允许快速
发布。准备后的最后等待边界重新核验请求代次、配置、来源、维护门、关闭
状态和资料库格式。连接与工具注册表不换代，状态明确 `scope:skills`。
失败保留原能力；不增加设置、持久账本、Runtime 请求字段或历史快照。
完整合同见[Skills 活跃发布](skills-live-design.md)。

## 独立审查与修复

作者先用真实临时 Runtime 的门控模型请求和资源读取 await，复现旧实现的
两个缺口：忙碌时一直等待全局空闲；无关 Skill 发布导致正在读取的资源
误报修订变化。修复后覆盖请求前后正文、旧 read 拒绝与下轮失效提示。

两位审查者定位手动重载先到、目录变化后到时，自动请求会覆盖原请求归属。
新增粘性的完整重载标志，仅同一请求的整代成功发布可以解除；后续目录发现
不能降低已有请求语义。有连接和无 MCP 连接的反例均覆盖此顺序。最终独立
静态审查没有剩余 P1/P2；文档中的现行说明同步，历史验证保留原边界。

## 作者验证

新增 26 项与既有 Skills、扩展、watcher、来源及本地工具相关 131 项合计
157 项通过，耗时 5.95 秒，峰值 150.3 MiB、swap 0。三份 Python Ruff/格式
以及两份源码严格类型检查通过，类型检查峰值 145.7 MiB、swap 0。
这些是作者分支证据，不能代替最终整合检查或跨平台 CI。

所有检查通过共享 `muli-checks.slice` 串行执行：合计 2 GiB、单次 1 GiB、
swap 0、一个 CPU 配额，本轮单组期限 30 秒。使用临时文件与离线客户端，
测试硬拒真实网络和子进程；没有安装依赖、读取用户 Skill 或连接真实 MCP。
Windows 的安全 Skills 文件句柄尚未实现，能力不足时继续明确拒绝加载；
不能把 CI 的平台跳过说成 Windows Skills 实测。

## 最终整合与发行

已整合签名片 `b350972` 为 `12ce75c`，版本元数据同步为 2.25.0。
最终本地整合按十个不同测试文件分为两个不重叠组：160 项和 230 项全部
通过，分别耗时 5.98 秒与 1.90 秒，峰值 156.1 MiB 与 116.4 MiB，swap 0。
它们包含作者测试，不能与前述 157 项重复累计。第二组同时覆盖 MCP、
Attention、兼容声明及发行校验。

全仓 Ruff、305 份 Python 文件格式、130 个源码的 `mypy --no-incremental`
与 83 个包的离线锁核验全部通过；类型检查峰值 395.8 MiB、swap 0。本版
没有前端产品变更，完整网页检查和安装包构建将由候选 CI 执行。
本地局部检查与下面的候选、main、发行验证分别记录。

## 正式发行与 main 检查差异

签名提交 `2df45a1c67f84c9930ccbfd3e8d84409c127738f` 的
[PR29 候选 CI](https://github.com/69gg/MuLi/actions/runs/39538578747) 八项成功。
[PR29](https://github.com/69gg/MuLi/pull/29) 于 2026-10-21 03:34:39 UTC 合入
main，精确保留同一签名提交。签名标签 `v2.25.0` 对象为
`f3b0f7f51f4067630036c379eb04ea48b28a3e84`，已核对本地 GPG 与远端指向。

[main CI](https://github.com/69gg/MuLi/actions/runs/39539335917) attempt 1 中
quality、MCP、Ubuntu 3.12、macOS/Windows 两个 Python 版本共七项成功；
Ubuntu 3.13 的 `target-rejected-rollback` 用例失败，汇总为 1 failed、
4177 passed、6 skipped。恢复 controller 已解决、两次真实 nonce 接口的
released 证明与身份断言已通过；最终 `live.wait` 使用原操作期限的剩余值，
仅得到 0.032199681 秒，抛出 `subprocess.TimeoutExpired`。没有取消作业或
重跑掩盖该证据，后续分支正在分离操作期限与受限收尾，不调整生产服务期限。

相同提交的[发行流水线](https://github.com/69gg/MuLi/actions/runs/39539335699)
attempt 1 的八项独立验证及 prepare/publish/github-release 共十一项成功。
[GitHub Release](https://github.com/69gg/MuLi/releases/tag/v2.25.0)
于 2026-10-21 03:42:10 UTC 发布；
[PyPI 2.25.0](https://pypi.org/project/muli-agent/2.25.0/) 未撤回，两个平台的
文件名、大小和完整 SHA-256 一致：

| 文件 | 大小（字节） | SHA-256 |
| --- | ---: | --- |
| `muli_agent-2.25.0-py3-none-any.whl` | 723014 | `a876036537d475d361b6e0a4484afbdb8b7aff9e353f75198232aba784e53296` |
| `muli_agent-2.25.0.tar.gz` | 3704347 | `dac8ee1e4aa074486cc93bca98bdd3d224b6a1da0aa58b2ce6c2e223e12fe145` |

发行对照只直连读取 JSON 元数据，没有下载构件或安装用户服务。
已发布不等于所有检查成功；上述 main 失败继续跟进，并在后续版本单独验证。
