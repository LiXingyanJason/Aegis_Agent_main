"""记忆额度通过PolicyProvider集中获取，不改变旧执行预算。"""
from dataclasses import FrozenInstanceError, replace

import pytest

from aegis_agent_worker.harness.policies.memory import MemoryContextPolicy
from aegis_agent_worker.harness.policy_provider import LEGACY_POLICY, KNOWLEDGE_CONTRACT_POLICY, PolicyProvider


@pytest.mark.parametrize('changes', [
    {'max_items':True}, {'max_items':0}, {'max_items':51},
    {'max_item_chars':4001}, {'max_total_chars':399}, {'max_content_bytes':0},
    {'max_estimated_tokens':0}, {'max_estimated_tokens':True}, {'version':''},
])
def test_invalid_limits(changes):
    with pytest.raises(ValueError):
        replace(MemoryContextPolicy(), **changes)


def test_provider_exposes_frozen_memory_contract_policy_without_execution_budget_change():
    provider = PolicyProvider((LEGACY_POLICY, KNOWLEDGE_CONTRACT_POLICY))
    for version in (LEGACY_POLICY.version, KNOWLEDGE_CONTRACT_POLICY.version):
        profile = provider.resolve(version)
        assert profile.memory == MemoryContextPolicy()
        assert profile.execution.max_models == 10 and profile.execution.max_tools == 6
        with pytest.raises(FrozenInstanceError):
            profile.memory.max_items = 100
    with pytest.raises(ValueError):
        replace(LEGACY_POLICY, memory={})
