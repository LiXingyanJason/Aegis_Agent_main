"""记忆用途白名单与有界额度；未知用途默认拒绝个性化。"""
from dataclasses import dataclass
from aegis_agent_worker.agent.contracts.memory import CALENDAR_MEMORY_KEYS


@dataclass(frozen=True, slots=True)
class MemoryContextPolicy:
    """记忆上下文额度设置"""
    version: str = 'memory-contract-v1'
    max_items: int = 5
    max_item_chars: int = 400
    max_total_chars: int = 1500
    max_content_bytes: int = 6000
    # 没有可信Token估算时不伪装成0；若配置Token预算则未知估算拒绝使用。
    max_estimated_tokens: int | None = None
    candidate_limit: int = 50
    calendar_version: str = 'calendar-memory-v1'
    ranking_version: str = 'memory-rank-v1'
    max_query_terms: int = 32
    max_task_chars: int = 4000
    max_audit_items: int = 50
    strong_term_matches: int = 2
    recall_timeout_seconds: int = 2

    def version_for(self,purpose):
        return self.calendar_version if purpose in CALENDAR_MEMORY_KEYS else self.version

    def types_for(self, purpose: str) -> frozenset[str]:
        # 不允许模型/Manifest扩大此白名单。事实抽取、知识回答、审批和主路由无记忆。
        if purpose in {'conversation_reply', 'mail_reply_draft', 'mail_draft_revision'}:
            return frozenset({'long_term_summary', 'entity'})
        if purpose in CALENDAR_MEMORY_KEYS:
            return frozenset({'entity'})
        return frozenset()

    def entity_keys_for(self, purpose):
        if purpose in CALENDAR_MEMORY_KEYS:
            return CALENDAR_MEMORY_KEYS[purpose]
        if purpose=='conversation_reply':
            return frozenset({'timezone','contact','reply_style','meeting_preferences'})
        return frozenset({'timezone','contact','reply_style'})

    def item_limit_for(self,purpose):
        return min(self.max_items,len(CALENDAR_MEMORY_KEYS[purpose])) if purpose in CALENDAR_MEMORY_KEYS else self.max_items

    def __post_init__(self):
        if not self.version or len(self.version) > 64:
            raise ValueError('记忆策略版本无效')
        if not self.calendar_version or len(self.calendar_version)>64:
            raise ValueError('日历记忆策略版本无效')
        if not self.ranking_version or len(self.ranking_version)>64:
            raise ValueError('记忆排名策略版本无效')
        for name,maximum in (('max_query_terms',64),('max_task_chars',12000),('max_audit_items',50),
                              ('strong_term_matches',64),('recall_timeout_seconds',10)):
            if type(getattr(self,name)) is not int or not 1<=getattr(self,name)<=maximum:
                raise ValueError('记忆排名策略额度无效')
        for name, maximum in (('candidate_limit',500),('max_items', 50), ('max_item_chars', 4000),
                              ('max_total_chars', 200000), ('max_content_bytes', 800000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('记忆策略额度无效')
        if self.max_item_chars > self.max_total_chars:
            raise ValueError('单条额度不能超过总额度')
        if self.max_estimated_tokens is not None and (
            type(self.max_estimated_tokens) is not int or self.max_estimated_tokens < 1
        ):
            raise ValueError('记忆Token额度无效')
