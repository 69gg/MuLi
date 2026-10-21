# 2.28 工作流原路汇报与测试稳定性审查

2.28.0 已正式发布，精确候选、main 各八项和发行十一项全部成功。

## 工作流原路汇报

签名切片 `d99c902` 整合为 `104c98c`。工作流在实际创建前冻结宿主请求的
`ReplyOrigin`；同一主 run 先后领取不同渠道输入时，每个子工作流仍按自己的
来源产生结果消息。权限与收件人分离，Team 保留父报告链，原无来源记录
不猜测默认目的地。完整合同与作者红例见[设计与验证](workflow-origin-design.md)。

保留原总输出预算、所有子工作流成功后才整批发布，以及父取消/晚取消边界。
消息、事件与本地发布 marker 使用同一个同步事务；未知外发仍经原 outbox
处理，不增加队列、恢复扫描或自动重发。父在发布意图提交前崩溃不会自动
补发，已完成子结果仍在详情保留。

作者新增 25 项及既有去重 234 项通过，两个独立静态审查均无剩余阻断。
根分支整合测试按十八个不同文件分成两组：工作流、Runtime、来源相关
157 项通过（3.35 秒、237.1 MiB）；Team、Channels、故障与系统事件、管理
相关 182 项通过（5.95 秒、191.4 MiB）。合计 339 项包含作者用例，不重复
累计；全部 swap 0，使用原共享 2 GiB、单次 1 GiB、单 CPU、30 秒串行守卫。

## AX 绝对期限夹具修复

2.26 main 的 macOS 3.13 AX 早回调测试将真实 helper 的启动和 IPC 塞进
200ms 绝对期限。签名修复 `4d4e8a4` 已整合为 `061e9f6`，仅改一个测试：
每租约局部受控时钟与立即完成的 helper 门保留真实 request/parser/retire，
两次早回调仍约原期限，两次请求剩余预算递减，到原期限拒绝复用；不调整
生产期限或跳过断言。既有真实 serial/timeout/launch 测试保持原样。

两个静态审查均通过。作者单例 1 项通过（0.07 秒、58.7 MiB），整文件
76 项通过（3.06 秒、59.6 MiB），包含单例，不累计为 77。Ruff、格式与
定向测试类型检查通过；首次类型检查只发现局部 clock 从 silent import
推断为 Any，补显式 float 注解后通过，无运行语义修改。详见
[完整夹具证据](ax-ttl-fixture-review.md)。远端 macOS 是否通过仍以新候选为准。

## 候选终检

版本、锁文件与兼容声明同步为 2.28.0；根分支兼容和发行两文件 166 项通过
（1.12 秒、68 MiB），与上述 339 项文件不重叠。全仓 Ruff、310 文件格式、
131 源码 `mypy --no-incremental` 及 83 包离线锁核验通过；类型检查峰值
396.9 MiB。所有检查继续使用共享守卫串行执行、swap 0；未下载依赖或包。
本版不改前端产品，完整平台矩阵、网页与构件检查由新候选 CI 执行。

本轮没有真实模型、外部账号、用户服务或桌面验收；有限 Python helper
只用于原测试的临时子进程验证。尚未将本地通过视为正式发行或平台送达。

## 固定候选

签名提交 `2cfdeb230d6ec2e1a24f0516158c9fa9ca7391f5` 通过
[PR32](https://github.com/69gg/MuLi/pull/32) 接受自己的
[CI 39544909670](https://github.com/69gg/MuLi/actions/runs/39544909670)。本地签名
有效、工作树干净后推送；后续开发与发行事实记录进入独立分支，不修改
此固定候选。该候选矩阵 attempt 1 八项全部成功，包含 macOS 的两个
Python 版本，AX 确定性修复已获得本候选的平台验证。

## 正式发行

[PR32](https://github.com/69gg/MuLi/pull/32) 于 2026-10-21 04:44:11 UTC 合入
main，保留精确签名 `2cfdeb230d6ec2e1a24f0516158c9fa9ca7391f5`。
签名标签 `v2.28.0` 对象为 `2def6682b7ca2e2c7fc552585b02971389bccfd6`，
本地 GPG 与远端签名、指向均已核对。
[main CI](https://github.com/69gg/MuLi/actions/runs/39545666522) attempt 1 八项
成功；同一提交的[发行流水线](https://github.com/69gg/MuLi/actions/runs/39545666431)
attempt 1 八项独立验证及 prepare/publish/github-release 共十一项成功。

[GitHub Release](https://github.com/69gg/MuLi/releases/tag/v2.28.0) 于
2026-10-21 04:50:46 UTC 发布；[PyPI 2.28.0](https://pypi.org/project/muli-agent/2.28.0/)
未撤回，两平台的文件名、字节数及 SHA-256 逐项相同：

| 文件 | 大小（字节） | SHA-256 |
| --- | ---: | --- |
| `muli_agent-2.28.0-py3-none-any.whl` | 727747 | `c5d4961c343c9cb732b8d837276d05e005a0249d4f109bc7a132c1ab5cdf41d7` |
| `muli_agent-2.28.0.tar.gz` | 3745370 | `c6d7c5632c8fa6a94c2675ede33750fb23446bb289f4028e59d5b07901f0ef7b` |

仅读取小型 JSON 核验，没有下载发行包。没有以新版本通过改写 2.26 的
历史 main 失败，也没有把临时 helper 验证称为真实 macOS 桌面操作验收。
