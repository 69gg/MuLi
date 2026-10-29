# 冻结的 2.112 格式读取器

`format_admission_v2_112.py` 和 `compatibility_v2_112.json` 分别保留 MuLi
2.112.0 的完整格式准入模块和随包声明。来源是本地
`shell-receipt-recovery` 基线 worktree 的 `src/muli/format_admission.py` 和
`src/muli/compatibility.json`（基线提交 `cce9`）；复制时该包声明为
`release=2.112.0`。原文件 SHA-256：

- `format_admission.py`：`34983abfaef33790d6517805a5b11edf6afb8f7a1d56ab282c12199a1f53618d`
- `compatibility.json`：`74a875e76c16e35e59e110fb45e4eccfcae8f9cfe905ea89fd170df3375eb6a7`

回归使用当前未变的格式模型、证据读取器和临时数据库，执行冻结旧模块的
`require_current_format`，核对旧精确状态能读、新状态被拒。它证明冻结旧 reader
代码的准入行为，不代表已构建、安装或运行真实 2.112 发行包，也不认证任意旧版。
后续更新当前格式时不要跟着改动这两份历史夹具。
