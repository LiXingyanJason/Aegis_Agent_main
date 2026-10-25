"""按可信启动配置选择新任务默认策略；历史任务仍按数据库中的版本恢复。"""
from aegis_agent_worker.harness.policy_provider import (
    PolicyProvider, LEGACY_POLICY, KNOWLEDGE_CONTRACT_POLICY,
    COOPERATION_CALENDAR_POLICY, COOPERATION_KNOWLEDGE_POLICY,
    with_skills,
)


def create_worker_policy_provider(*, knowledge_enabled=False, cooperation_enabled=False, skills_enabled=False):
    if cooperation_enabled:
        selected = COOPERATION_KNOWLEDGE_POLICY if knowledge_enabled else COOPERATION_CALENDAR_POLICY
    else:
        selected = KNOWLEDGE_CONTRACT_POLICY if knowledge_enabled else LEGACY_POLICY
    # 注册固定历史快照不等于给旧run增权；Harness按已持久化的policy_version解析。
    profiles=(LEGACY_POLICY, KNOWLEDGE_CONTRACT_POLICY, COOPERATION_CALENDAR_POLICY, COOPERATION_KNOWLEDGE_POLICY)
    return PolicyProvider((*profiles,*(with_skills(profile) for profile in profiles)),
        with_skills(selected).version if skills_enabled else selected.version)
