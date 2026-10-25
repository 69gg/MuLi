# v2.54 原地改期提醒审查

2026-10-24，基线为2.53签名提交 `c15dc4301392ff76c84049ea6c9ab7e14f96b401`。主人可以在对话中让主沐璃把已有普通提醒或目标复盘改到未来时间，保留同一ID、原渠道、目标绑定及触发历史。Web原来已经支持编辑；本片补的是主协调的自然语言工具入口。原“删除再建”会换ID、重置次数和按当前输入重新选路由，无法替代此合同。

## 实现与边界

唯一生产文件为 `src/muli/runtime.py`：原协调白名单加入 `reminder_reschedule`，schema只收准确id和due_at；复用原Reminders.update、维护准入、write审批及epoch末门。同步核提醒、严格任务绑定及当前任务未done/cancelled后只更新due_at/enabled，至写入无await；写后才发原reminder.updated。没有新存储格式、依赖、队列或轮询。

普通与任务绑定提醒均重新启用，周期只改下一次时间，文案、周期和触发历史保持。任务绑定版本推进，尚未最终接纳的旧通知沿原proof末门拒；普通已产生的通知无此proof，已接纳或已发送的回合不承诺追回。坏绑定、终态、缺失任务或提醒、非法时间及未授权调用均在写入前拒绝。

非作者分别审查生产与第二条正式旅程，无剩余P1/P2。源码审不代替真实模型选工具或真实平台送达。

## 实际本地验证

首跑因测试helper首参与工具identifier同名而发生fixture TypeError，1 failed、pytest0.55秒、守卫1.235秒、97.4MiB，不计作功能红。最小改为原ToolCall/JSON组装后，真实私聊7创建、私聊8读取和更新目标、两个主运行收口均通过，再准确发现新工具未启用：1 failed、pytest0.60秒、守卫1.283秒、90.8MiB。完整记录见[方案](reminder-reschedule-proposal.md)。

最小实现后首正式旅程加五项独立policy共6 passed、pytest0.83秒、守卫1.520秒、98.4MiB。新增第二旅程并提取共同helper后，两正式selector2 passed、pytest0.80秒、守卫1.547秒、103.1MiB，含首selector复验不重复累计；五policy未改，新增为七个不同节点。

首旅程正常重开后旧due不产生复盘，新due读取最新真实目标，只向原私聊7发送一次。第二旅程先由真实scheduler产生旧notice/count1，再在Attention最终接纳前有限暂停，另一私聊真实工具改期后放行原submit_attention，配置锁内原guard真实拒绝；等待真实事件游标进度后核零旧ingress、receipt和模型轮。旧notice保留；正常重开后新occurrence2/count2读取最新目标，仍只原私聊7一次，重复通知不重复运行或外发。

相关旧七文件76 passed、pytest2.35秒、守卫3.404秒、170.7MiB；发布兼容166 passed、pytest0.95秒、守卫1.459秒、68.4MiB。合计249个不同本地节点。150源码strict通过、守卫8.405秒、417.8MiB。每次均swap0、root唯一串行验证槽，原共享及单次限制未放宽。

Ruff初查只有一个import排序，按原工具整理该处和三个文件纯格式后，全Ruff及399文件格式通过。83包离线锁检查通过，没有下载依赖。离线wheel/sdist构建通过、守卫1.380秒、98.2MiB；版本、兼容声明、changelog及两包一致性通过、守卫185毫秒、19MiB，未覆盖旧release-notes。隔离离线wheel安装及原API/资产/lifespan检查通过、守卫1.438秒、100.2MiB。签名候选及精确远端随后补录，尚不作为正式发行。

测试用临时数据、受控时钟、固定模型和HTTP替身；没有操作真实账号或桌面。受控时钟下的真实scheduler旅程不证明真实墙钟、自然模型或平台联调质量；正常重开不等于进程强杀恢复。

## 签名候选与远端检查

签名候选 `2cc123f974e66bab1d66ffabbbfb5b23b1d7a21e`（Good）的精确候选
[CI 40075592254](https://github.com/69gg/MuLi/actions/runs/40075592254) 十项全通过。
PR [62](https://github.com/69gg/MuLi/pull/62) 于 `2026-10-24T07:58:09Z` 合并到同提交。
GPG 标签 `v2.54.0` 已验签 Good，与 main 原子推送；此前远端 main 准确为已正式
发行的 `c15dc4301392ff76c84049ea6c9ab7e14f96b401`，没有覆盖其它远端工作。

正式 [Release 40076198383](https://github.com/69gg/MuLi/actions/runs/40076198383) 十三项
全部通过；重复 main CI `40076198551` 已核最终 cancelled，不计通过。

## 发行闭合

GitHub Release 于 `2026-10-24T08:08:37Z` 发布，非 draft、非 prerelease。根只查询有限
GitHub/PyPI 元数据，未下载包；版本与两构件未 yanked、字节和 SHA-256 均核对一致：

| 构件 | 字节 | SHA-256 |
| --- | ---: | --- |
| `muli_agent-2.54.0-py3-none-any.whl` | 840243 | `ec79a22b937701bfc42a8f508ad381f9d03ffe19b0a9f2339539a869a4258c33` |
| `muli_agent-2.54.0.tar.gz` | 4461645 | `d36ccf58b1622407aa7d30e55a36ef4bd3a32c92a354051b96f2d17385e28fee` |

核对守卫1.274秒、48.4MiB、swap0。正式发行不改变上述真实账号、自然模型与强杀恢复未验边界。
