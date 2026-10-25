# 队友执行中的阶段成果与澄清

状态：v2.52.0 的单模块生产候选已冻结；两条正式旅程与五项独立边界负控共七个不同新增节点已由 root 实测通过，发布元数据已更新。基线为 v2.51 精确提交 `c849a1f3afa738b8d236c05425bc84cce7ab2716`；v2.51 正式发行已经 root 核对，见[发行记录](review-v2.51.md)。本片本地验证包含执行中协调与正常关闭重开后的同队友续作，远端CI及正式发行仍未完成。

主人在手机7交代资料任务。队友真实读取资料后，用既有 `send_input` 向自己的固定直接parent主会话交阶段依据或问一个澄清问题。主助手仍只负责协调，沿这个队友派生时冻结的 `ReplyOrigin` 将问题发回手机7，Web保留同一记录；即使手机8正在与主助手另谈事情，也不能将7的资料和问题错发给8。主人之后明确回答，主助手用原 `send_input` 将补充交回同一个持久队友，不重建身份或重做结果未知的操作。

## 已有接缝与缺口

- `team.py::send` 首先 `get(target)`，只认agent；真实main会话不是agent，所以child给自己的main parent发送也准确失败。`tools(caller, mode)` 已闭包捕获真实调用会话，模型schema没有sender；不能因此开放任意session投递。
- 原 `Conversation.enqueue`/Store ingress已有持久pending、原容量、event_id精确查重、附件引用、消息配对与安全边界消费。`Runtime._wake_main` 只在主会话无active task且有pending时启动，不打断模型流或工具。
- `Runtime._prepare_request` 将原 `source="team:"+id`、`principal="agent:"+id` 当当前主请求的补充事实，续轮保留原请求来源。这是最终报告的既有合同；仅给新消息填 `reply_origin` 不足以阻止phone8续轮夺路由。
- 队友结束已有真实 `team_report`；正常主run结束不取消子队友。无需新增报告类型、队列、工具、schema、scheduler或数据库格式。

## 最小接纳与来源合同

实现只在Team模块给原 `send_input` 闭包增加准确main目标分派，转入私有 `_progress` handler；普通目标仍调用原 `self.send`，`send_message` 保留原closure，不在public `Team.send` 默认放开main。新分支只认准确main ID及child持久记录中相等的直接 `parent_id`，不按名称、最近会话或模型声明选择。兄弟、其它main、孙代向祖main、任意session、无实际caller的legacy调用都不能借此进入main。主助手向原队友发送仍走原路径。现Runtime已经将非普通报告来源作为真实续轮触发；第一阶段已验证仅Team模块足够，Runtime未改。

闭包caller只提供定位，不能单独构成授权。借用 `task_file_publication` 已有真实执行模式，在最后同步段核实际tool_session与 `_tool_execution`、当前run_ids、PreparedRequest、准确running agent的run/generation和固定parent；同时核Team/Runtime未关闭、maintenance admission、父链未stop及任务scope/epoch。审批后到进入可信handler仍沿Runtime原末门，事务前不得再插await。缺失、代次变化、暂停、stop或关闭均无新ingress、无wake、无部分写入；不从当前模型上下文推断身份、任务或路由。

原模式与工具 `risk="write"` 保持：ask仍请求主人批准，auto走现reviewer与人工兜底，yolo只是本测试固定策略。队内内容始终是待核实资料，不是owner输入或新的执行授权；澄清不自动resume、approve或越过task epoch。仅已批准任务scope下正常执行的child可上报，暂停/stop竞态必须在最终接纳前受控拒绝。

新分支通过原enqueue一次保存内容、准确child principal及其冻结 `ReplyOrigin`，使用仅此可信分支可生成的阶段来源 `team-progress:<child_id>`。当前main在真实安全边界claim后按该来源切换本次阶段回复目的地；原最终 `team:` 报告继续作为补充事实，不泛化改其续轮路由。并发新owner输入仍按实际持久输入顺序处理，不能将全局最近owner或永久sticky来源当路由。发送时仍重新核当前owner/channel授权，blocked/unknown与原渠道合同保持，unknown不自动重发。

复用原附件ID数量/存在性验证和原pending上限；满邮箱、坏附件、权限失败在任何持久效果前拒绝。`wake=false` 只不主动启动main，不能阻拦已运行main在既有安全边界消费、其它真实唤醒或正常启动恢复。已有宿主 `event_id` 继续按准确来源、参数和附件查重，重复已有receipt不另wake、不补改route；模型不新增event_id参数，不按正文去重，不承诺不同工具call的exactly-once。实际工具结果丢失时先核真实状态，不能擅自重做未知副作用。

## 两个明确阶段

第一阶段的正式红测为 `tests/test_team_progress_runtime.py`：真实ASGI/OneBot、Runtime/Team、锁定SDK与流式MockTransport；手机7派生直接队友，队友先真实列目录并读取合成资料。手机8的独立主run确实处于模型流中时放行child原 `send_input`。有限provider Event使child此时仍为running，尚未产生最终报告。外层先检查accepted完整工具回执与真实资料前置，再要求parent发送成功；先红准确得到“队友不存在或名称不唯一，请使用 agent_id”，不以缺工具、导入、超时或伪造报告作为目标红。

成功分支将放行phone8当前响应，使主助手在原安全边界实际消费阶段消息；核实际请求有准确child和真实资料、阶段说明只向手机7出站一次且8无该资料，Web可见。先核child仍running、run未结束、无该代team_report，再释放Event让它自然结束；后来的最终报告单独核，不能用永远总outbox等于1掩盖合理终态回复。每段有界，不跨真实人类等待保留一个活run。

第二阶段在child自然结束且主报告收尾后正常close/reopen原Store，再由手机7明确回答，主助手原 `send_input` 唤醒同agent，核新generation真实读原历史/资料与补充，最后沿其冻结原来源汇报。复用现terminal/pending/显式唤醒合同；中断执行不自动重放，关闭不手造报告或改状态。首轮收尾说明的一次真实渠道请求明确超时形成unknown，重开和后续回答均不得重发；这只验证已有渠道投递的未知结果，不等于任意外部副作用恢复。它是独立后续节点，第一阶段通过不作为其验收证据。

独立第二节点用实际 `fs_read` 读取临时UTF-8草稿，原工具结果自然留在持久历史；重开后再次显式读取同一文件，并从实际补充消息核主人回答。复用默认应用/插件工厂关闭流程，不替换flush或finalizer，不写历史、report、批准或receipt。首轮主请求为2/1/1次、队友3次，重开后主请求2/1次、队友2次，仍沿原请求与等待预算；它不重复首节点的SDK和phone8竞争矩阵。

首测沿既有16000上下文字符、6000工具输出字符、3个pending run和MCP timeout2秒，单次请求历史最多32条。第一阶段主助手每个run至多2次请求，队友至多4次请求、3个真实工具调用；有限门控沿原3/4秒等待，不为通过增加期限、轮次或容量。测试替身只选择固定工具，原文只来自真实SDK资源read的MockTransport；不证明自然模型质量、auto自动批准、真实账号或外部服务送达。

## 实际验证记录

方案经既定独立reviewer前审后，root先取得准确功能红：真实SDK读取、完整accepted工具结果、两端仍running且无报告等前置均过，`send_input`的主会话目标被拒绝，1 failed / pytest 0.90秒 / 守卫1.713秒 / 115.1 MiB / swap 0。

单模块候选接线后，核心路由与中途上报已过；一次夹具误把含换行的原文与HTTP JSON编码体直接比较，200响应中的实际content完整但转义不同。仅改为逐条比较 `history.json()['messages']` 的准确content后，原selector实收1 passed / pytest 0.87秒 / 守卫1.693秒 / 115.3 MiB / swap 0。phone8在途同run续轮、手机7一次出站、8无该资料、worker仍running且无报告、随后真实终态报告等oracle均保持。

独立边界文件最初五项都停在父会话auto模式的批准前置，并非阶段接纳失败；仅通过真实认证PATCH使已存在main采用yolo后，五项整体实收5 passed / pytest 0.84秒 / 守卫1.717秒 / 106.5 MiB / swap 0。固定yolo不证明auto reviewer质量。原16个Team/任务/渠道相关文件首轮380 passed、1 failed；唯一旧断言要求两个工具handler对象相同，与新专属分支合同冲突，仅改为两参数schema相同、risk均为write后，该节点单独通过。因此381个旧节点逐项绿，不写成一次381全绿。

第二阶段首轮1 failed / pytest 4.62秒 / 守卫5.298秒 / 94 MiB，停在确认回复之前，固定模型仅进入主助手第一轮。源码确认主助手原合同允许read工具，夹具却误断言其工具目录不能包含 `fs_read`；移除此未授权假设，改由真实持久调用表证明主助手只调用spawn与send_input。首轮即记录真实run ID，并只输出状态、错误类型和一个宿主故障frame，诊断仍限1 KiB，不输出异常正文或改变原等待期限。这次是夹具前置失败，不作为重开功能红。

该修正后的第二轮实收1 failed / pytest 4.71秒 / 守卫5.476秒 / 98.4 MiB，已经过正常close/reopen、同ID代2执行、真实fs_read及旧report保持，停在最终主回复等待。源码发现夹具在构造最终正文前读取了 `team_report` 并不存在的generation字段；这会抛KeyError，但本轮未读实际数据库确认异常类型，故此处只记录源码定位。代次和完成修订实际在同事务的 `team.finished` 事件中，现改为按原有界完成索引核准确run_id/agent_id/generation/completion_revision，并绑定报告真实字段；没有新增报告字段或降低代次要求。诊断按当前主运行优先、再当前队友取两份有限状态，避免已完成队友挤掉失败主运行，仍不改1 KiB上限和原等待预算。

第二阶段修正后实收1 passed / pytest 0.64秒 / 守卫1.341秒 / 94.3 MiB / swap 0，同一selector的全部oracle保持：正常关闭重开、原主身份和同队友ID、代2实际读取、准确完成事件及报告、手机7唯一最终投递、Web可见、旧报告与unknown投递均不改写或重发。两条旅程及五项独立边界负控是七个不同节点，重跑不重复累计；固定工具策略不代表自然模型质量或真实账号送达。

root另将MCP专用CI作业改为自动发现 `tests/test_mcp*.py`、`tests/test_team_progress*.py`，保留渠道测试；新增资源链接与阶段协作文件将进入安装可选SDK的作业。本地全部MCP模式318项已通过，源码150文件strict及发布/兼容166项亦通过；相关组存在重叠，不在这里相加。v2.51发行时旧显式文件表没有新增SDK两文件，本次改发现规则不能倒推旧发行已远端运行这些节点，细节见[开发验证](development.md)。

本片只有 `src/muli/team.py` 生产改动；作者未运行检查、格式化、Git、网络或真实服务，root独占验证槽。随后Ruff/格式及离线包检查、签名候选CI十项、正式发行十三项与GitHub/PyPI两构件大小和SHA核对均已闭合，精确记录见[发行审查](review-v2.52.md#发行闭合)。这不改变真实平台与自然模型效果未验的边界。
