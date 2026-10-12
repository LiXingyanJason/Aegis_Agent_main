"""统一邮件领域决定的动作边界、目标保持和双入口测试。"""

import json
from uuid import UUID
import pytest

from aegis_agent_worker.agent.contracts.mail import MailDecision
from aegis_agent_worker.agent.contracts.decision import AgentHandoff, ResourceReference
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
from aegis_agent_worker.agent.workflows.mail.planning_workflow import MailPlanningWorkflow, finish_mail_planning

TARGET = ResourceReference(resource_type="email", id=UUID(int=10), source_version="v1")


class Client:
    provider_name = "test"
    model_name = "test"
    def __init__(self, response):
        self.response, self.calls = response, 0
    async def complete(self, messages):
        self.calls += 1
        assert "MailDecision" in messages[0].content
        return json.dumps(self.response)


def state(**updates):
    value = dict(run_id=str(UUID(int=1)), tenant_id=str(UUID(int=2)), user_id=str(UUID(int=3)),
                 conversation_id=str(UUID(int=4)), run_type="conversation",
                 latest_user_message="为这封邮件起草回复",
                 handoff=AgentHandoff(domain="mail", intent="draft_mail_reply",
                                     instruction="正式语气", target=TARGET).model_dump(mode="json"))
    value.update(updates)
    return value


@pytest.mark.parametrize("response", [
    {"action": "send", "target": TARGET.model_dump(mode="json")},
    {"action": "query", "query": {"action": "list"}, "target": {**TARGET.model_dump(mode="json"), "source_version": None}},
    {"action": "extract"},
    {"action": "clarify", "clarification_question": "哪封？", "target": TARGET.model_dump(mode="json")},
    {"action": "extract", "target": TARGET.model_dump(mode="json"), "approved": True},
])
def test_mail_decision_rejects_unknown_action_or_invalid_combination(response):
    """发送、伪造批准、缺失引用和互斥字段不能成为领域计划。"""
    with pytest.raises(ValueError):
        MailDecision.model_validate(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"action": "extract", "target": TARGET.model_dump(mode="json")},
    {"action": "draft_reply", "target": {**TARGET.model_dump(mode="json"), "id": str(UUID(int=99))}},
    {"action": "draft_reply", "target": {**TARGET.model_dump(mode="json"), "source_version": "v2"}},
])
async def test_mail_agent_cannot_change_intent_target_or_version(response):
    """主 Agent 已核验的动作范围、邮件 ID 和来源版本必须保持。"""
    agent = MailAgent(Client(response))
    with pytest.raises(ValueError):
        await agent.plan_mail("起草回复", AgentHandoff(domain="mail", intent="draft_mail_reply",
                                                     instruction="起草", target=TARGET))


@pytest.mark.asyncio
async def test_conversation_generation_is_blocked_before_model_call():
    """对话不能直接生成摘要或回复，只提示点击卡片，不读取正文或调用模型。"""
    client = Client(dict(action="draft_reply", target=TARGET.model_dump(mode="json"), instruction="使用正式语气"))
    result = await MailPlanningWorkflow(MailAgent(client), "draft_mail_reply").build().ainvoke(state())
    assert client.calls == 0
    assert result["mail_decision"]["action"] == "clarify"
    assert "点击" in result["mail_planning_error"]
    assert "body" not in result["mail_decision"]


@pytest.mark.asyncio
async def test_page_task_constructs_same_contract_without_llm_call():
    """页面任务动作固定且目标来自业务请求表，不额外花费模型分类调用。"""
    class Tasks:
        async def load_planning_reference(self, run):
            assert run.run_type == "mail_extraction"
            return {"target": TARGET.model_dump(mode="json"), "instruction": ""}
    client = Client({"action": "send"})
    result = await MailPlanningWorkflow(MailAgent(client), "extract_mail", Tasks()).build().ainvoke(
        state(run_type="mail_extraction", conversation_id="", handoff={}))
    assert client.calls == 0
    assert result["mail_decision"]["action"] == "extract"


@pytest.mark.asyncio
async def test_clarification_blocks_execution_and_background_errors_are_not_success():
    """信息不足返回澄清；后台参数错误应失败，不能被当成普通对话完成。"""
    client = Client({"action":"clarify", "clarification_question":"请明确回复要求"})
    result = await MailPlanningWorkflow(MailAgent(client), "draft_mail_reply").build().ainvoke(state())
    assert "点击" in result["mail_planning_error"]
    assert "点击" in finish_mail_planning(result)["assistant_content"]
    with pytest.raises(RuntimeError):
        finish_mail_planning({"run_type":"mail_extraction", "mail_planning_error":"引用失效"})
