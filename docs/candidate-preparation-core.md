# 离线候选准备核心

本片实现候选策略、有限本地材料冻结、最终目录、参数和环境构造，以及安装后证据校验。
核心不执行进程、不审批、不修改主配置、selector或事务账本。管理入口和内核执行后端
分别集成；本片独立测试不代表已经在真实安装上完成更新。

`CandidateSettings` 保持独立。2.22固定controller使用严格Settings解析配置，因此
不能向主配置格式1增加`updates.candidate`。宿主通过单独的Store策略对象提供设置；
显式管理可查看/保存完整wheelhouse、retained_root、uv_executable，有限status只给
`ExpectedPolicy(source,limits,disk_mode,revision)`。默认disabled，不扫描任意默认目录。

## 宿主接入

1. 使用`ManagedPreparation.observe_current()`的真实current slot、binding_revision、
   verify回调，加`read_managed_install_receipt()`和当前record.request_id对应计划的
   上次`CandidateEvidence`，构造`CandidateSource`。不拿固定controller receipt，
   不构造伪V1 manifest，不遍历历史祖先。`protected_roots`列当前/controller/需保留安装。
   宿主对每个managed计划保留`source_candidate_evidence`；update成功的target取本次
   `candidate_evidence`，rollback/restart/configure取source附件。core仍逐项核当前slot、
   receipt指纹和部署意图。外部switch没有附件时只能从该实际slot receipt建立新基线；
   update目标缺少本次证据时拒绝，不能用controller或祖先计划补齐。
2. `inspect_policy(settings,source)`返回`FrozenPolicy`。inspect不启动uv；receipt的
   0.13.0/0.13.1选择只是解析合同，实际producer随后受限检查。单一preflight截止覆盖
   整个wheelhouse扫描，不按文件续期。revision含全部实际策略、current binding、slot、
   receipt、原意图、wheel文件身份/哈希和uv文件指纹。
3. 真实审批展示`self_update(version,expected_policy)`原参数。expected仅If-Match，
   不授模型选择目录/材料/更大预算。宿主的`CandidateSource.verify`必须组合既有
   current观察和重新读取固定策略对象并精确比较冻结设置；只重用旧settings无法看见
   后续策略编辑。不存在通用审批hook或模型自报source_ref。
4. `create_attempt(...,request_id,version,configuration=实际配置bytes)`只创建最终
   `retained_root/candidate-ID`。已存在即拒，不自动采用、force、移动venv或删除重试。
   创建失败后宿主仍知道确定路径；错误/取消/未知清理时保持该归属。
5. 依次把`uv_version_spec()`、`install_spec()`、`dependency_spec()`、`probe_spec()`
   交`run_candidate_process`，逐次调用对应accept，最后`finish()`得到`ReadyCandidate`。
   backend确认进程树已空才能返回`ProcessOutput`。安装spec单次发出，失败不重发。
6. 回配置锁后调用`attempt.verify_ready(ready)`，随后原计划仍以canonical switch
   交`RecoveryHost.prepare(candidate=ready.paths)`。只原V2 resolved+精确Store投影才终态。

所有操作共用一个绝对单调截止，默认180秒；版本/依赖/schema预检另受30秒子上限，
不会延长总期限。host须强拥有实际准备Task及backend的清理，不能把stage字符串当排空证据。

## 材料、意图和验证

只消费显式本地wheelhouse。每个源为不可跟随链接的普通wheel，所有权为本用户/root，
不能group/world可写；无sdist、VCS、直接URL、构建或联网补齐。压缩字节、wheel数、
ZIP中央目录和声明解压总量/成员数均有限，在ZipFile展开中央目录前先核EOCD预算。
声明大小不是实际硬磁盘额度。材料流式哈希/复制到attempt，前后文件身份变化拒绝。

要求通过PyPA `Requirement`、`SpecifierSet`、`Marker`解析验证，不自行实现PEP解析、
求交或评估markers。原main/extras/with/constraints/overrides不丢；额外加入目标pin，
冲突由官方uv拒绝，不删除约束重试。已知自定义index或不同find-links不被静默改为PyPI。
明确批准的本次offline/no-config策略不声称复原历史shell/config。

子环境从有限键构造：私有tool/bin/cache/tmp/python/credentials目录、现存空NETRC，
keyring禁用、copy link模式；不继承UV/PIP/代理/Python覆盖键，不改HOME/CODEX_HOME。
uv固定offline/no-index/find-links/no-build/no-python-downloads/no-config等参数。
只有受限执行实际`uv --version`为0.13.0/0.13.1后才生成安装spec；这不是uv二进制认证。

依赖检查使用官方`uv pip check --python`。候选Python以`-I -B`检查真实词法入口、
prefix、导入包、distribution/module版本和实际Settings schema，随后仅在attempt的
独立scratch用默认ConfigFile构造Runtime，读取新Store格式/主会话，并在finally调用
`discard_unstarted`。不调用startup/activate_components，不打开主人数据，不加载
主人插件/MCP/模型；scratch占用也计候选阈值。这不证明真实用户库准入或完整启动成功。
`.pth/sitecustomize`仍可能运行，因此
所有Python探测也必须走受限backend，no-build不是恶意代码沙箱。

最后静态register_local_slot并核候选静态声明、真实receipt及其main/with/constraints/
overrides/python和本次部署选项；私有证据fsync后ready。保存original_intent与本次pin/
receipt选项两个层面。下一次仅从当前计划的一份自包含证据重验current slot/receipt后
恢复原意图，不把上次pin或attempt find-links变成永久主人要求。缺链/外部重装拒绝。
`CandidateEvidence.model_dump()`仍含私有路径、原要求和receipt；repr隐藏不等于公开
脱敏。私有计划可保存有界自包含证据，证据原文也保存在attempt私有文件；公开事件只能
给宿主选择的安全状态，不能直接输出证据model_dump。

默认threshold模式无quota可运行：周期扫描只报告owned tree字节/剩余空间停止阈值，
可有超调；会忽略uv自有临时文件并发消失，但不跟随目录链接。显式strict由当前后端
固定unsupported。内存/CPU/tasks/swap仍必须有真实内核硬限，backend缺失不能裸跑。
核心不做GC：宿主硬退出后transient worker仍可能存活到冻结期限，仅plan interrupted
或目录年龄不能授权删除。正常返回或cleanup_confirmed后才可按宿主原有清理规则处理，
未知收尾保留attempt，本片不新增自动启动回收。

API依据：[PyPA Requirement](https://packaging.pypa.io/en/stable/requirements.html)、
[SpecifierSet](https://packaging.pypa.io/en/stable/specifiers.html)、
[固定uv工具参数](https://github.com/astral-sh/uv/blob/0.13.0/crates/uv-cli/src/lib.rs#L5037)、
[receipt要求顺序](https://github.com/astral-sh/uv/blob/0.13.0/crates/uv-tool/src/tool.rs#L15)。
版本/资源/取消真实执行验收由集成片记录；本片不下载包、不进行真实uv安装。

## 本片验证

通过根`check_local.py --seconds 30`串行执行：核心与共享types共48项通过（7.84秒），
2源码mypy通过，4文件Ruff/format通过；最高148.4MiB、swap0。内嵌脚本调整排版后，
两个真实fresh Runtime构造/关闭用例另行通过（0.18秒）。所有本片用例禁止Popen与
asyncio subprocess入口；安装/uv版本/依赖阶段只提供受控返回值，未运行真实uv。
首轮反例暴露最后一个wheel扫描后缺总deadline复核，已补返回前检查；fixture的
tuple TOML序列化已改用JSON模式。没有提高检查限额或重置每文件扫描期限。
