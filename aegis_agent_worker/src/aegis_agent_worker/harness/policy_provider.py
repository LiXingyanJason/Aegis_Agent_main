"""当前生产使用的执行限制集中在 LEGACY_POLICY，版本为 harness-v1，由 PolicyProvider 提供"""
from dataclasses import dataclass, field
from types import MappingProxyType

from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.policies.execution import TaskExecutionPolicy
from aegis_agent_worker.harness.policies.workflow import MainContextPolicy, MailCollectionPolicy
from aegis_agent_worker.harness.policies.output import OutputPolicy


@dataclass(frozen=True)
class PolicyBundle:
    execution: TaskExecutionPolicy = field(default_factory=TaskExecutionPolicy)
    main_context: MainContextPolicy = field(default_factory=MainContextPolicy)
    mail_collection: MailCollectionPolicy = field(default_factory=MailCollectionPolicy)
    output: OutputPolicy = field(default_factory=OutputPolicy)
    def __post_init__(self):
        for name, kind in (("execution", TaskExecutionPolicy), ("main_context", MainContextPolicy),
                           ("mail_collection", MailCollectionPolicy), ("output", OutputPolicy)):
            if not isinstance(getattr(self, name), kind):
                raise ValueError(f"策略包{name}类型无效")
    @property
    def version(self): return self.execution.version


# 已发布版本必须固定全部值。以后调整默认值应新增版本，不能修改此快照。
LEGACY_POLICY = PolicyBundle(
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
