# 2.37 MCP 文本模板与 Windows 按钮操作审查

状态：两片实现与独立后审完成，根整合的定向回归及完整静态检查通过；
真实 Windows 动作与远端完整矩阵尚未验收，未发布。

## 实际行为

MCP 只增加两个固定 external/preserve_output 工具。显式列出模板与完整参数后，
通过当前目录已展示引用取回完整文本；服务端 user/assistant 角色仅是工具资料。
资源与模板复用有限目录、字节预算、宿主分页、调用准入与租约，分别保留缓存。
缺省参数和空对象原样传给 SDK，不补默认值；参数名不能安全展示、混合媒体、
超限正文或上下文无法容纳完整结果均明确失败。旧代已接纳 get 可按原连接与策略
收尾，迟到目录不能发布新引用，纯 Skills 更新继续保留当前 MCP 连接。

Windows 沿原 computer_action 操作当前快照保存的普通按钮对象。私有引用绑定
所选窗口、PID 与进程启动身份，原对象父链再次验证；IsPassword 在 Name/模式
之前检查。宿主在首个 await 前消费快照，并在实际写入前复核执行与 Runtime 门。
原生 Invoke 进入后异常或回执不明保留 unknown；accepted 只表示调用返回。
没有 Value、坐标、键盘或寻找替代控件的回退。

## 作者与独立审查

Windows 作者签名 `f1de77972a8f1232b92967f8b2cc8d59f29c4260` 整合为 `1cab913`。
两条旧码反例分别证明普通按钮能力被拒绝、unknown 被错误转换为 not_started；
修后 193 项唯一回归通过（40 新增、153 既有），4 项原生检查在 Linux 按原门跳过。
九份 Python Ruff/格式与两源码严格 mypy 通过，最高 213.4 MiB、swap 0。
根及 MCP 作者分别核对固定实现；没有剩余确定 P1/P2。

MCP 作者签名 `36b2c8b6f144a34b663972599beb8660b7c6a733` 整合为 `69935e4`。
旧码 prompts-only 没有入口的反例失败；修后 20 文件 402 项唯一回归通过，
包含 58 项新增、原资源 53 项未改断言及其它 291 项既有回归。
仅精确排除 `test_real_fastmcp_in_process_client_lifecycle`，无 skip。
七份 Python Ruff/格式与四源码严格 mypy 通过，最高 242.5 MiB、swap 0。
根与 Windows 作者分别核对参数、完整目录、代次、脱敏和实际三种模型编码；
最终独立审查没有剩余确定 P1/P2。

## 根整合验证

四组十九文件合计 398 项唯一回归通过，不与作者计数相加：

- UIA Invoke、宿主和两个纯窗口夹具文件：92 项，0.40 秒，74.2 MiB。
- 共享 Computer 配置、定位、Runtime 与集成四文件：77 项，0.31 秒，72 MiB。
- MCP 模板与资源六文件：111 项，3.36 秒，121.3 MiB。
- MCP 工具、两种换代、Extensions、Skills 五文件：118 项，2.59 秒，174.2 MiB；仅精确排除上述一个内存 server 用例。

完整 Ruff、348 文件格式检查通过；以独立临时缓存执行非增量严格 mypy，
142 源文件通过，7.865 秒、421 MiB。准备元数据时另有 166 项兼容/发行回归
及 83 包离线锁检查通过，未因无关代码修改重复运行。新增三个模板测试文件已
加入原 MCP extras 作业，普通无 extras 矩阵继续收集纯值与 Runtime 回归。

本地检查全部串行使用绝对路径共享守卫，每次 30 秒、单次 1 GiB、共享 2 GiB、
单 CPU、swap 0；未安装依赖、启动实际 MCP/stdio、运行本机原生服务或操作桌面。
锁定 SDK 只通过流式 MockTransport；没有真实远端 MCP 或模型额度验收。

新 Windows 用例只在 hosted CI 的自有标准 Win32 BUTTON 上，分别验证 accepted
与匹配父 HWND/控制 ID/按钮 HWND 的一次 BN_CLICKED，沿原 12 秒和 4 KiB 预算。
Linux 纯替身不能证明 PowerShell 语法、实际 provider 属性变化或 Windows 效果；
密码 Name getter 的动态访问、自定义 COM provider、跨权限窗口也未据此验收。
2.36 原 probe 的间歇超时仍待独立阶段证据，不把其第二次成功当作根因已修复。

设计与边界见 [MCP 模板](mcp-prompts-design.md)、[Windows 操作](windows-uia-actions-design.md)。
