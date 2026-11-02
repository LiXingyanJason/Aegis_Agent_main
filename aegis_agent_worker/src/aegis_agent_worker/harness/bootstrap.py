"""按可信启动配置选择新任务默认策略；历史任务仍按数据库中的版本恢复。"""
from aegis_agent_worker.harness.policy_provider import (
    PolicyProvider, LEGACY_POLICY, KNOWLEDGE_CONTRACT_POLICY,
    COOPERATION_CALENDAR_POLICY, COOPERATION_KNOWLEDGE_POLICY,
    with_skills,with_rerank,with_generations,
)


def create_worker_policy_provider(*, knowledge_enabled=False, cooperation_enabled=False, skills_enabled=False,rerank_enabled=False,rerank_fallback_allowed=False,generations_enabled=False):
    if cooperation_enabled:
        selected = COOPERATION_KNOWLEDGE_POLICY if knowledge_enabled else COOPERATION_CALENDAR_POLICY
    else:
        selected = KNOWLEDGE_CONTRACT_POLICY if knowledge_enabled else LEGACY_POLICY
    # 注册固定历史快照不等于给旧run增权；Harness按已持久化的policy_version解析。
    profiles=(LEGACY_POLICY, KNOWLEDGE_CONTRACT_POLICY, COOPERATION_CALENDAR_POLICY, COOPERATION_KNOWLEDGE_POLICY)
    all_profiles=(*profiles,*(with_skills(profile) for profile in profiles))
    chosen=with_skills(selected) if skills_enabled else selected
    registered=(*all_profiles,*(with_rerank(profile) for profile in all_profiles),
                          *(with_rerank(profile,fallback=True) for profile in all_profiles))
    if rerank_enabled and knowledge_enabled:chosen=with_rerank(chosen,fallback=rerank_fallback_allowed)
    return PolicyProvider((*registered,*(with_generations(profile) for profile in registered)),
        with_generations(chosen).version if generations_enabled and knowledge_enabled else chosen.version)
