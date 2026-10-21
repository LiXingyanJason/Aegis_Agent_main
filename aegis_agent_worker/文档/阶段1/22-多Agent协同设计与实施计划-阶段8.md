# 阶段8：多Agent协同设计与实施计划

> 2026-10-20：8.1～8.6后端最小实现及接口交接已完成，8.7和前端接入待实施。基线为阶段7.5；主计划见[结构化计划与工具结果状态传递开发计划](结构化计划与工具结果状态传递开发计划.md)。生产入口已可选装配，COOPERATION_ENABLED=true后重启启用，默认关闭；不恢复此前撤回的阶段8实现，不宣称真实服务端到端或前端已完成。第11～15节保留历史实施记录，当前事件协议和部署说明以第16节及根接口文档篇末为准。

## 1. 本次目标与不做的内容

以“请按续约邮件中的时间要求查询日历并安排会议草稿”为首个场景，建立真正有反馈的协作：协调Agent决定业务子目标，邮件和日历专家各自在领域内选择行动，返回结果后再决策。第一版仅串行执行一个活跃委派。

参考[AutoGen SelectorGroupChat](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/selector-group-chat.html)的角色描述、动态参与者选择和结果反馈概念，但不引入AutoGen SDK，也不复制其默认全员广播历史的方式。本项目选择显式协调者与选择性上下文共享，不等同于完全去中心化的Swarm。

不做：一次性生成完整DAG、固定步骤调度器、唯一执行路径检测、无限团队讨论、任意专家递归调用、开放联网、自动发送邮件或自主批准日历写入。路径明确的已有页面任务继续使用确定性Workflow，无需强行加入协调Agent。

复用LangGraph、PostgreSQL Checkpoint、Redis、MCP、Harness和既有模型客户端。参考[LangGraph中断机制](https://docs.langchain.com/oss/python/langgraph/interrupts)，但实现必须以项目已安装版本和现有interrupt/Command用法为准，不因最新文档直接升级依赖或假设API兼容。

## 2. 谁在决策

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| MainAgent | 根据已注册能力识别受支持的协同任务并交接 | 邮件查询策略、团队内部步骤 |
| CooperationAgent（新增） | 委派领域子目标，观察返回结果，决定继续、重新委派或结束 | 直接调用邮件/日历工具、批准外部写入 |
| MailAgent（扩展现有） | 找邮件、读相关对象、补问、整理要求 | 调用日历工具、猜测业务承诺 |
| CalendarAgent（扩展现有） | 查询空闲、解释结果、补问、推荐并生成站内草稿 | 将记忆视为可用性证明、自主创建正式日程 |
| Workflow / Service / Repository | 控制流、业务事实/事务、SQL，分别按现有分层承担 | 用Service替代Agent做语义决策 |
| ToolGateway / Harness | 参数、scope、审批、预算、超时和实际调用审计 | 相信模型自报权限或剩余额度 |

“邮件专家”“日历专家”是现有Agent加领域子图入口，不再另写同功能Agent套壳。主决策子图只知道已注册协同入口，不硬编码其内部邮件/日历步骤。

## 3. 一条示例轨迹与必要分支

正常轨迹：

1. MainAgent识别任务并进入团队工作流。
2. CooperationAgent委派邮件专家：“查明相关续约邮件的会议要求，保留来源与未解决问题。”
3. 邮件专家自行选择查询/读取，核实具体邮件、时间窗口和时长，返回结构化结果。
4. CooperationAgent观察结果，再委派日历专家：“在这些已核实要求内评估可用时间并起草会议。”
5. 日历专家自行查询，观察可信候选，选择满足条件的时段；代码校验并保存草稿。
6. 协调Agent取得真实草稿引用后结束团队生成阶段，呈现卡片并等待用户确认。
7. 用户确认后，确定性审批流程读取确认版本创建日程；模型不参与最终写入。

这不是强制调用顺序模板：

- 已有归属、版本有效的指定邮件引用：可直接读取，无须重复搜索。
- 多封续约邮件：邮件专家暂停，请用户选择，恢复后继续原委派。
- 时间表述或业务条件不明确：专家补问；不能默认第一封或擅自扩大日期范围。
- 日历无候选：日历专家解释冲突，询问是否调整条件；回答后再查询。协调Agent也可根据结果重新委派邮件专家核对要求。
- 来源不可访问、连接不可用、预算耗尽：返回真实失败/未完成状态，不伪造会议草稿。

不以“必须完成某几个证据步骤”规定团队路径。与此同时，调用某个工具仍要满足其参数、对象授权和业务前置条件；生成草稿仍须合法时间及实际可用性核验。放开编排不等于放开事实校验。

## 4. 契约与运行循环

### 4.1 建议契约（名称在8.1实施时冻结）

- `CooperationDecision`：`delegate / finish / finish_unavailable`等互斥动作；委派包含注册专家标识、业务子目标、必要约束与可信引用。不包含任意工具代码、完整DAG或模型可写的权限字段。
- `DelegationMessage`：程序分配委派ID、来源/目标、序号，载入已经校验的目标及上下文引用。模型提供的是请求，不是已经发生的交接事实。
- `ExpertResult`：实际委派标识、`completed / needs_clarification / unavailable / failed`等语义状态、安全摘要、结构化领域输出和真实Artifact引用。`needs_clarification`先由专家暂停处理，不代表自动开启另一个run。
- `TeamContext`：任务目标、明确约束、有界交接/结果摘要、未解决项和可复核引用。不保存完整原始邮件或模型思维过程。

注册表绑定专家描述、允许子任务、真实图入口、输入输出适配和风险上限；不能通过自然语言注册工具。领域工具仍由现有Gateway白名单管理，不让团队注册表替代授权。

### 4.2 循环

```text
装配可信任务与允许参与者
  → 协调Agent生成下一步结构化决策
  → 代码核验动作、目标、风险、预算
  → 调用所选专家子图
      → 专家决策 → 工具 / 补问 / 返回（有界局部循环）
  → 代码核验并装配专家结果
  → 更新有界共享上下文 → 再次协调决策
  → 有真实最终草稿时退出模型循环，进入确认流程
```

协调Agent委派“取得会议要求”，不是预先写死“先list，再get”；日历专家接收业务条件，不是被要求机械执行指定工具列表。模型客户端继续使用结构化JSON行动请求，不依赖当前客户端尚未提供的原生tool_calls接口。

`finish`须核验本场景实际保存的草稿引用、归属和版本；仅有模型文字不得标记完成。不可用/失败不必硬造草稿，保留稳定原因和面向用户的安全说明。

## 5. 上下文共享与状态边界

| 层次 | 内容 | 使用方式 |
| --- | --- | --- |
| 团队共享 | 目标、明确约束、必要结果摘要、问题与稳定引用 | 协调及相关专家按需读取，条数/字节有界 |
| 专家局部 | 本次领域决策状态、工具摘要、必要原文读取 | 仅当前专家使用；原文按需重读，不作为全队Checkpoint载荷 |
| 执行上下文 | tenant/user、run、权限、连接scope、预算、版本和审批 | 代码提供和核验，模型不能改写权限事实 |

根/子图允许不同State，但必须显式做输入输出映射，记录返回字段及合并规则。不同专家对相同字段的更新不能互相覆盖；使用领域结果或委派结果命名空间，避免依赖隐式同名字段传递。

Checkpoint只保留可恢复的局部状态、安全摘要和稳定引用；完整邮件正文、凭据、完整Prompt、未经脱敏响应及思维过程均不得写入。原文只在获准调用的短期输入内使用，恢复时重新读取并核验。

协同上下文不等于长期记忆，不生成或确认记忆记录。协调决策、邮件事实整理默认不注入长期记忆；日历仅沿用阶段7允许的时区及推荐软偏好。删除、过期、撤权后的旧记忆不能因团队恢复再次使用。

## 6. 补问、恢复与确认

### 6.1 专家拥有补问

邮件及日历专家各自执行“准备问题→interrupt→验证回答→继续局部任务”，复用现有InputRequestService。暂不新增或抽取`shared/user_input_workflow.py`。

沿用一个run及其thread_id；记录当前专家、委派ID、问题ID和局部进度。子图暂停导致父团队等待，既有服务在Checkpoint可恢复后再发布可回答事件。用户回答只恢复原专家，不重新走MainAgent或重新开始整个团队。

恢复需核对问题状态、归属、来源版本和权限；重复回答不能重复消费，过期问题不得推进。预算和已执行记录沿用原任务，不因恢复另开额度。中断节点可能重入，因此问题创建、草稿保存和事件写入均须考虑重放幂等。

### 6.2 区分团队结束和任务结束

```text
团队处理中 → 专家等待补问 → 原专家恢复 → 团队继续
团队取得实际草稿 → 模型循环结束 → waiting_confirmation
waiting_confirmation → 用户批准 → 确定性写入 → run完成
waiting_confirmation → 用户拒绝 → 结束且不写日历
```

团队已生成草稿不是run_completed。补充时间/主题不是批准；审批恢复不再次调用MainAgent、CooperationAgent或领域规划。最终写入只读确认版本，继续执行归属、版本锁定与幂等检查。

## 7. 建议目录与分层

以下为计划新增/扩展的位置，不表示文件已经存在；实施时仅建立承担实际职责的文件，不预建空壳。

```text
src/aegis_agent_worker/
├─ agent/
│  ├─ specialists/
│  │  ├─ main_agent.py                    # 现有：路由
│  │  ├─ cooperation_agent.py             # 新增：任务级协调
│  │  ├─ mail_agent.py                    # 扩展现有专家
│  │  └─ calendar_agent.py                # 扩展现有专家
│  ├─ contracts/cooperation/
│  │  ├─ decisions.py
│  │  ├─ messages.py
│  │  └─ context.py
│  ├─ routing/team_registry.py            # 专家与代码入口绑定
│  └─ workflows/
│     ├─ cooperation/
│     │  ├─ factory.py
│     │  ├─ team_workflow.py              # 协调→专家→观察→协调
│     │  ├─ state.py
│     │  └─ context.py                    # 纯装配，不做SQL
│     ├─ mail/
│     │  ├─ ...                            # 原有工作流及factory保持原位
│     │  └─ experts/                       # 仅分离可供团队委派的领域子目标
│     │     ├─ meeting_requirements_workflow.py
│     │     └─ meeting_state.py
│     └─ calendar/
│        ├─ experts/
│        │  ├─ meeting_draft_workflow.py   # 可供团队委派，到站内草稿为止
│        │  └─ state.py
│        └─ approval_workflow.py           # 确定性审批/确认版本写入
├─ service/
│  ├─ cooperation/team_event_service.py    # 安全事件持久化/发布
│  └─ runtime/input_request_service.py     # 现有复用
└─ harness/policies/cooperation.py         # 集中局部控制策略
```

邮件、日历领域仍放各自目录，不整体搬入cooperation；目录名不是路由条件或权限。现有其他文件与入口不在本示意中展开。

Agent负责提示词和结构化语义决策；Workflow负责图控制、局部循环和暂停；Service负责业务事实、事务、草稿及事件；只有新增SQL时才增加Repository。不要把模型包装、持久化和预算重新集中到一个大Service。

CalendarConfirmationWorkflow目前组合查询、草稿、等待确认和写入；8.3应明确复用/拆分边界，使独立会议入口与协同入口共享业务实现，而非复制两个最终写入流程。现有邮件信息收集绑定回复任务，新增会议要求入口应复用基础能力而非伪造回复任务来源。

## 8. 治理、幂等和失败

从8.1首次调用即接入治理，不等到最后才补安全：

- 协调、专家、格式修复与工具调用共享run累计预算；实际调用前由既有Harness占用额度，保留逐attempt用途与usage审计。
- 集中策略覆盖协调/专家轮次、委派次数、共享与局部上下文上限、格式修复和无进展终止。实施时测量正常链路成本后定值，并明确策略版本、恢复兼容与部署顺序。
- 风险上限为只读查询与站内草稿/待确认记录，不包含Agent控制的外部写入。输出Schema、工具参数、scope、对象归属和来源版本都必须经过代码核验。
- 日期、时长、候选时段及邮箱格式由领域校验；记忆偏好不能覆盖用户明确条件。日历Mock仅能证明其所代表用户的空闲，不虚构参会人的空闲。
- 网络、预算、取消、超时、连接及权限错误保留稳定分类，不按格式错误无限修复，也不回退成静默固定工具执行。
- 稳定委派与操作关联用于防重放；崩溃后先查实际草稿/审批状态。外部写入结果不确定时先核实，不重新调用模型猜测成功或盲目重试。
- 不设未经评测的模型confidence安全阈值，不让模型自报“已授权”“已保存”覆盖程序事实。

优先复用现有Checkpoint、run_events和业务表，不预设必须新增协同业务表。若现有结构不足，再提出单独Alembic迁移及兼容测试；此次文档不要求升级或回退数据库。

## 9. 可视过程与接口交接

建议在实施时冻结事件名及payload，以下仅为待定类别，不是当前已上线协议：

| 类别 | 真实触发点 | 安全展示 |
| --- | --- | --- |
| 团队开始/委派 | 已校验任务进入、已接受委派 | 哪位专家处理什么子目标 |
| 专家/工具活动 | 真实节点和工具调用前后 | 查询范围的安全摘要、成功或失败 |
| 等待输入/恢复 | 问题可恢复、回答成功消费 | 需要用户确认什么、由谁继续 |
| 专家结果/草稿就绪 | 已核验输出、草稿实际保存 | 结果摘要、真实Artifact引用 |
| 团队结束/失败 | 模型循环退出或失败 | 生成阶段结果；仍可能等待审批 |

事件由程序分配序号与关联身份，建议关联`run_id`、`delegation_id`、`agent_id`、团队轮次及工具/Artifact引用；模型只能提供经筛选的业务说明，不能伪造事件事实。

复用run_events写库、事务提交后Redis发布、现有SSE回放及去重。Redis通知遗漏可从持久化序号恢复；事件本身不是执行状态源、审批凭证或前端发起额外工具调用的指令。OTel关联实际调用，避免另建消息总线。

不展示隐藏推理或生成虚假的“讨论记录”，只显示实际委派、动作及安全结果。前端可据此展示团队阶段与专家活动，但本计划不实施前端代码。

实施协议变更时，在根目录[第01期接口设计](../../../第01期-接口设计.md)等实际承载协议的接口文档**篇末**追加协同交接章节，不插入原章节中间。注明新增字段/事件向后兼容，提供正常、补问、失败、重连和审批轨迹，区分团队结束与run终态；具体API路径应以既有接口核对后确定。

## 10. 实施顺序与验收矩阵

按主计划8.1→8.7推进：先角色/契约和有治理的运行骨架，再邮件、日历专家局部循环，再真实动态协作与恢复；集中补齐治理回归、可视事件及上线验证。安全核验、幂等和最小事件从首次执行就实施，后置小节不是放行例外。

| 用例 | 核验重点 |
| --- | --- |
| 唯一续约邮件且日历可用 | 实际专家决策与工具轨迹、真实草稿引用 |
| 指定合法邮件/多封/无结果 | 不强制重复查找、不擅选首封、正确暂停或未完成 |
| 时间模糊/无空闲 | 专家补问，未获同意不放宽明确条件 |
| 用户补充、重复回答、重启 | 同run原专家恢复、预算不重置、不重复草稿 |
| 伪造专家/邮件ID/候选/完成产物 | 校验失败，非法外部调用为0 |
| 邮件正文注入、权限撤销、旧版本 | 不执行正文指令、不交付失效内容 |
| 预算/网络/取消/无进展循环 | 有界停止和稳定分类，不静默固定回退 |
| 审批拒绝、通过、版本冲突、重复恢复 | 拒绝不写入；通过仅写确认版本且幂等 |
| SSE重连/通知丢失/事件重放 | 状态与实际一致、可回放去重、不触发业务操作 |
| 原单领域/页面/记忆入口 | 行为不回归，记忆用途边界不扩大 |

测试分层：契约与注册表单测、脚本化LLM决策的图测试、真实持久化/暂停恢复集成测试、受控工具端到端、经确认外发范围后的真实模型评测。不可仅mock最终草稿后宣称专家自主决策已通过。

实验报告记录任务成功、补问与委派轨迹、调用次数、非法调用、重复草稿/写入、平均/P95耗时和Token；缺失usage保持unknown，不预设准确率，不以唯一工具顺序评判模型。安全绕过与未审批写入必须为0。

灰度仅开放本场景；增加新任务时显式补注册能力、领域实现及测试，不让模型通过描述创造能力。上线前列明API/Worker兼容及必要迁移；回退应明确停止协同或提示使用已有单领域入口，不保留旧固定协同链作为隐藏后门。

## 11. 8.1实施记录（历史实施时点）

### 11.1 已落地的代码

- `agent/contracts/cooperation/`：协调动作只含action/expert/goal，不接受DAG、工具、审批或tenant字段；委派ID和序号由代码产生。ExpertResult目前只支持安全摘要、状态和稳定引用，8.2/8.3继续扩展领域结构化要求，不以自由文本摘要替代领域参数校验。
- `agent/specialists/cooperation_agent.py`：强制接收GuardedLLMClient；JSON格式修复最多沿用OutputPolicy允许的一次，修复提示不回显非法响应。用途为cooperation_routing/cooperation_routing_repair，默认无长期记忆注入。
- `agent/routing/team_registry.py`：冻结绑定邮件/日历真实异步图和业务事实核验器。verify_result必须按run归属及业务事实核验；身份/委派不匹配或邮件专家返回日历草稿均拒绝。模型不能注册新参与者。
- `agent/workflows/cooperation/`：准备→协调→委派→专家子图→观察→协调。使用显式输入映射，核验后只保存ExpertResult，不保存专家完整返回State；每轮决策及最终交付前重查历史引用。无固定邮件→日历边表。
- `harness/policies/cooperation.py`：局部轮次、委派数、共享字节和重复结果集中管理。不同专家、格式修复、恢复均沿用同一个run预算，不新增Harness或模型通信实现。
- `service/cooperation/team_event_service.py`：复用run_events及提交后发布协议；仅记录代码阶段、轮次、委派及专家标识，不保存用户目标、邮件正文或原始模型输出。
- 根图增加已注册cooperation_meeting的结束边；MainDecision Schema支持cooperation.mail_calendar_meeting，但仍须注册目录/候选校验通过。MainAgent仅描述该能力存在时才能交接，不负责内部计划。

### 11.2 为什么当前还不能在主对话测试完整场景

当前Worker装配保持不变：没有真实会议要求专家、日历自主草稿专家与独立审批续接，因此不注册生产团队能力。不能用现有邮件回复信息收集或整个日历确认图充当临时专家。

CooperationWorkflowFactory在代码组装时要求两个专家绑定及确定性approval_workflow，缺失即拒绝注册；并要求任务策略开放协作。测试使用编译的专家测试图和审批测试图验证骨架，不将它们注册为生产实现。

`harness-v3-team-runtime`未加入生产PolicyProvider，未扩大conversation工具范围，也未修改harness-v1/v2预算及旧任务语义。8.2加入读取邮件能力、8.3加入日历自主循环时，应测量调用数、确定授权边界，并使用新的发布策略快照，不在旧策略上悄悄增权。局部初值为6轮协调、4次委派、16000字节上下文、2次连续重复结果终止；它们不是经过真实模型成本评测的最终生产额度。

本次不需要数据库迁移，不部署/停止服务，不调用真实邮件、日历、阿里云或Qdrant，不修改前端。下一步按8.2实现真实邮件专家局部循环、领域结果契约和补问服务接入；8.3完成日历及审批边界后再开放生产链路。

### 11.3 验证方式

新增`tests/agent/test_cooperation_runtime.py`覆盖契约、注册表、动态委派、重复委派、真实结果影响后续输入、选择性共享、未知专家/伪造产物拒绝、格式修复及共享预算、局部限制、旧引用失效、嵌套专家interrupt/Command恢复和审批恢复不调用模型。事件测试检查持久化及提交后发布登记协议，不等同于真实Redis/SSE端到端验收。

测试使用脚本化模型返回、内存Checkpoint及数据库替身。真实模型质量、PostgreSQL进程重启恢复、Redis断连和前端展示留在后续阶段验证，不能由骨架测试结果推断已完成。

本次结果：新增39项骨架测试通过；Worker全量623通过、2跳过，`git diff --check`通过。Windows沙箱内Python事件循环的回环socket初始化受限，测试在获准的本地执行环境运行；未访问真实供应商或业务数据库。保留现有LangGraph序列化器的弃用警告，未为本次任务升级依赖。

## 12. 8.2邮件专家实施记录与当前边界

### 12.1 分层与入口

- `agent/contracts/cooperation/mail.py`：邮件会议局部动作、可缺失Facts、完整Requirements及有界时间窗口。动作只有search/read_mail/ask_user/return_requirements/finish_unavailable，不接收工具名、提供商ID、租户、连接或审批字段。
- `agent/specialists/mail_agent.py`：在现有MailAgent新增plan_meeting_information/extract_meeting_information；使用既有结构化输出修复及GuardedLLMClient，没有第二套通信代码。用途mail_meeting_planning/mail_meeting_extraction不允许长期记忆注入。
- `agent/workflows/mail/experts/meeting_requirements_workflow.py`和`experts/meeting_state.py`：专家自己的有界观察/行动循环及补问恢复，不复制邮件回复任务。binding()返回邮件专家注册项，实际绑定此编译子图及业务来源核验器。
- `service/mail/mail_meeting_source_service.py`：元数据查询复用MailQueryService；读取复用具有meeting_requirements用途的MailCollectionService及ToolGateway，来源校验/结果复核不调用LLM。
- `repository/mail_meeting_repository.py`：按邮件ID、tenant/user、来源版本及连接归属、active、有效期、mail.read范围查询，仅返回必要元数据及可信连接，不读正文。

供8.3/8.4组装的内部入口示意（不是新HTTP接口，也不应提前加到生产根图）：

```python
sources = MailMeetingSourceService(
    database,
    MailQueryService(database, runs, tools, events),
    MailCollectionService(database, runs, tools, events, purpose="meeting_requirements"),
)
mail_expert = MailMeetingRequirementsWorkflow(
    mail_agent, sources, input_requests, policies,
).binding()
# 在日历专家和确定性审批续接就绪后，再装入TeamRegistry及CooperationWorkflowFactory。
```

### 12.2 实际决策与防护

模型可先搜索再读取，也可读取团队传入、归属和版本已再次核验的邮件引用。搜索使用现有确定性MailQueryPlan筛选，不能以模型输出冒充工具结果。搜索返回候选被裁剪时仍保留真实matched_count；多封命中，未经用户选择不能默认读取第一封。用户选择按该次保存候选的序号或完整主题解析，不能改用邮箱最新顺序；复杂选择可进一步搜索缩小范围，无法确定时继续澄清，不猜ID。

读取动作使用站内ResourceReference，程序才从SQL快照取得provider_message_id和connection_id，构造ToolInvocation。工作流没有发送/日历动作；Gateway及Harness继续核验工具、连接scope、参数与预算。普通主对话邮件查询仍只返回操作卡片，不因加入团队专家而变成自动摘要/发送入口。

正文读取与要求提取在同一节点局部完成；模型只返回日期窗口、时区、时长、可选主题和参会邮箱。引文须来自邮件或本次用户要求/回答，核对后丢弃；不保存正文或引文到Checkpoint、团队结果及工具审计。超长正文明确返回MAIL_SOURCE_TOO_LARGE，不静默截断后宣称完整。

Requirements必须具有明确时间窗口及15～480分钟时长，窗口不超过62天并能容纳时长，邮箱格式受校验。主题和参会人不作为强制补问条件。时间窗口只是邮件/用户提出的条件，不是日历可用性证明；来源和参数核验也不是对LLM语义提取准确性的数学保证，真实质量仍需固定集与供应商评测。

ExpertResult新增meeting_requirements、error_code、unresolved_fields。成功结果关联唯一来源邮件；用户补充参与时，由Workflow设置user_supplemented=true并在安全摘要中说明，不能将补充要求伪装为邮件原始事实。失败结果不交付可用Requirements，可返回source_mail/time_windows/duration_minutes等稳定未解决项，不回显原文或原始异常。

### 12.3 补问与来源复核

专家本地执行prepare_mail_question→wait_mail_answer→verify_mail_after_answer。问题继续由InputRequestService保存，复用原有ready/claim/consume协议；可选request_id由委派ID与局部轮次派生，使同一准备节点重入复用有效待答问题，已消费/过期/内容变化时拒绝。该增强不新增表字段或迁移，旧调用不传request_id时行为不变。

回答必须匹配question_id，不能以自然语言回答替代批准。恢复保留同一run、委派、专家及累计预算；不是重走MainAgent或重新准备整个团队。所选来源在回答后重新核验SQL权限/版本；已读取邮件再经工具重读，比较安全摘要哈希，连“正文变了但提供商没换版本”也会停止，随后才重新提取补充后的要求。对外返回前再次重读，避免最终交接使用已变化来源。

团队每次调用结果核验器时检查当前SQL来源/连接事实；它不意味着每轮都强制轮询外部邮箱。提供商变动由显式读取或既有同步发现，不能承诺对外部系统瞬时变化有零延迟感知。重读是真实工具调用，同样占预算。

### 12.4 策略、兼容与验证

新增MailMeetingPolicy，默认关闭；harness-v1/v2/v3保持原工具范围及执行额度。新增测试/待组装快照harness-v4-team-mail：conversation增加mail.messages.get，模型16次、工具10次、活跃180秒；局部8轮、10候选、18000字节输入上下文、最多4次补问。没有新增工具执行通道，网络/预算错误不触发格式修复，各个Agent和重读共享原run预算。这是覆盖正常与补问测试轨迹的有界初始配置，未宣称真实成本最优，也未加入生产Provider。

邮件查询改为每次生成唯一mail_query步骤键，避免专家重复搜索违反(run_id,step_key)唯一约束；读取审计新增用途标签，原回复起草用途保留。未修改前端、未迁移业务数据库、未启动服务。生产Worker尚未装配完整团队；下一步为8.3日历专家及独立确定性审批续接。

新增38项8.2测试覆盖真实MailAgent/子图/来源Service/查询与读取服务/ToolGateway（通信和数据库使用替身），包括唯一/多封/空结果、精确主题、已有引用、limit裁剪歧义、补问恢复、错误问题ID、来源版本/正文/撤权、超长正文、伪造ID/引文/动作、注入、重复搜索、预算和局部轮次。真实邮件专家嵌入团队图的中断恢复已验证，协调Agent在专家返回前不会继续。

Worker全量661通过、2跳过；真实PostgreSQL/RLS、Redis/SSE断连、进程重启及供应商质量未在本次验证，不将替身测试当作生产端到端验收。接口变化仅在根接口文档篇末追加。

### 12.5 目录整理（2026-10-20）

仅将可供团队委派的会议要求子图及其局部State移到`agent/workflows/mail/experts/`，同步测试导入；原查询、摘要、回复、修改、发送确认、规划、信息收集和factory均保持原位。不新增tasks/components目录，不改变类名、图节点、工作流标识、注册方式、审批或业务行为。experts是代码组织边界，不是自动授权目录，仍由TeamRegistry显式开放。此次纯目录调整不改变接口或SSE协议，无需前端配合或数据库迁移。

## 13. 8.3日历专家与独立确认续接实施记录

### 13.1 实际代码分层

- `agent/contracts/cooperation/calendar.py`：CalendarTeamDecision仅允许query_availability/ask_user/generate_draft/finish_unavailable；查询选择已核验窗口序号，不能直接给工具、日期或批准字段。纯代码validate_team_proposal复核候选、时长、窗口、明确主题及参会邮箱。
- `agent/specialists/calendar_agent.py`：新增plan_team_meeting和generate_team_meeting_draft，复用原GuardedLLMClient及原草稿生成实现，不增加模型通信层。局部决策有反馈，草稿不是固定取第一个时段。
- `agent/workflows/calendar/experts/meeting_draft_workflow.py`及`state.py`：独立日历专家局部循环，binding()才是可供TeamRegistry开放的入口；原日历工作流保持原位。
- `service/calendar/calendar_expert_service.py`：将允许窗口转成CalendarDecision/ToolInvocation，复用CalendarToolExecutionService和ToolGateway；返回可信候选及当前用户日历核验警告，不读取邮件正文。
- `service/calendar/calendar_draft_service.py`：草稿幂等保存、内容/版本/权限/来源复核、审批准备及Checkpoint就绪后发布。不调用LLM、不执行calendar.create_event。
- `repository/calendar_draft_repository.py`：草稿、审批项与执行记录SQL；原创建草稿/确认项SQL迁入并被旧服务复用，不另写一套外部执行实现。
- `agent/workflows/calendar/approval_workflow.py`：确定性确认续接；原CalendarConfirmationWorkflow保留旧节点标识、委托其wait/finish方法，最终仍调用CalendarConfirmationService.create_approved_event。

### 13.2 查询、补问与记忆

已有邮件Requirements时，模型只选窗口序号，代码保留带偏移时间和明确条件。窗口恰好等于会议时长时，使用list_events做指定区间冲突检查；更大窗口使用find_free_time。一次委派固定查询连接，后续查询/保存不能静默改用另一个日历。重复读取使用唯一步骤键，避免原固定calendar_find_free_time键造成唯一约束冲突。

查询空结果是可观察的领域结果，不必立即终止；专家可查询另一个原有窗口，补问或返回不可用。模型要求生成草稿但没有候选时，不创建产物，只记录no_candidates并受局部/全局预算约束继续决策。

修改明确日期、时长、参会人等条件时，模型只能提出完整proposed_requirements；程序展示完整JSON条件并要求用户明确回复“确认调整”等受支持确认词，才替换有效条件。不能把“直接创建日历”等回答当作批准此新方案，更不能当作最终外部写入批准。过长完整方案不能截断展示后接受确认，返回CALENDAR_CHANGE_TOO_LARGE，后续可拆小修改。普通问题没有proposal时，回答只是后续推理的数据，不能自动更新明确条件；若确需变更，专家仍应提出可展示方案取得确认。

补问由本专家复用InputRequestService及interrupt；恢复核验问题ID、来源版本/权限，清空旧日历候选，再由专家决定如何重新查询。用户可以拒绝调整，原约束保留。没有已有Requirements时，不强制必须先执行邮件步骤；专家可向用户提出并确认完整条件，或返回未完成，不默认造一个时间窗口。

规划沿用calendar_planning用途：只允许已确认的结构化时区；已有Requirements时，以其时区作为明确上下文，避免读取无用时区偏好或重新解释已核验日期。草稿沿用calendar_draft_personalization，仅使用既有结构化软偏好；Guarded调用边界每次修复/恢复重新筛选，联系人、自由文本长期摘要不能参与。记忆不证明可用、不能改变候选或审批。

### 13.3 保存草稿与重放

模型草稿必须引用实际候选，明确主题、时长和邮箱不能被悄悄修改；候选必须落在当前有效窗口内且足以容纳会议。模型调用后再次查询所选实际会议区间，冲突时清除旧候选并返回局部循环，不保存草稿。其他参会人的空闲状态未由当前用户Mock日历证明。

站内草稿采用委派ID派生的稳定UUID，状态为draft，保存时不发布approval_required、不将run改成等待确认、不写外部日历。安全来源/要求/推荐与草稿内容快照放在同事务的cooperation_calendar_draft_saved事件，正文及完整Prompt不保存；业务内容仍在calendar_event_drafts及参会人表。快照归一化UTC时间、邮箱集合和连接ID，用于重放及内容一致性核验。

保存节点重入先读取原草稿，原要求/来源一致时直接复用实际ID和版本，不重新调用模型或创建第二份。版本、内容、来源、连接权限或状态变化时拒绝覆盖；不以模型声称“已保存”代替SQL事实。并发创建由run行锁及稳定主键防重。该幂等边界是一次委派，不等于所有不同委派都必须共享一个草稿。

上述保存事件的元数据用于草稿一致性和恢复，并不授予读取/批准/写入权限；授权仍由SQL归属/连接及真实审批表决定。若未来对run_events实施清理，必须保留有效草稿所依赖的这类恢复元数据或先迁移到专门业务存储；缺失时当前实现拒绝复用，不根据模型或展示事件猜测恢复。

### 13.4 确定性审批与最终写入

团队生成阶段结束后，独立CalendarApprovalWorkflow从实际保存的草稿版本准备确认，不接受模型重新生成参数。它先创建/复用确认项并把草稿改成pending_approval，但run仍是running；进入interrupt、Checkpoint完成保存后，Orchestrator通过approval_readiness调用CalendarDraftService.mark_ready，将任务置为waiting_confirmation并发布原approval_required。

API对日历确认项增加未就绪409守卫，避免新团队链路在检查点尚未保存时接受批准。旧独立日历入口的节点和业务时序保持兼容，其原有等待节点仍共享确定性方法；本次并未宣称旧入口全部检查点窗口都已重构。准备阶段不能把pending确认项的存在当作可恢复状态，前端应等待任务就绪。

批准/拒绝后只执行共享wait/finish，不再调用主Agent、协调Agent或专家。最终写入复核真实批准决定、checked、归属、有效期、确认版本与当前草稿版本，固定草稿原连接。已完成执行返回实际provider_resource_id；存在未完成/不确定执行记录时拒绝盲目重放，需先核对，不能调用LLM猜测成功。拒绝路径无外部写入。SQL创建与最终工具执行期间继续使用原Harness预算、Gateway权限和既有逐项确认接口。

### 13.5 组装边界与验证

新增harness-v5-team-calendar快照，继承16次模型/10次工具/180秒；日历局部8轮、最多10候选、18000字节、4次补问，集中由CalendarTeamPolicy提供。旧版本默认关闭日历团队专家，不改写旧任务预算，不新增环境变量或数据库迁移。

本次通过测试装配两个真实专家及确定性审批子图；正常示例轨迹共10次模型调用、5次只读工具调用后等待确认（最终写服务为替身），不要求所有任务固定采取该顺序。批准/拒绝恢复不增加模型调用。生产Worker尚未装入两个专家、CooperationWorkflowFactory及approval_readiness；8.4进行正式装配、交接/上下文与恢复增强，不能手工改预算版本提前开放。

新增35项日历/双专家/审批测试及1项受限记忆测试，Worker全量697通过/2跳过；API新增2项就绪守卫测试，全量156通过/36跳过。另覆盖查询其他原有窗口、不强制邮件前置步骤及网络故障不做格式重试。测试使用内存Checkpoint、模型/工具/SQL替身及最终写服务替身；另核验最终写SQL的版本/归属/批准谓词与成功/不确定结果防重逻辑。不代表真实数据库RLS、进程重启、Redis回放或真实供应商端到端已验收。前端未修改，接口交接在根接口文档篇末追加。

## 14. 8.4实施记录：生产装配、选择性共享与恢复

### 14.1 装配和策略

`agent/workflows/cooperation/bootstrap.py`是生产组合入口：接收现有MailAgent/CalendarAgent、同一GuardedLLMClient及原查询/输入/确认服务，绑定真实专家子图、TeamRegistry、CooperationAgent和CalendarApprovalWorkflow，返回Factory与审批就绪服务。无网络访问或模型通信实现，持久化仍归Service/Repository。Worker使用此装配函数，不在MainDecisionWorkflow添加内部步骤分支。

新增Worker配置`COOPERATION_ENABLED=false`；true时将Factory加入根图并向主Agent提供cooperation.mail_calendar_meeting目录，同时注入approval_readiness。缺少邮件/日历连接不等于能力消失，实际读取仍必须通过原业务预检、ToolInvocation与Gateway。关闭时不提供团队能力，不回退执行旧固定协同链路。

`harness/bootstrap.py`为新任务选择固定默认策略：

| 团队 | 知识 | 新任务默认 |
| --- | --- | --- |
| 关闭 | 关闭 | harness-v1 |
| 关闭 | 开启 | harness-v2-knowledge-contract |
| 开启 | 关闭 | harness-v5-team-calendar |
| 开启 | 开启 | harness-v6-team-knowledge |

v6仅在v5基础上增加conversation的knowledge.search，其他scope和额度不变；v1/v2/v5/v6均保留解析，旧run按数据库版本恢复，不随当前默认值增权、重置计数或自动换版。v3/v4是此前内部骨架测试快照，不作为生产默认。团队默认16模型/10工具/180秒仍是初值，不代表真实供应商充分预算或成本评测；复杂多次重新委派可能正常耗尽。

### 14.2 按调用共享，而不是广播

协调模型收到有界TeamContext；传给专家前，`cooperation/context.py::expert_input`只保留每个领域最近反馈，显式投影run归属、策略版本、程序生成委派和相关结果。邮件专家只拿邮件引用、安全反馈及未解决项，不拿日历草稿或旧Requirements字段；日历专家额外拿最近邮件已核验Requirements。不把完整根图history、原邮件正文、连接凭据或其他专家工具历史传下去。模型中的反馈标注为数据，不是指令；业务核验器仍复核真实引用。

专家输出仅提取expert_result，再经TeamRegistry事实核验写回协调State；不把整个专家State复制回主图。待解决字段、安全错误码和实际空结果供协调Agent自主决定再次委派或结束。相同请求可以形成“邮件→日历→草稿确认”“邮件→日历无空闲→不可用”“邮件→日历→重新委派邮件核对”等轨迹，无固定步骤配方。旧草稿后发生新委派时，finish必须以最新有效草稿结果收尾，不能掩盖最新未解决结果。

### 14.3 同一任务的补问与确认

专家用原InputRequestService准备问题、interrupt；LangGraph在原run_id/thread_id下保存嵌套Checkpoint。回答通过现有接口/CAS消费，Orchestrator恢复该中断，继续原委派及专家，再回协调循环；不重新调用MainAgent，不新建预算账户或委派ID。每次进入专家（包括恢复）前复核既有结果；邮件每次规划前复核候选读取权限/版本，日历回答后清空旧候选并复核来源。失效时不把旧内容继续交给模型。

草稿阶段结束后，独立确定性审批子图准备确认并中断；Checkpoint保存之后才调用mark_ready发布approval_required。此时run仍waiting_confirmation而非completed。批准恢复增加`CalendarDraftService.validate_approval_resume`，重新核验源邮件、草稿内容/版本及日历写权限，再交唯一最终写服务核验真实批准。拒绝没有写入；已知成功重放直接读取真实执行结果，未知结果仍拒绝盲重试。所有审批恢复均无模型调用。

### 14.4 部署与验证范围

无需新增Alembic迁移；保留阶段7.5及既有业务表。先停止旧Worker，更新带审批未就绪409守卫的API与本Worker，再在Worker环境显式设`COOPERATION_ENABLED=true`并重启；邮件/日历MCP服务、用户连接及读写scope仍需按实际业务准备。不要把测试快照版本手工写入旧预算。启用后不要混跑旧Worker；关闭前先完成或妥善终止等待补问/确认的团队任务，因为关闭开关将不再注册该根图节点。

新增`tests/agent/test_cooperation_integration.py`17项测试：生产装配函数及真实根图/专家/Service/Gateway/Harness，依赖模型、SQL存储、提供商和最终写服务替身。双补问到审批样例共13次模型/5次只读工具，路由一次，委派关系保持，草稿只插入一次；批准/拒绝恢复无模型调用。另覆盖真实空闲反馈改变轨迹、邮件重新委派复用核验引用、恢复期间撤权/改版、旧预算不增权及选择性投影。

Worker全量714通过/2跳过，API全量156通过/36跳过，生产Worker装配模块导入检查通过；存在既有LangGraph序列化弃用警告。测试使用内存Checkpoint，不代表真实PostgreSQL恢复、Redis断连回放、API端到端或真实供应商已验收；这些按8.7执行。8.5集中补齐治理失败语义、8.6对外专家/委派关联与完整协作事件仍待实施。此次不修改前端，接口交接追加在根接口文档第22章。

### 14.5 主路由邮件导航提示边界修正

联调出现“已读取卡片但未明确有效目标”时，该文本来自主决策invalid_card_reference守卫，表明该次最终决策是mail且无有效target/card_reference，而不是专家查日历无结果。原提示“邮件对话只有卡片导航”范围过宽，与已注册的邮件日历协同能力有语义重叠；现明确仅约束单领域邮件任务，跨领域完整目标交给已注册协同入口，由专家处理邮件唯一性与缺失信息。无明确卡片指代时不要求先读卡片，已读卡片也不锁定mail领域；不根据关键词强制改路由，不移除原引用守卫。

补充5项回归覆盖直接协同、读卡后协同、未注册能力拒绝、单邮件无引用仍拒绝，以及带卡片工具的完整根图至等待确认。卡片对象不自动转化为团队来源，仍由专家查询核验。测试使用模型替身，只证明控制流和提示契约，不证明真实模型路由准确率；需重启加载新提示词的Worker，重新提交新任务并按main_decision_completed检查实际domain/intent/workflow及候选目录，不重放已结束旧run。

### 14.6 邮件专家同引用重复读取防护

联调发现同一邮件版本被连续read_mail，每次读取和事实提取都消耗累计工具/模型预算，最终在交接日历前耗尽。样例mock-mail-renewal-001只有续约期限、签约联系人及预计完成时间，没有会议窗口和时长；不能把续约/合同截止时间当作会议时间。现为局部规划显式提供程序生成的read_state（已读引用、missing_fields），区分“未读取”与“已提取但事实缺失”，提示优先补问或返回完整要求。

同一委派重复read_mail相同已读引用时，先SQL复核来源，不再次调用MCP/提取模型：事实缺失则准备InputRequest并暂停；事实已完整却仍重复读取则返回MAIL_MEETING_NO_PROGRESS，交协调Agent决定后续，不伪装成完成。未改变累计预算/额度，不新增全局正文缓存，不规定专家固定工具顺序。回答后和最终交接前的必要来源重读/hash核验仍正常执行并计入预算。

补充3项用例覆盖空事实、缺少时长的重复读取→补问→原任务恢复，以及完整事实重复读取的无进展终止。测试使用模型/SQL/MCP替身，不证明真实模型不会选错邮件或误提取日期；真实外部模型/时间消耗需按8.7测量。已failed的旧run不自动复活，重启新版Worker后提交新任务测试。

## 15. 8.5实施记录：全链路治理与失败语义

### 15.1 额度基线及职责

继续使用不可变v5/v6策略快照，不提高调用额度，不恢复旧固定协同配置。协调6轮/4委派/16000字节/重复2次，两个专家各8轮/10候选/18000字节/4补问，格式修复1次，累计16模型/10工具/180活跃秒仍由PolicyProvider统一提供。暂停期间用户等待不计本次消费耗时；恢复继续累计原记录。

真实代码、依赖替身的代表性样例计数：

| 根入口轨迹 | 模型调用 | 只读工具 | 最终状态 |
| --- | --- | --- | --- |
| 邮件唯一→日历→草稿 | 11 | 5 | waiting_confirmation |
| 邮件选择补问→日历补问→草稿 | 13 | 5 | waiting_confirmation |
| 无空闲→重新委派邮件核对→停止 | 14 | 6 | completed，明确未完成业务目标 |

此为固定替身调用计数，不是供应商耗时、Token价格或所有复杂任务均够用的保证。修复、重读和重试都占额度，当前不因一次失败直接加大额度；真实成本与成功率待8.7测量。

### 15.2 委派预检与实际占用

`HarnessExecutor.check_remaining`→`ExecutionRepository.snapshot`读取按run/tenant/user联结的状态、已固定策略和累计用量；协调Agent委派前及专家执行/嵌套恢复前调用。检查尚有至少一次模型和工具调用空间及活跃时间，不扣次数、不创建attempt，不保证剩余额度足够完成整个专家子目标。

`GuardedLLMClient.check_delegation_capacity`只转交Harness，CooperationAgent不读取SQL。真实LLM/工具仍在Harness.attempt中原子reserve_call：增加归属及agent_runs.running条件，取消或终态不能继续占用。预检通过后状态也可能变化，因此不可省略实际调用检查。旧run不换策略、不清零，专家无独立预算账户。

### 15.3 稳定失败与取消

`harness/failures.py`统一安全attempt分类：LLM_UNAVAILABLE、MODEL_OUTPUT_INVALID、EXECUTION_TIMEOUT、EXECUTION_CANCELLED及工具/Harness原错误码；原始异常内容不写入此审计分类。网络/预算/超时/取消不进入结构修复。进程协程取消继续传播，不能伪装为业务成功。

任务DB状态为cancelled时，后续调用抛RUN_CANCELLED；Orchestrator返回cancelled，不调用失败覆盖。其他非running状态拒绝为EXECUTION_STATE_CHANGED；无法核验状态/版本时拒绝，不自动降级。失败SQL仅允许running→failed，CAS未命中不发布run_failed；最终回复写入前锁定本人run并复核running，取消后不再插入助手回复或发布completed。

无进展比较排除自由措辞summary，使用专家、状态、引用、草稿、结构化要求、错误与未解决项；改变文字不能伪装进展。协调finish_unavailable保留最新领域错误码。`cooperation_progress`增加可选error_code供审计，不公开正文、完整Prompt或推理，不取代run的终态。

### 15.4 写入、重试和保证边界

ToolGateway仍只对允许的只读不可用错误做至多一次额外重试，每次重新占用工具预算；写入及超时不自动重试。新任务不拥有额外工具或审批权限。CalendarWriteError接入既有ToolError收尾，保留无效批准、执行不确定及提供商错误码，不能丢失为泛化AGENT_EXECUTION_FAILED。

审批恢复仍只执行原确认版本的确定性路径，复核归属/版本/来源/有效期和原连接；已知成功读取真实回执，不确定执行记录返回CALENDAR_EXECUTION_UNCERTAIN，先核对业务状态，绝不自动重放外部写入。已有针对伪造目标、非法工具、未批准写入、预算修复绕过及重复恢复的测试继续保留。

检查只能阻止后续调用，不能撤回取消之前已发出的请求；远端可能已执行但本地未收到回执，需按不确定状态处理。没有声称远端exactly-once、强杀进程审计必达、真实并发事务/RLS已验证。治理作用于现有调用入口，无新迁移/配置/预算版本；更新后须重启Worker，未修改前端。接口交接见根接口文档第23章。

### 15.5 验证结果

新增`tests/agent/test_cooperation_governance.py`20项用例：模型/工具/时间剩余额度预检不委派；只读预检不扣次数、调用前取消零实际调用；终态/暂停状态不调用；网络/超时/取消/权限的attempt分类；摘要改写无法绕过无进展；SQL归属和running条件、失败CAS不覆盖终态；取消后不写最终回复；日历写错误保留代码；读重试各自计预算，写入和超时不重试；Orchestrator识别取消不失败覆盖。

Worker全量742通过/2跳过，API全量156通过/36跳过。既有真实图配替身的双补问、恢复、旧预算不增权、非法工具/目标/审批绕过及不确定写入不重放测试继续通过。模型、SQL、提供商及最终写服务依旧为替身；仅验证代码路径和查询谓词，不宣称真实数据库并发已通过。仍存在既有LangGraph序列化弃用警告，无新前端或真实服务启动操作。

## 16. 8.6实施记录：协作事件、SSE与前端交接

### 16.1 真动作和稳定标识

`event/cooperation_context.py`定义受约束CooperationAssociation和执行ContextVar；team_id由run_id派生，委派仍由run_id/sequence派生，actor只允许coordinator/mail/calendar/approval。消息不能自由指定身份；展示关联也不是权限证明。只在专家实际调用范围设置，嵌套恢复重新进入原委派范围，退出即清理，不存服务对象到Checkpoint。

TeamEventService记录真实started/coordinating/delegated/expert_started/expert_completed/draft_ready/finished/unavailable/failed，mark_ready补waiting_input/awaiting_confirmation，原专家消费回答后补resumed。静态label而非模型思维链；专家返回事件只带已核验result_status/references/draft，不复制自由summary。阶段事件用run/phase/round/委派/问题派生cooperation_event_id，run行锁+查询后写入防止Checkpoint重入重复追加；真实工具重试是不同实际调用，不能用此逻辑事件ID合并。

Worker统一append_event时附加当前cooperation关联，工具预览/步骤仍用原事件名和tool_call_id/step_id。append_event采用与API一致的run行锁串行分配event_no，所有推送仍在业务提交后。回滚不发布，Redis失败不丢SQL历史；不承诺远端消息天然exactly-once或真实数据库并发已验收。

### 16.2 补问、刷新和审批

邮件问题按代码状态区分mail_selection/mail_requirements，日历为calendar_requirements；中断载荷仅保存安全关联和类别。Orchestrator拿到已保存中断后交InputRequestService.mark_ready，事务内将问题置ready并发布带cooperation的input_required及waiting_input。只在pending、原归属、waiting_input及未ready时执行，重复就绪不重复发布。没有把“模型准备问”提前当成可回答状态。

API input-requests查询按同run/tenant/question_id的input_required持久事件补回cooperation，旧问题为null；仅用于刷新展示，不改变问题版本、CAS、有效期或回答队列逻辑。回答恢复仍走原run/专家并发布resumed，不广播回答全文。输入不是批准。

团队finished是生成阶段结束，其后仍需Checkpoint保存、确定性审批mark_ready及approval_required。审批关联actor=approval，后续日历写工具也附该标识，不表示新专家决策。最终仍以run_completed/run_failed/run_cancelled关闭任务，草稿事件和finished均不能自动批准或关闭SSE。

### 16.3 SSE数据库权威回放

原Last-Event-ID协议不变。连接先订阅通知再读取SQL历史；后续通知仅唤醒数据库查询，按event_no读取全部未发送事件，而非直接相信Pub/Sub载荷。15秒通知超时也补查，防止丢通知后在线页面永久漏进度；订阅/读取Redis失败后退为15秒SQL轮询。回放SQL同时检查run所属tenant/user，不读取别人的事件。已发送序号去重，忽略数据库查询之外的伪造终态；客户端也需按run_id/event_no幂等处理。

数据库断开、授权失效等异常不伪造进度；客户端依现有连接重试/任务查询恢复。刷新后若未保留本地时间线，应从0回放，而非仅保留游标导致没有初始展示。不能把SSE展示当成业务授权或工具触发入口。

### 16.4 交接与部署边界

根接口文档篇末第24章定义字段、示例及UI映射，前端同事负责时间线/角色、补问标题与恢复展示。当前页面的“起草任务”不会由本次后端改动自动变更；缺字段的旧任务回退通用“需要补充信息”，不靠解析自然语言猜角色。无新增HTTP路径、环境变量或Alembic迁移，更新并重启API和Worker；原Checkpoint无关联时保留兼容路径，不补造历史事件。

新增事件服务测试覆盖逻辑阶段重入去重、提交后发布/回滚不发布、补问就绪及scope隔离、非法关联；完整双补问图额外验证question_id/专家/问题类别传递。API测试覆盖通知丢失/伪造通知、SQL有序补发、真实run终态关闭、Last-Event-ID、Redis不可用仍补历史及刷新关联SQL归属。均使用SQL/Redis/模型替身，实际服务故障、进程重启和前端端到端按8.7另行验收。

最终验证：新增10项Worker事件用例、5项API用例，Worker全量752通过/2跳过，API全量161通过/36跳过；仍有既有LangGraph序列化弃用警告。未启动或修改真实服务、未运行迁移、未改前端。

## 17. 结构化邮件单选补问（8.6A）

8.6的input_kind是展示语义，不足以让用户直接点选。现在增加独立的input_type=text/single_choice和options契约；邮件多封匹配且尚未明确选择时，由代码构造有界、版本绑定的候选快照并保存到run_input_requests。ID由question_id与规范化邮件引用派生，标签限制主题/发件人/时间长度，不复制正文，不让LLM产生选项。

InputRequestService.prepare校验选项类型/数量/唯一性/大小及绑定，稳定问题ID重入要求快照完全一致；mark_ready在Checkpoint保存后发布公开id/label，不发布私有reference。API刷新查询同样只投影公开字段。数据库快照是业务恢复事实，不依赖SSE回传或临时字符串解析。

InputAnswerParam只允许response或selected_option_id之一。API校验单选属于当前问题、状态/版本/有效期和邮件SQL归属/来源版本/连接scope；通过后保存selected_option_id，队列仍只有run_id/question_id。Worker消费时核验快照绑定，专家按Checkpoint快照直接定位并复核来源，不再使用LLM或序号正则来解释新单选。明确选择后禁止模型搜索替换/读取其他候选，返回MAIL_SELECTION_MISMATCH；模型仍自主决定在所选邮件内读取、提取、补问和返回。

旧问题迁移默认text，旧Checkpoint中无input_type时保留原文本导航；新single_choice提交文本明确拒绝，不能悄悄回退。开放式时长/日期问题仍为text。前端按input_type渲染控件、input_kind渲染标题；单选不默认首项，不提交内部邮件ID，不把选项当审批。

新增021_structured_input_choices迁移及约束，无新配置或预算版本。先停止旧API/Worker写入、备份并升级021，再统一部署新版API/Worker与支持单选的前端；前端此次未实现，协议在根接口文档篇末第25章。未接入时可直接用API选择ID联调，旧文本表单不能回答新单选；降级需先清理活跃单选任务，删除新列将丢失选择快照。此次未迁移真实数据库。

测试覆盖选项快照校验/重入冲突、事件不暴露私有映射、只消费一次、篡改映射拒绝、跨问题/伪造ID与文本提交拒绝、邮件权限/版本复核、幂等、旧文本Checkpoint和完整团队恢复。迁移只做DDL代码检查，不等于真实数据库升级/并发/RLS已验收。

新增12项Worker测试、13项API/迁移测试；Worker全量764通过/2跳过，API174通过/36跳过。Alembic heads确认021_structured_input_choices为唯一head；未执行upgrade或真实端到端。

## 18. 邮件专家失败恢复与无进展止损（2026-10-21）

- 规划/输出校验/选择冲突等局部失败可在不可用结果中保留唯一已选邮件引用；不携带正文、旧提取事实或用户回答。来源变化、撤权等失败不保留引用。MailMeetingSourceService在注册表边界回PostgreSQL核验该引用，下一次委派prepare再次核验；恢复唯一来源后锁定目标，不允许模型搜索替换。引用不代表访问授权，也不代表邮件要求已完成。缺少的业务补充需再次询问，不把旧回答伪造为邮件事实。
- 同一委派已执行的结构化查询仅保存哈希；重复查询在MCP调用前返回MAIL_MEETING_SEARCH_NO_PROGRESS。不同改写连续无候选，按MailMeetingPolicy.max_empty_searches（默认2）停止，不等到8轮耗尽；新Checkpoint字段缺失时使用空历史兼容旧任务。已有非空查询结果不会使重复查询重新获得调用机会。
- 最新专家结果不可用/失败、且没有中间的新专家结果或用户输入时，禁止立即再次委派同一专家；直接返回原稳定错误码。delegate节点增加停止分支，保证停止后不进入execute_expert。成功结果的合法后续委派、其他专家提供新结果后的重派仍可进行。不靠换delegation重置局部额度，不增加Harness预算。
- expert_completed协作事件增加可选error_code与unresolved_fields，来自已校验ExpertResult；不增加自由文本summary或原始模型响应。前端交接见根接口文档篇末第26章；此次不改前端。
- 不回写已失败run，不自动重放已有工具调用，不修改审批、权限或幂等规则。部署新版Worker后新发起任务验证；已终止任务不会因更新代码自动恢复。本次不需要新迁移，结构化选择题仍依赖既有021迁移。

验证：新增5项本地回归，并更新重复搜索/重复失败委派的旧预期；覆盖引用复核与撤权、合法来源重入成功、重复查询零额外工具调用、不同空查询的集中策略上限、无新信息重派零专家调用、安全失败事件。Worker全量769通过/2跳过；未调用真实供应商，未重跑原失败run，不宣称真实端到端成功。原任务第一次不可用的具体原因仍需在新版事件中确认，不以末尾ROUND_LIMIT反推模型或服务故障。

## 19. 邮件模型输出的安全诊断与有界修复（2026-10-21）

保留MODEL_OUTPUT_INVALID稳定分类，ExpertResult增加可选output_stage、validation_issues、repair_count。邮件下一步决策/事实提取/补问恢复重提取的捕获点显式填写阶段，不从工具次数猜测。旧结果默认无诊断兼容；诊断只允许邮件模型失败且字段完整，成功结果不能冒充失败诊断。

公共OutputValidationIssue契约限定字段路径和错误类型；llm/output_validation.py只从Pydantic取loc/type，不读取msg/input/ctx，未知路径替换field、最多8条，未知类型归schema_invalid。MailAgent对引文不匹配/缺引文/全文复制标题使用固定类型，不能将异常消息或非法输出写入日志、Checkpoint、团队结果或修复提示。

_validated_json每次校验失败记录mail_model_validation_failed安全日志（purpose、尝试序号、路径/类型）。修复提示追加相同诊断，便于修正字段而不是盲目重试；最大修复次数仍取OutputPolicy.max_repairs，每次通信仍经过GuardedLLMClient/Harness。网络/超时/预算故障不按格式错误重试，不放宽Schema、引文、权限或审批校验。日志中的尝试序号从1开始，结果repair_count为额外修复次数。

expert_completed将安全诊断投影至SSE，接口文档篇末第27章；前端不在本次范围。无新迁移/模型配置/预算版本，重启Worker加载；旧任务未记录的原因无法追溯恢复。此次不宣称已定位旧run的确切非法字段，也不以诊断补齐代替真实供应商端到端验收。

验证：新增6项诊断回归，覆盖提取与决策阶段、缺引文/引文不匹配、非法字段名与值脱敏、修复提示/事件的安全字段投影、实际修复次数与累计预算、旧结果兼容。既有网络/预算不重试及补问恢复回归通过；Worker全量775通过/2跳过。未执行真实模型调用或修改数据库，未改前端。

## 20. 参会人Schema与邮件选择恢复集成回归（2026-10-21）

只读检查run b1905ead-6157-4abc-afdb-c364e534c3f4的安全事件，确认失败为mail_meeting_extraction/facts.attendees/value_error、已修复1次；该历史事件未保存原始输出，不能据此断定是显示名还是重复地址。测试A两个纯邮箱本身可通过校验。

原attendees只暴露字符串数组，格式和去重约束隐藏在validator，且两类失败都用value_error。本次将AttendeeEmail的pattern及纯邮箱说明写入模型JSON Schema，数组声明uniqueItems=true，提取提示明确不含姓名/括号、不拼接多个地址、同一邮箱仅一次。代码仍只规范化纯地址的首尾空白与大小写；不自动解析显示名、不静默去重，不从签名推定参会人。无参会人仍可为空数组。

新增invalid_attendee_email与duplicate_attendee_email固定诊断，可进入既有有界修复/安全日志/专家结果/SSE，不包含邮箱值。无新模型调用通道、预算、迁移或前端修改；接口交接追加至篇末第28章。重启Worker后新任务使用新契约。

验证：新增9项回归，覆盖Schema显式格式/唯一性、纯地址规范化、显示名/多地址拼接拒绝、大小写重复拒绝、两类错误的一次修复及累计预算，以及测试A两个参会人在多邮件选择恢复后的真实根图/双专家/草稿保存/等待审批链路；审批前写入为0。Worker全量784通过/2跳过。模型、SQL及工具为受控替身，未做真实供应商端到端或自动恢复旧任务。

## 21. 动作Schema对齐、日历诊断及真实链路验证（2026-10-21）

用户授权真实DeepSeek调用、代选和Mock日历批准写入后，以隔离队列运行生产Worker装配。先复现邮件下一步规划根级校验失败，再对齐MailMeetingDecision/CalendarTeamDecision的oneOf动作互斥Schema及最小JSON提示；动作参数不匹配诊断细分为action_parameter_mismatch。日历团队规划改用complete_json_prepared，仍复用GuardedLLMClient/Harness和调用前记忆复核。

日历补问恢复必须重查候选；未明确要求全员空闲时，无法查他人日历不再作为强制补问理由，草稿仍保留未核验警告。规划/草稿失败携带安全阶段calendar_team_planning/calendar_team_draft及路径、类型、实际修复次数，日志及一次有界修复不携带原始输出。ExpertResult按专家/阶段/错误码组合校验，旧结果仍兼容。

成功run=d1ff146b-5480-40c1-803d-c19038f44771：真实邮件多选一→双专家→草稿Checkpoint→代批准→一次calendar.create_event→completed；持久化事件mock-event-523caf28-6c68-51e2-b6d6-4678d9704935，为2026-10-26 09:00–09:50两人会议。12次模型、6次工具；未增加预算或迁移，普通Worker未被重启，需用户重启加载新代码。测试事件保留，不清空日历或修改旧失败run。

Worker791通过/2跳过，API174通过/36跳过。脚本scripts/run_cooperation_live_smoke.py为显式外发/费用/Mock写入的人工授权工具，不是自动化上线门禁；真实模型仅该样例验证，前端/SSE浏览器/并发/进程重启等仍待8.7。完整证据及限制见23号实验报告；接口协议追加在根文档第29章，前端本次未修改。
