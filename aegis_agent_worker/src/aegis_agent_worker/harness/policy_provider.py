"""不可变执行策略快照；生产默认值由harness.bootstrap选择，旧run沿用持久化版本。"""
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.policies.execution import TaskExecutionPolicy
from aegis_agent_worker.harness.policies.workflow import MainContextPolicy, MailCollectionPolicy
from aegis_agent_worker.harness.policies.output import OutputPolicy
from aegis_agent_worker.harness.policies.memory import MemoryContextPolicy
from aegis_agent_worker.harness.policies.session_memory import SessionMemoryPolicy
from aegis_agent_worker.harness.policies.cooperation import CooperationPolicy, MailMeetingPolicy, CalendarTeamPolicy


@dataclass(frozen=True)
class PolicyBundle:
    execution: TaskExecutionPolicy = field(default_factory=TaskExecutionPolicy)
    main_context: MainContextPolicy = field(default_factory=MainContextPolicy)
    mail_collection: MailCollectionPolicy = field(default_factory=MailCollectionPolicy)
    output: OutputPolicy = field(default_factory=OutputPolicy)
    memory: MemoryContextPolicy = field(default_factory=MemoryContextPolicy)
    session_memory: SessionMemoryPolicy = field(default_factory=SessionMemoryPolicy)
    cooperation: CooperationPolicy = field(default_factory=CooperationPolicy)
    mail_meeting: MailMeetingPolicy = field(default_factory=MailMeetingPolicy)
    calendar_team: CalendarTeamPolicy = field(default_factory=CalendarTeamPolicy)
    def __post_init__(self):
        for name, kind in (("execution", TaskExecutionPolicy), ("main_context", MainContextPolicy),
                           ("mail_collection", MailCollectionPolicy), ("output", OutputPolicy),
                           ("memory", MemoryContextPolicy), ("session_memory", SessionMemoryPolicy),
                           ("cooperation", CooperationPolicy), ("mail_meeting", MailMeetingPolicy),
                           ("calendar_team", CalendarTeamPolicy)):
            if not isinstance(getattr(self, name), kind):
                raise ValueError(f"策略包{name}类型无效")
    @property
    def version(self): return self.execution.version


# 已发布版本必须固定全部值。以后调整默认值应新增版本，不能修改此快照。
LEGACY_POLICY = PolicyBundle(
    calendar_team=CalendarTeamPolicy(enabled=False, max_rounds=8, max_candidates=10,
        max_context_bytes=18000, max_answers=4),
    mail_meeting=MailMeetingPolicy(enabled=False, max_rounds=8, max_candidates=10,
        max_context_bytes=18000, max_answers=4),
    cooperation=CooperationPolicy(enabled=False, max_rounds=6, max_delegations=4,
        max_context_bytes=16000, max_repeated_results=2),
    session_memory=SessionMemoryPolicy(version="session-work-v1",ttl_seconds=1800,max_user_sessions=32,
        max_tasks=6,max_cards=4,max_cache_bytes=12000,history_items=20,history_chars=12000,
        message_chars=4000,redis_timeout_seconds=1.0),
    # 独立记忆额度；7.4统一调用边界启用，不改变harness-v1执行预算。
    memory=MemoryContextPolicy(version="memory-contract-v1", max_items=5, max_item_chars=400,
        max_total_chars=1500, max_content_bytes=6000, max_estimated_tokens=None,candidate_limit=50,
        calendar_version='calendar-memory-v1',ranking_version='memory-rank-v1',max_query_terms=32,
        max_task_chars=4000,max_audit_items=50,strong_term_matches=2,recall_timeout_seconds=2),
    execution=TaskExecutionPolicy(version="harness-v1", max_models=10, max_tools=6, active_seconds=120,
        max_input_chars=48000, max_output_chars=80000, model_timeout_seconds=60, tool_timeout_seconds=20,
        tool_scopes=(
            ("conversation", frozenset({"calendar.list_events", "calendar.find_free_time", "calendar.create_event",
                                       "mail.messages.list", "conversation.cards.list", "conversation.cards.get"})),
            ("mail_extraction", frozenset({"mail.messages.get"})),
            ("mail_reply_draft", frozenset({"mail.messages.get", "mail.messages.list", "calendar.list_events", "calendar.find_free_time"})),
            ("mail_draft_revision", frozenset()), ("mail_send", frozenset({"mail.messages.send"})),
        )),
    main_context=MainContextPolicy(max_reads=2, max_rounds=3, max_context_bytes=24000),
    mail_collection=MailCollectionPolicy(max_rounds=5, max_evidence_bytes=12000),
    output=OutputPolicy(max_repairs=1, read_tool_retries=1, card_result_bytes=12000,
        tool_result_bytes=2_000_000, mail_source_chars=12000, repair_response_chars=12000,
        history_items=8, history_chars=12000, user_message_chars=4000),
)


# 新能力必须新增版本，不改写已发布harness-v1；只在知识入口显式启用时使用。
KNOWLEDGE_CONTRACT_POLICY = replace(LEGACY_POLICY, execution=replace(
    LEGACY_POLICY.execution, version="harness-v2-knowledge-contract",
    tool_scopes=tuple((kind, tools | frozenset({"knowledge.search"}) if kind == "conversation" else tools)
                      for kind, tools in LEGACY_POLICY.execution.tool_scopes),
))

# 8.1运行骨架专用，尚未加入生产Provider；不扩大旧任务工具范围或预算。
COOPERATION_CONTRACT_POLICY = replace(
    LEGACY_POLICY,
    execution=replace(LEGACY_POLICY.execution, version="harness-v3-team-runtime"),
    cooperation=CooperationPolicy(enabled=True, max_rounds=6, max_delegations=4,
        max_context_bytes=16000, max_repeated_results=2),
)

# 8.2真实邮件专家专用；仍不加入生产Provider，8.3完成后再组装完整入口。
COOPERATION_MAIL_POLICY = replace(
    COOPERATION_CONTRACT_POLICY,
    execution=replace(COOPERATION_CONTRACT_POLICY.execution, version="harness-v4-team-mail",
        max_models=16, max_tools=10, active_seconds=180,
        tool_scopes=tuple((kind, tools | frozenset({"mail.messages.get"}) if kind == "conversation" else tools)
                          for kind, tools in COOPERATION_CONTRACT_POLICY.execution.tool_scopes)),
    mail_meeting=MailMeetingPolicy(enabled=True, max_rounds=8, max_candidates=10,
        max_context_bytes=18000, max_answers=4),
)

# 8.3建立的完整团队快照；8.4显式启用时供新任务使用，不修改旧任务快照。
COOPERATION_CALENDAR_POLICY = replace(
    COOPERATION_MAIL_POLICY,
    execution=replace(COOPERATION_MAIL_POLICY.execution, version="harness-v5-team-calendar"),
    calendar_team=CalendarTeamPolicy(enabled=True, max_rounds=8, max_candidates=10,
        max_context_bytes=18000, max_answers=4),
)

# 8.4生产组合快照：知识与团队可同时启用，不改写v1～v5或丢失knowledge.search。
COOPERATION_KNOWLEDGE_POLICY = replace(
    COOPERATION_CALENDAR_POLICY,
    execution=replace(COOPERATION_CALENDAR_POLICY.execution, version="harness-v6-team-knowledge",
        tool_scopes=tuple((kind, tools | frozenset({"knowledge.search"}) if kind == "conversation" else tools)
                          for kind, tools in COOPERATION_CALENDAR_POLICY.execution.tool_scopes)),
)



class PolicyProvider:
    def __init__(self, profiles=None, default_version="harness-v1"):
        profiles = tuple(profiles) if profiles is not None else (LEGACY_POLICY,)
        registered = {profile.version: profile for profile in profiles}
        if len(registered) != len(profiles): raise ValueError("策略版本重复")
        self._profiles = MappingProxyType(registered)
        self._default_version = default_version
        self.resolve(default_version)
    @property
    def default_version(self): return self._default_version
    def resolve(self, version):
        try: return self._profiles[version]
        except KeyError:
            raise HarnessError("POLICY_VERSION_UNAVAILABLE", "任务原策略版本不可用，请恢复该策略或重新提交任务") from None
    def current(self, state=None):
        from aegis_agent_worker.harness.context import peek_execution
        runtime = peek_execution()
        profile = runtime.policy_bundle if runtime is not None else self.resolve(self.default_version)
        pinned = (state or {}).get("execution_policy_version")
        if pinned is not None and pinned != profile.version:
            if runtime is not None:
                raise HarnessError("POLICY_VERSION_CONFLICT", "检查点策略版本与任务记录不一致")
            profile = self.resolve(pinned)
        return profile


DEFAULT_POLICIES = PolicyProvider()
