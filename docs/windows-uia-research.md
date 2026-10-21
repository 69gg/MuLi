# Windows UI Automation 后续切片调研

状态：调研，未实现、未执行桌面或 Windows API。Windows Skills 的 NTFS 适配
不等于 Windows 电脑操作；现有产品仍只提供 Linux AT-SPI、实验性 macOS AX
以及独立截图/录屏。优先完成在线候选和当前平台回归，再决定此片实现。

## 已有接缝

`tools/computer.py` 保持统一 status / targets / snapshot / action / type 工具、
权限门和结构化 outcome；`ax_computer.py` 与 `ax_lease.py` 已有调用者绑定、
短期原生对象、绝对期限及回收协议。Windows 可复用产品合同，不能把针对
macOS 的具体 helper 或 POSIX 进程假设原样复制。主配置格式 1 仍由旧固定
恢复控制器读取，新后端的选择必须先核对旧配置兼容性。

## 已核实的接口约束

- 桌面查找不能使用整棵树的 `FindAll(Descendants)`；微软明确指出会遍历大量
  元素甚至栈溢出。候选设计应先有界列顶层窗口，再逐节点遍历选定窗口，
  节点、深度、子项和返回字节分别限额，不完整结果保留省略原因。
  [FindAll](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-findall)。
- UIA 对象的 RuntimeId 可随时间复用，格式不应解释为永久路径。不能持久
  存储一个 ID 后重新搜到相同值就动作；后续若开放动作，需要同一短期原生
  对象会话、调用者与目标身份，以及不可自动重试的 unknown 结果。
  [GetRuntimeId](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-getruntimeid)。
- 微软建议桌面 UIA 客户端在无窗口的独立 MTA 线程使用 API；跨线程的 COM
  对象也受原 apartment 生命周期约束。MuLi 异步线程池中的一次普通调用不能
  直接视作可取消的原生操作，隔离 helper 的超时与完整退出仍须单独证明。
  [线程模型](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)。
- 系统 .NET 的 `UIAutomationClient.dll` 已提供 AutomationElement；原生 COM
  也有同类接口。先比较已安装 Windows PowerShell/.NET 的薄桥与小型原生
  COM 适配，不新增通用 FFI 框架，也不安装 Pythonnet、SDK 或大型运行时。
  DLL 文档只证明 API 存在，不证明每台用户电脑都具备可运行桥接环境。
  [托管接口](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automationelement.getruntimeid?view=netframework-4.8.1)。

## 实施前需收敛的范围

首片建议提供可用性、顶层目标和选定窗口的只读树，继续沿统一 computer
权限、输出与资源预算。动作与输入在原生对象身份和进程回收验证前明确
不可用，不先用坐标或 RuntimeId 重新搜索兜底。树中页面文字只是外部资料。

桥接选择须由实际受限 Windows CI 证明：无提示启动、UTF-8/Unicode JSON、
阻塞调用超时、取消后进程退出、关闭/重载失效和完整输出预算。真实窗口
测试仅针对 CI 自建临时应用，不触碰本机桌面或外部账户。桌面会话缺失、
隔离桌面、权限不足或依赖缺失要给固定不可用原因；不把 mock 通过或
headless runner 的 skip 写成用户桌面验收。

本轮 Context7 先检索了 Win32 API 资料，再核对微软原始页面；尚未决定桥接
实现，不声称系统 PowerShell、原生 COM 或跨位数提供者已经测试成功。
