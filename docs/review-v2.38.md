# 2.38 MCP 资源附件审查

状态：实现、独立后审与根离线整合验证完成，精确候选/main 各十项、发行十三项
全部成功，签名与 PyPI/GitHub 两构件元数据一致，已正式发布。

## 实际行为

新增固定 `mcp_resource_save(server, ref, name)`，从已展示的普通资源引用取回
一个完整 blob，保存到现有附件库。完整回执包括 ID、MIME、大小与 SHA-256，
不包含正文、路径或 URI；后续看图和渠道发送仍由调用者显式选择。
单次 external 准入同时覆盖远端读取与本地保存，沿真实执行 owner、原请求、
维护和暂停门及原代连接 lease。已接纳操作使用原连接、回调、输出预算和期限
收尾，不承诺途中换代、暂停或禁用能撤销已接纳的保存。

严格核单 blob、ASCII/base64 和实际字节，在文件效果前预检完整回执；没有新
writer、DDL、Settings 或下载库。保存器进入后的失败保留 unknown，取消仍透传，
不自动再次取回或保存。文件与数据库不是跨介质原子事务；保存后无法完整交付
模型上下文，也不能把已经发生的文件效果改记为未保存。

## 作者与独立后审

设计作者签名 `dbe0824917c9f11ecffb9e40256e7004a1822e18`，根整合为 `f459040`。
实现作者签名 `ca13168058c4827e9c806fc9bb6d5b7beae7d6a2`，根整合为 `cfd02f7`；
取消/上下文测试补片 `fbb58cd7f5eb2c0e42506b760504dfe87c44d00b` 整合为 `cd126f1`。
作者先通过真实 Runtime 取得资源引用，旧码返回“工具未启用”的反例 1 红后实现；
最终新增文件 50 项通过，真实 SDK 只通过流式 MockTransport，完整证据见
[设计记录](mcp-resource-files-design.md)。

独立 Sol 只读审查跟踪了真实装配、入口门、原代 lease、已展示 ref、严格解码、
附件保存及失败状态，未发现可证实的 P1/P2。根又复核生产 diff、三个协议回执和
真实 file_io/metadata 取消补片；没有扩大取消、回退或外部集成的证明范围。

## 根整合验证

以下集合分别计数，不与作者重复累计：

- 资源附件、原资源和文本模板七文件：162 项通过，4.30 秒，145.9 MiB。
- MCP、两种换代、Extensions、Skills、LocalServices、附件记录和媒体八文件：
  228 项通过，3.19 秒，188.6 MiB；仅精确排除既有
  `test_real_fastmcp_in_process_client_lifecycle`。
- 兼容声明与发行两文件：166 项通过，1.00 秒，68.9 MiB。

完整 Ruff、353 文件格式、142 源码非增量严格 mypy 通过；类型检查 8.508 秒，
421.6 MiB。83 包锁在离线模式校验通过。版本、运行时、兼容声明、锁与 changelog
均同步为 2.38.0，没有改变持久格式。新增附件文件已加入原 MCP extras CI。
所有本地检查串行经过 30 秒守卫，1 GiB/共享 2 GiB、CPU 1、swap 0，输出确认实终态。

未安装、下载依赖或运行真实模型/MCP/stdio/渠道、设备、账户、挂载与系统服务。
vision 为方法替身，OneBot 为 MockTransport；没有真实账号或外部文件验收。
write/fsync 单次取消、metadata 真实提交后丢返回及三 codec 整批上下文不足均已覆盖，
任意重复取消仍未验。资源模板、订阅、多块和 stdio 资源不在本片范围。

## 既有回退的有限观察

候选还纳入作者签名 `fe204bec19ff9efe4781a3a51f699d8536834f06` 的测试观察补片，
根整合为 `42161c9`。只在原同步核验期间包裹原函数，分别观察 controller 与 selected
文件核验的调用数和耗时；仍逐次调用原函数，退出即恢复查找点。没有修改生产行为、
缓存校验、延长期限或减少真实握手/GET。总 check 已包含分项，不能重复相加。
严格 reader 继续保留 4 KiB、32 步和实际握手条件，只把已验证的记录加入失败摘要。
见[设计与协议](managed-recovery-timing-design.md)。

根复核 53 项纯例通过，0.22 秒、峰值 67.8 MiB/swap 0；三文件 Ruff/format 通过。
测试文件独立严格 mypy 初次未指定源码查找路径，父类导入被解析为 Any，产生两项
错误；明确 `MYPYPATH=/data0/MuLi/src` 后同三文件全部通过，6.333 秒、360 MiB，
没有关闭类型规则或修改实现规避错误。
独立 Sol 后审未发现确定 P1/P2。原 main 回退超时仍未证明修复，Linux 原生服务三事务
仍未验收，见[2.37 原始失败记录](review-v2.37.md)。

末次文档与发行入口审查纠正了 README/架构中 Windows 动作仍待验的旧表述：
2.37 的 CI 自有按钮检查已通过，真实用户桌面仍未验收。

## 精确候选与发行准入

精确签名 `5ad4bd7584c99ccc4574a1968e1afafc3341f2a6` 的候选
[`39973040645`](https://github.com/69gg/MuLi/actions/runs/39973040645) attempt 1
十项全部成功：quality、MCP、两种 Python 的 macOS/Windows，以及 Linux 各两片。
两个 Linux 第二片作业本次通过，不能据此称已证明修复间歇超时。

根确认实际十个 job、精确 SHA、有效 GPG、远端 main 快进关系与远端标签不存在后，
非强制原子推送 main 与签名 `v2.38.0`。标签对象为
`290f6dafa73c419fb376f494f4d6c9076a4adc19`，验签通过；
PR [44](https://github.com/69gg/MuLi/pull/44) 于 2026-10-23T14:59:41Z
按原提交合入。发行
[`39974392125`](https://github.com/69gg/MuLi/actions/runs/39974392125)
attempt 1 十三项全部成功，没有重跑、改签或重写标签。main
[`39974391964`](https://github.com/69gg/MuLi/actions/runs/39974391964)
同一精确 SHA 的十项也全部成功；历史 2.37 的回退超时仍保留为未证明修复。

GitHub release [`421983834`](https://github.com/69gg/MuLi/releases/tag/v2.38.0)
于 2026-10-23T15:07:18Z 发布，资产为：

| 构件 | 字节 | SHA-256 |
| --- | ---: | --- |
| `muli_agent-2.38.0-py3-none-any.whl` | 777405 | `186084a863670d61bcd0868c2347d3efc5f2e98e4537aaa03a1d2a8039e508df` |
| `muli_agent-2.38.0.tar.gz` | 4021288 | `0630e4b973183d596acc49429660e20ffd64bb59ef5e94ad74a9031a1faf1bfb` |

直连限时、限 128 KiB 读取 [PyPI 2.38.0 JSON](https://pypi.org/pypi/muli-agent/2.38.0/json)，
没有下载 wheel/sdist。守卫内只读核验项目名、版本、唯一两个文件、类型、未 yanked、
字节数与 SHA-256，全部与 GitHub 元数据一致；75 毫秒、15.4 MiB、swap 0。
