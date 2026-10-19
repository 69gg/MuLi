# PyPI 与版本发布

发行名是 **muli-agent**；Python 模块仍是 `muli`，命令同时提供 `MuLi` 和 `muli`。`MuLi` 发行名被 PyPI 的相似名称检查拒绝，因此安装使用 `uv tool install muli-agent`。

PyPI Trusted Publisher 已由维护者登记：项目 `muli-agent`、GitHub owner `69gg`、repository `MuLi`、workflow filename `release.yml`、environment `pypi`。发布工作流只在发布 job 获取短时 OIDC 凭据，不保存 PyPI API token。Pending Publisher 会在第一次成功发布时创建项目；登记不等于已经发布。

发布前同步 pyproject.toml、src/muli/__init__.py、uv.lock 和 CHANGELOG.md；完成独立审查及回归，创建 GPG 签名提交和签名的 `vX.Y.Z` 标签。推送标签后，release.yml 调用完整跨平台 CI，下载该次质量作业实际测试的同一组 wheel/sdist，验证名称/版本/变更记录，再发布 PyPI 和 GitHub Release。不能通过手工在 main 分支点运行绕过标签校验；失败重试需选择原标签。

验证以远端工作流结论、PyPI 文件及 GitHub Release 实际状态为准。`uv tool install muli-agent` 的推荐方式在首次发布成功后才具备可安装性；开发期间可用 `uv tool install .` 验证本地安装。

参考：[PyPI Pending Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)、[Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)。
