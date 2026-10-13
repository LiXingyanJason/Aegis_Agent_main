"""阶段4.3：主决策JSON修复、预算、安全澄清与邮件序号引用。"""
from uuid import UUID

import pytest

from aegis_agent_worker.agent.contracts.decision import ConversationCapability, MainDecision
from aegis_agent_worker.agent.specialists.main_agent import MainAgent, MainDecisionOutputError
from aegis_agent_worker.agent.workflows.main.decision_workflow import MainDecisionWorkflow
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.llm.client import LLMError, LLMOutputError


CAPS = (
    ConversationCapability(domain="mail", intent="query_mail", workflow="mail_query", description="邮件卡片"),
    ConversationCapability(domain="calendar", intent="arrange_meeting", workflow="calendar_confirmation", description="会议"),
)


class Client:
    provider_name = model_name = "test"
    def __init__(self, values):
        self.values, self.calls = iter(values), []
    async def complete(self, messages):
        raise AssertionError("应使用JSON模式")
    async def complete_json(self, messages):
        self.calls.append(messages)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


class Service:
    def __init__(self, references=(), current=True):
        self.references, self.current, self.audits = list(references), current, []
    async def load_references(self, run):
        return self.references
    async def validate_reference(self, run, reference):
        if not self.current or reference.source_version != "v1":
            return None
        return reference.model_dump(mode="json")
    async def record_decision(self, run, **kwargs):
        self.audits.append(kwargs)


def state(message="帮我处理任务"):
    return dict(run_id=str(UUID(int=1)), tenant_id=str(UUID(int=2)), user_id=str(UUID(int=3)),
                conversation_id=str(UUID(int=4)), run_type="conversation", latest_user_message=message)


async def infer(client, message="帮我处理任务", service=None):
    service = service or Service()
    class Tools:
        async def execute(self, run, request):
            return {"card_id": str(UUID(int=90)), "query_id": str(UUID(int=91)), "status": "valid",
                    "items": [{"email_id": r["id"], "source_version": r["source_version"],
                        "position": r.get("position"), "status": "valid"} for r in service.references
                        if r.get("position")]}
        async def resolve(self, run, reference, results):
            r = next((r for r in service.references if r.get("position") == reference.position), None)
            if r is None: return None
            from aegis_agent_worker.agent.contracts.decision import ResourceReference
            return await service.validate_reference(run, ResourceReference.model_validate({
                key: r[key] for key in ("resource_type", "id", "source_version")}))
    graph = MainDecisionWorkflow(MainAgent(client), service, CAPS, context_tools=Tools()).build()
    return await graph.ainvoke(state(message)), service


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "我要开始操作", "", "```json", "[]", '{"decision":"send"}',
    '{"decision":"reply","confidence":0.99}',
    '{"decision":"delegate","domain":"mail","intent":"query_mail","instruction":"查", "target":{"id":"invalid"}}',
    LLMOutputError("响应为空"),
])
async def test_invalid_output_repairs_once_with_safe_diagnostics(invalid):
    """普通文字、空响应、非法字段等最多一次修复，成功后才允许交接。"""
    client = Client([invalid, '{"decision":"delegate","domain":"mail","intent":"query_mail","instruction":"查看邮件"}'])
    result, service = await infer(client)
    assert result["workflow"] == "mail_query" and len(client.calls) == 2
    assert service.audits[0]["repair_count"] == 1
    assert service.audits[0]["validation_issue_codes"]
    assert client.calls[1][-1].role == "system"


@pytest.mark.asyncio
async def test_repair_prompt_and_final_error_never_contain_invalid_output_or_unknown_field():
    """非法输出及未知字段名可能携带敏感文本，只使用安全错误类型／字段路径。"""
    secret = "PRIVATE_SECRET"
    client = Client(['{"decision":"reply","PRIVATE_SECRET":"payload"}', "PRIVATE_SECRET"])
    result, service = await infer(client)
    assert result["workflow"] == "clarify" and not result["handoff"] and len(client.calls) == 2
    assert service.audits[0]["outcome"] == "invalid_output"
    assert secret not in client.calls[1][-1].content
    assert secret not in str(service.audits[0]["validation_issue_codes"])


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [LLMError("网络故障"), HarnessError("EXECUTION_BUDGET_EXCEEDED", "预算耗尽")])
async def test_network_and_budget_errors_are_not_repaired(error):
    """网络和预算错误维持原分类，不当成输出错误重试。"""
    client = Client([error])
    with pytest.raises(type(error)):
        await infer(client)
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_output_repair_uses_guarded_client_budget():
    """生产包装器在第二次修复请求前检查累计预算，不允许绕过上限。"""
    from dataclasses import replace
    from aegis_agent_worker.harness.executor import HarnessExecutor
    from aegis_agent_worker.harness.policy import TaskExecutionPolicy
    from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
    from test_harness_react_revision import Database, RUN
    client = Client(["非法输出", '{"decision":"reply"}'])
    db = Database()
    harness = HarnessExecutor(db, replace(TaskExecutionPolicy(), max_models=1))
    guarded = GuardedLLMClient(client, harness)
    async with harness.scope(RUN):
        with pytest.raises(HarnessError) as error:
            await MainAgent(guarded).decide(message="任务", history=[], capabilities=[], references=[])
    assert error.value.code == "EXECUTION_BUDGET_EXCEEDED" and len(client.calls) == 1
    assert db.budgets[RUN.id]["model_calls"] == 1


def reference(id=9, position=1):
    return {"resource_type": "email", "id": str(UUID(int=id)), "source_version": "v1", "position": position}


def mail_target(id=9, version="v1"):
    return '{"decision":"delegate","domain":"mail","intent":"query_mail","instruction":"查询",' + \
        f'"target":{{"resource_type":"email","id":"{UUID(int=id)}","source_version":"{version}"}}}}'


@pytest.mark.asyncio
@pytest.mark.parametrize("refs,output,current,expected", [
    ([reference()], mail_target(), True, "mail_query"),
    ([reference()], mail_target(99), True, "clarify"),
    ([reference()], mail_target(version="v0"), True, "clarify"),
    ([reference()], mail_target(), False, "clarify"),
    # 自然语言序号由模型理解，不再让正则替模型解析；结构化card_reference选中第一位置。
    ([reference(), reference(10, 2)], mail_target(10).replace('"instruction":"查询"',
        '"instruction":"查询","card_reference":{"card_id":"'+str(UUID(int=90))+'","query_id":"'+str(UUID(int=91))+'","position":1}'), True, "clarify"),
    ([reference()], '{"decision":"delegate","domain":"mail","intent":"query_mail","instruction":"查询"}', True, "clarify"),
])
async def test_ordinal_must_match_recent_card_and_current_version(refs, output, current, expected):
    """序号不能解析为另一张有效卡片、伪造目标或过期快照；缺target不能重新查列表替代。"""
    read = '{"action":"read_context","tool_name":"conversation.cards.get","arguments":{"selector":"latest"}}'
    result, _ = await infer(Client([read, output]), "查看第一封邮件", Service(refs, current))
    assert result["workflow"] == expected
    assert bool(result["handoff"]) == (expected == "mail_query")


@pytest.mark.asyncio
@pytest.mark.parametrize("refs", [[], [{"resource_type": "email", "id": str(UUID(int=9)), "source_version": "v1"}]])
async def test_missing_card_reference_never_uses_inbox_fallback(refs):
    """模型按需读卡片后，空卡片和无position引用均不冒充第一封。"""
    read = '{"action":"read_context","tool_name":"conversation.cards.get","arguments":{"selector":"latest"}}'
    client = Client([read, mail_target()])
    result, _ = await infer(client, "查看第一封邮件", Service(refs))
    assert result["workflow"] == "clarify" and len(client.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["查看主题为第一封续约通知的邮件", "查看《第一封续约通知》邮件"])
async def test_exact_subject_remains_queryable_without_target(message):
    """标题里的序号不是卡片指代，完整主题允许交给查询子图，不要求已有ID。"""
    output = '{"decision":"delegate","domain":"mail","intent":"query_mail","instruction":"按完整主题查询"}'
    result, _ = await infer(Client([output]), message)
    assert result["workflow"] == "mail_query" and result["handoff"]["target"] is None


@pytest.mark.asyncio
async def test_meeting_without_subject_or_attendees_still_delegates():
    """会议主题／参会人可选，不因缺失而改变现有日历交接语义。"""
    output = '{"decision":"delegate","domain":"calendar","intent":"arrange_meeting","instruction":"明天下午安排30分钟会议"}'
    result, _ = await infer(Client([output]), "明天下午安排30分钟会议")
    assert result["workflow"] == "calendar_confirmation"
