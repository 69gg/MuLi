# 2.30 Windows Skills 原生文件读取审查

已正式发布。精确修复候选与 main 各八项 CI、发行十一项全部成功，签名
标签和 PyPI/GitHub 构件元数据一致。本机 Linux 的通过结果不替代真实 Windows 行为。
上一版 2.29 已按原签名提交发布，main 与发行作业全部成功，证据独立记录。

## 合同与审查

签名实现 `9691ea88105ee3609403fcb6519a58b5c8cc11c4` 整合为 `c8727ac`。
首片只支持普通绝对盘符路径上的本地 NTFS，明确拒绝 UNC、设备/ADS 路径、
SUBST 子目录、重解析点和其它文件系统。Linux/macOS 原有读取逻辑保持。

Windows 路径不先解析链接。只在首次打开卷根时使用完整路径，并在同一
句柄核对 NTFS、磁盘类型和精确卷 GUID 根；之后的扫描、文件读取和末次
核对均相对于已打开目录，使用单组件 native open 与不跟随重解析点标志。
身份来自原生卷/文件 ID 与属性、大小、时间，不跨 API 比较 Python ctime。
枚举记录、页数、UTF-16、读取字节和文件数均有界，异常同步释放句柄。

Windows reviewer 和根审查分别读过原生读取层、Skills 接线及真实 junction
竞争门控，未发现剩余 P1/P2。现有 POSIX 竞争用例保持；Windows 新用例
要求真实原生 API 和 junction 构造成功，不把缺能力静默 skip。已打开父目录
被原位设置重解析点时，要么拒绝且不读外部文件，要么内核明确拒绝置换且
仍读旧内容。后续托管 Windows 3.12/3.13 的实际结果记录于文末。

## 本地验证

作者五文件不重复计数 166 项通过、6 项 Windows 原生用例平台跳过；四份
Python Ruff/格式与两份源码严格类型通过。作者峰值及首轮类型修正见
[设计记录](windows-skills-design.md)，不将本地 FFI 模拟称为 Windows 实跑。

根分支重新验证上述五文件：166 项通过、6 项跳过（2.85 秒、145.4 MiB）；
压缩、压缩恢复、Runtime、兼容和发行五个不同文件另 210 项通过
（1.99 秒、98.9 MiB），合计十文件 376 项，不累计作者重复结果。

全仓 Ruff、315 文件格式、133 源码 `mypy --no-incremental` 通过，类型检查
峰值 397.4 MiB。全部使用原单次 1 GiB、共享 2 GiB、单 CPU、30 秒守卫，
swap 0，没有安装依赖、接入真实用户服务或操作真实桌面。版本、锁和兼容
声明同步为 2.30.0，随后兼容/发行 166 项重新通过（0.99 秒、69 MiB），
83 包离线锁核验通过；这些重复项不另累计。候选 CI、安装包和平台完整
矩阵尚待执行。

## 冻结候选

精确 GPG 签名 `646f4279b3c4216be2cadf9dab81478066bc5dd6` 已提交
[PR34](https://github.com/69gg/MuLi/pull/34)，
[CI 39548142576](https://github.com/69gg/MuLi/actions/runs/39548142576) 首轮失败。
后续文档和在线候选实现另开分支，真实 Windows 验收必须对应这个候选
或明确记录的修复提交，不能借用上一版通过结果。

## 首轮真实 Windows 反馈

首轮 attempt 1 六项成功、Windows 3.12/3.13 两项失败，精确 job 分别为
`119050904114` 与 `119050904224`。两组均 3080 项通过、1320 项平台跳过、
2 项 failed 与 2 项 setup/teardown error；不能称为完整矩阵成功。

有限精确日志确认三个测试夹具问题：既有分页用例以 `write_text` 默认换行
写入，Windows 实际 CRLF 与期望 LF 不一致；真实拒写已抛
`PermissionError(errno=13)`，但 CRT 路径的 `winerror=None` 不符合只认 32
的断言；16382 个 emoji 的长路径边界被 pytest 自动用作参数 ID，写入
`PYTEST_CURRENT_TEST` 时超过 Windows 32767 字符限制，产生前后两个 error。

修复保留真实字节语义、拒写及内容不变检查，以及相同长路径边界输入；
只纠正夹具的换行、异常映射和测试标识，不改产品期限、不跳过验收或
重跑旧候选掩盖失败。修复与下一次精确候选结果待记录。

签名修复 `89d9a17696f8e93d49da6d2398018d726ff2ea3f` 整合为 `7c36679`，
仅修改两份测试及设计记录；分页现在分别验证实际 LF / CRLF 字节，拒写与
重命名分别核对 CRT / Win32 错误并确认旧内容完整、关闭后可正常编辑。
作者两文件 89 项通过、6 项原生 Windows 跳过（0.57 秒、79.1 MiB）；
根分支同组重新通过（0.58 秒、81.5 MiB），两文件 Ruff/格式通过。
均沿原 30 秒守卫、swap 0，未改产品或扩大期限。远端需运行新提交，
旧候选的六绿两失败结果不会被覆盖成成功。

## 第二轮 Windows 尾差

更新后的精确候选 `dc9c910c61515b112ccef2a46bf105ab979096c6` 进入
[CI 39549402229](https://github.com/69gg/MuLi/actions/runs/39549402229)。
Windows 3.13 job `119054901185` 实际完成 3083 passed、1320 skipped、
1 failed；旧换行和环境变量长度错误已消除。唯一失败为同一叶文件门控的
目录重命名：内核确实拒绝，但 `PermissionError(errno=13)` 携带原生
`winerror=5`，而断言仍只接受 32。Windows 3.12 job `119054901119`
也为同一项失败，3083 passed、1320 skipped。该轮最终六项成功、两项失败，
保留原 attempt 1，不覆盖或重跑成成功。

后续窄修将目录拒绝验证为 PermissionError/EACCES 与 5 或 32，保留持句柄
读取到完整旧正文、关闭后可写及可重命名的实际行为检查。此 Linux 无法
运行的原生尾差不重复执行本机跳过组冒充验证，只做必要静态检查后提交
新 Windows 候选，产品源码与各项资源、时间上限保持不变。

## 修复候选通过与发行启动

第二次夹具窄修 `3cecc8d2f23d1a06188dab1f507c7f0f3dc756c1` 整合为
`2fb9923`，仅修正目录拒绝错误码断言并同步设计说明。最终精确 GPG 提交
`b95968c3037a735ab8973686bddef0f082e47d49` 的
[CI 39550178928](https://github.com/69gg/MuLi/actions/runs/39550178928)
attempt 1 八项全部成功：两 Windows、两 Linux、两 macOS、MCP 和质量检查。
Windows 3.13 / 3.12 job 分别为 `119057340455` / `119057340588`，
不借用前两轮失败候选的任何成功项。

根审查独立核对精确 head、八项结果和 GPG 签名后，非强制原子推送 main
及签名标签 `v2.30.0`。PR34 于 `2026-10-21T05:41:26Z` 按原提交合入；
标签对象为 `40c2165a83fda38e413d8ee5e5fbaccc217b93c2`，指向同一提交。
随后同一 head 的 [main CI 39550976316](https://github.com/69gg/MuLi/actions/runs/39550976316)
attempt 1 八项全部成功，
[Release 39550976895](https://github.com/69gg/MuLi/actions/runs/39550976895)
attempt 1 十一项全部成功。根审查独立核对两个运行的 head 与全部结果。

[GitHub Release](https://github.com/69gg/MuLi/releases/tag/v2.30.0) ID
`419201674` 于 `2026-10-21T05:49:01Z` 发布，非 draft / prerelease；
远端 main、标签目标均为上述提交，commit/tag 签名验证均 valid。
PyPI 与 GitHub 两构件名称、大小、SHA256 完全一致，PyPI 均未撤回：

| 构件 | 字节 | SHA256 |
| --- | ---: | --- |
| `muli_agent-2.30.0-py3-none-any.whl` | 739247 | `53ffb5e18a1bed56d25e4b1a8144bac303e9cfdaebcec194f983d176deb843b0` |
| `muli_agent-2.30.0.tar.gz` | 3783728 | `379e68bd186c384a343679c8c70f84a63f5b0833ae9102e61de0329bbbbd7163` |

只读取发布元数据，没有下载发行包。前两轮 Windows 失败保留；后续在线
候选与 UIA 实现继续在独立分支推进，不因发布完成停止迭代。
