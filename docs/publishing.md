# PyPI 与版本发布

发行名是 **muli-agent**；Python 模块仍是 `muli`，命令同时提供 `MuLi` 和 `muli`。`MuLi` 发行名被 PyPI 的相似名称检查拒绝，因此安装使用 `uv tool install muli-agent`。

PyPI Trusted Publisher 已由维护者登记：项目 `muli-agent`、GitHub owner `69gg`、repository `MuLi`、workflow filename `release.yml`、environment `pypi`。发布工作流只在发布 job 获取短时 OIDC 凭据，不保存 PyPI API token。首次发布 [2.0.0](https://pypi.org/project/muli-agent/2.0.0/) 已于 2026-10-20（北京时间）成功创建项目，安装命令已可用。

发布前同步 pyproject.toml、src/muli/__init__.py、uv.lock 和 CHANGELOG.md；完成独立审查及回归，创建 GPG 签名提交和签名的 `vX.Y.Z` 标签。推送标签后，release.yml 调用完整跨平台 CI，下载该次质量作业实际测试的同一组 wheel/sdist，验证名称/版本/变更记录，再发布 PyPI 和 GitHub Release。不能通过手工在 main 分支点运行绕过标签校验；失败重试需选择原标签。

验证以远端工作流结论、PyPI 文件及 GitHub Release 实际状态为准。2.0.0 的 [发布流水线](https://github.com/69gg/MuLi/actions/runs/39288541118) 11 个作业全部通过；PyPI 与 [GitHub Release](https://github.com/69gg/MuLi/releases/tag/v2.0.0) 的 wheel 和源码包 SHA-256 分别一致。签名标签指向 `80a40a91fea6351ddc0a48f657671f27b4857f00`。本地开发也可用 `uv tool install .` 验证安装，不能把本地安装成功当作远端发布成功。

参考：[PyPI Pending Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)、[Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)。
