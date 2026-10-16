"""计划／证据／答案／来源的确定性契约测试。"""
import pytest
from pydantic import ValidationError
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan, KnowledgeAnswer, KnowledgeSearchResult


@pytest.mark.parametrize("payload", [
    {"query":" "}, {"query":"规则", "top_k": True}, {"query":"规则", "top_k": 6},
    {"query":"规则", "tenant_id":"other"}, {"query":"规则", "user_id":"other"},
    {"query":"规则", "filters":{"acl":"all"}}, {"query":"规则", "filters":{"category":"policy"}},
    {"query":"规则", "query_rewrites":["a","b","c"]}, {"query":"规则", "query_rewrites":["a","a"]},
])
def test_plan_rejects_unsafe_or_unbounded_fields(payload):
    with pytest.raises(ValidationError):
        KnowledgeQueryPlan.model_validate(payload)


@pytest.mark.parametrize("values", [
    {"answer":"内容[1]", "citation_numbers":[True], "can_confirm":True},
    {"answer":"内容", "citation_numbers":[1], "can_confirm":True},
    {"answer":"内容[2]", "citation_numbers":[1], "can_confirm":True},
    {"answer":"没有依据", "citation_numbers":[], "can_confirm":True},
    {"answer":"内容[1]", "citation_numbers":[1], "can_confirm":False},
])
def test_answer_citations_must_match(values):
    with pytest.raises(ValidationError):
        KnowledgeAnswer.model_validate(values)


def test_unknown_number_cannot_be_verified_without_evidence():
    answer = KnowledgeAnswer(answer="内容[1]", citation_numbers=[1], can_confirm=True)
    with pytest.raises(ValueError):
        answer.verify_sources([])


def test_empty_result_and_plan_invocation_are_explicit():
    plan = KnowledgeQueryPlan(query=" 续约条件 ")
    assert plan.to_invocation().tool_name == "knowledge.search"
    assert plan.to_invocation().arguments["query"] == "续约条件"
    assert KnowledgeSearchResult().evidence == []
