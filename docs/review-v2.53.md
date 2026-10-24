# v2.53 私聊索取成果文件审查

2026-10-24，基线为已正式发行2.52的签名提交 `3f8832a1e1551d431b8bdc046783ecb8e3122ea0`。本片新增主协调的窄入口 `channel_reply_files`：主人说“把队友的草稿发到这里”时，模型只选已有本地附件和稳定key，宿主从本次真实私聊冻结收件人。原通用 `channel_send` 已经可用，本片不冒称之前不能发送文件；它减少主协调填错收件人的机会。

## 接纳与生产范围

唯一生产文件为 `src/muli/channel_application.py`，其余是测试、文档及四份2.53.0元数据。复用原工具provider、附件整组验证、external审批、持久outbox、FrozenRouting和平台适配器；没有新存储格式、依赖、队列或后台轮询。

handler同时核真实主身份、当前Task、工具执行上下文、run和PreparedRequest，拒绝借用ContextVars的新Task、旧handler和worker。只认本次真实private origin，Web、群聊及无来源不选择默认目的地；接纳前核当前配置和当前manager授权。原external/epoch/pending末门到file-mode enqueue提交之间没有await，派送时继续按原owner/账号绑定复核。附件整组坏、重复引用或撤权无部分入队。幂等摘要绑定准确input_id与stablekey，同目的地的新输入不误合并；unknown仍不自动重发。

独立非作者审查生产链与第二正式旅程，均无剩余P1/P2。源码审不证明自然模型会选择正确工具，也不证明真实平台已收到附件。

## 实际本地验证

正式先红先经过真实任务登记、Team派工、worker写入与发布、最终报告、Web下载和另一私聊运行，再准确停在主目录缺新入口，1 failed、pytest0.67秒、守卫1.344秒、91.4MiB。具体合同和完整失败历史见[方案](channel-reply-files-proposal.md)。

最小实现后的六节点首轮5 passed、1 failed，pytest1.04秒、守卫1.905秒、112.6MiB。失败为独立worker负控在异步launch前读取空tasks；仅加原 `team.wait(timeout=2)` 等真实终态，准确selector随后1 passed、pytest0.47秒、守卫1.151秒、87.6MiB，原断言和预算未减。六个不同节点均有通过终态，不写成一次六项全绿。

第二正式旅程通过：实际上传已核bytes后抛ReadTimeout，经正式适配器形成unknown；同run相同input/key调用复用原ID，真实channel_status消费状态；默认关闭组件和Runtime，再以原Store新建Runtime/app。重投准确旧信封不新增消息、run或入站回执，新私聊实际运行并完成出站后原unknown和文件HTTP次数仍保持。该节点1 passed、pytest0.64秒、守卫1.335秒、92.4MiB。两正式加五独立负控合七个不同新增节点。

相关六文件232 passed，pytest2.28秒、守卫2.983秒、155.8MiB；发布和兼容166 passed，pytest0.97秒、守卫1.493秒、67MiB。新增及旧回归共405个不同本地节点。每次均swap0、root独占串行检查；共享2GiB和单次1GiB资源限制未放宽。

150源码strict通过，守卫8.284秒、417.5MiB；Ruff通过。格式检查只发现三个改动文件的纯格式差异，沿原formatter整理后397文件全部通过，没有语义变动或额外重复计数。离线锁检查83包通过，未下载依赖。离线wheel/sdist构建通过，守卫1.382秒、99.2MiB；版本、兼容声明、changelog及两包一致性通过，守卫181毫秒、19.3MiB，未覆盖旧release-notes。隔离离线wheel安装及原API/资产/生命周期检查通过，守卫1.273秒、99.4MiB。精确远端证据随后补录；候选尚未作为正式发行。

测试仅使用临时数据、ASGI、锁定SDK和平台HTTP替身，没有操作真实桌面或账号。正常关闭重开不等于强杀窗口恢复；unknown不能判断平台已经收到还是没收到，不因重开自动猜测或重发。
